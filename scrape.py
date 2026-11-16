"""Run one scrape now, from a terminal or cron (trigger "cli").

Overrides apply to this run only; everything else comes from the dashboard
settings. Exit codes: 0 completed/partial, 1 failed (or bad arguments),
2 another run holds the lock, 130 cancelled (Ctrl+C or Stop in the dashboard).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path
from typing import NoReturn

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from backend import db, normalize, repo  # noqa: E402
from backend.models import ScrapeOverrides  # noqa: E402
from backend.repo import ScrapeAlreadyRunning  # noqa: E402
from backend.scheduler import schedule_snapshot  # noqa: E402
from backend.scrape_service import (  # noqa: E402
    PreparedRun,
    ProgressEvent,
    RunResult,
    ScrapeService,
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 2
EXIT_CANCELLED = 130
CTRL_C_GRACE_SECONDS = 3.0
CTRL_C_MESSAGE = "Cancelled from the terminal (Ctrl+C)"


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        # Usage errors exit 1: exit code 2 means "another run holds the lock".
        self.print_usage(sys.stderr)
        self.exit(EXIT_FAILED, f"{self.prog}: error: {message}\n")


def split_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="scrape.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--terms", type=split_list, help='search terms, e.g. "Node.js Developer,SDE 1"')
    parser.add_argument("--locations", type=split_list, help='locations, e.g. "Bengaluru,Remote"')
    parser.add_argument("--sites", type=split_list, help="boards, e.g. linkedin,naukri")
    parser.add_argument("--results", type=int, metavar="N", help="results per query (1-50)")
    parser.add_argument("--hours-old", type=int, metavar="N", help="max posting age in hours")
    parser.add_argument(
        "--no-descriptions", action="store_true", help="skip fetching full descriptions (faster)"
    )
    parser.add_argument("--quiet", action="store_true", help="print only the summary")
    return parser


def build_overrides(args: argparse.Namespace) -> ScrapeOverrides:
    return ScrapeOverrides(
        search_terms=args.terms,
        locations=args.locations,
        sites=args.sites,
        results_per_query=args.results,
        hours_old=args.hours_old,
        fetch_description=False if args.no_descriptions else None,
    )


def _issues(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors(include_url=False):
        field = ".".join(str(part) for part in err["loc"]) or "arguments"
        lines.append(f"  {field}: {err['msg']}")
    return "\n".join(lines)


class Printer:
    def __init__(self, quiet: bool) -> None:
        self.quiet = quiet

    def say(self, text: str = "") -> None:
        print(text, flush=True)

    def header(self, prepared: PreparedRun) -> None:
        if self.quiet:
            return
        settings = prepared.settings
        self.say(
            f"Scrape run #{prepared.run_id}: {plural(len(prepared.queries), 'query', 'queries')} "
            f"({plural(len(settings.search_terms), 'term')} x "
            f"{plural(len(settings.locations), 'location')}) on {', '.join(settings.sites)}"
        )

    def event(self, event: ProgressEvent) -> None:
        if self.quiet:
            return
        if event.kind == "query_started":
            self.say(f"[{event.index}/{event.total}] {event.term} @ {event.location} ...")
        elif event.kind == "query_finished":
            self.say(
                f"    {event.found} found, {event.new} new, {event.updated} updated"
                + (f", {event.duplicates} duplicate(s)" if event.duplicates else "")
            )
            for site, message in event.errors.items():
                self.say(f"    {site} error: {message}")
        elif event.kind == "waiting":
            self.say(f"    waiting {event.wait_seconds:.0f}s ...")

    def summary(self, result: RunResult) -> None:
        with db.connection() as conn:
            stats = repo.get_stats(conn)
            snapshot = schedule_snapshot(conn)
        self.say(f"Run #{result.run_id}: {result.message}")
        self.say(
            f"  New: {result.new_jobs}  Updated: {result.updated_jobs}  "
            f"Found: {result.jobs_found}  (queries run: {result.queries_done}/{result.total_queries})"
        )
        self.say(
            f"  Database: {stats['total_jobs']} jobs in the default view, "
            f"{stats['total_stored']} stored"
        )
        for site, message in result.errors.items():
            self.say(f"  {site} error: {message}")
        self.say(f"  Next scheduled run: {next_run_text(snapshot)}")


def plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


def next_run_text(snapshot: dict[str, object]) -> str:
    if not snapshot.get("enabled"):
        return "automation is off (enable it in the dashboard's Automation tab)"
    upcoming = normalize.parse_iso(str(snapshot.get("next_run_at") or ""))
    if upcoming is None:
        return "not scheduled"
    when = upcoming.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    return f"{when} (only while the dashboard server is running)"


def exit_code(status: str) -> int:
    if status in ("completed", "partial"):
        return EXIT_OK
    if status == "cancelled":
        return EXIT_CANCELLED
    return EXIT_FAILED


def _cancel(service: ScrapeService, run_id: int, done: threading.Event, out: Printer) -> int:
    out.say("\nCtrl+C: cancelling the run ...")
    service.notify_cancel(run_id, CTRL_C_MESSAGE)
    try:
        done.wait(CTRL_C_GRACE_SECONDS)
    except KeyboardInterrupt:
        pass
    if not done.is_set():
        # Stuck inside a board request: record the cancel ourselves and leave.
        service.force_finish(run_id, "cancelled", CTRL_C_MESSAGE)
    out.say(f"Run #{run_id} cancelled.")
    return EXIT_CANCELLED


def execute(service: ScrapeService, prepared: PreparedRun, out: Printer) -> int:
    outcome: dict[str, object] = {}
    done = threading.Event()

    def work() -> None:
        try:
            outcome["result"] = service.execute(prepared, progress=out.event)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            done.set()

    # The run lives in a worker thread so Ctrl+C is handled at once, even while a
    # board request is in flight. Waiting on an Event, not Thread.join(): a
    # KeyboardInterrupt inside join() can mark a live thread as stopped (3.12).
    worker = threading.Thread(target=work, name=f"scrape-cli-{prepared.run_id}", daemon=True)
    worker.start()
    try:
        while not done.wait(0.25):
            pass
    except KeyboardInterrupt:
        return _cancel(service, prepared.run_id, done, out)
    result = outcome.get("result")
    if not isinstance(result, RunResult):
        out.say(f"Run #{prepared.run_id} crashed: {outcome.get('error')!r}")
        return EXIT_FAILED
    out.summary(result)
    return exit_code(result.status)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    out = Printer(args.quiet)
    try:
        overrides = build_overrides(args)
        db.init_db()
        service = ScrapeService()
        prepared = service.prepare(overrides, "cli")
    except ValidationError as exc:
        print(f"Invalid options:\n{_issues(exc)}", file=sys.stderr)
        return EXIT_FAILED
    except ScrapeAlreadyRunning as exc:
        print(f"Another scrape (run #{exc.run_id}) is already running; not starting.", file=sys.stderr)
        return EXIT_LOCKED
    out.header(prepared)
    return execute(service, prepared, out)


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    if code == EXIT_CANCELLED:
        # Skip joining jobspy's request threads, which may still be mid-request.
        os._exit(code)
    sys.exit(code)
