# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.3] - 2026-10-02

### Changed

- The container image runs Python 3.14 (was 3.13). The full test suite passes on both, and CI
  now tests every change on Python 3.13 and 3.14.

## [0.3.2] - 2026-10-02

### Changed

- The menu bar stays at the top of the window, and on wider screens only the page under it
  scrolls, so the scroll bar no longer runs alongside the menu bar.
- **Run now** is the only run button. **Options**, a button beside it, opens the tick boxes that
  apply when it is pressed (and, in demo mode, the demo run).
- Menus (Configuration, Help, Options) close when you click elsewhere on the page or press Escape.
- The run box says when the last run was and what it did. While a run is going it takes the full
  width, and the schedule beside it is hidden.
- Run results always say how many photos and videos were uploaded and skipped, even when 0, for
  example "Downloaded 2 archives, 13 photos and videos uploaded, 38 skipped".
- History shows the latest run; an arrow opens the earlier ones.
- The Logs page is redrawn: newest lines first, with new lines added at the top; levels shown as
  coloured labels; one toolbar with Filter, Pause or Resume, Download logs, and how many days to
  keep log files.
- The schedule sits beside the run controls on the dashboard, not under them.
- **Recent runs** on the dashboard is now **History**.
- The Google Photos figure counts every distinct photo and video found across all exports, so it
  grows with each export instead of showing only the latest one.
- The Google Drive figure shows what the Drive folder holds, as listed at the start of each run.
  Without a Drive source, the Google Drive icon is greyed out and leads to setting one up.
- The download folder figure grows while an archive downloads.
- Help → About and Help → Updates ask GitHub for the latest release as they open (at most once a
  minute, only while update checks are on), and never show a latest release older than the
  version running.

### Added

- **Downloading exports yourself**, as a full alternative to Google Drive with no Google Cloud
  setup: the dashboard and Sources page offer the choice, the download folder itself can be the
  source, and a new guide covers it. An export saved by hand is imported only once no part is
  missing between the others and none has changed for half an hour.
- **Search** in the guide: finds every section holding all the words, with the words marked.
- New guides: **What is Immich?** and **Setting up Immich**, from installing it with Docker to
  connecting it to this app, and **Backing up an Android phone** with the Immich app, alongside
  the Takeout imports.
- The menu bar shows a run going, with its stage and how far it is (for example
  "Downloading 42%"), or "Paused" with how far the paused run got. Nothing shows when no run is
  going or paused.
- **Update** during a run pauses the run first, then installs; the run resumes by itself once
  the new version starts, or straight away if the update fails.
- Log files older than 90 days are deleted; the period is set in Settings or on the Logs page,
  which also show how much space the logs take now. History is always kept.
- The update banner follows a one-click update to the end. It keeps checking while the app
  restarts, then says **Update complete** with what changed, or why the update failed, until
  dismissed. It no longer reloads the whole page every few seconds.

- The README's roadmap is replaced by a list of known limitations, also in the Troubleshooting
  guide.

### Fixed

- "Cancelling at the next safe point" and similar notices stayed on the dashboard after the run
  had stopped.
- **Download log file** failed in browsers ("couldn't finish download"): the log grew while it
  was sent, so more bytes arrived than announced. It now downloads every log file kept, as one
  zip.

## [0.3.1] - 2026-10-02

### Added

- **Pause** and **Cancel** while a run is going. Both stop at the next safe point, between or
  inside file transfers, so nothing done is lost. A paused run shows **Resume**, which carries on
  with the same options, or **Discard**. Neither sends a notification or counts as a failure.
- **Pause schedule** and **Resume schedule**, in a new **Schedule** box on the dashboard that
  shows what the schedule is set to and the next run. Run now still works while paused.
- Run now says whether the run started, or that one is already going.
- A paused run shows how far it got: size and number of files for each stage.
- Pause and Cancel first show what happens to the file being worked on: a download continues
  from where it stopped, reading the archives starts that export again, an upload is sent again.
- Deleting from the download folder first lists the files to be deleted and what stays.

### Changed

- The dashboard's Configure button is now **Configure dashboard**.
- **Download archives again** now fetches only archives whose copy is no longer in the download
  folder (for example after a cleanup). Copies still there are not fetched again, so resuming a
  paused run no longer downloads everything a second time.

### Fixed

- A resumed download's progress started from zero, so it looked like a fresh download. It now
  starts from the amount already downloaded; only the rest is fetched, as before.
- The background worker no longer stops for good if working out the next scheduled run fails;
  it logs the error and tries again shortly.

## [0.3.0] - 2026-10-02

### Added

- A **Help** menu with a built-in guide to every part of the app: how it works, first-time setup,
  Google Takeout, Google Drive, Immich and the download folder, schedule and notifications, dates
  and time zones, cleanup, updates, security and backups, and troubleshooting. The same pages
  are in the repository under `src/googich_takeaway/docs`. Updates and Logs moved into this
  menu, and an About page shows the version, an update check and the licence.
- A step-by-step guide to setting up a scheduled Google Takeout export to Google Drive.
- **Takeout reminders.** The dashboard warns, and a notification is sent once, when no new export
  has arrived for 10 days longer than expected, and three weeks before a Takeout schedule ends if
  you note its start date and frequency (monthly or every 2 months) on the Schedule page.
- **Follow my Takeout schedule**, a new schedule choice: the app runs on each expected export day
  and, if the export has not arrived, tries again every day (or every few days) until it does, for
  up to 14 days, and runs on no other days. A weekly run between exports is optional. The Schedule page shows a
  table of planned checks with each export's status, what the schedule is doing now, and the next
  runs.
- A **Google Photos** figure on the dashboard: how many photos and videos the latest Takeout
  export held, and how many of them are in Immich. Google offers no way to count a Google Photos
  library directly, so the latest export is the measure.
- `googich reset-password`, for a forgotten web password. Settings and credentials are kept.
- **Named notifications.** Each notification service is added with a name, listed by name and
  kind of service, and tested or removed on its own. A failed test shows the reason the service
  gave, such as a wrong token. Notification URLs saved before 0.3.0 are kept and named after their
  service. A URL starting with `http://` gets a hint to use `hassio://` or `json://`. A Home
  Assistant test says which notify service it went through, or that it is in the bell.
- **Check now** under Help → Updates asks GitHub for the latest release straight away (at most
  once a minute), and offers the update button there when a newer release is out.
- **Configure**, at the foot of the dashboard, chooses what it shows. Besides the journey strip
  and recent runs, it can show summaries of sources, destinations and how much space cleanup can
  free, with a button to the Cleanup page.
- On the dashboard, the Google Drive and download folder icons open their settings and have a
  Cleanup link under them; the Google Photos and Immich icons open those apps.
- The Sources and Destinations pages link to their guides, and to Google Drive and Immich. Each
  Drive source has an Open in Google Drive link.
- The Sources page shows each Google Drive folder's own name next to its full folder ID.

### Changed

- Immich and the download folder moved from Settings to a new **Destinations** page. The time
  zone for undated photos has its own section in Settings.
- Only one of the Configuration and Help menus is open at a time.
- A **Configuration** menu holds Sources, Destinations, Schedule, Notifications, Dashboard,
  Cleanup and Settings. Schedule and Notifications have their own pages; Settings keeps the time
  zone and the look and feel. The Immich link left the top bar, and Log out is a clearer button.
- The README is shorter and points to the guide.

## [0.2.1] - 2026-10-02

### Fixed

- Behind an HTTPS reverse proxy, every form was refused as "Cross-site request refused": the app
  saw plain http from the proxy while the browser said https. The same-site check now compares
  host and port only, which keeps its protection (another site cannot name this host) and works
  behind any proxy with no configuration.

### Added

- `GOOGICH_TRUSTED_PROXIES`, recommended behind a reverse proxy: the app then believes that
  proxy's forwarded scheme and client address, so the session cookie is marked secure and logins
  are throttled per visitor. Forwarded headers from any other address are ignored, and the app
  logs a reminder when it sees a proxy it has not been told about.
- The update helper no longer reports a real release as "not a published release" when GitHub
  is rate limiting anonymous requests (60 an hour per address) or cannot be reached. It retries,
  and if GitHub still cannot be asked it says so and changes nothing.

## [0.2.0] - 2026-10-02

A new look, one-click updates, and safer defaults for new installs.

### Added

- A new design: stages have colours (sky for Google Drive, violet for the download folder, green
  for Immich), progress travels through a spectrum between them, and red is kept for errors.
  The dashboard opens with a journey strip whose legs animate while files move.
- Progress bars in two styles, glossy striped bars with a travelling percentage or segmented
  capsules, drawn without inline styles so the strict security policy stays in place.
- Look and feel settings: theme (match the device, light or dark), colour scheme (spectrum,
  ocean or sunset), progress bar style, and animation on or off. Reduced-motion settings on the
  device are always respected.
- Bundled typefaces, Bricolage Grotesque and Atkinson Hyperlegible Next (SIL Open Font
  Licence), an original icon and favicon, and a two-column settings layout.
- Demo mode for previewing the progress views (`GOOGICH_DEMO=1`): a simulated run that records
  nothing.
- One-click updates, optional. With the update helper installed on the Docker host, the update
  banner offers Update now. The app only writes the version to install into a file; the helper,
  outside the container, checks it is a published release, switches the image version in
  `compose.yaml`, restarts the container, waits for the new version to answer and rolls back if
  anything fails. The app never gets access to Docker.

### Changed

- The example `compose.yaml` pins the release version instead of `latest`, and runs as the
  image's own unprivileged user (uid 10001). New installs `chown` the data and secrets folders
  to it. A test keeps the pinned version in step with each release.
- `/healthz` also answers `HEAD` requests, for monitors that use them.

### Documentation

- How to keep the master key out of Proxmox container backups, by moving it to the host and
  mounting it back in.
- First-run setup should happen straight after starting a new install.
- A roadmap, including an optional "unlock after restart" mode that never stores the key in usable
  form.

## [0.1.1] - 2026-10-02

Fixes for real-world use: SMB shares on Windows desktops, settings forms, and full-size
libraries of tens of thousands of photos.

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

[Unreleased]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.3...HEAD
[0.3.3]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/aistuartai/Googich_takeaway/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/aistuartai/Googich_takeaway/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aistuartai/Googich_takeaway/releases/tag/v0.1.0
