"""The one database module (PRD 02): every connection and every SQL statement lives here."""

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

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


def record_finding(
    conn: Connection,
    *,
    run_date: str,
    agent: str,
    canonical_url: str,
    window_hours: int,
    rank: int,
    verification: str,
    ats_date: str | None,
    ats_date_kind: str | None,
    model: str | None,
    effort: str | None,
) -> None:
    """Log one emitted posting before any insert; a repeat of the same day's find is a no-op (PRD 02)."""
    conn.execute(
        f"""
        INSERT INTO search_findings (
            run_date, agent, canonical_url, window_hours, rank, found_at, decision,
            verification, ats_date, ats_date_kind, model, effort
        ) VALUES (
            ?, ?, ?, ?, ?, {NOW},
            (SELECT decision FROM postings WHERE canonical_url = ?),
            ?, ?, ?, ?, ?
        )
        ON CONFLICT(run_date, agent, canonical_url) DO NOTHING
        """,
        (
            run_date,
            agent,
            canonical_url,
            window_hours,
            rank,
            canonical_url,
            verification,
            ats_date,
            ats_date_kind,
            model,
            effort,
        ),
    )


def open_search_run(
    conn: Connection,
    *,
    trigger: str,
    run_date: str,
    agent: str,
    window_hours: int,
    mode: str,
) -> int:
    """Open a run's row before its runner starts; a row never closed means the process died (PRD 02)."""
    rows = conn.execute(
        f"""
        INSERT INTO search_runs (run_date, trigger, agent, window_hours, mode, started_at)
        VALUES (?, ?, ?, ?, ?, {NOW})
        RETURNING id
        """,
        (run_date, trigger, agent, window_hours, mode),
    ).fetchall()
    return rows[0][0]


def close_search_run(
    conn: Connection,
    run_id: int,
    *,
    outcome: str,
    error: str | None,
    summary: str | None,
    warnings: str | None,
    model: str | None,
    effort: str | None,
) -> None:
    """The run row's one closing update; the model and effort a runner reports are known only now."""
    conn.execute(
        f"""
        UPDATE search_runs
        SET finished_at = {NOW}, outcome = ?, error = ?, summary = ?, warnings = ?,
            model = COALESCE(?, model), effort = COALESCE(?, effort)
        WHERE id = ?
        """,
        (outcome, error, summary, warnings, model, effort, run_id),
    )


def claim_cron_day(conn: Connection, run_date: str) -> bool:
    """True when this call won the day; every later claim of it is False (PRD 02)."""
    rows = conn.execute(
        "INSERT INTO cron_runs (run_date) VALUES (?) ON CONFLICT(run_date) DO NOTHING RETURNING run_date",
        (run_date,),
    ).fetchall()
    return bool(rows)


def cron_run_dates(conn: Connection) -> set[str]:
    """Every day the cron claimed, as profile-timezone dates."""
    return {row[0] for row in conn.execute("SELECT run_date FROM cron_runs").fetchall()}


def health_search_runs(conn: Connection, since: datetime) -> list[tuple]:
    """The runs review's health line reads, oldest first.

    Runs started since `since`, every run with no outcome, and each agent's latest run:
    (id, run_date, trigger, agent, started_at, outcome, error, summary, warnings).
    """
    return conn.execute(
        """
        SELECT id, run_date, trigger, agent, started_at, outcome, error, summary, warnings
        FROM search_runs
        WHERE started_at >= ? OR outcome IS NULL OR id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (
                    PARTITION BY agent ORDER BY started_at DESC, id DESC
                ) AS n FROM search_runs
            ) WHERE n = 1
        )
        ORDER BY started_at, id
        """,
        (
            since.astimezone(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        ),
    ).fetchall()


def find_posting(conn: Connection, url: str) -> tuple[int, str | None] | None:
    """A UX-only read: the posting's (id, decision) for this URL's canonical form."""
    rows = conn.execute(
        "SELECT id, decision FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchall()
    return (rows[0][0], rows[0][1]) if rows else None


def review_backlog(conn: Connection) -> list[tuple]:
    """Undecided, unclosed postings, oldest first: (id, company, title, location, url)."""
    return conn.execute(
        """
        SELECT id, company, title, location, url FROM postings
        WHERE decision IS NULL AND closed_at IS NULL
        ORDER BY first_seen_at, id
        """
    ).fetchall()


def mark_closed(conn: Connection, posting_id: int) -> None:
    """Record that a re-check found the posting closed; the decision is never touched (PRD 02)."""
    conn.execute(
        f"UPDATE postings SET closed_at = {NOW} WHERE id = ? AND closed_at IS NULL",
        (posting_id,),
    )


def _write_decision(
    conn: Connection, url: str, decision: str, feedback_assignment: str, params: tuple
) -> None:
    canonical = canonicalize_url(url)
    # The two writes are not atomic over HTTP. Telemetry goes first so a retry
    # still sees the old decision and repeats both.
    conn.execute(
        "UPDATE search_findings SET decision = ? WHERE canonical_url = ?",
        (decision, canonical),
    )
    conn.execute(
        f"UPDATE postings SET decision = ?, {feedback_assignment}decided_at = {NOW} WHERE canonical_url = ?",
        (decision, *params, canonical),
    )


def set_decision(conn: Connection, url: str, decision: str) -> None:
    """Change a posting's decision, keeping its feedback; telemetry follows (PRD 02)."""
    _write_decision(conn, url, decision, "", ())


def record_decision(
    conn: Connection, url: str, decision: str, feedback: str | None
) -> None:
    """Store a decision with its feedback together; telemetry follows (PRD 02)."""
    _write_decision(conn, url, decision, "fit_feedback = ?, ", (feedback,))


def clear_decision(conn: Connection, url: str) -> None:
    """Un-decide a posting. Telemetry keeps the last real decision (PRD 02)."""
    conn.execute(
        "UPDATE postings SET decision = NULL, fit_feedback = NULL, decided_at = NULL WHERE canonical_url = ?",
        (canonicalize_url(url),),
    )
