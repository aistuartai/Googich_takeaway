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
sed -i 's/googich_takeaway:0.2.1/googich_takeaway:0.3.0/' compose.yaml   # the versions you have and want
docker compose pull && docker compose up -d
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

As root on the Docker host, in the folder holding `compose.yaml` (for example `/opt/googich`),
using the release you are running:

```bash
cd /opt/googich
V=0.3.3
base=https://raw.githubusercontent.com/aistuartai/Googich_takeaway/v$V/deploy/updater
install -d -m 755 /usr/local/lib/googich-updater
curl -fsSL "$base/googich-updater.sh" -o /usr/local/lib/googich-updater/googich-updater.sh
chmod 755 /usr/local/lib/googich-updater/googich-updater.sh
curl -fsSL "$base/googich-updater.path" -o /etc/systemd/system/googich-updater.path
curl -fsSL "$base/googich-updater.service" -o /etc/systemd/system/googich-updater.service
install -d -m 770 -o "$(stat -c %u data)" -g "$(stat -c %g data)" data/updater
systemctl daemon-reload
systemctl enable --now googich-updater.path
systemctl start googich-updater.service   # reports "Ready for updates." to the app
```

Read the script before installing it: it runs as root. If your install is not in
`/opt/googich`, change the path in both systemd files. To remove the helper, run
`systemctl disable --now googich-updater.path` and delete the three files.
