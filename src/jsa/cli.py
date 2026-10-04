import argparse
import logging
import sys
from dataclasses import asdict
from datetime import date

from jsa import db
from jsa.add import add_posting
from jsa.config import load_environment
from jsa.cron import cron
from jsa.errors import JsaError
from jsa.http import make_client
from jsa.packet import build_packets
from jsa.review import review
from jsa.search import RUNNERS, Summary, search
from jsa.tracker import track


def _init_db(args: argparse.Namespace) -> None:
    db.connect().close()


def _add(args: argparse.Namespace) -> None:
    with make_client() as client:
        add_posting(
            client,
            args.url,
            company=args.company,
            title=args.title,
            date_posted=args.date_posted,
            no_input=args.no_input,
        )


def _review(args: argparse.Namespace) -> None:
    with make_client() as client:
        review(client)


def _packet(args: argparse.Namespace) -> None:
    build_packets(args.id, dry_run=args.dry_run)


def _track(args: argparse.Namespace) -> None:
    track(args.id, dry_run=args.dry_run)


def _print_search(summary: Summary, warnings: list[str]) -> None:
    for key, value in asdict(summary).items():
        print(f"{key}: {value}")
    for warning in warnings:
        print(f"warning: {warning}")


def _search(args: argparse.Namespace) -> None:
    with make_client() as client:
        _print_search(*search(client, args.agent, args.window_hours))


def _cron(args: argparse.Namespace) -> None:
    with make_client() as client:
        run = cron(client, ungated=args.ungated)
    if run.skipped:
        print(run.skipped)
    for summary, warnings in run.results:
        _print_search(summary, warnings)
    if run.failures:
        total = len(run.results) + len(run.failures)
        raise JsaError(
            f"{len(run.failures)} of {total} searches failed: "
            + "; ".join(run.failures)
        )


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        value = 0
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive whole number")
    return value


def _iso_date(text: str) -> str:
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a YYYY-MM-DD date") from None


def main() -> None:
    parser = argparse.ArgumentParser(prog="jsa", description="Job Search Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    init_db = commands.add_parser(
        "init-db", help="create the database tables (idempotent)"
    )
    init_db.set_defaults(run=_init_db)
    add = commands.add_parser(
        "add", help="add a posting you already want, decided Apply"
    )
    add.add_argument("url", help="the employer's own posting URL")
    add.add_argument("--company", help="company name (otherwise derived and confirmed)")
    add.add_argument("--title", help="job title (otherwise derived and confirmed)")
    add.add_argument("--date-posted", type=_iso_date, help="posting date, YYYY-MM-DD")
    add.add_argument(
        "--no-input",
        action="store_true",
        help="accept derived values without prompting",
    )
    add.set_defaults(run=_add)
    commands.add_parser(
        "review", help="decide Apply or Skip on each undecided posting"
    ).set_defaults(run=_review)
    packet = commands.add_parser(
        "packet", help="create a folder with the job description and a resume copy"
    )
    packet.add_argument(
        "--id", type=_positive_int, help="build this Apply posting's packet only"
    )
    packet.add_argument(
        "--dry-run", action="store_true", help="show the folders without creating them"
    )
    packet.set_defaults(run=_packet)
    track_command = commands.add_parser(
        "track", help="append Apply postings to the Google Sheet tracker"
    )
    track_command.add_argument(
        "--id", type=_positive_int, help="track this Apply posting only"
    )
    track_command.add_argument(
        "--dry-run", action="store_true", help="show the rows without appending them"
    )
    track_command.set_defaults(run=_track)
    search_command = commands.add_parser(
        "search", help="run one search-and-capture cycle"
    )
    search_command.add_argument("--agent", required=True, choices=sorted(RUNNERS))
    search_command.add_argument(
        "--window-hours",
        required=True,
        type=_positive_int,
        help="how far back to look for postings",
    )
    search_command.set_defaults(run=_search)
    cron_command = commands.add_parser(
        "cron", help="the scheduled machine's entrypoint: run the day's searches if due"
    )
    cron_command.add_argument(
        "--ungated",
        action="store_true",
        help="skip the time-of-day gate and the daily claim (smoke test)",
    )
    cron_command.set_defaults(run=_cron)
    args = parser.parse_args()

    # The search trace (steps, heartbeat, cost) goes to the log (PRD 01).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    load_environment()
    try:
        args.run(args)
    except JsaError as error:
        print(f"jsa: {error}", file=sys.stderr)
        sys.exit(1)
