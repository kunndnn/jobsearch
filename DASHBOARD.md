# JobSpy India Dashboard: user guide

A local dashboard on top of the `jobspy` scraper. It scrapes LinkedIn, Naukri, Indeed and
Glassdoor on a schedule, when you click **Run now**, or from the terminal. It scores every job
against your profile and keeps everything in a local SQLite database. Only one scrape runs at a
time, even across processes.

Paths below assume the project lives in `/home/anupam/JobSpy`; adjust them if yours differs.

## Quick start (any computer)

The only requirement is **Python 3.10+**. The UI ships prebuilt, so Node.js is not needed.
The first run creates `.venv` and installs the dependencies (internet needed once, ~1 min).

| OS | Command (run inside the project folder) |
|---|---|
| Windows | double-click `run.bat`, or `py run.py` |
| macOS / Linux | `./run.sh`, or `python3 run.py` |

The dashboard opens at <http://127.0.0.1:8000>. Stop it with Ctrl+C.
Extra flags pass through: `python run.py --port 9000 --no-browser`.
One terminal scrape: `python run.py scrape --sites linkedin --results 5`.

The manual setup below does the same thing step by step.

## 1. Setup

With [uv](https://docs.astral.sh/uv/) (recommended):

```bash
cd /home/anupam/JobSpy
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
# for running the tests: uv pip install --python .venv/bin/python -r requirements-dev.txt
```

With plain pip:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Build the UI once, and again after any frontend change (needs Node.js and npm):

```bash
cd frontend && npm install && npm run build
```

## 2. Start the dashboard

```bash
.venv/bin/python start.py            # http://127.0.0.1:8000, opens a browser tab
```

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Bind address. There is no login, so keep it local. |
| `--port` | `$PORT` or `8000` | Port to serve on. |
| `--no-browser` | off | Don't open a browser tab. |
| `--reload` | off | Restart the server when backend code changes (development only). |

At startup it prints the candidate name, the database path, the last run and the schedule
state. If `frontend/dist` is missing, it also tells you to build the UI (the API works either way).

## 3. Automation tab

- **Search**: edit the search terms, locations and exclude terms (Indeed only) as chips, and tick
  the boards to scrape. Each query is one term in one location, so the number of queries is
  terms × locations. You get a warning above 30 queries.
- **Limits**: results per query (1–50), max posting age in hours, whether to fetch full
  descriptions, retention in days (old jobs are pruned unless you saved or applied to them), and
  the random delay between queries.
- **Schedule**:
  - **Scrape automatically** with an interval preset (30m/1h/3h/6h/12h/24h) or a custom
    interval of 15 minutes to 7 days.
  - The next run is the last finished run plus the interval. That counts any run: scheduled,
    manual or CLI.
  - If the schedule was overdue while the server was off, one catch-up run starts 15 s after the
    server starts. Tick **Run when the server starts** to get that run every time.
  - A due run is skipped while another scrape is already running.
- **Save** applies the changes immediately, including the next-run time. Invalid values are
  listed next to the form.
- **Run now** and **Stop**: one-off runs with a live progress bar. The run history shows each
  run's status (`completed`, `partial` when some board failed, `failed`, `cancelled`,
  `interrupted`) and its per-board errors. Naukri and Indeed often block automated traffic, so a
  board error is usually not a bug.

The **Profile** tab edits the candidate data used for scoring. Saving it re-scores every job.

## 4. Terminal / cron: `scrape.py`

Runs one scrape synchronously using the dashboard's saved settings. It works from any
directory, with or without the server running.

```bash
.venv/bin/python scrape.py [--terms "A,B"] [--locations "X,Y"] [--sites linkedin,naukri] \
                           [--results N] [--hours-old N] [--no-descriptions] [--quiet]
```

| Flag | Meaning (applies to this run only, saved settings are not changed) |
|---|---|
| `--terms "A,B"` | Comma-separated search terms |
| `--locations "X,Y"` | Comma-separated locations |
| `--sites a,b` | Boards: `linkedin`, `naukri`, `indeed`, `glassdoor` |
| `--results N` | Results per query (1–50) |
| `--hours-old N` | Only jobs posted in the last N hours (1–720) |
| `--no-descriptions` | Skip full descriptions (faster, but weaker scoring) |
| `--quiet` | Print only the final summary |

It prints progress per query, then a summary: new, updated and found counts, database totals,
errors per board, and the next scheduled run.

| Exit code | Meaning |
|---|---|
| `0` | Completed, or partial (some boards errored) |
| `1` | Failed (every board errored and nothing was found), or invalid arguments |
| `2` | Another scrape holds the lock; nothing was done |
| `130` | Cancelled: Ctrl+C (the run is recorded as cancelled) or **Stop** in the dashboard |

Example: `.venv/bin/python scrape.py --sites linkedin --terms "Backend Developer" --locations Bengaluru --results 3`

## 5. Scheduling outside the server (backup)

The built-in scheduler only runs while `start.py` is running. As a backup you can call
`scrape.py` from cron or from Windows Task Scheduler. Overlaps are safe: a second run exits with
code 2.

**cron inside WSL.** WSL does not start cron by itself. Run `sudo service cron start` after each
WSL boot, or turn on systemd (`[boot]` `systemd=true` in `/etc/wsl.conf`) and run
`sudo systemctl enable cron`. Then add a line with `crontab -e`:

```cron
0 */6 * * * /home/anupam/JobSpy/.venv/bin/python /home/anupam/JobSpy/scrape.py --quiet >> /home/anupam/jobspy-cron.log 2>&1
```

cron only fires while the WSL VM is running. WSL shuts down shortly after the last WSL process
exits.

**Windows Task Scheduler** (works even when no WSL terminal is open). Create a task with a
trigger such as daily, repeating every 6 hours, and the action:

- Program: `wsl.exe`
- Arguments: `-e /home/anupam/JobSpy/.venv/bin/python /home/anupam/JobSpy/scrape.py --quiet`

Add `-d <DistroName>` before `-e` if Ubuntu is not your default distro.

## 6. Where data lives

Everything lives in the data directory, `data/` by default:

| File | Contents |
|---|---|
| `jobspy.db` (+ `-wal`, `-shm`) | Jobs, applications (saved/applied/…), profile, settings, run history |
| `jobs.json`, `applications.json` | Legacy files from the old dashboard. They are imported once on first start and never modified. |

- Set `JOBSPY_DATA_DIR=/some/dir` to use a different directory, for example for tests or a
  second profile. `start.py` and `scrape.py` both honour it, so set it for both.
- `JOBSPY_MIN_INTERVAL_MINUTES` lowers the 15-minute schedule minimum (testing only).
- `backend/profile.json` is only the template for a brand-new database. Edit your profile in the
  UI.
- `data/` runtime files are git-ignored because they contain personal data.
- Back up by copying `jobspy.db` while the server is stopped, or with
  `sqlite3 data/jobspy.db ".backup backup.db"`.

## 7. Keeping the server running

- Simplest: leave `start.py` running in a terminal tab, or in `tmux`/`screen`
  (`tmux new -s jobspy '.venv/bin/python start.py --no-browser'`, detach with Ctrl+B D).
- Background: `nohup .venv/bin/python start.py --no-browser > ~/jobspy-server.log 2>&1 &`.
  Stop it with `kill <pid>`; a run in progress is cancelled cleanly.
- WSL stops when Windows shuts down or after `wsl --shutdown`. To start the dashboard at logon,
  add a Windows Task Scheduler task with trigger "At log on":
  - Program: `wsl.exe`
  - Arguments: `-e /home/anupam/JobSpy/.venv/bin/python /home/anupam/JobSpy/start.py --no-browser`
- If the server dies during a scrape, the run is marked `interrupted` on the next start, or as
  soon as another run needs the lock.

## 8. Development

- Backend tests: `.venv/bin/python -m pytest -q tests`. They make no network calls.
- Lint: `.venv/bin/ruff check backend tests scrape.py start.py`.
- Frontend: `cd frontend && npm run lint && npm run build`.
- Hot-reload UI: run `start.py`, then `cd frontend && npm run dev`. Vite proxies `/api` to
  `$PORT` (default 8000); set `VITE_PORT` and `JOBSPY_API_URL` to use other ports.
