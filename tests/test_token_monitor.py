from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parents[1] / 'browser-helper'))
from token_monitor import TokenMonitor, validate


@pytest.mark.parametrize('status,expected', [
    (200,'healthy'),(401,'login_required'),(403,'unavailable'),
    (429,'unavailable'),(500,'unavailable'),(302,'unavailable'),
])
def test_statuses(status, expected):
    response = SimpleNamespace(status_code=status, json=lambda: {'user': {'username': 'test'}})
    assert validate({'access_token':'test','client_id':'key'}, lambda *a, **k: response) == expected


def test_network_not_expiry():
    def get(*args, **kwargs):
        raise requests.ConnectionError('private URL')
    assert validate({'access_token':'test','client_id':'key'}, get) == 'unavailable'


def test_malformed_server_response_not_expiry():
    def invalid():
        raise ValueError('bad json')
    response = SimpleNamespace(status_code=200, json=invalid)
    assert validate({'access_token':'test','client_id':'key'}, lambda *a, **k: response) == 'unavailable'


def test_monitor_expiry_and_recovery(tmp_path):
    output = tmp_path / 'token.json'
    alert = Mock()
    monitor = TokenMonitor(output,tmp_path,alert, lambda *a, **k: SimpleNamespace(status_code=200,json=lambda:{'user':{'username':'test'}}))
    output.write_text(json.dumps({'access_token':'private','client_id':'key','expires_at':1}))
    assert monitor.check() == 'login_required'
    alert.update.assert_called_with(True)
    assert not (tmp_path/'auth-ready').exists()
    output.write_text(json.dumps({'access_token':'private','client_id':'key'}))
    assert monitor.check() == 'healthy'
    alert.update.assert_called_with(False)
    assert (tmp_path/'auth-ready').exists()
    assert 'private' not in (tmp_path/'auth-status.json').read_text()
