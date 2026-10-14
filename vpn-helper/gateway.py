"""App-scoped CONNECT proxy with optional WireGuard transport and recovery."""

from __future__ import annotations

import configparser
import ipaddress
import json
import math
import os
import select
import signal
import socket
import ssl
import subprocess
import threading
import time
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

MARK = 51821
TABLE = 51821
RULE_PRIORITY = 1000
INTERFACE = "ptsvpn"


def command(args, required=True):
    try:
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except subprocess.TimeoutExpired:
        if required:
            raise RuntimeError("WireGuard command timed out.") from None
        return
    if required and result.returncode:
        raise RuntimeError("WireGuard command failed; check configuration and container capabilities.")


def connect(host, port, vpn=False, addresses=None):
    addresses = addresses or socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    for family, kind, proto, _, address in addresses:
        sock = socket.socket(family, kind, proto)
        try:
            sock.settimeout(15)
            if vpn:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_MARK, MARK)
            sock.connect(address)
            return sock
        except OSError:
            sock.close()
    raise OSError("Could not connect to upstream")


def probe(vpn=False):
    try:
        sock = connect("api.trakt.tv", 443, vpn)
        with ssl.create_default_context().wrap_socket(sock, server_hostname="api.trakt.tv") as stream:
            stream.sendall(b"HEAD / HTTP/1.1\r\nHost: api.trakt.tv\r\nConnection: close\r\n\r\n")
            line = stream.recv(512).split(b"\r\n", 1)[0]
            status = int(line.split()[1])
            return status < 500 and (vpn or status not in (401, 403, 412, 420, 429))
    except (OSError, ValueError, IndexError):
        return False


class WireGuard:
    """Only the gateway container's marked sockets enter the tunnel."""

    def __init__(self, directory):
        self.profiles = sorted(Path(directory).glob("*.conf"))
        self.path = Path("/etc/wireguard") / (INTERFACE + ".conf")
        self.policy_installed = False

    def start(self, index):
        if not self.profiles:
            raise RuntimeError("No WireGuard profiles configured.")
        profile = self.profiles[index % len(self.profiles)]
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read(profile)
        if set(parser.sections()) != {"Interface", "Peer"}:
            raise RuntimeError("A WireGuard profile must contain one Interface and one Peer.")
        # Do not execute wg-quick hooks or rewrite container DNS from a profile.
        allowed = {
            "Interface": {"PrivateKey", "Address", "ListenPort", "MTU", "DNS"},
            "Peer": {"PublicKey", "PresharedKey", "AllowedIPs", "Endpoint", "PersistentKeepalive"},
        }
        if any(set(parser[section]) - allowed[section] for section in parser.sections()):
            raise RuntimeError("Unsupported WireGuard fields or executable hooks in profile.")
        if "0.0.0.0/0" not in [value.strip() for value in parser.get("Peer", "AllowedIPs", fallback="").split(",")]:
            raise RuntimeError("WireGuard profile must allow IPv4 internet routing.")
        parser["Interface"].pop("DNS", None)
        parser["Interface"]["Table"] = "off"
        parser["Peer"].setdefault("PersistentKeepalive", "25")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w") as handle:
            parser.write(handle, space_around_delimiters=True)
        try:
            # A vanished interface must not let marked sockets fall through to
            # Docker's default route. Keep the terminal route even when stopped.
            command(["ip", "route", "replace", "prohibit", "default", "metric", "32767", "table", str(TABLE)])
            if not self.policy_installed:
                command(["ip", "rule", "add", "priority", str(RULE_PRIORITY), "fwmark", str(MARK), "lookup", str(TABLE)])
                self.policy_installed = True
            command(["wg-quick", "up", INTERFACE])
            command(["ip", "route", "replace", "default", "dev", INTERFACE, "metric", "10", "table", str(TABLE)])
        except Exception:
            self.stop()
            raise

    def stop(self):
        command(["wg-quick", "down", INTERFACE], False)
        self.path.unlink(missing_ok=True)


class Controller:
    def __init__(
        self, backend, check=probe, clock=time.monotonic, threshold=3, cooldown=120,
        recovery_interval=900, health_interval=30, mode="auto", rate_limit_failover=True,
    ):
        if mode not in ("auto", "always") or threshold < 1 or cooldown < 1 or recovery_interval < 30 or health_interval < 1:
            raise ValueError("Invalid VPN configuration.")
        self.backend, self.check, self.clock = backend, check, clock
        self.threshold, self.cooldown = threshold, cooldown
        self.recovery_interval, self.health_interval = recovery_interval, health_interval
        self.mode, self.route = mode, "direct" if mode == "auto" else "unavailable"
        self.index, self.failures, self.health_failures = -1, 0, 0
        self.last_change, self.last_recovery, self.last_health = -cooldown, clock(), clock()
        self.rate_limit_failover = rate_limit_failover
        self.rate_failures, self.retry_at = 0, 0
        self.error = None
        self.lock = threading.RLock()
        self.generation = 0
        self.tunnels = set()

    def close_tunnels(self):
        self.generation += 1
        for pair in list(self.tunnels):
            for stream in pair:
                with suppress(OSError):
                    stream.shutdown(socket.SHUT_RDWR)
        self.tunnels.clear()

    def status(self):
        with self.lock:
            return {
                "mode": self.mode,
                "route": self.route,
                "profile": self.index + 1 if self.index >= 0 else None,
                "profiles": len(self.backend.profiles),
                "failures": self.failures,
                "rate_limit_failover": self.rate_limit_failover,
                "rate_failures": self.rate_failures,
                "retry_after": max(0, self.retry_at - self.clock()),
                "error": self.error,
            }

    def activate(self):
        with self.lock:
            if not self.backend.profiles:
                self.error = "No WireGuard profiles configured."
                return False
            previous_route = self.route
            self.route = "unavailable"  # Fail closed while the tunnel changes.
            self.close_tunnels()
            self.index = (self.index + 1) % len(self.backend.profiles)
            self.last_change = self.clock()
            self.last_recovery = self.clock()
            try:
                self.backend.stop()
                self.backend.start(self.index)
                # Give a fresh handshake a bounded chance to settle before
                # discarding the tunnel. These are transport probes, not retries
                # of an application's API write.
                for attempt in range(3):
                    if self.check(True):
                        break
                    if attempt < 2:
                        time.sleep(1)
                else:
                    raise RuntimeError("VPN health check failed.")
                self.route, self.error = "vpn", None
                self.failures, self.health_failures = 0, 0
                self.rate_failures = 0
                print("VPN transport healthy; profile " + str(self.index + 1), flush=True)
                return True
            except Exception:
                with suppress(Exception):
                    self.backend.stop()
                self.error = "VPN startup or health check failed."
                # Preserve a working direct path only if we were already direct.
                if previous_route == "direct" and self.check(False):
                    self.route = "direct"
                print(self.error, flush=True)
                return False

    def report(self, reason, retry_after=0):
        if reason not in ("connection", "timeout", "gateway", "rate-limit"):
            return
        with self.lock:
            if reason == "rate-limit":
                if not self.rate_limit_failover:
                    return
                self.rate_failures += 1
                self.retry_at = max(self.retry_at, self.clock() + retry_after)
                # Defer tunnel startup, probes and retries until the shared pause ends.
                return
            self.failures += 1
            if self.failures >= self.threshold and self.clock() >= self.retry_at and self.clock() - self.last_change >= self.cooldown:
                self.activate()

    def success(self):
        with self.lock:
            self.failures = 0

    def stop(self):
        with self.lock:
            self.route = "unavailable"
            self.close_tunnels()
            self.backend.stop()
            self.route = "direct"
            self.failures, self.health_failures = 0, 0
            self.rate_failures = 0
            self.last_change = self.clock()

    def tick(self):
        with self.lock:
            now = self.clock()
            if now < self.retry_at:
                return
            if self.rate_failures >= self.threshold and now - self.last_change >= self.cooldown:
                self.activate()
                return
            if self.route == "direct":
                if self.mode == "always" and now - self.last_change >= self.cooldown:
                    self.activate()
                return
            if self.route == "unavailable":
                if now - self.last_change >= self.cooldown:
                    self.activate()
                return
            if now - self.last_health >= self.health_interval:
                self.last_health = now
                if self.check(True):
                    self.health_failures = 0
                else:
                    self.health_failures += 1
                    if self.health_failures >= self.threshold and now - self.last_change >= self.cooldown:
                        self.activate()
                        return
            if self.mode == "auto" and now - self.last_recovery >= self.recovery_interval:
                self.last_recovery = now
                if self.check(False):
                    self.stop()
                    print("Direct transport recovered; VPN stopped.", flush=True)

    def vpn_for(self, host):
        if host != "trakt.tv" and not host.endswith(".trakt.tv"):
            return False
        with self.lock:
            if self.rate_failures >= self.threshold:
                self.tick()
            if self.route == "unavailable":
                raise OSError("VPN transport unavailable; no direct fallback.")
            return self.route == "vpn"


class Proxy(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_CONNECT(self):
        try:
            host, port = self.path.rsplit(":", 1)
            host = host.lower()
            if int(port) != 443 or not host or "/" in host or "@" in host:
                self.send_error(400)
                return
            addresses = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_STREAM)
            if any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
                self.send_error(403)
                return
            controller = self.server.controller
            is_trakt = host == "trakt.tv" or host.endswith(".trakt.tv")
            with controller.lock:
                use_vpn = controller.vpn_for(host)
                generation = controller.generation
            upstream = connect(host, int(port), use_vpn, addresses)
            if is_trakt:
                with controller.lock:
                    if generation != controller.generation:
                        upstream.close()
                        raise OSError("Transport changed during connection.")
                    controller.tunnels.add((self.connection, upstream))
        except OSError:
            self.send_error(502)
            return
        self.send_response(200, "Connection Established")
        self.end_headers()
        self.wfile.flush()
        try:
            sockets = [self.connection, upstream]
            while True:
                readable, _, _ = select.select(sockets, [], [], 60)
                if not readable:
                    return
                for source in readable:
                    data = source.recv(65536)
                    if not data:
                        return
                    target = upstream if source is self.connection else self.connection
                    target.sendall(data)
        except OSError:
            pass
        finally:
            if is_trakt:
                with controller.lock:
                    controller.tunnels.discard((self.connection, upstream))
            upstream.close()
            self.close_connection = True


class Control(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/status":
            self.reply({"error": "Not found"}, 404)
            return
        self.reply(self.server.controller.status())

    def do_POST(self):
        controller = self.server.controller
        if self.path == "/start":
            okay = controller.activate()
            self.reply(controller.status(), 200 if okay else 503)
        elif self.path == "/stop":
            controller.stop()
            self.reply(controller.status())
        elif self.path == "/report":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 256:
                    raise ValueError()
                data = json.loads(self.rfile.read(length))
                reason = data["reason"]
                retry_after = float(data.get("retry_after", 0))
                if not math.isfinite(retry_after) or retry_after < 0:
                    raise ValueError()
                if reason == "success":
                    controller.success()
                else:
                    controller.report(reason, retry_after)
                self.reply(controller.status())
            except (ValueError, KeyError, TypeError):
                self.reply({"error": "Invalid report"}, 400)
        else:
            self.reply({"error": "Not found"}, 404)


class LocalHTTPServer(ThreadingHTTPServer):
    def server_bind(self):
        # Accept Docker traffic only; listeners must not accept VPN-interface input.
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b"eth0\0")
        super().server_bind()


def main():
    controller = Controller(
        WireGuard(os.getenv("VPN_CONFIG_DIR", "/wireguard")),
        threshold=int(os.getenv("VPN_FAILURE_THRESHOLD", "3")),
        cooldown=int(os.getenv("VPN_SWITCH_COOLDOWN", "120")),
        recovery_interval=int(os.getenv("VPN_RECOVERY_INTERVAL", "900")),
        health_interval=int(os.getenv("VPN_HEALTH_INTERVAL", "30")),
        mode=os.getenv("VPN_MODE", "auto"),
        rate_limit_failover=os.getenv("VPN_RATE_LIMIT_FAILOVER", "true").lower() == "true",
    )
    servers = [LocalHTTPServer(("0.0.0.0", 8080), Proxy), LocalHTTPServer(("0.0.0.0", 9090), Control)]
    for server in servers:
        server.controller = controller
        threading.Thread(target=server.serve_forever, daemon=True).start()
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        print("VPN gateway ready; initial transport " + controller.route + ".", flush=True)
        while not stopped.wait(1):
            controller.tick()
    finally:
        controller.backend.stop()
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
