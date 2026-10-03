"""Cleanup: what is safe to delete, here and in Google Drive."""

from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway import cleanup
from googich_takeaway.destinations.immich import ImmichError
from googich_takeaway.locations import (
    LocationError,
)
from googich_takeaway.progress import format_size
from googich_takeaway.web.common import (
    _zone,
    log,
)
from googich_takeaway.web.shared import ConfigDep, Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    clock = web.clock
    listing_cache = web.listing_cache
    worker = web.worker
    immich_factory = web.immich_factory
    page = web.page
    result = web.result
    drive_labels = web.drive_labels

    # --- cleanup -------------------------------------------------------------------------------

    @app.get("/cleanup", response_class=HTMLResponse)
    def cleanup_page(
        request: Request,
        config: ConfigDep,
        state: StateDep,
        message: str | None = None,
        error: str | None = None,
    ) -> Response:
        staging = config.staging_location()
        try:
            staged = cleanup.staged_exports(staging, state)
            partials = cleanup.partial_downloads(staging)
        except LocationError as problem:
            staged, partials = [], []
            error = error or f"Cannot read the download folder: {problem}"
        return page(
            request,
            config,
            "cleanup.html",
            staged=staged,
            partials=partials,
            drive=cleanup.drive_exports(state, drive_labels(config)),
            running=worker.status_running(),
            message=message,
            error=error,
            zone=_zone(config.general().timezone),
        )

    @app.get("/cleanup/staged/{export_id}/confirm", response_class=HTMLResponse)
    def confirm_delete_staged(
        request: Request, config: ConfigDep, state: StateDep, export_id: str
    ) -> Response:
        """Before deleting: list exactly which files go, and what stays."""
        try:
            staged = cleanup.staged_exports(config.staging_location(), state)
        except LocationError as problem:
            return RedirectResponse(f"/cleanup?error={quote(str(problem))}", status_code=303)
        copy = next((c for c in staged if c.export_id == export_id and c.ready), None)
        if copy is None:
            note = "That export is not ready to delete."
            return RedirectResponse(f"/cleanup?error={quote(note)}", status_code=303)
        return page(
            request,
            config,
            "cleanup_confirm.html",
            copy=copy,
            folder=config.general().describe(),
            running=worker.status_running(),
        )

    @app.get("/cleanup/dates/{export_id}", response_class=HTMLResponse)
    def review_dates(request: Request, config: ConfigDep, export_id: str) -> Response:
        files = config.state.date_mismatches("immich", export_id)
        if not files:
            return RedirectResponse("/cleanup", status_code=303)
        return page(
            request, config, "dates_confirm.html", export_id=export_id, files=files[:200],
            total=len(files),
        )  # fmt: skip

    @app.post("/cleanup/dates/{export_id}")
    def accept_dates(config: ConfigDep, export_id: str) -> Response:
        kept = config.state.accept_dates("immich", export_id, clock())
        if kept:
            log.warning("Kept Immich's dates for %d files of export %s", kept, export_id)
        return RedirectResponse("/cleanup?saved=dates", status_code=303)

    @app.get("/cleanup/partial/{name}/confirm", response_class=HTMLResponse)
    def confirm_delete_partial(request: Request, config: ConfigDep, name: str) -> Response:
        try:
            partials = cleanup.partial_downloads(config.staging_location())
        except LocationError as problem:
            return RedirectResponse(f"/cleanup?error={quote(str(problem))}", status_code=303)
        found = next((p for p in partials if p.name == name), None)
        if found is None:
            return RedirectResponse("/cleanup?error=No+such+partial+download.", status_code=303)
        return page(
            request,
            config,
            "cleanup_confirm.html",
            partial=found,
            folder=config.general().describe(),
            running=worker.status_running(),
        )

    @app.post("/cleanup/staged/{export_id}")
    def delete_staged(
        config: ConfigDep,
        state: StateDep,
        export_id: str,
        confirm: Annotated[str, Form()] = "",
    ) -> Response:
        staging = config.staging_location()
        if staging is None:
            return RedirectResponse("/cleanup?error=No+download+folder+is+set.", status_code=303)
        if worker.status_running():
            return RedirectResponse(
                "/cleanup?error=Wait+for+the+current+run+to+finish.", status_code=303
            )
        try:
            freed = cleanup.delete_staged_export(
                staging, state, export_id, bool(confirm), log=log.warning
            )
        except cleanup.CleanupError as error:
            return RedirectResponse(f"/cleanup?error={quote(str(error))}", status_code=303)
        listing_cache.clear()
        note = f"Deleted export {export_id} from the download folder, freeing {format_size(freed)}."
        return RedirectResponse(f"/cleanup?message={quote(note)}", status_code=303)

    @app.post("/cleanup/partial/{name}")
    def delete_partial(config: ConfigDep, name: str) -> Response:
        staging = config.staging_location()
        if staging is None or worker.status_running():
            return RedirectResponse(
                "/cleanup?error=Not+possible+while+a+run+is+going.", status_code=303
            )
        try:
            freed = cleanup.delete_partial(staging, name)
        except cleanup.CleanupError as error:
            return RedirectResponse(f"/cleanup?error={quote(str(error))}", status_code=303)
        log.warning("Deleted partial download %s", name)
        note = f"Deleted the partial download of {name}, freeing {format_size(freed)}."
        return RedirectResponse(f"/cleanup?message={quote(note)}", status_code=303)

    @app.post("/cleanup/drive/{export_id}/recheck", response_class=HTMLResponse)
    def recheck_drive(
        request: Request, config: ConfigDep, state: StateDep, export_id: str
    ) -> Response:
        immich = config.immich()
        key = config.immich_key()
        if not immich.url or not key:
            return result(request, False, "Set up Immich first.")
        try:
            with immich_factory(immich.url, key) as client:
                found = cleanup.recheck(state, client, "immich", export_id)
        except ImmichError as error:
            return result(request, False, str(error))
        if found.checked == 0:
            return result(request, False, "No uploads are recorded for this export.")
        if found.ok:
            return result(
                request, True, f"All {found.checked} files are in Immich now. Safe to remove."
            )
        problems = []
        if found.missing:
            problems.append(f"{found.missing} are no longer in Immich")
        if found.trashed:
            problems.append(f"{found.trashed} are in Immich's trash")
        return result(
            request,
            False,
            f"Of {found.checked} files, {' and '.join(problems)}. Keep this archive in Drive, "
            "and run a Re-import (or restore them in Immich) first.",
        )
