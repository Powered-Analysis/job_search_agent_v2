"""Environment configuration. Secrets and machine-local settings live in `.env` (XC-11)."""

import os

from dotenv import find_dotenv, load_dotenv

from jsa.errors import JsaError


class MissingKeyError(JsaError, RuntimeError):
    """A runner's API key is not set; raised before any model call."""


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


def api_key(name: str) -> str:
    """A model API key, validated only when its runner runs so other commands work without it."""
    key = os.environ.get(name)
    if not key:
        raise MissingKeyError(f"{name} is not set. Add it to .env (see .env.example).")
    return key


def search_anthropic_api_key() -> str | None:
    # Unset in development, where the Claude CLI uses its inherited credential (XC-1).
    return os.environ.get("JSA_SEARCH_ANTHROPIC_API_KEY") or None


def inbox_gws_credentials() -> str:
    """The jobs mailbox's exported `gws` credential (XC-1); inbox app only, so validated only by `jsa inbox`."""
    credential = os.environ.get("JSA_INBOX_GWS_CREDENTIALS")
    if not credential:
        raise JsaError(
            "JSA_INBOX_GWS_CREDENTIALS is not set. Add it to .env (see .env.example)."
        )
    return credential


def owner_gws_credentials() -> str | None:
    """The owner's exported `gws` credential (XC-1); set on the inbox app only. Unset locally, where `gws` uses its own login."""
    return os.environ.get("JSA_GWS_CREDENTIALS") or None


def gws_bin() -> str:
    return os.environ.get("JSA_GWS_BIN") or "gws"


def fly_bin() -> str:
    return os.environ.get("JSA_FLY_BIN") or "fly"


def pandoc_bin() -> str:
    return os.environ.get("JSA_PANDOC_BIN") or "pandoc"


def generate_workers() -> int:
    raw = os.environ.get("JSA_GENERATE_WORKERS") or "3"
    try:
        workers = int(raw)
    except ValueError:
        workers = 0
    if workers < 1:
        raise JsaError(
            f"JSA_GENERATE_WORKERS must be a positive whole number, not {raw!r}."
        )
    return workers
