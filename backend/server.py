from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Body, Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import ValidationError
from starlette.convertors import Convertor, register_url_convertor

from backend import config, db, repo
from backend.models import (
    JobStatusUpdate,
    Profile,
    ScrapeOverrides,
    SettingsUpdate,
    merge_partial,
)
from backend.repo import ScrapeAlreadyRunning
from backend.scheduler import STARTUP_DELAY_SECONDS, Scheduler, schedule_snapshot
from backend.scrape_service import ScrapeService

log = logging.getLogger("jobspy_dashboard.server")

SortBy = Literal["match_score", "exp_asc", "exp_desc", "date", "salary", "found"]
StatusFilter = Literal["all", "none", "saved", "applied", "interviewing", "rejected"]

_NOT_BUILT_HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>JobSpy dashboard</title></head><body style="font-family:sans-serif;padding:2rem">
<h1>The dashboard UI is not built yet</h1>
<p>Run <code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code>, then reload.
The API is available under <code>/api</code>.</p></body></html>"""


class _NonApiPath(Convertor[str]):
    """Matches any path except /api and /api/..., so unknown API paths 404 as JSON."""

    regex = r"(?!api(?:/|$)).*"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        return value


register_url_convertor("nonapi", _NonApiPath())


def get_conn() -> Iterator[sqlite3.Connection]:
    with db.connection() as conn:
        yield conn


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def _validation_422(exc: ValidationError, *prefix: str) -> HTTPException:
    issues = [
        {"loc": ["body", *prefix, *err["loc"]], "msg": err["msg"], "type": err["type"]}
        for err in exc.errors(include_url=False)
    ]
    return HTTPException(status_code=422, detail=issues)


def _settings_payload(conn: sqlite3.Connection) -> dict[str, object]:
    return {
        "scrape": repo.get_scrape_settings(conn).model_dump(),
        "schedule": repo.get_schedule_settings(conn).model_dump(),
        "available_sites": list(config.AVAILABLE_SITES),
        "limits": {
            "min_interval_minutes": config.min_interval_minutes(),
            "warn_queries": config.WARN_QUERIES,
        },
    }


def _configure_logging() -> None:
    logger = logging.getLogger("jobspy_dashboard")
    if logger.handlers or logging.getLogger().handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def _startup() -> None:
    db.init_db()
    with db.connection() as conn:
        interrupted = repo.mark_dead_runs_interrupted(conn)
    if interrupted:
        log.info("Marked %d abandoned run(s) as interrupted", interrupted)


def _shutdown(service: ScrapeService, scheduler: Scheduler | None, timeout: float) -> None:
    if scheduler is not None:
        scheduler.request_stop()
    service.shutdown(timeout=timeout)
    if scheduler is not None:
        scheduler.join(timeout=5.0)


def create_app(
    *,
    scheduler_enabled: bool = True,
    startup_delay: float = STARTUP_DELAY_SECONDS,
    service: ScrapeService | None = None,
    shutdown_timeout: float = 10.0,
) -> FastAPI:
    scrape_service = service or ScrapeService()
    scheduler = Scheduler(scrape_service, startup_delay=startup_delay) if scheduler_enabled else None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        _configure_logging()
        await asyncio.to_thread(_startup)
        if scheduler is not None:
            scheduler.start()
        try:
            yield
        finally:
            await asyncio.to_thread(_shutdown, scrape_service, scheduler, shutdown_timeout)

    app = FastAPI(title="JobSpy India Dashboard", lifespan=lifespan)
    app.state.service = scrape_service
    app.state.scheduler = scheduler
    _add_api_routes(app, scrape_service, scheduler)
    _add_spa_routes(app)
    return app


def _add_api_routes(
    app: FastAPI, service: ScrapeService, scheduler: Scheduler | None
) -> None:
    @app.get("/api/profile")
    def get_profile(conn: Conn) -> dict[str, object]:
        return repo.get_profile(conn).model_dump()

    @app.put("/api/profile")
    def put_profile(profile: Profile, conn: Conn) -> dict[str, object]:
        rescored = repo.replace_profile(conn, profile)
        return {"success": True, "profile": repo.get_profile(conn).model_dump(), "rescored": rescored}

    @app.get("/api/settings")
    def get_settings(conn: Conn) -> dict[str, object]:
        return _settings_payload(conn)

    @app.put("/api/settings")
    def put_settings(update: SettingsUpdate, conn: Conn) -> dict[str, object]:
        with db.transaction(conn):
            scrape = repo.get_scrape_settings(conn)
            schedule = repo.get_schedule_settings(conn)
            try:
                if update.scrape is not None:
                    scrape = merge_partial(scrape, update.scrape)
            except ValidationError as exc:
                raise _validation_422(exc, "scrape") from None
            try:
                if update.schedule is not None:
                    schedule = merge_partial(schedule, update.schedule)
            except ValidationError as exc:
                raise _validation_422(exc, "schedule") from None
            repo.save_scrape_settings(conn, scrape)
            repo.save_schedule_settings(conn, schedule)
        if scheduler is not None:
            scheduler.wake()
        return _settings_payload(conn)

    @app.post("/api/scrape")
    def start_scrape(
        overrides: Annotated[ScrapeOverrides | None, Body()] = None,
    ) -> dict[str, object]:
        try:
            run_id = service.start(overrides, "manual")
        except ValidationError as exc:
            raise _validation_422(exc) from None
        except ScrapeAlreadyRunning as exc:
            raise HTTPException(
                status_code=409, detail=f"A scrape is already running (run #{exc.run_id})"
            ) from None
        return {"success": True, "run_id": run_id}

    @app.post("/api/scrape/stop")
    def stop_scrape(conn: Conn) -> dict[str, object]:
        repo.reap_dead_runs(conn, active_run_ids=service.active_run_ids())
        run_id = repo.request_cancel(conn)
        if run_id is None:
            raise HTTPException(status_code=404, detail="No scrape is running")
        service.notify_cancel(run_id, "Cancelled from the dashboard")
        return {"success": True}

    @app.get("/api/scrape/status")
    def scrape_status(conn: Conn) -> dict[str, object]:
        repo.reap_dead_runs(conn, active_run_ids=service.active_run_ids())
        current = repo.get_current_run(conn)
        return {
            "is_scraping": current is not None,
            "current_run": current,
            "schedule": schedule_snapshot(conn),
            "history": repo.list_runs(conn, config.HISTORY_LIMIT),
        }

    @app.get("/api/jobs")
    def list_jobs(
        conn: Conn,
        search: str | None = None,
        role_track: str = "all",
        location: str = "all",
        sites: str = "all",
        sort_by: SortBy = "match_score",
        min_score: Annotated[int, Query(ge=0, le=100)] = 0,
        remote_only: bool = False,
        exclude_senior: bool = True,
        status: StatusFilter = "all",
        new_only: bool = False,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1)] = config.DEFAULT_PAGE_SIZE,
    ) -> dict[str, object]:
        filters = repo.JobFilters(
            search=search,
            role_track=role_track,
            location=location,
            sites=sites,
            sort_by=sort_by,
            min_score=min_score,
            remote_only=remote_only,
            exclude_senior=exclude_senior,
            status=status,
            new_only=new_only,
            page=page,
            page_size=page_size,
        )
        return repo.list_jobs(conn, filters)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, conn: Conn) -> dict[str, object]:
        job = repo.get_job(conn, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return job

    @app.post("/api/jobs/{job_id}/status")
    def set_job_status(job_id: str, update: JobStatusUpdate, conn: Conn) -> dict[str, object]:
        if not repo.set_application(conn, job_id, update.status, update.notes):
            raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
        return {"success": True, "job_id": job_id, "status": update.status}

    @app.get("/api/stats")
    def get_stats(conn: Conn) -> dict[str, object]:
        return repo.get_stats(conn)


def _add_spa_routes(app: FastAPI) -> None:
    # Unknown /api/* paths match no route, so FastAPI answers {"detail": "Not Found"}.
    @app.api_route("/{full_path:nonapi}", methods=["GET", "HEAD"], include_in_schema=False)
    def serve_frontend(full_path: str) -> Response:
        return _spa_response(full_path)


def _spa_response(full_path: str) -> Response:
    dist = config.FRONTEND_DIST
    index = dist / "index.html"
    if not index.is_file():
        return HTMLResponse(_NOT_BUILT_HTML, status_code=503)
    if full_path:
        candidate = (dist / full_path).resolve()
        if candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate, headers=_cache_headers(dist, candidate))
        if "." in Path(full_path).name:
            return JSONResponse({"detail": "Not Found"}, status_code=404)
    return FileResponse(index, headers={"Cache-Control": "no-cache"})


def _cache_headers(dist: Path, path: Path) -> dict[str, str]:
    if path.is_relative_to(dist / "assets"):
        return {"Cache-Control": "public, max-age=31536000, immutable"}
    return {"Cache-Control": "no-cache"}


app = create_app()
