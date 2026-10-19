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

**"Export … is still being written by Takeout; next run."**
Its newest archive in Google Drive is less than an hour old. The dashboard's run box shows it
as *Waiting*, with the time it is ready to import. The next run imports it, or press **Run
now** after that time. For
archives you save yourself, see
[Downloading exports yourself](manual-downloads.md#4-let-the-app-import-it).

**"Export … is not imported yet: …" or "a part failed to download"**
An export is held back until every part is here, so no photo gets a fallback date from a
missing part. The reason says which: a part *on Google Drive is not downloaded yet* (even when
Drive could not be reached this run), *a part has not finished downloading*, *part 002 is
missing*, or *a part was saved less than 30 minutes ago*. The next run carries on.

**"Cannot read the download folder right now" or "Cannot read …"**
The folder or share is not mounted, or its permissions changed. Check it is there and readable
by the app's user (uid 10001 in the container). Other sources still import.

**"files have no date and need review"**
These had no date anywhere (see [Dates and time zones](dates.md)). They were not uploaded and stay
in the archive and in Google Photos. Cleanup asks you to confirm before removing such an export.

**"files show a different date in Immich than the one sent"**
Immich read a different date from the file than the app sent. On the Cleanup page, **Review
dates** beside that export lists the files and what differs. Check a few in Immich, fix any that
are wrong there, then press **Keep Immich's dates**; until then Cleanup keeps that export's
archives.

**The schedule paused**
Several scheduled runs failed in a row (see
[Schedule and notifications](schedule.md#when-runs-fail)). Read the latest run's summary on the
dashboard, fix the problem, then press **Resume schedule**.

**Photos deleted in Immich come back**
They do not: the app remembers what it uploaded. Only **Re-import files missing from Immich**
(under **Options**, beside Run now) uploads them again.

**"files were left out because … deleted in Immich since"**
The app uploaded them before, and Immich no longer has them, so it takes them as deleted on
purpose. If you switched to a different Immich library, choose **Start afresh with this
library** under Destinations (see
[Switching to a different library](destinations.md#immich)), then run. Otherwise, **Re-import
files missing from Immich** uploads them again.

**"No albums: the API key needs …"** (or stacks, or tags)
The photos were imported; only that part was skipped. Add the permissions it names to the key in
Immich (*Account Settings → API Keys*), then run with **Re-import** to organise what is there.
Or turn that choice off under Settings → What to import.

**Albums, stacks or tags missing for photos imported before**
They are made as each export is imported. Run once with **Re-import** to bring them to exports
imported earlier (see [What gets imported](what-gets-imported.md)).

**A run "Completed with errors"**
A few files could not be uploaded. The run box on the dashboard lists them, with Retry and
Ignore: see [How it works](how-it-works.md#when-files-fail).

**"The import stopped early"**
Immich failed file after file, so the run stopped before trying the rest. Fix Immich, then
press **Retry these files**. Ignore is not offered: it would also give up on the files never
tried.

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
container), it did not answer in time, it answered with an error (*GitHub answered HTTP …*), its
answer could not be read, there is no release yet, or, rarely, its limit of 60 checks an hour
from each network address was used up. Try again later.

**Updates: "Update to … failed"**
The banner gives the update helper's reason, and the app keeps running the version it had:

- *Refused: … is older than …, which is running*: the helper does not go back to an older
  release; see [One-click updates](updates.md#one-click-updates) to allow it on purpose.
- *… Rolled back to …*: the new version could not be downloaded or did not start in time, or
  the update was stopped part way (*The update was stopped part way*). Try again later.
- *Could not ask GitHub to confirm …*: GitHub could not be reached from the Docker host.
- *The update helper on this host is too old to install release candidates*: upgrade it with
  the command under Help → Updates
  (see [Installing the update helper](updates.md#installing-the-update-helper)).

## Known limitations

- **iPhone Live Photos appear as two items**, a photo and a short video. Immich can stack them
  by hand.
- **HEIC and RAW photos** take their date from Takeout's sidecar file, which carries no time
  zone; the time zone under Settings fills the gap.
- **Shared albums** come across as your own albums, without the people they were shared with.
- **A very large first import takes hours**: files go three at a time by default (Settings →
  Speed, up to eight); later runs only send what is new.

## Reporting a problem

Open an issue on [GitHub](https://github.com/aistuartai/Googich_takeaway/issues) with what you did,
what happened, and the matching lines from the log. Logs never contain keys or passwords, but look
for anything else you would rather not share, such as file names.
