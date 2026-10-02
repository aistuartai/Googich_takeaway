"""The log viewer, its live tail and the log download."""

import io
import zipfile
from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from googich_takeaway.web.common import (
    LOG_LEVELS,
    _zone,
)
from googich_takeaway.web.shared import ConfigDep, Shared, StateDep


def register(app: FastAPI, web: Shared) -> None:
    log_usage = web.log_usage
    templates = web.templates
    log_store = web.log_store
    page = web.page

    @app.get("/logs", response_class=HTMLResponse)
    def logs_page(
        request: Request,
        config: ConfigDep,
        state: StateDep,
        level: str = "INFO",
        q: str = "",
        run: int | None = None,
        follow: int = 1,
    ) -> Response:
        since = until = None
        selected = None
        if run is not None:
            selected = next((r for r in state.recent_runs(200) if r.id == run), None)
            if selected:
                since, until = selected.started_at, selected.finished_at
        entries = log_store.buffer.query(level=level, text=q[:200], since=since, until=until)
        return page(
            request,
            config,
            "logs.html",
            entries=entries,
            retention_days=config.log_retention_days(),
            log_usage=log_usage(),
            saved=request.query_params.get("saved"),
            level=level.upper() if level.upper() in LOG_LEVELS else "INFO",
            levels=LOG_LEVELS,
            q=q[:200],
            run=selected,
            follow=bool(follow) and selected is None,
            last_seq=entries[-1].seq if entries else 0,
            zone=_zone(config.general().timezone),
        )

    @app.get("/logs/tail", response_class=HTMLResponse)
    def logs_tail(
        request: Request, config: ConfigDep, after: int = 0, level: str = "INFO", q: str = ""
    ) -> Response:
        entries = log_store.buffer.query(level=level, text=q[:200], after=after)
        context = {
            "entries": entries,
            "zone": _zone(config.general().timezone),
            "last_seq": entries[-1].seq if entries else after,
            "follow": True,
            "oob": True,
            "level": level,
            "q": q[:200],
        }
        rows = templates.get_template("_log_rows.html").render(context)
        tail = templates.get_template("_log_tail.html").render(context)
        return HTMLResponse(rows + tail)

    @app.get("/logs/download")
    def logs_download() -> Response:
        """Every log file kept, oldest first, in one zip.

        Each file is read whole before anything is sent: the current file grows while the app
        runs, and streaming it from disk sent more bytes than the Content-Length announced,
        so browsers gave up on the download."""
        files = log_store.files()
        if not files:
            return Response("No log file yet.", status_code=404, media_type="text/plain")
        packed = io.BytesIO()
        with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in reversed(files):
                try:
                    data = path.read_bytes()
                    written = datetime.fromtimestamp(path.stat().st_mtime)
                except OSError:
                    continue  # rotated away meanwhile
                entry = zipfile.ZipInfo(path.name, written.timetuple()[:6])
                entry.compress_type = zipfile.ZIP_DEFLATED
                archive.writestr(entry, data)
        stamp = f"{datetime.now(UTC):%Y%m%d-%H%M%S}"
        return Response(
            packed.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="googich-logs-{stamp}.zip"'},
        )
