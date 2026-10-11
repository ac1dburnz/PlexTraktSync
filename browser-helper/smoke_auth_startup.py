"""Exercise the real container entrypoint with isolated auth and fake processes."""
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from plextraktsync.trakt.PkceAuth import save_json


def wait_for(path, process, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return True
        if process.poll() is not None:
            raise AssertionError("Container entrypoint exited before startup completed")
        time.sleep(0.1)
    return False


def scenario(method, valid_pkce, initially_browser_ready, expect_start):
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        config, bins = root / "config", root / "bin"
        config.mkdir()
        bins.mkdir()
        marker = root / "sync-started"
        save_json(config / "browser-token.json", {"access_token": "session-token", "client_id": "session-client"})
        save_json(config / "trakt-auth.json", {"method": method})
        if valid_pkce:
            save_json(config / "trakt-pkce.json", {"access_token": "pkce-token", "refresh_token": "refresh", "client_id": "client", "redirect_uri": "http://localhost:18090/oauth/callback", "expires_at": time.time() + 3600})
        else:
            (config / "trakt-pkce.json").write_text("invalid PKCE")
        # Only substitute the long-running browser and sync executables. The
        # production entrypoint and actual auth readiness code run unchanged.
        wrapper = bins / "python"
        wrapper.write_text('''#!/bin/bash
if [[ "${1:-}" == /helper/capture_trakt_token.py ]]; then
  if [[ "$TEST_BROWSER_READY" == true ]]; then touch /browser/auth-ready; fi
  exec /bin/sleep 60
elif [[ "${1:-}" == -m && "${2:-}" == plextraktsync ]]; then
  touch "$TEST_SYNC_MARKER"
  exec /bin/sleep 60
else
  exec /opt/pts-venv/bin/python "$@"
fi
''')
        wrapper.chmod(0o755)
        env = {**os.environ, "PATH": str(bins) + ":" + os.environ["PATH"], "PTS_CONFIG_DIR": str(config),
               "TRAKT_BROWSER_TOKEN_FILE": str(config / "browser-token.json"), "TRAKT_BROWSER_HEADLESS": "true",
               "LIST_BRIDGE_ENABLED": "false", "TEST_SYNC_MARKER": str(marker),
               "TEST_BROWSER_READY": "true" if initially_browser_ready else "false"}
        process = subprocess.Popen(["/bin/bash", "/helper/all-in-one.sh", "watch"], env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True)
        try:
            if method == "browser" and not initially_browser_ready:
                time.sleep(1)
                assert not marker.exists(), "Old session bypassed its monitor validation gate"
                Path("/browser/auth-ready").touch()
            assert wait_for(marker, process) == expect_start
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            Path("/browser/auth-ready").unlink(missing_ok=True)


scenario("browser", False, False, True)
scenario("browser", False, True, True)
scenario("pkce", True, False, True)
scenario("pkce", False, True, False)
print(json.dumps({"browser_validation_gate_preserved": True, "browser_ignores_invalid_pkce": True,
                  "pkce_starts_without_browser_login": True, "invalid_pkce_does_not_fall_back": True}))
