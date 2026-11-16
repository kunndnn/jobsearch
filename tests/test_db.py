from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import LEGACY_JOBS, T0, make_job

from backend import config, db, matcher, repo
from backend.repo import JobFilters


def strict_loads(text: str) -> object:
    def reject(constant: str) -> object:
        raise ValueError(f"non-strict JSON constant {constant}")

    return json.loads(text, parse_constant=reject)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def legacy_copy(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "jobs.json"
    shutil.copyfile(LEGACY_JOBS, target)
    return target


def test_config_resolves_data_dir_lazily(data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert config.data_dir() == data_dir.resolve()
    assert config.db_path() == data_dir.resolve() / "jobspy.db"
    monkeypatch.delenv("JOBSPY_DATA_DIR")
    assert config.data_dir() == config.PROJECT_ROOT / "data"
    monkeypatch.setenv("JOBSPY_MIN_INTERVAL_MINUTES", "garbage")
    assert config.min_interval_minutes() == 15


def test_connection_pragmas(data_dir: Path) -> None:
    with db.connection() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
        assert isinstance(conn.execute("SELECT 1 AS x").fetchone(), sqlite3.Row)
    assert (data_dir / "jobspy.db").exists()


def test_migrate_is_idempotent(data_dir: Path) -> None:
    with db.connection() as conn:
        assert db.migrate(conn) == db.SCHEMA_VERSION
        assert db.migrate(conn) == db.SCHEMA_VERSION
        assert db.schema_version(conn) == db.SCHEMA_VERSION
        tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"jobs", "applications", "runs", "kv"} <= tables


def test_transaction_rolls_back(data_dir: Path) -> None:
    with db.connection() as conn:
        db.migrate(conn)
        with pytest.raises(RuntimeError), db.transaction(conn):
            repo.kv_set(conn, "k", 1)
            raise RuntimeError("boom")
        assert repo.kv_get(conn, "k") is None
        with db.transaction(conn):
            with db.transaction(conn):  # nested joins the outer transaction
                repo.kv_set(conn, "k", 2)
        assert repo.kv_get(conn, "k") == 2


def test_init_db_without_legacy_files(data_dir: Path) -> None:
    db.init_db()
    with db.connection() as conn:
        assert repo.count_jobs(conn) == 0
        assert repo.get_profile(conn).name == "Kundan"
        done = repo.kv_get(conn, "legacy_import_done")
        assert isinstance(done, dict) and done["jobs"] == 0


def test_legacy_import_of_real_jobs_copy(legacy_copy: Path) -> None:
    before = sha256(legacy_copy)
    records = json.loads(legacy_copy.read_text(encoding="utf-8"))
    assert len(records) == 89
    assert "NaN" in legacy_copy.read_text(encoding="utf-8")

    db.init_db()

    assert sha256(legacy_copy) == before
    unique_ids = {r["id"] for r in records}
    with db.connection() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
        assert {r["id"] for r in rows} == unique_ids
        assert len(rows) == len(unique_ids) == 86  # 3 ids appear twice in the file
        done = repo.kv_get(conn, "legacy_import_done")
        assert isinstance(done, dict)
        assert (done["records"], done["jobs"]) == (89, 86)

        key = repo.current_score_key(conn)
        by_id = {r["id"]: r for r in records}
        for row in rows:
            raw = strict_loads(row["raw_json"])
            assert isinstance(raw, dict) and "match_score" not in raw
            for column in ("matched_skills", "backend_skills", "ai_skills", "missing_skills"):
                assert isinstance(strict_loads(row[column]), list)
            assert row["score_key"] == key
            assert row["match_score"] is not None and 10 <= row["match_score"] <= 99
            assert row["first_seen_run_id"] is None and row["last_seen_run_id"] is None
            posted = by_id[row["id"]]["date_posted"]
            assert row["first_seen_at"] == f"{posted}T00:00:00.000+00:00"
            assert row["last_seen_at"] == row["first_seen_at"]
            for column in ("min_amount", "max_amount", "currency", "interval"):
                value = row[column]
                assert value is None or value == value  # no NaN stored

        listing = repo.list_jobs(conn, JobFilters(exclude_senior=False, page_size=200))
        assert listing["total_unfiltered"] + conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NOT NULL"
        ).fetchone()[0] == 86
        text = json.dumps(listing, allow_nan=False)
        assert strict_loads(text)
        assert all(not job["is_new"] for job in listing["jobs"])  # type: ignore[union-attr]
        for job in listing["jobs"]:  # type: ignore[union-attr]
            detail = repo.get_job(conn, str(job["id"]))
            json.dumps(detail, allow_nan=False)
        json.dumps(repo.get_stats(conn), allow_nan=False)

        naukri = conn.execute(
            "SELECT * FROM jobs WHERE id = 'nk-100826024113'"
        ).fetchone()
        assert naukri["experience_range"] == "2-5 Yrs"
        assert naukri["min_experience"] == 2.0 and naukri["is_senior"] == 0


def test_legacy_import_is_idempotent(legacy_copy: Path) -> None:
    db.init_db()
    with db.connection() as conn:
        repo.set_application(conn, "li-4479171416", "applied", "keep me")
        marker = repo.kv_get(conn, "legacy_import_done")
        assert db.import_legacy_data(conn) is None
    db.init_db()
    with db.connection() as conn:
        assert repo.count_jobs(conn) == 86
        assert repo.kv_get(conn, "legacy_import_done") == marker
        assert repo.get_application(conn, "li-4479171416")["notes"] == "keep me"  # type: ignore[index]


def test_legacy_applications_imported(legacy_copy: Path) -> None:
    apps = {
        "li-4479171416": {"status": "applied", "notes": "sent"},
        "li-4424684219": {"status": "saved"},
        "missing-job": {"status": "applied", "notes": "x"},
        "li-4424673345": {"status": "bogus"},
    }
    (legacy_copy.parent / "applications.json").write_text(json.dumps(apps), encoding="utf-8")
    db.init_db()
    with db.connection() as conn:
        assert repo.kv_get(conn, "legacy_import_done")["applications"] == 2  # type: ignore[index]
        assert repo.get_application(conn, "li-4479171416")["notes"] == "sent"  # type: ignore[index]
        assert repo.get_application(conn, "li-4424684219")["status"] == "saved"  # type: ignore[index]
        assert repo.get_application(conn, "li-4424673345") is None


def test_unreadable_legacy_file_is_retried_later(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "jobs.json").write_text("{not json", encoding="utf-8")
    db.init_db()
    with db.connection() as conn:
        assert repo.kv_get(conn, "legacy_import_done") is None
    (data_dir / "jobs.json").write_text(json.dumps([make_job()["raw"]]), encoding="utf-8")
    db.init_db()
    with db.connection() as conn:
        assert repo.count_jobs(conn) == 1


def test_init_db_rescores_stale_rows(legacy_copy: Path) -> None:
    db.init_db()
    with db.connection() as conn:
        conn.execute("UPDATE jobs SET score_key = '1:0', match_score = 0")
    db.init_db()
    with db.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE score_key != ? OR match_score = 0",
            (matcher.make_score_key(1),),
        ).fetchone()[0] == 0


def test_concurrent_init_imports_once(legacy_copy: Path) -> None:
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            db.init_db()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    with db.connection() as conn:
        assert repo.count_jobs(conn) == 86


def test_concurrent_writers_from_threads(conn: sqlite3.Connection) -> None:
    profile = repo.get_profile(conn)
    errors: list[BaseException] = []

    def writer(prefix: str) -> None:
        try:
            with db.connection() as own:
                for i in range(25):
                    job = make_job(id=f"{prefix}-{i}", company=f"{prefix} {i}")
                    repo.upsert_job(own, job, profile, 1, now=T0 + timedelta(seconds=i))
                    repo.list_jobs(own)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(f"t{n}",)) for n in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert repo.count_jobs(conn) == 100
