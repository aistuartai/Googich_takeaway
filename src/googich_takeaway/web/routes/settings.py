"""Settings, schedule and notifications."""

from typing import Annotated

from fastapi import FastAPI, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from googich_takeaway import reminders
from googich_takeaway.config import (
    BAR_STYLES,
    COLOUR_SCHEMES,
    MOTION,
    THEMES,
    Config,
    ConfigError,
)
from googich_takeaway.schedule import TAKEOUT_FREQUENCIES, WEEKDAYS, upcoming_runs
from googich_takeaway.web.common import (
    TIMEZONES,
    _export_state,
    _zone,
    log,
)
from googich_takeaway.web.shared import ConfigDep, Shared


def register(app: FastAPI, web: Shared) -> None:
    log_usage = web.log_usage
    worker = web.worker
    clock = web.clock
    page = web.page
    result = web.result

    # --- settings, schedule, notifications, updates ------------------------------------------
    #
    # A form that fails to save shows its page again with an error. ``draft`` holds what was typed,
    # so it is shown again; passwords, keys and notification addresses never are.

    def settings_page(request: Request, config: Config, error: str | None = None) -> Response:
        """Time zone, look and feel, and how long logs are kept."""
        return page(
            request,
            config,
            "settings.html",
            general=config.general(),
            retention_days=config.log_retention_days(),
            log_usage=log_usage(),
            timezones=TIMEZONES,
            themes=THEMES,
            colour_schemes=COLOUR_SCHEMES,
            bar_styles=BAR_STYLES,
            motions=MOTION,
            saved=request.query_params.get("saved"),
            error=error,
            status_code=400 if error else 200,
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_view(request: Request, config: ConfigDep) -> Response:
        return settings_page(request, config)

    @app.post("/settings/timezone")
    def save_timezone(
        request: Request, config: ConfigDep, timezone: Annotated[str, Form()]
    ) -> Response:
        try:
            config.save_timezone(timezone)
        except ConfigError as error:
            return settings_page(request, config, error=str(error))
        return RedirectResponse("/settings?saved=timezone#timezone", status_code=303)

    @app.post("/settings/look")
    def save_look(
        request: Request,
        config: ConfigDep,
        theme: Annotated[str, Form()] = "auto",
        colours: Annotated[str, Form()] = "spectrum",
        bars: Annotated[str, Form()] = "striped",
        motion: Annotated[str, Form()] = "auto",
    ) -> Response:
        try:
            config.save_look(theme, colours, bars, motion)
        except ConfigError as error:
            return settings_page(request, config, error=str(error))
        return RedirectResponse("/settings?saved=look#look", status_code=303)

    @app.post("/settings/logs")
    def save_log_retention(
        request: Request,
        config: ConfigDep,
        days: Annotated[str, Form()] = "",
        back: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            config.save_log_retention(days)
        except ConfigError as error:
            if back == "logs":
                return RedirectResponse("/logs?saved=retention-invalid", status_code=303)
            return settings_page(request, config, error=str(error))
        worker.prune_logs()  # a shorter period applies straight away
        if back == "logs":  # only ever these two places
            return RedirectResponse("/logs?saved=retention", status_code=303)
        return RedirectResponse("/settings?saved=logs#logs", status_code=303)

    def schedule_page(
        request: Request,
        config: Config,
        error: str | None = None,
        draft: dict[str, str] | None = None,
    ) -> Response:
        schedule = config.schedule()
        zone = _zone(config.general().timezone)
        today = clock().astimezone(zone).date()
        last = next(
            (r.started_at for r in config.state.recent_runs(50) if r.trigger == "schedule"), None
        )
        exports = schedule.exports()
        arrivals = [t.astimezone(zone).date() for t in config.state.download_times()]
        return page(
            request,
            config,
            "schedule.html",
            schedule=schedule,
            upcoming=upcoming_runs(schedule, clock(), zone, last),
            takeout_status=schedule.takeout_status(today),
            paused=config.schedule_paused(),
            zone=zone,
            today=today,
            takeout_frequencies=TAKEOUT_FREQUENCIES,
            takeout_ends=reminders.schedule_end(schedule.takeout_started),
            checks=[
                (
                    day,
                    schedule.check_days(day),
                    _export_state(day, exports, arrivals, today, schedule),
                )
                for day in exports
            ],
            next_export=next((day for day in exports if day >= today), None),
            weekdays=WEEKDAYS,
            general=config.general(),
            saved=request.query_params.get("saved"),
            error=error,
            draft=draft or {},
            status_code=400 if error else 200,
        )

    @app.get("/schedule", response_class=HTMLResponse)
    def schedule_view(request: Request, config: ConfigDep) -> Response:
        return schedule_page(request, config)

    @app.post("/schedule/takeout")
    def save_takeout_schedule(
        request: Request,
        config: ConfigDep,
        started: Annotated[str, Form()] = "",
        every_months: Annotated[str, Form()] = "2",
    ) -> Response:
        try:
            config.save_takeout_schedule(started, every_months)
        except ConfigError as error:
            return schedule_page(request, config, error=str(error))
        return RedirectResponse("/schedule?saved=takeout#takeout", status_code=303)

    @app.post("/schedule")
    def save_schedule(
        request: Request,
        config: ConfigDep,
        mode: Annotated[str, Form()],
        at: Annotated[str, Form()] = "03:00",
        weekday: Annotated[str, Form()] = "6",
        every_hours: Annotated[str, Form()] = "24",
        pause_after: Annotated[str, Form()] = "3",
    ) -> Response:
        try:
            config.save_schedule(mode, at, weekday, every_hours, pause_after)
        except ConfigError as error:
            draft = {
                "mode": mode,
                "at": at,
                "weekday": weekday,
                "every_hours": every_hours,
                "pause_after": pause_after,
            }
            return schedule_page(request, config, str(error), draft=draft)
        return RedirectResponse("/schedule?saved=schedule#schedule", status_code=303)

    @app.post("/schedule/follow")
    def save_follow_options(
        request: Request,
        config: ConfigDep,
        retry_days: Annotated[str, Form()] = "1",
        wait_days: Annotated[str, Form()] = "14",
        fallback_weekly: Annotated[str, Form()] = "",
        fallback_weekday: Annotated[str, Form()] = "6",
    ) -> Response:
        try:
            config.save_follow_options(
                retry_days, wait_days, bool(fallback_weekly), fallback_weekday
            )
        except ConfigError as error:
            return schedule_page(request, config, str(error))
        return RedirectResponse("/schedule?saved=follow#follow", status_code=303)

    def notifications_page(
        request: Request, config: Config, error: str | None = None, draft_name: str = ""
    ) -> Response:
        return page(
            request,
            config,
            "notifications.html",
            outcomes=config.notification_outcomes(),
            targets=config.notification_targets(),
            saved=request.query_params.get("saved"),
            error=error,
            draft_name=draft_name,
            status_code=400 if error else 200,
        )

    @app.get("/notifications", response_class=HTMLResponse)
    def notifications_view(request: Request, config: ConfigDep) -> Response:
        return notifications_page(request, config)

    @app.post("/notifications")
    def save_notifications(
        request: Request,
        config: ConfigDep,
        outcomes: Annotated[list[str] | None, Form()] = None,
    ) -> Response:
        try:
            config.save_notifications(None, outcomes or [])
        except ConfigError as error:
            return notifications_page(request, config, error=str(error))
        return RedirectResponse("/notifications?saved=outcomes#outcomes", status_code=303)

    @app.post("/notifications/add")
    def add_notification(
        request: Request,
        config: ConfigDep,
        name: Annotated[str, Form()] = "",
        url: Annotated[str, Form()] = "",
    ) -> Response:
        try:
            config.add_notification_target(name, url)
        except ConfigError as error:  # the URL is not put back into the page
            return notifications_page(request, config, error=str(error), draft_name=name)
        log.info("Notification %r added", name.strip())
        return RedirectResponse("/notifications?saved=added", status_code=303)

    @app.post("/notifications/{target_id}/remove")
    def remove_notification(config: ConfigDep, target_id: str) -> Response:
        config.remove_notification_target(target_id)
        return RedirectResponse("/notifications?saved=removed", status_code=303)

    @app.post("/notifications/test", response_class=HTMLResponse)
    def test_notifications(request: Request, config: ConfigDep) -> Response:
        if not config.has_notification_urls():
            return result(request, False, "Add a notification first.")
        ok, reasons, _ = config.test_notification()
        return test_result(request, ok, reasons)

    @app.post("/notifications/{target_id}/test", response_class=HTMLResponse)
    def test_notification(request: Request, config: ConfigDep, target_id: str) -> Response:
        found = next((t for t in config.notification_targets() if t.id == target_id), None)
        if found is None:
            return result(request, False, "No such notification.")
        ok, reasons, note = config.test_notification(target_id)
        log.info("Test notification to %r: %s", found.name, "sent" if ok else "failed")
        return test_result(request, ok, reasons, note)

    def test_result(request: Request, ok: bool, reasons: list[str], note: str = "") -> Response:
        if ok:
            return result(request, True, f"Test notification sent.{note}")
        why = " ".join(reasons) if reasons else "The service did not accept it."
        return result(request, False, f"Not delivered: {why}")
