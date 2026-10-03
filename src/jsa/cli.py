import argparse
import sys

from jsa import db
from jsa.config import load_environment
from jsa.errors import JsaError


def _init_db() -> None:
    db.connect().close()


def main() -> None:
    parser = argparse.ArgumentParser(prog="jsa", description="Job Search Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    init_db = commands.add_parser(
        "init-db", help="create the database tables (idempotent)"
    )
    init_db.set_defaults(run=_init_db)
    args = parser.parse_args()

    load_environment()
    try:
        args.run()
    except JsaError as error:
        print(f"jsa: {error}", file=sys.stderr)
        sys.exit(1)
