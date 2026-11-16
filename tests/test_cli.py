from __future__ import annotations

import _thread
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import PROJECT_ROOT
from fakes import FakeBoards, save_settings

import scrape
import start
from backend import db, repo, scrape_service


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch, data_dir: Path) -> FakeBoards:
    fake = FakeBoards()
    monkeypatch.setattr(scrape_service, "scrape_jobs", fake)
    db.init_db()
    save_settings(search_terms=["Backend Developer", "SDE 1"])
    return fake


def last_run() -> dict[str, object]:
    with db.connection() as conn:
        runs = repo.list_runs(conn, 1)
    assert runs
    return runs[0]


def run_script(script: str, *args: str, cwd: Path, data_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "JOBSPY_DATA_DIR": str(data_dir)}
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / script), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_run_with_overrides(boards: FakeBoards, capsys: pytest.CaptureFixture[str]) -> None:
    code = scrape.main(
        ["--terms", "Node.js Developer, SDE 1", "--locations", "Pune", "--sites", "LinkedIn",
         "--results", "3", "--hours-old", "24", "--no-descriptions"]
    )  # fmt: skip
    out = capsys.readouterr().out

    assert code == 0
    assert [(call["search_term"], call["location"]) for call in boards.calls] == [
        ("Node.js Developer", "Pune"),
        ("SDE 1", "Pune"),
    ]
    call = boards.calls[0]
    assert (call["site_name"], call["results_wanted"], call["hours_old"]) == (["linkedin"], 3, 24)
    assert call["fetch_description"] is False
    assert "[1/2] Node.js Developer @ Pune ..." in out
    assert "Run #1: Completed: 2 found, 2 new" in out and "New: 2" in out
    assert "2 queries (2 terms x 1 location) on linkedin" in out
    assert "Next scheduled run: automation is off" in out
    run = last_run()
    assert (run["trigger"], run["status"]) == ("cli", "completed")
    with db.connection() as conn:  # overrides never touch the saved settings
        assert repo.get_scrape_settings(conn).search_terms == ["Backend Developer", "SDE 1"]


def test_quiet_prints_only_the_summary(boards: FakeBoards, capsys: pytest.CaptureFixture[str]) -> None:
    assert scrape.main(["--quiet"]) == 0
    out = capsys.readouterr().out
    assert "[1/2]" not in out and "Run #1: Completed" in out


def test_partial_run_exits_0_and_failed_run_exits_1(
    boards: FakeBoards, capsys: pytest.CaptureFixture[str]
) -> None:
    save_settings(sites=["linkedin", "naukri"])
    boards.errors = {"naukri": "403 Forbidden"}
    assert scrape.main([]) == 0
    assert "naukri error: 403 Forbidden" in capsys.readouterr().out

    boards.errors = {"naukri": "403 Forbidden", "linkedin": "429 Too Many Requests"}
    assert scrape.main([]) == 1
    assert last_run()["status"] == "failed"


def test_lock_held_exits_2(boards: FakeBoards, capsys: pytest.CaptureFixture[str]) -> None:
    with db.connection() as conn:
        holder = repo.acquire_run_lock(conn, "manual")
    assert scrape.main([]) == 2
    assert f"run #{holder}" in capsys.readouterr().err
    assert boards.calls == []


@pytest.mark.parametrize("args", [["--sites", "monster"], ["--results", "0"], ["--terms", " , "]])
def test_invalid_overrides_exit_1(boards: FakeBoards, args: list[str]) -> None:
    assert scrape.main(args) == 1
    assert boards.calls == []


def test_usage_errors_exit_1_not_2(boards: FakeBoards) -> None:
    with pytest.raises(SystemExit) as excinfo:
        scrape.main(["--results", "lots"])
    assert excinfo.value.code == 1


def test_ctrl_c_cancels_the_run_and_exits_130(boards: FakeBoards) -> None:
    def ctrl_c_mid_query(call: dict[str, object]) -> None:
        _thread.interrupt_main()
        time.sleep(0.3)  # the board request is still "in flight"

    boards.before_call = ctrl_c_mid_query
    assert scrape.main(["--quiet"]) == 130
    run = last_run()
    assert run["status"] == "cancelled"
    assert str(run["message"]).startswith("Cancelled from the terminal (Ctrl+C) after 1/2")
    assert len(boards.calls) == 1


def test_ctrl_c_during_a_stuck_request_still_records_the_cancel(
    boards: FakeBoards, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scrape, "CTRL_C_GRACE_SECONDS", 0.1)
    release = threading.Event()

    def stuck(call: dict[str, object]) -> None:
        _thread.interrupt_main()
        release.wait(5)

    boards.before_call = stuck
    try:
        assert scrape.main(["--quiet"]) == 130
        run = last_run()
        assert (run["status"], run["message"]) == ("cancelled", scrape.CTRL_C_MESSAGE)
    finally:
        release.set()
    time.sleep(0.2)
    assert last_run()["status"] == "cancelled"


def test_stop_from_the_dashboard_exits_130(boards: FakeBoards) -> None:
    def stop_button(call: dict[str, object]) -> None:
        with db.connection() as conn:
            repo.request_cancel(conn)

    boards.before_call = stop_button
    assert scrape.main(["--quiet"]) == 130
    assert last_run()["status"] == "cancelled"


# --- real processes (no network: these exit before any board is contacted) ----


def test_scripts_work_from_any_directory(tmp_path: Path, data_dir: Path) -> None:
    for script in ("scrape.py", "start.py"):
        result = run_script(script, "--help", cwd=tmp_path, data_dir=data_dir)
        assert result.returncode == 0, result.stderr
    assert "--no-descriptions" in run_script("scrape.py", "--help", cwd=tmp_path, data_dir=data_dir).stdout


def test_cli_process_exit_codes_without_network(tmp_path: Path, data_dir: Path) -> None:
    bad = run_script("scrape.py", "--sites", "monster", cwd=tmp_path, data_dir=data_dir)
    assert bad.returncode == 1 and "unknown site" in bad.stderr

    db.init_db()
    with db.connection() as conn:  # held by this (live) test process
        repo.acquire_run_lock(conn, "manual")
    locked = run_script("scrape.py", "--quiet", cwd=tmp_path, data_dir=data_dir)
    assert locked.returncode == 2, locked.stderr
    assert "already running" in locked.stderr


# --- start.py helpers --------------------------------------------------------


def test_start_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    assert start.browser_url("0.0.0.0", 8770) == "http://127.0.0.1:8770"
    assert start.browser_url("::1", 8770) == "http://[::1]:8770"
    monkeypatch.setenv("PORT", "9123")
    assert start.parse_args([]).port == 9123
    assert start.parse_args(["--port", "8771", "--no-browser"]).no_browser is True
    off = {"enabled": False, "run_on_startup": False}
    assert start.schedule_line(off, start.normalize.utc_now()).startswith("off")
    overdue = {"enabled": True, "interval_minutes": 60, "next_run_at": "2020-01-01T00:00:00+00:00"}
    assert "overdue" in start.schedule_line(overdue, start.normalize.utc_now())


def test_start_detects_runs_stuck_after_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    service = SimpleNamespace(active_run_ids=lambda: [7])
    server = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(service=service)))
    monkeypatch.setitem(sys.modules, "backend.server", server)
    assert start.stuck_runs() == [7]
    service.active_run_ids = lambda: []
    assert start.stuck_runs() == []
    monkeypatch.delitem(sys.modules, "backend.server")
    assert start.stuck_runs() == []
