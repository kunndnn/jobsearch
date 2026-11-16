from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from backend import config, db, normalize, repo
from backend.models import ScheduleSettings, ScrapeOverrides
from backend.repo import ScrapeAlreadyRunning

log = logging.getLogger("jobspy_dashboard.scheduler")

STARTUP_DELAY_SECONDS = 15.0
IDLE_WAIT_SECONDS = 3600.0  # disabled schedule: settings changes wake us anyway
BUSY_RETRY_SECONDS = 30.0  # another run holds the lock: look again after this
ERROR_RETRY_SECONDS = 60.0

Clock = Callable[[], datetime]


class RunsScrapes(Protocol):
    def run(self, overrides: ScrapeOverrides | None = None, trigger: str = ...) -> object: ...


# --- pure scheduling rules ---------------------------------------------------


def next_run_at(
    schedule: ScheduleSettings, last_finished_at: datetime | None, now: datetime
) -> datetime | None:
    """last finished run (any trigger, not interrupted) + interval; due now if none."""
    if not schedule.enabled:
        return None
    if last_finished_at is None:
        return now
    return last_finished_at + timedelta(minutes=schedule.interval_minutes)


def due_trigger(
    schedule: ScheduleSettings,
    last_finished_at: datetime | None,
    now: datetime,
    *,
    startup: bool = False,
) -> str | None:
    """Trigger to run now ("startup" / "schedule"), or None."""
    upcoming = next_run_at(schedule, last_finished_at, now)
    overdue = upcoming is not None and upcoming <= now
    if startup:
        return "startup" if overdue or schedule.run_on_startup else None
    return "schedule" if overdue else None


def seconds_until_due(
    schedule: ScheduleSettings, last_finished_at: datetime | None, now: datetime
) -> float:
    upcoming = next_run_at(schedule, last_finished_at, now)
    if upcoming is None:
        return IDLE_WAIT_SECONDS
    return min(max((upcoming - now).total_seconds(), 0.0), IDLE_WAIT_SECONDS)


def last_finished_at(conn: sqlite3.Connection) -> datetime | None:
    run = repo.get_last_finished_run(conn)
    return normalize.parse_iso(str(run["finished_at"])) if run else None


def schedule_snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, object]:
    """The `schedule` block of GET /api/scrape/status."""
    schedule = repo.get_schedule_settings(conn)
    last = last_finished_at(conn)
    upcoming = next_run_at(schedule, last, now or normalize.utc_now())
    return {
        "enabled": schedule.enabled,
        "interval_minutes": schedule.interval_minutes,
        "run_on_startup": schedule.run_on_startup,
        "last_run_at": normalize.iso(last) if last else None,
        "next_run_at": normalize.iso(upcoming) if upcoming else None,
    }


# --- thread ------------------------------------------------------------------


class Scheduler:
    """Daemon thread inside the server: one catch-up/startup check STARTUP_DELAY
    after start, then runs whenever the schedule is due. `wake()` re-reads the
    settings immediately. Exceptions are logged; the thread never dies."""

    def __init__(
        self,
        service: RunsScrapes,
        *,
        clock: Clock = normalize.utc_now,
        startup_delay: float = STARTUP_DELAY_SECONDS,
        busy_retry: float = BUSY_RETRY_SECONDS,
        error_retry: float = ERROR_RETRY_SECONDS,
        db_path: Path | None = None,
    ) -> None:
        self._service = service
        self._clock = clock
        self._startup_delay = startup_delay
        self._busy_retry = busy_retry
        self._error_retry = error_retry
        self._db_path = db_path
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_started: datetime | None = None  # last run this thread started

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.is_alive:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scrape-scheduler", daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def request_stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def stop(self, timeout: float | None = 10.0) -> None:
        self.request_stop()
        self.join(timeout)

    def tick(self, *, startup: bool = False) -> float:
        """One scheduling decision (runs a due scrape synchronously). Returns the
        number of seconds to wait before the next decision."""
        with db.connection(self._db_path) as conn:
            schedule = repo.get_schedule_settings(conn)
            last = last_finished_at(conn)
        now = self._clock()
        trigger = due_trigger(schedule, last, now, startup=startup)
        if trigger is None:
            return seconds_until_due(schedule, last, now)
        guard = self._min_gap_remaining(now)
        if guard > 0:
            log.warning("Schedule due again too soon; waiting %.0fs", guard)
            return guard
        return self._run(trigger, now)

    def _min_gap_remaining(self, now: datetime) -> float:
        # Safety net: a run whose finish could not be recorded must not loop.
        if self._last_started is None:
            return 0.0
        gap = config.min_interval_minutes() * 60
        return max(0.0, gap - (now - self._last_started).total_seconds())

    def _run(self, trigger: str, now: datetime) -> float:
        log.info("Starting %s scrape", trigger)
        previous, self._last_started = self._last_started, now
        try:
            self._service.run(None, trigger)
        except ScrapeAlreadyRunning as exc:
            self._last_started = previous
            log.info("Scheduled scrape skipped: run %s is in progress", exc.run_id)
            return self._busy_retry
        except Exception:
            # run() only raises when the run could not start (execute() records
            # its own failures), so this attempt must not trip the min-gap guard.
            self._last_started = previous
            log.exception("Scheduled scrape could not run")
            return self._error_retry
        return 0.0

    def _loop(self) -> None:
        if self._stop.wait(self._startup_delay):
            return
        startup = True
        while not self._stop.is_set():
            self._wake.clear()
            try:
                wait = self.tick(startup=startup)
            except Exception:
                log.exception("Scheduler check failed")
                wait = self._error_retry
            startup = False
            if wait > 0:
                self._wake.wait(wait)
