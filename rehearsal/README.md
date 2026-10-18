# Release rehearsal

Before a final release, its release candidate is rehearsed on a throwaway virtual machine:
the earlier release installed as the README says, the update helper installed with the command
the app shows, then the update made through **Update now**, a rollback forced, and the helper's
refusals checked. Everything runs against releases published on GitHub, with real Docker,
systemd and a real browser.

The machine is cloned from a Debian 13 cloud-init template on a Proxmox VE node, used once and
destroyed. Nothing about the node or its network is kept in this repository: the script reads it
from a settings file on the node.

## Settings, once per node

As root on the node, create `/root/.config/googich-rehearsal.env` with mode 600:

```sh
TEMPLATE=<template VM ID>           # Debian 13 genericcloud with cloud-init, network set
STORAGE=<storage for the clone>     # a storage that holds VM disks
FIRST_VMID=<first VM ID to use>     # free IDs from here up
SNIPPETS_STORAGE=<storage id>       # a storage with the "snippets" content type
SNIPPETS_DIR=<its snippets folder>  # such as /var/lib/vz/snippets
RESULTS=/root/googich-rehearsals    # where results are kept
# Optional: DISK_GROW=+20G MEMORY=4096 CORES=2
```

## Running it

Copy this folder to the node (or clone the repository there), then as root:

```sh
bash rehearsal/proxmox.sh --from 0.6.0-rc1 --to 0.6.0-rc2
```

`--from` is the published release installed first and `--to` the one updated to; both must be
published on GitHub. Leave out `--from` to rehearse a fresh install of `--to` only. `--keep`
leaves the machine running for a look inside.

It takes about ten minutes. The last line says PASSED or FAILED, and where the result is:
`result.json` (versions, the commit tested, passed or not), `junit.xml`, screenshots of every
step, and the app's, helper's and browser's logs.

## What it checks

- The earlier release installs from its own example compose file and sets up from the token in
  its logs.
- The update helper installs with the command Help → Updates shows, checksum and all.
- The newer release is offered (with release candidates switched on, for a candidate), and
  What's new shows what it brings.
- With the image registry unreachable, **Update now** fails and the helper rolls back.
- Then **Update now** installs it: the helper reports done, the new version answers, and the
  password and settings are kept.
- The helper refuses a candidate not asked for as one, an unpublished version, a version newer
  than any release, and anything that is not a version.
- Every page opens with no script errors or content security policy complaints.
