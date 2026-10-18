#!/bin/bash
# Prepares a release: bumps the version everywhere it is written, dates the CHANGELOG section,
# runs every check, then commits and tags. It does not push; it prints the commands to do that.
#
#   scripts/release.sh 0.4.0
#   scripts/release.sh 0.4.0-rc1   # a release candidate, published as a GitHub pre-release
#
# A release candidate changes only the version in pyproject.toml (Python writes it 0.4.0rc1): the
# README, the example compose file and the guide keep pointing at the last final release, and
# what it brings stays under Unreleased in CHANGELOG.md, which is where its notes are read from.
#
# A final release needs its last release candidate rehearsed first (rehearsal/README.md): the
# record of a passed update rehearsal to that candidate in .rehearsals/X.Y.Z-rcN.json, and nothing
# but version numbers changed since the candidate. RELEASE_WITHOUT_REHEARSAL=1 skips this, for an
# urgent fix only.
#
# Stops at the first problem, leaving the working tree for you to inspect (git diff).
set -euo pipefail

new="${1:-}"
if [[ "$new" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-rc([0-9]+)$ ]]; then
  candidate=yes
  python_version="${BASH_REMATCH[1]}rc${BASH_REMATCH[2]}"
elif [[ "$new" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  candidate=no
  python_version="$new"
else
  echo "usage: scripts/release.sh X.Y.Z or X.Y.Z-rcN" >&2
  exit 2
fi
base="${new%%-rc*}"
cd "$(git rev-parse --show-toplevel)"

[ -z "$(git status --porcelain)" ] || { echo "commit or stash your changes first" >&2; exit 1; }
[ "$(git branch --show-current)" = main ] || { echo "release from main" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/v$new" >/dev/null && { echo "v$new already exists" >&2; exit 1; }

old=$(sed -nE 's/^version = "([0-9]+\.[0-9]+\.[0-9]+(rc[0-9]+)?)"$/\1/p' pyproject.toml)
[ -n "$old" ] || { echo "no version in pyproject.toml" >&2; exit 1; }
[ "$old" != "$python_version" ] || { echo "already at $new" >&2; exit 1; }
# The last final release: what the example compose file runs. Every link and image tag names it.
stable=$(sed -nE 's|^[[:space:]]*image:[[:space:]]*ghcr\.io/aistuartai/googich_takeaway:([0-9]+\.[0-9]+\.[0-9]+)[[:space:]]*$|\1|p' docker/compose.yaml | head -1)
[ -n "$stable" ] || { echo "no final release version in docker/compose.yaml" >&2; exit 1; }
if [ "$base" = "$stable" ] || [ "$(printf '%s\n%s\n' "$base" "$stable" | sort -V | tail -1)" != "$base" ]; then
  echo "$new must be newer than the last final release, $stable" >&2
  exit 1
fi
grep -q '^## \[Unreleased\]' CHANGELOG.md || { echo "CHANGELOG.md has no Unreleased section" >&2; exit 1; }
if [ -z "$(awk '/^## \[Unreleased\]/{f=1;next} /^## \[/{exit} f && NF' CHANGELOG.md)" ]; then
  echo "the CHANGELOG's Unreleased section is empty: say what changed first" >&2
  exit 1
fi
if [ "$candidate" = no ]; then
  if [ -n "${RELEASE_WITHOUT_REHEARSAL:-}" ]; then
    echo "WARNING: releasing $new without a rehearsal (RELEASE_WITHOUT_REHEARSAL is set)" >&2
  else
    rc=$(git tag -l "v$new-rc*" | sort -V | tail -1)
    [ -n "$rc" ] || { echo "no release candidate for $new: release $new-rc1 and rehearse it first" >&2; exit 1; }
    record=".rehearsals/${rc#v}.json"
    [ -f "$record" ] || { echo "no rehearsal of ${rc#v}: copy its result.json from the node to $record" >&2; exit 1; }
    python3 - "$record" "${rc#v}" "$(git rev-list -n 1 "$rc")" <<'CHECK' || exit 1
import json, sys
path, candidate, commit = sys.argv[1:]
try:
    result = json.load(open(path))
except (OSError, ValueError) as error:
    sys.exit(f"{path} cannot be read: {error}")
problems = []
if result.get("to") != candidate:
    problems.append(f"it rehearsed {result.get('to')}, not {candidate}")
if result.get("from") in (None, "", candidate):
    problems.append("it was a fresh install only: rehearse an update to it (--from)")
if result.get("commit") != commit:
    problems.append(f"it tested commit {result.get('commit')}, but {candidate} is {commit}")
if result.get("passed") is not True:
    problems.append("it did not pass")
if problems:
    sys.exit(f"the rehearsal in {path} does not clear {candidate}: " + "; ".join(problems))
CHECK
    changed=$(git diff --name-only "$rc" HEAD -- . ':!pyproject.toml' ':!uv.lock')
    if [ -n "$changed" ]; then
      echo "changed since ${rc#v}, so not rehearsed: release another candidate first:" >&2
      echo "$changed" >&2
      exit 1
    fi
    echo "Rehearsal of ${rc#v} passed: $(python3 -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["from"], "to", r["to"], "on", r["at"])' "$record")"
  fi
fi
if [ "$candidate" = yes ]; then
  echo "Releasing candidate $new (was $old; last final release $stable)"
else
  echo "Releasing $new (was $old; last final release $stable)"
fi

# Every place a version is written, from the same list the release tests check.
sed -i -E "s/^version = \"$old\"$/version = \"$python_version\"/" pyproject.toml
if [ "$candidate" = no ]; then
  sed -i -E "s|(ghcr\.io/aistuartai/googich_takeaway:)$stable|\1$new|" docker/compose.yaml
  sed -i -E "s|(Googich_takeaway/)v$stable/|\1v$new/|g" README.md
  sed -i -E "s|(releases/download/)v$stable/|\1v$new/|g" src/googich_takeaway/docs/updates.md

  # CHANGELOG: the Unreleased notes become this release's section; links updated.
  today=$(date +%Y-%m-%d)
  sed -i "0,/^## \[Unreleased\]$/s//## [Unreleased]\n\n## [$new] - $today/" CHANGELOG.md
  sed -i -E "s|^\[Unreleased\]: (.*)/compare/v$stable\.\.\.HEAD$|[Unreleased]: \1/compare/v$new...HEAD\n[$new]: \1/compare/v$stable...v$new|" CHANGELOG.md
fi

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
