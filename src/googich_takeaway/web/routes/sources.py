"""Sources: Google Drive folders and folders of downloaded archives."""

from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway.config import (
    DOWNLOAD_FOLDER_KIND,
    MAX_KEY_FILE_BYTES,
    Config,
    ConfigError,
)
from googich_takeaway.locations import (
    LocationError,
)
from googich_takeaway.locations import archives as list_archives
from googich_takeaway.sources.base import SourceError
from googich_takeaway.sources.local import LocalSource
from googich_takeaway.web.shared import ConfigDep, Shared


def register(app: FastAPI, web: Shared) -> None:
    drive_factory = web.drive_factory
    page = web.page
    result = web.result

    # --- sources -------------------------------------------------------------------------------

    def sources_page(request: Request, config: Config, error: str | None = None) -> Response:
        drives = [s for s in config.sources() if s.kind == "gdrive"]
        accounts = {s.id: config.drive_account(s.id) for s in drives}
        folder_names = {s.id: config.drive_folder_name(s.id) for s in drives}
        return page(
            request,
            config,
            "sources.html",
            sources=config.sources(),
            accounts=accounts,
            folder_names=folder_names,
            staging=config.general().describe() if config.general().configured else None,
            has_download_source=any(s.kind == DOWNLOAD_FOLDER_KIND for s in config.sources()),
            error=error,
            status_code=400 if error else 200,
        )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_view(request: Request, config: ConfigDep) -> Response:
        return sources_page(request, config)

    @app.post("/sources/drive")
    def add_drive(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()],
        folder_id: Annotated[str, Form()],
        key_file: Annotated[UploadFile, File()],
    ) -> Response:
        # A plain function, so FastAPI runs it off the event loop: adding the source asks Google
        # for the folder's name, and pages polling meanwhile must not freeze.
        data = key_file.file.read(MAX_KEY_FILE_BYTES + 1)
        try:
            source_id = config.add_drive_source(name, folder_id, data)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        try:  # best effort: the folder may not be shared with the account yet
            with drive_factory(folder_id.strip(), config.drive_key(source_id)) as drive:
                config.set_drive_folder_name(source_id, drive.check_folder())
        except (SourceError, ConfigError):
            pass
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/download-folder")
    def add_download_folder(
        request: Request, config: ConfigDep, name: Annotated[str, Form()] = ""
    ) -> Response:
        if not config.general().configured:
            return sources_page(request, config, error="Choose the download folder first.")
        try:
            config.add_download_folder_source(name)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/local")
    def add_local(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()],
        path: Annotated[str, Form()],
    ) -> Response:
        try:
            config.add_local_source(name, path)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/key")
    def replace_key(
        request: Request,
        config: ConfigDep,
        source_id: int,
        key_file: Annotated[UploadFile, File()],
    ) -> Response:
        if not any(s.id == source_id and s.kind == "gdrive" for s in config.sources()):
            return Response(status_code=404)
        data = key_file.file.read(MAX_KEY_FILE_BYTES + 1)
        try:
            config.replace_drive_key(source_id, data)
        except ConfigError as error:
            return sources_page(request, config, error=str(error))
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/delete")
    def delete_source(config: ConfigDep, source_id: int) -> Response:
        config.delete_source(source_id)
        return RedirectResponse("/sources", status_code=303)

    @app.post("/sources/{source_id}/test", response_class=HTMLResponse)
    def test_source(request: Request, config: ConfigDep, source_id: int) -> Response:
        source = next((s for s in config.sources() if s.id == source_id), None)
        if source is None:
            return result(request, False, "No such source.")
        try:
            if source.kind == "local":
                files = LocalSource(Path(source.location)).list_archives()
            elif source.kind == DOWNLOAD_FOLDER_KIND:
                location = config.staging_location()
                if location is None:
                    return result(request, False, "No download folder is set.")
                try:
                    found = list_archives(location)
                except LocationError as error:
                    return result(request, False, f"Cannot read the download folder: {error}")
                count, size = len(found), sum(a.size for a in found) / 1000**3
                if not count:
                    return result(request, True, "Connected. No Takeout archives saved there yet.")
                return result(
                    request,
                    True,
                    f"Connected. {count} archives in the download folder, {size:.1f} GB.",
                )
            else:
                with drive_factory(source.location, config.drive_key(source_id)) as drive:
                    files = drive.list_archives()
                    if drive.folder_name:
                        config.set_drive_folder_name(source_id, drive.folder_name)
        except (SourceError, ConfigError) as error:
            return result(request, False, str(error))
        name = config.drive_folder_name(source_id) if source.kind == "gdrive" else None
        connected = f"Connected to the folder “{name}”." if name else "Connected."
        if not files:
            return result(request, True, f"{connected} No Takeout archives in the folder yet.")
        total = sum(f.size for f in files) / 1000**3
        return result(
            request, True, f"{connected} {len(files)} archives in the folder, {total:.1f} GB."
        )
