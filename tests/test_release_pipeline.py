from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("newer,missing_gateway", [(False, False), (True, False), (False, True)])
def test_image_promotion_waits_for_gateway_and_preserves_newer_release(tmp_path, newer, missing_gateway):
    remote, checkout = tmp_path / "remote.git", tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args], text=True).strip()

    git("config", "user.email", "test@example.invalid")
    git("config", "user.name", "Test")
    git("commit", "--allow-empty", "-m", "first release")
    sha = git("rev-parse", "HEAD")
    git("tag", "v2.0.1")
    git("commit", "--allow-empty", "-m", "newer release")
    git("tag", "v2.0.2")
    git("push", "origin", "HEAD", "--tags")
    git("checkout", sha)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.jsonl"
    for tool in ("docker", "gh"):
        executable = bin_dir / tool
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "args = sys.argv[1:]\n"
            f"tool = {tool!r}\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as output:\n"
            "    output.write(json.dumps([tool, *args]) + '\\n')\n"
            "if tool == 'gh' and args[:2] == ['release', 'list']:\n"
            "    print(os.environ['PUBLISHED_TAG'])\n"
            "if tool == 'docker' and 'inspect' in args and os.environ['MISSING_GATEWAY'] == 'true':\n"
            "    raise SystemExit(1)\n"
        )
        executable.chmod(0o755)
    env = {
        **os.environ, "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "COMMAND_LOG": str(log), "PUBLISHED_TAG": "v2.0.2" if newer else "v2.0.1",
        "MISSING_GATEWAY": str(missing_gateway).lower(), "GITHUB_REPOSITORY": "ac1dburnz/PlexTraktSync",
        "GITHUB_SHA": sha, "VERSION": "2.0.1", "RELEASE_TAG": "v2.0.1", "PUBLISH_DOCKERHUB": "false",
    }
    result = subprocess.run(["bash", str(SCRIPT.with_name("publish_release.sh"))], cwd=checkout, env=env, capture_output=True)
    assert result.returncode == (1 if missing_gateway else 0), result.stderr.decode()
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    promotions = [cmd for cmd in commands if cmd[:4] == ["docker", "buildx", "imagetools", "create"]]
    if newer or missing_gateway:
        assert promotions == []
        assert ["gh", "release", "edit", "v2.0.1", "--latest"] not in commands
    else:
        image = "ghcr.io/ac1dburnz/plextraktsync"
        assert [cmd[-1] for cmd in promotions] == [
            image + ":2.0.1", image + ":all-in-one-2.0.1", image + ":wireguard-gateway-2.0.1",
        ]
        assert promotions[-1][4:6] == ["--tag", image + ":wireguard-gateway"]
        assert ["gh", "release", "edit", "v2.0.1", "--latest"] in commands
