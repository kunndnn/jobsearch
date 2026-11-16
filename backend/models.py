from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, TypeVar

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from backend import config

STANDARD_SKILL_CATEGORIES: tuple[str, ...] = (
    "core_backend",
    "ai_llm",
    "frontend",
    "devops_cloud",
)
RESERVED_TRACK_IDS = frozenset({"all", "other"})

DEFAULT_TITLE_TOKENS: dict[str, list[str]] = {
    "genai": [
        "genai",
        "gen ai",
        "generative ai",
        "llm",
        "rag",
        "ai engineer",
        "ai developer",
        "machine learning",
    ],
    "backend_heavy": [
        "backend",
        "back-end",
        "back end",
        "node",
        "nodejs",
        "node.js",
        "server-side",
    ],
    "fullstack": ["full stack", "fullstack", "full-stack", "mern"],
    "sde": [
        "sde",
        "software engineer",
        "software development engineer",
        "software developer",
    ],
}


def clean_string_list(values: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for value in values:
        text = value.strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            cleaned.append(text)
    return cleaned


StrList = Annotated[list[str], AfterValidator(clean_string_list)]
NonEmptyStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


def _require_items(values: list[str], field: str) -> list[str]:
    if not values:
        raise ValueError(f"{field} needs at least one non-empty entry")
    return values


def default_target_tracks() -> list[TargetTrack]:
    titles = {
        "genai": "AI / GenAI Backend Engineer",
        "backend_heavy": "Backend Engineer / Developer",
        "fullstack": "Full Stack Engineer",
        "sde": "SDE-1 / SDE-2",
    }
    return [
        TargetTrack(id=track_id, title=title, keywords=[], priority="high")
        for track_id, title in titles.items()
    ]


def default_skills() -> dict[str, list[str]]:
    return {category: [] for category in STANDARD_SKILL_CATEGORIES}


class TargetTrack(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]+$")]
    title: str = ""
    keywords: StrList = Field(default_factory=list)
    title_tokens: StrList | None = None
    priority: str = "high"

    @field_validator("id")
    @classmethod
    def _id_not_reserved(cls, value: str) -> str:
        if value.lower() in RESERVED_TRACK_IDS:
            raise ValueError(f"track id '{value}' is reserved")
        return value

    @field_validator("priority")
    @classmethod
    def _priority_lower(cls, value: str) -> str:
        return value.lower() or "high"

    @model_validator(mode="after")
    def _fill_defaults(self) -> TargetTrack:
        if not self.title:
            self.title = self.id
        if self.title_tokens is None:
            default = DEFAULT_TITLE_TOKENS.get(self.id)
            fallback = [keyword.lower() for keyword in self.keywords]
            self.title_tokens = list(default) if default else fallback
        return self


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    name: NonEmptyStr
    headline: str = ""
    email: str = ""
    phone: str = ""
    experience_years: float = Field(default=0, ge=0, le=60)
    max_experience_years: float = Field(default=3, gt=0, le=60)
    experience_bracket: str = ""
    education: str = ""
    preferred_locations: StrList = Field(default_factory=list)
    skills: dict[str, StrList] = Field(default_factory=default_skills)
    target_tracks: list[TargetTrack] = Field(default_factory=default_target_tracks)
    disqualifiers: StrList = Field(default_factory=list)

    @field_validator("skills")
    @classmethod
    def _clean_skill_categories(
        cls, value: dict[str, list[str]]
    ) -> dict[str, list[str]]:
        cleaned: dict[str, list[str]] = {}
        for category, skills in value.items():
            key = category.strip()
            if not key:
                raise ValueError("skill category names must be non-empty")
            cleaned.setdefault(key, [])
            cleaned[key] = clean_string_list(cleaned[key] + skills)
        return cleaned

    @field_validator("target_tracks")
    @classmethod
    def _unique_track_ids(cls, value: list[TargetTrack]) -> list[TargetTrack]:
        seen: set[str] = set()
        for track in value:
            if track.id in seen:
                raise ValueError(f"duplicate track id '{track.id}'")
            seen.add(track.id)
        return value


class ScrapeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search_terms: StrList = Field(
        default_factory=lambda: [
            "Full Stack Developer",
            "Node.js Developer",
            "Backend Developer",
            "Python Developer",
            "React Developer",
            "Laravel Developer",
        ]
    )
    locations: StrList = Field(
        default_factory=lambda: [
            "Chandigarh",
            "Mohali",
            "Panchkula",
            "Zirakpur",
            "Remote",
        ]
    )
    sites: StrList = Field(default_factory=lambda: ["linkedin", "naukri"])
    results_per_query: int = Field(default=10, ge=1, le=50)
    hours_old: int = Field(default=72, ge=1, le=720)
    fetch_description: bool = True
    country_indeed: NonEmptyStr = "india"
    exclude_terms: StrList = Field(
        default_factory=lambda: [
            "senior",
            "lead",
            "principal",
            "architect",
            "staff",
            "sr",
        ]
    )
    retention_days: int = Field(default=30, ge=1, le=365)
    delay_min_seconds: int = Field(default=3, ge=0, le=120)
    delay_max_seconds: int = Field(default=8, ge=0, le=120)

    @field_validator("search_terms", "locations")
    @classmethod
    def _non_empty(cls, value: list[str], info: ValidationInfo) -> list[str]:
        return _require_items(value, info.field_name or "list")

    @field_validator("sites")
    @classmethod
    def _known_sites(cls, value: list[str]) -> list[str]:
        sites = clean_string_list([site.lower() for site in value])
        unknown = [site for site in sites if site not in config.AVAILABLE_SITES]
        if unknown:
            raise ValueError(
                f"unknown site(s) {unknown}; choose from {list(config.AVAILABLE_SITES)}"
            )
        return _require_items(sites, "sites")

    @model_validator(mode="after")
    def _delay_order(self) -> ScrapeSettings:
        if self.delay_min_seconds > self.delay_max_seconds:
            raise ValueError("delay_min_seconds must be <= delay_max_seconds")
        return self


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_minutes: int = 360
    run_on_startup: bool = False

    @field_validator("interval_minutes")
    @classmethod
    def _interval_bounds(cls, value: int) -> int:
        low, high = config.min_interval_minutes(), config.MAX_INTERVAL_MINUTES
        if not low <= value <= high:
            raise ValueError(f"interval_minutes must be between {low} and {high}")
        return value


class ScrapeOverrides(BaseModel):
    """Per-run overrides accepted by POST /api/scrape and the CLI."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    search_terms: StrList | None = None
    locations: StrList | None = None
    sites: StrList | None = None
    results_per_query: int | None = Field(default=None, ge=1, le=50)
    hours_old: int | None = Field(default=None, ge=1, le=720)
    fetch_description: bool | None = None


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scrape: dict[str, object] | None = None
    schedule: dict[str, object] | None = None


class JobStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["none", "saved", "applied", "interviewing", "rejected"]
    notes: str | None = Field(default=None, max_length=10000)


ModelT = TypeVar("ModelT", bound=BaseModel)


def merge_partial(current: ModelT, partial: Mapping[str, object]) -> ModelT:
    """Return a re-validated copy of `current` with `partial` applied.

    Raises pydantic.ValidationError (unknown keys included) -> map to HTTP 422.
    """
    return type(current).model_validate({**current.model_dump(), **partial})


def apply_overrides(
    settings: ScrapeSettings, overrides: ScrapeOverrides | None
) -> ScrapeSettings:
    if overrides is None:
        return settings
    return merge_partial(settings, overrides.model_dump(exclude_none=True))


def load_lenient(model: type[ModelT], data: object) -> ModelT:
    """Validate stored data; unknown or invalid fields fall back to their defaults."""
    if not isinstance(data, Mapping):
        return model()
    known = {k: v for k, v in data.items() if k in model.model_fields}
    for _ in range(len(known) + 1):
        try:
            return model.model_validate(known)
        except ValidationError as exc:
            bad = {err["loc"][0] for err in exc.errors() if err["loc"]}
            if not bad & known.keys():
                break
            known = {k: v for k, v in known.items() if k not in bad}
    return model()


def load_profile_template(path: Path | None = None) -> Profile:
    source = path or config.PROFILE_TEMPLATE_PATH
    with open(source, encoding="utf-8") as handle:
        return Profile.model_validate(json.load(handle))
