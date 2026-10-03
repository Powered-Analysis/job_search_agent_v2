import re
import uuid

import pytest
from conftest import REJECTED, TABLES, drop_all_tables, existing_tables, raw_connect

from jsa import db
from jsa.errors import JsaError


def test_connect_creates_every_missing_table(db_url):
    drop_all_tables(db_url)
    assert not set(TABLES) & existing_tables(db_url)
    db.connect().close()
    assert set(TABLES) <= existing_tables(db_url)


def test_connect_recreates_a_single_missing_table(db_url):
    db.connect().close()
    conn = raw_connect(db_url)
    conn.execute("DROP TABLE cron_runs")
    conn.close()
    db.connect().close()
    assert "cron_runs" in existing_tables(db_url)


def test_schema_creation_is_idempotent_and_keeps_data(db_url):
    drop_all_tables(db_url)
    conn = db.connect()
    run_date = uuid.uuid4().hex
    conn.execute("INSERT INTO cron_runs (run_date) VALUES (?)", (run_date,))
    conn.close()

    raw = raw_connect(db_url)
    before = raw.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    raw.close()

    for _ in range(2):
        db.connect().close()

    raw = raw_connect(db_url)
    after = raw.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows = raw.execute("SELECT run_date FROM cron_runs").fetchall()
    raw.close()
    assert after == before
    assert rows == [(run_date,)]


def test_connect_without_database_url_names_variable_and_env_example(monkeypatch):
    monkeypatch.delenv("TURSO_DATABASE_URL", raising=False)
    with pytest.raises(JsaError) as excinfo:
        db.connect()
    assert "TURSO_DATABASE_URL" in str(excinfo.value)
    assert ".env.example" in str(excinfo.value)


def test_empty_database_url_counts_as_unset(monkeypatch):
    monkeypatch.setenv("TURSO_DATABASE_URL", "")
    with pytest.raises(JsaError) as excinfo:
        db.connect()
    assert "TURSO_DATABASE_URL" in str(excinfo.value)


def test_write_persists_after_close_on_http(http_url):
    posting_id = None
    url = f"https://jobs.lever.co/acme/{uuid.uuid4().hex}"
    conn = db.connect()
    posting_id = db.insert_posting(
        conn, company="Acme", title="Engineer", url=url, search_agent="claude"
    )
    conn.close()

    raw = raw_connect(http_url)
    rows = raw.execute("SELECT id FROM postings WHERE id = ?", (posting_id,)).fetchall()
    raw.close()
    assert rows == [(posting_id,)]


def test_write_persists_after_close_on_file(tmp_path, monkeypatch):
    url = f"file:{tmp_path / 'persist.db'}"
    monkeypatch.setenv("TURSO_DATABASE_URL", url)
    conn = db.connect()
    posting_id = db.insert_posting(
        conn,
        company="Acme",
        title="Engineer",
        url="https://jobs.lever.co/acme/x1",
        search_agent="claude",
    )
    conn.close()
    reopened = db.connect()
    rows = reopened.execute("SELECT id FROM postings").fetchall()
    reopened.close()
    assert rows == [(posting_id,)]


# --- CHECK constraints -------------------------------------------------------

POSTING_SQL = (
    "INSERT INTO postings (company, title, url, canonical_url, search_agent, decision) "
    "VALUES ('Acme', 'Engineer', ?, ?, ?, ?)"
)
FINDING_SQL = (
    "INSERT INTO search_findings (run_date, agent, canonical_url, window_hours, rank, found_at, "
    "decision, verification, ats_date, ats_date_kind, model, effort) "
    "VALUES (?, ?, ?, 24, 1, '2026-01-01T00:00:00.000Z', NULL, ?, NULL, ?, 'm', NULL)"
)
RUN_SQL = (
    "INSERT INTO search_runs (run_date, trigger, agent, window_hours, mode, model, effort, "
    "started_at, finished_at, outcome, error, summary, warnings) "
    "VALUES ('2026-01-01', ?, 'claude', 24, 'strict', 'm', NULL, '2026-01-01T00:00:00.000Z', "
    "NULL, ?, NULL, NULL, NULL)"
)

VERIFICATIONS = [
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
]


def _posting(conn, agent, decision):
    url = f"https://example.com/{uuid.uuid4().hex}"
    conn.execute(POSTING_SQL, (url, url, agent, decision))


def _finding(conn, agent="claude", verification="verified", kind=None):
    conn.execute(
        FINDING_SQL, (uuid.uuid4().hex, agent, uuid.uuid4().hex, verification, kind)
    )


def _run(conn, trigger="scheduled", outcome=None):
    conn.execute(RUN_SQL, (trigger, outcome))


@pytest.mark.parametrize("agent", ["claude", "perplexity", "gemini", "manual"])
def test_postings_search_agent_accepts_listed_values(conn, agent):
    _posting(conn, agent, None)


@pytest.mark.parametrize("agent", ["bogus", "Claude", "", "openai"])
def test_postings_search_agent_rejects_others(conn, agent):
    with pytest.raises(REJECTED):
        _posting(conn, agent, None)


@pytest.mark.parametrize("decision", ["Apply", "Skip", None])
def test_postings_decision_accepts_listed_values_and_null(conn, decision):
    _posting(conn, "claude", decision)


@pytest.mark.parametrize("decision", ["apply", "skip", "Maybe", ""])
def test_postings_decision_rejects_others(conn, decision):
    with pytest.raises(REJECTED):
        _posting(conn, "claude", decision)


def test_postings_canonical_url_is_not_null_and_unique(conn):
    with pytest.raises(REJECTED):
        conn.execute(
            "INSERT INTO postings (company, title, url, canonical_url, search_agent) "
            "VALUES ('A', 'B', 'u', NULL, 'claude')"
        )
    url = f"https://example.com/{uuid.uuid4().hex}"
    conn.execute(POSTING_SQL, (url, url, "claude", None))
    with pytest.raises(REJECTED):
        conn.execute(POSTING_SQL, (url, url, "gemini", None))


@pytest.mark.parametrize("agent", ["claude", "perplexity", "gemini"])
def test_findings_agent_accepts_listed_values(conn, agent):
    _finding(conn, agent=agent)


@pytest.mark.parametrize("agent", ["manual", "bogus", ""])
def test_findings_agent_rejects_others(conn, agent):
    with pytest.raises(REJECTED):
        _finding(conn, agent=agent)


@pytest.mark.parametrize("verification", VERIFICATIONS)
def test_findings_verification_accepts_listed_values(conn, verification):
    _finding(conn, verification=verification)


@pytest.mark.parametrize("verification", ["Verified", "ok", "", "dead"])
def test_findings_verification_rejects_others(conn, verification):
    with pytest.raises(REJECTED):
        _finding(conn, verification=verification)


def test_findings_verification_is_not_null(conn):
    with pytest.raises(REJECTED):
        _finding(conn, verification=None)


@pytest.mark.parametrize("kind", ["updated", "created", "published", None])
def test_findings_ats_date_kind_accepts_listed_values_and_null(conn, kind):
    _finding(conn, kind=kind)


@pytest.mark.parametrize("kind", ["modified", "Updated", ""])
def test_findings_ats_date_kind_rejects_others(conn, kind):
    with pytest.raises(REJECTED):
        _finding(conn, kind=kind)


def test_findings_primary_key_is_run_date_agent_canonical_url(conn):
    run_date, url = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(FINDING_SQL, (run_date, "claude", url, "verified", None))
    conn.execute(FINDING_SQL, (run_date, "gemini", url, "verified", None))
    conn.execute(FINDING_SQL, (uuid.uuid4().hex, "claude", url, "verified", None))
    with pytest.raises(REJECTED):
        conn.execute(FINDING_SQL, (run_date, "claude", url, "verified", None))


@pytest.mark.parametrize("trigger", ["scheduled", "hand", "smoke"])
def test_runs_trigger_accepts_listed_values(conn, trigger):
    _run(conn, trigger=trigger)


@pytest.mark.parametrize("trigger", ["cron", "manual", ""])
def test_runs_trigger_rejects_others(conn, trigger):
    with pytest.raises(REJECTED):
        _run(conn, trigger=trigger)


@pytest.mark.parametrize("outcome", ["ok", "failed", None])
def test_runs_outcome_accepts_listed_values_and_null(conn, outcome):
    _run(conn, outcome=outcome)


@pytest.mark.parametrize("outcome", ["success", "OK", ""])
def test_runs_outcome_rejects_others(conn, outcome):
    with pytest.raises(REJECTED):
        _run(conn, outcome=outcome)


def test_cron_runs_run_date_is_primary_key(conn):
    run_date = uuid.uuid4().hex
    conn.execute("INSERT INTO cron_runs (run_date) VALUES (?)", (run_date,))
    with pytest.raises(REJECTED):
        conn.execute("INSERT INTO cron_runs (run_date) VALUES (?)", (run_date,))


# --- timestamps --------------------------------------------------------------

ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|\+00:00)$")


def _shape(value: str) -> str:
    return re.sub(r"\d", "9", value)


def test_every_default_timestamp_is_utc_iso_8601_in_one_format(conn):
    url = f"https://example.com/{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO postings (company, title, url, canonical_url, search_agent) VALUES ('A', 'B', ?, ?, 'claude')",
        (url, url),
    )
    first_seen = conn.execute(
        "SELECT first_seen_at FROM postings WHERE canonical_url = ?", (url,)
    ).fetchone()[0]

    run_date = uuid.uuid4().hex
    conn.execute("INSERT INTO cron_runs (run_date) VALUES (?)", (run_date,))
    claimed = conn.execute(
        "SELECT claimed_at FROM cron_runs WHERE run_date = ?", (run_date,)
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO prompt_refinement_runs (cutoff, considered, changed) VALUES ('2026-01-01T00:00:00.000Z', 0, 0)"
    )
    run_at = conn.execute(
        "SELECT run_at FROM prompt_refinement_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]

    for value in (first_seen, claimed, run_at):
        assert ISO_UTC.match(value), value
    assert len({_shape(v) for v in (first_seen, claimed, run_at)}) == 1


def test_timestamps_sort_as_text(conn):
    import time

    urls = [f"https://example.com/{uuid.uuid4().hex}" for _ in range(3)]
    ids = []
    for url in urls:
        ids.append(
            db.insert_posting(
                conn, company="A", title="B", url=url, search_agent="claude"
            )
        )
        time.sleep(0.02)
    rows = conn.execute(
        f"SELECT id FROM postings WHERE id IN ({','.join('?' * 3)}) ORDER BY first_seen_at",
        ids,
    ).fetchall()
    assert [r[0] for r in rows] == ids


# --- delete by primary key on HTTP ------------------------------------------


def _count(conn, sql, params=()):
    return conn.execute(sql, params).fetchone()[0]


def test_postings_row_deletable_by_primary_key(http_conn):
    url = f"https://example.com/{uuid.uuid4().hex}"
    posting_id = db.insert_posting(
        http_conn, company="A", title="B", url=url, search_agent="claude"
    )
    http_conn.execute("DELETE FROM postings WHERE id = ?", (posting_id,))
    assert (
        _count(http_conn, "SELECT COUNT(*) FROM postings WHERE id = ?", (posting_id,))
        == 0
    )


def test_search_findings_row_deletable_by_primary_key(http_conn):
    run_date, url = uuid.uuid4().hex, uuid.uuid4().hex
    http_conn.execute(FINDING_SQL, (run_date, "claude", url, "verified", None))
    http_conn.execute(
        "DELETE FROM search_findings WHERE run_date = ? AND agent = ? AND canonical_url = ?",
        (run_date, "claude", url),
    )
    assert (
        _count(
            http_conn,
            "SELECT COUNT(*) FROM search_findings WHERE run_date = ?",
            (run_date,),
        )
        == 0
    )


def test_search_runs_row_deletable_by_primary_key(http_conn):
    _run(http_conn)
    run_id = http_conn.execute(
        "SELECT id FROM search_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    http_conn.execute("DELETE FROM search_runs WHERE id = ?", (run_id,))
    assert (
        _count(http_conn, "SELECT COUNT(*) FROM search_runs WHERE id = ?", (run_id,))
        == 0
    )


def test_cron_runs_row_deletable_by_primary_key(http_conn):
    run_date = uuid.uuid4().hex
    http_conn.execute("INSERT INTO cron_runs (run_date) VALUES (?)", (run_date,))
    http_conn.execute("DELETE FROM cron_runs WHERE run_date = ?", (run_date,))
    assert (
        _count(
            http_conn, "SELECT COUNT(*) FROM cron_runs WHERE run_date = ?", (run_date,)
        )
        == 0
    )


def test_prompt_refinement_runs_row_deletable_by_primary_key(http_conn):
    http_conn.execute(
        "INSERT INTO prompt_refinement_runs (cutoff, considered, changed) VALUES ('2026-01-01T00:00:00.000Z', 1, 1)"
    )
    run_id = http_conn.execute(
        "SELECT id FROM prompt_refinement_runs ORDER BY id DESC LIMIT 1"
    ).fetchone()[0]
    http_conn.execute("DELETE FROM prompt_refinement_runs WHERE id = ?", (run_id,))
    assert (
        _count(
            http_conn,
            "SELECT COUNT(*) FROM prompt_refinement_runs WHERE id = ?",
            (run_id,),
        )
        == 0
    )


def test_search_runs_and_refinement_ids_autoincrement(conn):
    _run(conn)
    first = conn.execute("SELECT MAX(id) FROM search_runs").fetchone()[0]
    _run(conn)
    second = conn.execute("SELECT MAX(id) FROM search_runs").fetchone()[0]
    assert second > first
    conn.execute("DELETE FROM search_runs WHERE id = ?", (second,))
    _run(conn)
    third = conn.execute("SELECT MAX(id) FROM search_runs").fetchone()[0]
    assert third > second  # AUTOINCREMENT never reuses a deleted id


def test_no_foreign_key_from_findings_to_postings(conn):
    run_date, url = uuid.uuid4().hex, uuid.uuid4().hex
    conn.execute(FINDING_SQL, (run_date, "claude", url, "verified", None))
    assert (
        _count(
            conn, "SELECT COUNT(*) FROM search_findings WHERE run_date = ?", (run_date,)
        )
        == 1
    )


def test_postings_has_no_application_state_columns(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(postings)").fetchall()}
    assert {"status", "date_applied"}.isdisjoint({c.lower() for c in columns})
    expected = {
        "id",
        "company",
        "title",
        "url",
        "date_posted",
        "canonical_url",
        "normalized_company",
        "title_slug",
        "jd_markdown",
        "location",
        "search_agent",
        "first_seen_at",
        "decision",
        "fit_feedback",
        "decided_at",
        "added_to_tracker",
        "closed_at",
    }
    assert expected <= columns
