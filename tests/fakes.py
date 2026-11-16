"""Test doubles for the scrape engine: a fake jobspy.scrape_jobs (no network)."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable

import pandas as pd

from backend import db, repo
from backend.models import ScrapeSettings

JOB_COLUMNS = [
    "id", "site", "job_url", "job_url_direct", "title", "company", "location",
    "date_posted", "job_type", "salary_source", "interval", "min_amount", "max_amount",
    "currency", "is_remote", "job_level", "job_function", "listing_type", "emails",
    "description", "company_industry", "company_url", "company_logo",
    "company_url_direct", "company_addresses", "company_num_employees",
    "company_revenue", "company_description", "skills", "experience_range",
    "company_rating", "company_reviews_count", "vacancy_count", "work_from_home_type",
]  # fmt: skip
PREFIX = {"linkedin": "li", "naukri": "nk", "indeed": "in", "glassdoor": "gd"}
DESCRIPTION = "Build REST APIs with **Node.js**, TypeScript and PostgreSQL. 1-3 years experience."


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def job_row(site: str, job_id: str, title: str, company: str, location: str, **extra: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(JOB_COLUMNS)
    row.update(
        id=job_id,
        site=site,
        job_url=f"https://{site}.example/jobs/{job_id}",
        title=title,
        company=company,
        location=location,
        date_posted="2026-10-15",
        min_amount=float("nan"),  # jobspy frames carry NaN for missing numbers
        max_amount=float("nan"),
        is_remote=False,
        description=DESCRIPTION,
        company_url=f"https://{slug(company)}.example",
    )
    row.update(extra)
    return row


def default_rows(site: str, call: dict[str, object]) -> list[dict[str, object]]:
    term = str(call["search_term"]).split('"')[1] if '"' in str(call["search_term"]) else str(call["search_term"])
    location = str(call["location"])
    job_id = f"{PREFIX.get(site, 'x')}-{slug(term)}-{slug(location)}"
    return [job_row(site, job_id, term, f"{site.title()} {slug(term)} Labs", f"{location}, India")]


class FakeBoards:
    """Callable stand-in for scrape_jobs(**kwargs) returning fixture DataFrames.

    - rows(site, call) -> records for one board (default: one job per query)
    - errors: {site: message} reported in df.attrs["errors"] (that board returns nothing)
    - before_call(call): hook to block, cancel or raise
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.rows: Callable[[str, dict[str, object]], list[dict[str, object]]] = default_rows
        self.errors: dict[str, str] = {}
        self.before_call: Callable[[dict[str, object]], None] | None = None
        self.lock = threading.Lock()

    def __call__(self, **kwargs: object) -> pd.DataFrame:
        with self.lock:
            self.calls.append(kwargs)
        if self.before_call is not None:
            self.before_call(kwargs)
        sites = [str(site) for site in kwargs["site_name"]]  # type: ignore[attr-defined]
        records = [
            row for site in sites if site not in self.errors for row in self.rows(site, kwargs)
        ]
        frame = pd.DataFrame(records, columns=JOB_COLUMNS)
        frame.attrs = {
            "errors": {site: msg for site, msg in self.errors.items() if site in sites},
            "warnings": {},
        }
        return frame

    def searched(self) -> list[tuple[tuple[str, ...], str, str]]:
        return [
            (tuple(call["site_name"]), str(call["search_term"]), str(call["location"]))  # type: ignore[arg-type]
            for call in self.calls
        ]


def save_settings(**overrides: object) -> ScrapeSettings:
    """Store fast test settings (no delays) and return them."""
    values: dict[str, object] = {
        "search_terms": ["Backend Developer"],
        "locations": ["Bengaluru"],
        "sites": ["linkedin"],
        "delay_min_seconds": 0,
        "delay_max_seconds": 0,
    }
    values.update(overrides)
    settings = ScrapeSettings.model_validate(values)
    with db.connection() as conn:
        repo.save_scrape_settings(conn, settings)
    return settings
