from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from backend import config
from backend.models import (
    DEFAULT_TITLE_TOKENS,
    JobStatusUpdate,
    Profile,
    ScheduleSettings,
    ScrapeOverrides,
    ScrapeSettings,
    TargetTrack,
    apply_overrides,
    load_lenient,
    load_profile_template,
    merge_partial,
)


def test_template_profile_has_spec_fields_and_track_order() -> None:
    raw = json.loads(config.PROFILE_TEMPLATE_PATH.read_text(encoding="utf-8"))
    assert raw["headline"]
    assert raw["max_experience_years"] == 5
    profile = load_profile_template()
    assert [t.id for t in profile.target_tracks] == ["genai", "backend_heavy", "fullstack", "sde"]
    for track in profile.target_tracks:
        assert track.title_tokens == DEFAULT_TITLE_TOKENS[track.id]
    assert profile.name == "Kundan"
    assert "core_backend" in profile.skills


def test_profile_defaults_for_missing_fields() -> None:
    profile = Profile.model_validate({"name": "Test"})
    assert profile.max_experience_years == 3
    assert profile.headline == ""
    assert [t.id for t in profile.target_tracks] == ["genai", "backend_heavy", "fullstack", "sde"]
    assert set(profile.skills) == {"core_backend", "ai_llm", "frontend", "devops_cloud"}


def test_string_lists_trimmed_deduped_case_insensitive() -> None:
    profile = Profile.model_validate(
        {
            "name": "  A  ",
            "preferred_locations": [" Pune ", "pune", "", "  ", "Mumbai"],
            "skills": {"core_backend": ["Node.js", "node.js", " Redis "], " extra ": ["X"]},
            "disqualifiers": ["PHP", "php"],
        }
    )
    assert profile.name == "A"
    assert profile.preferred_locations == ["Pune", "Mumbai"]
    assert profile.skills["core_backend"] == ["Node.js", "Redis"]
    assert profile.skills["extra"] == ["X"]
    assert profile.disqualifiers == ["PHP"]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},
        {"name": "   "},
        {},
        {"name": "A", "experience_years": -1},
        {"name": "A", "max_experience_years": 0},
        {"name": "A", "preferred_locations": [1, 2]},
        {"name": "A", "skills": {"": ["x"]}},
        {"name": "A", "target_tracks": [{"id": "all"}]},
        {"name": "A", "target_tracks": [{"id": "bad id"}]},
        {"name": "A", "target_tracks": [{"id": "x"}, {"id": "x"}]},
    ],
)
def test_invalid_profiles(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Profile.model_validate(payload)


def test_track_title_tokens_fallbacks() -> None:
    assert TargetTrack(id="genai").title_tokens == DEFAULT_TITLE_TOKENS["genai"]
    custom = TargetTrack(id="devops", keywords=["DevOps Engineer", "SRE"])
    assert custom.title_tokens == ["devops engineer", "sre"]
    assert custom.title == "devops"
    assert TargetTrack(id="x", title_tokens=[]).title_tokens == []


def test_scrape_settings_defaults() -> None:
    settings = ScrapeSettings()
    assert settings.search_terms == [
        "Full Stack Developer",
        "Node.js Developer",
        "Backend Developer",
        "Python Developer",
        "React Developer",
        "Laravel Developer",
    ]
    assert settings.locations[0] == "Chandigarh" and settings.locations[-1] == "Remote"
    assert settings.sites == ["linkedin", "naukri"]
    assert (settings.results_per_query, settings.hours_old) == (10, 72)
    assert settings.fetch_description is True
    assert settings.country_indeed == "india"
    assert settings.exclude_terms == ["senior", "lead", "principal", "architect", "staff", "sr"]
    assert settings.retention_days == 30
    assert (settings.delay_min_seconds, settings.delay_max_seconds) == (3, 8)


@pytest.mark.parametrize(
    "partial",
    [
        {"search_terms": []},
        {"search_terms": ["  "]},
        {"locations": []},
        {"sites": []},
        {"sites": ["monster"]},
        {"results_per_query": 0},
        {"results_per_query": 51},
        {"hours_old": 721},
        {"retention_days": 0},
        {"retention_days": 366},
        {"delay_min_seconds": 9, "delay_max_seconds": 8},
        {"delay_max_seconds": 121},
        {"unknown_field": 1},
    ],
)
def test_invalid_scrape_settings(partial: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        merge_partial(ScrapeSettings(), partial)


def test_merge_partial_keeps_other_fields() -> None:
    merged = merge_partial(ScrapeSettings(), {"sites": ["LinkedIn", "indeed"], "hours_old": 24})
    assert merged.sites == ["linkedin", "indeed"]
    assert merged.hours_old == 24
    assert merged.locations == ScrapeSettings().locations


def test_schedule_interval_bounds(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ScheduleSettings().interval_minutes == 360
    with pytest.raises(ValidationError):
        ScheduleSettings(interval_minutes=14)
    with pytest.raises(ValidationError):
        ScheduleSettings(interval_minutes=10081)
    assert ScheduleSettings(interval_minutes=15).interval_minutes == 15
    monkeypatch.setenv("JOBSPY_MIN_INTERVAL_MINUTES", "1")
    assert config.min_interval_minutes() == 1
    assert ScheduleSettings(interval_minutes=1).interval_minutes == 1


def test_overrides_apply_and_validate() -> None:
    overrides = ScrapeOverrides(search_terms=["Go Developer"], results_per_query=3)
    applied = apply_overrides(ScrapeSettings(), overrides)
    assert applied.search_terms == ["Go Developer"]
    assert applied.results_per_query == 3
    assert applied.sites == ["linkedin", "naukri"]
    assert apply_overrides(ScrapeSettings(), None) == ScrapeSettings()
    with pytest.raises(ValidationError):
        apply_overrides(ScrapeSettings(), ScrapeOverrides(sites=["nope"]))
    with pytest.raises(ValidationError):
        ScrapeOverrides(results_per_query=0)


def test_load_lenient_drops_bad_fields() -> None:
    stored = {"enabled": True, "interval_minutes": 5, "run_on_startup": True, "old": 1}
    loaded = load_lenient(ScheduleSettings, stored)
    assert loaded.enabled is True and loaded.run_on_startup is True
    assert loaded.interval_minutes == 360
    assert load_lenient(ScrapeSettings, None) == ScrapeSettings()


def test_job_status_update() -> None:
    assert JobStatusUpdate(status="applied").notes is None
    with pytest.raises(ValidationError):
        JobStatusUpdate(status="archived")  # type: ignore[arg-type]
