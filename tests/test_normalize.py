from __future__ import annotations

import json
import math
from datetime import date, datetime, timezone
from enum import Enum

import numpy as np
import pandas as pd
import pytest

from backend import normalize


class Color(Enum):
    RED = "red"


def test_json_safe_converts_pandas_and_numpy_values() -> None:
    record = {
        "nan": float("nan"),
        "inf": float("inf"),
        "np_nan": np.float64("nan"),
        "nat": pd.NaT,
        "na": pd.NA,
        "np_int": np.int64(7),
        "np_float": np.float32(1.5),
        "np_bool": np.bool_(True),
        "day": date(2026, 10, 1),
        "ts": pd.Timestamp("2026-10-01T10:00:00Z"),
        "dt": datetime(2026, 10, 1, tzinfo=timezone.utc),
        "enum": Color.RED,
        "nested": {"x": [float("nan"), np.int32(2)]},
        "arr": np.array([1, 2]),
    }
    safe = normalize.json_safe_record(record)
    assert safe["nan"] is None and safe["inf"] is None and safe["np_nan"] is None
    assert safe["nat"] is None and safe["na"] is None
    assert safe["np_int"] == 7 and isinstance(safe["np_int"], int)
    assert safe["np_float"] == 1.5 and isinstance(safe["np_float"], float)
    assert safe["np_bool"] is True
    assert safe["day"] == "2026-10-01"
    assert str(safe["ts"]).startswith("2026-10-01T10:00:00")
    assert safe["enum"] == "red"
    assert safe["nested"] == {"x": [None, 2]}
    assert safe["arr"] == [1, 2]
    json.dumps(safe, allow_nan=False)


def test_dataframe_to_records() -> None:
    df = pd.DataFrame(
        [{"id": "a", "min_amount": np.nan, "date_posted": date(2026, 10, 2)}]
    )
    assert normalize.dataframe_to_records(df) == [
        {"id": "a", "min_amount": None, "date_posted": "2026-10-02"}
    ]
    assert normalize.dataframe_to_records(pd.DataFrame()) == []


def test_dataframe_to_jobs_with_jobspy_columns() -> None:
    from jobspy.model import JobPost

    row = {name: None for name in JobPost.model_fields}
    row.update(
        id="li-42",
        site="linkedin",
        job_url="https://www.linkedin.com/jobs/view/42",
        title="Node.js Developer",
        company="Acme",
        location="Gurugram, Haryana, India",
        date_posted=date(2026, 10, 15),
        min_amount=np.nan,
        is_remote=False,
        interval="yearly",
    )
    df = pd.DataFrame([row], columns=list(JobPost.model_fields))
    [job] = normalize.dataframe_to_jobs(df)
    assert job["id"] == "li-42" and job["city_norm"] == "gurgaon"
    assert job["date_posted"] == "2026-10-15" and job["min_amount"] is None
    assert set(normalize.JOB_FIELDS) <= set(job)
    json.dumps(job, allow_nan=False)


@pytest.mark.parametrize(
    ("location", "remote", "expected"),
    [
        ("Bangalore, Karnataka, India", False, "bengaluru"),
        ("Bengaluru, Karnataka, India", None, "bengaluru"),
        ("Gurugram, Haryana", False, "gurgaon"),
        ("Bombay", False, "mumbai"),
        ("New  Delhi, India", False, "delhi"),
        ("Pune, Maharashtra", True, "remote"),
        ("Remote", False, "remote"),
        (None, None, ""),
        (float("nan"), None, ""),
    ],
)
def test_normalize_city(location: object, remote: object, expected: str) -> None:
    assert normalize.normalize_city(location, remote) == expected


def test_city_variants() -> None:
    assert normalize.city_variants("Bengaluru") == ["bengaluru", "bangalore"]
    assert normalize.city_variants("bangalore") == ["bengaluru", "bangalore"]
    assert normalize.city_variants("Pune") == ["pune"]
    assert normalize.city_variants(" ") == []


@pytest.mark.parametrize(
    ("company", "expected"),
    [
        ("Infosys Pvt. Ltd.", "infosys"),
        ("Acme Private Limited", "acme"),
        ("Foo Technologies, Inc.", "foo technologies"),
        ("Corp Solutions LLP", "corp solutions"),
        ("Private", "private"),
        (None, ""),
    ],
)
def test_normalize_company(company: object, expected: str) -> None:
    assert normalize.normalize_company(company) == expected


def test_normalize_title_keeps_language_identity() -> None:
    assert normalize.normalize_title("Sr. C# Developer (.NET)") == "sr csharp developer dotnet"
    assert normalize.normalize_title("C++ Developer") != normalize.normalize_title("C Developer")
    assert normalize.normalize_title("Node.js  Developer!") == "node js developer"


def test_dedupe_key_matches_across_boards() -> None:
    linkedin = normalize.normalize_job(
        {
            "id": "li-1",
            "site": "linkedin",
            "title": "Backend Developer",
            "company": "Acme Pvt Ltd",
            "location": "Bangalore, Karnataka, India",
        }
    )
    naukri = normalize.normalize_job(
        {
            "id": "nk-9",
            "site": "naukri",
            "title": "Backend  Developer",
            "company": "ACME Private Limited",
            "location": "Bengaluru",
        }
    )
    assert linkedin["dedupe_key"] == naukri["dedupe_key"] == "acme|backend developer|bengaluru"


def test_normalize_job_sanitises_legacy_record() -> None:
    record = {
        "id": "nk-1",
        "site": "Naukri",
        "job_url": "https://naukri.com/x",
        "title": " Java Developer ",
        "company": "Infosys",
        "location": "Pune, India",
        "date_posted": "2026-10-16",
        "is_remote": False,
        "description": float("nan"),
        "min_amount": float("nan"),
        "max_amount": 2500000.0,
        "currency": "INR",
        "interval": "Yearly",
        "experience_range": "2-5 Yrs",
        "company_url": float("nan"),
        "match_score": 72,
        "matched_skills": ["Java"],
    }
    job = normalize.normalize_job(record)
    assert job["site"] == "naukri"
    assert job["title"] == "Java Developer"
    assert job["description"] is None
    assert job["min_amount"] is None and job["max_amount"] == 2500000.0
    assert job["interval"] == "yearly"
    assert job["is_remote"] is False
    assert job["city_norm"] == "pune"
    raw = job["raw"]
    assert isinstance(raw, dict)
    assert "match_score" not in raw and "matched_skills" not in raw
    assert raw["company_url"] is None
    json.dumps(raw, allow_nan=False)


def test_normalize_job_generates_stable_id() -> None:
    record = {"site": "linkedin", "job_url": "https://x/1", "title": "Dev"}
    first = normalize.normalize_job(record)["id"]
    assert first == normalize.normalize_job(dict(record))["id"]
    assert str(first).startswith("li-")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2026, 1, 2), "2026-01-02"),
        (datetime(2026, 1, 2, 23, 0), "2026-01-02"),
        (pd.Timestamp("2026-01-02"), "2026-01-02"),
        ("2026-01-02T10:00:00", "2026-01-02"),
        ("yesterday", None),
        (pd.NaT, None),
        (float("nan"), None),
    ],
)
def test_to_iso_date(value: object, expected: str | None) -> None:
    assert normalize.to_iso_date(value) == expected


def test_iso_timestamps_are_fixed_width_utc() -> None:
    stamp = normalize.iso(datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc))
    assert stamp == "2026-10-01T09:00:00.000+00:00"
    naive = normalize.iso(datetime(2026, 10, 1, 9, 0))
    assert naive == stamp
    assert normalize.parse_iso(stamp) == datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    assert normalize.parse_iso("garbage") is None


def test_optional_helpers() -> None:
    assert normalize.optional_float("12.5") == 12.5
    assert normalize.optional_float(True) is None
    assert normalize.optional_float(float("inf")) is None
    assert normalize.optional_bool("yes") is True
    assert normalize.optional_bool(np.bool_(False)) is False
    assert normalize.optional_bool("maybe") is None
    assert normalize.clean_str(float("nan")) == ""
    assert math.isnan(float("nan"))


def test_plain_text_snippet() -> None:
    markdown = "## Role\n\n**Build** [APIs](https://x.y) with `Node.js`\n\n- item one\n- item two"
    assert normalize.markdown_to_text(markdown) == "Role Build APIs with Node.js item one item two"
    long = "word " * 200
    snippet = normalize.plain_text_snippet(long, 300)
    assert len(snippet) <= 300 and snippet.endswith("…")
    assert normalize.plain_text_snippet(None) == ""
