from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta

import pytest
from conftest import T0

from backend import db, normalize, repo, scheduler
from backend.models import ScheduleSettings, ScrapeOverrides
from backend.repo import ScrapeAlreadyRunning
from backend.scheduler import (
    IDLE_WAIT_SECONDS,
    Scheduler,
    due_trigger,
    next_run_at,
    schedule_snapshot,
    seconds_until_due,
)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


class FakeService:
    """Records triggers; by default stores a finished run at the clock's time."""

    def __init__(self, clock: Callable[[], datetime] = normalize.utc_now) -> None:
        self.clock = clock
        self.triggers: list[str] = []
        self.error: BaseException | None = None
        self.record_finish = True
        self.lock = threading.Lock()

    def run(self, overrides: ScrapeOverrides | None = None, trigger: str = "manual") -> None:
        with self.lock:
            self.triggers.append(trigger)
        if self.error is not None:
            raise self.error
        if self.record_finish:
            finish_run_at(self.clock(), trigger)


def finish_run_at(moment: datetime, trigger: str = "manual", status: str = "completed") -> None:
    with db.connection() as conn:
        run_id = repo.acquire_run_lock(conn, trigger, now=moment)
        repo.finish_run(conn, run_id, status, now=moment)


def set_schedule(**values: object) -> ScheduleSettings:
    schedule = ScheduleSettings.model_validate(values)
    with db.connection() as conn:
        repo.save_schedule_settings(conn, schedule)
    return schedule


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


@pytest.fixture(autouse=True)
def database(data_dir: object) -> None:
    db.init_db()


# --- pure rules --------------------------------------------------------------


def test_next_run_at() -> None:
    on = ScheduleSettings(enabled=True, interval_minutes=60)
    assert next_run_at(ScheduleSettings(enabled=False), T0, T0) is None
    assert next_run_at(on, None, T0) == T0  # never ran: due now
    assert next_run_at(on, T0, T0 + timedelta(minutes=5)) == T0 + timedelta(minutes=60)


@pytest.mark.parametrize(
    ("enabled", "on_startup", "last_minutes_ago", "startup", "expected"),
    [
        (True, False, None, True, "startup"),  # never ran: overdue at startup
        (True, False, 90, True, "startup"),  # overdue catch-up
        (True, False, 10, True, None),  # not due yet
        (True, True, 10, True, "startup"),  # run_on_startup forces it
        (False, True, 10, True, "startup"),  # ... even with the schedule off
        (False, False, None, True, None),
        (True, False, 90, False, "schedule"),
        (True, False, 60, False, "schedule"),  # exactly due
        (True, False, 59, False, None),
        (False, False, 999, False, None),
        (True, True, 10, False, None),  # run_on_startup only matters at startup
    ],
)
def test_due_trigger(
    enabled: bool, on_startup: bool, last_minutes_ago: int | None, startup: bool, expected: str | None
) -> None:
    schedule = ScheduleSettings(enabled=enabled, interval_minutes=60, run_on_startup=on_startup)
    last = None if last_minutes_ago is None else T0 - timedelta(minutes=last_minutes_ago)
    assert due_trigger(schedule, last, T0, startup=startup) == expected


def test_seconds_until_due() -> None:
    on = ScheduleSettings(enabled=True, interval_minutes=60)
    assert seconds_until_due(on, T0 - timedelta(minutes=50), T0) == 600
    assert seconds_until_due(on, T0 - timedelta(hours=3), T0) == 0
    assert seconds_until_due(ScheduleSettings(enabled=False), None, T0) == IDLE_WAIT_SECONDS
    week = ScheduleSettings(enabled=True, interval_minutes=10080)
    assert seconds_until_due(week, T0, T0) == IDLE_WAIT_SECONDS  # re-checked hourly


def test_schedule_snapshot_uses_last_finished_non_interrupted_run() -> None:
    with db.connection() as conn:
        assert schedule_snapshot(conn, T0) == {
            "enabled": False,
            "interval_minutes": 360,
            "run_on_startup": False,
            "last_run_at": None,
            "next_run_at": None,
        }
    set_schedule(enabled=True, interval_minutes=120)
    finish_run_at(T0 - timedelta(hours=1), "manual", "partial")
    finish_run_at(T0 - timedelta(minutes=10), "cli", "interrupted")
    with db.connection() as conn:
        snap = schedule_snapshot(conn, T0)
    assert snap["last_run_at"] == "2026-10-01T08:00:00.000+00:00"
    assert snap["next_run_at"] == "2026-10-01T10:00:00.000+00:00"


# --- tick (injected clock) ---------------------------------------------------


def make(clock: FakeClock, service: FakeService | None = None, **kwargs: float) -> Scheduler:
    return Scheduler(service or FakeService(clock), clock=clock, **kwargs)


def test_startup_tick_catches_up_once_then_waits_an_interval() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    set_schedule(enabled=True, interval_minutes=30)
    sched = make(clock, service)

    assert sched.tick(startup=True) == 0.0
    assert service.triggers == ["startup"]
    assert sched.tick() == 1800
    clock.advance(minutes=29)
    assert sched.tick() == 60
    clock.advance(minutes=1)
    assert sched.tick() == 0.0
    assert service.triggers == ["startup", "schedule"]


def test_startup_tick_does_nothing_when_not_due() -> None:
    clock = FakeClock(T0)
    finish_run_at(T0 - timedelta(minutes=10))
    set_schedule(enabled=True, interval_minutes=60)
    sched = make(clock)
    assert sched.tick(startup=True) == 50 * 60
    assert sched._service.triggers == []  # type: ignore[attr-defined]


def test_run_on_startup_runs_even_when_disabled() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    finish_run_at(T0 - timedelta(minutes=10))
    set_schedule(enabled=False, run_on_startup=True)
    sched = make(clock, service)
    sched.tick(startup=True)
    assert service.triggers == ["startup"]
    assert sched.tick() == IDLE_WAIT_SECONDS


def test_next_run_follows_any_trigger() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    set_schedule(enabled=True, interval_minutes=60)
    finish_run_at(T0 - timedelta(minutes=50), "cli")  # a terminal run resets the timer
    assert make(clock, service).tick() == 600
    assert service.triggers == []


def test_busy_lock_skips_and_retries_later() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    service.error = ScrapeAlreadyRunning(7)
    set_schedule(enabled=True)
    sched = make(clock, service, busy_retry=12.0)
    assert sched.tick() == 12.0
    service.error = None
    assert sched.tick() == 0.0  # the skip does not trip the min-gap guard
    assert service.triggers == ["schedule", "schedule"]


def test_failing_run_waits_error_retry() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    service.error = RuntimeError("boom")
    set_schedule(enabled=True)
    assert make(clock, service, error_retry=33.0).tick() == 33.0


def test_run_that_could_not_start_retries_after_error_retry() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    service.error = RuntimeError("database is locked")  # prepare() failed: no run
    set_schedule(enabled=True)
    sched = make(clock, service, error_retry=60.0)
    assert sched.tick() == 60.0
    clock.advance(seconds=60)
    service.error = None
    assert sched.tick() == 0.0  # not held back by the min-gap guard
    assert service.triggers == ["schedule", "schedule"]


def test_unrecorded_finish_cannot_cause_a_run_storm() -> None:
    clock = FakeClock(T0)
    service = FakeService(clock)
    service.record_finish = False
    set_schedule(enabled=True)
    sched = make(clock, service)
    assert sched.tick() == 0.0
    assert sched.tick() > 14 * 60  # guarded by the minimum interval
    assert service.triggers == ["schedule"]


# --- the thread --------------------------------------------------------------


def test_thread_runs_startup_once_and_stops_cleanly() -> None:
    service = FakeService()
    set_schedule(enabled=True, interval_minutes=60)
    sched = Scheduler(service, startup_delay=0.05)
    sched.start()
    try:
        wait_for(lambda: service.triggers == ["startup"])
        time.sleep(0.2)
        assert service.triggers == ["startup"]
    finally:
        sched.stop(timeout=2)
    assert not sched.is_alive


def test_wake_applies_new_settings_immediately() -> None:
    service = FakeService()
    sched = Scheduler(service, startup_delay=0.01)
    sched.start()
    try:
        time.sleep(0.1)  # startup check: schedule is off, nothing runs
        assert service.triggers == []
        set_schedule(enabled=True, interval_minutes=60)
        sched.wake()
        wait_for(lambda: service.triggers == ["schedule"], timeout=2)
    finally:
        sched.stop(timeout=2)


def test_thread_survives_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    service = FakeService()
    set_schedule(enabled=True)
    real = repo.get_schedule_settings
    failures = {"left": 2}

    def flaky(conn: object) -> ScheduleSettings:
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("database is locked")
        return real(conn)  # type: ignore[arg-type]

    monkeypatch.setattr(scheduler.repo, "get_schedule_settings", flaky)
    sched = Scheduler(service, startup_delay=0.0, error_retry=0.05)
    sched.start()
    try:
        wait_for(lambda: service.triggers == ["schedule"])
        assert sched.is_alive
        service.error = RuntimeError("scrape exploded")
        set_schedule(enabled=True, run_on_startup=False)
        sched.wake()
        time.sleep(0.1)
        assert sched.is_alive
    finally:
        sched.stop(timeout=2)


def test_stop_during_startup_delay_is_prompt() -> None:
    service = FakeService()
    set_schedule(enabled=True)
    sched = Scheduler(service, startup_delay=30)
    sched.start()
    started = time.monotonic()
    sched.stop(timeout=2)
    assert time.monotonic() - started < 1
    assert not sched.is_alive and service.triggers == []
