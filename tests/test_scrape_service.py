from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import timedelta

import pytest
from conftest import make_job
from fakes import FakeBoards, job_row, save_settings
from pydantic import ValidationError

from backend import db, normalize, repo, scrape_service
from backend.models import ScrapeOverrides, ScrapeSettings
from backend.repo import ScrapeAlreadyRunning
from backend.scrape_service import (
    BoardCall,
    ScrapeService,
    board_calls,
    indeed_search_term,
    plan_queries,
)


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch) -> FakeBoards:
    fake = FakeBoards()
    monkeypatch.setattr(scrape_service, "scrape_jobs", fake)
    return fake


@pytest.fixture
def service(data_dir: object) -> ScrapeService:
    db.init_db()
    return ScrapeService(cancel_poll=0.02, heartbeat_interval=0.05)


def run_row(run_id: int) -> dict[str, object]:
    with db.connection() as conn:
        run = repo.get_run(conn, run_id)
    assert run is not None
    return run


def job_row_in_db(job_id: str) -> dict[str, object]:
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert row is not None
    return dict(row)


def heartbeat_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith("scrape-heartbeat")]


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


# --- planning ----------------------------------------------------------------


def test_plan_is_locations_by_terms() -> None:
    settings = ScrapeSettings(search_terms=["A", "B"], locations=["X", "Y"])
    plan = [(q.index, q.term, q.location) for q in plan_queries(settings)]
    assert plan == [(1, "A", "X"), (2, "B", "X"), (3, "A", "Y"), (4, "B", "Y")]


def test_indeed_gets_its_own_call_with_negative_keywords() -> None:
    settings = ScrapeSettings(
        sites=["linkedin", "indeed", "naukri"], exclude_terms=["senior", "team lead"]
    )
    assert board_calls("Node.js Developer", settings) == [
        BoardCall(("linkedin", "naukri"), "Node.js Developer"),
        BoardCall(("indeed",), '"Node.js Developer" -senior -"team lead"'),
    ]
    only_indeed = ScrapeSettings(sites=["indeed"], exclude_terms=[])
    assert board_calls("SDE 1", only_indeed) == [BoardCall(("indeed",), '"SDE 1"')]
    assert indeed_search_term('Say "hi"', ["sr"]) == '"Say hi" -sr'


# --- a full run --------------------------------------------------------------


def test_run_scrapes_every_query_and_stores_jobs(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(
        search_terms=["Backend Developer", "SDE 1"],
        locations=["Bengaluru", "Pune"],
        sites=["linkedin", "naukri"],
        results_per_query=7,
        hours_old=48,
    )
    result = service.run(trigger="manual")

    assert result.status == "completed"
    assert (result.total_queries, result.queries_done) == (4, 4)
    assert (result.jobs_found, result.new_jobs, result.updated_jobs) == (8, 8, 0)
    assert [(loc, term) for _, term, loc in boards.searched()] == [
        ("Bengaluru", "Backend Developer"),
        ("Bengaluru", "SDE 1"),
        ("Pune", "Backend Developer"),
        ("Pune", "SDE 1"),
    ]
    first = boards.calls[0]
    assert first["site_name"] == ["linkedin", "naukri"]
    assert first["results_wanted"] == 7 and first["hours_old"] == 48
    assert first["fetch_description"] is True and first["country_indeed"] == "india"
    assert first["verbose"] == 0

    run = run_row(result.run_id)
    assert run["status"] == "completed" and run["trigger"] == "manual"
    assert (run["current_query"], run["total_queries"]) == (4, 4)
    assert (run["jobs_found"], run["new_jobs"], run["updated_jobs"]) == (8, 8, 0)
    assert run["errors"] == {} and run["finished_at"]
    assert str(run["message"]).startswith("Completed: 8 found, 8 new")

    stored = job_row_in_db("li-backend-developer-bengaluru")
    assert "Node.js" in stored["description"]
    assert stored["first_seen_run_id"] == result.run_id
    assert stored["min_amount"] is None  # NaN from the frame became NULL
    with db.connection() as conn:
        detail = repo.get_job(conn, "nk-sde-1-pune")
    assert detail is not None and detail["company_url"] == "https://naukri-sde-1-labs.example"
    assert heartbeat_threads() == []


def test_second_run_updates_and_keeps_first_seen(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings()
    first = service.run()
    before = job_row_in_db("li-backend-developer-bengaluru")
    time.sleep(0.01)
    second = service.run()

    assert (second.new_jobs, second.updated_jobs) == (0, 1)
    after = job_row_in_db("li-backend-developer-bengaluru")
    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["last_seen_at"] > before["last_seen_at"]
    assert (after["first_seen_run_id"], after["last_seen_run_id"]) == (first.run_id, second.run_id)


def test_cross_board_copy_is_linked_not_new(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(sites=["linkedin", "naukri"])
    boards.rows = lambda site, call: [
        job_row(site, f"{site}-1", "Backend Developer", "Acme Pvt Ltd", "Bangalore, India")
    ]
    result = service.run()

    assert (result.new_jobs, result.duplicates) == (1, 1)
    with db.connection() as conn:
        listing = repo.list_jobs(conn)
    assert listing["total"] == 1
    assert listing["jobs"][0]["also_on"] == [
        {"site": "naukri", "job_url": "https://naukri.example/jobs/naukri-1"}
    ]


# --- errors and statuses -----------------------------------------------------


def test_board_error_makes_the_run_partial(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(locations=["Bengaluru", "Pune"], sites=["linkedin", "naukri"])
    boards.errors = {"naukri": "403 Forbidden"}
    result = service.run()

    assert result.status == "partial"
    assert result.errors == {"naukri": "403 Forbidden (failed in 2 of 2 queries)"}
    assert result.new_jobs == 2
    run = run_row(result.run_id)
    assert run["status"] == "partial" and run["errors"] == result.errors
    assert "errors on naukri" in str(run["message"])


def test_every_board_failing_without_results_fails(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(sites=["linkedin", "indeed"])
    boards.errors = {"linkedin": "429 Too Many Requests", "indeed": "blocked"}
    with db.connection() as conn:
        repo.upsert_job(conn, make_job(id="li-old"), repo.get_profile(conn), 1,
                        now=normalize.utc_now() - timedelta(days=90))  # fmt: skip
    result = service.run()

    assert result.status == "failed"
    assert set(result.errors) == {"linkedin", "indeed"}
    assert str(run_row(result.run_id)["message"]).startswith("Failed")
    with db.connection() as conn:
        assert repo.job_exists(conn, "li-old")  # a failed run never prunes


def test_exception_in_one_query_is_recorded_and_the_run_continues(
    boards: FakeBoards, service: ScrapeService
) -> None:
    save_settings(locations=["Bengaluru", "Pune"])

    def explode_once(call: dict[str, object]) -> None:
        if call["location"] == "Bengaluru":
            raise RuntimeError("connection reset")

    boards.before_call = explode_once
    result = service.run()

    assert result.status == "partial"
    assert result.queries_done == 2 and result.new_jobs == 1
    assert "RuntimeError: connection reset" in result.errors["linkedin"]
    assert "failed in 1 of 2 queries" in result.errors["linkedin"]


def test_store_failure_is_recorded_for_the_query(
    boards: FakeBoards, service: ScrapeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings()

    def broken(*args: object, **kwargs: object) -> object:
        raise ValueError("disk full")

    monkeypatch.setattr(repo, "upsert_jobs", broken)
    result = service.run()
    assert result.status == "partial"
    assert result.errors == {"linkedin": "Saving results failed: disk full"}


def test_busy_database_during_progress_and_prune_does_not_fail_the_run(
    boards: FakeBoards, service: ScrapeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings(locations=["Bengaluru", "Pune"])

    def locked(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(repo, "update_run_progress", locked)
    monkeypatch.setattr(repo, "prune_jobs", locked)
    result = service.run()

    assert result.status == "completed" and result.new_jobs == 2
    assert run_row(result.run_id)["status"] == "completed"


def test_unexpected_error_fails_the_run_and_stops_the_heartbeat(
    boards: FakeBoards, service: ScrapeService, monkeypatch: pytest.MonkeyPatch
) -> None:
    save_settings()

    def broken(*args: object, **kwargs: object) -> int:
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(repo, "prune_jobs", broken)
    result = service.run()

    assert result.status == "failed"
    run = run_row(result.run_id)
    assert run["status"] == "failed" and "prune exploded" in str(run["message"])
    assert heartbeat_threads() == []
    assert service.active_run_ids() == []


def test_keyboard_interrupt_marks_the_run_cancelled(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings()

    def ctrl_c(call: dict[str, object]) -> None:
        raise KeyboardInterrupt

    boards.before_call = ctrl_c
    prepared = service.prepare(None, "cli")
    with pytest.raises(KeyboardInterrupt):
        service.execute(prepared)
    run = run_row(prepared.run_id)
    assert run["status"] == "cancelled" and run["finished_at"]
    assert heartbeat_threads() == []


# --- locking -----------------------------------------------------------------


def test_second_run_is_refused_while_one_is_running(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings()
    with db.connection() as conn:
        holder = repo.acquire_run_lock(conn, "cli")
    with pytest.raises(ScrapeAlreadyRunning) as excinfo:
        service.run()
    assert excinfo.value.run_id == holder
    assert boards.calls == []


def test_invalid_overrides_fail_before_locking(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings()
    with pytest.raises(ValidationError):
        service.run(ScrapeOverrides(sites=["monster"]))
    with db.connection() as conn:
        assert repo.list_runs(conn) == []


def test_overrides_apply_to_this_run_only(boards: FakeBoards, service: ScrapeService) -> None:
    stored = save_settings()
    result = service.run(ScrapeOverrides(locations=["Pune"], fetch_description=False), "cli")

    assert boards.calls[0]["location"] == "Pune"
    assert boards.calls[0]["fetch_description"] is False
    with db.connection() as conn:
        assert repo.get_scrape_settings(conn) == stored
        params = repo.get_run_params(conn, result.run_id)
    assert params["locations"] == ["Pune"]
    assert params["overrides"] == {"locations": ["Pune"], "fetch_description": False}


# --- cancellation and delays -------------------------------------------------


def test_cancel_stops_before_the_next_query(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(search_terms=["A", "B", "C"])
    boards.before_call = lambda call: service.cancel() if len(boards.calls) == 1 else None
    result = service.run()

    assert result.status == "cancelled"
    assert result.queries_done == 1 and len(boards.calls) == 1
    run = run_row(result.run_id)
    assert run["status"] == "cancelled" and run["cancel_requested"] is True
    assert str(run["message"]).startswith("Cancelled after 1/3 queries")


def test_cancel_during_the_last_query_ends_cancelled(
    boards: FakeBoards, service: ScrapeService
) -> None:
    save_settings(search_terms=["A"])
    boards.before_call = lambda call: service.cancel("Cancelled from the dashboard")
    result = service.run()

    assert result.status == "cancelled" and result.queries_done == 1
    assert result.new_jobs > 0  # what the query found is still stored
    run = run_row(result.run_id)
    assert run["status"] == "cancelled"
    assert str(run["message"]).startswith("Cancelled from the dashboard after 1/1 queries")


def test_cancel_from_another_process_interrupts_the_delay(
    boards: FakeBoards, service: ScrapeService
) -> None:
    save_settings(search_terms=["A", "B"], delay_min_seconds=30, delay_max_seconds=30)
    run_id = service.start()
    wait_for(lambda: str(run_row(run_id)["message"]).startswith("Waiting 30s"))

    started = time.monotonic()
    with db.connection() as conn:  # the DB flag only, as the API of another process would
        assert repo.request_cancel(conn) == run_id
    assert service.wait(run_id, timeout=5)

    assert time.monotonic() - started < 2
    run = run_row(run_id)
    assert run["status"] == "cancelled" and run["current_query"] == 1
    assert len(boards.calls) == 1


def test_in_process_cancel_wakes_the_delay_at_once(boards: FakeBoards, data_dir: object) -> None:
    db.init_db()
    slow_poll = ScrapeService(cancel_poll=60)
    save_settings(search_terms=["A", "B"], delay_min_seconds=60, delay_max_seconds=60)
    run_id = slow_poll.start()
    wait_for(lambda: str(run_row(run_id)["message"]).startswith("Waiting"))

    slow_poll.notify_cancel(run_id, "Cancelled from the dashboard")
    assert slow_poll.wait(run_id, timeout=2)
    run = run_row(run_id)
    assert run["status"] == "cancelled"
    assert str(run["message"]).startswith("Cancelled from the dashboard after 1/2")


def test_heartbeat_runs_during_a_query(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings()
    beats: list[str] = []

    def slow(call: dict[str, object]) -> None:
        with db.connection() as conn:
            run = conn.execute("SELECT heartbeat_at FROM runs WHERE status = 'running'").fetchone()
            beats.append(run["heartbeat_at"])
            time.sleep(0.25)
            run = conn.execute("SELECT heartbeat_at FROM runs WHERE status = 'running'").fetchone()
            beats.append(run["heartbeat_at"])

    boards.before_call = slow
    service.run()
    assert beats[1] > beats[0]
    assert heartbeat_threads() == []


def test_shutdown_interrupts_a_run_stuck_in_a_board_request(
    boards: FakeBoards, service: ScrapeService
) -> None:
    save_settings()
    release = threading.Event()
    boards.before_call = lambda call: release.wait(5)
    run_id = service.start()
    wait_for(lambda: len(boards.calls) == 1)

    service.shutdown(timeout=0.1)
    assert run_row(run_id)["status"] == "interrupted"
    release.set()
    assert service.wait(run_id, timeout=5)
    assert run_row(run_id)["status"] == "interrupted"  # the late finish does not overwrite
    wait_for(lambda: heartbeat_threads() == [])


def test_prune_after_a_successful_run(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(retention_days=30)
    with db.connection() as conn:
        repo.upsert_job(conn, make_job(id="li-old", title="Old Job", company="Gone"),
                        repo.get_profile(conn), 1,
                        now=normalize.utc_now() - timedelta(days=45))  # fmt: skip
    result = service.run()
    assert result.pruned == 1
    with db.connection() as conn:
        assert not repo.job_exists(conn, "li-old")
    assert "pruned 1 old job" in result.message


def test_progress_events(boards: FakeBoards, service: ScrapeService) -> None:
    save_settings(search_terms=["A", "B"], delay_min_seconds=0, delay_max_seconds=0)
    events = []
    service.run(progress=events.append)
    assert [(e.kind, e.index, e.total) for e in events] == [
        ("query_started", 1, 2),
        ("query_finished", 1, 2),
        ("query_started", 2, 2),
        ("query_finished", 2, 2),
    ]
    assert events[1].found == 1 and events[1].new == 1
