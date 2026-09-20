from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / 'scripts/release_version.py'
spec = importlib.util.spec_from_file_location('release_version', SCRIPT)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_version_ignores_non_semver_and_bumps_numeric_patch():
    assert release.select_version(['2.0.2', 'v2.0.10', '1.1.1.1', 'v99.0.0-beta'], []) == ('v2.0.11', '2.0.11', True)
    assert release.select_version([], []) == ('v2.0.2', '2.0.2', True)


def test_retry_reuses_tag_even_after_newer_release():
    assert release.select_version(['2.0.2', 'v2.0.3'], ['2.0.2']) == ('2.0.2', '2.0.2', False)


def test_reservation_push_and_retry_with_real_git(tmp_path):
    remote, checkout = tmp_path/'remote.git', tmp_path/'checkout'
    subprocess.run(['git', 'init', '--bare', str(remote)], check=True, capture_output=True)
    subprocess.run(['git', 'clone', str(remote), str(checkout)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(['git', '-C', str(checkout), *args], text=True).strip()

    git('config', 'user.email', 'test@example.invalid')
    git('config', 'user.name', 'Test')
    git('commit', '--allow-empty', '-m', 'base')
    git('tag', '2.0.2')
    git('push', 'origin', 'HEAD', '--tags')
    git('commit', '--allow-empty', '-m', 'merge PR')
    output = tmp_path/'output'
    env = {**os.environ, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_OUTPUT': str(output)}
    subprocess.run([sys.executable, str(SCRIPT)], cwd=checkout, env=env, check=True, capture_output=True)
    assert 'version=2.0.3' in output.read_text()
    assert git('rev-parse', 'v2.0.3') == git('rev-parse', 'HEAD')
    subprocess.run([sys.executable, str(SCRIPT)], cwd=checkout, env=env, check=True, capture_output=True)
    assert git('tag', '--list').splitlines() == ['2.0.2', 'v2.0.3']
    # PR validation never reserves a version or pushes a tag.
    env.update(GITHUB_EVENT_NAME='pull_request', GITHUB_RUN_NUMBER='123')
    subprocess.run([sys.executable, str(SCRIPT)], cwd=checkout, env=env, check=True, capture_output=True)
    assert 'version=0.0.0-pr.123' in output.read_text()
    assert git('tag', '--list').splitlines() == ['2.0.2', 'v2.0.3']
