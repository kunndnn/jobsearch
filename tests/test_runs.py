from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import PROJECT_ROOT, T0

from backend import repo
from backend.repo import ScrapeAlreadyRunning


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def status(conn: sqlite3.Connection, run_id: int) -> str:
    run = repo.get_run(conn, run_id)
    assert run is not None
    return str(run["status"])


def test_acquire_creates_running_row(conn: sqlite3.Connection) -> None:
    run_id = repo.acquire_run_lock(conn, "manual", {"search_terms": ["x"]}, 4, now=T0)
    run = repo.get_current_run(conn)
    assert run is not None and run["id"] == run_id
    assert run["status"] == "running" and run["trigger"] == "manual"
    assert run["total_queries"] == 4 and run["current_query"] == 0
    assert run["errors"] == {} and run["cancel_requested"] is False
    assert run["started_at"] == "2026-10-01T09:00:00.000+00:00"
    assert repo.get_run_params(conn, run_id) == {"search_terms": ["x"]}
    assert set(run) == {
        "id", "trigger", "status", "started_at", "finished_at", "message",
        "total_queries", "current_query", "jobs_found", "new_jobs", "updated_jobs",
        "errors", "cancel_requested",
    }  # fmt: skip


def test_refuses_while_fresh_and_alive(conn: sqlite3.Connection) -> None:
    first = repo.acquire_run_lock(conn, "schedule", now=T0)
    with pytest.raises(ScrapeAlreadyRunning) as excinfo:
        repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(minutes=4))
    assert excinfo.value.run_id == first
    assert status(conn, first) == "running"
    assert len(repo.list_runs(conn)) == 1


def test_heartbeat_keeps_lock(conn: sqlite3.Connection) -> None:
    first = repo.acquire_run_lock(conn, "manual", now=T0)
    repo.heartbeat(conn, first, now=T0 + timedelta(minutes=4))
    with pytest.raises(ScrapeAlreadyRunning):
        repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(minutes=8))


def test_stale_heartbeat_is_taken_over(conn: sqlite3.Connection) -> None:
    first = repo.acquire_run_lock(conn, "manual", now=T0)
    second = repo.acquire_run_lock(conn, "cli", now=T0 + timedelta(minutes=5))
    assert second != first
    assert status(conn, first) == "interrupted"
    assert status(conn, second) == "running"
    old = repo.get_run(conn, first)
    assert old is not None and old["finished_at"] == "2026-10-01T09:05:00.000+00:00"


def test_dead_pid_is_taken_over_even_with_fresh_heartbeat(conn: sqlite3.Connection) -> None:
    first = repo.acquire_run_lock(conn, "manual", now=T0, pid=dead_pid())
    second = repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(seconds=10))
    assert status(conn, first) == "interrupted"
    assert status(conn, second) == "running"


def test_other_host_relies_on_heartbeat_only(conn: sqlite3.Connection) -> None:
    first = repo.acquire_run_lock(conn, "manual", now=T0, pid=dead_pid(), host="other-box")
    with pytest.raises(ScrapeAlreadyRunning):
        repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(minutes=1))
    repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(minutes=6))
    assert status(conn, first) == "interrupted"


def test_only_one_running_row_at_db_level(conn: sqlite3.Connection) -> None:
    repo.acquire_run_lock(conn, "manual", now=T0)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO runs (trigger, status, started_at) VALUES ('cli', 'running', 'x')"
        )


def test_progress_cancel_and_finish(conn: sqlite3.Connection) -> None:
    run_id = repo.acquire_run_lock(conn, "manual", total_queries=3, now=T0)
    repo.update_run_progress(
        conn,
        run_id,
        current_query=2,
        message="Query 2/3",
        jobs_found=12,
        new_jobs=5,
        updated_jobs=7,
        errors={"naukri": "403 Forbidden"},
        now=T0 + timedelta(minutes=1),
    )
    run = repo.get_run(conn, run_id)
    assert run is not None
    assert (run["current_query"], run["jobs_found"], run["new_jobs"], run["updated_jobs"]) == (
        2,
        12,
        5,
        7,
    )
    assert run["message"] == "Query 2/3" and run["errors"] == {"naukri": "403 Forbidden"}
    repo.update_run_progress(conn, run_id, message="Query 3/3")
    assert repo.get_run(conn, run_id)["jobs_found"] == 12  # type: ignore[index]

    assert repo.is_cancel_requested(conn, run_id) is False
    assert repo.request_cancel(conn) == run_id
    assert repo.is_cancel_requested(conn, run_id) is True
    assert repo.get_current_run(conn)["cancel_requested"] is True  # type: ignore[index]

    repo.finish_run(conn, run_id, "cancelled", "Stopped by user", now=T0 + timedelta(minutes=2))
    run = repo.get_run(conn, run_id)
    assert run is not None and run["status"] == "cancelled"
    assert run["finished_at"] == "2026-10-01T09:02:00.000+00:00"
    assert run["errors"] == {"naukri": "403 Forbidden"}
    assert repo.get_current_run(conn) is None
    assert repo.request_cancel(conn) is None
    with pytest.raises(ValueError):
        repo.finish_run(conn, run_id, "running")


def test_history_and_last_finished(conn: sqlite3.Connection) -> None:
    ids = []
    for minute, final in ((0, "completed"), (10, "failed"), (20, "interrupted")):
        run_id = repo.acquire_run_lock(conn, "schedule", now=T0 + timedelta(minutes=minute))
        repo.finish_run(conn, run_id, final, now=T0 + timedelta(minutes=minute + 1))
        ids.append(run_id)
    assert [r["id"] for r in repo.list_runs(conn)] == list(reversed(ids))
    assert len(repo.list_runs(conn, limit=2)) == 2
    last = repo.get_last_finished_run(conn)
    assert last is not None and last["id"] == ids[1]  # interrupted is ignored


def test_mark_dead_runs_interrupted(conn: sqlite3.Connection) -> None:
    dead = repo.acquire_run_lock(conn, "manual", now=T0, pid=dead_pid())
    assert repo.mark_dead_runs_interrupted(conn) == 1
    assert status(conn, dead) == "interrupted"

    own = repo.acquire_run_lock(conn, "manual", now=T0 + timedelta(minutes=1))
    assert repo.mark_dead_runs_interrupted(conn) == 1  # this process cannot own a run yet
    assert status(conn, own) == "interrupted"

    remote = repo.acquire_run_lock(
        conn, "manual", now=T0 + timedelta(minutes=2), pid=dead_pid(), host="other-box"
    )
    assert repo.mark_dead_runs_interrupted(conn) == 0
    assert status(conn, remote) == "running"


def test_reap_own_run_only_when_no_worker_owns_it(conn: sqlite3.Connection) -> None:
    run_id = repo.acquire_run_lock(conn, "manual", now=T0)
    stale = T0 + timedelta(minutes=6)
    assert repo.reap_dead_runs(conn, now=stale) == 0  # workers unknown: kept
    assert repo.reap_dead_runs(conn, now=stale, active_run_ids=[run_id]) == 0  # slept
    fresh = T0 + timedelta(minutes=1)
    assert repo.reap_dead_runs(conn, now=fresh, active_run_ids=[]) == 0  # starting
    assert repo.reap_dead_runs(conn, now=stale, active_run_ids=[]) == 1
    assert status(conn, run_id) == "interrupted"


def test_pid_alive() -> None:
    assert repo.pid_alive(os.getpid())
    assert not repo.pid_alive(dead_pid())
    assert not repo.pid_alive(None)
    assert not repo.pid_alive(0)


_HOLDER = """
import sys, time
sys.path.insert(0, sys.argv[1])
from backend import db, repo
conn = db.connect()
db.migrate(conn)
run_id = repo.acquire_run_lock(conn, "cli")
print(run_id, flush=True)
time.sleep(60)
"""


def test_lock_across_processes(conn: sqlite3.Connection, data_dir: Path) -> None:
    env = {**os.environ, "JOBSPY_DATA_DIR": str(data_dir)}
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(PROJECT_ROOT)],
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert holder.stdout is not None
        held = int(holder.stdout.readline())
        with pytest.raises(ScrapeAlreadyRunning) as excinfo:
            repo.acquire_run_lock(conn, "manual")
        assert excinfo.value.run_id == held
    finally:
        holder.kill()
        holder.wait()
    deadline = time.monotonic() + 5
    while repo.pid_alive(holder.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    mine = repo.acquire_run_lock(conn, "manual")
    assert status(conn, held) == "interrupted"
    assert status(conn, mine) == "running"
