"""PKCE credentials shared by the CLI and list bridge, alongside browser login."""
from __future__ import annotations

import base64
import ipaddress
import hashlib
import json
import math
import os
import secrets
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import requests
from click import ClickException

from plextraktsync.trakt.BrowserTokenAuth import BrowserTokenAuth



def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as handle:
        if os.name == "nt":
            import msvcrt
            handle.write("0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def load_json(path):
    try:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (OSError, ValueError):
        raise ClickException("Trakt authentication configuration cannot be read.") from None


class PkceAuth(BrowserTokenAuth):
    def __init__(self, path, post=None):
        super().__init__(path)
        from plextraktsync.trakt.TraktTransport import post as trakt_post
        self.post = post or trakt_post

    def exchange(self, payload):
        try:
            response = self.post("https://auth.trakt.tv/oauth/token", json=payload, timeout=20, allow_redirects=False)
            if response.status_code != 200:
                raise ClickException("Trakt token exchange failed. Reconnect PKCE if authorization expired or was revoked.")
            data = response.json()
            for key in ("access_token", "refresh_token"):
                if not isinstance(data.get(key), str) or not data[key] or any(c.isspace() for c in data[key]):
                    raise ValueError()
            expires = data["expires_in"]
            created = data.get("created_at", time.time())
            if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in (expires, created)) or expires <= 60:
                raise ValueError()
            return {"access_token": data["access_token"], "refresh_token": data["refresh_token"],
                    "expires_at": created + expires, "client_id": payload["client_id"], "redirect_uri": payload["redirect_uri"]}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            raise ClickException("Trakt token exchange could not be completed. Try connecting again.") from None

    def read(self):
        with locked(self.path):
            data = load_json(self.path)
            try:
                expires = data["expires_at"]
                if isinstance(expires, bool) or not isinstance(expires, (int, float)) or not math.isfinite(expires):
                    raise ValueError()
                for key in ("access_token", "refresh_token", "client_id", "redirect_uri"):
                    if not isinstance(data[key], str) or not data[key] or any(c.isspace() for c in data[key]):
                        raise ValueError()
            except (ValueError, KeyError, TypeError):
                raise ClickException("PKCE credentials are invalid. Connect Trakt again.") from None
            if expires <= time.time() + 60:
                data = self.exchange({"grant_type": "refresh_token", "refresh_token": data["refresh_token"],
                                      "client_id": data["client_id"], "redirect_uri": data["redirect_uri"]})
                save_json(self.path, data)
            return data

    @staticmethod
    def check_response(response, **kwargs):
        if response.status_code == 401:
            raise ClickException("Trakt rejected PKCE credentials. Connect Trakt again; the request was not replayed.")
        if response.is_redirect:
            raise ClickException("Unexpected Trakt API redirect; credentials were not forwarded.")
        return response


class TraktAuth(BrowserTokenAuth):
    """Select credentials per request; never silently switch accounts on failure."""
    def __init__(self, browser_file, config_dir, post=None):
        self.browser = BrowserTokenAuth(browser_file)
        self.pkce = PkceAuth(Path(config_dir) / "trakt-pkce.json", post)
        self.settings = Path(config_dir) / "trakt-auth.json"
        self.pending = Path(config_dir) / "trakt-pkce-pending.json"

    def method(self):
        method = load_json(self.settings).get("method", "browser") if self.settings.exists() else "browser"
        if method not in ("browser", "pkce"):
            raise ClickException("Unknown Trakt authentication method.")
        return method

    def select(self, method):
        if method not in ("browser", "pkce"):
            raise ClickException("Choose browser or PKCE authentication.")
        if method == "pkce":
            self.pkce.read()
        save_json(self.settings, {"method": method})

    def read(self):
        return (self.pkce if self.method() == "pkce" else self.browser).read()

    def __call__(self, request):
        return (self.pkce if self.method() == "pkce" else self.browser)(request)

    def start(self, redirect_uri, client_id=None, allow_local_http=False):
        client_id = client_id or os.getenv("TRAKT_PKCE_CLIENT_ID", "")
        uri = urlsplit(redirect_uri)
        local = uri.hostname == "localhost"
        try:
            address = ipaddress.ip_address(uri.hostname or "")
            local = address.is_loopback or any(address in network for network in (
                ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
                ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7")))
        except ValueError:
            pass
        valid_scheme = uri.scheme == "https" or (allow_local_http and uri.scheme == "http" and local)
        if not valid_scheme or not uri.hostname or uri.username or uri.password or uri.fragment or uri.query:
            raise ClickException("Use an HTTPS callback, or explicitly enable a local HTTP callback for LAN testing.")
        if not isinstance(client_id, str) or not client_id or any(c.isspace() for c in client_id):
            raise ClickException("Set TRAKT_PKCE_CLIENT_ID to your Trakt app Client ID.")
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        with locked(self.pending):
            save_json(self.pending, {"verifier": verifier, "state": state, "client_id": client_id,
                                     "redirect_uri": redirect_uri, "expires_at": time.time() + 600})
        return "https://auth.trakt.tv/oauth/authorize?" + urlencode({"response_type": "code", "client_id": client_id,
            "redirect_uri": redirect_uri, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})

    def finish(self, code, state):
        with locked(self.pending):
            pending = load_json(self.pending)
            expected_state = pending.get("state")
            expires = pending.get("expires_at")
            if (not isinstance(state, str) or not isinstance(expected_state, str)
                    or not secrets.compare_digest(state, expected_state)
                    or isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or expires <= time.time()):
                raise ClickException("Invalid or expired PKCE state. Start a new connection.")
            if any(not isinstance(pending.get(key), str) or not pending[key] for key in ("verifier", "client_id", "redirect_uri")):
                raise ClickException("Invalid PKCE login configuration. Start a new connection.")
            if not isinstance(code, str) or not code or len(code) > 4096:
                raise ClickException("Missing authorization code.")
            self.pending.unlink()  # One attempt per authorization, including failures.
            with locked(self.pkce.path):
                data = self.pkce.exchange({"grant_type": "authorization_code", "code": code,
                    "code_verifier": pending["verifier"], "client_id": pending["client_id"], "redirect_uri": pending["redirect_uri"]})
                save_json(self.pkce.path, data)
            save_json(self.settings, {"method": "pkce"})
