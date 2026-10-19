# What gets imported

Every run uploads the photos and videos Immich does not have yet, each with its date, place,
description and favourite mark. **Settings → What to import** chooses what else comes across, and
which photos are left out. The run box on the dashboard shows the choices under **Brings** and
**Leaves out**; **Change** opens the settings.

| Choice | Unless you change it |
|---|---|
| Albums | Recreated |
| Stacks: edited copies, RAW with JPEG, bursts | All three stacked |
| Tags: people, and the export each photo came from | Off |
| Photos in Google Photos' Archive | Go to Immich's Archive |
| Photos saved from a partner's library | Imported |
| Date range | Every photo |
| Photos in Google Photos' bin | Left out, unless a run includes them |

Choices apply to the next exports imported. To bring albums, stacks and tags to photos imported
before, run once with **Re-import files missing from Immich** (under **Options**, beside **Run
now**). That reads each export again, which is quick if it was read in the last 90 days, and
organises the photos Immich already has. Re-import also uploads again any photo you deleted in
Immich since, so check for those first. Photos still in Immich's trash are left alone.

## Changes you make in Immich stay

Each album, stack and tag is applied once per photo, and remembered. A photo you take out of an
album in Immich is not put back, a stack you split is not made again, and an album you delete is
not recreated. Starting afresh with a different Immich library (under **Destinations**) forgets
all of this, along with the uploads.

## Albums

Each Google Photos album becomes an Immich album with the same name and the same photos. An album
that already exists in Immich under that name is used rather than a second one made. Albums
without a name are left out.

Takeout puts a copy of each album photo in the album's folder as well as in its year folder. The
photo is uploaded once, and added to every album it was in.

## Stacks

A stack shows several photos as one in Immich, with the others a click away. Three kinds are
found, each from file names in the same folder:

- **Edited copies** with their original: `IMG_1.jpg` and `IMG_1-edited.jpg` (the word is in the
  export's language). The edited one is shown.
- **RAW and JPEG** shot together: `DSC_2.NEF` and `DSC_2.JPG` (or HEIC). The JPEG is shown.
- **Bursts**, by the names Pixel, Samsung, Huawei, Sony and Nothing phones give them. The one the
  phone marked as the cover is shown, or else the first.

## Tags

- **People named in Google Photos** become tags such as `People/Ann`. Immich finds and groups
  faces itself; the tags keep the names you gave in Google Photos, for searching.
- **The export each photo came from** tags what each import uploads, such as
  `Google Takeout/2026-10-03`, so you can find or undo one import.

## Archived, partner and bin photos

- **Archive.** Photos you archived in Google Photos go to Immich's Archive, out of the timeline,
  unless you choose to show them like any other.
- **Partner photos.** With Google Photos' partner sharing, photos your partner shares that you
  saved are in your export. They are imported unless you leave them out: do that if your
  partner imports their own library, and share it with Immich's partner sharing instead.
- **Google Photos' bin.** Photos you deleted in Google Photos can still be in the export. They
  are left out. To bring them across anyway, tick **Include photos in Google's bin** under
  **Options** before **Run now**: they go into Immich's trash, where you can restore the ones to
  keep. Immich empties its trash after 30 days unless set otherwise. If the run is paused or
  stopped before moving them, the next run moves them; one you restore in Immich stays
  restored.

## Date range

**Only photos taken** from one date, to another, or between the two. Photos outside the range,
and photos with no date at all, are left out. They stay in the archive, so Cleanup asks before
removing it. To import them later, widen the range and run with **Re-import**.

## Permissions

Each of these needs a permission on the Immich API key besides `asset.upload` and `asset.read`:

| For | Permissions |
|---|---|
| Albums | `album.read`, `album.create`, `albumAsset.create` |
| Stacks | `stack.create` |
| Tags | `tag.create`, `tag.asset` |
| Photos from Google's bin, in Immich's trash | `asset.delete` |

**Test connection**, under Destinations, says which are missing for what you ticked. A run
without one still imports the photos, and its summary says what was skipped and why.
