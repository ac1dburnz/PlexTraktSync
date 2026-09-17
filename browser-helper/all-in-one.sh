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
rm -f /browser/auth-ready
python /helper/capture_trakt_token.py --profile /browser/profile --output "$TRAKT_BROWSER_TOKEN_FILE" &
browser_pid=$!
while [[ ! -f /browser/auth-ready ]]; do
  kill -0 "$browser_pid" 2>/dev/null || exit 1
  sleep 2
done
if [[ "${1:-}" == browser-only ]]; then
  wait "$browser_pid"
  exit $?
fi
python -m plextraktsync "$@" &
wait -n
