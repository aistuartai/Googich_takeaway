# Backing up an Android phone to Immich

Googich Takeaway brings your Google Photos library into Immich. To keep new photos flowing in
straight from your phone as well, use the Immich app's own backup. The two work side by side.

Menu names below are from the Immich documentation; they can change between app versions.

## 1. Install and sign in

1. Install **Immich** from Google Play, F-Droid or the project's GitHub releases.
2. Open it and enter the server address, for example `http://192.168.1.20:2283`, or your public
   address such as `https://photos.example.com` if you want backup to work away from home.
3. Sign in as the **same Immich user** that Googich Takeaway uploads to. Immich recognises a photo
   it already has only within the same user's library.

## 2. Choose what to back up

1. Tap the **cloud icon** at the top right to open the backup screen.
2. Choose the albums (folders on the phone) to back up. **Camera** is the one most people want;
   add others, such as screenshots or a messaging app's pictures, if you want those too.
3. Scroll down and tap **Enable Backup**.

The first backup uploads everything in those albums, so start it on Wi-Fi and leave the phone on
charge.

## 3. Keep it running in the background

The app backs up while open (foreground backup), and also in the background, when Android lets
it. The two are separate: Android decides when background work runs, and battery saving is the
usual reason it does not.

- In the app's backup settings, turn on background backup, and choose whether it needs Wi-Fi or
  charging.
- In Android, open **Settings → Apps → Immich → Battery** and choose **Unrestricted** (wording
  varies by phone). Samsung phones also have *Sleeping apps* lists under Battery: make sure Immich
  is not on them.
- If backups still stall, open the app now and then: foreground backup catches up.

## 4. Photos already imported from Google Photos

Immich checks each file's contents (a checksum) before storing it, so a photo that is
**identical** to one already imported from Takeout is not stored twice.

Some will not be identical, and then you get two copies:

- **Storage saver.** If Google Photos stored your photos in *Storage saver* (compressed) quality,
  the Takeout copy differs from the original on the phone. Both end up in Immich.
- **Edited photos.** Google Photos exports edits as separate copies; the phone may hold a
  different version again.

Immich's *Utilities* page can find likely duplicates for you to review and remove. If your
Google Photos library was kept in Original quality, there should be few.

## 5. Freeing space on the phone

The app offers **Free Up Space**, which removes photos from the phone once they are safely in
Immich. It deletes from the phone, so use it only when you are sure the server, and its backups,
are in good order. Photos still in Google Photos are not affected.

## Checking it works

Take a photo, open the app, and watch the backup screen: the count of backed-up items goes up. In
the Immich web interface the photo appears on the timeline within a minute or two.
