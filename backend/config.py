from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = PROJECT_ROOT / "backend"


def frontend_dist() -> Path:
    override = os.environ.get("JOBSPY_FRONTEND_DIST", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    candidates = [
        PROJECT_ROOT / "frontend" / "dist",
        Path.cwd() / "frontend" / "dist",
        Path("/app/frontend/dist"),
    ]
    for c in candidates:
        if (c / "index.html").is_file():
            return c.resolve()
    return (PROJECT_ROOT / "frontend" / "dist").resolve()


FRONTEND_DIST = frontend_dist()
PROFILE_TEMPLATE_PATH = BACKEND_DIR / "profile.json"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"

DB_FILENAME = "jobspy.db"
LEGACY_JOBS_FILENAME = "jobs.json"
LEGACY_APPLICATIONS_FILENAME = "applications.json"

# Bump on any change to the matching rules in matcher.py; stale rows get re-scored.
MATCHER_VERSION = 3

AVAILABLE_SITES: tuple[str, ...] = ("linkedin", "naukri", "indeed", "glassdoor")
APPLICATION_STATUSES: tuple[str, ...] = ("saved", "applied", "interviewing", "rejected")
RUN_TRIGGERS: tuple[str, ...] = ("manual", "schedule", "startup", "cli")
RUN_STATUSES: tuple[str, ...] = (
    "running",
    "completed",
    "partial",
    "failed",
    "interrupted",
    "cancelled",
)

DEFAULT_MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 10080
WARN_QUERIES = 30
HEARTBEAT_INTERVAL_SECONDS = 30
STALE_HEARTBEAT_SECONDS = 300
HISTORY_LIMIT = 20
SNIPPET_LENGTH = 300
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
BUSY_TIMEOUT_MS = 10000


def data_dir() -> Path:
    override = os.environ.get("JOBSPY_DATA_DIR", "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_DATA_DIR


def ensure_data_dir() -> Path:
    path = data_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / DB_FILENAME


def legacy_jobs_path() -> Path:
    return data_dir() / LEGACY_JOBS_FILENAME


def legacy_applications_path() -> Path:
    return data_dir() / LEGACY_APPLICATIONS_FILENAME


def min_interval_minutes() -> int:
    raw = os.environ.get("JOBSPY_MIN_INTERVAL_MINUTES", "").strip()
    try:
        value = int(raw) if raw else DEFAULT_MIN_INTERVAL_MINUTES
    except ValueError:
        return DEFAULT_MIN_INTERVAL_MINUTES
    return min(max(value, 1), MAX_INTERVAL_MINUTES)
