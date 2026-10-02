<h1 align="center">Googich Takeaway</h1>

<p align="center">
  <strong>Your Google Photos library, in your own <a href="https://immich.app/">Immich</a>, kept up to date.</strong><br>
  Self-hosted. Read-only on Google. Every photo with its right date.
</p>

<p align="center">
  <a href="https://github.com/aistuartai/Googich_takeaway/releases/latest"><img alt="Latest release" src="https://img.shields.io/github/v/release/aistuartai/Googich_takeaway"></a>
  <a href="https://github.com/aistuartai/Googich_takeaway/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/aistuartai/Googich_takeaway/actions/workflows/ci.yml/badge.svg"></a>
  <a href="LICENSE"><img alt="MIT licence" src="https://img.shields.io/badge/licence-MIT-blue"></a>
</p>

![The dashboard: Google Photos, Google Drive, the download folder and Immich, with the run box,
schedule, cleanup summary and history](assets/dashboard.png)

Google Takeout exports your library as a pile of archives, with each photo's date tucked into a
separate file beside it. Googich Takeaway picks up each export, works out every photo's real
date and time zone, and adds to Immich only what it does not have yet, on a schedule.

## Highlights

- **Two ways in.** Fetch scheduled exports from Google Drive with a read-only service account,
  or download them yourself from Takeout's email, with no Google Cloud setup at all.
- **Dates done right.** Pairs every photo and video with its Takeout metadata, across all parts
  of an export, and sends the date, time zone and location with each upload, then reads it back
  to check.
- **Never twice.** Archives are not downloaded again, photos already in Immich are skipped, and
  photos you delete in Immich stay deleted.
- **Resumable.** Downloads continue where they stopped. Pause, resume or cancel at any time, even
  across an update.
- **Clear at a glance.** Live progress with time estimates, history, logs, a built-in guide with
  search, and notifications through [Apprise](https://github.com/caronc/apprise).
- **Tidy.** Tells you which archives are fully in Immich and safe to delete, here and in Drive.
- **Careful with secrets.** Credentials encrypted at rest (AES-256-GCM), password login, strict
  browser security, and an unprivileged user on a read-only container.

## Quick start

You need Docker with Compose, an Immich server, and room for one Takeout export (on this machine
or an SMB share).

```bash
mkdir googich && cd googich
curl -fsSLO https://raw.githubusercontent.com/aistuartai/Googich_takeaway/v0.3.7/docker/compose.yaml
less compose.yaml                  # read what you are about to run

mkdir -p data secrets && chmod 700 data secrets
head -c 32 /dev/urandom | base64 > secrets/master.key && chmod 600 secrets/master.key
chown -R 10001:10001 data secrets  # the app's unprivileged user in the container

docker compose up -d
docker compose logs googich | grep "First-run setup"
```

Then **straight away** open `http://<this host>:8080/setup`, enter the token from the log and
choose a password: until then, anyone who can see the log could claim the install. The
dashboard walks you through the rest: connect Immich, pick the download folder, and choose how
exports arrive.

> [!IMPORTANT]
> Keep a copy of `secrets/master.key` somewhere safe, away from this machine: it unlocks the
> stored credentials. Back up the `data` folder, and try the first import on a test Immich user.

## Guide

The same guide is built into the app under **Help → Guide**, with search.

| Getting started | Running it | Reference |
|---|---|---|
| [How it works](src/googich_takeaway/docs/how-it-works.md) | [Schedule and notifications](src/googich_takeaway/docs/schedule.md) | [Dates and time zones](src/googich_takeaway/docs/dates.md) |
| [Setting up Immich](src/googich_takeaway/docs/immich-setup.md) | [Cleanup](src/googich_takeaway/docs/cleanup.md) | [Security and backups](src/googich_takeaway/docs/security.md) |
| [First-time setup](src/googich_takeaway/docs/first-setup.md) | [Updates and one-click updates](src/googich_takeaway/docs/updates.md) | [Command-line tools](src/googich_takeaway/docs/command-line.md) |
| [Google Takeout](src/googich_takeaway/docs/takeout.md) and [Google Drive](src/googich_takeaway/docs/google-drive.md) | [Backing up an Android phone](src/googich_takeaway/docs/android-backup.md) | [Troubleshooting](src/googich_takeaway/docs/troubleshooting.md) |
| [Downloading exports yourself](src/googich_takeaway/docs/manual-downloads.md) | [Reverse proxy and HTTPS](src/googich_takeaway/docs/security.md#reaching-it-from-outside-your-network) | [All topics](src/googich_takeaway/docs/README.md) |

## Updating

The app tells you when a new release is out, and never updates itself. With the optional
[update helper](src/googich_takeaway/docs/updates.md#installing-the-update-helper) (one command
to install on the Docker host), **Update now** installs it in one click, pausing a running run
first. Or by hand, in the folder holding `compose.yaml`:

```bash
V=$(curl -fsS https://api.github.com/repos/aistuartai/Googich_takeaway/releases/latest \
  | grep -oE '"tag_name": *"v[0-9.]+"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')   # newest release
[ -n "$V" ] && sed -i -E "s|(googich_takeaway:)[0-9]+\.[0-9]+\.[0-9]+|\1$V|" compose.yaml \
  && docker compose pull && docker compose up -d
```

## Limitations

- iPhone Live Photos import as a photo and a video, and Google's edited copies sit beside their
  originals rather than stacked with them.
- HEIC and RAW photos are dated from Takeout's metadata file, which has no time zone; the one set
  in Settings fills the gap.
- Uploads go one at a time, so a very large first import takes a while.

## Development

```bash
uv sync
uv run ruff check && uv run mypy && uv run pytest
```

Tests build synthetic Takeout exports and use in-memory stand-ins for Immich, Google Drive and
SMB. No real photos are stored in the repository.

## Licence

[MIT](LICENSE). Googich Takeaway is an independent project, not affiliated with, endorsed by or
sponsored by Google or Immich. Google Drive, Google Photos and Google Takeout are trademarks of
Google LLC.
