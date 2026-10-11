#!/bin/bash
set -euo pipefail
: "${WIREGUARD_CONFIG_PATH:?Set the WireGuard profile path}"
root=$(cd "$(dirname "$0")/.." && pwd)
export VPN_TEST_CONTAINER="pts-vpn-test-$$"
export VPN_TEST_CLIENT="pts-vpn-client-$$"
export VPN_TEST_NETWORK="pts-vpn-network-$$"
trap 'result=$?; if [[ "$result" != 0 ]]; then docker logs "$VPN_TEST_CONTAINER" 2>&1 || true; fi; docker rm -f "$VPN_TEST_CONTAINER" "$VPN_TEST_CLIENT" >/dev/null 2>&1 || true; docker network rm "$VPN_TEST_NETWORK" >/dev/null 2>&1 || true' EXIT
docker network create "$VPN_TEST_NETWORK" >/dev/null
docker build -t plextraktsync:wireguard-gateway "$root/vpn-helper"
docker run -d --name "$VPN_TEST_CONTAINER" --network "$VPN_TEST_NETWORK" --cap-add NET_ADMIN \
  -e VPN_FAILURE_THRESHOLD=2 -e VPN_SWITCH_COOLDOWN=1 \
  -e VPN_HEALTH_INTERVAL=1 -e VPN_RECOVERY_INTERVAL=30 \
  -v "$WIREGUARD_CONFIG_PATH:/wireguard/provider.conf:ro" \
  plextraktsync:wireguard-gateway >/dev/null
docker run -d --name "$VPN_TEST_CLIENT" --network "$VPN_TEST_NETWORK" \
  --entrypoint sleep plextraktsync:wireguard-gateway 180 >/dev/null
python3 "$root/vpn-helper/smoke_docker.py"
