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

## Requirements (planned)

- Docker
- An Immich server
- Free disk space for one full Takeout export
- Your own Google Cloud project with a service account (setup steps will be documented here)

## Licence

[MIT](LICENSE)

Googich Takeaway is an independent project. It is not affiliated with, endorsed by, or sponsored
by Google or Immich.
