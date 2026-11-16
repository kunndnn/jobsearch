from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import db, normalize, repo  # noqa: E402
from backend.models import Profile, load_profile_template  # noqa: E402

LEGACY_JOBS = PROJECT_ROOT / "data" / "jobs.json"
T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    folder = tmp_path / "data"
    monkeypatch.setenv("JOBSPY_DATA_DIR", str(folder))
    monkeypatch.delenv("JOBSPY_MIN_INTERVAL_MINUTES", raising=False)
    return folder


@pytest.fixture
def conn(data_dir: Path) -> Iterator[sqlite3.Connection]:
    connection = db.connect()
    db.migrate(connection)
    repo.seed_defaults(connection)
    try:
        yield connection
    finally:
        connection.close()


@pytest.fixture
def profile() -> Profile:
    return load_profile_template()


def make_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "id": "li-1",
        "site": "linkedin",
        "job_url": "https://www.linkedin.com/jobs/view/1",
        "job_url_direct": None,
        "title": "Backend Developer",
        "company": "Acme Pvt Ltd",
        "location": "Bangalore, Karnataka, India",
        "date_posted": "2026-10-01",
        "is_remote": False,
        "description": "Build REST APIs with Node.js and PostgreSQL. 1-3 years experience.",
        "min_amount": None,
        "max_amount": None,
        "currency": None,
        "interval": None,
        "experience_range": None,
        "company_url": "https://acme.example",
    }
    record.update(overrides)
    return record


def make_job(**overrides: object) -> dict[str, object]:
    return normalize.normalize_job(make_record(**overrides))
