# How it works

Googich Takeaway copies your Google Photos library into [Immich](https://immich.app/) and keeps it
topped up. Google offers no way for other apps to read a whole Google Photos library, so it works
from Google Takeout exports: complete copies of your library that Google makes for you.

## The journey

The dashboard shows four places, from left to right:

1. **Google Photos.** Your library. Google Takeout exports it to Google Drive on a schedule you
   set up once (see [Setting up Google Takeout](takeout.md)). The figure counts every distinct
   photo and video found in any export so far, the closest measure of your library there is. It
   only grows: an export that holds part of the library adds what is new and lowers nothing.
2. **Google Drive.** Takeout writes each export there as one or more archive files (`.zip` or
   `.tgz`). The app reads the folder with a service account that can see only that folder, and
   never changes anything in your Google account. The figure is what the folder held when a run
   last looked, so it updates as soon as a run starts.
3. **Download folder.** Archives are copied here, on this server or a network share, then read.
   While an archive downloads, its size so far is counted too.
4. **Immich.** Photos and videos Immich does not have yet are uploaded with their correct date.

The arrows animate while files are being downloaded or uploaded.

## What happens in a run

A run starts on the schedule you choose, or when you press **Run now**.

1. **Fetch.** Each source is listed. Archives not downloaded before are copied to the download
   folder. Interrupted downloads resume, and each download is checked against Drive's checksum.
2. **Wait for Takeout to finish.** An export whose newest archive is less than an hour old is left
   for the next run, because Takeout may still be writing it.
3. **Scan.** The archives of each export are read without unpacking them. Every photo and video is
   paired with Takeout's metadata file, and its capture date is worked out (see
   [Dates and time zones](dates.md)).
4. **Compare.** Immich is asked which files it already has, by content, so renamed copies and
   files from earlier exports are not uploaded twice.
5. **Upload.** New files go to Immich with their date and location.
6. **Check.** Each upload is read back to confirm Immich shows the date that was sent. Immich
   processes big imports in the background, so some checks finish on later runs.

At the end the run is recorded under **History** on the dashboard, and a notification is sent if you set one up.

While a run is going, **Pause** and **Cancel** stop it at the next safe point, between or inside
file transfers, so nothing done is lost. After Pause, **Resume** carries on where it stopped:
downloads continue from their partial files, and files already in Immich are not sent again.
After Cancel, the next run carries on in the same way.

## Words used in the app

- **Export:** one Takeout export, made of one or more archives that share an ID such as
  `20261001T010203Z` (the date and time Takeout started it).
- **Imported completely:** every file of the export is in Immich, or was deliberately left out
  (no date, or a type Immich does not accept).
- **Verified:** Immich shows the date the app sent.
- **Safe to remove:** imported completely and verified, so the archives are no longer needed (see
  [Cleanup](cleanup.md)).

## What it remembers

The app keeps a small database in its `data` folder: which archives were downloaded, which files
were uploaded, and your settings. That is why archives are not downloaded twice, and why photos you
delete in Immich are not brought back by the next export. A **Re-import** (Options under Run now) brings
them back on purpose.
