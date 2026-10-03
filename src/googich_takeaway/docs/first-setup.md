# First-time setup

Set these up in order. The dashboard shows a checklist until the first three are done.

No Immich yet? Start with [What is Immich?](what-is-immich.md) and
[Setting up Immich](immich-setup.md).

## 1. Connect Immich

Open **Configuration → Destinations**.

1. Enter the address this app uses to reach Immich, for example `http://immich:2283` or
   `http://192.168.1.20:2283`.
2. In Immich, open *Account Settings → API Keys*, create a key with the `asset.upload` and
   `asset.read` permissions (add `stack.create` if offered), and paste it in.
3. Press **Save**, then **Test connection**.

If you open Immich at a different address in your browser, such as
`https://photos.example.com`, enter it as the public address; it is used only for links.

More detail: [Immich and the download folder](destinations.md).

## 2. Choose the download folder

On the same page, choose where archives wait between download and import: a folder on this
server, or a folder on an SMB share (a NAS or Windows file server). It needs room for one complete
export. The folder is tested before it is saved.

## 3. Add a source

Choose how exports reach the app, under **Configuration → Sources**. The dashboard offers
the same choice.

**Automatically, from Google Drive.** Nothing to do once set up.

1. Set up a scheduled Google Takeout export to Google Drive:
   [Setting up Google Takeout](takeout.md).
2. Create a service account and its key, add the Drive folder under **Configuration → Sources**,
   then share the folder with the address the Sources page shows. Press **Test**. Step by step:
   [Connecting Google Drive](google-drive.md).

**Downloading the exports yourself.** No Google Cloud: Takeout emails you a link, and you save
the archives into the download folder (or a folder of your own). See
[Downloading exports yourself](manual-downloads.md).

## 4. Time zone, schedule and notifications

All under the **Configuration** menu:

- **Settings → Time zone:** used for photos that carry no time zone of their own.
- **Schedule:** when to run. **Follow my Takeout schedule** checks on each day an export is
  expected; **Weekly** is a simple alternative.
- **Settings → Speed:** how many files go to Immich at once (three unless you change it), and
  whether archives are read while the next ones download (on unless you turn it off).
- **Notifications:** where to send messages after runs (see
  [Schedule and notifications](schedule.md)).

## 5. First run

Press **Run now** on the dashboard. The first export of a big library takes a while: the dashboard
shows progress and an estimate of the time left. You can close the page; the run continues.

> [!IMPORTANT]
> Back up your Immich database before the first real import, and consider trying a test Immich
> user first.
