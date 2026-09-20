#!/usr/bin/env bash
set -euo pipefail
image="ghcr.io/${GITHUB_REPOSITORY,,}"
# A delayed rerun of an older SHA must not roll moving tags backwards.
git fetch origin --tags
newer=false
published_tags=$(gh release list --limit 1000 --json tagName,isDraft --jq '.[] | select(.isDraft == false) | .tagName')
while IFS= read -r tag; do
  if [[ "$tag" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    target=$(git rev-parse "$tag^{commit}")
    if [[ "$target" != "$GITHUB_SHA" ]] && git merge-base --is-ancestor "$GITHUB_SHA" "$target"; then
      newer=true
    fi
  fi
done <<< "$published_tags"
if [[ "$newer" == false ]]; then
  docker buildx imagetools create --tag "$image:latest" --tag "$image:main" "$image:$VERSION"
  docker buildx imagetools create --tag "$image:all-in-one" "$image:all-in-one-$VERSION"
  if [[ "${PUBLISH_DOCKERHUB:-false}" == true ]]; then
    docker buildx imagetools create --tag ac1dburn/plextraktsync:latest --tag ac1dburn/plextraktsync:main "ac1dburn/plextraktsync:$VERSION"
  fi
fi
# Use the same job/token, not a tag-triggered workflow (bot pushes don't trigger it).
if ! gh release view "$RELEASE_TAG" >/dev/null 2>&1; then
  gh release create "$RELEASE_TAG" --verify-tag --title "PlexTraktSync $VERSION" --generate-notes --latest=false
fi
if [[ "$newer" == false ]]; then
  gh release edit "$RELEASE_TAG" --latest
fi
