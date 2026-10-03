"""Destinations: Immich and the download folder."""

from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway.config import (
    Config,
    ConfigError,
)
from googich_takeaway.destinations.immich import ImmichError
from googich_takeaway.locations import (
    LocationError,
)
from googich_takeaway.pipeline import forget_library
from googich_takeaway.progress import format_size
from googich_takeaway.web.common import (
    OPTIONAL_PERMISSIONS,
    REQUIRED_PERMISSIONS,
    log,
)
from googich_takeaway.web.shared import ConfigDep, Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    immich_factory = web.immich_factory
    smb_test = web.smb_test
    page = web.page
    result = web.result

    # --- destinations --------------------------------------------------------------------------

    def destinations_page(
        request: Request,
        config: Config,
        error: str | None = None,
        saved: str | None = None,
        draft: dict[str, str] | None = None,
        failed: str | None = None,
    ) -> Response:
        """Where photos go (Immich) and where archives wait (the download folder).

        ``draft`` holds what was typed into a form that failed to save, so it is shown again.
        Passwords and keys are never put back into the page."""
        return page(
            request,
            config,
            "destinations.html",
            immich=config.immich(),
            general=config.general(),
            has_smb_password=config.has_smb_password(),
            error=error,
            saved=saved,
            draft=draft or {},
            failed=failed,
            status_code=400 if error else 200,
        )

    @app.get("/destinations", response_class=HTMLResponse)
    def destinations_view(
        request: Request, config: ConfigDep, saved: str | None = None, error: str | None = None
    ) -> Response:
        return destinations_page(request, config, error=error[:300] if error else None, saved=saved)

    @app.post("/destinations/immich")
    def save_immich(
        request: Request,
        config: ConfigDep,
        state: StateDep,
        url: Annotated[str, Form()],
        public_url: Annotated[str, Form()] = "",
        api_key: Annotated[str, Form()] = "",
    ) -> Response:
        before = config.immich()
        old_key = config.immich_key() if api_key else None
        try:
            config.save_immich(url, public_url, api_key or None)
        except ConfigError as error:
            draft = {"url": url, "public_url": public_url}
            return destinations_page(request, config, str(error), draft=draft, failed="immich")
        # Another server or another key may be another library: ask, if anything was uploaded.
        moved = before.url is not None and (
            before.url != config.immich().url or (old_key is not None and old_key != api_key)
        )
        if moved and state.upload_count("immich"):
            return RedirectResponse("/destinations/immich/library?changed=1", status_code=303)
        return RedirectResponse("/destinations?saved=immich#immich", status_code=303)

    @app.get("/destinations/immich/library", response_class=HTMLResponse)
    def library_question(
        request: Request, config: ConfigDep, state: StateDep, changed: str = ""
    ) -> Response:
        return page(
            request,
            config,
            "library_confirm.html",
            changed=bool(changed),
            uploads=state.upload_count("immich"),
        )

    @app.post("/destinations/immich/forget")
    def forget_previous_library(config: ConfigDep, state: StateDep) -> Response:
        if web.worker.run_pending_or_going():
            note = "A run is going: start afresh once it has finished."
            return RedirectResponse(f"/destinations?error={quote(note)}#immich", status_code=303)
        forgotten = forget_library(state, "immich", web.clock())
        log.warning(
            "Started afresh with a different Immich library: forgot %d uploads and every "
            "imported export",
            forgotten,
        )
        return RedirectResponse("/destinations?saved=afresh#immich", status_code=303)

    @app.post("/destinations/immich/test", response_class=HTMLResponse)
    def test_immich(request: Request, config: ConfigDep) -> Response:
        immich = config.immich()
        key = config.immich_key()
        if not immich.url or not key:
            return result(request, False, "Save the Immich address and API key first.")
        try:
            with immich_factory(immich.url, key) as client:
                version = client.server_version()
                granted = set(client.key_permissions())
        except ImmichError as error:
            return result(request, False, str(error))
        missing = [p for p in REQUIRED_PERMISSIONS if p not in granted and "all" not in granted]
        optional = [p for p in OPTIONAL_PERMISSIONS if p not in granted and "all" not in granted]
        if missing:
            return result(
                request,
                False,
                f"Connected to Immich {version}, but the API key lacks: {', '.join(missing)}.",
            )
        note = "".join(f" Optional, for {OPTIONAL_PERMISSIONS[p]}: {p}." for p in optional)
        return result(request, True, f"Connected to Immich {version}. The API key is fine.{note}")

    @app.post("/destinations/downloads")
    def save_general(
        request: Request,
        config: ConfigDep,
        storage: Annotated[str, Form()] = "local",
        staging: Annotated[str, Form()] = "",
        smb_server: Annotated[str, Form()] = "",
        smb_share: Annotated[str, Form()] = "",
        smb_folder: Annotated[str, Form()] = "",
        smb_username: Annotated[str, Form()] = "",
        smb_password: Annotated[str, Form()] = "",
        smb_domain: Annotated[str, Form()] = "",
        smb_port: Annotated[str, Form()] = "445",
    ) -> Response:
        try:
            if storage == "smb":
                config.save_smb(
                    smb_server,
                    smb_share,
                    smb_folder,
                    smb_username,
                    smb_password or None,
                    port=smb_port,
                    domain=smb_domain,
                    test=smb_test,
                )
            else:
                config.save_general(staging)
        except ConfigError as error:
            draft = {
                "storage": storage,
                "staging": staging,
                "smb_server": smb_server,
                "smb_share": smb_share,
                "smb_folder": smb_folder,
                "smb_username": smb_username,
                "smb_domain": smb_domain,
                "smb_port": smb_port,
            }
            return destinations_page(request, config, str(error), draft=draft, failed="downloads")
        return RedirectResponse("/destinations?saved=downloads#downloads", status_code=303)

    @app.post("/destinations/downloads/test", response_class=HTMLResponse)
    def test_storage(request: Request, config: ConfigDep) -> Response:
        location = config.staging_location()
        if location is None:
            return result(request, False, "Save a download folder first.")
        try:
            location.prepare()
            free = location.free_space()
        except LocationError as error:
            return result(request, False, str(error))
        except Exception as error:
            log.exception("Download folder test failed")
            return result(request, False, f"Unexpected error ({type(error).__name__}); see Logs.")
        space = f" {format_size(free)} free." if free is not None else ""
        return result(request, True, f"Can write to {location.describe()}.{space}")
