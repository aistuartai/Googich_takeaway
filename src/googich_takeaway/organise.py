"""After uploading an export: albums, stacks, tags, and photos from Google's bin to the trash.

Each is done once per photo and remembered, so what you change in Immich afterwards stays
changed: a photo taken out of an album is not put back, a stack you split is not remade, and an
album you delete is not made again. Photos Immich already had are organised too, so a run with
Re-import brings albums and stacks to photos imported before these options existed.

Each needs its own permission on the API key. One that is missing does not fail the run: the
run's summary says what was skipped and which permission would allow it.
"""

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial

from googich_takeaway.config import ImportSettings
from googich_takeaway.destinations.immich import ImmichClient, ImmichError, ImmichNotFoundError
from googich_takeaway.importer import Decision, ImportPlan, ImportResult
from googich_takeaway.progress import Stage, Tracker
from googich_takeaway.state import TRASH_DONE, TRASH_PENDING, State
from googich_takeaway.takeout.scan import ScannedItem
from googich_takeaway.takeout.stacks import BURST, EDITED, RAW, stack_groups

log = logging.getLogger("googich.run")

BATCH = 500
PEOPLE_TAG = "People"
IMPORT_TAG = "Google Takeout"

_NEEDS = {
    "albums": "album.create, album.read and albumAsset.create",
    "stacks": "stack.create",
    "tags": "tag.create and tag.asset",
    "trash": "asset.delete",
}


@dataclass
class OrganiseResult:
    albums_made: int = 0
    album_photos: int = 0
    """Photos added to albums."""
    stacks: int = 0
    tagged: int = 0
    """Photos given a tag."""
    trashed: int = 0
    """Photos from Google's bin moved to Immich's trash."""
    notes: list[str] = field(default_factory=list)
    """What could not be done, and why."""

    def add(self, other: "OrganiseResult") -> None:
        self.albums_made += other.albums_made
        self.album_photos += other.album_photos
        self.stacks += other.stacks
        self.tagged += other.tagged
        self.trashed += other.trashed
        self.notes += [n for n in other.notes if n not in self.notes]


def organise(
    plan: ImportPlan,
    result: ImportResult,
    client: ImmichClient,
    state: State,
    destination: str,
    settings: ImportSettings,
    clock: Callable[[], datetime],
    tracker: Tracker | None = None,
) -> OrganiseResult:
    done = OrganiseResult()
    present = [p.item for p in plan.items if p.decision in (Decision.UPLOAD, Decision.IN_IMMICH)]
    assets = state.asset_ids(destination, (i.sha1 for i in present))
    present = [i for i in present if i.sha1 in assets]
    uploaded = {i.sha1 for i in result.uploaded}

    def step(feature: str, detail: str, work: Callable[[], None]) -> None:
        if tracker:
            tracker.set_detail(Stage.UPLOAD, detail)
        try:
            work()
        except ImmichError as error:
            log.warning("Could not finish %s: %s", feature, error)
            if "permission" in str(error):
                done.notes.append(
                    f"No {feature}: the API key needs {_NEEDS[feature]} (Destinations)."
                )
            else:
                done.notes.append(f"Not all {feature} were made: {error}. Next run tries again.")
        finally:
            if tracker:
                tracker.set_detail(Stage.UPLOAD, "")

    if settings.albums:
        step(
            "albums",
            "Adding photos to albums.",
            partial(_albums, present, assets, client, state, destination, clock, done),
        )
    kinds = {EDITED: settings.stack_edited, RAW: settings.stack_raw, BURST: settings.stack_burst}
    if any(kinds.values()):
        step(
            "stacks",
            "Stacking photos that belong together.",
            partial(_stacks, present, assets, kinds, client, state, destination, clock, done),
        )
    if settings.tagging:
        step(
            "tags",
            "Tagging photos.",
            partial(
                _tags,
                present,
                assets,
                uploaded,
                plan.export_id,
                settings,
                client,
                state,
                destination,
                clock,
                done,
            ),
        )
    # Photos uploaded from Google's bin, by this run or by one stopped before getting here,
    # and not moved yet. Once moved, never again: a photo restored in Immich stays restored.
    sha1s = [i.sha1 for i in present]
    wanted = state.organised(destination, TRASH_PENDING, sha1s)
    moved = state.organised(destination, TRASH_DONE, sha1s)
    to_trash = [i.sha1 for i in present if ("", i.sha1) in wanted and ("", i.sha1) not in moved]
    if to_trash:
        step(
            "trash",
            "Moving photos from Google's bin to Immich's trash.",
            partial(_trash, to_trash, assets, client, state, destination, clock, done),
        )
    return done


def remember_trash_intent(
    plan: ImportPlan, state: State, destination: str, clock: Callable[[], datetime]
) -> None:
    """Before uploading: which of the files to upload go to Immich's trash afterwards."""
    pairs = [("", p.item.sha1) for p in plan.with_decision(Decision.UPLOAD) if p.to_trash]
    if pairs:
        state.mark_organised(destination, TRASH_PENDING, pairs, clock())


def _batches(ids: list[str]) -> Iterable[list[str]]:
    for start in range(0, len(ids), BATCH):
        yield ids[start : start + BATCH]


def _albums(
    items: list[ScannedItem],
    assets: dict[str, str],
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    done: OrganiseResult,
) -> None:
    already = state.organised(destination, "album", (i.sha1 for i in items))
    wanted: dict[str, list[str]] = {}
    for item in items:
        for title in item.albums:
            if (title, item.sha1) not in already:
                wanted.setdefault(title, []).append(item.sha1)
    existing: dict[str, str] | None = None
    for title, sha1s in sorted(wanted.items()):
        album_id = state.destination_album(destination, title)
        if album_id == "":
            continue  # deleted in Immich since it was made: not made again
        if album_id is None:
            if existing is None:
                existing = client.albums()
            album_id = existing.get(title)
            if album_id is None:
                album_id = client.create_album(title)
                done.albums_made += 1
                log.info("Made the album %s in Immich", title)
            state.set_destination_album(destination, title, album_id)
        try:
            for batch in _batches([assets[s] for s in sha1s]):
                client.add_to_album(album_id, batch)
        except ImmichNotFoundError:
            log.info("The album %s was deleted in Immich; it is not made again", title)
            state.set_destination_album(destination, title, "")
            continue
        state.mark_organised(destination, "album", ((title, s) for s in sha1s), clock())
        done.album_photos += len(sha1s)


def _stacks(
    items: list[ScannedItem],
    assets: dict[str, str],
    kinds: dict[str, bool],
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    done: OrganiseResult,
) -> None:
    already = {sha1 for _, sha1 in state.organised(destination, "stack", (i.sha1 for i in items))}
    for key, members in stack_groups(items).items():
        if not kinds.get(members[0].stack_kind) or any(m.sha1 in already for m in members):
            continue
        ids = list(dict.fromkeys(assets[m.sha1] for m in members))
        if len(ids) < 2:
            continue
        client.create_stack(ids)
        state.mark_organised(destination, "stack", ((key, m.sha1) for m in members), clock())
        done.stacks += 1


def _tags(
    items: list[ScannedItem],
    assets: dict[str, str],
    uploaded: set[str],
    export_id: str,
    settings: ImportSettings,
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    done: OrganiseResult,
) -> None:
    already = state.organised(destination, "tag", (i.sha1 for i in items))
    wanted: dict[str, list[str]] = {}
    import_tag = f"{IMPORT_TAG}/{export_id[:4]}-{export_id[4:6]}-{export_id[6:8]}"
    for item in items:
        # A "/" in a name would nest tags in Immich: People/AC/DC is not what was meant.
        people = [p.replace("/", "-") for p in item.people] if settings.tag_people else []
        names = [f"{PEOPLE_TAG}/{p}" for p in people]
        if settings.tag_import and item.sha1 in uploaded:
            names.append(import_tag)
        for name in names:
            if (name, item.sha1) not in already:
                wanted.setdefault(name, []).append(item.sha1)
    if not wanted:
        return
    tag_ids = client.upsert_tags(sorted(wanted))
    tagged: set[str] = set()
    for name, sha1s in sorted(wanted.items()):
        tag_id = tag_ids.get(name)
        if tag_id is None:
            continue
        for batch in _batches([assets[s] for s in sha1s]):
            client.tag_assets(tag_id, batch)
        state.mark_organised(destination, "tag", ((name, s) for s in sha1s), clock())
        tagged.update(sha1s)
    done.tagged += len(tagged)


def _trash(
    sha1s: list[str],
    assets: dict[str, str],
    client: ImmichClient,
    state: State,
    destination: str,
    clock: Callable[[], datetime],
    done: OrganiseResult,
) -> None:
    for batch in _batches(sha1s):
        client.trash([assets[s] for s in batch])
        state.mark_organised(destination, TRASH_DONE, [("", s) for s in batch], clock())
        done.trashed += len(batch)
