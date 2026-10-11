from __future__ import annotations

from unittest.mock import Mock

import pytest
import requests
from requests.adapters import HTTPAdapter

from plextraktsync.trakt.TraktTransport import PacedAdapter, TraktAdapter, configure


@pytest.fixture(autouse=True)
def no_real_pacing(monkeypatch):
    monkeypatch.setattr("plextraktsync.trakt.TraktTransport.RateLimiter", Mock)


def test_disabled_still_paces_without_proxy():
    session = requests.Session()
    assert configure(session, {}) is session
    assert isinstance(session.get_adapter("https://api.trakt.tv/test"), PacedAdapter)
    assert not isinstance(session.get_adapter("https://api.trakt.tv/test"), TraktAdapter)


def test_only_trakt_endpoints_use_proxy():
    session = configure(requests.Session(), {"TRAKT_VPN_ENABLED": "true"})
    for host in ("api.trakt.tv", "apiz.trakt.tv", "auth.trakt.tv"):
        assert isinstance(session.get_adapter(f"https://{host}/test"), TraktAdapter)
    for url in ("https://plex.tv/", "http://192.168.1.100:8090/", "https://api.trakt.tv.evil.example/"):
        assert not isinstance(session.get_adapter(url), TraktAdapter)


@pytest.mark.parametrize(
    "code,reason", [(200, "success"), (401, None), (403, None), (420, None), (429, None), (502, "gateway"), (503, "gateway"), (504, "gateway")]
)
@pytest.mark.parametrize("token", ["browser-session", "pkce-access"])
def test_auth_independent_reporting_without_request_replay(monkeypatch, code, reason, token):
    report = Mock()
    adapter = TraktAdapter("http://gateway:8080", "http://gateway:9090", report=report)
    request = requests.Request(
        "POST", "https://api.trakt.tv/sync/history", headers={"Authorization": "Bearer " + token}, json={"movies": []}
    ).prepare()
    response = requests.Response()
    response.status_code = code
    send = Mock(return_value=response)
    monkeypatch.setattr(HTTPAdapter, "send", send)
    assert adapter.send(request) is response
    assert send.call_count == 1
    assert request.headers["Authorization"] == "Bearer " + token
    assert send.call_args.kwargs["proxies"] == {"https": "http://gateway:8080"}
    if reason:
        report.assert_called_once_with(reason)
    else:
        report.assert_not_called()


@pytest.mark.parametrize("exception,reason", [(requests.Timeout, "timeout"), (requests.ConnectionError, "connection")])
def test_network_failure_is_reported_and_not_replayed(monkeypatch, exception, reason):
    report = Mock()
    adapter = TraktAdapter("http://gateway:8080", "http://gateway:9090", report=report)
    send = Mock(side_effect=exception("failure"))
    monkeypatch.setattr(HTTPAdapter, "send", send)
    with pytest.raises(exception):
        adapter.send(requests.Request("GET", "https://api.trakt.tv/test").prepare())
    assert send.call_count == 1
    report.assert_called_once_with(reason)


def test_controller_failure_does_not_break_successful_request(monkeypatch):
    monkeypatch.setattr(requests, "post", Mock(side_effect=requests.ConnectionError()))
    response = requests.Response()
    response.status_code = 200
    monkeypatch.setattr(HTTPAdapter, "send", Mock(return_value=response))
    adapter = TraktAdapter("http://gateway:8080", "http://gateway:9090")
    assert adapter.send(requests.Request("GET", "https://api.trakt.tv/test").prepare()) is response
