"""Capture only Trakt API credentials from a dedicated, user-authenticated browser."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import tempfile
from contextlib import suppress
from pathlib import Path
from urllib.parse import urlsplit

from alerts import SlackAlerts
from token_monitor import TokenMonitor, validate


def save_token(path, token, client_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".trakt-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump({"access_token": token, "client_id": client_id}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    from playwright.sync_api import sync_playwright

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=300)
    args = parser.parse_args()
    if args.interval < 60:
        parser.error("interval must be at least 60 seconds")
    args.profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile_lock = (args.profile.parent / "helper.lock").open("a")
    fcntl.flock(profile_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # This dedicated profile is exclusively owned by this helper.
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (args.profile / name).unlink(missing_ok=True)
    def stop(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, stop)
    last = None
    alerts = SlackAlerts(args.profile.parent / "slack-alert-state.json")

    ready = args.profile.parent / "auth-ready"
    ready.unlink(missing_ok=True)

    monitor = TokenMonitor(args.output, args.profile.parent, alerts)

    def capture(response):
        if response.status != 200:
            return
        request = response.request
        nonlocal last
        url = urlsplit(request.url)
        if url.scheme != "https" or url.netloc not in ("api.trakt.tv", "apiz.trakt.tv"):
            return
        headers = request.all_headers()
        authorization = headers.get("authorization", "")
        key = headers.get("trakt-api-key")
        if not authorization.startswith("Bearer ") or not key:
            return
        pair = (authorization[7:], key)
        if pair != last:
            if validate({"access_token": pair[0], "client_id": pair[1]}) != "healthy":
                print("Captured candidate not validated; keeping the previous token.", flush=True)
                return
            save_token(args.output, *pair)
            last = pair
            print("Updated Trakt token file (credentials hidden).", flush=True)
            monitor.check()

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(args.profile), headless=os.getenv("TRAKT_BROWSER_HEADLESS", "false").lower() == "true",
        )
        context.on("response", capture)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://app.trakt.tv/", wait_until="domcontentloaded")
        except Exception:
            print("Initial page load unavailable; retrying later.", flush=True)
        print("Sign in to your own Trakt account in this browser. Keep it open for token capture.", flush=True)
        monitor.check()
        while not page.is_closed():
            page.wait_for_timeout(args.interval * 1000)
            monitor.check()
            if not page.is_closed():
                try:
                    page.reload(wait_until="domcontentloaded")
                except Exception:
                    print("Page reload failed; retrying at next interval.", flush=True)


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
