"""PKCE behavior tests; also runnable directly without the wider pytest suite."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

from click import ClickException
from requests import Request, Response

from plextraktsync.trakt.PkceAuth import PkceAuth, TraktAuth, save_json

spec = importlib.util.spec_from_file_location("pkce_bridge", Path(__file__).parents[1] / "browser-helper/list_bridge.py")
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)
SECRET = "test-bridge-secret-at-least-24-characters"
CALLBACK = "http://192.168.1.100:8090/oauth/callback"


class PkceTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch

        client_env = patch.dict(os.environ, {"TRAKT_PKCE_CLIENT_ID": "test-pkce-client-id"})
        client_env.start()
        self.addCleanup(client_env.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.browser = self.root / "browser.json"
        save_json(self.browser, {"access_token": "session-token", "client_id": "session-client"})
        self.calls = []
        self.auth = TraktAuth(self.browser, self.root, post=self.post)

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"access_token": "pkce-token", "refresh_token": "rotated-refresh", "expires_in": 604800, "created_at": time.time()},
        )

    def connect(self):
        url = self.auth.start(CALLBACK, allow_local_http=True)
        params = parse_qs(urlsplit(url).query)
        self.auth.finish("authorization-code", params["state"][0])
        return params

    def test_pkce_login_preserves_session_and_sends_no_secret(self):
        self.connect()
        self.assertEqual(self.auth.method(), "pkce")
        self.assertEqual(self.auth.read()["access_token"], "pkce-token")
        url, kwargs = self.calls[0]
        self.assertEqual(url, "https://auth.trakt.tv/oauth/token")
        self.assertNotIn("client_secret", kwargs["json"])
        self.assertFalse(kwargs["allow_redirects"])
        self.auth.select("browser")
        self.assertEqual(self.auth.read()["access_token"], "session-token")
        self.assertTrue(self.auth.pkce.path.exists())

    def test_challenge_and_unique_attempts(self):
        first = parse_qs(urlsplit(self.auth.start(CALLBACK, allow_local_http=True)).query)
        pending = json.loads(self.auth.pending.read_text())
        expected = base64.urlsafe_b64encode(hashlib.sha256(pending["verifier"].encode()).digest()).decode().rstrip("=")
        self.assertEqual(first["code_challenge"], [expected])
        self.assertEqual(first["code_challenge_method"], ["S256"])
        second = parse_qs(urlsplit(self.auth.start(CALLBACK, allow_local_http=True)).query)
        self.assertNotEqual(first["state"], second["state"])
        with self.assertRaises(ClickException):
            self.auth.finish("code", first["state"][0])
        self.assertFalse(self.calls)

    def test_rejected_state_expiration_and_replay(self):
        params = parse_qs(urlsplit(self.auth.start(CALLBACK, allow_local_http=True)).query)
        with self.assertRaises(ClickException):
            self.auth.finish("code", "wrong-state")
        data = json.loads(self.auth.pending.read_text())
        data["expires_at"] = time.time() - 1
        save_json(self.auth.pending, data)
        with self.assertRaises(ClickException):
            self.auth.finish("code", params["state"][0])
        params = self.connect()
        with self.assertRaises(ClickException):
            self.auth.finish("code", params["state"][0])
        self.assertEqual(len(self.calls), 1)

    def test_http_requires_explicit_local_opt_in(self):
        with self.assertRaises(ClickException):
            self.auth.start(CALLBACK)
        for uri in (
            "http://example.com/oauth/callback",
            "urn:ietf:wg:oauth:2.0:oob",
            "https://example.com/callback?code=x",
            "https://user:pass@example.com/callback",
        ):
            with self.assertRaises(ClickException):
                self.auth.start(uri, allow_local_http=True)
        self.auth.start("https://example.com/oauth/callback")
        self.auth.start("http://localhost:8090/oauth/callback", allow_local_http=True)

    def test_refresh_rotates_once_across_auth_instances(self):
        self.connect()
        data = json.loads(self.auth.pkce.path.read_text())
        data["expires_at"] = time.time() - 1
        save_json(self.auth.pkce.path, data)
        self.calls.clear()
        instances = [PkceAuth(self.auth.pkce.path, post=self.post) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda auth: auth.read(), instances))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["json"]["grant_type"], "refresh_token")
        self.assertEqual(results[0]["refresh_token"], "rotated-refresh")
        self.assertTrue(all(item == results[0] for item in results))
        self.assertEqual(json.loads(self.auth.pkce.path.read_text()), results[0])
        self.assertEqual(os.stat(self.auth.pkce.path).st_mode & 0o777, 0o600)

    def test_refresh_failure_keeps_method_and_session(self):
        self.connect()
        data = json.loads(self.auth.pkce.path.read_text())
        data["expires_at"] = time.time() - 1
        save_json(self.auth.pkce.path, data)
        self.auth.pkce.post = lambda *args, **kwargs: SimpleNamespace(status_code=400)
        with self.assertRaises(ClickException):
            self.auth.read()
        self.assertEqual(self.auth.method(), "pkce")
        self.assertEqual(json.loads(self.browser.read_text())["access_token"], "session-token")
        self.assertEqual(json.loads(self.auth.pkce.path.read_text()), data)

    def test_selected_credentials_on_read_and_write_and_host_guard(self):
        self.connect()
        for method, token in (("pkce", "pkce-token"), ("browser", "session-token")):
            self.auth.select(method)
            for verb in ("GET", "POST"):
                prepared = Request(verb, "https://api.trakt.tv/sync/history", auth=self.auth).prepare()
                self.assertEqual(prepared.headers["Authorization"], "Bearer " + token)
        with self.assertRaises(ClickException):
            Request("GET", "https://example.com", auth=self.auth).prepare()
        response = Response()
        response.status_code = 401
        with self.assertRaisesRegex(ClickException, "not replayed"):
            PkceAuth.check_response(response)

    def app(self, **kwargs):
        return bridge.create_app(
            self.root, self.browser, SECRET, pkce_redirect_uri=CALLBACK, auth_post=self.post, allow_local_http=True, **kwargs
        ).test_client()

    def test_bridge_protects_selection_and_start_but_accepts_valid_callback(self):
        client = self.app()
        self.assertEqual(client.post("/api/auth/pkce/start", json={}).status_code, 403)
        headers = {"X-Bridge-Key": SECRET}
        result = client.post("/api/auth/pkce/start", json={}, headers=headers)
        params = parse_qs(urlsplit(result.json["url"]).query)
        self.assertEqual(client.get("/oauth/callback?code=x&state=wrong").status_code, 400)
        result = client.get("/oauth/callback", query_string={"code": "code", "state": params["state"][0]})
        self.assertEqual(result.status_code, 200)
        status = client.get("/api/auth", headers=headers).json
        self.assertEqual(status["method"], "pkce")
        self.assertTrue(status["pkce_connected"])
        self.assertNotIn("pkce-token", json.dumps(status))
        self.assertEqual(client.get("/oauth/callback", query_string={"code": "code", "state": params["state"][0]}).status_code, 400)
        result = client.post("/api/auth/method", json={"method": "browser"}, headers=headers)
        self.assertEqual(result.json["method"], "browser")
        self.assertEqual(client.post("/api/auth/method", json=["pkce"], headers=headers).status_code, 400)

    def test_feeds_use_both_methods_and_keep_saved_feeds(self):
        used = []

        def get(url, **kwargs):
            prepared = Request("GET", url, auth=kwargs["auth"]).prepare()
            used.append(prepared.headers["Authorization"])
            return SimpleNamespace(status_code=200, headers={}, json=lambda: [{"movie": {"title": "Example", "ids": {"tmdb": 42}}}])

        client = self.app(get=get)
        headers = {"X-Bridge-Key": SECRET}
        feed = {"name": "test-feed", "media": "movies", "source": "trending", "limit": 10}
        for connected in (False, True):
            if connected:
                self.connect()
            result = client.post("/api/preview/cards", json=feed, headers=headers)
            self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(used, ["Bearer session-token", "Bearer pkce-token"])

    def test_browser_capture_updates_do_not_overwrite_pkce(self):
        import sys
        from unittest.mock import patch

        with patch.object(sys, "path", [str(Path(__file__).parents[1] / "browser-helper"), *sys.path]):
            from capture_trakt_token import save_token
        self.connect()
        saved_pkce = self.auth.pkce.path.read_bytes()
        save_token(self.browser, "renewed-session", "renewed-session-client")
        self.assertEqual(self.auth.method(), "pkce")
        self.assertEqual(self.auth.read()["access_token"], "pkce-token")
        self.assertEqual(self.auth.pkce.path.read_bytes(), saved_pkce)
        self.auth.select("browser")
        self.assertEqual(self.auth.read()["access_token"], "renewed-session")
        self.auth.select("pkce")
        self.assertEqual(self.auth.read()["access_token"], "pkce-token")

    def test_inactive_method_cannot_break_active_method(self):
        self.connect()
        self.browser.write_text("corrupted session")
        self.assertEqual(self.auth.read()["access_token"], "pkce-token")
        self.auth.select("browser")
        with self.assertRaises(ClickException):
            self.auth.read()
        self.auth.select("pkce")
        save_json(self.browser, {"access_token": "session-token", "client_id": "session-client"})
        self.auth.select("browser")
        self.auth.pkce.path.write_text("corrupted pkce")
        self.assertEqual(self.auth.read()["access_token"], "session-token")
        with self.assertRaises(ClickException):
            self.auth.select("pkce")
        self.assertEqual(self.auth.method(), "browser")
        self.assertEqual(self.auth.read()["access_token"], "session-token")

    def test_failed_reconnect_keeps_both_credentials(self):
        self.connect()
        pkce_bytes, browser_bytes = self.auth.pkce.path.read_bytes(), self.browser.read_bytes()
        self.auth.select("browser")
        url = self.auth.start(CALLBACK, allow_local_http=True)
        self.auth.pkce.post = lambda *a, **kw: SimpleNamespace(status_code=400)
        with self.assertRaises(ClickException):
            self.auth.finish("bad-code", parse_qs(urlsplit(url).query)["state"][0])
        self.assertEqual(self.auth.method(), "browser")
        self.assertEqual(self.auth.pkce.path.read_bytes(), pkce_bytes)
        self.assertEqual(self.browser.read_bytes(), browser_bytes)

    def test_refresh_is_not_called_when_browser_selected(self):
        self.connect()
        data = json.loads(self.auth.pkce.path.read_text())
        data["expires_at"] = 1
        save_json(self.auth.pkce.path, data)
        self.auth.select("browser")
        self.calls.clear()
        self.assertEqual(self.auth.read()["access_token"], "session-token")
        self.assertEqual(self.calls, [])
        self.auth.select("pkce")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(json.loads(self.browser.read_text())["access_token"], "session-token")

    def test_shared_selection_survives_new_process_and_restart(self):
        import subprocess
        import sys

        self.connect()
        code = (
            "from plextraktsync.trakt.PkceAuth import TraktAuth;import sys;"
            "a=TraktAuth(sys.argv[1],sys.argv[2]);print(a.method(),a.read()['access_token'])"
        )
        for method, token in (("pkce", "pkce-token"), ("browser", "session-token"), ("pkce", "pkce-token")):
            self.auth.select(method)
            result = subprocess.run([sys.executable, "-c", code, str(self.browser), str(self.root)], capture_output=True, text=True, check=True)
            self.assertEqual(result.stdout.strip(), method + " " + token)

    def test_refresh_serializes_across_processes(self):
        import multiprocessing

        self.connect()
        data = json.loads(self.auth.pkce.path.read_text())
        data["expires_at"] = 1
        save_json(self.auth.pkce.path, data)
        calls = self.root / "refresh-calls"

        def worker():
            def post(url, **kwargs):
                with calls.open("a") as handle:
                    handle.write("refresh\n")
                return self.post(url, **kwargs)

            PkceAuth(self.auth.pkce.path, post=post).read()

        context = multiprocessing.get_context("fork")
        processes = [context.Process(target=worker) for _ in range(6)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        self.assertEqual(calls.read_text().splitlines(), ["refresh"])

    def test_startup_readiness_preserves_browser_monitor_and_allows_pkce(self):
        ready_spec = importlib.util.spec_from_file_location("pkce_auth_ready", Path(__file__).parents[1] / "browser-helper/auth_ready.py")
        module = importlib.util.module_from_spec(ready_spec)
        ready_spec.loader.exec_module(module)
        self.assertFalse(module.auth_ready(self.browser, self.root, self.root))
        (self.root / "auth-ready").touch()
        self.assertTrue(module.auth_ready(self.browser, self.root, self.root))
        (self.root / "auth-ready").unlink()
        self.connect()
        self.browser.unlink()
        self.assertTrue(module.auth_ready(self.browser, self.root, self.root))
        self.auth.pkce.path.write_text("corrupt")
        self.assertFalse(module.auth_ready(self.browser, self.root, self.root))

    def test_sync_engine_uses_selected_credentials_for_reads_and_writes(self):
        from unittest.mock import patch

        import trakt.core
        from requests import Session
        from requests.adapters import BaseAdapter

        from plextraktsync.trakt.TraktApi import TraktApi

        sent = []

        class Adapter(BaseAdapter):
            def send(self, request, **kwargs):
                sent.append((request.method, request.headers["Authorization"]))
                response = Response()
                response.status_code = 200
                response._content = b"{}"
                response.request = request
                return response

            def close(self):
                pass

        self.connect()
        session = Session()
        session.mount("https://", Adapter())
        trakt.core.api.cache_clear()
        try:
            with patch("plextraktsync.trakt.TraktApi.factory") as factory, patch("plextraktsync.trakt.TraktApi.config_dir", str(self.root)):
                factory.config = {"TRAKT_BROWSER_TOKEN_FILE": str(self.browser)}
                factory.session = session
                api = TraktApi()
                self.assertTrue(api.external_auth)
                for method, token in (("pkce", "pkce-token"), ("browser", "session-token"), ("pkce", "pkce-token")):
                    self.auth.select(method)
                    trakt.core.api().get("users/settings")
                    trakt.core.api().post("sync/history", {"movies": []})
                    self.assertEqual(sent[-2:], [("GET", "Bearer " + token), ("POST", "Bearer " + token)])
        finally:
            trakt.core.api.cache_clear()

    def test_legacy_device_login_still_uses_its_own_credentials(self):
        from unittest.mock import patch

        from plextraktsync.trakt.TraktApi import TraktApi

        with patch("trakt.init", return_value="legacy-success") as init:
            self.assertEqual(TraktApi.device_auth("old-client", "old-secret"), "legacy-success")
            init.assert_called_once_with(client_id="old-client", client_secret="old-secret", store=True)
        self.assertFalse(self.auth.settings.exists())
        self.assertEqual(self.auth.read()["access_token"], "session-token")

    def test_every_feed_source_with_both_methods_and_switching_back(self):
        self.connect()
        examples = {"list": "123", "person": "example-person", "seed": "example-title", "query": "space", "identifier": "tt1234567"}
        headers = {"X-Bridge-Key": SECRET}
        checked = 0
        for source, spec in bridge.SOURCES.items():
            for media in spec["media"]:
                with self.subTest(source=source, media=media):
                    raw = {"name": "all-sources", "source": source, "media": media}
                    raw.update({k: examples[k] for k in spec["fields"] if bridge.OPTIONS[k].get("required")})
                    feed = bridge.validate_feed(raw)
                    seen = []

                    def get(url, seen=seen, media=media, source=source, **kwargs):
                        prepared = Request("GET", url, auth=kwargs["auth"]).prepare()
                        seen.append(prepared.headers["Authorization"])
                        item = (
                            {"movie": {"title": "Movie", "ids": {"tmdb": 42}}}
                            if media == "movies"
                            else {"show": {"title": "Show", "ids": {"tvdb": 42}}}
                        )
                        data = [item]
                        return SimpleNamespace(status_code=200, headers={}, json=lambda: {"cast": data, "crew": {}} if source == "credits" else data)

                    client = self.app(get=get, ttl=0)
                    for method, token in (("browser", "session-token"), ("pkce", "pkce-token"), ("browser", "session-token")):
                        result = client.post("/api/auth/method", json={"method": method}, headers=headers)
                        self.assertEqual(result.status_code, 200)
                        self.assertEqual(client.post("/api/feeds", json=feed, headers=headers).status_code, 201)
                        preview = client.post("/api/preview", json=feed, headers=headers)
                        self.assertEqual(preview.status_code, 200, preview.json)
                        target = "radarr" if media == "movies" else "sonarr"
                        exported = client.get(f"/{target}/all-sources.json", query_string={"key": SECRET})
                        self.assertEqual(exported.status_code, 200, exported.json)
                        self.assertEqual(exported.json, preview.json)
                        self.assertEqual(seen[-1], "Bearer " + token)
                        checked += 1
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
