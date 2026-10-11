#!/bin/bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
cd "$root"
docker build -f Dockerfile.all-in-one -t plextraktsync:pkce-local .
docker build -f browser-helper/Dockerfile.pkce-tests -t plextraktsync:pkce-tests .
docker run --rm --network none \
  -e PTS_CONFIG_DIR=/tmp/pts-config -e PTS_CACHE_DIR=/tmp/pts-cache -e PTS_LOG_DIR=/tmp/pts-log \
  -v "$root/tests:/tests-src:ro" -v "$root/browser-helper:/app/browser-helper:ro" -v "$root/vpn-helper:/app/vpn-helper:ro" -v "$root/scripts:/app/scripts:ro" \
  plextraktsync:pkce-tests
docker run --rm --network none -e PTS_CONFIG_DIR=/tmp/pts-config plextraktsync:pkce-local test
docker run --rm --network none --entrypoint python -e PYTHONPATH=/app \
  -v "$root/browser-helper/smoke_auth_startup.py:/tmp/smoke_auth_startup.py:ro" \
  plextraktsync:pkce-local /tmp/smoke_auth_startup.py
smoke_container="pts-pkce-smoke-$$"
trap 'docker rm -f "$smoke_container" >/dev/null 2>&1 || true' EXIT
docker run -d --network none --name "$smoke_container" --entrypoint python \
  -e PYTHONPATH=/app -e LIST_BRIDGE_SECRET=isolated-smoke-test-key-at-least-24-characters \
  -e TRAKT_PKCE_CLIENT_ID=test-pkce-client-id \
  -e TRAKT_PKCE_REDIRECT_URI=http://localhost:18090/oauth/callback \
  -e TRAKT_PKCE_ALLOW_LOCAL_HTTP=true plextraktsync:pkce-local /helper/list_bridge.py >/dev/null
docker exec "$smoke_container" python -c 'import time,urllib.request
for attempt in range(50):
 try:
  urllib.request.urlopen("http://localhost:8090/healthz",timeout=1);break
 except OSError:
  time.sleep(.1)
else:
 raise SystemExit("Local bridge did not start")'
docker cp browser-helper/smoke_pkce_local.py "$smoke_container":/tmp/smoke_pkce_local.py
docker exec "$smoke_container" python /tmp/smoke_pkce_local.py
