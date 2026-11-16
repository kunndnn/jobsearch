# JobSpy India Dashboard — Architecture

The dashboard (`backend/`, `frontend/`) is built on top of the unmodified `jobspy/`
scraping library. It scrapes jobs automatically on a schedule, on demand from the UI,
or from the terminal. All candidate and search configuration is editable in the UI;
nothing about the candidate is hardcoded.

Agreed decisions: scheduler runs inside the dashboard server; SQLite storage;
no external notifications (dashboard only).

## 1. Runtime

```
 python start.py ──▶ uvicorn (1 worker, no reload by default)
                       └─ FastAPI app
                            ├─ lifespan ─▶ Scheduler thread ────┐
                            ├─ /api/* routes ──▶ "Run now" ─────┼─▶ ScrapeService.run()  (one at a time)
                            └─ serves frontend/dist              │        │
 python scrape.py ──────────────────────────────────────────────┘        ▼
   (terminal / cron, separate process)                    <data dir>/jobspy.db  (SQLite, WAL)
```

- Three triggers (schedule/startup, manual button, CLI) share one code path.
- One run at a time across ALL processes, enforced by a lock row in the DB.
- Data dir: `data/` by default, overridable with env `JOBSPY_DATA_DIR` (tests use this).
- `jobspy/` and `main.py` are never modified.

## 2. Backend modules (`backend/`)

| Module | Responsibility |
|---|---|
| `config.py` | Paths (project root, data dir, DB path, frontend dist), env overrides, constants (`MATCHER_VERSION`, min interval) |
| `db.py` | Connection factory (WAL, `busy_timeout=10000`, `foreign_keys=ON`, row factory), schema creation/migrations via `PRAGMA user_version`, one-time import of legacy JSON |
| `models.py` | Pydantic models + defaults + validation: `Profile`, `TargetTrack`, `ScrapeSettings`, `ScheduleSettings` |
| `repo.py` | All SQL: jobs upsert/query/prune, applications, kv (profile/settings), runs (lock, progress, heartbeat, finish, history) |
| `normalize.py` | DataFrame/records → JSON-safe dicts (NaN/NaT/numpy → None/native, dates → ISO), city alias normalisation, company/title normalisation, dedupe key |
| `matcher.py` | Pure functions, no I/O: `score_job(job, profile) -> dict`, experience parsing, seniority, role track, skills, salary display |
| `scrape_service.py` | One run: acquire lock → plan queries → scrape → normalise → score → upsert → prune → finish |
| `scheduler.py` | Daemon thread; computes next run, wakes on settings change |
| `server.py` | Thin FastAPI routes + SPA serving |
| `../start.py` | Starts server (`--host 127.0.0.1`, `--port` (env `PORT` or 8000), `--no-browser`, `--reload`) |
| `../scrape.py` | CLI: one synchronous run, trigger `cli` |

## 3. SQLite schema (`<data dir>/jobspy.db`)

**jobs**
- `id TEXT PRIMARY KEY` (jobspy id, e.g. `li-4479171416`), `site`, `title`, `company`, `location`,
  `city_norm`, `dedupe_key` (indexed), `duplicate_of TEXT NULL` (id of the primary row),
  `job_url`, `job_url_direct`, `date_posted` (ISO date or NULL), `is_remote` (0/1/NULL),
  `job_type`, `description` (markdown), `min_amount`, `max_amount`, `currency`, `interval`,
  `experience_range` (Naukri structured field), `raw_json` (full normalised jobspy record).
- Tracking: `first_seen_at`, `last_seen_at` (ISO UTC), `first_seen_run_id`, `last_seen_run_id` (NULL for imported jobs).
- Scoring: `match_score INT`, `matched_skills`, `backend_skills`, `ai_skills`, `missing_skills` (JSON arrays),
  `role_track`, `experience_required`, `min_experience REAL`, `max_experience REAL`,
  `is_senior INT`, `is_intern INT`, `senior_reason`, `salary_display`, `annual_salary_value REAL` (for sorting),
  `score_key TEXT` (`"<profile_version>:<MATCHER_VERSION>"`).

**applications** — `job_id TEXT PRIMARY KEY`, `status` (`saved|applied|interviewing|rejected`), `notes`, `updated_at`.
Separate from jobs so re-scraping never touches it. Jobs with an application row are never pruned.

**runs** — `id INTEGER PRIMARY KEY`, `trigger` (`manual|schedule|startup|cli`),
`status` (`running|completed|partial|failed|interrupted|cancelled`), `started_at`, `finished_at`,
`heartbeat_at`, `pid`, `params_json`, `total_queries`, `current_query`, `message`,
`jobs_found` (raw results this run), `new_jobs`, `updated_jobs`, `errors_json` (`{site: message}`),
`cancel_requested INT`.

**kv** — `key TEXT PRIMARY KEY`, `value TEXT` (JSON), `updated_at`. Keys: `profile`, `profile_version` (int),
`scrape_settings`, `schedule`, `legacy_import_done`.

### Legacy import (first start, idempotent via `legacy_import_done`)
- `data/jobs.json` (89 jobs, contains bare `NaN`): sanitise, insert all, `first_seen_at`/`last_seen_at` =
  `date_posted` at 00:00 UTC if present else now, run ids NULL, then score.
- `data/applications.json` if present → applications.
- Profile: `backend/profile.json` is the default template (seeded into kv when missing; any field missing
  from the template gets its model default).
- Settings: model defaults. Legacy JSON files are left on disk untouched.

## 4. Configuration models

**Profile** (`GET/PUT /api/profile`)
```json
{
  "name": "str (required, non-empty)",
  "headline": "str",
  "email": "str", "phone": "str",
  "experience_years": 2.2,
  "max_experience_years": 3,
  "experience_bracket": "str", "education": "str",
  "preferred_locations": ["Bengaluru", "..."],
  "skills": { "core_backend": ["Node.js"], "ai_llm": [], "frontend": [], "devops_cloud": [], "<any>": [] },
  "target_tracks": [ { "id": "backend_heavy", "title": "Backend Engineer / Developer",
                       "keywords": ["..."], "title_tokens": ["backend", "node"], "priority": "highest" } ],
  "disqualifiers": ["PHP", "..."]
}
```
- `max_experience_years`: a job whose minimum required experience is `>=` this is `is_senior`.
- Skill categories are dynamic; `core_backend`, `ai_llm`, `frontend`, `devops_cloud` keep their weights,
  other categories count as generic skills with a small weight.
- Template track order and default `title_tokens`:
  `genai` ["genai","gen ai","generative ai","llm","rag","ai engineer","ai developer","machine learning"],
  `backend_heavy` ["backend","back-end","back end","node","nodejs","node.js","server-side"],
  `fullstack` ["full stack","fullstack","full-stack","mern"],
  `sde` ["sde","software engineer","software development engineer","software developer"].
- Lists of strings are trimmed, empty strings dropped, duplicates removed (case-insensitive). Invalid → 422.

**ScrapeSettings** (defaults)
| field | default | rule |
|---|---|---|
| `search_terms` | ["Node.js Developer","Backend Developer","SDE 1","Full Stack Developer"] | ≥1 |
| `locations` | ["Bengaluru","Pune","Hyderabad","Gurgaon","Noida","Mumbai","Remote"] | ≥1 |
| `sites` | ["linkedin","naukri"] | ≥1, subset of `available_sites` |
| `results_per_query` | 10 | 1–50 |
| `hours_old` | 72 | 1–720 |
| `fetch_description` | true | |
| `country_indeed` | "india" | |
| `exclude_terms` | ["senior","lead","principal","architect","staff","sr"] | negative keywords, Indeed only |
| `retention_days` | 30 | 1–365 |
| `delay_min_seconds` / `delay_max_seconds` | 3 / 8 | 0–120, min ≤ max |

**ScheduleSettings** (defaults): `enabled` false, `interval_minutes` 360 (min 15 — overridable for tests by env
`JOBSPY_MIN_INTERVAL_MINUTES` — max 10080), `run_on_startup` false.

`available_sites`: `["linkedin","naukri","indeed","glassdoor"]`.

## 5. Scrape run (`ScrapeService.run(overrides, trigger)`)

1. **Acquire lock** in one `BEGIN IMMEDIATE` transaction: if a `running` run exists whose heartbeat is
   < 5 min old and (same host) whose pid is alive → refuse (`ScrapeAlreadyRunning`; API 409, CLI exit 2).
   Otherwise mark it `interrupted` and insert the new `running` row. On server startup, any `running` row
   whose pid is dead is marked `interrupted` immediately.
2. **Plan**: queries = locations × search_terms. Per query: Indeed (if selected) gets its own `scrape_jobs`
   call with `"<term>" -t1 -t2 …` from `exclude_terms`; all other selected boards share one call with the
   plain term. Pass `fetch_description`, `hours_old`, `results_wanted`, `country_indeed`, `verbose=0`.
3. **Heartbeat** thread updates `heartbeat_at` every 30 s. Between queries: check `cancel_requested`;
   sleep a random `delay_min..delay_max` seconds using an interruptible wait.
4. **Per query**: collect `df.attrs["errors"]` per site → normalise → score with current profile → upsert:
   - existing id → update fields + `last_seen_*`; keep `first_seen_*`; never replace a non-empty description with an empty one;
   - new id whose `dedupe_key` matches a primary row → insert with `duplicate_of = <primary id>` (cross-board copies and reposts never show as new);
   - otherwise insert as a new primary job.
   - update run progress (`current_query`, `jobs_found`, `new_jobs`, `updated_jobs`, `message`).
   - an exception in one query is recorded and the run continues.
5. **Finish**: status `cancelled` if cancelled; `failed` if no results at all and every attempted board
   errored; `partial` if some board errored; else `completed`. Prune jobs with `last_seen_at` older than
   `retention_days` and no application row (and their duplicates). Store summary message.

Dedupe key: `normalise(company) | normalise(title) | city_norm`. Company normalisation strips punctuation and
legal suffixes (`pvt`, `private`, `ltd`, `limited`, `inc`, `llp`, `llc`, `corp`). City aliases: bangalore→bengaluru,
gurugram→gurgaon, bombay→mumbai, new delhi→delhi; first comma-separated location token; "remote" if remote.

## 6. Scheduler

- `next_run_at = finished_at of the last finished run (any trigger, any status except interrupted) + interval`.
  No previous run → due now.
- On server start, if enabled and overdue (or `run_on_startup`) → run **once**, 15 s after startup (trigger `startup`).
- Waits on a `threading.Event` with timeout; `PUT /api/settings` sets the event so changes apply immediately.
- If a run is in progress when due → skip; the next time follows from that run's finish.
- Exceptions inside a run are logged; the thread never dies.

## 7. Matching rules (`matcher.py`, `MATCHER_VERSION` bump on any rule change)

- Experience: Naukri `experience_range` first; else description patterns only in an experience context
  (`experience`, `exp`, `yrs`, `years of`): ranges `2-5 years`, `3+ yrs`, `minimum 3 years`, months
  (`18 months` → 1.5). Company-age phrases ("10+ years in business") must not count.
- Senior: title matches senior words on word boundaries (senior, sr, lead, principal, architect, staff,
  director, head of, manager *only with engineering context*, sde 3 / sde iii) — but `sde 1/sde i` titles are
  never senior; or `min_experience >= profile.max_experience_years`. Intern: intern/internship/trainee/apprentice in title.
- Role track: first profile track whose `title_tokens` match the title on word boundaries, else `other`.
- Skills / disqualifiers: case-insensitive, boundary-safe for tokens like `C#`, `.NET`, `Node.js`
  (lookarounds, not `\b`), aliases node/nodejs, express/expressjs, react/reactjs, next/nextjs, rest/restful, rag/retrieval-augmented.
- Score: existing weights (baseline 40, title bonus, capped skill bonuses, senior/intern penalty, disqualifier
  −25 each unless node/typescript present), clamped 10–99.
- Salary: interval-aware: INR yearly `₹12L – ₹18L /yr`, INR monthly `₹45K /mo`, other currencies `$120,000 /yr`;
  missing → `Not disclosed`. `annual_salary_value` = annualised max (or min) for sorting.
- Re-scoring: `PUT /api/profile` bumps `profile_version` and re-scores all jobs in one transaction.
  On startup, rows whose `score_key` differs from current are re-scored.

## 8. HTTP API

All timestamps ISO-8601 UTC with offset. Errors: `{"detail": ...}`. Unknown `/api/*` → 404 JSON.

| Method & path | Request | Response |
|---|---|---|
| `GET /api/profile` | | Profile |
| `PUT /api/profile` | full Profile | `{"success": true, "profile": Profile, "rescored": int}`; 422 invalid |
| `GET /api/settings` | | `{"scrape": ScrapeSettings, "schedule": ScheduleSettings, "available_sites": [str], "limits": {"min_interval_minutes": int, "warn_queries": 30}}` |
| `PUT /api/settings` | `{"scrape"?: partial, "schedule"?: partial}` | same as GET; 422 invalid; wakes scheduler |
| `POST /api/scrape` | optional overrides: `search_terms, locations, sites, results_per_query, hours_old, fetch_description` | `{"success": true, "run_id": int}`; 409 if running |
| `POST /api/scrape/stop` | | `{"success": true}`; 404 if nothing running |
| `GET /api/scrape/status` | | see below |
| `GET /api/jobs` | query params below | `{"total", "total_unfiltered", "page", "page_size", "jobs": [JobSummary]}` |
| `GET /api/jobs/{id}` | | JobDetail; 404 |
| `POST /api/jobs/{id}/status` | `{"status": "none|saved|applied|interviewing|rejected", "notes"?: str}` | `{"success": true, "job_id", "status"}`; 422/404 |
| `GET /api/stats` | | see below |

**Run** object: `{id, trigger, status, started_at, finished_at, message, total_queries, current_query,
jobs_found, new_jobs, updated_jobs, errors: {site: str}, cancel_requested: bool}`.

**GET /api/scrape/status** → `{"is_scraping": bool, "current_run": Run|null,
"schedule": {"enabled", "interval_minutes", "run_on_startup", "last_run_at": iso|null, "next_run_at": iso|null},
"history": [Run]}` (newest first, max 20).

**GET /api/jobs** params: `search` (title/company/description/skills, case-insensitive), `role_track`
(`all` | track id | `other`), `location` (`all` | substring, alias-aware), `sites` (comma list | `all`; a job
matches if its own site OR any duplicate's site is selected; empty → no results), `sort_by`
(`match_score` | `exp_asc` | `exp_desc` | `date` | `salary` | `found`), `min_score`, `remote_only`,
`exclude_senior` (default true; hides `is_senior` and `is_intern`), `status` (`all|none|saved|applied|interviewing|rejected`),
`new_only`, `page` (default 1), `page_size` (default 50, max 200). Duplicate rows are never listed themselves.

**JobSummary**: `id, site, title, company, location, job_url, job_url_direct, date_posted, is_remote,
salary_display, match_score, matched_skills, missing_skills, backend_skills, ai_skills, role_track,
experience_required, min_experience, max_experience, is_senior, is_intern, senior_reason, first_seen_at,
last_seen_at, is_new, also_on: [{site, job_url}], app_status ("none" default), app_notes ("" default),
description_snippet (≤ 300 chars plain text)`.
`is_new`: `first_seen_at >=` started_at of the most recent run that is running or finished as
completed/partial/cancelled; imported jobs are never new.

**JobDetail**: JobSummary + `description` (markdown), `job_type`, `currency`, `interval`, `min_amount`,
`max_amount`, `experience_range`, `company_url`.

**GET /api/stats** → `{"total_jobs" (primary, non-senior, non-intern), "total_stored", "high_match_count" (≥80,
non-senior), "new_count", "applied_count", "saved_count", "interviewing_count", "rejected_count",
"platform_distribution": {site: n}, "top_skills": [{"name","count"}], "top_companies": [{"name","count"}],
"last_scrape_at": iso|null}`.

## 9. CLI (`scrape.py`)

`python scrape.py [--terms "A,B"] [--locations "X,Y"] [--sites linkedin,naukri] [--results N] [--hours-old N]
[--no-descriptions] [--quiet]` — overrides apply to this run only. Works from any cwd. Prints progress and a
summary (new / updated / total, per-board errors, next scheduled run). Exit codes: 0 completed/partial,
1 failed, 2 another run holds the lock, 130 Ctrl+C (run marked cancelled).

## 10. Frontend (`frontend/src/`)

- `api.js` — every request; non-2xx throws an Error with the server `detail`.
- Hooks: `useScrapeStatus` (poll 3 s while running, 30 s idle; fires `onRunFinished(run)` once per run),
  `useJobs` (300 ms debounce, AbortController for stale requests, pagination "Load more").
- Tabs (remembered in localStorage, try/catch): **Jobs**, **Automation**, **Profile**.
- Jobs: stats cards (incl. new and last scraped), filter pills from profile tracks / preferred locations /
  available sites, status filter, New-only, senior toggle (explicit true/false), NEW and "also on" badges,
  detail modal (fetches `/api/jobs/{id}`; status dropdown + notes; description as text, no raw HTML).
- Automation: chip editors (terms, locations, exclude terms), board checkboxes, numeric fields, schedule
  card (enable, interval presets 30m/1h/3h/6h/12h/24h + custom, run on startup, last/next run), query-count
  warning, Save (dirty indicator, 422 details), Run now / Stop, live progress bar, run history table.
- Profile: all fields editable incl. skill categories and target tracks; save → refetch jobs + stats.
- Components: `ChipEditor`, `JobCard`, `JobDetailModal`, `ProgressBanner`, `RunHistory`, `Toast`, `StatsBar`.
- Backend-unreachable banner. Responsive to ~380 px. Keep the existing dark style.

## 11. Testing

- `tests/` with pytest (`requirements-dev.txt`: pytest, httpx, ruff).
- Unit: matcher (experience/seniority edge cases, tracks, skills incl. `C#`/`.NET`, salary), normalize
  (NaN, cities, dedupe key), repo (first-seen kept, description not blanked, duplicates linked, prune keeps
  applications), lock (stale heartbeat, refusal), scheduler next-run logic with an injected clock.
- API: FastAPI `TestClient`, temp `JOBSPY_DATA_DIR`, fake `scrape_jobs` returning fixture DataFrames — no network.
- Manual live smoke: LinkedIn, 1 query, 3 results, isolated data dir.
- Frontend: `npm run lint` and `npm run build`.

## 12. Operations

- Setup: `uv venv .venv && uv pip install --python .venv/bin/python -r requirements.txt` (plain `pip` also works).
- Build UI: `cd frontend && npm install && npm run build`. Start: `python start.py`.
- Auto-scraping runs while the server runs. Cron/Task Scheduler can call `scrape.py` as a backup.
- Server binds `127.0.0.1` by default (no auth). `data/` is git-ignored (contains personal profile data).
