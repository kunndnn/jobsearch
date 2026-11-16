"""Start the JobSpy India dashboard: API, built UI and the scrape scheduler."""

from __future__ import annotations

import argparse
import os
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import config, db, normalize, repo  # noqa: E402
from backend.scheduler import STARTUP_DELAY_SECONDS, schedule_snapshot  # noqa: E402

DEFAULT_PORT = 8000


def default_port() -> int:
    raw = os.environ.get("PORT", "").strip()
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def port_number(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument(
        "--port", type=port_number, default=default_port(), help="port (default $PORT or 8000)"
    )
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser tab")
    parser.add_argument("--reload", action="store_true", help="restart on backend code changes")
    return parser.parse_args(argv)


def _when(value: object) -> str:
    moment = normalize.parse_iso(value) if isinstance(value, str) else None
    if moment is None:
        return "never"
    return moment.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def schedule_line(snapshot: dict[str, object], now: datetime) -> str:
    if snapshot.get("run_on_startup"):
        startup = f"; a startup scrape runs {STARTUP_DELAY_SECONDS:.0f}s after start"
    else:
        startup = ""
    if not snapshot.get("enabled"):
        return f"off (enable it in the Automation tab){startup}"
    every = f"every {snapshot['interval_minutes']} min"
    upcoming = normalize.parse_iso(str(snapshot.get("next_run_at") or ""))
    if upcoming is not None and upcoming <= now:
        return f"{every}; overdue, runs {STARTUP_DELAY_SECONDS:.0f}s after start"
    return f"{every}; next run {_when(snapshot.get('next_run_at'))}{startup}"


def print_banner(url: str) -> None:
    db.init_db()
    with db.connection() as conn:
        profile = repo.get_profile(conn)
        snapshot = schedule_snapshot(conn)
        stats = repo.get_stats(conn)
    lines = [
        "JobSpy India Dashboard",
        f"  Candidate : {profile.name}" + (f" - {profile.headline}" if profile.headline else ""),
        f"  Database  : {config.db_path()} ({stats['total_stored']} jobs stored)",
        f"  Last run  : {_when(snapshot.get('last_run_at'))}",
        f"  Schedule  : {schedule_line(snapshot, normalize.utc_now())}",
        f"  Serving at: {url}",
    ]
    if not (config.frontend_dist() / "index.html").is_file():
        lines.append(
            "  UI        : not built - run `cd frontend && npm install && npm run build`"
            " (the API works meanwhile)"
        )
    rule = "=" * 64
    print("\n".join([rule, *lines, rule]), flush=True)


def browser_url(host: str, port: int) -> str:
    shown = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    if ":" in shown:
        shown = f"[{shown}]"
    return f"http://{shown}:{port}"


def stuck_runs() -> list[int]:
    """Runs of this process's server whose worker is still inside a board request
    after shutdown (their rows were already marked interrupted)."""
    server = sys.modules.get("backend.server")
    state = getattr(getattr(server, "app", None), "state", None)
    service = getattr(state, "service", None)
    return list(service.active_run_ids()) if service is not None else []


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    import uvicorn

    url = browser_url(args.host, args.port)
    print_banner(url)
    if not args.no_browser:
        timer = threading.Timer(1.5, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        reload_dirs=[str(PROJECT_ROOT / "backend")] if args.reload else None,
        app_dir=str(PROJECT_ROOT),
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    code = main()
    if stuck_runs():
        # Exit now instead of joining jobspy's request threads at interpreter exit.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)
    sys.exit(code)
