from __future__ import annotations

import importlib
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import urlsplit

import pytest
import requests
import trakt.core
import trakt.movies
import trakt.sync
from requests.adapters import BaseAdapter

from plextraktsync.queue.TraktBatchWorker import TraktBatchWorker
from plextraktsync.queue.TraktMarkWatchedWorker import TraktMarkWatchedWorker
from plextraktsync.queue.TraktScrobbleWorker import TraktScrobbleWorker
from plextraktsync.trakt.PkceAuth import TraktAuth, save_json
from plextraktsync.trakt.TraktApi import TraktApi


@pytest.fixture
def core(tmp_path, monkeypatch):
    browser = tmp_path / "browser-token.json"
    save_json(browser, {"access_token": "test-session-token", "client_id": "test-session-client"})
    save_json(
        tmp_path / "trakt-pkce.json",
        {
            "access_token": "test-pkce-token",
            "refresh_token": "test-refresh-token",
            "client_id": "test-pkce-client",
            "redirect_uri": "https://callback.example/oauth",
            "expires_at": time.time() + 3600,
        },
    )
    auth = TraktAuth(browser, tmp_path)
    auth.select("pkce")
    calls = []

    class OfflineAdapter(BaseAdapter):
        def send(self, request, **kwargs):
            calls.append((request.method, urlsplit(request.url).path, request.headers["Authorization"], request.headers["trakt-api-key"]))
            response = requests.Response()
            response.status_code = 200
            response.request = request
            path = urlsplit(request.url).path
            data = {"username": "test-user"} if path == "/users/me" else [] if request.method == "GET" else {}
            response._content = json.dumps(data).encode()
            return response

        def close(self):
            pass

    session = requests.Session()
    session.mount("https://", OfflineAdapter())
    # PKCE must work without enabling the captured browser-token environment setting.
    config = {"TRAKT_BROWSER_TOKEN_FILE": None, "TRAKT_USERNAME": None}
    factory = SimpleNamespace(config=config, session=session, print=Mock(), has_plex_token=True)
    monkeypatch.setattr("plextraktsync.trakt.TraktApi.factory", factory)
    monkeypatch.setattr("plextraktsync.trakt.TraktApi.config_dir", str(tmp_path))
    trakt.core.api.cache_clear()
    api = TraktApi()
    factory.trakt_api = api
    login = importlib.import_module("plextraktsync.commands.trakt_login")
    monkeypatch.setattr(login, "factory", factory)
    monkeypatch.setattr(login, "config_dir", str(tmp_path))
    yield SimpleNamespace(api=api, auth=auth, calls=calls, factory=factory, login=login, root=tmp_path)
    session.close()
    trakt.core.api.cache_clear()


@pytest.mark.parametrize(
    "method,token,client",
    [
        ("pkce", "test-pkce-token", "test-pkce-client"),
        ("browser", "test-session-token", "test-session-client"),
    ],
)
def test_sync_and_watch_workers_use_selected_auth(core, method, token, client):
    core.auth.select(method)
    assert core.api.external_auth
    assert core.api.me.username == "test-user"
    _ = core.api.watchlist_movies
    _ = core.api.watchlist_shows
    _ = core.api.movie_collection
    _ = core.api.watched_movies
    _ = core.api.get_ratings("movies")
    core.api.rate(trakt.movies.Movie("Synthetic", 2000, ids={"trakt": 1}), 8)
    items = {"movies": [{"ids": {"trakt": 1}}]}
    batch = TraktBatchWorker()
    for operation in ("add_to_collection", "remove_from_collection", "add_to_watchlist", "remove_from_watchlist"):
        getattr(batch, operation)(items)
    TraktMarkWatchedWorker().add_to_history(items)
    media = SimpleNamespace(to_json_singular=lambda: {"movie": {"ids": {"trakt": 1}}})
    scrobbler = trakt.sync.Scrobbler(media, 0, "test", None)
    watch = TraktScrobbleWorker()
    watch.scrobble(scrobbler, "update", 20)
    watch.scrobble(scrobbler, "stop", 95)
    assert {path for _, path, _, _ in core.calls} >= {
        "/users/me",
        "/users/test-user/watchlist/movies",
        "/users/test-user/watchlist/shows",
        "/users/test-user/collection/movies",
        "/users/test-user/watched/movies",
        "/users/test-user/ratings/movies",
        "/sync/collection",
        "/sync/collection/remove",
        "/sync/watchlist",
        "/sync/watchlist/remove",
        "/sync/history",
        "/sync/ratings",
        "/scrobble/start",
        "/scrobble/stop",
    }
    assert all(auth == "Bearer " + token and app == client for _, _, auth, app in core.calls)


def test_first_sync_login_uses_pkce_without_device_secret_prompt(core, monkeypatch):
    class Config(dict):
        save = Mock()

    config = Config(core.factory.config)
    core.factory.config = config
    monkeypatch.setattr(core.login, "trakt_authenticate", Mock(side_effect=AssertionError("Device login must not run")))
    ensure = importlib.import_module("plextraktsync.commands.login")
    monkeypatch.setattr(ensure, "factory", core.factory)
    ensure.ensure_login()
    assert config["TRAKT_USERNAME"] == "test-user"
    config.save.assert_called_once()
    assert core.login.has_trakt_token()
    assert core.calls[0][2:] == ("Bearer test-pkce-token", "test-pkce-client")


def test_cli_method_selection_verifies_account_and_saves_username(core, monkeypatch):
    class Config(dict):
        save = Mock()

    core.factory.config = Config(core.factory.config)
    monkeypatch.setattr(core.login, "trakt_authenticate", Mock(side_effect=AssertionError("Device login must not run")))
    core.login.trakt_login(method="browser")
    assert core.factory.config["TRAKT_USERNAME"] == "test-user"
    assert core.auth.method() == "browser"
    assert core.calls[-1][2:] == ("Bearer test-session-token", "test-session-client")


def test_account_save_preserves_pkce_and_vpn_config(tmp_path, monkeypatch):
    from plextraktsync.config.Config import Config

    values = {
        "TRAKT_PKCE_CLIENT_ID": "test-pkce-client",
        "TRAKT_PKCE_REDIRECT_URI": "https://callback.example/oauth",
        "TRAKT_PKCE_ALLOW_LOCAL_HTTP": "false",
        "TRAKT_VPN_ENABLED": "true",
        "TRAKT_VPN_PROXY_URL": "http://vpn:8080",
        "TRAKT_VPN_CONTROL_URL": "http://vpn:9090",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    config = Config()
    config.env_file = str(tmp_path / ".env")
    config.config_yml = str(tmp_path / "config.yml")
    config.config_file = str(tmp_path / "config.json")
    config.initialize()
    config["TRAKT_USERNAME"] = "test-user"
    config.save()
    saved = (tmp_path / ".env").read_text()
    assert all(key + "=" + value in saved for key, value in values.items())
    assert all(key not in config.serialize() for key in values)


def test_cli_pkce_exchange_verifies_account_without_browser_login(core, monkeypatch):
    from urllib.parse import parse_qs

    class Config(dict):
        save = Mock()

    core.factory.config = Config(core.factory.config)
    monkeypatch.setenv("TRAKT_PKCE_CLIENT_ID", "test-pkce-client")
    browser_before = (core.root / "browser-token.json").read_bytes()

    def approve(_prompt):
        params = parse_qs(urlsplit(core.factory.print.call_args.args[0]).query)
        return "https://callback.example/oauth?code=test-code&state=" + params["state"][0]

    def exchange(url, **kwargs):
        assert url == "https://auth.trakt.tv/oauth/token"
        assert kwargs["json"]["grant_type"] == "authorization_code"
        assert kwargs["json"]["code"] == "test-code"
        assert "client_secret" not in kwargs["json"]
        return SimpleNamespace(
            status_code=200,
            json=lambda: {
                "access_token": "test-new-pkce-token",
                "refresh_token": "test-new-refresh-token",
                "created_at": time.time(),
                "expires_in": 3600,
            },
        )

    monkeypatch.setattr(core.login.Prompt, "ask", approve)
    monkeypatch.setattr("plextraktsync.trakt.TraktTransport.post", exchange)
    monkeypatch.setattr(core.login, "trakt_authenticate", Mock(side_effect=AssertionError("Device login must not run")))
    core.login.trakt_login(method="pkce", redirect_uri="https://callback.example/oauth")
    assert core.auth.method() == "pkce"
    assert core.factory.config["TRAKT_USERNAME"] == "test-user"
    assert core.calls[-1][2:] == ("Bearer test-new-pkce-token", "test-pkce-client")
    assert (core.root / "browser-token.json").read_bytes() == browser_before
