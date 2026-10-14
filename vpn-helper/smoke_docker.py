"""Live WireGuard lifecycle test; use test_docker.sh with a supplied profile."""

from __future__ import annotations

import json
import os
import subprocess
import time

D = os.getenv("DOCKER_BIN", "docker")
C = os.environ["VPN_TEST_CONTAINER"]
CLIENT = os.environ["VPN_TEST_CLIENT"]


def control(path, reason=None, retry_after=0):
    code = "import urllib.request; print(urllib.request.urlopen(urllib.request.Request(" + repr("http://" + C + ":9090/" + path)
    if reason is not None:
        payload = json.dumps({"reason": reason, "retry_after": retry_after}).encode()
        code += ",data=" + repr(payload) + ',headers={"Content-Type":"application/json"},method="POST"'
    code += "),timeout=45).read().decode())"
    return json.loads(subprocess.check_output([D, "exec", CLIENT, "python", "-c", code], text=True))


assert control("status")["route"] == "direct"
assert control("report", "rate-limit", retry_after=3)["route"] == "direct"
status = control("report", "rate-limit", retry_after=3)
assert status["route"] == "direct" and status["rate_failures"] == 2, status
time.sleep(1)
assert control("status")["route"] == "direct"
for _ in range(15):
    if control("status")["route"] == "vpn":
        break
    time.sleep(1)
else:
    raise SystemExit("Repeated rate limits did not activate VPN after the retry timer")
print("Repeated rate limits started London VPN after the full pause", flush=True)
subprocess.run([D, "exec", C, "ip", "link", "del", "ptsvpn"], check=True)
# Verify the kernel rejects a marked connection rather than falling through direct.
code = """import sys
sys.path.insert(0, "/opt/pts-vpn")
import gateway
try:
    gateway.connect("api.trakt.tv", 443, True)
except OSError:
    print("Crash protection blocked direct fallback")
else:
    raise SystemExit("Marked connection leaked direct")
"""
subprocess.run([D, "exec", C, "python", "-c", code], check=True)
for _ in range(25):
    code = (
        'import subprocess; p=subprocess.run(["ip","link","show","ptsvpn"],'
        "stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);raise SystemExit(p.returncode)"
    )
    exists = subprocess.run([D, "exec", C, "python", "-c", code], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if exists and control("status")["route"] == "vpn":
        break
    time.sleep(1)
else:
    raise SystemExit("Tunnel did not recover after interface crash")
print("Crashed tunnel restarted and became healthy", flush=True)
code = 'import sys; sys.path.insert(0,"/opt/pts-vpn"); import gateway; raise SystemExit(0 if gateway.probe(False) else 1)'
direct_healthy = subprocess.run([D, "exec", C, "python", "-c", code]).returncode == 0
if not direct_healthy:
    assert control("status")["route"] == "vpn"
    print("Direct probe is denied/unhealthy; keeping VPN active", flush=True)
    raise SystemExit(0)
for _ in range(45):
    if control("status")["route"] == "direct":
        break
    time.sleep(1)
else:
    raise SystemExit("Direct recovery did not stop the tunnel")
print("Healthy direct route restored; VPN stopped automatically", flush=True)
