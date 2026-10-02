# Setting up Immich

A short path to a working Immich, ready for Googich Takeaway. The
[Immich documentation](https://docs.immich.app/) is the full reference, and always wins where
this page and it differ.

## What you need

- A 64-bit Linux machine (Ubuntu, Debian and similar) with **Docker Engine** and the
  **Docker Compose plugin** (`docker compose`, not the old `docker-compose`). Install Docker from
  Docker's own repository, not your distribution's packages.
- **Memory:** 6 GB at least, 8 GB recommended. **Processor:** 2 cores at least, 4 recommended.
- **Disk:** room for your whole library, plus 10 to 20% for thumbnails and converted videos. Your
  Takeout export's total size is a good guide to how much the library needs.
- The database (usually 1 to 3 GB) must be on a **local disk, ideally an SSD, not a network
  share**. The photos themselves may be on a larger disk.

## Install

On the server:

```bash
mkdir ~/immich-app && cd ~/immich-app
wget -O docker-compose.yml https://github.com/immich-app/immich/releases/latest/download/docker-compose.yml
wget -O .env https://github.com/immich-app/immich/releases/latest/download/example.env
```

Edit `.env`:

| Setting | What to put |
|---|---|
| `UPLOAD_LOCATION` | The folder for photos and videos, on a disk with enough room |
| `DB_DATA_LOCATION` | The database folder, on a local disk (SSD if you can) |
| `DB_PASSWORD` | A new password, letters and numbers only |
| `TZ` | Your time zone, for example `Australia/Melbourne` (remove the `#` in front) |

Then start it:

```bash
docker compose up -d
```

## First start

1. Open `http://<server address>:2283` in a browser.
2. Register. **The first user to register becomes the administrator.** Use this account, or a
   user you create under *Administration → Users*, for your Google Photos library.
3. Optional: install the Immich app on your phone (App Store, Google Play or F-Droid) and sign in
   with the same server address.

## Connect Googich Takeaway

1. In Immich, sign in as the user who should own the photos, open *Account Settings → API Keys*,
   and create a key with the `asset.upload` and `asset.read` permissions (and `stack.create` if
   offered). Copy it: Immich shows it only once.
2. In Googich Takeaway, open **Configuration → Destinations** and enter:
   - **Immich address:** how this app reaches Immich, for example `http://192.168.1.20:2283`.
     If both run in the same Docker network, the container name works too, for example
     `http://immich-server:2283`.
   - **API key:** the key from step 1.
3. Press **Save**, then **Test connection**. It shows the Immich version and any permission the
   key is missing.

More detail on these settings: [Immich and the download folder](destinations.md).

## Before you import a large library

- **Check the space.** Imported photos land in `UPLOAD_LOCATION`. Leave room for the whole
  library plus thumbnails.
- **Expect Immich to be busy.** After a big import Immich makes thumbnails, reads dates and runs
  face and object recognition for every photo. On a small server this can take hours or days.
  Progress shows under *Administration → Jobs*.
- **Back up Immich.** Once your library is there, back up the upload folder and the database.
  The Immich documentation explains how.
