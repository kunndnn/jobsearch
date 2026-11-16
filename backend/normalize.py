from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum

import numpy as np
import pandas as pd

CITY_ALIASES: dict[str, str] = {
    "bangalore": "bengaluru",
    "gurugram": "gurgaon",
    "bombay": "mumbai",
    "new delhi": "delhi",
    "sas nagar": "mohali",
    "s.a.s. nagar": "mohali",
    "s.a.s nagar": "mohali",
    "sahibzada ajit singh nagar": "mohali",
}
LEGAL_SUFFIXES = frozenset({"pvt", "private", "ltd", "limited", "inc", "llp", "llc", "corp"})
JOB_FIELDS: tuple[str, ...] = (
    "id",
    "site",
    "title",
    "company",
    "location",
    "city_norm",
    "dedupe_key",
    "job_url",
    "job_url_direct",
    "date_posted",
    "is_remote",
    "job_type",
    "description",
    "min_amount",
    "max_amount",
    "currency",
    "interval",
    "experience_range",
)
# Keys written by the old dashboard's matcher; not part of a jobspy record.
LEGACY_SCORE_KEYS = frozenset(
    {
        "match_score",
        "matched_skills",
        "backend_skills",
        "ai_skills",
        "missing_skills",
        "role_track",
        "experience_required",
        "min_experience",
        "max_experience",
        "is_senior",
        "is_intern",
        "senior_reason",
        "salary_display",
    }
)
SITE_PREFIXES = {"linkedin": "li", "naukri": "nk", "indeed": "in", "glassdoor": "gd"}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    """Fixed-width UTC timestamp, so ISO strings also compare correctly in SQL."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def utc_now_iso() -> str:
    return iso(utc_now())


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_missing(value: object) -> bool:
    if value is None or value is pd.NaT or value is pd.NA:
        return True
    if isinstance(value, (float, np.floating)):
        return not math.isfinite(float(value))
    return False


def json_safe(value: object) -> object:
    """Convert pandas/numpy/date values into plain JSON-compatible Python values."""
    if is_missing(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating, Decimal)):
        return float(value)
    if isinstance(value, str):
        return value
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset, np.ndarray)):
        return [json_safe(v) for v in value]
    return str(value)


def json_safe_record(record: Mapping[str, object]) -> dict[str, object]:
    return {str(key): json_safe(value) for key, value in record.items()}


def dataframe_to_records(df: pd.DataFrame) -> list[dict[str, object]]:
    if df is None or df.empty:
        return []
    return [json_safe_record(row) for row in df.to_dict(orient="records")]


def clean_str(value: object) -> str:
    if is_missing(value):
        return ""
    return str(value).strip()


def optional_str(value: object) -> str | None:
    return clean_str(value) or None


def optional_float(value: object) -> float | None:
    if is_missing(value) or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def optional_bool(value: object) -> bool | None:
    if is_missing(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def to_iso_date(value: object) -> str | None:
    if is_missing(value):
        return None
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    match = re.match(r"\s*(\d{4}-\d{2}-\d{2})", str(value))
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1)).isoformat()
    except ValueError:
        return None


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def canonical_city(name: str) -> str:
    key = _collapse(name.lower())
    return CITY_ALIASES.get(key, key)


def normalize_city(location: object, is_remote: object = None) -> str:
    if optional_bool(is_remote):
        return "remote"
    first = clean_str(location).split(",")[0]
    return canonical_city(first)


def city_variants(name: str) -> list[str]:
    """All spellings of a city for alias-aware filtering, canonical first."""
    canonical = canonical_city(name)
    if not canonical:
        return []
    aliases = [alias for alias, target in CITY_ALIASES.items() if target == canonical]
    return [canonical, *[a for a in aliases if a != canonical]]


def normalize_company(company: object) -> str:
    words = re.sub(r"[^\w\s]", " ", clean_str(company).lower()).split()
    stripped = list(words)
    while stripped and stripped[-1] in LEGAL_SUFFIXES:
        stripped.pop()
    return " ".join(stripped or words)


def normalize_title(title: object) -> str:
    text = clean_str(title).lower()
    for token, word in (("c#", " csharp "), ("c++", " cpp "), (".net", " dotnet ")):
        text = text.replace(token, word)
    return _collapse(re.sub(r"[^\w\s]", " ", text))


def dedupe_key(company: object, title: object, city_norm: str) -> str:
    return f"{normalize_company(company)}|{normalize_title(title)}|{city_norm}"


def fallback_job_id(record: Mapping[str, object]) -> str:
    site = clean_str(record.get("site")).lower()
    basis = clean_str(record.get("job_url")) or "|".join(
        clean_str(record.get(k)) for k in ("site", "company", "title", "location")
    )
    digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
    return f"{SITE_PREFIXES.get(site, 'x')}-{digest}"


def normalize_job(record: Mapping[str, object]) -> dict[str, object]:
    """jobspy (or legacy) record -> dict with JOB_FIELDS plus `raw` (JSON-safe)."""
    raw = {
        key: value
        for key, value in json_safe_record(record).items()
        if key not in LEGACY_SCORE_KEYS
    }
    is_remote = optional_bool(raw.get("is_remote"))
    location = optional_str(raw.get("location"))
    city = normalize_city(location, is_remote)
    interval = optional_str(raw.get("interval"))
    job = {
        "id": optional_str(raw.get("id")) or fallback_job_id(raw),
        "site": clean_str(raw.get("site")).lower() or None,
        "title": clean_str(raw.get("title")),
        "company": optional_str(raw.get("company")),
        "location": location,
        "city_norm": city,
        "dedupe_key": dedupe_key(raw.get("company"), raw.get("title"), city),
        "job_url": optional_str(raw.get("job_url")),
        "job_url_direct": optional_str(raw.get("job_url_direct")),
        "date_posted": to_iso_date(raw.get("date_posted")),
        "is_remote": is_remote,
        "job_type": optional_str(raw.get("job_type")),
        "description": optional_str(raw.get("description")),
        "min_amount": optional_float(raw.get("min_amount")),
        "max_amount": optional_float(raw.get("max_amount")),
        "currency": optional_str(raw.get("currency")),
        "interval": interval.lower() if interval else None,
        "experience_range": optional_str(raw.get("experience_range")),
    }
    job["raw"] = raw
    return job


def normalize_jobs(records: Iterable[Mapping[str, object]]) -> list[dict[str, object]]:
    return [normalize_job(record) for record in records]


def dataframe_to_jobs(df: pd.DataFrame) -> list[dict[str, object]]:
    """scrape_jobs() DataFrame -> normalised jobs ready for repo.upsert_jobs."""
    return normalize_jobs(dataframe_to_records(df))


_MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_MD_MARKUP = re.compile(r"(`{1,3}|\*{1,3}|_{2,3}|~~|^\s{0,3}#{1,6}\s*|^\s*>\s?|^\s*[-*+]\s+)", re.M)
_HTML_TAG = re.compile(r"<[^>]+>")


def markdown_to_text(markdown: object) -> str:
    text = clean_str(markdown)
    text = _MD_LINK.sub(r"\1", text)
    text = _HTML_TAG.sub(" ", text)
    text = _MD_MARKUP.sub("", text)
    text = text.replace("\\", "")
    return _collapse(text)


def plain_text_snippet(markdown: object, limit: int = 300) -> str:
    text = markdown_to_text(markdown)
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0] or text[: limit - 1]
    return cut.rstrip(" ,.;:-") + "…"
