# Updates

## Finding out about new releases

Once a day the app asks GitHub whether a new release is out, and shows a banner at the top of
every page when there is one. Opening **Help → Updates** or **Help → About** also asks, if the
last answer is more than 10 minutes old, so the latest release they show is current. Press
**Check now** under Help → Updates to ask straight away. A check can be repeated once a
minute: GitHub allows 60 anonymous checks an hour from each network address, shared by everything
on your network.

The check is anonymous and sends nothing but the app's version. Switch the daily check off under
Help → Updates if you prefer. The app never installs anything by itself.

## Updating by hand

On the Docker host, in the folder holding `compose.yaml`:

```bash
V=$(curl -fsS https://api.github.com/repos/aistuartai/Googich_takeaway/releases/latest \
  | grep -oE '"tag_name": *"v[0-9.]+"' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+')   # newest release
[ -n "$V" ] && sed -i -E "s|(googich_takeaway:)[0-9]+\.[0-9]+\.[0-9]+|\1$V|" compose.yaml \
  && docker compose pull && docker compose up -d
```

The compose file names a fixed version, so the app changes only when you choose.

## One-click updates

With the optional **update helper** installed on the Docker host, the banner and Help → Updates offer an
**Update** button. The app itself never gets access to Docker: it only writes the version it found
into `data/updater/request.json`. The helper, a shell script started by systemd outside the
container, then:

1. accepts nothing but a plain version number such as `0.3.0`,
2. checks it is a published release of this project on GitHub,
3. sets that version in `compose.yaml`, keeping a copy of the old file,
4. pulls the image, restarts the container, and waits for it to report the new version,
5. restores the previous version automatically if anything fails.

If a run is going when you press Update, the app pauses it first, at the next safe point, and
only then asks the helper to install. Once the new version starts, the paused run resumes by
itself. If the update fails, the run resumes straight away.

Progress and the result appear in the banner and under Help → Updates.

## Installing the update helper

On the Docker host, run:

```bash
curl -fsSLO https://github.com/aistuartai/Googich_takeaway/releases/download/v0.3.5/install-updater.sh
sudo bash install-updater.sh /opt/googich
```

Change `/opt/googich` if `compose.yaml` is somewhere else. In a Proxmox container, run it inside
the container with `pct exec <id> -- bash -c '…'`, without `sudo`.

**Help → Updates shows the same command for the version you run, with one more line** that
checks the installer's SHA-256 against the copy inside the app, so a changed download is caught
before it runs. The installer then checks every helper file it downloads against the release's
published checksums, and installs nothing if one does not match.

**The same command upgrades it.** When a release brings a newer helper, Help → Updates (and the
*Update complete* banner) says so; run the command again.

What it does, as root:

1. reads the version you run from `compose.yaml`, downloads that release's helper files from
   GitHub, and checks each against the release's SHA-256 sums;
2. installs the script in `/usr/local/lib/googich-updater/` and two systemd units that watch
   for requests from the app;
3. makes `data/updater` belong to root with the sticky bit set (mode `1770`): the app can add
   its request there, but cannot replace or redirect the files root writes;
4. starts watching, and reports *Ready for updates.* to the app.

Read the script first if you like: it is short. To remove the helper, run
`systemctl disable --now googich-updater.path` and delete
`/usr/local/lib/googich-updater` and the two `googich-updater` files in `/etc/systemd/system`.
