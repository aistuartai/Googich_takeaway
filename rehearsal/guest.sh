#!/bin/bash
# Inside the throwaway rehearsal VM, as root (rehearsal/proxmox.sh runs it):
#
#   sudo bash guest.sh --from 0.6.0-rc1 --to 0.6.0-rc2
#
# Installs Docker as its own install guide says, fetches the end-to-end tests from the --to
# release's tag, and runs e2e/test_rehearsal.py: install --from as the README says, then update
# to --to through the web interface and the update helper. Results go to ~rehearsal/results:
# result.json, junit.xml, screenshots and logs.
#
# Never run this anywhere else: it installs into /opt/googich and edits /etc/hosts.
set -euo pipefail

REPOSITORY="aistuartai/Googich_takeaway"
UV_VERSION="0.13.1"
VERSION='^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$'
say() { printf '%s\n' "$*"; }
die() { printf 'guest: %s\n' "$*" >&2; exit 1; }

from="" to=""
while [ $# -gt 0 ]; do
  case "$1" in
    --from) from="${2:-}"; shift 2 ;;
    --to) to="${2:-}"; shift 2 ;;
    *) die "unknown argument $1" ;;
  esac
done
[[ "$from" =~ $VERSION ]] && [[ "$to" =~ $VERSION ]] || die "use --from VERSION --to VERSION"
[ "$(id -u)" = 0 ] || die "run as root"
grep -qs "googich-rehearsal" /etc/hostname || die "this is not a rehearsal VM"

results=/home/rehearsal/results
mkdir -p "$results"
export DEBIAN_FRONTEND=noninteractive

say "Installing Docker"
apt-get update -q
apt-get install -y -q ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' \
  "$(dpkg --print-architecture)" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list
apt-get update -q
apt-get install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker compose version

say "Fetching the tests from v$to"
rm -rf /root/googich-src
git clone -q --depth 1 --branch "v$to" "https://github.com/$REPOSITORY.git" /root/googich-src
commit=$(git -C /root/googich-src rev-parse HEAD)
curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-installer.sh" \
  | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh
cd /root/googich-src
uv sync -q --locked --only-group e2e
uv run --no-sync playwright install --with-deps chromium

say "Rehearsing $from to $to"
set +e
E2E_REHEARSAL=1 E2E_FROM="$from" E2E_TO="$to" E2E_ARTIFACTS="$results" \
  uv run --no-sync pytest e2e/test_rehearsal.py -v -p no:cacheprovider \
  --junitxml="$results/junit.xml"
outcome=$?
set -e

passed=false
[ "$outcome" = 0 ] && passed=true
printf '{"from": "%s", "to": "%s", "commit": "%s", "passed": %s, "at": "%s"}\n' \
  "$from" "$to" "$commit" "$passed" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$results/result.json"
chown -R rehearsal:rehearsal "$results"
exit "$outcome"
