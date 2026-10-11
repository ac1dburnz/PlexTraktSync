"""Optional Trakt-only VPN proxy, independent of the selected authentication."""

from __future__ import annotations

import os
import threading
from contextlib import suppress
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

from plextraktsync.trakt.RateLimiter import RateLimiter


class PacedAdapter(HTTPAdapter):
    def __init__(self, limiter=None):
        super().__init__(max_retries=0)
        self.limiter = limiter if limiter is not None else RateLimiter()

    def send(self, request, **kwargs):
        self.limiter.acquire(request.method)
        response = super().send(request, **kwargs)
        self.limiter.observe(response, request.method, request.url, bool(request.headers.get("Authorization")))
        return response


class TraktAdapter(PacedAdapter):
    def __init__(self, proxy_url, control_url, report=None, limiter=None):
        super().__init__(limiter=limiter)
        for url in (proxy_url, control_url):
            parsed = urlsplit(url)
            if parsed.scheme != "http" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError("VPN gateway URLs must be internal HTTP endpoints without credentials.")
        self.proxy_url, self.control_url = proxy_url.rstrip("/"), control_url.rstrip("/")
        self.report = report or self._report

    def _report(self, reason):
        # Do not include paths, tokens, bodies or headers in transport reports.
        with suppress(requests.RequestException):
            requests.post(self.control_url + "/report", json={"reason": reason}, timeout=1, allow_redirects=False)

    def send(self, request, **kwargs):
        kwargs["proxies"] = {"https": self.proxy_url}
        try:
            response = super().send(request, **kwargs)
        except requests.Timeout:
            self.report("timeout")
            raise
        except requests.ConnectionError:
            self.report("connection")
            raise
        if response.status_code in (502, 503, 504):
            self.report("gateway")
        elif response.status_code < 400:
            self.report("success")
        # 401/403/420/429 do not change transport. Existing retry/backoff applies.
        return response


def configure(session, env=None):
    env = os.environ if env is None else env
    adapter = (
        TraktAdapter(env.get("TRAKT_VPN_PROXY_URL", "http://trakt-vpn:8080"), env.get("TRAKT_VPN_CONTROL_URL", "http://trakt-vpn:9090"))
        if env.get("TRAKT_VPN_ENABLED", "false").lower() == "true" else PacedAdapter()
    )
    for host in ("api.trakt.tv", "apiz.trakt.tv", "auth.trakt.tv"):
        session.mount("https://" + host + "/", adapter)
    return session


_local = threading.local()


def session():
    settings = tuple(os.getenv(key, "") for key in ("TRAKT_VPN_ENABLED", "TRAKT_VPN_PROXY_URL", "TRAKT_VPN_CONTROL_URL"))
    if getattr(_local, "settings", None) != settings:
        old = getattr(_local, "session", None)
        if old:
            old.close()
        _local.session = configure(requests.Session())
        _local.settings = settings
    return _local.session


def get(*args, **kwargs):
    return session().get(*args, **kwargs)


def post(*args, **kwargs):
    return session().post(*args, **kwargs)
