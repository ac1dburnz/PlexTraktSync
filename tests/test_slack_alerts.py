from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

spec = importlib.util.spec_from_file_location('alerts', Path(__file__).parents[1] / 'browser-helper/alerts.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_disabled(tmp_path, monkeypatch):
    monkeypatch.delenv('SLACK_WEBHOOK_URL', raising=False)
    calls = []
    module.SlackAlerts(tmp_path / 'state', lambda *a, **k: calls.append(k)).update(True)
    assert calls == []


def test_persistent_dedup_and_recovery(tmp_path, monkeypatch):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.com/services/test')
    calls = []
    def send(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(status_code=200, text='ok')
    path = tmp_path / 'state'
    module.SlackAlerts(path, send).update(True)
    module.SlackAlerts(path, send).update(True)
    assert len(calls) == 1
    module.SlackAlerts(path, send).update(False)
    module.SlackAlerts(path, send).update(True)
    assert len(calls) == 2
    assert 'Authorization' not in str(calls)


def test_failure_cooldown_and_no_secret_logging(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv('SLACK_WEBHOOK_URL', 'https://hooks.slack.com/services/secret')
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError('secret')
    alert = module.SlackAlerts(tmp_path / 'state', fail)
    alert.update(True)
    alert.update(True)
    assert len(calls) == 1
    assert 'secret' not in caplog.text
