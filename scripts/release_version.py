"""Reserve one patch version per commit; retries reuse the same tag."""

from __future__ import annotations

import os
import re
import subprocess


PATTERN = re.compile(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def select_version(tags, at_head):
    versions = {tag: tuple(map(int, match.groups())) for tag in tags if (match := PATTERN.fullmatch(tag))}
    existing = [tag for tag in at_head if tag in versions]
    if existing:
        tag = max(existing, key=versions.get)
        return tag, ".".join(map(str, versions[tag])), False
    major, minor, patch = max(versions.values(), default=(2, 0, 1))
    version = f"{major}.{minor}.{patch + 1}"
    return "v" + version, version, True


def git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def main():
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        tag, version, created = "", "0.0.0-pr." + os.environ["GITHUB_RUN_NUMBER"], False
    else:
        git("fetch", "origin", "--tags")
        tag, version, created = select_version(git("tag", "--list").splitlines(), git("tag", "--points-at", "HEAD").splitlines())
        if created:
            git("tag", tag, "HEAD")
            git("push", "origin", "refs/tags/" + tag)
    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
        output.write(f"version={version}\ntag={tag}\n")
    print(f"Release version: {version} ({'reserved' if created else 'reused or preview'})")


if __name__ == "__main__":
    main()
