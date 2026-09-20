# Automatic fork releases

One orchestrator (`CI`) validates pull requests and releases every successful push to `main`, including PR merges. There are no independent scheduled, tag-triggered or path-filtered Docker publishers.

## Sequence

1. Run Python tests and Ruff on PRs **and main**.
2. For main, allocate the next patch version from numeric Git tags: `2.0.2` → `v2.0.3`. Push the tag on the exact checked-out commit. A retry reuses that commit's tag; no bump commit or extra PR is needed. PR builds use a non-published preview version.
3. Build and test the slim and all-in-one variants with the same `APP_VERSION`. The all-in-one tests include browser startup and both offline UI suites. Slim builds validate amd64, arm64 and arm/v7 on PRs as well as main; all-in-one remains amd64, matching the existing production platform.
4. Publish versioned images only on main. After **both** jobs succeed, promote moving tags and create a GitHub Release. A partial build failure does not promote either moving tag. A network failure during promotion can leave aliases partially updated; rerun the failed jobs to finish.

| Image tag | Contents |
| --- | --- |
| `ghcr.io/ac1dburnz/plextraktsync:2.0.3` | Versioned slim image |
| `ghcr.io/ac1dburnz/plextraktsync:all-in-one-2.0.3` | Versioned browser/list-bridge image |
| `latest`, `main` | Most recently promoted slim version |
| `all-in-one` | Most recently promoted all-in-one version; keep this in TrueNAS |

Both application version output and OCI version metadata use the release version. Docker Hub publication of the slim variant remains optional: set repository variable `PUBLISH_DOCKERHUB=true` and secrets `DOCKER_USERNAME` / `DOCKER_PASSWORD`. GHCR uses `GITHUB_TOKEN`; no personal token is needed. Ensure repository rules permit Actions to create `v*` tags and publish packages.

## Retries and ordering

The workflow queues main releases (`queue: max`, up to GitHub's 100 pending-run limit) rather than canceling earlier merges. A failed build can leave a reserved Git tag without a GitHub Release. Rerun the failed jobs on that run to reuse its version. Image versions are not newly allocated on a retry. Older reruns can publish their versioned images but cannot replace moving tags once a descendant commit has a published release.

Tag creation and publication happen in the same workflow. GitHub normally suppresses workflows triggered by a `GITHUB_TOKEN` tag push, so release correctness does not rely on a second tag-triggered run. Manual `CI` dispatch on main can retry/rebuild; dispatches on other branches do not publish.

The upstream PyPI workflow is restricted to `Taxel/PlexTraktSync` and no longer has duplicate tag/release triggers. This fork distributes Docker images, not the upstream Python package. Historical failed runs remain in Actions; the replacement applies after this PR merges.

## Validation

Local full Python suite, real temporary-Git version allocation/retry tests, image builds, offline self-tests and browser tests. `actionlint` 1.7.12 predates GitHub's documented `queue` property: validate with `actionlint -ignore 'unexpected key "queue" for "concurrency" section'` until the checker supports it; other workflow errors are not ignored. Also run `shellcheck scripts/publish_release.sh`.

References: [GitHub workflow concurrency and queue](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/control-workflow-concurrency), [GITHUB_TOKEN workflow triggers](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow).
