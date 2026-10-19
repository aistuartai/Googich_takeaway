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

`--staging` is the download folder. Key files must be readable only by you (`chmod 600`;
Docker secrets are accepted as they are). Keys are never accepted on the command line, so they
stay out of your shell history.

Run `uv run googich <command> --help` for every option, such as `import --reimport` (which also
uploads files deleted in Immich since), `import --yes`, `scan --json` or `fetch --list-only`.
`--immich-url` and `--key-file` can come from `GOOGICH_IMMICH_URL` and
`GOOGICH_IMMICH_KEY_FILE`; `--timezone` defaults to `$TZ`, else UTC.

`import` and `fetch` keep their records in `~/.local/share/googich/state.db` (or `--state`, or
`$GOOGICH_STATE`), separate from the web interface's: what they upload is not known to the web
interface unless both use the same file.

`import` uploads photos and videos with their dates, and leaves out photos in Google Photos' bin.
It does not make albums, stacks or tags, and does not apply the date range or partner choice:
those come from **Settings → What to import** in the web interface, on its runs (see
[What gets imported](what-gets-imported.md)).

In the container, `googich reset-password` clears the web password, so the next start offers
first-run setup again: see [Forgot the password](troubleshooting.md#the-web-interface).
