# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- SMB download folders no longer exhaust the server's connection limit. Every operation opened a
  new SMB connection and never closed it, which a Windows desktop (20 connections at most) soon
  refuses with STATUS_REQUEST_NOT_ACCEPTED. The app now keeps one shared connection per share,
  closes it when the settings change or the app stops, and disconnects straight after testing a
  share. Common SMB errors are explained in plain words.
- Settings forms keep what was typed when saving fails, so nothing has to be entered again
  except passwords and keys, which are never put back into the page.
- Large imports no longer leave exports half-finished. Immich reads metadata in the background,
  and after thousands of uploads that can take hours; files it had not processed within a few
  seconds were treated as unfinished, so every later run rescanned the whole export. An export
  now counts as imported once everything is uploaded, and dates are checked on later runs from
  the database, without reading the archives again. Cleanup still waits until every file is
  confirmed, and also waits when a date in Immich differs from the one sent.
- The dashboard stays fast during large imports. Progress is kept as running totals, so a
  refresh costs the same for 50 files or 50,000, and the queue shows file counts, the active file,
  failures and the next files waiting instead of every file.
- Time left for thousands of small files now accounts for the per-file overhead, using the slower
  of the files-per-second and bytes-per-second estimates.

## [0.1.0] - 2026-10-02

First release. Googich Takeaway fetches Google Takeout archives from Google Drive or a local
folder on a schedule, keeps them on local disk or an SMB share, and uploads only what Immich does
not already have, with correct capture dates and locations, verified after upload. It runs as a
Docker container with a password-protected web interface: settings, live progress, logs, run
history, cleanup guidance and Apprise notifications. Credentials are encrypted at rest and Google
Drive access is read-only. See the README for the quick start.

Images: `ghcr.io/aistuartai/googich_takeaway:0.1.0` (also `0.1` and `latest`), for amd64 and arm64.

### Added

- Project skeleton: licence, README, changelog, security policy, packaging metadata.
- Development tooling: ruff, mypy (strict), pytest, hypothesis, locked with uv.
- Continuous integration: lint, format, type check, tests, and a secret scan of the full history.
- Dependabot updates for GitHub Actions and Python dependencies.
- Capture date resolver: combines Takeout sidecar, EXIF, video and filename dates into one local
  time with an explicit UTC offset, handling time zones and dates edited in Google Photos.
- Filename date parsing for common camera, Pixel, WhatsApp and screenshot names.
- Test fixture builder that generates small synthetic Takeout exports (zip and tgz, multi-part)
  covering known naming quirks. No real photos are stored in the repository.
- Sidecar matcher: pairs each photo and video with its Takeout JSON sidecar across all parts of
  an export, handling truncated names, duplicate suffixes, edited copies in several languages,
  Live Photo videos and matching by title.
- Export scanner: streams every part of a zip or tgz export in one pass without extracting,
  hashing each media file (SHA-1), reading EXIF and video dates, pairing sidecars and resolving
  capture dates. Identical copies (for example in album folders) are marked so they upload once.
  Corrupt or truncated archives are reported as archive errors.
- Immich client with a read-only duplicate check, batched, with clear errors that never include
  the API key. Key files readable by other users are refused.
- `googich scan` command: a dry run that reports files, capture dates, sidecar pairing and, with an
  Immich server, which files are new. Text or JSON output. Never uploads or changes anything.
- Local state database (SQLite, versioned schema, owner-only file) recording every upload, so
  photos deleted in Immich are not uploaded again.
- XMP sidecar builder carrying the capture date with an explicit offset, GPS and description.
- `googich import` command: shows the plan, asks for confirmation, streams new files to Immich
  with an XMP sidecar, checks each file's SHA-1 while sending, then reads every upload back to
  verify its date. Files deleted in Immich since an earlier upload, files in Immich's trash and
  files with no capture date are skipped and reported. Interrupted runs resume without
  duplicates.
- Google Drive source using a service account with read-only access to one shared folder.
- `googich fetch` command: downloads new archives into a staging folder with resume, retries,
  checksum verification and atomic completion, refuses downloads that would not fit, and
  remembers what was downloaded (with `--ignore-history` to override).
- Local folder source.
- Pixel motion photos: the separate `.MP` video copy is skipped when the `.MP.jpg` still already
  embeds it; a lone `.MP` file is treated as a video.
- Drive folder is checked first, so a missing share is reported directly with the account to
  share with. Downloaded archives and the staging folder are created owner-only.
- State database schema 2: download history. Version 1 databases are upgraded in place.
- Web interface foundation (`googich serve`): first-run setup protected by a one-time token from
  the server log, Argon2id password, server-side sessions stored as hashes, CSRF tokens and
  same-origin checks on every change, login throttling, strict Content Security Policy and
  security headers, and an unauthenticated `/healthz`. htmx 2.0.11 is bundled, not loaded from
  a CDN. State database schema 3 adds the login tables.
- Settings in the web interface: Immich address, public address and API key, download folder,
  default time zone, and any number of Google Drive and local folder sources, each with a test
  button. Credentials are encrypted with AES-256-GCM under a master key kept outside the
  database, and are never shown again. State database schema 4.
- Run cycle: fetch from every Drive source, then import every complete export from the download
  folder and local sources, with a report for the notification. Exports are only imported when
  all parts downloaded and Takeout has stopped adding parts for an hour; cleanly imported exports
  are not rescanned. Retries stay bounded.
- Notifications through Apprise (ntfy, email, Home Assistant, Discord and many more) for success,
  no new data, failure and pause, each switchable. URLs are stored encrypted and never logged.
- State database schema 5: run history and completed exports.
- Background worker and schedule: off, every few hours, daily or weekly at a local time (daylight
  saving aware). One run at a time; runs interrupted by a restart are closed and resume next time.
  After a set number of failed scheduled runs in a row (default 3) the schedule pauses itself,
  notifies, and waits for Resume.
- Live progress on the dashboard: download, archive reading and upload queues with sizes,
  per-file progress bars, smoothed transfer rates and time left per stage and for the whole run.
  Before a stage starts its time is estimated from rates measured on earlier runs.
- Dashboard with status, next run, Run now, recent runs with details, and the pause banner.
- Update check: once a day the app asks GitHub for the latest release and shows a banner when a
  newer one exists. Notify only; nothing is downloaded or installed. Can be switched off.
- Download folder on an SMB share (NAS or file server), built in: the app connects with
  smbprotocol itself, so no mounts or container privileges are needed. Downloads resume after a
  lost connection, archives are read and uploaded from the share, and cleanup works there too.
  The share is tested before settings are saved; the password is stored encrypted.
- Cleanup page. Download folder: delete archives of exports imported completely, re-checked
  against the files on disk at the time of deletion; partial downloads listed with their age.
  Google Drive: the app never deletes there; it lists downloaded exports ready to remove, with
  links, a Check Immich now button and instructions, and shows archives already removed. Files
  that were never imported (no date, or rejected by Immich) are called out and need confirmation.
- Logs: JSON lines in rotating owner-only files beside the database, and a web viewer with
  level and text filters, a live tail, per-run views linked from the dashboard and a download.
  A redaction filter removes authorization headers, API keys, private keys and credentials in
  URLs from every line before it is written. htmx is configured never to evaluate code.
  Settings for the schedule and notifications, with a test notification button.

[Unreleased]: https://github.com/aistuartai/Googich_takeaway/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/aistuartai/Googich_takeaway/releases/tag/v0.1.0
