from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import sys
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend import config, matcher, normalize
from backend.db import transaction
from backend.models import (
    Profile,
    ScheduleSettings,
    ScrapeSettings,
    load_lenient,
    load_profile_template,
)

log = logging.getLogger("jobspy_dashboard.repo")

SORT_OPTIONS: tuple[str, ...] = ("match_score", "exp_asc", "exp_desc", "date", "salary", "found")
STATUS_FILTERS: tuple[str, ...] = ("all", "none", *config.APPLICATION_STATUSES)
NEW_RUN_STATUSES: tuple[str, ...] = ("running", "completed", "partial", "cancelled")
FINAL_RUN_STATUSES: tuple[str, ...] = ("completed", "partial", "failed", "cancelled")

_JSON_LIST_COLUMNS = ("matched_skills", "backend_skills", "ai_skills", "missing_skills")
_SCORE_COLUMNS = (
    "match_score",
    *_JSON_LIST_COLUMNS,
    "role_track",
    "experience_required",
    "min_experience",
    "max_experience",
    "is_senior",
    "is_intern",
    "senior_reason",
    "salary_display",
    "annual_salary_value",
    "score_key",
)


class ScrapeAlreadyRunning(Exception):
    def __init__(self, run_id: int) -> None:
        super().__init__(f"Scrape run {run_id} is already in progress")
        self.run_id = run_id


# --- helpers -----------------------------------------------------------------


def _now_iso(now: datetime | None) -> str:
    return normalize.iso(now or normalize.utc_now())


def _dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _loads_list(text: object) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    try:
        value = json.loads(text)
    except ValueError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


def _loads_dict(text: object) -> dict[str, object]:
    if not isinstance(text, str) or not text:
        return {}
    try:
        value = json.loads(text)
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _bool_or_none(value: object) -> bool | None:
    return None if value is None else bool(value)


# --- kv: profile & settings --------------------------------------------------


def kv_get(conn: sqlite3.Connection, key: str, default: object = None) -> object:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def kv_set(
    conn: sqlite3.Connection, key: str, value: object, now: datetime | None = None
) -> None:
    conn.execute(
        "INSERT INTO kv (key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, _dumps(value), _now_iso(now)),
    )


def seed_defaults(conn: sqlite3.Connection) -> None:
    """Seed the profile from backend/profile.json and profile_version=1 if missing."""
    with transaction(conn):
        if kv_get(conn, "profile") is None:
            kv_set(conn, "profile", load_profile_template().model_dump())
        if kv_get(conn, "profile_version") is None:
            kv_set(conn, "profile_version", 1)


def get_profile(conn: sqlite3.Connection) -> Profile:
    stored = kv_get(conn, "profile")
    if isinstance(stored, dict):
        try:
            return Profile.model_validate(stored)
        except ValueError:
            log.warning("Stored profile is invalid; using the template")
    return load_profile_template()


def get_profile_version(conn: sqlite3.Connection) -> int:
    value = kv_get(conn, "profile_version", 1)
    return value if isinstance(value, int) and value > 0 else 1


def current_score_key(conn: sqlite3.Connection) -> str:
    return matcher.make_score_key(get_profile_version(conn))


def replace_profile(conn: sqlite3.Connection, profile: Profile) -> int:
    """Store profile, bump profile_version and re-score every job in one write
    transaction (scores are computed before it). Returns the re-scored count."""
    rows = _rows_to_score(conn, None)
    scores = _scores_for(rows, profile)
    with transaction(conn):
        version = get_profile_version(conn) + 1
        score_key = matcher.make_score_key(version)
        kv_set(conn, "profile", profile.model_dump())
        kv_set(conn, "profile_version", version)
        written = _write_scores(conn, rows, scores, score_key)
        return written + _rescore_locked(conn, profile, score_key)


def get_scrape_settings(conn: sqlite3.Connection) -> ScrapeSettings:
    return load_lenient(ScrapeSettings, kv_get(conn, "scrape_settings"))


def save_scrape_settings(conn: sqlite3.Connection, settings: ScrapeSettings) -> None:
    kv_set(conn, "scrape_settings", settings.model_dump())


def get_schedule_settings(conn: sqlite3.Connection) -> ScheduleSettings:
    return load_lenient(ScheduleSettings, kv_get(conn, "schedule"))


def save_schedule_settings(conn: sqlite3.Connection, settings: ScheduleSettings) -> None:
    kv_set(conn, "schedule", settings.model_dump())


# --- jobs: upsert ------------------------------------------------------------


@dataclass
class UpsertCounts:
    new: int = 0
    updated: int = 0
    duplicates: int = 0

    def add(self, outcome: str) -> None:
        field = {"new": "new", "updated": "updated", "duplicate": "duplicates"}[outcome]
        setattr(self, field, getattr(self, field) + 1)


def _score_values(
    job: Mapping[str, object], profile: Profile, score_key: str
) -> dict[str, object]:
    scores = matcher.score_job(job, profile)
    values = {c: scores[c] for c in _SCORE_COLUMNS if c in scores}
    for column in _JSON_LIST_COLUMNS:
        values[column] = _dumps(values[column])
    values["is_senior"] = int(bool(values["is_senior"]))
    values["is_intern"] = int(bool(values["is_intern"]))
    values["score_key"] = score_key
    return values


def _data_values(job: Mapping[str, object]) -> dict[str, object]:
    values = {c: job.get(c) for c in normalize.JOB_FIELDS if c != "id"}
    remote = values["is_remote"]
    values["is_remote"] = None if remote is None else int(bool(remote))
    values["raw_json"] = _dumps(normalize.json_safe(job.get("raw") or {}))
    return values


def _find_primary(conn: sqlite3.Connection, job_id: str, key: object) -> str | None:
    if not isinstance(key, str) or key.startswith("|") or "||" in key:
        return None  # company or title missing: too weak to dedupe on
    row = conn.execute(
        "SELECT id FROM jobs WHERE dedupe_key = ? AND duplicate_of IS NULL AND id != ? "
        "ORDER BY first_seen_at, id LIMIT 1",
        (key, job_id),
    ).fetchone()
    return row["id"] if row else None


def upsert_job(
    conn: sqlite3.Connection,
    job: Mapping[str, object],
    profile: Profile,
    profile_version: int,
    run_id: int | None = None,
    now: datetime | None = None,
) -> str:
    """Insert or update one normalised job (normalize.normalize_job output).

    Returns "new" (new primary), "duplicate" (new id linked to a primary via
    dedupe_key) or "updated" (existing id: first_seen_* kept, last_seen_* bumped,
    a non-empty description is never replaced by an empty one).
    """
    job_id = str(job["id"])
    stamp = _now_iso(now)
    score_key = matcher.make_score_key(profile_version)
    with transaction(conn):
        existing = conn.execute(
            "SELECT description FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        data = _data_values(job)
        if existing is not None and not data["description"] and existing["description"]:
            data["description"] = existing["description"]
        scores = _score_values({**job, "description": data["description"]}, profile, score_key)
        values = {**data, **scores, "last_seen_at": stamp, "last_seen_run_id": run_id}
        if existing is not None:
            assignments = ", ".join(f"{c} = ?" for c in values)
            conn.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?", (*values.values(), job_id)
            )
            return "updated"
        primary = _find_primary(conn, job_id, data["dedupe_key"])
        values.update(
            id=job_id,
            duplicate_of=primary,
            first_seen_at=stamp,
            first_seen_run_id=run_id,
        )
        columns = ", ".join(values)
        marks = ", ".join("?" for _ in values)
        conn.execute(f"INSERT INTO jobs ({columns}) VALUES ({marks})", tuple(values.values()))
        return "duplicate" if primary else "new"


def upsert_jobs(
    conn: sqlite3.Connection,
    jobs: Iterable[Mapping[str, object]],
    profile: Profile,
    profile_version: int,
    run_id: int | None = None,
    now: datetime | None = None,
) -> UpsertCounts:
    counts = UpsertCounts()
    with transaction(conn):
        for job in jobs:
            counts.add(upsert_job(conn, job, profile, profile_version, run_id, now))
    return counts


# --- jobs: re-scoring --------------------------------------------------------
# Scoring costs milliseconds per job, so rows are scored before the write lock is
# taken and only the UPDATEs run under it. An UPDATE applies only if the row's
# score inputs are unchanged; rows changed or added meanwhile still carry an old
# score_key and are re-scored inside the transaction.

_SCORE_INPUTS = (
    "title",
    "description",
    "experience_range",
    "min_amount",
    "max_amount",
    "currency",
    "interval",
)


def _rows_to_score(conn: sqlite3.Connection, stale_for: str | None) -> list[sqlite3.Row]:
    query = f"SELECT id, {', '.join(_SCORE_INPUTS)} FROM jobs"
    if stale_for is None:
        return conn.execute(query).fetchall()
    query += " WHERE score_key IS NULL OR score_key != ?"
    return conn.execute(query, (stale_for,)).fetchall()


def _scores_for(rows: list[sqlite3.Row], profile: Profile) -> list[dict[str, object]]:
    return [_score_values(dict(row), profile, "") for row in rows]


def _write_scores(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    scores: list[dict[str, object]],
    score_key: str,
) -> int:
    if not rows:
        return 0
    assignments = ", ".join(f"{c} = ?" for c in _SCORE_COLUMNS)
    unchanged = " AND ".join(f"{c} IS ?" for c in _SCORE_INPUTS)
    params = [
        (
            *({**values, "score_key": score_key}[c] for c in _SCORE_COLUMNS),
            row["id"],
            *(row[c] for c in _SCORE_INPUTS),
        )
        for row, values in zip(rows, scores, strict=True)
    ]
    sql = f"UPDATE jobs SET {assignments} WHERE id = ? AND {unchanged}"
    return conn.executemany(sql, params).rowcount


def _rescore_locked(conn: sqlite3.Connection, profile: Profile, score_key: str) -> int:
    """Inside the write transaction: score every row not yet at score_key."""
    rows = _rows_to_score(conn, score_key)
    return _write_scores(conn, rows, _scores_for(rows, profile), score_key)


def rescore_stale(conn: sqlite3.Connection) -> int:
    """Re-score rows whose score_key differs from the current profile/matcher."""
    version = get_profile_version(conn)
    profile = get_profile(conn)
    rows = _rows_to_score(conn, matcher.make_score_key(version))
    scores = _scores_for(rows, profile)
    with transaction(conn):
        if get_profile_version(conn) != version:  # replaced meanwhile: scores are moot
            version, profile = get_profile_version(conn), get_profile(conn)
            rows, scores = [], []
        score_key = matcher.make_score_key(version)
        written = _write_scores(conn, rows, scores, score_key)
        return written + _rescore_locked(conn, profile, score_key)


# --- jobs: queries -----------------------------------------------------------


@dataclass
class JobFilters:
    search: str | None = None
    role_track: str = "all"
    location: str = "all"
    sites: str | None = "all"
    sort_by: str = "match_score"
    min_score: int = 0
    remote_only: bool = False
    exclude_senior: bool = True
    status: str = "all"
    new_only: bool = False
    page: int = 1
    page_size: int = config.DEFAULT_PAGE_SIZE


_LAST_NEW_RUN = (
    "(SELECT MAX(started_at) FROM runs WHERE status IN "
    f"({', '.join(repr(s) for s in NEW_RUN_STATUSES)}))"
)
_IS_NEW = (
    "(CASE WHEN j.first_seen_run_id IS NOT NULL "
    f"AND j.first_seen_at >= {_LAST_NEW_RUN} THEN 1 ELSE 0 END)"
)
_SUMMARY_SELECT = f"""
SELECT j.id, j.site, j.title, j.company, j.location, j.job_url, j.job_url_direct,
       j.date_posted, j.is_remote, j.salary_display, j.match_score, j.matched_skills,
       j.missing_skills, j.backend_skills, j.ai_skills, j.role_track,
       j.experience_required, j.min_experience, j.max_experience, j.is_senior,
       j.is_intern, j.senior_reason, j.first_seen_at, j.last_seen_at,
       j.duplicate_of, {_IS_NEW} AS is_new,
       a.status AS app_status, a.notes AS app_notes,
       substr(j.description, 1, 2000) AS description_head
"""
_FROM = " FROM jobs j LEFT JOIN applications a ON a.job_id = j.id"
_ORDER_BY = {
    "match_score": "j.match_score DESC, j.date_posted IS NULL, j.date_posted DESC, "
    "j.first_seen_at DESC",
    "exp_asc": "j.min_experience IS NULL, j.min_experience ASC, j.match_score DESC",
    "exp_desc": "j.min_experience IS NULL, j.min_experience DESC, j.match_score DESC",
    "date": "j.date_posted IS NULL, j.date_posted DESC, j.first_seen_at DESC, "
    "j.match_score DESC",
    "salary": "j.annual_salary_value IS NULL, j.annual_salary_value DESC, "
    "j.match_score DESC",
    "found": "j.first_seen_at DESC, j.match_score DESC",
}


def parse_sites(sites: str | None) -> list[str] | None:
    """None = all sites; [] = nothing selected (no results)."""
    if sites is None or sites.strip().lower() == "all":
        return None
    return [s.strip().lower() for s in sites.split(",") if s.strip()]


def _location_clause(location: str) -> tuple[str, list[object]]:
    variants = normalize.city_variants(location)
    if not variants:
        return "1", []
    parts = ["instr(lower(coalesce(j.location, '')), ?) > 0" for _ in variants]
    params: list[object] = list(variants)
    parts.append("j.city_norm = ?")
    params.append(variants[0])
    if variants[0] == "remote":
        parts.append("j.is_remote = 1")
    return "(" + " OR ".join(parts) + ")", params


def _site_clause(sites: Sequence[str]) -> tuple[str, list[object]]:
    marks = ", ".join("?" for _ in sites)
    clause = (
        f"(j.site IN ({marks}) OR EXISTS (SELECT 1 FROM jobs d "
        f"WHERE d.duplicate_of = j.id AND d.site IN ({marks})))"
    )
    return clause, [*sites, *sites]


def _filter_clauses(filters: JobFilters) -> tuple[list[str], list[object]]:
    clauses = ["j.duplicate_of IS NULL"]
    params: list[object] = []
    search = (filters.search or "").strip().lower()
    if search:
        columns = ("j.title", "j.company", "j.description", "j.matched_skills")
        parts = [f"instr(lower(coalesce({c}, '')), ?) > 0" for c in columns]
        clauses.append("(" + " OR ".join(parts) + ")")
        params.extend([search] * len(columns))
    if filters.role_track and filters.role_track != "all":
        clauses.append("j.role_track = ?")
        params.append(filters.role_track)
    if filters.location and filters.location.strip().lower() != "all":
        clause, values = _location_clause(filters.location)
        clauses.append(clause)
        params.extend(values)
    sites = parse_sites(filters.sites)
    if sites is not None:
        clause, values = _site_clause(sites) if sites else ("0", [])
        clauses.append(clause)
        params.extend(values)
    if filters.min_score:
        clauses.append("j.match_score >= ?")
        params.append(filters.min_score)
    if filters.remote_only:
        clauses.append(
            "(j.is_remote = 1 OR j.city_norm = 'remote' "
            "OR instr(lower(coalesce(j.location, '')), 'remote') > 0)"
        )
    if filters.exclude_senior:
        clauses.append("j.is_senior = 0 AND j.is_intern = 0")
    if filters.status == "none":
        clauses.append("a.job_id IS NULL")
    elif filters.status and filters.status != "all":
        clauses.append("a.status = ?")
        params.append(filters.status)
    if filters.new_only:
        clauses.append(f"{_IS_NEW} = 1")
    return clauses, params


def _also_on(
    conn: sqlite3.Connection, primaries: Mapping[str, str | None]
) -> dict[str, list[dict[str, object]]]:
    """Other boards carrying a copy of each primary: one entry per other site."""
    result: dict[str, list[dict[str, object]]] = {pid: [] for pid in primaries}
    if not primaries:
        return result
    ids = list(primaries)
    marks = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT duplicate_of, site, job_url FROM jobs WHERE duplicate_of IN ({marks}) "
        "ORDER BY first_seen_at, id",
        ids,
    ).fetchall()
    for row in rows:
        owner = row["duplicate_of"]
        entries = result[owner]
        sites = {primaries[owner], *(e["site"] for e in entries)}
        if row["site"] not in sites:
            entries.append({"site": row["site"], "job_url": row["job_url"]})
    return result


def _summary(row: sqlite3.Row, also_on: list[dict[str, object]]) -> dict[str, object]:
    return {
        "id": row["id"],
        "site": row["site"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "job_url": row["job_url"],
        "job_url_direct": row["job_url_direct"],
        "date_posted": row["date_posted"],
        "is_remote": _bool_or_none(row["is_remote"]),
        "salary_display": row["salary_display"],
        "match_score": row["match_score"],
        "matched_skills": _loads_list(row["matched_skills"]),
        "missing_skills": _loads_list(row["missing_skills"]),
        "backend_skills": _loads_list(row["backend_skills"]),
        "ai_skills": _loads_list(row["ai_skills"]),
        "role_track": row["role_track"],
        "experience_required": row["experience_required"],
        "min_experience": row["min_experience"],
        "max_experience": row["max_experience"],
        "is_senior": bool(row["is_senior"]),
        "is_intern": bool(row["is_intern"]),
        "senior_reason": row["senior_reason"],
        "first_seen_at": row["first_seen_at"],
        "last_seen_at": row["last_seen_at"],
        "is_new": bool(row["is_new"]),
        "also_on": also_on,
        "app_status": row["app_status"] or "none",
        "app_notes": row["app_notes"] or "",
        "description_snippet": normalize.plain_text_snippet(
            row["description_head"], config.SNIPPET_LENGTH
        ),
    }


def clamp_page(page: int, page_size: int) -> tuple[int, int]:
    return max(1, page), max(1, min(page_size, config.MAX_PAGE_SIZE))


def list_jobs(conn: sqlite3.Connection, filters: JobFilters | None = None) -> dict[str, object]:
    """GET /api/jobs: {"total", "total_unfiltered", "page", "page_size", "jobs"}."""
    flt = filters or JobFilters()
    page, page_size = clamp_page(flt.page, flt.page_size)
    clauses, params = _filter_clauses(flt)
    where = " WHERE " + " AND ".join(clauses)
    total = conn.execute(f"SELECT COUNT(*){_FROM}{where}", params).fetchone()[0]
    unfiltered = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE duplicate_of IS NULL"
    ).fetchone()[0]
    order = _ORDER_BY.get(flt.sort_by, _ORDER_BY["match_score"])
    rows = conn.execute(
        f"{_SUMMARY_SELECT}{_FROM}{where} ORDER BY {order}, j.id LIMIT ? OFFSET ?",
        [*params, page_size, (page - 1) * page_size],
    ).fetchall()
    also = _also_on(conn, {row["id"]: row["site"] for row in rows})
    return {
        "total": total,
        "total_unfiltered": unfiltered,
        "page": page,
        "page_size": page_size,
        "jobs": [_summary(row, also[row["id"]]) for row in rows],
    }


def job_exists(conn: sqlite3.Connection, job_id: str) -> bool:
    return conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is not None


def _group_also_on(
    conn: sqlite3.Connection, job_id: str, site: str | None, primary_id: str
) -> list[dict[str, object]]:
    rows = conn.execute(
        "SELECT site, job_url FROM jobs WHERE (id = ? OR duplicate_of = ?) AND id != ? "
        "ORDER BY duplicate_of IS NOT NULL, first_seen_at, id",
        (primary_id, primary_id, job_id),
    ).fetchall()
    entries: list[dict[str, object]] = []
    seen = {site}
    for row in rows:
        if row["site"] not in seen:
            seen.add(row["site"])
            entries.append({"site": row["site"], "job_url": row["job_url"]})
    return entries


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, object] | None:
    """GET /api/jobs/{id}: JobSummary + detail fields, or None."""
    row = conn.execute(
        f"{_SUMMARY_SELECT}, j.description, j.job_type, j.currency, j.interval, "
        "j.min_amount, j.max_amount, j.experience_range, "
        f"json_extract(j.raw_json, '$.company_url') AS company_url{_FROM} WHERE j.id = ?",
        (job_id,),
    ).fetchone()
    if row is None:
        return None
    primary_id = row["duplicate_of"] or row["id"]
    also_on = _group_also_on(conn, row["id"], row["site"], primary_id)
    detail = _summary(row, also_on)
    for column in (
        "description",
        "job_type",
        "currency",
        "interval",
        "min_amount",
        "max_amount",
        "experience_range",
        "company_url",
    ):
        detail[column] = row[column]
    return detail


# --- applications ------------------------------------------------------------


def set_application(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    notes: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Upsert an application row; status "none" deletes it. notes=None keeps the
    existing notes. Returns False if the job does not exist (-> 404)."""
    if status == "none":
        with transaction(conn):
            if not job_exists(conn, job_id):
                return False
            delete_application(conn, job_id)
            return True
    if status not in config.APPLICATION_STATUSES:
        raise ValueError(f"invalid application status: {status}")
    with transaction(conn):
        if not job_exists(conn, job_id):
            return False
        conn.execute(
            "INSERT INTO applications (job_id, status, notes, updated_at) "
            "VALUES (?, ?, coalesce(?, ''), ?) "
            "ON CONFLICT(job_id) DO UPDATE SET status = excluded.status, "
            "notes = coalesce(?, applications.notes), updated_at = excluded.updated_at",
            (job_id, status, notes, _now_iso(now), notes),
        )
    return True


def delete_application(conn: sqlite3.Connection, job_id: str) -> bool:
    cursor = conn.execute("DELETE FROM applications WHERE job_id = ?", (job_id,))
    return cursor.rowcount > 0


def get_application(conn: sqlite3.Connection, job_id: str) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT job_id, status, notes, updated_at FROM applications WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    return dict(row) if row else None


# --- stats & pruning ---------------------------------------------------------


_VISIBLE = "j.duplicate_of IS NULL AND j.is_senior = 0 AND j.is_intern = 0"


def get_stats(conn: sqlite3.Connection) -> dict[str, object]:
    """GET /api/stats. Counts use primary, non-senior, non-intern jobs unless noted."""

    def scalar(sql: str) -> int:
        return int(conn.execute(sql).fetchone()[0] or 0)

    status_counts = {
        row["status"]: row["n"]
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM applications GROUP BY status"
        )
    }
    platforms = {
        row["site"] or "unknown": row["n"]
        for row in conn.execute(
            f"SELECT j.site, COUNT(*) AS n FROM jobs j WHERE {_VISIBLE} "
            "GROUP BY j.site ORDER BY n DESC, j.site"
        )
    }
    top_skills = conn.execute(
        f"SELECT s.value AS name, COUNT(*) AS count FROM jobs j, "
        f"json_each(j.matched_skills) s WHERE {_VISIBLE} "
        "GROUP BY s.value ORDER BY count DESC, name LIMIT 10"
    ).fetchall()
    top_companies = conn.execute(
        f"SELECT j.company AS name, COUNT(*) AS count FROM jobs j WHERE {_VISIBLE} "
        "AND coalesce(trim(j.company), '') != '' "
        "GROUP BY j.company ORDER BY count DESC, name LIMIT 8"
    ).fetchall()
    last_run = get_last_finished_run(conn)
    return {
        "total_jobs": scalar(f"SELECT COUNT(*) FROM jobs j WHERE {_VISIBLE}"),
        "total_stored": scalar("SELECT COUNT(*) FROM jobs"),
        "high_match_count": scalar(
            "SELECT COUNT(*) FROM jobs j WHERE j.duplicate_of IS NULL "
            "AND j.is_senior = 0 AND j.match_score >= 80"
        ),
        # Like the run's new_jobs: every new primary job, senior and intern included.
        "new_count": scalar(
            f"SELECT COUNT(*) FROM jobs j WHERE j.duplicate_of IS NULL AND {_IS_NEW} = 1"
        ),
        "applied_count": status_counts.get("applied", 0),
        "saved_count": status_counts.get("saved", 0),
        "interviewing_count": status_counts.get("interviewing", 0),
        "rejected_count": status_counts.get("rejected", 0),
        "platform_distribution": platforms,
        "top_skills": [dict(row) for row in top_skills],
        "top_companies": [dict(row) for row in top_companies],
        "last_scrape_at": last_run["finished_at"] if last_run else None,
    }


def prune_jobs(
    conn: sqlite3.Connection, retention_days: int, now: datetime | None = None
) -> int:
    """Delete job groups (primary + duplicates) whose newest last_seen_at is older
    than retention_days and where no member has an application row."""
    cutoff = normalize.iso((now or normalize.utc_now()) - timedelta(days=retention_days))
    with transaction(conn):
        ids = [
            row[0]
            for row in conn.execute(
                """
                SELECT id FROM jobs WHERE coalesce(duplicate_of, id) IN (
                    SELECT coalesce(j.duplicate_of, j.id) AS gid FROM jobs j
                    LEFT JOIN applications a ON a.job_id = j.id
                    GROUP BY gid
                    HAVING MAX(j.last_seen_at) < ? AND COUNT(a.job_id) = 0
                )
                ORDER BY duplicate_of IS NULL
                """,
                (cutoff,),
            )
        ]
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            conn.execute(f"DELETE FROM jobs WHERE id IN ({marks})", chunk)
    return len(ids)


def count_jobs(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])


# --- runs --------------------------------------------------------------------


def pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return code.value == 259  # STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def this_host() -> str:
    return socket.gethostname()


def _run_dict(row: sqlite3.Row) -> dict[str, object]:
    return {
        "id": row["id"],
        "trigger": row["trigger"],
        "status": row["status"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "message": row["message"],
        "total_queries": row["total_queries"],
        "current_query": row["current_query"],
        "jobs_found": row["jobs_found"],
        "new_jobs": row["new_jobs"],
        "updated_jobs": row["updated_jobs"],
        "errors": _loads_dict(row["errors_json"]),
        "cancel_requested": bool(row["cancel_requested"]),
    }


def _holder_alive(row: sqlite3.Row, now: datetime, host: str) -> bool:
    beat = normalize.parse_iso(row["heartbeat_at"] or row["started_at"])
    if beat is None or now - beat >= timedelta(seconds=config.STALE_HEARTBEAT_SECONDS):
        return False
    if row["host"] in (None, host):
        return pid_alive(row["pid"])
    return True


def acquire_run_lock(
    conn: sqlite3.Connection,
    trigger: str,
    params: Mapping[str, object] | None = None,
    total_queries: int = 0,
    *,
    now: datetime | None = None,
    pid: int | None = None,
    host: str | None = None,
    message: str | None = "Starting…",
) -> int:
    """Insert a `running` run row inside BEGIN IMMEDIATE and return its id.

    A running row with a heartbeat < 5 min old whose pid is alive (checked only on
    the same host) -> ScrapeAlreadyRunning; otherwise it is marked `interrupted`.
    """
    moment = now or normalize.utc_now()
    stamp = normalize.iso(moment)
    owner_host = host or this_host()
    with transaction(conn):
        for row in conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall():
            if _holder_alive(row, moment, owner_host):
                raise ScrapeAlreadyRunning(row["id"])
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, message = ? "
                "WHERE id = ?",
                (stamp, "Interrupted: worker stopped responding", row["id"]),
            )
        cursor = conn.execute(
            "INSERT INTO runs (trigger, status, started_at, heartbeat_at, pid, host, "
            "params_json, total_queries, message) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?, ?)",
            (
                trigger,
                stamp,
                stamp,
                pid or os.getpid(),
                owner_host,
                _dumps(normalize.json_safe(dict(params or {}))),
                total_queries,
                message,
            ),
        )
        return int(cursor.lastrowid or 0)


def heartbeat(conn: sqlite3.Connection, run_id: int, now: datetime | None = None) -> None:
    conn.execute(
        "UPDATE runs SET heartbeat_at = ? WHERE id = ? AND status = 'running'",
        (_now_iso(now), run_id),
    )


def update_run_progress(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    current_query: int | None = None,
    total_queries: int | None = None,
    message: str | None = None,
    jobs_found: int | None = None,
    new_jobs: int | None = None,
    updated_jobs: int | None = None,
    errors: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> None:
    """Set (absolute values) any given progress fields; also refreshes the heartbeat."""
    values: dict[str, object] = {
        "current_query": current_query,
        "total_queries": total_queries,
        "message": message,
        "jobs_found": jobs_found,
        "new_jobs": new_jobs,
        "updated_jobs": updated_jobs,
        "errors_json": None if errors is None else _dumps(dict(errors)),
    }
    values = {k: v for k, v in values.items() if v is not None}
    values["heartbeat_at"] = _now_iso(now)
    assignments = ", ".join(f"{k} = ?" for k in values)
    conn.execute(
        f"UPDATE runs SET {assignments} WHERE id = ? AND status = 'running'",
        (*values.values(), run_id),
    )


def request_cancel(conn: sqlite3.Connection) -> int | None:
    """Flag the running run for cancellation; returns its id or None (-> 404)."""
    row = conn.execute(
        "UPDATE runs SET cancel_requested = 1 WHERE status = 'running' RETURNING id"
    ).fetchone()
    return int(row["id"]) if row else None


def is_cancel_requested(conn: sqlite3.Connection, run_id: int) -> bool:
    row = conn.execute(
        "SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)
    ).fetchone()
    return bool(row and row["cancel_requested"])


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    message: str | None = None,
    *,
    errors: Mapping[str, str] | None = None,
    now: datetime | None = None,
    only_if_running: bool = False,
) -> bool:
    """Set the final status. With only_if_running, a run that was already finished
    (e.g. force-marked by another thread) is left alone. Returns True if updated."""
    if status not in FINAL_RUN_STATUSES and status != "interrupted":
        raise ValueError(f"invalid final run status: {status}")
    stamp = _now_iso(now)
    guard = " AND status = 'running'" if only_if_running else ""
    cursor = conn.execute(
        "UPDATE runs SET status = ?, finished_at = ?, heartbeat_at = ?, "
        "message = coalesce(?, message), errors_json = coalesce(?, errors_json) "
        f"WHERE id = ?{guard}",
        (
            status,
            stamp,
            stamp,
            message,
            None if errors is None else _dumps(dict(errors)),
            run_id,
        ),
    )
    return cursor.rowcount > 0


def get_run(conn: sqlite3.Connection, run_id: int) -> dict[str, object] | None:
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _run_dict(row) if row else None


def get_run_params(conn: sqlite3.Connection, run_id: int) -> dict[str, object]:
    row = conn.execute("SELECT params_json FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _loads_dict(row["params_json"]) if row else {}


def get_current_run(conn: sqlite3.Connection) -> dict[str, object] | None:
    row = conn.execute(
        "SELECT * FROM runs WHERE status = 'running' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return _run_dict(row) if row else None


def list_runs(
    conn: sqlite3.Connection, limit: int = config.HISTORY_LIMIT
) -> list[dict[str, object]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
    return [_run_dict(row) for row in rows]


def get_last_finished_run(conn: sqlite3.Connection) -> dict[str, object] | None:
    """Most recently finished run of any trigger, excluding `interrupted`."""
    marks = ", ".join("?" for _ in FINAL_RUN_STATUSES)
    row = conn.execute(
        f"SELECT * FROM runs WHERE finished_at IS NOT NULL AND status IN ({marks}) "
        "ORDER BY finished_at DESC, id DESC LIMIT 1",
        FINAL_RUN_STATUSES,
    ).fetchone()
    return _run_dict(row) if row else None


def _holder_gone(
    row: sqlite3.Row, now: datetime, host: str, active: Collection[int] | None
) -> bool:
    beat = normalize.parse_iso(row["heartbeat_at"] or row["started_at"])
    stale = beat is None or now - beat >= timedelta(seconds=config.STALE_HEARTBEAT_SECONDS)
    if row["host"] in (None, host):
        if not pid_alive(row["pid"]):
            return True
        if row["pid"] == os.getpid():
            # Our own run: a stale beat with a live worker means the machine slept.
            return stale and active is not None and row["id"] not in active
    return stale


def reap_dead_runs(
    conn: sqlite3.Connection,
    now: datetime | None = None,
    host: str | None = None,
    active_run_ids: Collection[int] | None = None,
) -> int:
    """Mark `running` rows whose owner is gone (dead pid on this host, a stale
    heartbeat from another process, or a stale own row that none of
    `active_run_ids` — this process's workers — owns) as `interrupted`."""
    moment = now or normalize.utc_now()
    owner_host = host or this_host()
    rows = conn.execute("SELECT * FROM runs WHERE status = 'running'").fetchall()
    dead = [
        row["id"]
        for row in rows
        if _holder_gone(row, moment, owner_host, active_run_ids)
    ]
    if not dead:
        return 0
    with transaction(conn):
        for run_id in dead:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, message = ? "
                "WHERE id = ? AND status = 'running'",
                (normalize.iso(moment), "Interrupted: worker stopped responding", run_id),
            )
    return len(dead)


def mark_dead_runs_interrupted(
    conn: sqlite3.Connection, now: datetime | None = None, host: str | None = None
) -> int:
    """Startup only: `running` rows of this host whose pid is dead (or is this very
    process, which cannot own a run yet) become `interrupted`."""
    owner_host = host or this_host()
    me = os.getpid()
    stamp = _now_iso(now)
    with transaction(conn):
        rows = conn.execute(
            "SELECT id, pid, host FROM runs WHERE status = 'running'"
        ).fetchall()
        dead = [
            row["id"]
            for row in rows
            if row["host"] in (None, owner_host)
            and (row["pid"] == me or not pid_alive(row["pid"]))
        ]
        for run_id in dead:
            conn.execute(
                "UPDATE runs SET status = 'interrupted', finished_at = ?, message = ? "
                "WHERE id = ?",
                (stamp, "Interrupted: process exited before finishing", run_id),
            )
    return len(dead)
