# Troubleshooting

**Help → Logs** shows what the app did, newest first. Each run under History on the dashboard has
a *log* link showing only that run, in full detail. **Download log files** saves every log file
kept, in one zip.

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
Its newest archive is less than an hour old. The next run imports it.

**"files have no date and need review"**
These had no date anywhere (see [Dates and time zones](dates.md)). They were not uploaded and stay
in the archive and in Google Photos. Cleanup asks you to confirm before removing such an export.

**"files show a different date in Immich than the one sent"**
Immich read a different date from the file than the app sent. The log names the files. Check them
in Immich before removing the export's archives.

**The schedule paused**
Several scheduled runs failed in a row. Read the latest run's summary on the dashboard, fix the
problem, then press **Resume the schedule**.

**Photos deleted in Immich come back**
They do not: the app remembers what it uploaded. Only a **Re-import** (Options under Run now) uploads
them again.

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
GitHub allows 60 checks an hour from each network address, shared by everything on your network.
Try again later.

## Reporting a problem

Open an issue on [GitHub](https://github.com/aistuartai/Googich_takeaway/issues) with what you did,
what happened, and the matching lines from the log. Logs never contain keys or passwords, but look
for anything else you would rather not share, such as file names.

## Some files failed

A run that uploads most of an export but cannot send a few files ends **Completed with errors**,
not failed: it does not count towards pausing the schedule, and the notification lists the
files. If more than 5% of the files it tried failed, or it could not reach Immich or the
download folder, the run has **failed**.

The run box on the dashboard then lists the files that failed, with the reason, and offers:

- **Retry these files:** a run that tries just those again. Everything else is done, so it goes
  straight to them. Where Immich refused a file (for example "Unsupported file type"), the list
  says retrying will not help.
- **Ignore them:** after asking, counts the export as done, so Cleanup can offer its archives
  (it asks again before deleting, as these files are not in Immich). The files stay listed in
  the export's details.

Until you choose, the export counts as unfinished, and each run tries those files again.

## Known limitations

- **Live Photos appear as two items**, a photo and a short video, and **edited copies sit beside
  their originals** rather than stacked with them. Immich can stack items by hand.
- **HEIC and RAW photos** take their date from Takeout's sidecar file, which carries no time
  zone; the time zone under Settings fills the gap.
- **Uploads go one at a time.** A very large first import can take many hours; later runs only
  send what is new.
