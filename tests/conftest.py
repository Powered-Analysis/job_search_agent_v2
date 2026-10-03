import os
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest
import turso_serverless
import turso_serverless.dbapi

REPO_ROOT = Path(__file__).resolve().parent.parent
TABLES = (
    "postings",
    "search_findings",
    "search_runs",
    "cron_runs",
    "prompt_refinement_runs",
)
# What a violated CHECK / NOT NULL / UNIQUE constraint raises, per transport.
REJECTED = (sqlite3.IntegrityError, turso_serverless.dbapi.IntegrityError)
# The libSQL service container (CI environment); captured before any test edits the env.
HTTP_URL = os.environ["TURSO_DATABASE_URL"]


def venv_script(name: str) -> str:
    return str(Path(sys.executable).parent / name)


def raw_connect(url: str):
    """A connection that bypasses the app's database module, for inspecting state."""
    if url.startswith("file:"):
        return sqlite3.connect(url, uri=True, autocommit=True)
    return turso_serverless.connect(url, isolation_level=None)


def drop_all_tables(url: str) -> None:
    conn = raw_connect(url)
    try:
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    finally:
        conn.close()


def existing_tables(url: str) -> set[str]:
    conn = raw_connect(url)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {row[0] for row in rows}


def unique_url(prefix: str = "https://job-boards.greenhouse.io/acme/jobs/") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@pytest.fixture(params=["http", "file"])
def db_url(request, tmp_path, monkeypatch) -> str:
    url = HTTP_URL if request.param == "http" else f"file:{tmp_path / 'test.db'}"
    monkeypatch.setenv("TURSO_DATABASE_URL", url)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    return url


@pytest.fixture
def http_url(monkeypatch) -> str:
    monkeypatch.setenv("TURSO_DATABASE_URL", HTTP_URL)
    monkeypatch.delenv("TURSO_AUTH_TOKEN", raising=False)
    return HTTP_URL


@pytest.fixture
def conn(db_url):
    from jsa import db

    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def http_conn(http_url):
    from jsa import db

    connection = db.connect()
    yield connection
    connection.close()
