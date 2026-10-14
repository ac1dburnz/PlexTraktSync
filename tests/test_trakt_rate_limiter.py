from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from itertools import pairwise
from unittest.mock import Mock

import pytest
import requests

from plextraktsync.trakt.RateLimiter import RateLimiter
from plextraktsync.trakt.TraktTransport import PacedAdapter, TraktAdapter


@pytest.fixture
def lab(tmp_path):
    now = [1000.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(tmp_path / "rate.sqlite", clock=lambda: now[0], sleep=sleep)
    return limiter, now, sleeps


def response(status=429, **headers):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers)
    return result


def test_shared_instances_pace_reads_and_writes_separately(lab):
    limiter, now, sleeps = lab
    other = RateLimiter(limiter.path, clock=limiter.clock, sleep=limiter.sleep)
    limiter.acquire("GET")
    other.acquire("GET")
    assert sleeps == [0.75]
    limiter.acquire("POST")
    other.acquire("DELETE")
    assert sleeps == [0.75, pytest.approx(1.1)]


def test_rolling_budget_survives_restart(lab):
    limiter, now, sleeps = lab
    with limiter.connect() as db:
        db.executemany("INSERT INTO calls VALUES (?, ?)", [("get", now[0])] * 450)
    other = RateLimiter(limiter.path, clock=limiter.clock, sleep=limiter.sleep)
    other.acquire("GET")
    assert sleeps == [300]


def test_long_retry_after_blocks_all_processes_and_verbs(lab):
    limiter, now, sleeps = lab
    limiter.observe(response(**{"Retry-After": "7200"}), "GET", "https://api.trakt.tv/test", True)
    other = RateLimiter(limiter.path, clock=limiter.clock, sleep=limiter.sleep)
    assert other.blocked()
    other.acquire("POST")
    assert sleeps == [7200]
    assert not other.blocked()


def test_missing_headers_back_off_exponentially(lab, monkeypatch):
    limiter, now, sleeps = lab
    monkeypatch.setattr("plextraktsync.trakt.RateLimiter.random.uniform", lambda *_: 0.5)
    for wait in (2.5, 4.5, 8.5):
        limiter.observe(response(), "GET", "https://api.trakt.tv/test")
        limiter.acquire("GET")
        assert sleeps[-1] == wait


def acquire_write(path):
    RateLimiter(path).acquire("POST")


@pytest.mark.parametrize("executor", [ThreadPoolExecutor, ProcessPoolExecutor])
def test_concurrent_clients_cannot_reserve_same_slot(tmp_path, executor):
    path = tmp_path / "shared.sqlite"
    clients = [RateLimiter(path) for _ in range(3)]
    with executor(max_workers=3) as pool:
        list(pool.map(acquire_write, [path] * 3))
    with clients[0].connect() as db:
        times = [row[0] for row in db.execute("SELECT at FROM calls ORDER BY at")]
    assert len(times) == 3
    assert all(b - a >= 1.1 for a, b in pairwise(times))


def test_transport_observes_throttling_without_replaying_write(monkeypatch):
    limiter = Mock()
    adapter = PacedAdapter(limiter)
    request = requests.Request("POST", "https://api.trakt.tv/sync/history", headers={"Authorization": "Bearer private"}).prepare()
    result = response(**{"Retry-After": "10"})
    send = Mock(return_value=result)
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", send)
    assert adapter.send(request) is result
    assert send.call_count == 1
    limiter.acquire.assert_called_once_with("POST")
    limiter.observe.assert_called_once_with(result, "POST", request.url, True)


def test_exhausted_server_bucket_pauses_before_next_request(lab):
    import json
    from datetime import datetime, timezone

    limiter, now, sleeps = lab
    until = datetime.fromtimestamp(now[0] + 200, timezone.utc).isoformat()
    limiter.observe(response(200, **{"X-Ratelimit": json.dumps({"remaining": 0, "until": until})}), "GET", "https://api.trakt.tv/users/settings")
    limiter.acquire("GET")
    assert sleeps == [200]


@pytest.mark.parametrize("value", ["NaN", "garbage", "-1"])
def test_invalid_retry_header_uses_backoff(lab, value):
    limiter, _now, sleeps = lab
    limiter.observe(response(**{"Retry-After": value}), "GET", "https://api.trakt.tv/test")
    limiter.acquire("GET")
    assert 2 <= sleeps[-1] <= 3


def test_vpn_denial_reporting_preserves_shared_retry_pause(lab, monkeypatch):
    limiter, now, sleeps = lab
    report = Mock()
    adapter = TraktAdapter("http://gateway:8080", "http://gateway:9090", report=report, limiter=limiter)
    denied = response(**{"Retry-After": "596"})
    accepted = response(status=200)
    send = Mock(side_effect=[denied, accepted])
    monkeypatch.setattr("requests.adapters.HTTPAdapter.send", send)
    request = requests.Request("POST", "https://api.trakt.tv/scrobble/start").prepare()
    assert adapter.send(request) is denied
    report.assert_called_once_with("rate-limit", retry_after=596)
    assert send.call_count == 1 and limiter.remaining() == 596
    assert adapter.send(request) is accepted
    assert sleeps == [596] and send.call_count == 2
    assert limiter.remaining() == 0
