#!/bin/bash
# Prepares a release: bumps the version everywhere it is written, dates the CHANGELOG section,
# runs every check, then commits and tags. It does not push; it prints the commands to do that.
#
#   scripts/release.sh 0.4.0
#
# Stops at the first problem, leaving the working tree for you to inspect (git diff).
set -euo pipefail

new="${1:-}"
[[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: scripts/release.sh X.Y.Z" >&2; exit 2; }
cd "$(git rev-parse --show-toplevel)"

[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "release from main" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$new" >/dev/null && { echo "v$new already exists" >&2; exit 1; }

old=$(sed -nE 's/^version = "([0-9]+\.[0-9]+\.[0-9]+)"$/\1/p' pyproject.toml)
[ -n "$old" ] || { echo "no version in pyproject.toml" >&2; exit 1; }
[ "$old" != "$new" ] || { echo "already at $new" >&2; exit 1; }
grep -q '^## \[Unreleased\]' CHANGELOG.md || { echo "CHANGELOG.md has no Unreleased section" >&2; exit 1; }
if [ -z "$(awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{exit} f && NF' CHANGELOG.md)" ]; then
  echo "the CHANGELOG's Unreleased section is empty: say what changed first" >&2
  exit 1
fi
echo "Releasing $new (was $old)"

# Every place a version is written, from the same list the release tests check.
sed -i -E "s/^version = \"$old\"$/version = \"$new\"/" pyproject.toml
sed -i -E "s|(ghcr\.io/aistuartai/googich_takeaway:)$old|\1$new|" docker/compose.yaml
sed -i -E "s|(Googich_takeaway/)v$old/|\1v$new/|g" README.md
sed -i -E "s|(releases/download/)v$old/|\1v$new/|g" src/googich_takeaway/docs/updates.md

# CHANGELOG: the Unreleased notes become this release's section; links updated.
today=$(date +%Y-%m-%d)
sed -i "0,/^## \[Unreleased\]$/s//## [Unreleased]\n\n## [$new] - $today/" CHANGELOG.md
sed -i -E "s|^\[Unreleased\]: (.*)/compare/v$old\.\.\.HEAD$|[Unreleased]: \1/compare/v$new...HEAD\n[$new]: \1/compare/v$old...v$new|" CHANGELOG.md

uv lock -q
echo "Checking on Python 3.13"
uv run ruff check -q src tests
uv run ruff format --check -q src tests
uv run mypy src tests >/dev/null
uv run pytest -q -p no:warnings | tail -1
echo "Checking on Python 3.14"
env314=$(mktemp -d)
trap 'rm -rf "$env314"' EXIT
UV_PROJECT_ENVIRONMENT="$env314" uv sync -q --locked --python 3.14
UV_PROJECT_ENVIRONMENT="$env314" uv run --python 3.14 --no-sync \
  pytest -q -p no:cacheprovider -p no:warnings | tail -1
bash -n deploy/updater/*.sh

git add -A
git commit -q -m "Googich Takeaway $new"
git tag -a "v$new" -m "Googich Takeaway $new"
echo
echo "Committed and tagged v$new. To publish:"
echo "  git push origin main && git push origin v$new"
