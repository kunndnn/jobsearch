from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache

from backend.config import MATCHER_VERSION
from backend.models import Profile

__all__ = [
    "MATCHER_VERSION",
    "ExperienceInfo",
    "SeniorityInfo",
    "annual_salary_value",
    "classify_role_track",
    "detect_seniority",
    "extract_skills",
    "find_terms",
    "format_salary",
    "make_score_key",
    "parse_experience",
    "parse_experience_range",
    "score_job",
    "term_in_text",
]

BASELINE_SCORE = 40
CATEGORY_WEIGHTS: dict[str, tuple[int, int]] = {
    "core_backend": (6, 24),
    "ai_llm": (7, 14),
    "frontend": (3, 9),
    "devops_cloud": (3, 6),
}
GENERIC_SKILL_WEIGHT = (2, 6)
PRIORITY_TITLE_BONUS = {"highest": 25, "high": 20, "medium": 15, "low": 10}
GENERIC_TITLE_BONUS = 15
SENIOR_PENALTY = 45
INTERN_PENALTY = 40
PENALTY_CAP = 40
DISQUALIFIER_PENALTY = 25
SCORE_MIN, SCORE_MAX = 10, 99
NOT_DISCLOSED = "Not disclosed"
NOTABLE_STACK = ("Kafka", "Kubernetes", "AWS", "GraphQL", "Go", "Python", "Microservices")
MAX_MISSING_SKILLS = 4
MAX_YEARS = 40.0

# Alias groups: any spelling in the set maps to one pattern. Bare English words
# ("next", "rest") are not counted unless written as a tech term (Next.js, REST).
_ALIAS_PATTERNS: tuple[tuple[frozenset[str], str], ...] = (
    (frozenset({"node", "node.js", "nodejs", "node js"}), r"node(?:\.?js|\s+js)?"),
    (frozenset({"express", "express.js", "expressjs"}), r"express(?:\.?js)?"),
    (frozenset({"react", "react.js", "reactjs"}), r"react(?:\.?js)?"),
    (frozenset({"next", "next.js", "nextjs", "next js"}), r"next(?:\.js|js|\s+js)"),
    (
        frozenset({"rest", "restful", "rest api", "rest apis", "restful api", "restful apis"}),
        r"(?:restful(?:\s+apis?)?|rest\s+apis?|(?-i:REST))",
    ),
    (
        frozenset({"rag", "retrieval-augmented", "retrieval augmented", "retrieval-augmented generation"}),
        r"(?:rag|retrieval[\s-]+augmented(?:\s+generation)?)",
    ),
    (frozenset({"go", "golang"}), r"(?:golang|(?-i:Go))"),
    (
        frozenset({"postgres", "postgresql", "pgsql", "postgre"}),
        r"postgre(?:sql)?|pgsql",
    ),
    (
        frozenset({"socket.io", "socketio", "socket io"}),
        r"socket(?:\.?io|\s+io)",
    ),
)

_GENERIC_TITLE = re.compile(r"(?<![\w])(?:developer|engineer|sde|programmer)(?![\w])", re.I)
_INTERN_TITLE = re.compile(r"(?<![\w])(?:intern|interns|internship|trainee|apprentice)(?![\w])", re.I)
_SDE = r"(?:sde|software\s+(?:development\s+)?engineer)[\s_-]*"
_SENIOR_TITLE = re.compile(
    r"(?<![\w])(?:senior|sr|lead|principal|architects?|staff|director|head\s+of"
    rf"|{_SDE}(?:3|iii))(?![\w])",
    re.I,
)
_SDE_ONE_TITLE = re.compile(rf"(?<![\w]){_SDE}(?:1|i)(?![\w])", re.I)
_MANAGER_TITLE = re.compile(r"(?<![\w])managers?(?![\w])", re.I)
_ENGINEERING_CONTEXT = re.compile(
    r"(?<![\w])(?:engineering|engineers?|software|technical|technology|tech|developers?"
    r"|development\s+(?:team|center|centre)|backend|back-end|frontend|front-end"
    r"|full[\s-]?stack|devops|sre|qa|it|platform|infrastructure|cloud|data|security"
    r"|systems?|programs?\s+delivery)(?![\w])",
    re.I,
)

_EXPERIENCE_MENTION = re.compile(
    r"(?<![\w.])(?P<min>\d{1,3}(?:\.\d{1,2})?)"
    r"(?:\s*(?:-|–|—|to)\s*(?P<max>\d{1,3}(?:\.\d{1,2})?))?"
    r"\s*(?P<plus>\+)?\s*"
    r"(?P<unit>years?|yrs?|yoe|months?)(?![a-z])",
    re.I,
)
_RANGE_FIELD = re.compile(r"(\d{1,2}(?:\.\d+)?)\s*(?:-|–|—|to)\s*(\d{1,2}(?:\.\d+)?)")
_SINGLE_FIELD = re.compile(r"(\d{1,2}(?:\.\d+)?)")
# A dot inside a token ("Node.js") does not end the sentence.
_CONTEXT_BEFORE = re.compile(
    r"(?<![\w])(?:experienced?|exp)(?![\w])\.?(?:[^\n.;]|\.(?=\w)){0,30}$", re.I
)
_AFTER_UNIT = r"^['’]?\s*\+?\s*"  # optional possessive: "5 years' experience"
_CONTEXT_AFTER = re.compile(
    _AFTER_UNIT + r"(?:of\s+)?(?:[\w-]+\s+){0,3}?(?:experienced?|exp)(?![\w])", re.I
)
_YEARS_OF = re.compile(_AFTER_UNIT + r"of(?![\w])", re.I)
_COMPANY_AGE_AFTER = re.compile(
    _AFTER_UNIT + r"(?:"
    r"in\s+(?:the\s+)?(?:business|market|existence|operations?)"
    r"|of\s+(?:\w+\s+)?(?:business|excellence|existence|operations?|legacy|history"
    r"|trust|service|innovation|presence|success)"
    r"|old|ago|young"
    r")(?![\w])",
    re.I,
)
_COMPANY_AGE_BEFORE = re.compile(
    r"(?:(?<![\w])(?:company|firm|organi[sz]ation|agency|we)"
    r"\s+(?:with|of|having|has|have|has\s+been|have\s+been|is|are)"
    r"|(?<![\w])(?:established|founded|incorporated|legacy\s+of|track\s+record\s+of))"
    r"\s+(?:over|more\s+than|nearly|almost|around|about|a)?\s*$",
    re.I,
)

# Emails and URLs are not job text: ".NET" must not match "hr@acme.net".
_LINKS = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+|(?:https?://|www\.)\S+", re.I)

_INTERVAL_SUFFIX = {
    "yearly": "/yr",
    "monthly": "/mo",
    "weekly": "/wk",
    "daily": "/day",
    "hourly": "/hr",
}
_ANNUAL_FACTOR = {"yearly": 1, "monthly": 12, "weekly": 52, "daily": 260, "hourly": 2080}
_CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "GBP": "£", "INR": "₹"}
_INR_ALIASES = {"INR", "RS", "RS.", "₹", "RUPEE", "RUPEES"}


@dataclass(frozen=True)
class ExperienceInfo:
    min_years: float | None = None
    max_years: float | None = None
    raw: str | None = None


@dataclass(frozen=True)
class SeniorityInfo:
    is_senior: bool
    is_intern: bool
    reason: str | None


def make_score_key(profile_version: int) -> str:
    return f"{profile_version}:{MATCHER_VERSION}"


def _text(value: object) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return ""
    return str(value).strip()


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


# --- term matching -----------------------------------------------------------


def _alias_pattern(term: str) -> str | None:
    key = re.sub(r"\s+", " ", term.strip().lower())
    for spellings, pattern in _ALIAS_PATTERNS:
        if key in spellings:
            return pattern
    return None


@lru_cache(maxsize=4096)
def _term_regex(term: str) -> re.Pattern[str]:
    body = _alias_pattern(term)
    if body is None:
        words = term.strip().split()
        body = r"[\s-]+".join(re.escape(word) for word in words)
    stripped = term.strip()
    left = r"(?<![A-Za-z0-9_])" if stripped[:1].isalnum() else ""
    right_class = r"A-Za-z0-9_#+" if len(stripped) == 1 else r"A-Za-z0-9_"
    right = rf"(?![{right_class}])" if stripped[-1:].isalnum() else ""
    return re.compile(rf"{left}(?:{body}){right}", re.I)


def _alias_group(term: str) -> str:
    pattern = _alias_pattern(term)
    return pattern if pattern is not None else term.strip().lower()


def term_in_text(term: str, text: str) -> bool:
    return bool(term.strip()) and _term_regex(term).search(text) is not None


def find_terms(terms: Iterable[str], text: str) -> list[str]:
    """Terms present in text, one per alias group, in input order."""
    found: list[str] = []
    groups: set[str] = set()
    for term in terms:
        group = _alias_group(term)
        if group not in groups and term_in_text(term, text):
            groups.add(group)
            found.append(term)
    return found


def extract_skills(text: str, profile: Profile) -> dict[str, list[str]]:
    """Matched profile skills per category (an alias group counts once overall)."""
    groups: set[str] = set()
    result: dict[str, list[str]] = {}
    for category, skills in profile.skills.items():
        hits: list[str] = []
        for skill in skills:
            group = _alias_group(skill)
            if group not in groups and term_in_text(skill, text):
                groups.add(group)
                hits.append(skill)
        result[category] = hits
    return result


# --- experience --------------------------------------------------------------


def parse_experience_range(value: object) -> ExperienceInfo:
    """Naukri-style structured field: '2-5 Yrs', '5+ Yrs', 'Fresher'."""
    text = _text(value)
    if not text:
        return ExperienceInfo()
    if re.search(r"(?<![\w])fresher", text, re.I):
        return ExperienceInfo(0.0, 0.0, text)
    match = _RANGE_FIELD.search(text)
    if match:
        low, high = float(match.group(1)), float(match.group(2))
        return ExperienceInfo(min(low, high), max(low, high), text)
    match = _SINGLE_FIELD.search(text)
    if match:
        return ExperienceInfo(float(match.group(1)), None, text)
    return ExperienceInfo()


def _window(text: str, start: int, end: int) -> tuple[str, str]:
    before = text[max(0, start - 60) : start].rsplit("\n", 1)[-1]
    after = text[end : end + 60].split("\n", 1)[0]
    return before, after


def _is_company_age(before: str, after: str) -> bool:
    if _COMPANY_AGE_BEFORE.search(before):
        return True
    return bool(_COMPANY_AGE_AFTER.match(after)) and not _CONTEXT_AFTER.match(after)


def _has_experience_context(unit: str, before: str, after: str) -> bool:
    if unit in ("yr", "yrs", "yoe"):
        return True
    if _CONTEXT_BEFORE.search(before) or _CONTEXT_AFTER.match(after):
        return True
    return unit.startswith("year") and bool(_YEARS_OF.match(after))


def _mention_to_info(match: re.Match[str]) -> ExperienceInfo | None:
    unit = match.group("unit").lower()
    divisor = 12.0 if unit.startswith("month") else 1.0
    low = round(float(match.group("min")) / divisor, 2)
    high_raw = match.group("max")
    high = round(float(high_raw) / divisor, 2) if high_raw else None
    if low > MAX_YEARS or (high is not None and (high > MAX_YEARS or high < low)):
        return None
    return ExperienceInfo(low, high, re.sub(r"\s+", " ", match.group(0)).strip())


def parse_experience(text: str, experience_range: object = None) -> ExperienceInfo:
    """Structured range first; else the first description mention in an
    experience context that is not a company-age phrase."""
    structured = parse_experience_range(experience_range)
    if structured.min_years is not None:
        return structured
    for match in _EXPERIENCE_MENTION.finditer(text or ""):
        before, after = _window(text, match.start(), match.end())
        if _is_company_age(before, after):
            continue
        if not _has_experience_context(match.group("unit").lower(), before, after):
            continue
        info = _mention_to_info(match)
        if info is not None:
            return info
    return ExperienceInfo()


# --- seniority / track -------------------------------------------------------


def _senior_title_word(title: str) -> str | None:
    match = _SENIOR_TITLE.search(title)
    if match:
        return match.group(0)
    manager = _MANAGER_TITLE.search(title)
    if manager and _ENGINEERING_CONTEXT.search(title):
        return manager.group(0)
    return None


def detect_seniority(
    title: str, min_experience: float | None, max_experience_years: float
) -> SeniorityInfo:
    if _INTERN_TITLE.search(title):
        return SeniorityInfo(False, True, "Internship / trainee role")
    if _SDE_ONE_TITLE.search(title):
        return SeniorityInfo(False, False, None)
    word = _senior_title_word(title)
    if word:
        return SeniorityInfo(True, False, f"Title indicates senior level ('{word}')")
    if min_experience is not None and min_experience >= max_experience_years:
        return SeniorityInfo(
            True,
            False,
            f"Requires {min_experience:g}+ years (your limit: {max_experience_years:g})",
        )
    return SeniorityInfo(False, False, None)


def classify_role_track(title: str, profile: Profile) -> str:
    for track in profile.target_tracks:
        if find_terms(track.title_tokens or [], title):
            return track.id
    return "other"


def _title_bonus(title: str, track_id: str, profile: Profile) -> int:
    for track in profile.target_tracks:
        if track.id == track_id:
            return PRIORITY_TITLE_BONUS.get(track.priority, PRIORITY_TITLE_BONUS["medium"])
    return GENERIC_TITLE_BONUS if _GENERIC_TITLE.search(title) else 0


# --- salary ------------------------------------------------------------------


def _short_number(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _inr(value: float) -> str:
    for size, suffix in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if value >= size:
            return f"₹{_short_number(value / size)}{suffix}"
    return f"₹{_short_number(value)}"


def _currency_code(currency: object) -> str:
    code = _text(currency).upper()
    return "INR" if not code or code in _INR_ALIASES else code


def _interval_key(interval: object) -> str:
    key = _text(interval).lower()
    return key if key in _ANNUAL_FACTOR else "yearly"


def _amount_formatter(code: str) -> Callable[[float], str]:
    if code == "INR":
        return _inr
    symbol = _CURRENCY_SYMBOL.get(code, f"{code} ")

    def fmt(value: float) -> str:
        return f"{symbol}{value:,.0f}"

    return fmt


def format_salary(
    min_amount: object, max_amount: object, currency: object, interval: object
) -> str:
    amounts = sorted(a for a in (_number(min_amount), _number(max_amount)) if a)
    if not amounts:
        return NOT_DISCLOSED
    fmt = _amount_formatter(_currency_code(currency))
    suffix = _INTERVAL_SUFFIX[_interval_key(interval)]
    low, high = fmt(amounts[0]), fmt(amounts[-1])
    if len(amounts) == 2 and low != high:
        return f"{low} – {high} {suffix}"
    if len(amounts) == 2 or _number(min_amount) is None:
        prefix = "" if len(amounts) == 2 else "Up to "
        return f"{prefix}{low} {suffix}"
    return f"From {low} {suffix}"


def annual_salary_value(
    min_amount: object, max_amount: object, interval: object
) -> float | None:
    amount = _number(max_amount) or _number(min_amount)
    if amount is None:
        return None
    return amount * _ANNUAL_FACTOR[_interval_key(interval)]


# --- scoring -----------------------------------------------------------------


def _skill_score(hits: Mapping[str, list[str]]) -> int:
    score = 0
    generic = 0
    for category, skills in hits.items():
        if category in CATEGORY_WEIGHTS:
            weight, cap = CATEGORY_WEIGHTS[category]
            score += min(len(skills) * weight, cap)
        else:
            generic += len(skills)
    weight, cap = GENERIC_SKILL_WEIGHT
    return score + min(generic * weight, cap)


def _disqualifier_penalty(text: str, profile: Profile) -> int:
    hits = find_terms(profile.disqualifiers, text)
    if (
        not hits
        or term_in_text("node", text)
        or term_in_text("typescript", text)
        or term_in_text("laravel", text)
        or term_in_text("python", text)
        or term_in_text("react", text)
    ):
        return 0
    return DISQUALIFIER_PENALTY * len(hits)


def _missing_skills(text: str, matched: list[str], profile: Profile) -> list[str]:
    known = {_alias_group(s) for skills in profile.skills.values() for s in skills}
    known |= {_alias_group(s) for s in matched}
    missing = [s for s in NOTABLE_STACK if _alias_group(s) not in known]
    return find_terms(missing, text)[:MAX_MISSING_SKILLS]


def _as_profile(profile: Profile | Mapping[str, object]) -> Profile:
    return profile if isinstance(profile, Profile) else Profile.model_validate(profile)


def score_job(
    job: Mapping[str, object], profile: Profile | Mapping[str, object]
) -> dict[str, object]:
    """Scoring fields for a job (title, description, experience_range, salary keys)."""
    prof = _as_profile(profile)
    title = _text(job.get("title"))
    full_text = _LINKS.sub(" ", f"{title}\n{_text(job.get('description'))}")

    hits = extract_skills(full_text, prof)
    matched = sorted({s for skills in hits.values() for s in skills}, key=str.lower)
    experience = parse_experience(full_text, job.get("experience_range"))
    seniority = detect_seniority(title, experience.min_years, prof.max_experience_years)
    track = classify_role_track(title, prof)

    score = BASELINE_SCORE + _title_bonus(title, track, prof) + _skill_score(hits)
    if seniority.is_senior:
        score = min(score - SENIOR_PENALTY, PENALTY_CAP)
    elif seniority.is_intern:
        score = min(score - INTERN_PENALTY, PENALTY_CAP)
    score -= _disqualifier_penalty(full_text, prof)
    score = max(SCORE_MIN, min(SCORE_MAX, score))

    amounts = (job.get("min_amount"), job.get("max_amount"))
    return {
        "match_score": int(score),
        "matched_skills": matched,
        "backend_skills": hits.get("core_backend", []),
        "ai_skills": hits.get("ai_llm", []),
        "missing_skills": _missing_skills(full_text, matched, prof),
        "role_track": track,
        "experience_required": experience.raw,
        "min_experience": experience.min_years,
        "max_experience": experience.max_years,
        "is_senior": seniority.is_senior,
        "is_intern": seniority.is_intern,
        "senior_reason": seniority.reason,
        "salary_display": format_salary(*amounts, job.get("currency"), job.get("interval")),
        "annual_salary_value": annual_salary_value(*amounts, job.get("interval")),
    }
