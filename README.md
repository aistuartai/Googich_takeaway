# Googich Takeaway

Self-hosted tool that moves your Google Photos library into [Immich](https://immich.app/), using
Google Takeout archives.

> **Status: early development.** Nothing here is ready to use yet. Watch the
> [releases](https://github.com/aistuartai/Googich_takeaway/releases) page for the first version.

## What it will do

- Pick up Google Takeout archives from a Google Drive folder or a local folder, on a schedule.
- Download them to a staging location you choose (local disk or SMB share), resuming if interrupted.
- Handle direct photo/video files as well as `.zip` and `.tgz` archives.
- Upload only what Immich does not already have, keeping the original capture dates and locations
  from Takeout's metadata files.
- Remember what it has downloaded and uploaded, so nothing is processed twice — with a switch to
  re-import when you need to.
- Show queue progress with sizes and time estimates, and logs, in a password-protected web interface.
- Tell you which archives are fully imported and safe to delete, locally and in Google Drive.

Google Drive access is read-only: the app never changes or deletes anything in your Google account.

## Trying the scanner

The first working piece is a dry run. It reads Takeout archives and reports what an import would
do, without uploading or changing anything.

```bash
uv run googich scan /path/to/takeout-archives --timezone Australia/Melbourne
```

To also see which files your Immich server already has, create an Immich API key with the
`asset.upload` permission, save it in a file only you can read (`chmod 600`), and add:

```bash
--immich-url http://your-immich:2283 --key-file /path/to/immich.key
```

Add `--list` for every file, or `--json` for machine-readable output. The key is read from the
file, so it never appears in your shell history or the process list.

## Fetching from Google Drive

Google Takeout can write its exports to a Google Drive folder on a schedule. `googich fetch`
downloads new archives from that folder. It uses a Google Cloud service account that can only
**read** the one folder you share with it, so it can never change or delete anything in your
Google account.

One-time setup:

1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable
   the **Google Drive API** for it.
2. Under **IAM & Admin → Service Accounts**, create a service account. It needs no roles.
3. On the service account's **Keys** tab, add a JSON key. Save the downloaded file somewhere only
   you can read, then run `chmod 600` on it. Treat it like a password.
4. In Google Drive, open the folder Takeout writes to, choose **Share**, and share it with the
   service account's email address (it ends in `iam.gserviceaccount.com`) as **Viewer**.
5. Copy the folder ID: the last part of the folder's URL in your browser.

Then:

```bash
uv run googich fetch --drive-folder FOLDER_ID --service-account /path/to/key.json \
  --staging /path/to/downloads
```

Downloads resume if interrupted, are checked against the checksum Drive reports, and are only
renamed into place once complete. Archives downloaded before are skipped, even after you delete
the local copy; use `--ignore-history` to download everything again, or `--list-only` to see what
would be downloaded. A download that would not fit in the free space is refused before it starts.

## Importing

```bash
uv run googich import /path/to/takeout-archives --timezone Australia/Melbourne \
  --immich-url http://your-immich:2283 --key-file /path/to/immich.key
```

The import shows what it will do and asks before uploading. It needs an API key with the
`asset.upload` and `asset.read` permissions. Each file is uploaded unmodified, with a small XMP
sidecar that carries its capture date and location, and is then read back from Immich to check
the date.

It remembers what it uploaded (in `~/.local/share/googich/state.db` by default), so running it
again uploads only what is new. If you delete a photo in Immich, later imports will not bring it
back; use `--reimport` if you want them to. Files with no capture date at all are listed for you
to review rather than being given today's date.

Try it on a test Immich user first, and back up your Immich database before the first real import.

## Requirements (planned)

- Docker
- An Immich server
- Free disk space for one full Takeout export
- Your own Google Cloud project with a service account (setup steps will be documented here)

## Where credentials are kept

Credentials entered in the web interface (the Immich API key and Google service account keys)
are encrypted before they are stored, and the interface never shows them again. They are
encrypted with a master key kept outside the database:

- Set `GOOGICH_MASTER_KEY_FILE` to a file holding a base64-encoded 32-byte key, for example a
  Docker secret. Create one with `head -c 32 /dev/urandom | base64 > master.key`.
- If it is not set, `master.key` is created next to the database on first start, readable only
  by its owner.

The second option protects a copy of the database on its own, such as a backup, but not someone
who can read the whole data folder. Keep the master key out of the same backup as the database if
you can. If the master key is lost, enter the credentials again.

## Licence

[MIT](LICENSE)

Googich Takeaway is an independent project. It is not affiliated with, endorsed by, or sponsored
by Google or Immich.
