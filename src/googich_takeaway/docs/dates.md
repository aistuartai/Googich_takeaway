# Dates and time zones

Every photo and video should appear in Immich at the moment it was taken, in the local time of
the place it was taken. Takeout makes that harder than it sounds, because its sources of time
disagree:

- **Takeout's metadata file** (`photoTakenTime`) gives the moment in UTC, with no time zone.
- **The photo's EXIF data** (`DateTimeOriginal`) gives the local clock time, usually with no
  time zone.
- **A video's own creation time** is in UTC.

Immich treats a time without a time zone as UTC, which would shift photos by hours. So the app
always sends a date with an explicit offset, such as `2019-07-04T10:15:00+10:00`.

## How the date is chosen

The rules are fixed, so the same file always gets the same date.

1. **The photo has an EXIF date.** That clock time is used.
   - Its offset comes from the photo's own offset field if it has one.
   - Otherwise, from the difference between the EXIF time and Takeout's UTC time, which is the
     time zone the photo was taken in.
   - Otherwise, from the time zone set under **Configuration → Settings**.
   - If the EXIF time and Takeout's time are more than 14 hours apart, the date was changed in
     Google Photos after the photo was taken. Takeout's date wins, because that is the date you
     chose.
2. **No EXIF date, but Takeout has one** (most videos, screenshots, and images saved from the
   web). Takeout's moment is used, shown in the photo's own offset if it has one, or else the time
   zone set under Configuration → Settings.
3. **Neither, but the video has its own creation time.** That moment is used the same way.
4. **Nothing else, but the file name holds a date**, such as `IMG_20190704_101500.jpg` or
   `Screenshot_2019-07-04-10-15-00.png`. That is used.
5. **No date at all.** The file is not uploaded, because it would appear in Immich at the wrong
   time. The run reports how many files *need review*; they stay in the archive, and in Google
   Photos.

Dates that are impossible, such as before 1900, in the future, or the "zero" dates some cameras
write when they do not know, are ignored.

## Checking dates

After uploading, the app reads each file back from Immich and compares the date. A difference is
reported in the run summary and the log. Immich reads large uploads in the background, so some
checks finish on later runs; Cleanup waits for them.

## The time zone setting

It matters only for photos with no time zone information of their own. Set it to where most of
your photos were taken.
