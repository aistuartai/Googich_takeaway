# How it works

Googich Takeaway copies your Google Photos library into [Immich](https://immich.app/) and keeps it
topped up. Google offers no way for other apps to read a whole Google Photos library, so it works
from Google Takeout exports: complete copies of your library that Google makes for you.

## The journey

The dashboard shows four places, from left to right:

1. **Google Photos.** Your library. Google Takeout exports it to Google Drive on a schedule you
   set up once (see [Setting up Google Takeout](takeout.md)). The figure counts every distinct
   photo and video found in any export so far, the closest measure of your library there is. It
   only grows: an export that holds part of the library adds what is new and lowers nothing. It
   shows **?** until the first export has been read.
2. **Google Drive.** Takeout writes each export there as one or more archive files (`.zip` or
   `.tgz`). The app reads the folder with a service account that can see only that folder, and
   never changes anything in your Google account. The figure is what the folder held when a run
   (or **Refresh**) last looked; the line under it says how many of those archives have a copy
   in the download folder now. Without a Drive source it is greyed out,
   and leads to setting one up.
3. **Download folder.** Archives are copied here, on this server or a network share, then read.
   While an archive downloads, its size so far is counted too. During and after an import it
   also shows how many photos and videos are not in Immich yet.
4. **Immich.** Photos and videos Immich does not have yet are uploaded with their correct date.
   The big number is everything in your Immich library, from Takeout or not, if the API key has
   the `asset.statistics` permission; otherwise it counts what this app has put there. The line
   under it says how much came from Takeout, and how much of the latest export is in Immich.
   The latest export is the one imported last, which is not always the newest: a large export
   takes Takeout longer to write.

The arrows animate while files are being downloaded or uploaded. **Refresh**, beside **Run now**
and **Options**, reads Google Drive, the download folder and Immich again straight away, instead of
waiting for the next run; it lists files only, and downloads nothing. **Configure dashboard**, at the
bottom of the dashboard (or Configuration → Dashboard), chooses what else it shows: the
schedule, summaries of sources, destinations and cleanup, and History.

## What happens in a run

A run starts on the schedule you choose, or when you press **Run now**.

1. **Fetch.** Each source is listed. Archives not downloaded before are copied to the download
   folder. Interrupted downloads resume, and each download is checked against Drive's checksum.
2. **Wait for the export to be complete.** An export whose newest archive in Drive is less than an
   hour old is left for the next run, because Takeout may still be writing it. Archives you save
   yourself wait until no part is missing and none changed for half an hour (see
   [Downloading exports yourself](manual-downloads.md#4-let-the-app-import-it)).
3. **Read.** The archives of each export are read without unpacking them to disk. Every photo and
   video is paired with Takeout's metadata file, and its capture date is worked out (see
   [Dates and time zones](dates.md)). What is found is saved as it goes, so a paused or
   interrupted run does not read the same files again.
4. **Compare.** Immich is asked which files it already has, by content, so renamed copies and
   files from earlier exports are not uploaded twice.
5. **Upload.** New files go to Immich with their date and location, three at a time (Settings →
   Uploads, one to eight; files over 32 MB always go one at a time). Copies of the same photo
   inside the export, such as album copies, go once.
6. **Check.** Each upload is read back to confirm Immich shows the date that was sent. Immich
   processes big imports in the background, so some checks finish on later runs.

At the end the run is recorded under **History** on the dashboard, and a notification is sent if
you set one up. The box at the top of the dashboard, with **Run now**, is the *run box*: it shows
the run going now, or the last one's result.

### How a run ends

| Result | Meaning |
|---|---|
| **Completed** | Everything new is in Immich. |
| **Nothing new** | No new archives, and nothing left to upload. |
| **Completed with errors** | A few files could not be uploaded (see [When files fail](#when-files-fail)). |
| **Failed** | More than 5% of the files it tried failed, or it could not do its job: Immich or the download folder could not be reached, or five files failed in a row. |
| **Paused** or **Cancelled** | You stopped it. No notification is sent. |

Only scheduled runs that **failed** count towards pausing the schedule (see
[Schedule and notifications](schedule.md#when-runs-fail)).

### Pausing and resuming

While a run is going, **Pause** and **Cancel** ask first, then stop it at the next safe point,
between or inside file transfers, so nothing done is lost. A paused run offers **Resume**, which
carries on with the same options, or **Discard**.

- Downloads continue from their partial files.
- Reading carries on where it stopped: files already read are not read again. In a `.tgz`
  archive the app unpacks it again up to that point, which the dashboard shows as *catching up*.
- Files already in Immich are not sent again.

After Cancel, or if the app restarts or is updated mid-run, the next run carries on in the same
way. An update pressed during a run pauses the run first and resumes it afterwards.

### When files fail

A run that cannot send a few files ends **Completed with errors**. The run box then lists them,
with the reason, and offers:

- **Retry these files:** a run that tries just those again; everything else is done, so it goes
  straight to them. Where Immich refused a file (for example "Unsupported file type"), the list
  says *Retrying won't help: Immich refused it.*
- **Ignore them:** after asking, counts the export as done, so Cleanup can offer its archives. It
  asks again before deleting, as these files are not in Immich. The files stay listed on the
  Cleanup page, and **Re-import files missing from Immich** can bring them in later.

Until you choose, the export counts as unfinished, and every run tries those files again.

## Words used in the app

- **Export:** one Takeout export, made of one or more archives that share an ID such as
  `20261001T010203Z` (the date and time Takeout started it).
- **Imported completely:** every file of the export is in Immich, or was deliberately left out
  (no date, a type Immich does not accept, or failed files you chose to ignore).
- **Verified:** Immich shows the date the app sent.
- **Safe to remove:** imported completely and verified, so the archives are no longer needed (see
  [Cleanup](cleanup.md)).

## What it remembers

The app keeps a small database in its `data` folder: which archives were downloaded, which files
were uploaded, and your settings. That is why archives are not downloaded twice, and why photos you
delete in Immich are not brought back by the next export. **Re-import files missing from Immich**
(under **Options**, beside Run now) brings them back on purpose.
