# Setting up Google Takeout

Google Takeout makes a complete copy of your Google Photos library. Set it up once to export to
Google Drive every month or every two months, and the app picks up each export by itself. Or
have Takeout email you a download link instead, and save the archives yourself.

Google offers no way for apps to start or schedule Takeout, so this part is done by you, in your
browser, signed in to the Google account that holds your photos.

## Create the scheduled export

1. Open [Google Takeout for Google Photos](https://takeout.google.com/settings/takeout/custom/photos).
   It opens with only Google Photos selected. If it shows every product instead, press
   **Deselect all**, then tick **Google Photos**.
2. **All photo albums included:** to make the export much smaller, include only the year
   folders (see [Making the export smaller](#making-the-export-smaller) below). Press
   **Next step**.
3. **Destination:** choose **Add to Drive**, so the app fetches each export by itself. To
   download the exports yourself instead, with no Google Cloud setup, choose **Send download link
   via email** (see [Downloading exports yourself](manual-downloads.md)).
4. **Frequency:** choose a scheduled export, every month or every 2 months. Either runs for one
   year: twelve or six exports. Every 2 months is usually enough, and uses less Google storage.
5. **File type:** choose **`.zip`**. `.tgz` works too, but if a run is paused or stopped while
   reading a `.zip`, it carries on almost at once; a `.tgz` has to be unpacked again from the
   start (without checking each file again).
6. **File size:** choose **2 GB** (anything up to 10 GB is fine). A large library comes as many
   parts, which the app handles the same as one. Small parts mean less to repeat when something
   is interrupted: a download that fails in a browser, or a `.tgz` that must be unpacked again
   after a pause. With `.zip` from Drive the size matters little, as downloads resume where they
   stopped either way.
7. Press **Create export**.

Google emails you when each export is ready. The first one can take hours or days for a big
library.

Then note today's date, and how often it exports, under **Configuration → Schedule**, in *Google
Takeout schedule*. The page then lists the days the exports are expected; the app reminds you three weeks before the schedule
ends, and can run on the same rhythm (see
[Schedule and notifications](schedule.md#following-the-takeout-schedule)).

## Making the export smaller

A Takeout export is often two or three times the size Google Photos says your library uses.
Two things make it so.

**Album copies.** Takeout writes every photo once in its year folder (*Photos from 2019*), and
again in every album it belongs to, as a separate copy. Googich Takeaway recognises the copies
and uploads each photo once, but they are still downloaded and read. To leave them out, at step 2
press **All photo albums included**, then **Deselect all**, and tick only the folders named
*Photos from …*. Every photo in your own library is in one of those.

- Albums are not recreated in Immich either way, so nothing is lost by leaving them out.
- One exception: photos in a **shared album** that someone else added, and that you never saved
  to your library, are only in that album's folder. Tick those shared albums too if you want
  them.

**Photos that do not count towards your Google storage.** Photos and videos backed up before
1 June 2021 in *High quality* (now *Storage saver*), and those backed up free from older Pixel
phones, are not counted in the storage Google shows you, but Takeout exports all of them. They
are real, distinct photos, and they are uploaded. They are the compressed copies Google kept,
not your originals: if the originals are still on a phone, the Immich phone app can back those
up too (see [Backing up an Android phone](android-backup.md)).

**What uploads, compared with what downloads.** After reading an export, the app uploads only
what Immich does not have yet, once each. Copies inside the export, files already in Immich,
photos you deleted from Immich before, and the export's metadata files are not uploaded, so
the upload is usually much smaller than the download. History shows the split for each run.

## Where the export goes

Takeout creates a folder called **Takeout** in your Google Drive, and puts each export's archives
in it. That is the folder to share with the app's service account (see
[Connecting Google Drive](google-drive.md)). Its folder ID is the last part of its address in
Drive, after `/folders/`.

## Things to know

> Every export counts against your Google storage until you delete it. A library of 200 GB needs
> 200 GB free in your Google account for each export. Delete exports from Drive once the app says
> they are safe to remove (see [Cleanup](cleanup.md)), and empty Drive's trash afterwards.

- **The schedule ends after a year.** Create a new scheduled export when it ends. The app warns you
  on the dashboard, and by notification, when no new export has arrived for 10 days longer than
  expected (40 days for monthly exports, 70 for every two months), and three weeks before the
  schedule you noted on the Schedule page ends.
- **Each export is a complete copy.** The app imports only what Immich does not already have, so
  later exports are quick.
- **Export links expire.** Takeout's download links last about a week, but archives added to
  Drive stay until you delete them.
- **Edits made in Google Photos** appear in the export as separate edited copies, next to the
  originals. Both are imported.

## Checking it worked

After Google's email arrives, open **Configuration → Sources** and press **Test** on the Drive
source: it lists the archives it can see. Then press **Run now** on the dashboard, or wait for the
schedule.
