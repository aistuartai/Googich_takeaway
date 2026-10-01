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
