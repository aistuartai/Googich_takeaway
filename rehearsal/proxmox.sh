#!/bin/bash
# Release rehearsal, run as root on a Proxmox VE node:
#
#   bash rehearsal/proxmox.sh --to 0.6.0-rc2 --from 0.6.0-rc1 [--keep]
#
# Clones a throwaway VM from a Debian 13 cloud-init template, rehearses the update from one
# published release to another inside it (rehearsal/guest.sh), copies back the result and
# screenshots, and destroys the VM. Nothing else on the node is changed. --from defaults to
# --to (a fresh install only). --keep leaves the VM running to look into, and prints how to reach
# and destroy it.
#
# Settings come from a file on the node, never from this repository (see rehearsal/README.md):
#   ${GOOGICH_REHEARSAL_CONF:-/root/.config/googich-rehearsal.env}
set -euo pipefail

say() { printf '%s\n' "$*"; }
die() { printf 'rehearsal: %s\n' "$*" >&2; exit 1; }

HERE=$(cd "$(dirname "$0")" && pwd -P)
CONF="${GOOGICH_REHEARSAL_CONF:-/root/.config/googich-rehearsal.env}"
VERSION='^[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$'
OWNER_UID="${GOOGICH_REHEARSAL_OWNER:-0}"   # tests run as an ordinary user

to="" from="" keep=no
while [ $# -gt 0 ]; do
  case "$1" in
    --to) to="${2:-}"; shift 2 ;;
    --from) from="${2:-}"; shift 2 ;;
    --keep) keep=yes; shift ;;
    *) die "unknown argument $1 (use --to VERSION [--from VERSION] [--keep])" ;;
  esac
done
[[ "$to" =~ $VERSION ]] || die "--to should be a published version, such as 0.6.0-rc2"
from="${from:-$to}"
[[ "$from" =~ $VERSION ]] || die "--from should be a published version, such as 0.6.0-rc1"

[ "$(id -u)" = "$OWNER_UID" ] || die "run this as root on the Proxmox node"
for command in qm pvesh python3 ssh scp ssh-keygen; do
  command -v "$command" >/dev/null || die "$command is needed (is this a Proxmox node?)"
done

# The settings file: root's alone, as it names parts of the network.
[ -f "$CONF" ] || die "no settings file $CONF; see rehearsal/README.md"
[ "$(stat -c '%u %a' "$CONF")" = "$OWNER_UID 600" ] || die "$CONF must be owned by root with mode 600"
# shellcheck source=/dev/null
. "$CONF"
for name in TEMPLATE STORAGE FIRST_VMID SNIPPETS_STORAGE SNIPPETS_DIR RESULTS; do
  [ -n "${!name:-}" ] || die "$CONF does not set $name"
done
DISK_GROW="${DISK_GROW:-+20G}"
MEMORY="${MEMORY:-4096}"
CORES="${CORES:-2}"
[ -d "$SNIPPETS_DIR" ] || die "no snippets folder $SNIPPETS_DIR (SNIPPETS_DIR)"

# The first free VM ID from FIRST_VMID up.
vmid="$FIRST_VMID"
until pvesh get /cluster/nextid --vmid "$vmid" >/dev/null 2>&1; do
  vmid=$((vmid + 1))
  [ "$vmid" -lt $((FIRST_VMID + 100)) ] || die "no free VM ID from $FIRST_VMID"
done
name="googich-rehearsal-$vmid"
work=$(mktemp -d)
snippet="googich-rehearsal-$vmid.yaml"
created=no

address=""
finish() {
  local code=$?
  if [ "$created" = yes ] && [ "$keep" = yes ]; then  # also when it stopped part way
    say "VM $vmid kept${address:+: ssh -i $work/key rehearsal@$address}"
    say "When done: qm stop $vmid && qm destroy $vmid --purge 1; rm -f $SNIPPETS_DIR/$snippet; rm -rf $work"
  fi
  if [ "$created" = yes ] && [ "$keep" = no ]; then
    say "Destroying VM $vmid"
    qm stop "$vmid" --skiplock 1 >/dev/null 2>&1 || true
    qm destroy "$vmid" --purge 1 --skiplock 1 >/dev/null 2>&1 || say "could not destroy VM $vmid: do it by hand"
  fi
  if [ "$keep" = no ]; then
    rm -f "$SNIPPETS_DIR/$snippet"
    rm -rf "$work"
  fi
  exit "$code"
}
trap finish EXIT

# A key for this run only, so no lasting key reaches the VM.
ssh-keygen -q -t ed25519 -N '' -C "$name" -f "$work/key"
ssh_opts=(-i "$work/key" -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new
  -o UserKnownHostsFile="$work/known_hosts")

# The cloud image has no guest agent: it is installed at first boot, so the VM's address can be
# read through it.
cat > "$SNIPPETS_DIR/$snippet" <<'EOF'
#cloud-config
package_update: true
packages:
  - qemu-guest-agent
runcmd:
  - [systemctl, enable, --now, qemu-guest-agent]
EOF

say "Cloning template $TEMPLATE to VM $vmid ($name)"
qm clone "$TEMPLATE" "$vmid" --name "$name" --full 1 --storage "$STORAGE" >/dev/null
created=yes
qm set "$vmid" --ciuser rehearsal --sshkeys "$work/key.pub" --memory "$MEMORY" --cores "$CORES" \
  --cicustom "vendor=$SNIPPETS_STORAGE:snippets/$snippet" >/dev/null
qm resize "$vmid" scsi0 "$DISK_GROW" >/dev/null
qm start "$vmid"

say "Waiting for the VM to boot and install its guest agent"
for _ in $(seq 1 120); do
  qm agent "$vmid" ping >/dev/null 2>&1 && break
  sleep 5
done
qm agent "$vmid" ping >/dev/null 2>&1 || die "the guest agent never answered"
address=""
for _ in $(seq 1 30); do
  address=$(qm guest cmd "$vmid" network-get-interfaces 2>/dev/null | python3 -c '
import json, sys
for interface in json.load(sys.stdin):
    if interface.get("name") == "lo":
        continue
    for ip in interface.get("ip-addresses", []):
        if ip.get("ip-address-type") == "ipv4":
            print(ip["ip-address"]); sys.exit()
' || true)
  [ -n "$address" ] && break
  sleep 5
done
[ -n "$address" ] || die "the VM never had an IPv4 address"
for _ in $(seq 1 30); do
  ssh "${ssh_opts[@]}" "rehearsal@$address" true 2>/dev/null && break
  sleep 5
done
ssh "${ssh_opts[@]}" "rehearsal@$address" true 2>/dev/null || die "the VM never accepted SSH at $address"

say "Rehearsing $from to $to in VM $vmid"
scp -q "${ssh_opts[@]}" "$HERE/guest.sh" "rehearsal@$address:guest.sh"
set +e
ssh "${ssh_opts[@]}" "rehearsal@$address" sudo bash guest.sh --from "$from" --to "$to"
outcome=$?
set -e

out="$RESULTS/$to-from-$from-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$out"
chmod 700 "$RESULTS" "$out"
scp -q -r "${ssh_opts[@]}" "rehearsal@$address:results/." "$out/" \
  || say "could not copy the results back"
say ""
if [ "$outcome" = 0 ]; then
  say "PASSED: $from to $to. Result and screenshots in $out"
else
  say "FAILED: $from to $to. See $out (junit.xml, screenshots, logs)"
fi
exit "$outcome"
