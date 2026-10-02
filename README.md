# Googich Takeaway

Self-hosted tool that moves your Google Photos library into [Immich](https://immich.app/), using
Google Takeout archives, and keeps it topped up on a schedule.

> **Status: early releases (0.3).** It works end to end and has been tested against real Google
> Drive, Immich and SMB, but it is young. Try it on a test Immich user first, and back up your
> Immich database before the first real import.

![The dashboard: Google Photos, Google Drive, the download folder and Immich, with run
controls, a cleanup summary and recent runs](assets/dashboard.png)

## What it does

- Picks up Google Takeout archives from a Google Drive folder, or a local folder, on a schedule.
- Downloads them to a folder you choose, on local disk or an SMB share (NAS), resuming if
  interrupted and checking each download against Drive's checksum.
- Reads `.zip` and `.tgz` exports, pairs every photo and video with Takeout's metadata file, and
  works out the correct capture date and time zone.
- Uploads only what Immich does not already have, with the date and location attached, then reads
  each upload back to check the date.
- Remembers what it has done: archives are not downloaded twice, and photos you delete in Immich
  are not brought back (unless you ask for a re-import).
- Shows progress with sizes and time estimates, logs, and run history in a password-protected web
  interface, and sends notifications through [Apprise](https://github.com/caronc/apprise).
- Tells you which archives are fully imported and safe to delete, locally and in Google Drive.

Google Drive access is read-only: the app never changes or deletes anything in your Google account.

## Quick start (Docker)

Requirements: Docker with Compose, an Immich server, and space for one full Takeout export
(locally or on an SMB share).

```bash
mkdir googich && cd googich
curl -fsSLO https://raw.githubusercontent.com/aistuartai/Googich_takeaway/v0.3.1/docker/compose.yaml
less compose.yaml   # read what you are about to run

# A data folder, and a master key that encrypts the credentials you enter later. Both belong to
# the app's unprivileged user inside the container, uid 10001.
mkdir -p data secrets && chmod 700 data secrets
head -c 32 /dev/urandom | base64 > secrets/master.key && chmod 600 secrets/master.key
chown -R 10001:10001 data secrets

docker compose up -d
docker compose logs googich | grep "First-run setup"
```

Straight away, open `http://<this host>:8080/setup`, enter the setup token from the log, and
choose a password. Until a password is set, anyone on your network who sees the log could claim
the install; the token keeps it to you, so do not leave a fresh install waiting.

Keep a copy of `secrets/master.key` somewhere safe outside this machine. The compose file pins a
version, so the app changes only when you update it.

Edit `compose.yaml` first if you want a different port, time zone, or a large local disk for
downloads. The web interface is meant for your local network; put it behind HTTPS (a reverse
proxy) if you reach it from anywhere else.

### Behind a reverse proxy (HTTPS)

The app works behind Nginx Proxy Manager, Caddy, Traefik or similar without any changes. The
proxy must pass the original `Host` header, which Nginx Proxy Manager does by default.

Telling the app the proxy's IP address is recommended: the session cookie is then marked secure,
and failed logins are throttled per visitor rather than for everyone coming through the proxy.
The app logs a reminder when it sees a proxy it has not been told about. In `compose.yaml`:

```yaml
    environment:
      GOOGICH_TRUSTED_PROXIES: 192.168.1.10   # the proxy's address; separate several with commas
```

Only that address's `X-Forwarded-Proto` and `X-Forwarded-For` headers are believed.

## Setting it up

The dashboard shows a checklist until the essentials are done, and **Help → Guide** in the app
explains every part. The same guide is in [`src/googich_takeaway/docs`](src/googich_takeaway/docs/README.md):

- [First-time setup](src/googich_takeaway/docs/first-setup.md), in order
- [Setting up Google Takeout](src/googich_takeaway/docs/takeout.md): a scheduled export to Drive
- [Connecting Google Drive](src/googich_takeaway/docs/google-drive.md): a read-only service account
- [Immich and the download folder](src/googich_takeaway/docs/destinations.md), including SMB shares
- [Troubleshooting](src/googich_takeaway/docs/troubleshooting.md)

## Updating

The app checks GitHub once a day and shows a banner on every page when a new release is out
(switch this off in Help → Updates). **Check now** in Help → Updates asks GitHub straight
away. The app never updates itself on its own.

To update by hand, pull the new image and restart. If `compose.yaml` names a fixed version, such
as `ghcr.io/aistuartai/googich_takeaway:0.3.1` (recommended, so updates happen only when you
choose), change that version first:

```bash
sed -i 's/googich_takeaway:0.3.0/googich_takeaway:0.3.1/' compose.yaml
docker compose pull && docker compose up -d
```

### One-click updates (optional)

With a small helper installed on the Docker host, the banner offers **Update now**. The app itself
never gets access to Docker: it only writes the version to install into
`data/updater/request.json`. The helper, a shell script started by systemd outside the container,
then:

1. accepts nothing but a plain version number such as `0.1.2`,
2. checks it is a published release of this project on GitHub (not a draft or pre-release),
3. sets that version on the image line in `compose.yaml`, keeping a copy of the old file,
4. pulls the image, restarts the container and waits for it to report the new version,
5. restores the previous version automatically if anything fails.

Progress and the result appear in the banner and under Help → Updates.

To install it, as root on the Docker host, in the folder holding `compose.yaml` (for example
`/opt/googich`), using the release you are running:

```bash
cd /opt/googich
V=0.3.1
base=https://raw.githubusercontent.com/aistuartai/Googich_takeaway/v$V/deploy/updater
install -d -m 755 /usr/local/lib/googich-updater
curl -fsSL "$base/googich-updater.sh" -o /usr/local/lib/googich-updater/googich-updater.sh
chmod 755 /usr/local/lib/googich-updater/googich-updater.sh
curl -fsSL "$base/googich-updater.path" -o /etc/systemd/system/googich-updater.path
curl -fsSL "$base/googich-updater.service" -o /etc/systemd/system/googich-updater.service
install -d -m 770 -o "$(stat -c %u data)" -g "$(stat -c %g data)" data/updater
systemctl daemon-reload
systemctl enable --now googich-updater.path
systemctl start googich-updater.service   # reports "Ready for updates." to the app
```

Read the script before installing it; it runs as root. If your install is not in
`/opt/googich`, change the path in both systemd files. To remove the helper, run
`systemctl disable --now googich-updater.path` and delete the three files.

## Backups

Back up the `data` folder: it holds the state database (what has been downloaded and uploaded,
your settings and encrypted credentials) and logs. Keep `secrets/master.key` somewhere separate;
without it the stored credentials cannot be read, and you would need to enter them again.

### Keeping the master key out of backups (Proxmox containers)

If the app runs in a Proxmox container that is backed up whole, every backup holds the master key
next to the credentials it encrypts, so the encryption no longer protects those backups. Move the
key out of the container onto the Proxmox host, and mount it back in. Proxmox never includes
bind mounts in container backups. On the Proxmox host, as root, with the container stopped
(204 and `/opt/googich` are examples):

```bash
CT=204
install -d -m 700 /srv/googich-secrets
pct pull $CT /opt/googich/secrets/master.key /srv/googich-secrets/master.key
# The container's uid 10001 is uid 110001 on the host in an unprivileged container.
chown -R 110001:110001 /srv/googich-secrets && chmod 600 /srv/googich-secrets/master.key
pct exec $CT -- rm /opt/googich/secrets/master.key
pct set $CT -mp0 /srv/googich-secrets,mp=/opt/googich/secrets
pct start $CT
```

If the app inside the container runs as a different uid, add 100000 to it for the host-side
owner. Older backups taken before the move still contain the key; if that matters, delete them,
or replace the stored credentials (create a new Immich API key and Google service account key,
change the SMB password) so the old copies become useless.

## Where credentials are kept

Credentials entered in the web interface (the Immich API key, Google service account keys, the SMB
password and notification URLs) are encrypted with AES-256-GCM before they are stored, and the
interface never shows them again. The master key is read from `GOOGICH_MASTER_KEY_FILE` (the
Docker secret in `compose.yaml`). If that is not set, `master.key` is created next to the database
on first start, readable only by its owner; that protects a copy of the database on its own, such
as a backup, but not someone who can read the whole data folder.

Logs are written as JSON lines in `data/logs/` and pass through a filter that removes keys,
tokens and passwords before anything is written.

## Command-line tools

The same engine runs from the command line, for scripting or a quick look before using the web
interface. From a checkout of this repository, with [uv](https://docs.astral.sh/uv/):

```bash
uv run googich scan /path/to/archives --timezone Australia/Melbourne          # dry run
uv run googich scan /path/to/archives --immich-url http://immich:2283 \
  --key-file /path/to/immich.key                                             # what is new
uv run googich import /path/to/archives --immich-url http://immich:2283 \
  --key-file /path/to/immich.key --timezone Australia/Melbourne              # asks first
uv run googich fetch --drive-folder FOLDER_ID --service-account key.json \
  --staging /path/to/downloads                                               # from Drive
```

Key files must be readable only by you (`chmod 600`); keys are never accepted on the command line,
so they stay out of your shell history.

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```

Tests use synthetic Takeout exports generated at test time, and in-memory stand-ins for Immich,
Google Drive and SMB. No real photos are stored in the repository.

## Roadmap

- **Unlock after restart (optional):** keep the master key protected by a passphrase, so it is
  never stored anywhere in usable form. After each restart the app would wait to be unlocked in
  the web interface before running again. This protects against a stolen disk or backup, at the
  cost of runs pausing after reboots and updates until someone unlocks it.
- Linking Live Photo pairs and stacking edited copies with their originals in Immich.
- Reading capture dates from HEIC and RAW files directly.
- Optional parallel uploads for very large first imports.

## Licence

[MIT](LICENSE)

Googich Takeaway is an independent project. It is not affiliated with, endorsed by, or sponsored
by Google or Immich. Google Drive, Google Photos and Google Takeout are trademarks of Google LLC.
