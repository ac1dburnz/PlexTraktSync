"""Live WireGuard lifecycle test; use test_docker.sh with a supplied profile."""

from __future__ import annotations

import json
import os
import subprocess
import time

D = os.getenv("DOCKER_BIN", "docker")
C = os.environ["VPN_TEST_CONTAINER"]


def control(path, reason=None):
    code = "import urllib.request; print(urllib.request.urlopen(urllib.request.Request(" + repr("http://localhost:9090/" + path)
    if reason is not None:
        code += ",data=" + repr(json.dumps({"reason": reason}).encode()) + ',headers={"Content-Type":"application/json"},method="POST"'
    code += "),timeout=45).read().decode())"
    return json.loads(subprocess.check_output([D, "exec", C, "python", "-c", code], text=True))


assert control("status")["route"] == "direct"
assert control("report", "connection")["route"] == "direct"
assert control("report", "timeout")["route"] == "vpn"
print("Repeated network failures started London VPN", flush=True)
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
for _ in range(45):
    if control("status")["route"] == "direct":
        break
    time.sleep(1)
else:
    raise SystemExit("Direct recovery did not stop the tunnel")
print("Healthy direct route restored; VPN stopped automatically", flush=True)
