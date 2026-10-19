# Updates

## Finding out about new releases

Once a day the app asks GitHub whether a new release is out, and shows a banner at the top of
every page when there is one. Opening **Help → Updates** or **Help → About** also asks, if the
last answer is more than 10 minutes old, so the latest release they show is current. Press
**Check now** under Help → Updates to ask straight away; it can be repeated once a minute. The
check reads GitHub's public releases page. If a check fails, Help → Updates says why.

The check is anonymous and sends nothing but the app's version. Switch the daily check off under
Help → Updates if you prefer. The app never installs anything by itself.

## What's new

When a new release is out, **What's new** under Help → Updates shows what it brings, before you
install it; the banner's **What's new** link leads there. **Change log**, under Help → Updates and
Help → About, lists every release, newest first: any newer releases, the one you are running
(marked **Running**) and the three before it in full, and earlier ones folded away.

The app carries its own change log, so the releases up to the one running can always be read.
For a newer release it fetches that release's `CHANGELOG.md` from GitHub once, anonymously: with
the daily check, when you press **Check now**, or in the background when Help → Updates opens.
Until then, What's new links to the release notes on GitHub instead. From the fetched change log
the app uses only the releases newer than the one running.

## Release candidates

A release candidate is a test build of the next release, such as `0.6.0-rc1`, published as a
pre-release on GitHub so it can be tried before everyone gets it. They are off unless you switch
them on: tick **Offer release candidates** under Help → Updates and press **Save**. The app then
checks GitHub's list of releases, which includes pre-releases, and offers the newest one: a
release candidate, or a final release when that is newer. Untick it to be offered only final
releases again.

Release candidates are for testing. They can have bugs, including ones that stop an import part
way. Your settings and history are kept when you update. Switching release candidates off does
not take you back to the last final release: you stay on the candidate until a newer final
release replaces it.

What a release candidate brings is shown under What's new as usual, marked **Release
candidate**. One-click updates install release candidates with update helper version 4 or later;
an older helper offers the one-command upgrade first. The helper installs a candidate only when
the app says release candidates are switched on, only if it is a published pre-release of this
project, and only until its final release is out.

## Updating by hand

On the Docker host, in the folder holding `compose.yaml`:

```bash
# The newest release, from where GitHub's "latest release" page points:
V=$(curl -fsS -o /dev/null -w '%{redirect_url}' \
  https://github.com/aistuartai/Googich_takeaway/releases/latest | grep -oE '[0-9.]+$')
[ -n "$V" ] && sed -i -E "s|(googich_takeaway:)[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?|\1$V|" compose.yaml \
  && docker compose pull && docker compose up -d
```

The compose file names a fixed version, so the app changes only when you choose.

## One-click updates

With the optional **update helper** installed on the Docker host, the banner offers **Update now**
and Help → Updates offers **Update to X.Y.Z**. The app itself never gets access to Docker: it only
writes the version it found into `data/updater/request.json`. The helper, a shell script started by systemd outside the
container, then:

1. accepts nothing but a plain version number such as `0.6.1`, or a release candidate such as
   `0.6.2-rc1` when release candidates are switched on,
2. checks it is a published release of this project, on GitHub's releases pages, that has its
   helper files attached (so it installs 0.3.5 or later),
3. refuses a release older than the one running, so nothing can take the app back to a
   version with bugs fixed since (helper version 5 and later; to go back on purpose, run
   `systemctl edit googich-updater.service` and add `[Service]` and
   `Environment=GOOGICH_ALLOW_DOWNGRADE=1`, which survives upgrading the helper),
4. sets that version in `compose.yaml`, keeping a copy of the old file,
5. pulls the image, restarts the container, and waits for it to report the new version,
6. restores the previous version automatically if anything fails, or if the update is stopped
   part way (by a shutdown, say).

If a run is going when you press Update, the app pauses it first, at the next safe point, and
only then asks the helper to install. Once the new version starts, the paused run resumes by
itself. If the update fails, the run resumes straight away.

Progress and the result appear in the banner and under Help → Updates.

## Installing the update helper

Copy the command from **Help → Updates**: it is made for the version you run, with a line that
checks the installer first. For this release it is the same as:

```bash
curl -fsSLO https://github.com/aistuartai/Googich_takeaway/releases/download/v0.6.1/install-updater.sh
sudo bash install-updater.sh /opt/googich
```

Change `/opt/googich` if `compose.yaml` is somewhere else. In a Proxmox container, run it inside
the container, without `sudo`, for example:

```bash
pct exec 123 -- bash -c 'cd /tmp && curl -fsSLO <installer URL> && bash install-updater.sh /opt/googich'
```

The extra line on Help → Updates checks the installer's SHA-256 against the copy inside the app,
so a changed download is caught before it runs. The installer then checks every helper file it
downloads against the release's published checksums, and installs nothing if one does not
match.

**The same command upgrades it.** Help → Updates and Help → About show the installed helper's
version (5 at present), and say plainly whether upgrading is needed:

- **Upgrade required**: this release, or release candidates if you switched them on, needs a
  newer helper for one-click updates. The *Update complete* banner says so too. Run the
  command shown.
- **Optional**: a newer helper is available and says what it adds, but yours keeps working as
  it is. Upgrade whenever suits you, or not at all.
- **Up to date**: nothing to do.

The helper changes rarely: an older one keeps working with newer releases unless a release
says it needs more.

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
`/usr/local/lib/googich-updater`, the two `googich-updater` files in `/etc/systemd/system`, and
`/etc/systemd/system/googich-updater.service.d` if you made one.
