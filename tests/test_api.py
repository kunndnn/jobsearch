from __future__ import annotations

import shutil
import threading
import time
import warnings
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from conftest import LEGACY_JOBS, T0
from fakes import FakeBoards, save_settings

from backend import config, db, repo, scrape_service
from backend.server import create_app

with warnings.catch_warnings():  # starlette nags about httpx vs httpx2; both work
    warnings.filterwarnings("ignore", message="Using `httpx` with `starlette.testclient`")
    from fastapi.testclient import TestClient

RUN_KEYS = {
    "id", "trigger", "status", "started_at", "finished_at", "message", "total_queries",
    "current_query", "jobs_found", "new_jobs", "updated_jobs", "errors", "cancel_requested",
}  # fmt: skip
SUMMARY_KEYS = {
    "id", "site", "title", "company", "location", "job_url", "job_url_direct",
    "date_posted", "is_remote", "salary_display", "match_score", "matched_skills",
    "missing_skills", "backend_skills", "ai_skills", "role_track", "experience_required",
    "min_experience", "max_experience", "is_senior", "is_intern", "senior_reason",
    "first_seen_at", "last_seen_at", "is_new", "also_on", "app_status", "app_notes",
    "description_snippet",
}  # fmt: skip
DETAIL_KEYS = SUMMARY_KEYS | {
    "description", "job_type", "currency", "interval", "min_amount", "max_amount",
    "experience_range", "company_url",
}  # fmt: skip
STATS_KEYS = {
    "total_jobs", "total_stored", "high_match_count", "new_count", "applied_count",
    "saved_count", "interviewing_count", "rejected_count", "platform_distribution",
    "top_skills", "top_companies", "last_scrape_at",
}  # fmt: skip


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch) -> FakeBoards:
    fake = FakeBoards()
    monkeypatch.setattr(scrape_service, "scrape_jobs", fake)
    return fake


@pytest.fixture
def client(data_dir: Path) -> Iterator[TestClient]:
    with TestClient(create_app(scheduler_enabled=False, shutdown_timeout=0.5)) as test_client:
        yield test_client


@pytest.fixture
def legacy_client(data_dir: Path) -> Iterator[TestClient]:
    data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(LEGACY_JOBS, data_dir / "jobs.json")
    with TestClient(create_app(scheduler_enabled=False)) as test_client:
        yield test_client


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def wait_idle(client: TestClient) -> dict[str, object]:
    wait_for(lambda: not client.get("/api/scrape/status").json()["is_scraping"])
    return client.get("/api/scrape/status").json()


def dead_running_run() -> int:
    import subprocess
    import sys

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    with db.connection() as conn:
        return repo.acquire_run_lock(conn, "cli", pid=proc.pid)


# --- profile -----------------------------------------------------------------


def test_profile_round_trip_rescores(legacy_client: TestClient) -> None:
    profile = legacy_client.get("/api/profile").json()
    assert profile["name"] == "Kundan"

    profile["name"] = "  Test Person  "
    profile["skills"]["databases"] = ["SQLite", " sqlite ", ""]
    response = legacy_client.put("/api/profile", json=profile)
    body = response.json()

    assert response.status_code == 200
    assert body["success"] is True and body["rescored"] == 86
    assert body["profile"]["name"] == "Test Person"
    assert body["profile"]["skills"]["databases"] == ["SQLite"]
    assert legacy_client.get("/api/profile").json() == body["profile"]
    with db.connection() as conn:
        assert repo.get_profile_version(conn) == 2


def test_invalid_profile_is_422_and_not_saved(client: TestClient) -> None:
    profile = client.get("/api/profile").json()
    bad = {**profile, "name": "   "}
    response = client.put("/api/profile", json=bad)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], list)
    tracks = profile["target_tracks"]
    response = client.put("/api/profile", json={**profile, "target_tracks": [tracks[0], tracks[0]]})
    assert response.status_code == 422
    assert client.get("/api/profile").json() == profile


# --- settings ----------------------------------------------------------------


def test_settings_defaults_and_shape(client: TestClient) -> None:
    body = client.get("/api/settings").json()
    assert set(body) == {"scrape", "schedule", "available_sites", "limits"}
    assert body["scrape"]["sites"] == ["linkedin", "naukri"]
    assert body["scrape"]["results_per_query"] == 10 and body["scrape"]["hours_old"] == 72
    assert body["schedule"] == {"enabled": False, "interval_minutes": 360, "run_on_startup": False}
    assert body["available_sites"] == ["linkedin", "naukri", "indeed", "glassdoor"]
    assert body["limits"] == {"min_interval_minutes": 15, "warn_queries": 30}


def test_partial_settings_update_wakes_the_scheduler(data_dir: Path) -> None:
    app = create_app(scheduler_enabled=True, startup_delay=3600)
    wakes: list[int] = []
    app.state.scheduler.wake = lambda: wakes.append(1)
    with TestClient(app) as client:
        response = client.put("/api/settings", json={"scrape": {"results_per_query": 5}})
        assert response.status_code == 200
        body = response.json()
        assert body["scrape"]["results_per_query"] == 5
        assert body["scrape"]["hours_old"] == 72
        assert body["schedule"]["interval_minutes"] == 360
        assert wakes == [1]

        client.put("/api/settings", json={"schedule": {"enabled": True, "interval_minutes": 30}})
        assert client.get("/api/settings").json()["schedule"]["enabled"] is True
        assert len(wakes) == 2


@pytest.mark.parametrize(
    ("payload", "loc"),
    [
        ({"scrape": {"results_per_query": 5}, "schedule": {"interval_minutes": 1}},
         ["body", "schedule", "interval_minutes"]),
        ({"scrape": {"results_per_query": 0}}, ["body", "scrape", "results_per_query"]),
        ({"scrape": {"sites": ["monster"]}}, ["body", "scrape", "sites"]),
        ({"scrape": {"search_terms": [" ", ""]}}, ["body", "scrape", "search_terms"]),
        ({"scrape": {"delay_min_seconds": 9, "delay_max_seconds": 3}}, ["body", "scrape"]),
        ({"scrape": {"bogus": 1}}, ["body", "scrape", "bogus"]),
        ({"bogus": {}}, ["body", "bogus"]),
    ],
)  # fmt: skip
def test_invalid_settings_are_422_and_nothing_is_saved(
    client: TestClient, payload: dict[str, object], loc: list[str]
) -> None:
    before = client.get("/api/settings").json()
    response = client.put("/api/settings", json=payload)
    assert response.status_code == 422
    assert [issue["loc"] for issue in response.json()["detail"]][0][: len(loc)] == loc
    assert client.get("/api/settings").json() == before


def test_min_interval_can_be_lowered_by_env(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBSPY_MIN_INTERVAL_MINUTES", "1")
    response = client.put("/api/settings", json={"schedule": {"interval_minutes": 1}})
    assert response.status_code == 200
    assert response.json()["limits"]["min_interval_minutes"] == 1


# --- scraping ----------------------------------------------------------------


def test_scrape_runs_in_the_background(client: TestClient, boards: FakeBoards) -> None:
    save_settings(search_terms=["Backend Developer", "SDE 1"])
    response = client.post("/api/scrape", json={})
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    assert response.json() == {"success": True, "run_id": run_id}

    status = wait_idle(client)
    assert status["current_run"] is None
    assert set(status["schedule"]) == {
        "enabled", "interval_minutes", "run_on_startup", "last_run_at", "next_run_at",
    }  # fmt: skip
    run = status["history"][0]
    assert set(run) == RUN_KEYS
    assert (run["id"], run["status"], run["trigger"]) == (run_id, "completed", "manual")
    assert (run["new_jobs"], run["total_queries"], run["current_query"]) == (2, 2, 2)
    assert status["schedule"]["last_run_at"] == run["finished_at"]

    jobs = client.get("/api/jobs").json()
    assert jobs["total"] == 2 and all(job["is_new"] for job in jobs["jobs"])
    stats = client.get("/api/stats").json()
    assert stats["new_count"] == 2 and stats["last_scrape_at"] == run["finished_at"]


def test_scrape_without_a_body_uses_saved_settings(client: TestClient, boards: FakeBoards) -> None:
    save_settings(locations=["Pune"])
    assert client.post("/api/scrape").status_code == 200
    wait_idle(client)
    assert boards.calls[0]["location"] == "Pune"


def test_overrides_are_validated(client: TestClient, boards: FakeBoards) -> None:
    for body in ({"sites": ["monster"]}, {"results_per_query": 0}, {"locations": []},
                 {"bogus": True}, {"hours_old": 9999}):  # fmt: skip
        response = client.post("/api/scrape", json=body)
        assert response.status_code == 422, body
    assert client.get("/api/scrape/status").json()["history"] == []
    assert boards.calls == []


def test_conflict_while_running_then_stop(client: TestClient, boards: FakeBoards) -> None:
    save_settings(search_terms=["A", "B"])
    release = threading.Event()
    boards.before_call = lambda call: release.wait(5)
    run_id = client.post("/api/scrape", json={"locations": ["Pune"]}).json()["run_id"]
    wait_for(lambda: len(boards.calls) == 1)

    conflict = client.post("/api/scrape", json={})
    assert conflict.status_code == 409
    assert f"#{run_id}" in conflict.json()["detail"]
    status = client.get("/api/scrape/status").json()
    assert status["is_scraping"] is True and status["current_run"]["id"] == run_id

    assert client.post("/api/scrape/stop").json() == {"success": True}
    assert client.get("/api/scrape/status").json()["current_run"]["cancel_requested"] is True
    release.set()
    run = wait_idle(client)["history"][0]
    assert run["status"] == "cancelled" and run["current_query"] == 1
    assert run["message"].startswith("Cancelled from the dashboard after 1/2")
    assert client.post("/api/scrape/stop").status_code == 404


def test_server_shutdown_interrupts_a_stuck_run(data_dir: Path, boards: FakeBoards) -> None:
    release = threading.Event()
    boards.before_call = lambda call: release.wait(5)
    with TestClient(create_app(scheduler_enabled=False, shutdown_timeout=0.2)) as client:
        save_settings()
        run_id = client.post("/api/scrape").json()["run_id"]
        wait_for(lambda: len(boards.calls) == 1)
    release.set()
    with db.connection() as conn:
        run = repo.get_run(conn, run_id)
    assert run is not None and run["status"] == "interrupted"


def test_startup_marks_dead_runs_interrupted(data_dir: Path) -> None:
    db.init_db()
    run_id = dead_running_run()
    with TestClient(create_app(scheduler_enabled=False)) as client:
        status = client.get("/api/scrape/status").json()
    assert status["is_scraping"] is False
    assert status["history"][0]["id"] == run_id
    assert status["history"][0]["status"] == "interrupted"


def test_status_reaps_a_dead_cli_run(client: TestClient) -> None:
    run_id = dead_running_run()
    status = client.get("/api/scrape/status").json()
    assert status["is_scraping"] is False
    assert status["history"][0]["id"] == run_id
    assert status["history"][0]["status"] == "interrupted"
    assert client.post("/api/scrape/stop").status_code == 404


def test_status_reaps_an_orphaned_run_of_this_process(client: TestClient) -> None:
    with db.connection() as conn:  # its worker died before recording the finish
        run_id = repo.acquire_run_lock(conn, "manual", now=T0)
    status = client.get("/api/scrape/status").json()
    assert status["is_scraping"] is False
    assert status["history"][0]["id"] == run_id
    assert status["history"][0]["status"] == "interrupted"


def test_scheduler_catch_up_runs_once_after_startup(data_dir: Path, boards: FakeBoards) -> None:
    db.init_db()
    save_settings()
    with db.connection() as conn:
        repo.save_schedule_settings(conn, repo.get_schedule_settings(conn).model_copy(
            update={"enabled": True, "interval_minutes": 60}))  # fmt: skip
    with TestClient(create_app(startup_delay=0.05)) as client:
        wait_for(lambda: len(client.get("/api/scrape/status").json()["history"]) == 1)
        status = wait_idle(client)
        time.sleep(0.2)
        history = client.get("/api/scrape/status").json()["history"]
    assert [(run["trigger"], run["status"]) for run in history] == [("startup", "completed")]
    assert status["schedule"]["next_run_at"] > status["schedule"]["last_run_at"]


# --- jobs, applications, stats -----------------------------------------------


def test_job_listing_filters_and_validation(legacy_client: TestClient) -> None:
    body = legacy_client.get("/api/jobs").json()
    assert (body["total_unfiltered"], body["page"], body["page_size"]) == (86, 1, 50)
    assert len(body["jobs"]) == 50 and set(body["jobs"][0]) == SUMMARY_KEYS
    assert not any(job["is_new"] for job in body["jobs"])  # imported jobs are never new
    scores = [job["match_score"] for job in body["jobs"]]
    assert scores == sorted(scores, reverse=True)

    page2 = legacy_client.get("/api/jobs", params={"page": 2}).json()
    assert len(page2["jobs"]) == body["total"] - 50
    assert legacy_client.get("/api/jobs", params={"page_size": 500}).json()["page_size"] == 200
    assert legacy_client.get("/api/jobs", params={"sites": ""}).json()["total"] == 0
    naukri = legacy_client.get("/api/jobs", params={"sites": "naukri", "page_size": 200}).json()
    assert naukri["total"] > 0
    assert all(
        job["site"] == "naukri" or any(o["site"] == "naukri" for o in job["also_on"])
        for job in naukri["jobs"]
    )
    everything = legacy_client.get("/api/jobs", params={"exclude_senior": "false"}).json()
    assert everything["total"] >= body["total"]
    for params in ({"sort_by": "bogus"}, {"status": "maybe"}, {"page": 0}, {"min_score": -1}):
        assert legacy_client.get("/api/jobs", params=params).status_code == 422, params
    for sort in ("exp_asc", "exp_desc", "date", "salary", "found"):
        assert legacy_client.get("/api/jobs", params={"sort_by": sort}).status_code == 200


def test_job_detail_and_404(legacy_client: TestClient) -> None:
    job_id = legacy_client.get("/api/jobs").json()["jobs"][0]["id"]
    detail = legacy_client.get(f"/api/jobs/{job_id}").json()
    assert set(detail) == DETAIL_KEYS and detail["id"] == job_id
    missing = legacy_client.get("/api/jobs/li-does-not-exist")
    assert missing.status_code == 404 and "detail" in missing.json()


def test_application_status_flow(legacy_client: TestClient) -> None:
    job_id = legacy_client.get("/api/jobs").json()["jobs"][0]["id"]
    url = f"/api/jobs/{job_id}/status"
    response = legacy_client.post(url, json={"status": "saved", "notes": "call back"})
    assert response.json() == {"success": True, "job_id": job_id, "status": "saved"}
    detail = legacy_client.get(f"/api/jobs/{job_id}").json()
    assert (detail["app_status"], detail["app_notes"]) == ("saved", "call back")
    assert legacy_client.get("/api/jobs", params={"status": "saved"}).json()["total"] == 1
    assert legacy_client.get("/api/stats").json()["saved_count"] == 1

    legacy_client.post(url, json={"status": "applied"})  # notes kept
    assert legacy_client.get(f"/api/jobs/{job_id}").json()["app_notes"] == "call back"
    legacy_client.post(url, json={"status": "none"})
    assert legacy_client.get(f"/api/jobs/{job_id}").json()["app_status"] == "none"

    assert legacy_client.post(url, json={"status": "maybe"}).status_code == 422
    assert legacy_client.post("/api/jobs/nope/status", json={"status": "saved"}).status_code == 404


def test_stats_shape(legacy_client: TestClient) -> None:
    stats = legacy_client.get("/api/stats").json()
    assert set(stats) == STATS_KEYS
    assert stats["total_stored"] == 86 and stats["last_scrape_at"] is None
    assert all(set(item) == {"name", "count"} for item in stats["top_skills"])


# --- routing -----------------------------------------------------------------


def test_unknown_api_paths_are_json_404(client: TestClient) -> None:
    for method, path in [("GET", "/api/nope"), ("POST", "/api/nope/deeper"), ("GET", "/api"),
                         ("PUT", "/api/")]:  # fmt: skip
        response = client.request(method, path)
        assert response.status_code == 404, path
        assert response.json() == {"detail": "Not Found"}
    assert client.delete("/api/profile").status_code == 405


def test_spa_and_static_files(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>spa</title>")
    (dist / "assets" / "app-123.js").write_text("console.log(1)")
    (dist / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("nope")
    monkeypatch.setattr(config, "FRONTEND_DIST", dist)

    for path in ("/", "/jobs", "/automation/settings"):
        response = client.get(path)
        assert response.status_code == 200 and "spa" in response.text, path
    asset = client.get("/assets/app-123.js")
    assert asset.text == "console.log(1)" and "immutable" in asset.headers["cache-control"]
    assert client.get("/favicon.svg").text == "<svg/>"
    assert client.head("/").status_code == 200
    assert client.get("/assets/old-999.js").status_code == 404
    assert "nope" not in client.get("/..%2Fsecret.txt").text


def test_missing_frontend_build_shows_a_hint(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, "FRONTEND_DIST", tmp_path / "no-dist")
    response = client.get("/")
    assert response.status_code == 503 and "npm run build" in response.text
    assert client.get("/api/stats").status_code == 200
