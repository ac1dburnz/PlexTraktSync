#!/bin/bash
set -euo pipefail
umask 077
if [[ "${1:-}" == test ]]; then
  exec python -m plextraktsync info
fi
mkdir -p /app/config /browser/profile
# Existing dotenv files can override environment variables; migrate this one key.
python - <<'PYTHON'
import os
from pathlib import Path
from dotenv import set_key
path = Path(os.environ.get("PTS_CONFIG_DIR", "/app/config")) / ".env"
set_key(str(path), "TRAKT_BROWSER_TOKEN_FILE", os.environ["TRAKT_BROWSER_TOKEN_FILE"])
PYTHON
trap 'kill $(jobs -pr) 2>/dev/null || true' EXIT
trap 'exit 0' TERM INT
if [[ "${TRAKT_BROWSER_HEADLESS:-false}" != true ]]; then
  rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
  Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
  sleep 1
  x11vnc -display :99 -localhost -rfbport 5900 -nopw -forever -shared >/tmp/vnc.log 2>&1 &
  websockify --web=/usr/share/novnc 6080 localhost:5900 >/tmp/websockify.log 2>&1 &
fi
# Start the feed builder even while browser login is pending.
if [[ "${LIST_BRIDGE_ENABLED:-false}" == true ]]; then
  PYTHONPATH=/app:/helper python -c 'import os; from list_bridge import create_app; create_app(os.environ["PTS_CONFIG_DIR"], os.environ["TRAKT_BROWSER_TOKEN_FILE"], os.environ.get("LIST_BRIDGE_SECRET", ""))' 2>/dev/null || {
    echo "List bridge configuration invalid: set LIST_BRIDGE_SECRET to at least 24 ASCII characters." >&2
    exit 1
  }
  PYTHONPATH=/app python /helper/list_bridge.py &
  bridge_pid=$!
fi
rm -f /browser/auth-ready
python /helper/capture_trakt_token.py --profile /browser/profile --output "$TRAKT_BROWSER_TOKEN_FILE" &
browser_pid=$!
while [[ ! -f /browser/auth-ready ]]; do
  kill -0 "$browser_pid" 2>/dev/null || exit 1
  if [[ -n "${bridge_pid:-}" ]]; then kill -0 "$bridge_pid" 2>/dev/null || exit 1; fi
  sleep 2
done
if [[ "${1:-}" == browser-only ]]; then
  if [[ -n "${bridge_pid:-}" ]]; then wait -n "$browser_pid" "$bridge_pid"; else wait "$browser_pid"; fi
  exit $?
fi
python -m plextraktsync "$@" &
wait -n
