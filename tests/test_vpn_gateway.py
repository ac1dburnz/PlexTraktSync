from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

spec = importlib.util.spec_from_file_location("vpn_gateway", Path(__file__).resolve().parents[1] / "vpn-helper/gateway.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class Backend:
    profiles = ["one", "two"]

    def __init__(self):
        self.starts = []
        self.stops = 0
        self.fail = False

    def start(self, index):
        self.starts.append(index)
        if self.fail:
            raise RuntimeError("private key must never appear")

    def stop(self):
        self.stops += 1


@pytest.fixture
def lab():
    backend = Backend()
    now = [1000]
    healthy = {False: True, True: True}
    controller = gateway.Controller(
        backend, check=lambda vpn: healthy[vpn], clock=lambda: now[0], threshold=3, cooldown=10, recovery_interval=30, health_interval=2
    )
    return controller, backend, now, healthy


def test_threshold_rotation_and_cooldown(lab):
    c, backend, now, _ = lab
    for _ in range(2):
        c.report("connection")
    assert c.route == "direct"
    c.report("timeout")
    assert c.route == "vpn" and backend.starts == [0]
    for _ in range(3):
        c.report("gateway")
    assert backend.starts == [0]
    now[0] += 10
    c.report("gateway")
    assert backend.starts == [0, 1]
    now[0] += 10
    for _ in range(3):
        c.report("connection")
    assert backend.starts == [0, 1, 0]


def test_auth_and_rate_denials_never_rotate(lab):
    c, backend, _, _ = lab
    for reason in ("401", "403", "420", "429", "denied") * 10:
        c.report(reason)
    assert c.route == "direct" and backend.starts == []


def test_success_resets_consecutive_failures(lab):
    c, backend, _, _ = lab
    c.report("timeout")
    c.report("timeout")
    c.success()
    c.report("timeout")
    assert c.failures == 1 and backend.starts == []


def test_crash_rotates_and_recovers(lab):
    c, backend, now, healthy = lab
    c.activate()
    healthy[True] = False
    now[0] += 10
    c.tick()
    now[0] += 2
    c.tick()
    now[0] += 2
    c.tick()
    assert c.route == "unavailable" and backend.starts == [0, 1]
    with pytest.raises(OSError):
        c.vpn_for("api.trakt.tv")
    assert c.vpn_for("plex.tv") is False
    healthy[True] = True
    now[0] += 10
    c.tick()
    assert c.route == "vpn" and backend.starts == [0, 1, 0]


def test_direct_recovery_stops_vpn(lab):
    c, backend, now, healthy = lab
    c.activate()
    healthy[False] = False
    now[0] += 30
    c.tick()
    assert c.route == "vpn"
    healthy[False] = True
    now[0] += 30
    c.tick()
    assert c.route == "direct" and not c.vpn_for("api.trakt.tv")


def test_failed_initial_start_preserves_healthy_direct(lab, capsys):
    c, backend, _, _ = lab
    backend.fail = True
    assert not c.activate()
    assert c.route == "direct"
    assert "private key" not in capsys.readouterr().out
    assert "private key" not in str(c.status())


def test_failed_rotation_never_falls_back_direct(lab):
    c, backend, _, _ = lab
    assert c.activate()
    backend.fail = True
    assert not c.activate()
    assert c.route == "unavailable"


def test_always_mode_has_no_initial_direct_window():
    c = gateway.Controller(Backend(), check=lambda vpn: True, mode="always")
    with pytest.raises(OSError):
        c.vpn_for("api.trakt.tv")
    c.tick()
    assert c.route == "vpn"


def test_profile_hooks_rejected_without_execution(tmp_path, monkeypatch):
    (tmp_path / "a.conf").write_text("[Interface]\nPrivateKey = secret\nPostUp = echo bad\n[Peer]\nAllowedIPs = 0.0.0.0/0\n")
    execute = Mock()
    monkeypatch.setattr(gateway, "command", execute)
    with pytest.raises(RuntimeError, match="hooks"):
        gateway.WireGuard(tmp_path).start(0)
    execute.assert_not_called()


def test_profile_never_rewrites_dns_and_has_private_permissions(tmp_path, monkeypatch):
    (tmp_path / "a.conf").write_text(
        "[Interface]\nPrivateKey = secret\nAddress = 10.1.1.2/32\nDNS = 10.1.1.1\n"
        "[Peer]\nPublicKey = public\nAllowedIPs = 0.0.0.0/0\nEndpoint = vpn.example:51820\n"
    )
    backend = gateway.WireGuard(tmp_path)
    backend.path = tmp_path / "generated" / "ptsvpn.conf"
    monkeypatch.setattr(gateway, "command", Mock())
    backend.start(0)
    text = backend.path.read_text()
    assert "DNS" not in text and "Table = off" in text
    assert backend.path.stat().st_mode & 0o777 == 0o600
    backend.stop()
    assert not backend.path.exists()


def test_route_changes_close_existing_trakt_tunnels(lab):
    c, _, _, _ = lab
    client, upstream = Mock(), Mock()
    c.tunnels.add((client, upstream))
    assert c.activate()
    client.shutdown.assert_called_once()
    upstream.shutdown.assert_called_once()
    assert not c.tunnels
    c.tunnels.add((client, upstream))
    c.stop()
    assert client.shutdown.call_count == 2


def test_crash_guard_is_kept_when_interface_stops(tmp_path, monkeypatch):
    (tmp_path / "a.conf").write_text("[Interface]\nPrivateKey = secret\n[Peer]\nAllowedIPs = 0.0.0.0/0\n")
    backend = gateway.WireGuard(tmp_path)
    backend.path = tmp_path / "generated.conf"
    commands = []
    monkeypatch.setattr(gateway, "command", lambda args, required=True: commands.append(args))
    backend.start(0)
    assert any("prohibit" in args for args in commands)
    commands.clear()
    backend.stop()
    assert not any("rule" in args or "flush" in args for args in commands)


def test_rate_limited_health_probe_does_not_rotate_tunnel(monkeypatch):
    stream = MagicMock()
    stream.recv.return_value = b"HTTP/1.1 429 Too Many Requests\r\n"
    context = MagicMock()
    context.wrap_socket.return_value.__enter__.return_value = stream
    monkeypatch.setattr(gateway, "connect", Mock())
    monkeypatch.setattr(gateway.ssl, "create_default_context", lambda: context)
    assert gateway.probe(True)


def test_startup_tolerates_transient_handshake_failure(lab, monkeypatch):
    controller, backend, _now, _healthy = lab
    controller.check = Mock(side_effect=[False, False, True])
    monkeypatch.setattr(gateway.time, "sleep", lambda _: None)
    assert controller.activate()
    assert controller.status()["route"] == "vpn"
    assert backend.starts == [0]


def test_rate_denials_take_over_after_full_pause(lab):
    c, backend, now, _ = lab
    for _ in range(3):
        c.report("rate-limit", retry_after=596)
        c.success()  # Unrelated healthy reads must not erase write denials.
    c.tick()
    assert c.rate_failures == 3 and backend.starts == []
    now[0] += 595
    c.tick()
    assert backend.starts == []
    now[0] += 1
    assert c.vpn_for("api.trakt.tv")  # First retry must use the new route.
    assert backend.starts == [0] and c.rate_failures == 0


def test_rate_denials_rotate_and_respect_switch_cooldown(lab):
    c, backend, now, _ = lab
    c.activate()
    for _ in range(3):
        c.report("rate-limit", retry_after=2)
    now[0] += 2
    c.tick()
    assert backend.starts == [0]
    now[0] += 8
    c.tick()
    assert backend.starts == [0, 1]


def test_rate_limit_failover_can_be_disabled(lab):
    c, backend, now, _ = lab
    c.rate_limit_failover = False
    for _ in range(5):
        c.report("rate-limit", retry_after=596)
    now[0] += 600
    c.tick()
    assert backend.starts == [] and c.rate_failures == 0


def test_no_health_or_recovery_probe_during_rate_pause(lab):
    c, _, now, _ = lab
    c.activate()
    c.check = Mock(return_value=True)
    c.report("rate-limit", retry_after=596)
    now[0] += 30
    c.tick()
    c.check.assert_not_called()
    assert c.route == "vpn"


@pytest.mark.parametrize("status", [403, 412, 420, 429])
def test_denied_direct_probe_does_not_claim_recovery(monkeypatch, status):
    stream = MagicMock()
    stream.recv.return_value = f"HTTP/1.1 {status} Denied\r\n".encode()
    context = MagicMock()
    context.wrap_socket.return_value.__enter__.return_value = stream
    monkeypatch.setattr(gateway, "connect", Mock())
    monkeypatch.setattr(gateway.ssl, "create_default_context", lambda: context)
    assert not gateway.probe(False)
    assert gateway.probe(True)
