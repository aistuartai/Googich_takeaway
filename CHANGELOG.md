# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
