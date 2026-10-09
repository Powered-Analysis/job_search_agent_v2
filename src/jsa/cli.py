import argparse
import logging
import sys
from collections.abc import Callable
from dataclasses import asdict
from datetime import date

from jsa import db, prompts
from jsa.add import (
    NO_PACKET_JD,
    AddOutcome,
    Derived,
    MissingFieldsError,
    add_posting,
    require_fields,
)
from jsa.config import load_environment
from jsa.cron import cron
from jsa.deploy import deploy
from jsa.errors import JsaError
from jsa.generate import generate
from jsa.http import make_client
from jsa.inbox import inbox
from jsa.packet import build_packets
from jsa.refetch import refetch
from jsa.refine import accept, refine, reject
from jsa.review import review, revise
from jsa.search import RUNNERS, Summary, search
from jsa.tracker import track


def _init_db(args: argparse.Namespace) -> None:
    db.connect().close()


def _confirm(label: str, derived: str | None) -> str:
    while not (value := prompts.ask(label, derived or "").strip()):
        pass
    return value


def _add_fields(args: argparse.Namespace) -> Callable[[Derived], tuple[str, str]]:
    """Company and title for `jsa add`: the flags, else the derived values, confirmed unless --no-input."""

    def fields(derived: Derived) -> tuple[str, str]:
        if derived.capture_failure:
            print(f"Capture failed: {derived.capture_failure}")
        if args.no_input:
            try:
                return require_fields(
                    args.company or derived.company, args.title or derived.title
                )
            except MissingFieldsError as error:
                flags = " and ".join(f"--{name}" for name in error.missing)
                raise JsaError(f"{error}; pass {flags} or drop --no-input") from None
        return (
            args.company or _confirm("Company", derived.company),
            args.title or _confirm("Title", derived.title),
        )

    return fields


def _print_added(outcome: AddOutcome) -> None:
    if outcome.kind == "already_apply":
        print(f"Posting {outcome.posting_id}: already Apply; no change.")
    elif outcome.kind == "promoted":
        previous = outcome.previous_decision or "undecided"
        print(f"Posting {outcome.posting_id}: {previous} → Apply")
    else:
        message = f"Added posting {outcome.posting_id}: {outcome.company} — {outcome.title} (Apply)"
        print(message if outcome.has_jd else f"{message}; {NO_PACKET_JD}")


def _add(args: argparse.Namespace) -> None:
    with make_client() as client:
        _print_added(
            add_posting(
                client,
                args.url,
                date_posted=args.date_posted,
                fields=_add_fields(args),
            )
        )


def _inbox(args: argparse.Namespace) -> None:
    with make_client() as client:
        inbox(client)


def _review(args: argparse.Namespace) -> None:
    with make_client() as client:
        review(client)


def _revise(args: argparse.Namespace) -> None:
    revise(posting_id=args.id, last=args.last)


def _packet(args: argparse.Namespace) -> None:
    build_packets(args.id, dry_run=args.dry_run)


def _generate(args: argparse.Namespace) -> None:
    with make_client() as client:
        generate(client, args.id, dry_run=args.dry_run)


def _track(args: argparse.Namespace) -> None:
    track(args.id, dry_run=args.dry_run)


def _refine(args: argparse.Namespace) -> None:
    if args.accept:
        accept()
    elif args.reject:
        reject()
    else:
        refine(dry_run=args.dry_run)


def _refetch(args: argparse.Namespace) -> None:
    with make_client() as client:
        refetch(client, args.id, every_row=args.all, dry_run=args.dry_run)


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


def _deploy(args: argparse.Namespace) -> None:
    deploy(dry_run=args.dry_run, smoke=args.smoke)


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
        "inbox",
        help="the inbox machine's entrypoint: add each posting emailed to the jobs mailbox",
    ).set_defaults(run=_inbox)
    commands.add_parser(
        "review", help="decide Apply or Skip on each undecided posting"
    ).set_defaults(run=_review)
    revise_command = commands.add_parser(
        "revise", help="amend the decision and feedback of a decided posting"
    )
    revise_modes = revise_command.add_mutually_exclusive_group(required=True)
    revise_modes.add_argument("--id", type=_positive_int, help="the posting's id")
    revise_modes.add_argument(
        "--last", action="store_true", help="the posting decided most recently"
    )
    revise_command.set_defaults(run=_revise)
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
    generate_command = commands.add_parser(
        "generate",
        help="build each Apply posting's packet with a resume checklist and an ATS redline, then track it",
    )
    generate_command.add_argument(
        "--id",
        type=_positive_int,
        help="generate this Apply posting only, rewriting its checklist",
    )
    generate_command.add_argument(
        "--dry-run",
        action="store_true",
        help="show the folders without building anything",
    )
    generate_command.set_defaults(run=_generate)
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
    refine_command = commands.add_parser(
        "refine", help="propose search-profile edits learned from your decisions"
    )
    refine_modes = refine_command.add_mutually_exclusive_group()
    refine_modes.add_argument(
        "--dry-run",
        action="store_true",
        help="show the decisions in scope without calling the model or recording a run",
    )
    refine_modes.add_argument(
        "--accept",
        action="store_true",
        help="replace your search fragments with the resolved proposal",
    )
    refine_modes.add_argument(
        "--reject",
        action="store_true",
        help="discard the pending proposal and keep your search fragments",
    )
    refine_command.set_defaults(run=_refine)
    refetch_command = commands.add_parser(
        "refetch",
        help="update stored postings, their tracker titles, and packets from the employer's edits",
    )
    refetch_command.add_argument(
        "--id", type=_positive_int, help="reconcile this posting only"
    )
    refetch_command.add_argument(
        "--all",
        action="store_true",
        help="reconcile every posting, not just unapplied Apply rows",
    )
    refetch_command.add_argument(
        "--dry-run", action="store_true", help="show the changes without making them"
    )
    refetch_command.set_defaults(run=_refetch)
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
    deploy_command = commands.add_parser(
        "deploy", help="build the image and swap it onto the scheduled Fly machine"
    )
    deploy_mode = deploy_command.add_mutually_exclusive_group()
    deploy_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="validate the search profile and list the files that would ship; no build",
    )
    deploy_mode.add_argument(
        "--smoke",
        action="store_true",
        help="build, then run one ungated cron on a throwaway machine; the scheduled machine is untouched",
    )
    deploy_command.set_defaults(run=_deploy)
    args = parser.parse_args()

    # The search trace (steps, heartbeat, cost) goes to the log (PRD 01).
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # httpx logs every request at INFO, which would bury that trace.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_environment()
    try:
        args.run(args)
    except JsaError as error:
        print(f"jsa: {error}", file=sys.stderr)
        sys.exit(1)
