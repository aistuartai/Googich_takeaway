#!/bin/bash
# Googich Takeaway update helper. Runs on the Docker host, outside the container, as root.
#
# The web interface cannot touch Docker. When you press "Update now" it only writes
#   <data>/updater/request.json   {"version": "X.Y.Z"}
# and this script, started by googich-updater.path, does the update:
#   1. accepts nothing but a plain X.Y.Z version from that file
#   2. checks it is a published, non-pre-release GitHub release of this project
#   3. sets that tag on the image line in compose.yaml (a backup is kept)
#   4. pulls and restarts the container, then waits for /healthz to report the new version
#   5. on any failure, restores the previous compose.yaml and restarts the old version
# Progress and the result go to <data>/updater/status.json, which the web interface shows.
set -euo pipefail

COMPOSE_DIR="${GOOGICH_COMPOSE_DIR:-/opt/googich}"
DATA_DIR="${GOOGICH_DATA_DIR:-$COMPOSE_DIR/data}"
IMAGE="${GOOGICH_IMAGE:-ghcr.io/aistuartai/googich_takeaway}"
REPOSITORY="${GOOGICH_REPOSITORY:-aistuartai/Googich_takeaway}"
SERVICE="${GOOGICH_SERVICE:-googich}"
HEALTH_URL="${GOOGICH_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
HELPER_VERSION="1"

UPDATER_DIR="$DATA_DIR/updater"
REQUEST="$UPDATER_DIR/request.json"
STATUS="$UPDATER_DIR/status.json"
COMPOSE="$COMPOSE_DIR/compose.yaml"

json_text() {  # escape for a JSON string; messages are this script's own text
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\000-\037'
}

status() {  # state message [version]
  local tmp
  tmp=$(mktemp "$UPDATER_DIR/.status.XXXXXX")
  printf '{"helper": "%s", "state": "%s", "message": "%s", "version": "%s", "at": "%s"}\n' \
    "$HELPER_VERSION" "$1" "$(json_text "$2")" "$(json_text "${3:-}")" \
    "$(date -u +%Y-%m-%dT%H:%M:%S+00:00)" > "$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$STATUS"
}

exec 9>"$UPDATER_DIR/.lock"
flock -n 9 || exit 0   # one update at a time

if [ ! -f "$REQUEST" ]; then
  # Started by installation, or by the request file being removed: keep the last result.
  [ -f "$STATUS" ] || status idle "Ready for updates."
  exit 0
fi
version=$(grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]{0,20}"' "$REQUEST" 2>/dev/null \
  | head -1 | sed -E 's/.*"([^"]*)"$/\1/' || true)
rm -f "$REQUEST"
if ! [[ "$version" =~ ^[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}$ ]]; then
  status failed "Refused: the requested version was not a plain version number."
  exit 1
fi

status updating "Checking that $version is a published release." "$version"
release=$(curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/$REPOSITORY/releases/tags/v$version" 2>/dev/null || true)
flat=$(printf '%s' "$release" | tr -d '\n\r\t ')
if ! { printf '%s' "$flat" | grep -q "\"tag_name\":\"v$version\"" \
       && printf '%s' "$flat" | grep -q '"draft":false' \
       && printf '%s' "$flat" | grep -q '"prerelease":false'; }; then
  status failed "Refused: $version is not a published release of $REPOSITORY." "$version"
  exit 1
fi

current=$(grep -E "^[[:space:]]*image:[[:space:]]*\"?$IMAGE:" "$COMPOSE" | head -1 \
  | sed -E "s|.*$IMAGE:||" | tr -cd 'A-Za-z0-9._-' || true)
if [ -z "$current" ]; then
  status failed "Refused: no image line for $IMAGE found in $COMPOSE." "$version"
  exit 1
fi

cp -p "$COMPOSE" "$COMPOSE.before-$version"
sed -i -E "s|^([[:space:]]*image:[[:space:]]*\"?)$IMAGE:[^\"[:space:]]+|\\1$IMAGE:$version|" "$COMPOSE"

rollback() {
  cp -p "$COMPOSE.before-$version" "$COMPOSE"
  (cd "$COMPOSE_DIR" && docker compose up -d "$SERVICE") >/dev/null 2>&1 || true
  status failed "$1 Rolled back to $current." "$version"
  exit 1
}

status updating "Downloading $version." "$version"
(cd "$COMPOSE_DIR" && docker compose pull "$SERVICE") >/dev/null 2>&1 || rollback "Could not download $version."
status updating "Restarting with $version." "$version"
(cd "$COMPOSE_DIR" && docker compose up -d "$SERVICE") >/dev/null 2>&1 || rollback "Could not start $version."

for _ in $(seq 1 60); do
  if curl -fsS --max-time 3 "$HEALTH_URL" 2>/dev/null | grep -q "\"version\":\"$version\""; then
    status done "Updated from $current to $version." "$version"
    exit 0
  fi
  sleep 2
done
rollback "$version did not become healthy within two minutes."
