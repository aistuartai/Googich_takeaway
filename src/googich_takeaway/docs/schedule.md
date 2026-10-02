# Schedule and notifications

Both have their own page in the **Configuration** menu: **Schedule** and **Notifications**.

## Schedule

| Choice | Runs |
|---|---|
| Off | only when you press **Run now** |
| Every few hours | every chosen number of hours |
| Daily | once a day, at the time you choose |
| Weekly | once a week, on the day and at the time you choose |
| Follow my Takeout schedule | on each expected Takeout export day, trying again until it arrives |

Times are in the time zone set under Configuration → Settings. Takeout exports arrive every month
or two, so **weekly** is plenty; a run that finds nothing new takes seconds.

### Following the Takeout schedule

Takeout's scheduled export runs for a year from the day it was set up, every month or every two
months. Note the day and how often in the *Google Takeout schedule* box on the Schedule page.

Then choose **Follow my Takeout schedule** in the *Googich schedule* box, with the time of day to
run, and save. A third box, *Following Takeout*, appears:

- **Waiting for an export.** On each expected export day the app runs at that time. An export can
  take hours or days to arrive in Drive, so if it has not arrived, the app tries again every day
  (or every few days) until it has, giving up after 14 days (or as you choose). Trying stops as
  soon as an archive from that export has been downloaded.
- **Between exports.** Nothing runs, unless you switch on the optional weekly run, on the day you
  choose, in case an export comes late or the noted date is slightly off.
- **Planned checks.** A table lists each expected export, the days the app will check for it,
  and whether it has arrived, is being waited for, is still planned, or was not seen.

Under *Coming up*, the page says what the schedule is doing now (for example, waiting for the
export expected on 15 December) and lists the next runs. When the Takeout year is over, only the
weekly runs continue (if switched on), and the app reminds you to set up a new Takeout schedule.

### Pausing the schedule

The dashboard's **Schedule** box shows what the schedule is set to and when it runs next. **Pause
schedule** stops scheduled runs until you press **Resume schedule**; Run now still works.

### When runs fail

A failed run never retries in a loop. It ends, records why, and notifies you. The next scheduled
run tries again. If several scheduled runs fail in a row (three, unless you change it), the
schedule **pauses**: the dashboard says so, and you press **Resume the schedule** once the problem
is fixed. Runs you start yourself do not count towards the pause.

### Run options

Under **Run now**, *Options* has two tick boxes for unusual cases. Tick one, then press **Run now**:

- **Re-import files missing from Immich:** rescans exports that were already imported and uploads
  anything Immich no longer has, including photos you deleted there. Photos in Immich's trash are
  left alone: restore those in Immich instead.
- **Download archives again:** fetches Drive archives downloaded before whose copy is no longer
  in the download folder, for example after a cleanup. Copies still there are not fetched again.

## Notifications

Notifications go through [Apprise](https://github.com/caronc/apprise), which supports most
services. Under **Configuration → Notifications**, add each one with a name, such as *Phone* or
*Family email*, and its Apprise URL. Each is listed by name and kind of service, with its own
**Test** and **Remove** buttons. URLs often contain passwords or tokens, so they are encrypted
before they are stored and never shown again.

Some example URLs:

| Service | URL |
|---|---|
| ntfy | `ntfys://ntfy.sh/your-topic` |
| Email | `mailtos://user:password@example.com?to=you@example.com` |
| Home Assistant | `hassio://homeassistant.local:8123/long-lived-access-token` |
| Discord | `discord://webhook_id/webhook_token` |
| Pushover | `pover://user_key@app_token` |
| Telegram | `tgram://bot_token/chat_id` |

The [Apprise wiki](https://github.com/caronc/apprise/wiki) lists every service and its format.
Apprise chooses the service by the start of the URL, so a URL starting with `http://` or
`https://` is not accepted.

When a test fails, the reason the service gave is shown, such as a wrong token or a server that
cannot be reached.

### Home Assistant

1. In Home Assistant, open your profile, then **Security → Long-lived access tokens**, and create
   a token.
2. Add a notification with the URL `hassio://ADDRESS:8123/TOKEN`, using `hassios://` if Home
   Assistant is only reachable over HTTPS. Use an address the app's container can reach, such as
   the IP address.
3. That shows the message in Home Assistant's notifications (the bell). To send it to a phone with
   the Home Assistant app instead, add the notify service after the token:
   `hassio://ADDRESS:8123/TOKEN/mobile_app_yourphone`. The service's name is under Developer tools
   → Actions; search for `notify.mobile_app`.

Choose what to be told about:

- **A run imports new files** (on by default).
- **A run finds nothing new** (off by default; it happens most runs).
- **A run fails** (on by default).
- **The schedule pauses after failed runs** (on by default).
- **The Takeout schedule needs attention** (on by default): no new export for 10 days longer than
  expected, or the scheduled export ends within three weeks. Each reminder is sent once.
