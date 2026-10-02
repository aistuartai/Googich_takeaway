#!/bin/bash
# Installs or upgrades the Googich Takeaway update helper. Run as root on the Docker host:
#
#   sudo bash install-updater.sh [folder holding compose.yaml, default /opt/googich]
#
# The same command installs it the first time and upgrades it later. It reads the version you
# are running from compose.yaml, downloads that release's helper files from GitHub, puts them
# in place for your folder, sets up <folder>/data/updater, and starts watching for updates.
set -euo pipefail

REPOSITORY="aistuartai/Googich_takeaway"
IMAGE="ghcr.io/aistuartai/googich_takeaway"
LIB="${GOOGICH_LIB_DIR:-/usr/local/lib/googich-updater}"   # changed only by the tests
UNITS="${GOOGICH_UNIT_DIR:-/etc/systemd/system}"

say() { printf '%s\n' "$*"; }
die() { printf 'install-updater: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run this as root (sudo bash install-updater.sh)"
command -v curl >/dev/null || die "curl is needed"
command -v systemctl >/dev/null || die "systemd is needed"

dir=$(cd "${1:-/opt/googich}" 2>/dev/null && pwd -P) || die "no folder ${1:-/opt/googich}"
compose="$dir/compose.yaml"
[ -f "$compose" ] || die "no compose.yaml in $dir"
[ -d "$dir/data" ] && [ ! -L "$dir/data" ] || die "no data folder in $dir"
case "$dir" in *[!A-Za-z0-9/._-]*) die "the folder path may only hold letters, digits and / . _ -" ;; esac

line=$(grep -E "^[[:space:]]*image:[[:space:]]*\"?$IMAGE:" "$compose" | head -1 || true)
if [[ "$line" =~ $IMAGE:([0-9]+\.[0-9]+\.[0-9]+)([\"[:space:]]|$) ]]; then
  version="${BASH_REMATCH[1]}"
else
  die "compose.yaml should name a fixed version, such as $IMAGE:0.3.4"
fi

say "Installing the update helper from release $version for $dir"
# Release files, not the repository: the release workflow publishes them with SHA-256 sums
# (and a build attestation), and every file must match its sum before anything is installed.
base="https://github.com/$REPOSITORY/releases/download/v$version"
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
for file in SHA256SUMS googich-updater.sh googich-updater.path googich-updater.service; do
  curl -fsSL "$base/$file" -o "$work/$file" \
    || die "could not download $file for $version (releases before 0.3.5 have no helper files)"
done
(
  cd "$work"
  grep -E '  googich-updater\.(sh|path|service)$' SHA256SUMS > wanted.sums
  [ "$(wc -l < wanted.sums)" = 3 ] || exit 1
  sha256sum --quiet -c wanted.sums
) || die "the downloaded helper files do not match the release's checksums; nothing installed"

# Point the units at this folder (they name /opt/googich).
sed -i "s|/opt/googich|$dir|g" "$work/googich-updater.path" "$work/googich-updater.service"

install -d -m 755 "$LIB"
install -m 755 "$work/googich-updater.sh" "$LIB/googich-updater.sh"
install -m 644 "$work/googich-updater.path" "$UNITS/googich-updater.path"
install -m 644 "$work/googich-updater.service" "$UNITS/googich-updater.service"

# The app may add its request here, but only root may replace or remove root's files.
# The container owns data/, so it could swap data/updater for a link at any moment: change
# the folder only from inside it, after checking it really is <folder>/data/updater.
group=$(stat -c %g "$dir/data")
mkdir -p "$dir/data/updater"
cd "$dir/data/updater"
[ "$(pwd -P)" = "$dir/data/updater" ] \
  || die "$dir/data/updater is a link; remove it and run this again"
chown root:"$group" .
chmod 1770 .
rm -f ./.lock ./.release.json  # left by helper version 1
cd /

systemctl daemon-reload
systemctl enable --now googich-updater.path >/dev/null
systemctl start googich-updater.service || true   # reports "Ready for updates." to the app
say "Done. The app shows one-click updates as ready under Help -> Updates."
