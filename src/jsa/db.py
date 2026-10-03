"""The one database module (PRD 02): every connection and every SQL statement lives here."""

import sqlite3
from collections.abc import Callable

import turso_serverless

from jsa.config import database_auth_token, database_url
from jsa.naming import normalize_company, title_slug
from jsa.urls import canonicalize_url

type Connection = sqlite3.Connection | turso_serverless.Connection

# The one timestamp format: UTC ISO-8601 with milliseconds. SQLite compares
# timestamps as text, so every stored one must come from here (PRD 02).
NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"

SEARCH_AGENTS = ("claude", "perplexity", "gemini")
POSTING_SOURCES = (*SEARCH_AGENTS, "manual")
DECISIONS = ("Apply", "Skip")
VERIFICATIONS = (
    "verified",
    "verified_no_date",
    "reachable",
    "reachable_no_date",
    "aggregator",
    "unsupported",
    "unverifiable",
    "not_on_index",
    "page_closed",
    "out_of_window",
)
ATS_DATE_KINDS = ("updated", "created", "published")
SEARCH_TRIGGERS = ("scheduled", "hand", "smoke")
SEARCH_OUTCOMES = ("ok", "failed")


def _text_in(column: str, values: tuple[str, ...], *, not_null: bool = False) -> str:
    allowed = ", ".join(repr(v) for v in values)
    null = " NOT NULL" if not_null else ""
    return f"{column} TEXT{null} CHECK ({column} IN ({allowed}))"


# Each table's columns are defined once; the CREATE is built from this (PRD 02).
TABLES: dict[str, tuple[str, ...]] = {
    "postings": (
        "id INTEGER PRIMARY KEY",
        "company TEXT",
        "title TEXT",
        "url TEXT",
        "date_posted TEXT",
        "canonical_url TEXT NOT NULL UNIQUE",
        "normalized_company TEXT",
        "title_slug TEXT",
        "jd_markdown TEXT",
        "location TEXT",
        _text_in("search_agent", POSTING_SOURCES),
        f"first_seen_at TEXT NOT NULL DEFAULT ({NOW})",
        _text_in("decision", DECISIONS),
        "fit_feedback TEXT",
        "decided_at TEXT",
        "added_to_tracker INTEGER NOT NULL DEFAULT 0",
        "closed_at TEXT",
    ),
    "search_findings": (
        "run_date TEXT NOT NULL",
        _text_in("agent", SEARCH_AGENTS, not_null=True),
        "canonical_url TEXT NOT NULL",
        "window_hours INTEGER",
        "rank INTEGER",
        "found_at TEXT",
        _text_in("decision", DECISIONS),
        _text_in("verification", VERIFICATIONS, not_null=True),
        "ats_date TEXT",
        _text_in("ats_date_kind", ATS_DATE_KINDS),
        "model TEXT",
        "effort TEXT",
        "PRIMARY KEY (run_date, agent, canonical_url)",
    ),
    "search_runs": (
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "run_date TEXT",
        _text_in("trigger", SEARCH_TRIGGERS),
        "agent TEXT",
        "window_hours INTEGER",
        "mode TEXT",
        "model TEXT",
        "effort TEXT",
        "started_at TEXT",
        "finished_at TEXT",
        _text_in("outcome", SEARCH_OUTCOMES),
        "error TEXT",
        "summary TEXT",
        "warnings TEXT",
    ),
    "cron_runs": (
        "run_date TEXT PRIMARY KEY",
        f"claimed_at TEXT NOT NULL DEFAULT ({NOW})",
    ),
    "prompt_refinement_runs": (
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        f"run_at TEXT NOT NULL DEFAULT ({NOW})",
        "cutoff TEXT",
        "considered INTEGER",
        "changed INTEGER",
    ),
}

# Schema changes ship as migrations that run before the creates (PRD 02):
# a rebuild parks data under another name, and a create run first would fill
# that gap with an empty table. The initial schema needs none.
MIGRATIONS: tuple[Callable[[Connection], None], ...] = ()


def ensure_schema(conn: Connection) -> None:
    for migrate in MIGRATIONS:
        migrate(conn)
    for table, columns in TABLES.items():
        conn.execute(f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)})")


def connect() -> Connection:
    url = database_url()
    conn: Connection
    if url.startswith("file:"):
        # XC-8: autocommit, or every write is silently rolled back on close.
        conn = sqlite3.connect(url, uri=True, autocommit=True)
    else:
        # XC-8: isolation_level=None is the only real autocommit here; the
        # client's `autocommit` attribute is a silent no-op.
        conn = turso_serverless.connect(
            url, auth_token=database_auth_token(), isolation_level=None
        )
    ensure_schema(conn)
    return conn


def insert_posting(
    conn: Connection,
    *,
    company: str,
    title: str,
    url: str,
    search_agent: str,
    date_posted: str | None = None,
) -> int | None:
    """Return the new posting's id, or None when its canonical URL is already stored."""
    manual = search_agent == "manual"
    # A manual add is decided in the INSERT itself so the row is never visible as undecided.
    decision, decided_at = ("'Apply'", NOW) if manual else ("NULL", "NULL")
    rows = conn.execute(
        f"""
        INSERT INTO postings (
            company, title, url, date_posted, canonical_url,
            normalized_company, title_slug, search_agent, decision, decided_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, {decision}, {decided_at})
        ON CONFLICT(canonical_url) DO NOTHING
        RETURNING id
        """,
        (
            company,
            title,
            url,
            date_posted,
            canonicalize_url(url),
            normalize_company(company),
            title_slug(title),
            search_agent,
        ),
    ).fetchall()
    return rows[0][0] if rows else None


def capture_jd(
    conn: Connection,
    posting_id: int,
    *,
    jd_markdown: str,
    location: str | None,
    title: str | None = None,
) -> None:
    """Store the JD; a non-empty title also replaces the title and its slug."""
    if title:
        conn.execute(
            "UPDATE postings SET jd_markdown = ?, location = ?, title = ?, title_slug = ? WHERE id = ?",
            (jd_markdown, location, title, title_slug(title), posting_id),
        )
    else:
        conn.execute(
            "UPDATE postings SET jd_markdown = ?, location = ? WHERE id = ?",
            (jd_markdown, location, posting_id),
        )
