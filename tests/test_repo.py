from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest
from conftest import T0, make_job

from backend import db, matcher, repo
from backend.models import Profile, ScheduleSettings, ScrapeSettings
from backend.repo import JobFilters


def upsert(
    conn: sqlite3.Connection,
    profile: Profile,
    run_id: int | None = None,
    at: timedelta = timedelta(0),
    **overrides: object,
) -> str:
    return repo.upsert_job(conn, make_job(**overrides), profile, 1, run_id=run_id, now=T0 + at)


def row(conn: sqlite3.Connection, job_id: str) -> sqlite3.Row:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def listed(conn: sqlite3.Connection, **filters: object) -> list[str]:
    result = repo.list_jobs(conn, JobFilters(**filters))  # type: ignore[arg-type]
    return [job["id"] for job in result["jobs"]]  # type: ignore[index]


def start_run(conn: sqlite3.Connection, at: timedelta = timedelta(0)) -> int:
    run_id = repo.acquire_run_lock(conn, "manual", {}, now=T0 + at)
    return run_id


# --- kv ----------------------------------------------------------------------


def test_kv_roundtrip(conn: sqlite3.Connection) -> None:
    repo.kv_set(conn, "x", {"a": [1, 2]})
    assert repo.kv_get(conn, "x") == {"a": [1, 2]}
    assert repo.kv_get(conn, "missing", 5) == 5


def test_profile_seeded_from_template(conn: sqlite3.Connection, profile: Profile) -> None:
    assert repo.get_profile(conn) == profile
    assert repo.get_profile_version(conn) == 1
    assert repo.current_score_key(conn) == f"1:{matcher.MATCHER_VERSION}"


def test_settings_default_and_roundtrip(conn: sqlite3.Connection) -> None:
    assert repo.get_scrape_settings(conn) == ScrapeSettings()
    assert repo.get_schedule_settings(conn) == ScheduleSettings()
    repo.save_scrape_settings(conn, ScrapeSettings(hours_old=24))
    repo.save_schedule_settings(conn, ScheduleSettings(enabled=True, interval_minutes=60))
    assert repo.get_scrape_settings(conn).hours_old == 24
    assert repo.get_schedule_settings(conn).interval_minutes == 60


def test_replace_profile_bumps_version_and_rescores(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    upsert(conn, profile, id="li-1", title="Backend Developer")
    upsert(conn, profile, id="li-2", title="Java Developer", company="Other")
    changed = profile.model_copy(update={"max_experience_years": 1.0})
    assert repo.replace_profile(conn, changed) == 2
    assert repo.get_profile_version(conn) == 2
    first = row(conn, "li-1")
    assert first["score_key"] == f"2:{matcher.MATCHER_VERSION}"
    assert first["is_senior"] == 1  # "1-3 years experience" >= new limit of 1


def test_replace_profile_scores_outside_the_write_lock(
    conn: sqlite3.Connection, profile: Profile, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert(conn, profile, id="li-1", title="Backend Developer")
    upsert(conn, profile, id="li-2", title="Java Developer", company="Other")
    other = db.connect()
    other.execute("PRAGMA busy_timeout = 0")  # fail at once if the lock is held
    real_score = matcher.score_job
    writes: list[str] = []

    def score_while_a_scrape_writes(
        job: dict[str, object], prof: Profile
    ) -> dict[str, object]:
        if not writes:  # a concurrent scrape updates li-2 and adds li-3
            writes.append("done")
            other.execute(
                "UPDATE jobs SET description = 'Need 5+ years of experience.' "
                "WHERE id = 'li-2'"
            )
            repo.upsert_job(other, make_job(id="li-3", company="Third"), profile, 1)
        return real_score(job, prof)

    monkeypatch.setattr(matcher, "score_job", score_while_a_scrape_writes)
    try:
        assert repo.replace_profile(conn, profile) == 3
    finally:
        other.close()
    key = f"2:{matcher.MATCHER_VERSION}"
    assert [row(conn, i)["score_key"] for i in ("li-1", "li-2", "li-3")] == [key] * 3
    changed = row(conn, "li-2")
    assert changed["min_experience"] == 5.0 and changed["is_senior"] == 1


def test_rescore_stale_only_touches_stale_rows(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    upsert(conn, profile, id="li-1")
    upsert(conn, profile, id="li-2", company="Other")
    conn.execute("UPDATE jobs SET score_key = 'old', match_score = 0 WHERE id = 'li-2'")
    assert repo.rescore_stale(conn) == 1
    assert row(conn, "li-2")["match_score"] > 0
    assert repo.rescore_stale(conn) == 0


# --- upsert rules ------------------------------------------------------------


def test_new_job_inserted_with_scores(conn: sqlite3.Connection, profile: Profile) -> None:
    run_id = start_run(conn)
    assert upsert(conn, profile, run_id=run_id) == "new"
    job = row(conn, "li-1")
    assert job["first_seen_run_id"] == run_id == job["last_seen_run_id"]
    assert job["city_norm"] == "bengaluru"
    assert job["dedupe_key"] == "acme|backend developer|bengaluru"
    assert job["role_track"] == "backend_heavy"
    assert json.loads(job["matched_skills"])
    assert job["score_key"] == f"1:{matcher.MATCHER_VERSION}"
    raw = json.loads(job["raw_json"])
    assert raw["company_url"] == "https://acme.example"


def test_existing_job_keeps_first_seen(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile, run_id=None)
    assert upsert(conn, profile, run_id=None, at=timedelta(days=2), title="Backend Developer") == (
        "updated"
    )
    job = row(conn, "li-1")
    assert job["first_seen_at"] == "2026-10-01T09:00:00.000+00:00"
    assert job["last_seen_at"] == "2026-10-03T09:00:00.000+00:00"


def test_empty_description_never_blanks_existing(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    p = profile.model_copy(update={"max_experience_years": 3})
    upsert(conn, p, description="Node.js and Redis. 3+ yrs experience")
    upsert(conn, p, at=timedelta(hours=1), description=None)
    job = row(conn, "li-1")
    assert job["description"] == "Node.js and Redis. 3+ yrs experience"
    assert job["is_senior"] == 1  # scored with the kept description
    upsert(conn, p, at=timedelta(hours=2), description="New text with Node.js")
    assert row(conn, "li-1")["description"] == "New text with Node.js"


def test_cross_board_copy_linked_as_duplicate(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile)
    outcome = upsert(
        conn,
        profile,
        at=timedelta(hours=1),
        id="nk-1",
        site="naukri",
        job_url="https://naukri.com/1",
        company="ACME Private Limited",
        location="Bengaluru",
    )
    assert outcome == "duplicate"
    assert row(conn, "nk-1")["duplicate_of"] == "li-1"
    assert listed(conn) == ["li-1"]
    # updating the duplicate keeps the link
    upsert(conn, profile, at=timedelta(hours=2), id="nk-1", site="naukri", location="Bengaluru")
    assert row(conn, "nk-1")["duplicate_of"] == "li-1"


def test_weak_dedupe_key_not_linked(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile, company=None)
    assert upsert(conn, profile, id="li-2", company=None) == "new"


def test_upsert_jobs_counts(conn: sqlite3.Connection, profile: Profile) -> None:
    jobs = [
        make_job(id="li-1"),
        make_job(id="li-2", title="Full Stack Developer"),
        make_job(id="nk-1", site="naukri"),
    ]
    counts = repo.upsert_jobs(conn, jobs, profile, 1, run_id=None, now=T0)
    assert (counts.new, counts.updated, counts.duplicates) == (2, 0, 1)
    again = repo.upsert_jobs(conn, jobs[:1], profile, 1, now=T0)
    assert (again.new, again.updated) == (0, 1)


# --- listing -----------------------------------------------------------------


@pytest.fixture
def catalog(conn: sqlite3.Connection, profile: Profile) -> sqlite3.Connection:
    upsert(conn, profile, id="li-backend", title="Backend Developer", date_posted="2026-09-30")
    upsert(
        conn,
        profile,
        id="li-senior",
        title="Senior Backend Engineer",
        company="Beta",
        date_posted="2026-10-01",
    )
    upsert(
        conn,
        profile,
        id="li-intern",
        title="Backend Intern",
        company="Gamma",
        location="Pune, India",
    )
    upsert(
        conn,
        profile,
        id="nk-fullstack",
        site="naukri",
        title="Full Stack Developer",
        company="Delta",
        location="Gurugram, Haryana",
        description="React and Node.js, 2-4 years experience",
        experience_range="2-4 Yrs",
        min_amount=600000,
        max_amount=900000,
        currency="INR",
        interval="yearly",
        date_posted=None,
    )
    upsert(
        conn,
        profile,
        at=timedelta(minutes=5),
        id="li-remote",
        title="AI Engineer",
        company="Epsilon",
        location="Pune, India",
        is_remote=True,
        description="LLM and RAG with Python. 0-1 years experience",
        min_amount=50000,
        max_amount=80000,
        currency="INR",
        interval="monthly",
    )
    upsert(
        conn,
        profile,
        id="gd-copy",
        site="glassdoor",
        job_url="https://glassdoor/1",
        title="Backend Developer",
        location="Bengaluru",
    )
    return conn


def test_duplicates_never_listed_and_totals(catalog: sqlite3.Connection) -> None:
    result = repo.list_jobs(catalog, JobFilters(exclude_senior=False))
    ids = [job["id"] for job in result["jobs"]]  # type: ignore[index]
    assert "gd-copy" not in ids
    assert result["total"] == 5
    assert result["total_unfiltered"] == 5
    assert result["page"] == 1 and result["page_size"] == 50


def test_exclude_senior_hides_senior_and_intern(catalog: sqlite3.Connection) -> None:
    default = set(listed(catalog))
    assert default == {"li-backend", "nk-fullstack", "li-remote"}
    assert {"li-senior", "li-intern"} <= set(listed(catalog, exclude_senior=False))


def test_search_filter(catalog: sqlite3.Connection) -> None:
    assert listed(catalog, search="delta") == ["nk-fullstack"]
    assert listed(catalog, search="FULL stack") == ["nk-fullstack"]
    assert listed(catalog, search="rag") == ["li-remote"]  # description
    assert set(listed(catalog, search="node.js")) == {"li-backend", "nk-fullstack"}
    assert listed(catalog, search="zzz") == []


def test_role_track_filter(catalog: sqlite3.Connection) -> None:
    assert listed(catalog, role_track="fullstack") == ["nk-fullstack"]
    assert listed(catalog, role_track="genai") == ["li-remote"]
    assert listed(catalog, role_track="other") == []
    assert len(listed(catalog, role_track="all")) == 3


def test_location_filter_is_alias_aware(catalog: sqlite3.Connection) -> None:
    assert listed(catalog, location="Bangalore") == ["li-backend"]
    assert listed(catalog, location="bengaluru") == ["li-backend"]
    assert listed(catalog, location="Gurgaon") == ["nk-fullstack"]
    assert listed(catalog, location="guru") == ["nk-fullstack"]
    assert listed(catalog, location="Remote") == ["li-remote"]
    assert len(listed(catalog, location="all")) == 3


def test_sites_filter_matches_duplicates(catalog: sqlite3.Connection) -> None:
    assert listed(catalog, sites="glassdoor") == ["li-backend"]
    assert listed(catalog, sites="naukri") == ["nk-fullstack"]
    assert set(listed(catalog, sites="naukri, LinkedIn")) == {
        "li-backend",
        "nk-fullstack",
        "li-remote",
    }
    assert listed(catalog, sites="") == []
    assert listed(catalog, sites=",") == []
    assert len(listed(catalog, sites="all")) == 3
    assert len(listed(catalog, sites=None)) == 3


def test_min_score_and_remote_only(catalog: sqlite3.Connection) -> None:
    scores = {
        j["id"]: j["match_score"]
        for j in repo.list_jobs(catalog, JobFilters())["jobs"]  # type: ignore[union-attr]
    }
    threshold = max(scores.values())
    assert listed(catalog, min_score=threshold) == [
        k for k, v in scores.items() if v >= threshold
    ]
    assert listed(catalog, remote_only=True) == ["li-remote"]


def test_status_filter(catalog: sqlite3.Connection) -> None:
    assert repo.set_application(catalog, "li-backend", "applied", "sent CV")
    assert repo.set_application(catalog, "li-remote", "saved")
    assert listed(catalog, status="applied") == ["li-backend"]
    assert listed(catalog, status="saved") == ["li-remote"]
    assert listed(catalog, status="none") == ["nk-fullstack"]
    assert listed(catalog, status="rejected") == []


def test_sorting(catalog: sqlite3.Connection) -> None:
    by_score = repo.list_jobs(catalog, JobFilters())["jobs"]
    scores = [j["match_score"] for j in by_score]  # type: ignore[union-attr]
    assert scores == sorted(scores, reverse=True)
    assert listed(catalog, sort_by="exp_asc") == ["li-remote", "li-backend", "nk-fullstack"]
    assert listed(catalog, sort_by="exp_desc") == ["nk-fullstack", "li-backend", "li-remote"]
    assert listed(catalog, sort_by="date") == ["li-remote", "li-backend", "nk-fullstack"]
    # monthly 80K -> 960K/yr beats yearly 900K
    assert listed(catalog, sort_by="salary") == ["li-remote", "nk-fullstack", "li-backend"]
    assert listed(catalog, sort_by="found")[0] == "li-remote"
    assert listed(catalog, sort_by="bogus") == listed(catalog)


def test_pagination(catalog: sqlite3.Connection) -> None:
    all_ids = listed(catalog, exclude_senior=False)
    page1 = repo.list_jobs(catalog, JobFilters(exclude_senior=False, page_size=2))
    page3 = repo.list_jobs(catalog, JobFilters(exclude_senior=False, page_size=2, page=3))
    assert [j["id"] for j in page1["jobs"]] == all_ids[:2]  # type: ignore[union-attr]
    assert [j["id"] for j in page3["jobs"]] == all_ids[4:]  # type: ignore[union-attr]
    assert page1["total"] == 5
    clamped = repo.list_jobs(catalog, JobFilters(page=0, page_size=1000))
    assert (clamped["page"], clamped["page_size"]) == (1, 200)


def test_summary_shape(catalog: sqlite3.Connection) -> None:
    repo.set_application(catalog, "li-backend", "interviewing", "round 2")
    jobs = {j["id"]: j for j in repo.list_jobs(catalog, JobFilters())["jobs"]}  # type: ignore[union-attr]
    job = jobs["li-backend"]
    assert set(job) == {
        "id", "site", "title", "company", "location", "job_url", "job_url_direct",
        "date_posted", "is_remote", "salary_display", "match_score", "matched_skills",
        "missing_skills", "backend_skills", "ai_skills", "role_track",
        "experience_required", "min_experience", "max_experience", "is_senior",
        "is_intern", "senior_reason", "first_seen_at", "last_seen_at", "is_new",
        "also_on", "app_status", "app_notes", "description_snippet",
    }  # fmt: skip
    assert job["also_on"] == [{"site": "glassdoor", "job_url": "https://glassdoor/1"}]
    assert job["app_status"] == "interviewing" and job["app_notes"] == "round 2"
    assert job["is_remote"] is False and job["is_senior"] is False
    assert isinstance(job["matched_skills"], list)
    assert jobs["nk-fullstack"]["app_status"] == "none"
    assert jobs["nk-fullstack"]["app_notes"] == ""
    assert jobs["nk-fullstack"]["salary_display"] == "₹6L – ₹9L /yr"
    assert jobs["nk-fullstack"]["experience_required"] == "2-4 Yrs"
    assert jobs["li-remote"]["salary_display"] == "₹50K – ₹80K /mo"
    assert jobs["li-remote"]["description_snippet"].startswith("LLM and RAG")  # type: ignore[union-attr]


def test_also_on_ignores_same_site_reposts(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile)
    upsert(conn, profile, id="li-2", job_url="https://www.linkedin.com/jobs/view/2")
    assert row(conn, "li-2")["duplicate_of"] == "li-1"
    job = repo.list_jobs(conn)["jobs"][0]  # type: ignore[index]
    assert job["also_on"] == []


def test_description_snippet_limit(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile, description="**Node.js** " * 200)
    snippet = repo.list_jobs(conn)["jobs"][0]["description_snippet"]  # type: ignore[index]
    assert len(snippet) <= 300 and "*" not in snippet


# --- is_new ------------------------------------------------------------------


def test_is_new_rules(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile, id="imported", run_id=None, at=timedelta(days=5), company="Imp")
    run1 = start_run(conn, timedelta(hours=1))
    upsert(conn, profile, id="from-run1", run_id=run1, at=timedelta(hours=1, minutes=1), company="R1")
    repo.finish_run(conn, run1, "completed", now=T0 + timedelta(hours=2))
    flags = {j["id"]: j["is_new"] for j in repo.list_jobs(conn)["jobs"]}  # type: ignore[union-attr]
    assert flags == {"imported": False, "from-run1": True}
    assert listed(conn, new_only=True) == ["from-run1"]
    assert repo.get_stats(conn)["new_count"] == 1

    run2 = start_run(conn, timedelta(hours=3))
    assert listed(conn, new_only=True) == []  # a newer running run resets "new"
    upsert(conn, profile, id="from-run2", run_id=run2, at=timedelta(hours=3, minutes=1), company="R2")
    repo.finish_run(conn, run2, "failed", now=T0 + timedelta(hours=4))
    # a failed run is not eligible, so "new" is still measured from run1's start
    assert set(listed(conn, new_only=True)) == {"from-run1", "from-run2"}


def test_interrupted_run_does_not_reset_new(conn: sqlite3.Connection, profile: Profile) -> None:
    run1 = start_run(conn)
    upsert(conn, profile, run_id=run1, at=timedelta(minutes=1))
    repo.finish_run(conn, run1, "partial", now=T0 + timedelta(minutes=5))
    run2 = start_run(conn, timedelta(hours=1))
    repo.finish_run(conn, run2, "interrupted", now=T0 + timedelta(hours=2))
    assert listed(conn, new_only=True) == ["li-1"]


def test_new_count_includes_senior_but_not_duplicates(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    run1 = start_run(conn)
    upsert(conn, profile, id="li-mid", run_id=run1, at=timedelta(minutes=1), company="A")
    upsert(conn, profile, id="li-sr", run_id=run1, at=timedelta(minutes=1), company="B",
           title="Senior Backend Engineer")  # fmt: skip
    upsert(conn, profile, id="nk-mid", run_id=run1, at=timedelta(minutes=1), company="A",
           site="naukri")  # fmt: skip
    repo.finish_run(conn, run1, "completed", now=T0 + timedelta(minutes=5))
    assert row(conn, "nk-mid")["duplicate_of"] == "li-mid"
    assert repo.get_stats(conn)["new_count"] == 2


# --- detail, applications ----------------------------------------------------


def test_get_job_detail(catalog: sqlite3.Connection) -> None:
    detail = repo.get_job(catalog, "nk-fullstack")
    assert detail is not None
    assert detail["description"] == "React and Node.js, 2-4 years experience"
    assert detail["experience_range"] == "2-4 Yrs"
    assert (detail["min_amount"], detail["max_amount"]) == (600000, 900000)
    assert (detail["currency"], detail["interval"]) == ("INR", "yearly")
    assert detail["company_url"] == "https://acme.example"
    assert "job_type" in detail and "description_snippet" in detail
    assert repo.get_job(catalog, "missing") is None


def test_get_job_detail_of_duplicate_lists_primary(catalog: sqlite3.Connection) -> None:
    detail = repo.get_job(catalog, "gd-copy")
    assert detail is not None
    assert detail["also_on"] == [
        {"site": "linkedin", "job_url": "https://www.linkedin.com/jobs/view/1"}
    ]


def test_applications(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile)
    assert repo.set_application(conn, "li-1", "saved", "look later")
    assert repo.set_application(conn, "li-1", "applied")  # notes omitted -> kept
    app = repo.get_application(conn, "li-1")
    assert app is not None and app["status"] == "applied" and app["notes"] == "look later"
    assert repo.set_application(conn, "li-1", "applied", "")
    assert repo.get_application(conn, "li-1")["notes"] == ""  # type: ignore[index]
    assert repo.set_application(conn, "li-1", "none")
    assert repo.get_application(conn, "li-1") is None
    assert repo.set_application(conn, "nope", "saved") is False
    assert repo.set_application(conn, "nope", "none") is False
    with pytest.raises(ValueError):
        repo.set_application(conn, "li-1", "archived")


def test_rescrape_never_touches_application(conn: sqlite3.Connection, profile: Profile) -> None:
    upsert(conn, profile)
    repo.set_application(conn, "li-1", "applied", "notes")
    upsert(conn, profile, at=timedelta(days=1), title="Backend Developer II")
    assert repo.get_application(conn, "li-1")["notes"] == "notes"  # type: ignore[index]


# --- stats & pruning ---------------------------------------------------------


def test_stats(catalog: sqlite3.Connection) -> None:
    repo.set_application(catalog, "li-backend", "applied")
    repo.set_application(catalog, "li-senior", "rejected")
    stats = repo.get_stats(catalog)
    assert stats["total_jobs"] == 3
    assert stats["total_stored"] == 6
    assert stats["applied_count"] == 1 and stats["rejected_count"] == 1
    assert stats["saved_count"] == 0 and stats["interviewing_count"] == 0
    assert stats["platform_distribution"] == {"linkedin": 2, "naukri": 1}
    names = [s["name"] for s in stats["top_skills"]]  # type: ignore[union-attr]
    assert "Node.js" in names
    assert stats["top_companies"][0]["count"] >= 1  # type: ignore[index]
    assert stats["last_scrape_at"] is None
    assert stats["new_count"] == 0
    assert isinstance(stats["high_match_count"], int)


def test_stats_last_scrape(conn: sqlite3.Connection) -> None:
    run_id = start_run(conn)
    repo.finish_run(conn, run_id, "completed", now=T0 + timedelta(minutes=3))
    assert repo.get_stats(conn)["last_scrape_at"] == "2026-10-01T09:03:00.000+00:00"


def test_prune_removes_stale_groups_but_keeps_applications(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    upsert(conn, profile, id="old")
    upsert(conn, profile, id="old-dup", site="naukri")
    upsert(conn, profile, id="old-applied", company="Kept")
    upsert(conn, profile, id="old-group", company="Group")
    upsert(conn, profile, id="fresh-dup", company="Group", site="naukri", at=timedelta(days=40))
    upsert(conn, profile, id="fresh", company="Fresh", at=timedelta(days=40))
    repo.set_application(conn, "old-applied", "saved")
    assert row(conn, "old-dup")["duplicate_of"] == "old"
    assert row(conn, "fresh-dup")["duplicate_of"] == "old-group"

    deleted = repo.prune_jobs(conn, retention_days=30, now=T0 + timedelta(days=41))
    remaining = {r["id"] for r in conn.execute("SELECT id FROM jobs")}
    assert deleted == 2
    assert remaining == {"old-applied", "old-group", "fresh-dup", "fresh"}


def test_prune_keeps_group_when_duplicate_has_application(
    conn: sqlite3.Connection, profile: Profile
) -> None:
    upsert(conn, profile, id="p")
    upsert(conn, profile, id="d", site="naukri")
    repo.set_application(conn, "d", "applied")
    assert repo.prune_jobs(conn, 30, now=T0 + timedelta(days=60)) == 0
    assert repo.count_jobs(conn) == 2
