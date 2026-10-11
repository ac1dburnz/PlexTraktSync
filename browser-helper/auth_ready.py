"""Preserve browser validation before startup while allowing independent PKCE."""
from pathlib import Path

from click import ClickException

from plextraktsync.trakt.PkceAuth import TraktAuth


def auth_ready(browser_file, config_dir, state_dir):
    try:
        auth = TraktAuth(browser_file, config_dir)
        if auth.method() == "browser":
            # The browser monitor creates this only after a successful API check.
            return (Path(state_dir) / "auth-ready").exists()
        auth.pkce.read()
        return True
    except (ClickException, OSError):
        return False
