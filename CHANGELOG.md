# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- The dashboard's Immich figure counts everything in your Immich library, from Takeout or not,
  so it is right after switching to another Immich. It needs the optional `asset.statistics`
  permission on the API key; without it the figure counts what this app uploaded, as before.
- **Refresh figures** under the dashboard's journey reads Google Drive, the download folder and
  Immich again now, rather than waiting for a run. It only lists; nothing is downloaded.

### Fixed

- The dashboard's "latest export" figures did not update after importing an export Takeout
  made before the one shown, which happens when a large export finishes after a small one.
  They now show the export imported last.

## [0.4.0] - 2026-10-03

### Added

- **Review dates** on the Cleanup page: an export with files Immich dates differently from what
  was sent could never be cleaned up. It now lists those files and what differs, and
  **Keep Immich's dates** lets the export go ahead (nothing in Immich changes).
- A test keeps every link between guide pages, and from the README into the guide, working.
- `scripts/release.sh X.Y.Z` for maintainers: bumps the version everywhere, dates the
  changelog, runs every check on Python 3.13 and 3.14, then commits and tags.

### Changed

- The guide is reviewed against the app and easier to read: topics grouped (Getting started,
  Running it, Reference), numbered steps, tables and GitHub-style notes and warnings shown as
  boxes in the app. New: how a run ends (completed, with errors, failed), pausing and resuming,
  what the dashboard figures mean, Settings → Uploads. Corrected: the upload speed, update check
  and helper details, button names, and Cleanup's conditions.
- The update command in the README and the Updates guide asks GitHub's releases page, not its
  rate-limited API.
- After a run that failed because too many files failed, the run box says "Run failed", not
  "Completed with errors".
- The Takeout guides recommend `.zip` exports in 2 GB parts (up to 10 GB) instead of 50 GB:
  less to repeat when a browser download fails or a `.tgz` is resumed; with `.zip` from Drive
  the size matters little.

## [0.3.8] - 2026-10-03

### Added

- **Completed with errors.** A run where a few files could not be uploaded no longer counts as
  failed: it completes with errors (amber in History), does not count towards pausing the
  schedule, and has its own notification, on by default. It fails only if more than 5% of the
  files it tried failed, or it could not do its job (Immich or the folder unreachable, five
  failures in a row).
- **Retry or ignore failed files** from the run box: it lists them with Immich's reason, marks
  the ones Immich refused (retrying will not help), and offers **Retry these files** or
  **Ignore them**, which counts the export as done so Cleanup can offer it.

### Changed

- While uploading, the dashboard says when it is catching up: a resumed run passes over the
  files already done to reach the next new one (a `.tgz` must be read from its start for that),
  and the bar used to sit at 0% meanwhile. Before uploading, it says it is checking which files
  Immich already has.
- Files on an SMB share are read with 1 MB buffers, so small reads (zip directories, metadata
  files) cost far fewer round trips.

### Fixed

- Pixel motion videos with a numbered name, such as `PXL_…(1).MP`, were not recognised as the
  copy of the video already inside `PXL_….MP(1).jpg`, so the app tried to upload them and
  Immich refused them (400). They are now skipped like the others; a `.MP` video whose photo is
  not in the export is uploaded as `.mp4`, which Immich accepts.
- Errors from Immich include Immich's own reason, such as "Unsupported file type", instead of
  only the HTTP status.
- The SMB library logged every block read from a share, dozens of lines a second, which cost
  CPU, slowed reading and filled the logs. Only its warnings and errors are kept.

## [0.3.7] - 2026-10-02

### Added

- Help → Updates and Help → About show which version of the update helper is installed, and
  whether this release brings a newer one.

### Changed

- The Takeout guide explains why an export is often far bigger than the storage Google shows
  (album copies, and photos that do not count towards Google storage) and how to leave the
  album copies out, and now recommends `.zip`, which resumes best.
- The dashboard keeps up during a run: the Google Photos figure updates as soon as an export
  has been read, Immich's "of the latest export" line counts up while uploading, and the
  download folder shows how many photos and videos are not in Immich yet.
- A failed update check says what went wrong (refused by GitHub's limit, no answer, could not
  connect, an HTTP error) instead of always pointing at the rate limit.

## [0.3.6] - 2026-10-02

### Fixed

- **One-click updates no longer use GitHub's API either.** The update helper (version 3)
  confirms a release from GitHub's releases pages: the requested version must be no newer than
  the newest published release, and must have the release's helper files attached. It can
  therefore only install 0.3.5 or later. Help → Updates offers the helper upgrade.
- The update check could fail with "rate limited" when other things on the same network had
  used up GitHub's 60 anonymous API requests an hour. It now reads the releases page instead,
  which that limit does not cover, and asks the API only if the page gives no answer.

## [0.3.5] - 2026-10-02

### Added

- **Pause and Resume no longer read the archives again.** What reading an export finds (each
  file's checksum and dates, each metadata file) is saved as it goes, so a run paused, cancelled
  or stopped while reading or uploading carries on where it was: finished parts are not read at
  all, and in a part read halfway, files already done are skipped (`.zip`) or only unpacked,
  not checked again (`.tgz`). Saved scans are deleted once the export is fully in Immich, or
  after 60 days. State database schema 9.
- **Several uploads at once.** Photos and other files up to 32 MB go to Immich three at a time
  by default (1 to 8, under Settings → Uploads); larger files still stream one at a time.
  With Immich taking 100 ms per upload, a test export went 2.6 times faster at three and 4.3
  times faster at six.
- **Checked update helper files.** Each release publishes the helper's files with SHA-256 sums
  and a build attestation, and the installer installs nothing unless every file matches.
  Help → Updates shows the install command with a check of the installer itself against the
  copy inside the app.

### Changed

- A saved Immich API key is only sent to the address it was entered for, and a saved SMB
  password only to its server and user: changing those asks for the secret again. Someone who
  got hold of a session can no longer point the app at their own machine to collect them.
- After a large import only the first 200 uploads are checked against Immich straight away;
  the rest are checked on later runs, once Immich has processed them, instead of asking about
  every file while it is still busy.
- `.tgz` archives are read in 1 MB blocks instead of 10 KB, far fewer round trips to an SMB
  share.
- Matching photos with very long names to their metadata files no longer slows down sharply
  in big folders (20,000 such files: 22 s down to 1.5 s).
- The example `compose.yaml` sets memory and process ceilings (`mem_limit: 2g`,
  `pids_limit: 256`).
- The update helper and its installer change `data/updater` only from inside it, after checking
  it really is that folder and not a link the container swapped in, and the helper requires
  mode exactly `1770`. The installer reads the version from `compose.yaml` strictly (a comment
  after it could change the version read).
- The update commands in the README and the Updates guide find the newest release themselves,
  instead of naming versions that went out of date with each release.
- Code tidy-up, with no change in behaviour: the web application is split into one module per
  area (sign-in, dashboard, logs, guide, cleanup, settings, updates, destinations, sources)
  instead of one 1,700-line file; unused code and CSS rules defined twice are gone; settings
  stored as JSON share one helper.
- The dashboard figures live in their own module (`summaries.py`), apart from the web routes.
- Each page reads every setting and the list of sources once: the dashboard makes 48 database
  queries instead of 88, and the live status 37 instead of 53.
- Four functions only the tests used are gone.
- Loading a page never changes saved state any more: the worker settles a finished update
  request in the background.
- The live log asks only for the lines it has not seen, and each log line is cleaned of secrets
  once instead of once per log handler.

### Fixed

- A file the upload step could not find in its archive was silently skipped while its export
  was marked complete; it is now reported as failed and tried again.
- After more than 50 manual runs the schedule could lose track of its last scheduled run.
- The live log stopped showing new lines after the app restarted, until the page was reloaded.
- A failed batch save could leave the database mid-save, so later batched saves did nothing.
- Uploads deleted in Immich before their date was checked were asked about on every run, and
  kept "still being processed" showing; they are now marked as gone.
- A crafted `.tgz` with a huge name header could exhaust memory; such headers are refused.
- "Resuming the paused run" and "Run started" stayed on the dashboard for the whole run; they
  now go as soon as the progress shows.

## [0.3.4] - 2026-10-02

### Security

- **Update helper:** it runs as root, but kept its lock and scratch files in a folder the
  container can write to, so a compromised app could have made root overwrite host files through
  planted links. It now keeps them in a root-only runtime folder, refuses to work unless
  `data/updater` belongs to root with the sticky bit, never reads the request through a link,
  and runs with `NoNewPrivileges` and a private `/tmp`.

### Added

- **One command installs or upgrades the update helper** (`install-updater.sh`): it reads the
  version you run from `compose.yaml` and sets everything up. Help → Updates shows the command,
  and says when the installed helper is older than the one in the release, as does the
  *Update complete* banner. A helper installed before 0.3.4 keeps working until upgraded.
- **Login throttling** counts each attempt the moment it starts, so simultaneous guesses can no
  longer slip past it, and at most two passwords are checked at once (each check uses 64 MB).
  Old entries are forgotten.
- Requests before sign-in are limited to 16 KB and must state their length; others to 1 MB.
- Only the exact login, setup and health paths skip sign-in.
- A crafted video can no longer crash a scan, and one damaged export no longer stops the others
  from importing.
- The banner's Dismiss never redirects to another site.

### Changed

- Faster: the database no longer waits for the disk on every save (the usual safe setting for
  its journal mode), sessions are checked without writing on every page refresh, and bulk
  records are saved in one go. Re-scanning a large export is minutes quicker.
- Uploads open only the files being sent and stop reading an archive once they are all sent,
  instead of reading every archive to the end (a `.tgz` was unpacked whole for one new photo).
- Downloads are checked against Drive's checksum while they arrive, instead of being read back
  afterwards (half the traffic on an SMB share).
- Matching photos to their metadata files is linear: 4,000 photos in one folder take 0.2 s, not
  5 s.
- Pages stay responsive while a Drive source is added or its key replaced.
- A second press of Run now while a run is starting is refused instead of queuing another run.
- State database schema 8 (an index for Cleanup and the dashboard).
- README rewritten to be short: highlights, quick start and a guide table. Installing the update
  helper, reverse proxy settings and keeping the master key out of Proxmox backups moved into the
  guide (Updates, and Security and backups), with a new Command-line tools guide.

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

[Unreleased]: https://github.com/aistuartai/Googich_takeaway/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.8...v0.4.0
[0.3.8]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.7...v0.3.8
[0.3.7]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.6...v0.3.7
[0.3.6]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.5...v0.3.6
[0.3.5]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.4...v0.3.5
[0.3.4]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.3...v0.3.4
[0.3.3]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.2...v0.3.3
[0.3.2]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/aistuartai/Googich_takeaway/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/aistuartai/Googich_takeaway/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/aistuartai/Googich_takeaway/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/aistuartai/Googich_takeaway/releases/tag/v0.1.0
