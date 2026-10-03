# Command-line tools

The same engine runs from the command line, for scripting or a quick look before using the web
interface. From a checkout of the repository, with [uv](https://docs.astral.sh/uv/):

```bash
uv run googich scan /path/to/archives --timezone Australia/Melbourne          # dry run
uv run googich scan /path/to/archives --immich-url http://immich:2283 \
  --key-file /path/to/immich.key                                             # what is new
uv run googich import /path/to/archives --immich-url http://immich:2283 \
  --key-file /path/to/immich.key --timezone Australia/Melbourne              # asks first
uv run googich fetch --drive-folder FOLDER_ID --service-account key.json \
  --staging /path/to/downloads                                               # from Drive
```

`--staging` is the download folder. Key files must be readable only by you (`chmod 600`). Keys are never accepted on the command
line, so they stay out of your shell history.

In the container, `googich reset-password` clears the web password, so the next start offers
first-run setup again: see [Forgot the password](troubleshooting.md#the-web-interface).
