"""Read-only authentication checks, separated from browser event handling."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import requests


def validate(data, get=requests.get):
    if not isinstance(data, dict):
        return "login_required"
    for name in ("access_token", "client_id"):
        value = data.get(name)
        if not isinstance(value, str) or not value or any(c.isspace() for c in value):
            return "login_required"
    expires = data.get("expires_at")
    if expires is not None:
        if isinstance(expires, bool) or not isinstance(expires, (float, int)) or not math.isfinite(expires) or expires <= time.time():
            return "login_required"
    try:
        response = get("https://api.trakt.tv/users/settings", headers={
            "Authorization": "Bearer " + data["access_token"],
            "trakt-api-key": data["client_id"], "trakt-api-version": "2",
            "User-Agent": "PlexTraktSync", "Accept": "application/json",
        }, timeout=20, allow_redirects=False)
        if response.status_code == 401:
            return "login_required"
        if response.status_code == 200 and response.json().get("user", {}).get("username"):
            return "healthy"
    except (requests.RequestException, ValueError, AttributeError):
        pass
    return "unavailable"


class TokenMonitor:
    def __init__(self, output, state_dir, alerts, get=requests.get):
        self.output = Path(output)
        self.state_dir = Path(state_dir)
        self.alerts = alerts
        self.get = get

    def check(self):
        try:
            data = json.loads(self.output.read_text())
        except (OSError, ValueError):
            data = None
        status = validate(data, self.get)
        ready = self.state_dir / "auth-ready"
        if status == "healthy":
            ready.touch(mode=0o600)
            self.alerts.update(False)
        else:
            ready.unlink(missing_ok=True)
            if status == "login_required":
                self.alerts.update(True)
        # Public status contains no credentials or account identifiers.
        (self.state_dir / "auth-status.json").write_text(json.dumps({"status": status, "checked_at": time.time()}))
        print("Trakt authentication: " + status, flush=True)
        return status
