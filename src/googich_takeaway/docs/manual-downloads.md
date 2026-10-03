# Downloading exports yourself

You can skip Google Drive and Google Cloud entirely. Takeout emails you a link to each export;
you download the archives in your browser and save them where this app reads them. The app does
the rest: it waits until every part is there, imports what Immich does not have, and tells you
when the archives are safe to delete.

## Which way suits you

| | From Google Drive | Downloading yourself |
|---|---|---|
| One-off setup | About 15 minutes in Google Cloud | None |
| Each export | Nothing to do | A few minutes of downloading |
| Google storage | Each export uses Drive space until deleted | None used |
| Runs unattended | Yes | Only after you have downloaded |

Downloading yourself suits a smaller library, or anyone who would rather not create a Google
Cloud project. You can switch later, or use both.

## 1. Set up Takeout to email you

Follow [Setting up Google Takeout](takeout.md) (including
[Making the export smaller](takeout.md#making-the-export-smaller), which matters more when you
download it yourself), but at **Destination** choose
**Send download link via email** instead of Add to Drive. Keep **File size** small, such as
**2 GB**: browsers cope better with small files, and if one part fails, there is less to repeat.
A large library then comes as many parts, all listed on the email's download page.

## 2. Choose where to save the archives

Under **Configuration → Sources**, choose **I download the exports myself**, then one of:

- **The download folder** (simplest). The app already reads it on every run. If it is an SMB
  share, such as a folder on a NAS, save the archives straight into it from your browser, for
  example `\\nas\photos\takeout`.
- **Another folder on this server.** Map the folder into the container in `compose.yaml`, for
  example `- /srv/takeout-downloads:/data/manual:ro`, then add `/data/manual` as a local folder.
  Archives there are read where they are, never copied or changed.

## 3. Download each export

When Google's email arrives (*Your Google data is ready to download*):

1. Open the link, signed in to the Google account that holds the photos. Google may ask for your
   password again.
2. Download **every** part: `takeout-…-001.zip`, `takeout-…-002.zip`, and so on.
3. Save them all into the folder from step 2. Do not rename them: the names tell the app which
   parts belong together.

The links stop working after about a week, so download within a few days.

## 4. Let the app import it

The next run, scheduled or **Run now**, imports the export once it looks complete:

- **No part is missing between the others.** If parts 001 and 003 are there but 002 is not, the
  run says *part 002 is missing* and waits.
- **No part changed in the last half hour**, so an export whose parts are still downloading or
  copying is not imported half-done. Browsers save unfinished downloads under another name
  (`.crdownload`, `.part`), which the app ignores.

The app cannot tell that the **last** part is missing, because Takeout's file names do not say
how many parts there are. Check the email's list of files, and download them all before the next
run. If a part turns up later, the export is read again with it, and only what Immich is still
missing is uploaded.

A good schedule for this way of working is
**Follow my Takeout schedule** (see [Schedule and notifications](schedule.md)), so the app
checks a day or two after each export is due.

## 5. Tidy up

Once an export is fully in Immich, **Cleanup** shows its archives as safe to delete, and frees
the space for you if they are in the download folder. Archives in a folder of your own are left
for you to delete.
