from __future__ import annotations

import pytest

from backend import matcher
from backend.config import MATCHER_VERSION
from backend.models import Profile, TargetTrack


def score(profile: Profile, **job: object) -> dict[str, object]:
    return matcher.score_job(job, profile)


# --- experience --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We need 2-5 years of experience in Node.js", (2.0, 5.0)),
        ("Experience: 2 to 4 years", (2.0, 4.0)),
        ("3+ yrs experience with TypeScript", (3.0, None)),
        ("3+ yrs in backend", (3.0, None)),
        ("Minimum 3 years of experience", (3.0, None)),
        ("Exp. 1-3 years", (1.0, 3.0)),
        ("18 months experience required", (1.5, None)),
        ("6-12 months of experience", (0.5, 1.0)),
        ("1.5+ years of hands-on experience", (1.5, None)),
        ("Backend Developer (Node.js) | 3+ YOE", (3.0, None)),
        ("2 years of Node.js", (2.0, None)),
        ("Experience 2–4 Years", (2.0, 4.0)),
        ("We need 5+ years' experience in backend development.", (5.0, None)),
        ("5+ years’ experience with Node.js", (5.0, None)),
        ("3 years' of hands-on work", (3.0, None)),
        ("Experience in Node.js - 4 to 6 years", (4.0, 6.0)),
        ("Strong experience with Node.js (5+ years)", (5.0, None)),
    ],
)
def test_parse_experience_patterns(text: str, expected: tuple[float, float | None]) -> None:
    info = matcher.parse_experience(text)
    assert (info.min_years, info.max_years) == expected
    assert info.raw


@pytest.mark.parametrize(
    "text",
    [
        "We are a company with 10+ years in business.",
        "A company with 15 years of experience in the industry is hiring.",
        "Celebrating 25 years of excellence in fintech.",
        "Celebrating 25 years’ of excellence in fintech.",
        "We value experience. Founded 12 years ago.",
        "Founded 12 years ago, we build payments software.",
        "Contract duration: 2 years",
        "B.Tech (4 years) graduates welcome",
        "",
    ],
)
def test_parse_experience_ignores_non_experience_numbers(text: str) -> None:
    assert matcher.parse_experience(text).min_years is None


def test_company_age_skipped_but_later_requirement_found() -> None:
    text = (
        "We are a company with 10+ years in business.\n"
        "Looking for a developer with 2 years of experience."
    )
    info = matcher.parse_experience(text)
    assert info.min_years == 2.0


def test_business_analysis_experience_is_not_company_age() -> None:
    info = matcher.parse_experience("3 years of business analysis experience")
    assert info.min_years == 3.0


def test_structured_experience_range_wins() -> None:
    info = matcher.parse_experience("5+ years of experience", "2-5 Yrs")
    assert (info.min_years, info.max_years, info.raw) == (2.0, 5.0, "2-5 Yrs")


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0-1 Yrs", (0.0, 1.0)), ("5+ Yrs", (5.0, None)), ("Fresher", (0.0, 0.0)), (None, (None, None))],
)
def test_parse_experience_range(value: str | None, expected: tuple[object, object]) -> None:
    info = matcher.parse_experience_range(value)
    assert (info.min_years, info.max_years) == expected


def test_nan_experience_range_falls_back_to_text() -> None:
    info = matcher.parse_experience("3+ yrs", float("nan"))
    assert info.min_years == 3.0


# --- seniority ---------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Senior Backend Engineer",
        "Sr. Node.js Developer",
        "SR Software Engineer",
        "Tech Lead - Node",
        "Principal Engineer",
        "Solutions Architect",
        "Staff Software Engineer",
        "Director of Engineering",
        "Head of Engineering",
        "Engineering Manager",
        "Software Development Manager",
        "SDE 3",
        "SDE-III Backend",
        "Software Development Engineer III",
        "Lead Generation Executive",
    ],
)
def test_senior_titles(title: str) -> None:
    info = matcher.detect_seniority(title, None, 3)
    assert info.is_senior and not info.is_intern
    assert info.reason and "Title" in info.reason


@pytest.mark.parametrize(
    "title",
    [
        "Backend Developer",
        "Sales Manager",
        "Product Manager",
        "Business Development Manager",
        "Staffing Executive",
        "Software Engineer II",
        "Leadership Development Program - Software Developer",
        "SDE II",
    ],
)
def test_not_senior_titles(title: str) -> None:
    assert not matcher.detect_seniority(title, None, 3).is_senior


@pytest.mark.parametrize(
    "title", ["SDE 1", "SDE-1", "SDE I", "SDE1 Backend", "Software Development Engineer I"]
)
def test_sde_one_never_senior(title: str) -> None:
    assert not matcher.detect_seniority(title, 5.0, 3).is_senior
    assert not matcher.detect_seniority(f"Lead {title}", 5.0, 3).is_senior


def test_min_experience_against_profile_limit() -> None:
    assert not matcher.detect_seniority("Backend Developer", 2.0, 3).is_senior
    senior = matcher.detect_seniority("Backend Developer", 3.0, 3)
    assert senior.is_senior and "3+" in (senior.reason or "")
    assert not matcher.detect_seniority("Backend Developer", 3.0, 4).is_senior


@pytest.mark.parametrize(
    "title", ["Software Engineering Intern", "Internship - Backend", "Graduate Trainee", "Apprentice Dev"]
)
def test_intern_titles(title: str) -> None:
    info = matcher.detect_seniority(title, None, 3)
    assert info.is_intern and not info.is_senior


def test_internal_tools_is_not_intern() -> None:
    assert not matcher.detect_seniority("Internal Tools Engineer", None, 3).is_intern


def test_score_job_two_to_five_years_not_senior(profile: Profile) -> None:
    result = score(profile, title="Node.js Developer", description="2-5 years of experience")
    assert result["min_experience"] == 2.0
    assert result["max_experience"] == 5.0
    assert result["is_senior"] is False
    assert result["senior_reason"] is None


def test_score_job_three_plus_years_is_senior(profile: Profile) -> None:
    p = profile.model_copy(update={"max_experience_years": 3})
    result = score(p, title="Node.js Developer", description="3+ yrs experience")
    assert result["is_senior"] is True
    assert result["match_score"] <= 40


def test_score_job_possessive_years_is_senior(profile: Profile) -> None:
    text = "We need 5+ years' experience in backend development."
    result = score(profile, title="Backend Developer", description=text)
    assert result["is_senior"] and result["min_experience"] == 5.0


def test_score_job_company_age_not_counted(profile: Profile) -> None:
    result = score(
        profile,
        title="Backend Developer",
        description="Join a company with 10+ years in business. Node.js, Express.",
    )
    assert result["min_experience"] is None
    assert result["is_senior"] is False


# --- role track --------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("GenAI Engineer", "genai"),
        ("LLM Platform Developer", "genai"),
        ("RAG Developer", "genai"),
        ("Backend Developer", "backend_heavy"),
        ("Node.js Developer", "backend_heavy"),
        ("NodeJS Developer", "backend_heavy"),
        ("Back End Developer", "backend_heavy"),
        ("Full Stack Developer", "fullstack"),
        ("MERN Stack Developer", "fullstack"),
        ("Software Engineer", "sde"),
        ("SDE 1", "sde"),
        ("Storage Engineer", "other"),
        ("Data Analyst - Mumbai", "other"),
        ("Storage Admin, Mumbai", "other"),
        ("Server Administrator", "other"),
        ("AI Engineer - Backend (Node)", "genai"),
    ],
)
def test_role_track(profile: Profile, title: str, track: str) -> None:
    assert matcher.classify_role_track(title, profile) == track


def test_role_track_uses_profile_order(profile: Profile) -> None:
    reordered = profile.model_copy(
        update={"target_tracks": list(reversed(profile.target_tracks))}
    )
    assert matcher.classify_role_track("Backend AI Engineer", profile) == "genai"
    assert matcher.classify_role_track("Backend AI Engineer", reordered) == "backend_heavy"


def test_custom_track_tokens(profile: Profile) -> None:
    custom = profile.model_copy(
        update={"target_tracks": [TargetTrack(id="devops", title_tokens=["devops", "sre"])]}
    )
    assert matcher.classify_role_track("Junior SRE", custom) == "devops"
    assert matcher.classify_role_track("Backend Developer", custom) == "other"


# --- skills ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("term", "text", "expected"),
    [
        ("C#", "Strong C# and SQL", True),
        ("C#", "Strong C and SQL", False),
        ("C", "C++ developer", False),
        ("C", "C/C++ developer", True),
        (".NET", "Experience with .NET Core", True),
        (".NET", "ASP.NET MVC", True),
        (".NET", "internet scale systems", False),
        (".NET", "a fast network", False),
        ("Node.js", "Node.js, Express", True),
        ("Node.js", "NodeJS microservices", True),
        ("Node.js", "node js", True),
        ("Node.js", "nodes in a graph", False),
        ("Express.js", "ExpressJS APIs", True),
        ("React.js", "React and Redux", True),
        ("React", "ReactJS", True),
        ("Next.js", "built with NextJS", True),
        ("Next.js", "the next big thing", False),
        ("REST APIs", "RESTful services", True),
        ("REST APIs", "design REST APIs", True),
        ("REST APIs", "REST", True),
        ("REST APIs", "the rest of the team", False),
        ("RAG", "retrieval-augmented generation pipelines", True),
        ("RAG", "storage engineer", False),
        ("Java", "JavaScript only", False),
        ("Java", "Java/J2EE", True),
        ("PHP", "phpMyAdmin", False),
        ("Go", "Golang services", True),
        ("Go", "Go and Rust", True),
        ("Go", "ready to go live", False),
        ("Go", "Google Cloud", False),
        ("Prompt Engineering", "prompt-engineering skills", True),
        ("AWS S3", "aws  s3 buckets", True),
    ],
)
def test_term_matching(term: str, text: str, expected: bool) -> None:
    assert matcher.term_in_text(term, text) is expected


def test_extract_skills_alias_counted_once(profile: Profile) -> None:
    hits = matcher.extract_skills("ReactJS, React, Node.js, RAG, Docker", profile)
    assert hits["frontend"] == ["React.js"]
    assert hits["core_backend"] == ["Node.js"]
    assert hits["ai_llm"] == ["RAG"]
    assert hits["devops_cloud"] == ["Docker"]


def test_score_job_skill_lists(profile: Profile) -> None:
    result = score(
        profile,
        title="Backend Developer",
        description="Node.js, TypeScript, PostgreSQL, Redis, RAG, Kafka and Kubernetes.",
    )
    assert result["backend_skills"] == ["Node.js", "TypeScript", "PostgreSQL", "Redis"]
    assert result["ai_skills"] == ["RAG"]
    assert result["matched_skills"] == sorted(
        ["Node.js", "TypeScript", "PostgreSQL", "Redis", "RAG"], key=str.lower
    )
    assert result["missing_skills"] == ["Kafka", "Kubernetes"]


# --- scoring -----------------------------------------------------------------


def test_score_weights(profile: Profile) -> None:
    # 40 baseline + 15 generic title + 6 backend skills * 6 (cap 24)
    # + 1 ai skill * 7 + 1 frontend * 3 + 1 devops * 3 + 1 generic * 2
    result = score(
        profile,
        title="Java Developer",
        description="Node.js TypeScript PostgreSQL Redis MongoDB RAG React Docker "
        "Stripe Elements",
    )
    assert result["role_track"] == "other"
    assert result["match_score"] == 40 + 15 + 24 + 7 + 3 + 3 + 2


def test_score_title_bonus_by_priority(profile: Profile) -> None:
    assert score(profile, title="Full Stack Developer")["match_score"] == 60
    assert score(profile, title="Backend Developer")["match_score"] == 65
    assert score(profile, title="Java Developer")["match_score"] == 40 + 15 + 6
    assert score(profile, title="Producer")["match_score"] == 40


def test_score_clamped(profile: Profile) -> None:
    rich = " ".join(s for skills in profile.skills.values() for s in skills)
    assert score(profile, title="Backend Developer", description=rich)["match_score"] == 99
    low = score(
        profile, title="Senior PHP Developer", description="PHP WordPress Salesforce Cobol"
    )
    assert low["match_score"] == 10


def test_senior_and_intern_penalties(profile: Profile) -> None:
    senior = score(profile, title="Senior Backend Developer", description="Node.js")
    assert senior["is_senior"] and senior["match_score"] == min(40 + 25 + 6 - 45, 40)
    intern = score(profile, title="Backend Intern", description="Node.js TypeScript Redis")
    assert intern["is_intern"] and intern["match_score"] == 40
    assert intern["senior_reason"]


def test_disqualifier_penalty_and_node_exemption(profile: Profile) -> None:
    base = score(profile, title="Backend Developer", description="Java and SQL")["match_score"]
    hit = score(profile, title="Backend Developer", description="Java, C# and .NET")
    assert hit["match_score"] == base - 50
    exempt = score(profile, title="Backend Developer", description="C# and Node.js")
    assert exempt["match_score"] == 40 + 25 + 6


def test_disqualifier_is_boundary_safe(profile: Profile) -> None:
    result = score(profile, title="Backend Developer", description="internet scale, phpunit-free")
    assert result["match_score"] == 65


@pytest.mark.parametrize(
    "contact",
    [
        "Send your resume to jobs@acme.net",
        "Apply at www.example.net/careers",
        "Apply: https://acme.net/jobs?ref=.net",
    ],
)
def test_links_do_not_trigger_disqualifiers(profile: Profile, contact: str) -> None:
    text = "Java and PostgreSQL backend services."
    base = score(profile, title="Software Engineer", description=text)["match_score"]
    with_link = score(profile, title="Software Engineer", description=f"{text} {contact}")
    assert with_link["match_score"] == base
    dotnet = score(profile, title="Software Engineer", description=f"{text} ASP.NET MVC")
    assert dotnet["match_score"] == base - 25


def test_score_job_accepts_mapping_profile(profile: Profile) -> None:
    as_dict = profile.model_dump()
    assert matcher.score_job({"title": "Backend Developer"}, as_dict)["match_score"] == 65


def test_score_job_handles_missing_fields(profile: Profile) -> None:
    result = matcher.score_job({}, profile)
    assert result["role_track"] == "other"
    assert result["salary_display"] == "Not disclosed"
    assert result["annual_salary_value"] is None


# --- salary ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((1200000, 1800000, "INR", "yearly"), "₹12L – ₹18L /yr"),
        ((45000, 45000, "INR", "monthly"), "₹45K /mo"),
        ((40000, 60000, "INR", "monthly"), "₹40K – ₹60K /mo"),
        ((50000, None, "INR", "yearly"), "From ₹50K /yr"),
        ((None, 1250000, None, None), "Up to ₹12.5L /yr"),
        ((100000, 1400000, "INR", "yearly"), "₹1L – ₹14L /yr"),
        ((120000, None, "USD", "yearly"), "From $120,000 /yr"),
        ((100000, 120000, "USD", "yearly"), "$100,000 – $120,000 /yr"),
        ((120000, 120000, "USD", "yearly"), "$120,000 /yr"),
        ((50, 70, "USD", "hourly"), "$50 – $70 /hr"),
        ((60000, 80000, "EUR", None), "€60,000 – €80,000 /yr"),
        ((900, 900, "AED", "monthly"), "AED 900 /mo"),
        ((None, None, "INR", "yearly"), "Not disclosed"),
        ((float("nan"), float("nan"), float("nan"), float("nan")), "Not disclosed"),
        ((0, 0, "INR", "yearly"), "Not disclosed"),
    ],
)
def test_format_salary(args: tuple[object, ...], expected: str) -> None:
    assert matcher.format_salary(*args) == expected


def test_annual_salary_value() -> None:
    assert matcher.annual_salary_value(40000, 60000, "monthly") == 720000
    assert matcher.annual_salary_value(1200000, None, "yearly") == 1200000
    assert matcher.annual_salary_value(None, 50, "hourly") == 50 * 2080
    assert matcher.annual_salary_value(None, None, "yearly") is None


def test_score_key() -> None:
    assert matcher.make_score_key(3) == f"3:{MATCHER_VERSION}"
    assert matcher.MATCHER_VERSION == MATCHER_VERSION
