# Troubleshooting

**Help → Logs** shows what the app did, newest first. Each run under History on the dashboard has
a *log* link showing only that run, in full detail. **Download logs** saves every log file kept,
in one zip.

Detailed log files are kept for 90 days, then deleted. Change this under **Configuration →
Settings → Logs**, or at the bottom of the Logs page (7 to 3650 days). History on the dashboard
is always kept.

## Google Drive

**"the Drive folder was not found, or is not shared with …"**
Share the Takeout folder with the service account's address (shown on the Sources page) as
Viewer, and check the folder ID is the part of the folder's address after `/folders/`.

**"the Google Drive API is not enabled in the service account's Google Cloud project"**
In the Google Cloud console, open APIs & Services → Library → Google Drive API, press Enable, wait
a few minutes, and try again.

**"Google refused the service account"**
The key was deleted or disabled in the Google Cloud console. Create a new key and upload it with
**Replace key**.

**Google says service account key creation is disabled**
See [Connecting Google Drive](google-drive.md#2-create-the-service-account-and-its-key).

**Test says "No Takeout archives in the folder yet"**
Takeout has not delivered an export yet, or it went to a different folder. Takeout's email says
when an export is ready.

## The download folder

**"needs … plus … margin, but only … is free"**
The download folder cannot hold the next archive. Free space with **Cleanup**, or choose a bigger
folder.

**SMB: "the server refused a new connection because it has reached its limit"**
(`STATUS_REQUEST_NOT_ACCEPTED`.) Windows desktop editions accept 20 connections at once. Close
other connections to that computer, or wait about 15 minutes for idle ones to drop.

**SMB: "the user name or password was not accepted"**
Check the user name and password, and the domain if the server needs one. On Windows, a Microsoft
account's user name is usually its email address.

**SMB: "the share name was not found on the server"**
The share name is the name it is shared as, not a folder path. Put any folders inside the share
in *Folder in the share*.

**SMB: "the account is not allowed to write to that folder"**
Give the account change (write) permission on the share and the folder.

## Runs

**An export is "waiting: Takeout is still writing it"**
Its newest archive in Google Drive is less than an hour old. The next run imports it. For
archives you save yourself, see
[Downloading exports yourself](manual-downloads.md#4-let-the-app-import-it).

**"files have no date and need review"**
These had no date anywhere (see [Dates and time zones](dates.md)). They were not uploaded and stay
in the archive and in Google Photos. Cleanup asks you to confirm before removing such an export.

**"files show a different date in Immich than the one sent"**
Immich read a different date from the file than the app sent. The log names the files. Check
them in Immich: until they are sorted out, Cleanup keeps that export's archives (it will not
offer to delete them).

**The schedule paused**
Several scheduled runs failed in a row (see
[Schedule and notifications](schedule.md#when-runs-fail)). Read the latest run's summary on the
dashboard, fix the problem, then press **Resume the schedule**.

**Photos deleted in Immich come back**
They do not: the app remembers what it uploaded. Only **Re-import files missing from Immich**
(under **Options**, beside Run now) uploads them again.

**A run "Completed with errors"**
A few files could not be uploaded. The run box on the dashboard lists them, with Retry and
Ignore: see [How it works](how-it-works.md#when-files-fail).

## The web interface

**"Cross-site request refused"**
The browser's address did not match the address the app was reached at. Behind a reverse proxy,
make sure the proxy passes the original `Host` header (Nginx Proxy Manager does by default).

**Forgot the password**
On the Docker host, in the folder holding `compose.yaml`, run:

```bash
docker compose exec googich googich reset-password
docker compose restart googich
docker compose logs googich | grep "First-run setup"
```

Then open `/setup`, enter the setup token from the log, and choose a new password. Settings and
credentials are kept.

**Updates: "Could not get the latest release from GitHub"**
The message ends with the reason: GitHub could not be reached (a network or DNS problem from the
container), it did not answer in time, or, rarely, its limit of 60 checks an hour from each
network address was used up. Try again later.

## Known limitations

- **Live Photos appear as two items**, a photo and a short video, and **edited copies sit beside
  their originals** rather than stacked with them. Immich can stack items by hand.
- **HEIC and RAW photos** take their date from Takeout's sidecar file, which carries no time
  zone; the time zone under Settings fills the gap.
- **Albums are not recreated** in Immich.
- **A very large first import takes hours**: files go three at a time by default (Settings →
  Uploads, up to eight); later runs only send what is new.

## Reporting a problem

Open an issue on [GitHub](https://github.com/aistuartai/Googich_takeaway/issues) with what you did,
what happened, and the matching lines from the log. Logs never contain keys or passwords, but look
for anything else you would rather not share, such as file names.
