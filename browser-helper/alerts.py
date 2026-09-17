"""Optional Slack notifications; no authentication material enters messages."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


class SlackAlerts:
    def __init__(self, state_path, send=None):
        self.path = Path(state_path)
        self.send = send or requests.post
        self.url = os.getenv("SLACK_WEBHOOK_URL", "")

    def update(self, needs_login):
        if not self.url:
            return
        try:
            url = urlsplit(self.url)
            if url.scheme != "https" or url.hostname not in ("hooks.slack.com", "hooks.slack-gov.com"):
                raise ValueError("Invalid webhook host")
            try:
                state = json.loads(self.path.read_text())
            except (OSError, ValueError):
                state = {}
            if not needs_login:
                if state:
                    self.path.write_text('{}')
                return
            if state.get("sent") or time.time() - state.get("attempt", 0) < 900:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            state = {"attempt": time.time(), "sent": False}
            self.path.write_text(json.dumps(state))
            self.path.chmod(0o600)
            response = self.send(self.url, json={"text": (
                os.getenv("SLACK_ALERT_LABEL", "PlexTraktSync")[:100] + ": Trakt authentication needs attention. "
                "The browser token is missing, expired, or rejected. "
                "Open the container's login interface and sign in with your email PIN."
            )}, timeout=10, allow_redirects=False)
            if response.status_code != 200 or response.text.strip() != "ok":
                raise ValueError("Webhook failed")
            state["sent"] = True
            self.path.write_text(json.dumps(state))
        except Exception:
            # Never log exception text: requests errors can include the secret webhook URL.
            logging.warning("Slack notification unavailable; will retry after cooldown.")
