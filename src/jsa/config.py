"""Environment configuration. Secrets and machine-local settings live in `.env` (XC-11)."""

import os

from dotenv import find_dotenv, load_dotenv

from jsa.errors import JsaError


def load_environment() -> None:
    # Real environment variables win over `.env`; Fly has no `.env` at all.
    load_dotenv(find_dotenv(usecwd=True))


def database_url() -> str:
    url = os.environ.get("TURSO_DATABASE_URL")
    if not url:
        raise JsaError(
            "TURSO_DATABASE_URL is not set. Copy .env.example to .env and fill it in."
        )
    return url


def database_auth_token() -> str | None:
    # Hosted Turso needs it; file: URLs and CI's local libSQL server do not.
    return os.environ.get("TURSO_AUTH_TOKEN") or None
