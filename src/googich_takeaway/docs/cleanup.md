# Cleanup

Once an export is safely in Immich, its archives are just copies. **Configuration → Cleanup**
shows which ones can go, in two sections: **Download folder** and **Google Drive**. The
dashboard's Cleanup summary (switch it on with **Configure dashboard**, at the bottom of the
dashboard) shows the total.

## When an export is safe to remove

All of these must be true:

- The export was **imported completely**: every file is in Immich, or was deliberately left out.
- Immich has **finished processing** the uploads, and their dates were **checked**. After a big
  import this can take a few runs.
- No file shows a **different date** in Immich than the one sent, or you chose to keep Immich's
  dates: **Review dates** beside such an export lists the files and what differs, and
  **Keep Immich's dates** lets the export go ahead (it changes nothing in Immich).
- No **failed files** are waiting: they were retried successfully, or you chose to ignore them
  (see [When files fail](how-it-works.md#when-files-fail)).

Files with **no date**, types Immich does not accept, and failed files you ignored are not in
Immich. They exist only in the archive and in Google Photos, so removing such an export asks you
to confirm first.

## The download folder

The app deletes these archives itself when you press **Delete**, but only for exports that are
safe to remove, and only after checking the files on disk are still the ones that were imported.
The app remembers them, so they are not downloaded again.

**Partial downloads** are downloads that were interrupted; the next run resumes them. Delete one
only if you do not want it to resume.

Deleting is paused while a run is going.

## Google Drive

The app **never deletes anything in Google Drive**: its access is read-only. It lists the archives
it downloaded, and links to each one once its export is safe to remove. To free the space:

1. Press **Check Immich now** on the export. It asks Immich whether every file is still there, in
   case something was deleted since the import.
2. Open each archive's link and delete it in Drive.
3. **Empty Drive's trash.** Deleted files keep using your Google storage until the trash is
   emptied, or for 30 days.

The next run notices the archives are gone, and Cleanup marks them as removed.

> [!IMPORTANT]
> Removing archives does not touch Google Photos. After removing them, your photos are in Google
> Photos and in Immich's storage. Make sure Immich's storage is backed up.
