# Immich and the download folder

Both are set under **Configuration → Destinations**.

## Immich

**Immich address** is the address this app uses to reach Immich: from inside the container, so a
local network address such as `http://192.168.1.20:2283`, or `http://immich-server:2283` if both
run in the same Docker network.

**Public address** (optional) is the address you open Immich at in your browser, for example
`https://photos.example.com`. It is used only for links, such as the Immich icon on the dashboard.

**API key.** In Immich, open *Account Settings → API Keys* and create a key for the Immich user
whose library should receive the photos. Give it these permissions:

| Permission | Needed for |
|---|---|
| `asset.upload` | uploading photos and videos |
| `asset.read` | checking what Immich already has, and reading dates back |
| `stack.create` | optional: kept for stacking edited copies in a later release |

**Test connection** shows the Immich version and any missing permission. The key is encrypted
before it is stored, and never shown again; leave the field empty to keep the saved key.

## Download folder

Archives wait here between download and import. Choose:

- **A folder on this server**, for example `/data/staging` (inside the container). Map a large
  disk to it in `compose.yaml` if the container's data folder is small.
- **A folder on an SMB share**, on a NAS or Windows file server. The app connects to the share
  itself, so nothing needs mounting and the container needs no extra privileges.

For SMB, enter the server, share, folder within the share, user name and password (and the domain
if the server needs one). Use an account that can reach only that folder. The folder is created if
missing, and a test file is written and removed before the settings are saved.

### How much space

Enough for **one complete export**, plus 1 GB to spare: the app checks free space before each download
and stops, with a clear message, rather than fill the disk. Archives already imported can be
deleted from **Cleanup** to free space.

### Archives already in the folder

You can also copy Takeout archives into the download folder yourself. They are imported on the
next run like downloaded ones.
