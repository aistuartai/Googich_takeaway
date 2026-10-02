# Updates

## Finding out about new releases

Once a day the app asks GitHub whether a new release is out, and shows a banner at the top of
every page when there is one. Press **Check now** under **Help → Updates**, or **Check for
updates** under **Help → About**, to ask straight away. A check can be repeated once a
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

Progress and the result appear in the banner and under Help → Updates. Installation steps are
in the project's README, under *One-click updates*.
