"""The dashboard, its live status, and starting, pausing and stopping runs."""

from typing import Annotated

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway import reminders
from googich_takeaway.config import (
    DASHBOARD_ITEMS,
    ConfigError,
)
from googich_takeaway.pipeline import RunOptions, ignore_failures, pending_failures
from googich_takeaway.web import demo
from googich_takeaway.web.common import (
    _zone,
)
from googich_takeaway.web.shared import ConfigDep, Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    settings = web.settings
    worker = web.worker
    templates = web.templates
    clock = web.clock
    page = web.page
    activity = web.activity
    journey = web.journey
    source_summaries = web.source_summaries
    destination_summary = web.destination_summary
    cleanup_summary = web.cleanup_summary
    dashboard_notice = web.dashboard_notice

    @app.get("/activity", response_class=HTMLResponse)
    def activity_view(request: Request, state: StateDep) -> Response:
        """Polled by the menu bar on every page, so a run started elsewhere shows up."""
        return templates.TemplateResponse(request, "_activity.html", {"activity": activity(state)})

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, config: ConfigDep, state: StateDep) -> Response:
        items = config.dashboard_items()
        return page(
            request,
            config,
            "dashboard.html",
            items=items,
            dashboard_items=DASHBOARD_ITEMS,
            journey=journey(config, state) if {"journey", "destinations"} & set(items) else None,
            source_cards=source_summaries(config, state) if "sources" in items else [],
            destinations=destination_summary(config, state) if "destinations" in items else None,
            cleanable=cleanup_summary(config, state) if "cleanup" in items else None,
            immich=config.immich(),
            general=config.general(),
            sources=config.sources(),
            schedule=config.schedule(),
            status=worker.status(),
            progress=worker.progress(),
            failures=config.scheduled_failures(),
            runs=state.recent_runs(15) if "runs" in items else [],
            last_run=next(iter(state.recent_runs(1)), None),
            failed_exports=pending_failures(state),
            notice=dashboard_notice(request.query_params.get("notice", "")),
            reminders=reminders.takeout_reminders(config, state, clock()),
            zone=_zone(config.general().timezone),
        )

    @app.get("/dashboard/configure", response_class=HTMLResponse)
    def dashboard_configure(request: Request, config: ConfigDep) -> Response:
        return page(
            request,
            config,
            "dashboard_configure.html",
            items=config.dashboard_items(),
            dashboard_items=DASHBOARD_ITEMS,
            saved=request.query_params.get("saved"),
        )

    @app.post("/dashboard/items")
    def save_dashboard(
        config: ConfigDep,
        items: Annotated[list[str] | None, Form()] = None,
        back: Annotated[str, Form()] = "/",
    ) -> Response:
        try:
            config.save_dashboard_items(items or [])
        except ConfigError:
            return Response("Unknown dashboard item.", status_code=400)
        if back == "/dashboard/configure":  # only ever these two places
            return RedirectResponse("/dashboard/configure?saved=1", status_code=303)
        return RedirectResponse("/", status_code=303)

    @app.post("/demo/run")
    def demo_run() -> Response:
        if not settings.demo:
            return Response(status_code=404)
        demo.play(worker)
        return RedirectResponse("/", status_code=303)

    @app.get("/status", response_class=HTMLResponse)
    def status_card(request: Request, config: ConfigDep, state: StateDep) -> Response:
        """Polled by the dashboard while a run is going; reloads the page when it ends."""
        current = worker.status()
        if not current.running:
            # Back to the plain dashboard: a "Pausing…" or "Started" notice in the address
            # would otherwise come back with the reload.
            return Response(status_code=200, headers={"HX-Redirect": "/"})
        return templates.TemplateResponse(
            request,
            "_status.html",
            {
                "progress": worker.progress(),
                "items": config.dashboard_items(),
                "notice": None,
                "immich_link": config.immich().link,
                "journey": journey(config, state),
                "look": config.look(),
                "status": current,
                "schedule": config.schedule(),
                "general": config.general(),
                "sources": config.sources(),
                "zone": _zone(config.general().timezone),
            },
        )

    @app.post("/runs")
    def run_now(
        reimport: Annotated[str, Form()] = "",
        download_again: Annotated[str, Form()] = "",
    ) -> Response:
        options = RunOptions(reimport=bool(reimport), download_again=bool(download_again))
        if not worker.request_run(options):
            return RedirectResponse("/?notice=busy", status_code=303)
        worker.discard_paused()  # a new run replaces a paused one
        return RedirectResponse("/?notice=started", status_code=303)

    @app.get("/runs/stop", response_class=HTMLResponse)
    def confirm_stop(request: Request, config: ConfigDep, kind: str = "pause") -> Response:
        """Before Pause or Cancel: say exactly what happens to the file being worked on."""
        if kind not in ("pause", "cancel"):
            kind = "pause"
        if not worker.status_running():
            return RedirectResponse("/?notice=idle", status_code=303)
        active = worker.tracker.active()
        return page(
            request,
            config,
            "stop_confirm.html",
            kind=kind,
            stage=active[0].value if active else None,
            item=active[1] if active else None,
        )

    @app.post("/runs/pause")
    def pause_run() -> Response:
        stopping = worker.request_stop("pause")
        return RedirectResponse(f"/?notice={'pausing' if stopping else 'idle'}", status_code=303)

    @app.post("/runs/cancel")
    def cancel_run() -> Response:
        stopping = worker.request_stop("cancel")
        return RedirectResponse(f"/?notice={'cancelling' if stopping else 'idle'}", status_code=303)

    @app.post("/runs/resume")
    def resume_run() -> Response:
        resumed = worker.resume_paused()
        return RedirectResponse(f"/?notice={'resumed' if resumed else 'busy'}", status_code=303)

    @app.get("/runs/failures/ignore", response_class=HTMLResponse)
    def confirm_ignore_failures(
        request: Request, config: ConfigDep, state: StateDep, export: str = ""
    ) -> Response:
        record = next((r for r in pending_failures(state) if r["export_id"] == export), None)
        if record is None:
            return RedirectResponse("/", status_code=303)
        return page(request, config, "failures_confirm.html", record=record)

    @app.post("/runs/failures/ignore")
    def ignore_failed_files(state: StateDep, export: Annotated[str, Form()] = "") -> Response:
        if worker.status_running():
            return RedirectResponse("/?notice=busy", status_code=303)
        ignored = ignore_failures(state, export, clock())
        return RedirectResponse(f"/?notice={'ignored' if ignored else 'idle'}", status_code=303)

    @app.post("/runs/discard")
    def discard_run() -> Response:
        worker.discard_paused()
        return RedirectResponse("/?notice=discarded", status_code=303)

    @app.post("/schedule/pause")
    def pause_schedule(config: ConfigDep) -> Response:
        config.set_schedule_paused(True, by_user=True)
        return RedirectResponse("/?notice=schedule-paused", status_code=303)

    @app.post("/schedule/resume")
    def resume(config: ConfigDep) -> Response:
        config.set_schedule_paused(False)
        return RedirectResponse("/?notice=schedule-resumed", status_code=303)
