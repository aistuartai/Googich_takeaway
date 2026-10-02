# What is Immich?

[Immich](https://immich.app/) is a self-hosted photo and video library: your own Google Photos,
running on a computer you control. It is free and open source.

## What it does

- Keeps your photos and videos on your own server, organised on a timeline by the date each one
  was taken.
- Has apps for Android and iPhone that can back up the phone's camera roll, and a web interface
  for any browser.
- Recognises faces and things in photos (search for "beach" or "dog"), shows photos on a map,
  and supports albums, sharing, and several users with their own libraries.

## Why it pairs with Googich Takeaway

Google Photos keeps your library in Google's cloud. Google Takeout can export a copy of it, but
the export is a set of large archives, with each photo's date often stored in a separate file
beside it. Googich Takeaway fetches those exports, works out the right date for every photo, and
adds anything Immich does not have yet. The result is a complete, private copy of your Google
Photos library in Immich, kept up to date on a schedule.

Googich Takeaway only adds to Immich. It never deletes or changes anything there, and never
changes anything in your Google account.

## Things to know

- **Immich is not a backup on its own.** It holds your library; back up its upload folder and
  database as you would any important data. See the Immich documentation on backups.
- **Immich changes quickly.** Read its release notes before upgrading.
- **You need a server for it.** Any 64-bit Linux machine with Docker works: a home server, a NAS
  that runs Docker, or a small PC. See [Setting up Immich](immich-setup.md).

If you already have Immich running, go straight to [First-time setup](first-setup.md). To back
up new photos straight from a phone too, see
[Backing up an Android phone](android-backup.md).
