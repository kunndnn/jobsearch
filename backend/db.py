from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from backend import config

log = logging.getLogger("jobspy_dashboard.db")

SCHEMA_VERSION = 1

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    site TEXT,
    title TEXT,
    company TEXT,
    location TEXT,
    city_norm TEXT,
    dedupe_key TEXT,
    duplicate_of TEXT NULL REFERENCES jobs(id) ON DELETE SET NULL,
    job_url TEXT,
    job_url_direct TEXT,
    date_posted TEXT,
    is_remote INTEGER,
    job_type TEXT,
    description TEXT,
    min_amount REAL,
    max_amount REAL,
    currency TEXT,
    interval TEXT,
    experience_range TEXT,
    raw_json TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    first_seen_run_id INTEGER,
    last_seen_run_id INTEGER,
    match_score INTEGER,
    matched_skills TEXT NOT NULL DEFAULT '[]',
    backend_skills TEXT NOT NULL DEFAULT '[]',
    ai_skills TEXT NOT NULL DEFAULT '[]',
    missing_skills TEXT NOT NULL DEFAULT '[]',
    role_track TEXT,
    experience_required TEXT,
    min_experience REAL,
    max_experience REAL,
    is_senior INTEGER NOT NULL DEFAULT 0,
    is_intern INTEGER NOT NULL DEFAULT 0,
    senior_reason TEXT,
    salary_display TEXT,
    annual_salary_value REAL,
    score_key TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key);
CREATE INDEX IF NOT EXISTS idx_jobs_duplicate_of ON jobs(duplicate_of);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_match_score ON jobs(match_score);

CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL
        CHECK (status IN ('saved', 'applied', 'interviewing', 'rejected')),
    notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'schedule', 'startup', 'cli')),
    status TEXT NOT NULL CHECK (status IN
        ('running', 'completed', 'partial', 'failed', 'interrupted', 'cancelled')),
    started_at TEXT NOT NULL,
    finished_at TEXT,
    heartbeat_at TEXT,
    pid INTEGER,
    host TEXT,
    params_json TEXT NOT NULL DEFAULT '{}',
    total_queries INTEGER NOT NULL DEFAULT 0,
    current_query INTEGER NOT NULL DEFAULT 0,
    message TEXT,
    jobs_found INTEGER NOT NULL DEFAULT 0,
    new_jobs INTEGER NOT NULL DEFAULT 0,
    updated_jobs INTEGER NOT NULL DEFAULT 0,
    errors_json TEXT NOT NULL DEFAULT '{}',
    cancel_requested INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_finished_at ON runs(finished_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_runs_single_running
    ON runs(status) WHERE status = 'running';

CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_MIGRATIONS: dict[int, str] = {1: _SCHEMA_V1}


def connect(path: Path | None = None) -> sqlite3.Connection:
    """New autocommit connection; use `transaction()` for atomic units of work.

    One connection per thread / unit of work. `check_same_thread` is off so a
    connection may be handed to another thread, but never share one concurrently.
    """
    target = path or config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        target,
        timeout=config.BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {config.BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    _enable_wal(conn)
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def _enable_wal(conn: sqlite3.Connection) -> None:
    # Switching a fresh database to WAL can fail with SQLITE_BUSY immediately
    # (the busy handler is not consulted) when another process opens it at the
    # same moment, e.g. the server and scrape.py starting together.
    deadline = time.monotonic() + config.BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


@contextmanager
def connection(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(
    conn: sqlite3.Connection, immediate: bool = True
) -> Iterator[sqlite3.Connection]:
    """BEGIN [IMMEDIATE] ... COMMIT / ROLLBACK. Re-entrant: nested use joins the
    outer transaction. IMMEDIATE takes the write lock up front (waits busy_timeout)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


def schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection) -> int:
    with transaction(conn):
        current = schema_version(conn)
        for version in sorted(v for v in _MIGRATIONS if v > current):
            for statement in _split_sql(_MIGRATIONS[version]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {version}")
            current = version
    return current


def _split_sql(script: str) -> list[str]:
    return [part.strip() for part in script.split(";") if part.strip()]


# --- legacy import -----------------------------------------------------------


@dataclass(frozen=True)
class LegacyImportResult:
    records_read: int
    jobs_stored: int
    applications: int


def _read_legacy_json(path: Path) -> object:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)  # bare NaN is accepted here and sanitised later


def _first_seen_for(date_posted: object, now: datetime) -> datetime:
    if isinstance(date_posted, str):
        try:
            day = date.fromisoformat(date_posted[:10])
        except ValueError:
            return now
        return datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    return now


def import_legacy_data(
    conn: sqlite3.Connection, data_dir: Path | None = None, now: datetime | None = None
) -> LegacyImportResult | None:
    """One-time import of <data dir>/jobs.json (+ applications.json).

    Returns None when already done (kv `legacy_import_done`) or the file is unreadable.
    Files on disk are never modified.
    """
    from backend import normalize, repo

    folder = data_dir or config.data_dir()
    moment = now or normalize.utc_now()
    with transaction(conn):
        if repo.kv_get(conn, "legacy_import_done"):
            return None
        jobs_path = folder / config.LEGACY_JOBS_FILENAME
        records: object = []
        if jobs_path.exists():
            try:
                records = _read_legacy_json(jobs_path)
            except (OSError, ValueError) as exc:
                log.error("Legacy import skipped, cannot read %s: %s", jobs_path, exc)
                return None
        if not isinstance(records, list):
            log.error("Legacy import skipped, %s is not a JSON list", jobs_path)
            return None

        profile = repo.get_profile(conn)
        version = repo.get_profile_version(conn)
        rows = [normalize.normalize_job(r) for r in records if isinstance(r, dict)]
        for row in rows:
            seen = _first_seen_for(row.get("date_posted"), moment)
            repo.upsert_job(conn, row, profile, version, run_id=None, now=seen)
        applications = _import_legacy_applications(conn, folder, moment)
        stored = len({row["id"] for row in rows})
        repo.kv_set(
            conn,
            "legacy_import_done",
            {
                "at": normalize.iso(moment),
                "records": len(rows),
                "jobs": stored,
                "applications": applications,
            },
        )
    log.info("Imported %d legacy jobs (%d records)", stored, len(rows))
    return LegacyImportResult(len(rows), stored, applications)


def _import_legacy_applications(
    conn: sqlite3.Connection, folder: Path, now: datetime
) -> int:
    from backend import repo

    path = folder / config.LEGACY_APPLICATIONS_FILENAME
    if not path.exists():
        return 0
    try:
        data = _read_legacy_json(path)
    except (OSError, ValueError) as exc:
        log.error("Cannot read legacy applications %s: %s", path, exc)
        return 0
    if not isinstance(data, dict):
        return 0
    imported = 0
    for job_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "")
        notes = entry.get("notes")
        if status not in config.APPLICATION_STATUSES:
            continue
        note_text = notes if isinstance(notes, str) else ""
        if repo.set_application(conn, str(job_id), status, note_text, now=now):
            imported += 1
    return imported


def init_db(path: Path | None = None) -> None:
    """Create/migrate the schema, seed defaults, import legacy JSON once, and
    re-score rows whose score_key is stale. Safe to call from every process."""
    from backend import repo

    target = path or config.db_path()
    with connection(target) as conn:
        migrate(conn)
        repo.seed_defaults(conn)
        import_legacy_data(conn, data_dir=target.parent)
        repo.rescore_stale(conn)
