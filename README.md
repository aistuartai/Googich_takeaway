# Googich Takeaway

Self-hosted tool that moves your Google Photos library into [Immich](https://immich.app/), using
Google Takeout archives, and keeps it topped up on a schedule.

> **Status: first release (0.1).** It works end to end and has been tested against real Google
> Drive, Immich and SMB, but it is young. Try it on a test Immich user first, and back up your
> Immich database before the first real import.

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
curl -fsSLO https://raw.githubusercontent.com/aistuartai/Googich_takeaway/main/docker/compose.yaml

# A data folder, and a master key that encrypts the credentials you enter later.
mkdir -p data secrets && chmod 700 data secrets
head -c 32 /dev/urandom | base64 > secrets/master.key && chmod 600 secrets/master.key

PUID=$(id -u) PGID=$(id -g) docker compose up -d
docker compose logs googich | grep "First-run setup"
```

Open `http://<this host>:8080/setup`, enter the setup token from the log, and choose a password.
The token stops anyone else on your network from claiming a fresh install.

Edit `compose.yaml` first if you want a different port, time zone, or a large local disk for
downloads. The web interface is meant for your local network; put it behind HTTPS (a reverse
proxy) if you reach it from anywhere else.

## First-time setup in the web interface

The dashboard shows a checklist until these are done.

1. **Settings → Immich.** Enter the Immich address the app can reach, for example
   `http://immich:2283`, and an API key. Create the key in Immich under *Account Settings → API
   Keys* with the `asset.upload` and `asset.read` permissions (`stack.create` is optional). Press
   **Test connection**.
2. **Settings → Downloads.** Choose a local folder (for example `/data/staging`) or an SMB share,
   and your time zone. The folder is tested before it is saved.
3. **Sources.** Add your Google Drive Takeout folder (see below), or a local folder of archives
   you downloaded yourself. Press **Test**.
4. **Settings → Schedule and Notifications.** Choose how often to run, and where to send
   notifications. Or press **Run now** on the dashboard.

### Connecting Google Drive

The app reads your Takeout folder with a Google Cloud *service account* that can see only the one
folder you share with it.

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable
   the **Google Drive API** for it.
2. Under **IAM & Admin → Service Accounts**, create a service account. It needs no roles.
3. On its **Keys** tab, add a JSON key. Upload this file in the web interface (Sources → Add a
   Google Drive folder), then delete your downloaded copy. Treat it like a password.
4. In Google Drive, open the folder Takeout writes to, choose **Share**, and add the service
   account's address (shown on the Sources page; it ends in `iam.gserviceaccount.com`) as
   **Viewer**. Keep *General access* set to *Restricted*.
5. The folder ID is the last part of the folder's address, after `/folders/`.

If Google says key creation is disabled, your account belongs to a Google Cloud organisation that
blocks service account keys. Use a project under a personal account, or ask the organisation's
administrator.

### Scheduling Google Takeout

In [Google Takeout](https://takeout.google.com/), select only **Google Photos**, choose **Add to
Drive** as the delivery method and a **scheduled export**, and pick `.tgz` or `.zip`. `.tgz` suits
SMB shares better. Each scheduled export is a complete copy of your library; the app imports only
what is new.

## Updating

The app checks GitHub once a day and shows a banner when a new release is out (switch this off in
Settings → Updates). It never updates itself. To update:

```bash
docker compose pull && docker compose up -d
```

## Backups

Back up the `data` folder: it holds the state database (what has been downloaded and uploaded,
your settings and encrypted credentials) and logs. Keep `secrets/master.key` somewhere separate;
without it the stored credentials cannot be read, and you would need to enter them again.

## Where credentials are kept

Credentials entered in the web interface (the Immich API key, Google service account keys, the SMB
password and notification URLs) are encrypted with AES-256-GCM before they are stored, and the
interface never shows them again. The master key is read from `GOOGICH_MASTER_KEY_FILE` (the
Docker secret in `compose.yaml`). If that is not set, `master.key` is created next to the database
on first start, readable only by its owner; that protects a copy of the database on its own, such
as a backup, but not someone who can read the whole data folder.

Logs are written as JSON lines in `data/logs/` and pass through a filter that removes keys,
tokens and passwords before anything is written.

## Download folder on a NAS

Settings → Downloads can keep downloaded archives in a folder on an SMB share (a NAS or Windows
file server). The app connects to the share itself, so nothing has to be mounted and the container
needs no extra privileges. Use an account that can reach only that folder.

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

## Licence

[MIT](LICENSE)

Googich Takeaway is an independent project. It is not affiliated with, endorsed by, or sponsored
by Google or Immich. Google Drive, Google Photos and Google Takeout are trademarks of Google LLC.
