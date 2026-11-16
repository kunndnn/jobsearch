from __future__ import annotations

import logging
import random
import sqlite3
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from backend import config, db, normalize, repo
from backend.models import ScrapeOverrides, ScrapeSettings, apply_overrides
from jobspy import scrape_jobs

log = logging.getLogger("jobspy_dashboard.scrape")

INDEED = "indeed"
CANCEL_POLL_SECONDS = 1.0
MAX_ERROR_LENGTH = 300


@dataclass(frozen=True)
class SearchQuery:
    index: int  # 1-based position in the plan
    term: str
    location: str


@dataclass(frozen=True)
class BoardCall:
    sites: tuple[str, ...]
    search_term: str


@dataclass(frozen=True)
class PreparedRun:
    run_id: int
    trigger: str
    settings: ScrapeSettings
    queries: tuple[SearchQuery, ...]


@dataclass(frozen=True)
class ProgressEvent:
    """Emitted to the optional progress callback: query_started, query_finished, waiting."""

    kind: str
    run_id: int
    index: int
    total: int
    term: str = ""
    location: str = ""
    found: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    wait_seconds: float = 0.0


@dataclass(frozen=True)
class RunResult:
    run_id: int
    status: str
    message: str
    total_queries: int
    queries_done: int
    jobs_found: int
    new_jobs: int
    updated_jobs: int
    duplicates: int
    errors: dict[str, str]
    pruned: int


ProgressFn = Callable[[ProgressEvent], None]


# --- planning ----------------------------------------------------------------


def plan_queries(settings: ScrapeSettings) -> tuple[SearchQuery, ...]:
    pairs = [(term, loc) for loc in settings.locations for term in settings.search_terms]
    return tuple(SearchQuery(i, term, loc) for i, (term, loc) in enumerate(pairs, start=1))


def indeed_search_term(term: str, exclude_terms: list[str]) -> str:
    phrase = term.replace('"', "").strip()
    negatives = [
        f'-"{word}"' if " " in word else f"-{word}"
        for word in (t.replace('"', "").strip() for t in exclude_terms)
        if word
    ]
    return " ".join([f'"{phrase}"', *negatives])


def board_calls(term: str, settings: ScrapeSettings) -> list[BoardCall]:
    """Indeed gets its own call with negative keywords; other boards share one."""
    shared = tuple(site for site in settings.sites if site != INDEED)
    calls = [BoardCall(shared, term)] if shared else []
    if INDEED in settings.sites:
        calls.append(BoardCall((INDEED,), indeed_search_term(term, settings.exclude_terms)))
    return calls


def run_params(settings: ScrapeSettings, overrides: ScrapeOverrides | None) -> dict[str, object]:
    keys = ("search_terms", "locations", "sites", "results_per_query", "hours_old")
    params: dict[str, object] = {key: getattr(settings, key) for key in keys}
    params["fetch_description"] = settings.fetch_description
    params["overrides"] = overrides.model_dump(exclude_none=True) if overrides else {}
    return params


# --- run bookkeeping ---------------------------------------------------------


def _short(message: object) -> str:
    text = " ".join(str(message).split()) or "unknown error"
    return text if len(text) <= MAX_ERROR_LENGTH else text[: MAX_ERROR_LENGTH - 1] + "…"


@dataclass
class _Tally:
    found: int = 0
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    done: int = 0
    attempted: Counter[str] = field(default_factory=Counter)
    failed: Counter[str] = field(default_factory=Counter)
    last_error: dict[str, str] = field(default_factory=dict)

    def add_query(
        self, sites: list[str], errors: dict[str, str], found: int, counts: repo.UpsertCounts
    ) -> None:
        self.attempted.update(dict.fromkeys(sites, 1))
        self.failed.update(dict.fromkeys(errors, 1))
        self.last_error.update(errors)
        self.found += found
        self.new += counts.new
        self.updated += counts.updated
        self.duplicates += counts.duplicates
        self.done += 1

    def errors(self) -> dict[str, str]:
        result = {}
        for site, message in sorted(self.last_error.items()):
            failed, attempted = self.failed[site], self.attempted[site]
            if attempted > 1:
                message = f"{message} (failed in {failed} of {attempted} queries)"
            result[site] = message
        return result

    def status(self, cancelled: bool) -> str:
        if cancelled:
            return "cancelled"
        if not self.last_error:
            return "completed"
        if self.found == 0 and set(self.attempted) <= set(self.failed):
            return "failed"
        return "partial"


def _summary(status: str, tally: _Tally, total: int, pruned: int, reason: str | None) -> str:
    counts = f"{tally.found} found, {tally.new} new, {tally.updated} updated"
    if tally.duplicates:
        noun = "duplicate" if tally.duplicates == 1 else "duplicates"
        counts += f", {tally.duplicates} {noun} (repost or other board)"
    heads = {
        "completed": "Completed",
        "partial": "Finished with board errors",
        "failed": "Failed: every board returned an error",
        "cancelled": f"{reason or 'Cancelled'} after {tally.done}/{total} queries",
    }
    parts = [f"{heads[status]}: {counts}"]
    if status in ("partial", "failed"):
        parts.append("errors on " + ", ".join(sorted(tally.last_error)))
    if pruned:
        parts.append(f"pruned {pruned} old job{'s' if pruned != 1 else ''}")
    return "; ".join(parts)


def _event(
    kind: str, prepared: PreparedRun, query: SearchQuery, **values: object
) -> ProgressEvent:
    return ProgressEvent(
        kind,
        prepared.run_id,
        query.index,
        len(prepared.queries),
        query.term,
        query.location,
        **values,  # type: ignore[arg-type]
    )


def _finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    message: str,
    errors: dict[str, str] | None,
) -> bool:
    return repo.finish_run(conn, run_id, status, message, errors=errors, only_if_running=True)


def _progress(conn: sqlite3.Connection, run_id: int, **fields: object) -> None:
    """Progress is cosmetic: a busy database must not fail the run."""
    try:
        repo.update_run_progress(conn, run_id, **fields)  # type: ignore[arg-type]
    except sqlite3.OperationalError as exc:
        log.warning("Could not update progress of run %s: %s", run_id, exc)


def _prune(conn: sqlite3.Connection, retention_days: int) -> int:
    try:
        return repo.prune_jobs(conn, retention_days)
    except sqlite3.OperationalError as exc:
        log.warning("Pruning old jobs skipped: %s", exc)
        return 0


class _Heartbeat:
    """Refreshes runs.heartbeat_at from its own thread and connection."""

    def __init__(self, run_id: int, interval: float, db_path: Path | None) -> None:
        self._run_id = run_id
        self._interval = interval
        self._db_path = db_path
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._loop, name=f"scrape-heartbeat-{run_id}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                with db.connection(self._db_path) as conn:
                    repo.heartbeat(conn, self._run_id)
            except Exception:
                log.warning("Heartbeat for run %s failed", self._run_id, exc_info=True)


@dataclass
class _ActiveRun:
    run_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    reason: str | None = None


# --- service -----------------------------------------------------------------


class ScrapeService:
    """Runs one scrape at a time (across processes, via the runs lock row).

    `run()` is synchronous (scheduler, CLI); `start()` acquires the lock and runs
    in a background thread (API). Both use prepare() + execute().
    """

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        heartbeat_interval: float = config.HEARTBEAT_INTERVAL_SECONDS,
        cancel_poll: float = CANCEL_POLL_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        self._db_path = db_path
        self._heartbeat_interval = heartbeat_interval
        self._cancel_poll = cancel_poll
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._active: dict[int, _ActiveRun] = {}

    # public API

    def prepare(
        self, overrides: ScrapeOverrides | None = None, trigger: str = "manual"
    ) -> PreparedRun:
        """Resolve settings and take the run lock. Raises pydantic ValidationError
        (bad overrides) or repo.ScrapeAlreadyRunning."""
        with db.connection(self._db_path) as conn:
            settings = apply_overrides(repo.get_scrape_settings(conn), overrides)
            queries = plan_queries(settings)
            run_id = repo.acquire_run_lock(
                conn, trigger, run_params(settings, overrides), len(queries)
            )
        with self._lock:
            self._active[run_id] = _ActiveRun(run_id)
        log.info("Run %s (%s) started: %d queries", run_id, trigger, len(queries))
        return PreparedRun(run_id, trigger, settings, queries)

    def execute(self, prepared: PreparedRun, progress: ProgressFn | None = None) -> RunResult:
        """Run a prepared scrape to the end. Always finishes the run row; a
        KeyboardInterrupt marks it cancelled and is re-raised."""
        with self._lock:
            active = self._active.setdefault(prepared.run_id, _ActiveRun(prepared.run_id))
        heartbeat = _Heartbeat(prepared.run_id, self._heartbeat_interval, self._db_path)
        tally = _Tally()
        try:
            heartbeat.start()
            with db.connection(self._db_path) as conn:
                return self._execute(conn, prepared, active, tally, progress)
        except Exception as exc:
            log.exception("Run %s failed", prepared.run_id)
            message = f"Failed: {_short(exc)}"
            self._finish(prepared.run_id, "failed", message, tally.errors())
            return self._result(prepared, "failed", message, tally, 0)
        except BaseException:
            total = len(prepared.queries)
            message = _summary("cancelled", tally, total, 0, "Cancelled (process interrupted)")
            self._finish(prepared.run_id, "cancelled", message, tally.errors())
            raise
        finally:
            heartbeat.stop()
            active.done.set()
            with self._lock:
                self._active.pop(prepared.run_id, None)

    def run(
        self,
        overrides: ScrapeOverrides | None = None,
        trigger: str = "manual",
        progress: ProgressFn | None = None,
    ) -> RunResult:
        return self.execute(self.prepare(overrides, trigger), progress)

    def start(self, overrides: ScrapeOverrides | None = None, trigger: str = "manual") -> int:
        prepared = self.prepare(overrides, trigger)
        thread = threading.Thread(
            target=self._execute_in_background,
            args=(prepared,),
            name=f"scrape-run-{prepared.run_id}",
            daemon=True,
        )
        thread.start()
        return prepared.run_id

    def notify_cancel(self, run_id: int | None = None, reason: str | None = None) -> None:
        """Wake in-process runs so they notice a cancel without waiting for a poll."""
        with self._lock:
            runs = [r for r in self._active.values() if run_id in (None, r.run_id)]
        for active in runs:
            active.reason = active.reason or reason
            active.cancel.set()

    def cancel(self, reason: str | None = None) -> int | None:
        """Flag the running run (any process) for cancellation; None if none runs."""
        with db.connection(self._db_path) as conn:
            run_id = repo.request_cancel(conn)
        if run_id is not None:
            self.notify_cancel(run_id, reason)
        return run_id

    def force_finish(self, run_id: int, status: str, message: str) -> bool:
        """Finish a run whose worker is stuck (e.g. inside a board request)."""
        return self._finish(run_id, status, message, None)

    def wait(self, run_id: int, timeout: float | None = None) -> bool:
        with self._lock:
            active = self._active.get(run_id)
        return True if active is None else active.done.wait(timeout)

    def active_run_ids(self) -> list[int]:
        with self._lock:
            return list(self._active)

    def shutdown(self, timeout: float = 10.0) -> None:
        """Server stop: cancel in-process runs, wait, then mark leftovers interrupted."""
        with self._lock:
            runs = list(self._active.values())
        if not runs:
            return
        self.notify_cancel(reason="Stopped because the server shut down")
        deadline = time.monotonic() + timeout
        for active in runs:
            if not active.done.wait(max(0.0, deadline - time.monotonic())):
                self.force_finish(
                    active.run_id,
                    "interrupted",
                    "Interrupted: the server shut down during a board request",
                )

    # internals

    def _execute_in_background(self, prepared: PreparedRun) -> None:
        try:
            self.execute(prepared)
        except BaseException:
            log.exception("Background run %s crashed", prepared.run_id)

    def _execute(
        self,
        conn: sqlite3.Connection,
        prepared: PreparedRun,
        active: _ActiveRun,
        tally: _Tally,
        progress: ProgressFn | None,
    ) -> RunResult:
        cancelled = self._run_queries(conn, prepared, active, tally, progress)
        status = tally.status(cancelled)
        pruned = 0 if status == "failed" else _prune(conn, prepared.settings.retention_days)
        message = _summary(status, tally, len(prepared.queries), pruned, active.reason)
        self._finish(prepared.run_id, status, message, tally.errors(), conn)
        log.info("Run %s: %s", prepared.run_id, message)
        return self._result(prepared, status, message, tally, pruned)

    def _run_queries(
        self,
        conn: sqlite3.Connection,
        prepared: PreparedRun,
        active: _ActiveRun,
        tally: _Tally,
        progress: ProgressFn | None,
    ) -> bool:
        """Returns True when the run was cancelled before every query ran."""
        total = len(prepared.queries)
        emit = progress or (lambda event: None)
        for query in prepared.queries:
            if query.index > 1 and self._pause_between(conn, prepared, active, query, emit):
                return True
            if self._cancelled(conn, prepared.run_id, active):
                return True
            label = f"Query {query.index}/{total}: “{query.term}” in {query.location}"
            _progress(
                conn, prepared.run_id, current_query=query.index, message=f"{label}…"
            )
            emit(_event("query_started", prepared, query))
            found, counts, errors = self._run_query(conn, prepared, query)
            tally.add_query(list(prepared.settings.sites), errors, found, counts)
            _progress(
                conn,
                prepared.run_id,
                current_query=query.index,
                jobs_found=tally.found,
                new_jobs=tally.new,
                updated_jobs=tally.updated,
                errors=tally.errors(),
                message=f"{label}: {found} found, {counts.new} new",
            )
            emit(
                _event(
                    "query_finished",
                    prepared,
                    query,
                    found=found,
                    new=counts.new,
                    updated=counts.updated,
                    duplicates=counts.duplicates,
                    errors=errors,
                )
            )
        # A Stop pressed during the last query still ends the run as cancelled.
        return self._cancelled(conn, prepared.run_id, active)

    def _pause_between(
        self,
        conn: sqlite3.Connection,
        prepared: PreparedRun,
        active: _ActiveRun,
        query: SearchQuery,
        emit: ProgressFn,
    ) -> bool:
        settings = prepared.settings
        delay = self._rng.uniform(settings.delay_min_seconds, settings.delay_max_seconds)
        if delay <= 0:
            return False
        total = len(prepared.queries)
        _progress(
            conn,
            prepared.run_id,
            message=f"Waiting {delay:.0f}s before query {query.index}/{total}",
        )
        emit(_event("waiting", prepared, query, wait_seconds=delay))
        return self._interruptible_wait(conn, prepared.run_id, active, delay)

    def _interruptible_wait(
        self, conn: sqlite3.Connection, run_id: int, active: _ActiveRun, seconds: float
    ) -> bool:
        """Sleep up to `seconds`; True as soon as a cancel is seen (in-process
        event, or the DB flag set by another process)."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            if active.cancel.wait(min(remaining, self._cancel_poll)):
                return True
            if repo.is_cancel_requested(conn, run_id):
                return True
        return False

    def _cancelled(self, conn: sqlite3.Connection, run_id: int, active: _ActiveRun) -> bool:
        return active.cancel.is_set() or repo.is_cancel_requested(conn, run_id)

    def _run_query(
        self, conn: sqlite3.Connection, prepared: PreparedRun, query: SearchQuery
    ) -> tuple[int, repo.UpsertCounts, dict[str, str]]:
        """Scrape one term × location on every selected board and store the results.
        Errors are returned per site; nothing here raises."""
        errors: dict[str, str] = {}
        frames: list[pd.DataFrame] = []
        for call in board_calls(query.term, prepared.settings):
            frame, call_errors = self._call_boards(call, query.location, prepared.settings)
            errors.update(call_errors)
            if frame is not None and not frame.empty:
                frames.append(frame)
        found = sum(len(frame) for frame in frames)
        try:
            jobs = [job for frame in frames for job in normalize.dataframe_to_jobs(frame)]
            counts = self._store(conn, jobs, prepared.run_id)
        except Exception as exc:
            log.exception("Saving results of query %s failed", query.index)
            for site in prepared.settings.sites:
                errors.setdefault(site, f"Saving results failed: {_short(exc)}")
            counts = repo.UpsertCounts()
        return found, counts, errors

    def _call_boards(
        self, call: BoardCall, location: str, settings: ScrapeSettings
    ) -> tuple[pd.DataFrame | None, dict[str, str]]:
        try:
            frame = scrape_jobs(
                site_name=list(call.sites),
                search_term=call.search_term,
                location=location,
                results_wanted=settings.results_per_query,
                hours_old=settings.hours_old,
                fetch_description=settings.fetch_description,
                country_indeed=settings.country_indeed,
                verbose=0,
            )
        except Exception as exc:
            log.warning("scrape_jobs failed for %s: %s", ",".join(call.sites), exc)
            return None, {site: _short(f"{type(exc).__name__}: {exc}") for site in call.sites}
        attrs_errors = (getattr(frame, "attrs", None) or {}).get("errors") or {}
        errors = {str(site): _short(msg) for site, msg in attrs_errors.items() if msg}
        return frame, errors

    def _store(
        self, conn: sqlite3.Connection, jobs: list[dict[str, object]], run_id: int
    ) -> repo.UpsertCounts:
        if not jobs:
            return repo.UpsertCounts()
        with db.transaction(conn):
            profile = repo.get_profile(conn)  # re-read: the profile may change mid-run
            version = repo.get_profile_version(conn)
            return repo.upsert_jobs(conn, jobs, profile, version, run_id)

    def _finish(
        self,
        run_id: int,
        status: str,
        message: str,
        errors: dict[str, str] | None,
        conn: sqlite3.Connection | None = None,
    ) -> bool:
        try:
            if conn is not None:
                return _finish_run(conn, run_id, status, message, errors)
            with db.connection(self._db_path) as fresh:
                return _finish_run(fresh, run_id, status, message, errors)
        except Exception:
            log.exception("Could not mark run %s as %s", run_id, status)
            return False

    @staticmethod
    def _result(
        prepared: PreparedRun, status: str, message: str, tally: _Tally, pruned: int
    ) -> RunResult:
        return RunResult(
            run_id=prepared.run_id,
            status=status,
            message=message,
            total_queries=len(prepared.queries),
            queries_done=tally.done,
            jobs_found=tally.found,
            new_jobs=tally.new,
            updated_jobs=tally.updated,
            duplicates=tally.duplicates,
            errors=tally.errors(),
            pruned=pruned,
        )
