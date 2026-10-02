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
2. Leave **All photo albums included** as it is, unless you want only some albums. Press
   **Next step**.
3. **Destination:** choose **Add to Drive**, so the app fetches each export by itself. To
   download the exports yourself instead, with no Google Cloud setup, choose **Send download link
   via email** (see [Downloading exports yourself](manual-downloads.md)).
4. **Frequency:** choose a scheduled export, every month or every 2 months. Either runs for one
   year: twelve or six exports. Every 2 months is usually enough, and uses less Google storage.
5. **File type:** `.tgz` or `.zip`. Either works; `.tgz` suits SMB shares slightly better.
6. **File size:** choose **50 GB**. Bigger parts mean fewer files; a library larger than that is
   split into several parts, which is fine.
7. Press **Create export**.

Google emails you when each export is ready. The first one can take hours or days for a big
library.

Then note today's date, and how often it exports, under **Configuration → Schedule**, in *Google
Takeout schedule*. The page then lists the days the exports are expected; the app reminds you three weeks before the schedule
ends, and can run on the same rhythm (see
[Schedule and notifications](schedule.md#following-the-takeout-schedule)).

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
