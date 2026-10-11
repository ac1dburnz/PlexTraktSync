#!/bin/bash
set -euo pipefail
: "${WIREGUARD_CONFIG_PATH:?Set the WireGuard profile path}"
root=$(cd "$(dirname "$0")/.." && pwd)
export VPN_TEST_CONTAINER="pts-vpn-test-$$"
trap 'docker rm -f "$VPN_TEST_CONTAINER" >/dev/null 2>&1 || true' EXIT
docker build -t plextraktsync:wireguard-gateway "$root/vpn-helper"
docker run -d --name "$VPN_TEST_CONTAINER" --cap-add NET_ADMIN \
  -e VPN_FAILURE_THRESHOLD=2 -e VPN_SWITCH_COOLDOWN=1 \
  -e VPN_HEALTH_INTERVAL=1 -e VPN_RECOVERY_INTERVAL=30 \
  -v "$WIREGUARD_CONFIG_PATH:/wireguard/provider.conf:ro" \
  plextraktsync:wireguard-gateway >/dev/null
python3 "$root/vpn-helper/smoke_docker.py"
