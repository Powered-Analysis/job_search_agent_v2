import argparse
import sys
from datetime import date

from jsa import db
from jsa.add import add_posting
from jsa.config import load_environment
from jsa.errors import JsaError
from jsa.http import make_client
from jsa.review import review


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
    args = parser.parse_args()

    load_environment()
    try:
        args.run(args)
    except JsaError as error:
        print(f"jsa: {error}", file=sys.stderr)
        sys.exit(1)
