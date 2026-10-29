"""PRD 02 "search_runs", "search_findings" and "Schema creation and change" (issue #199): the
`search_family` column on both tables, and the rebuild that gives `search_findings` its new
primary key. Tests of the database module run on both transports."""

import pytest
from conftest import REJECTED, TABLES, raw_connect

from jsa import db

LEGACY_FINDINGS = """
CREATE TABLE search_findings (
    run_date TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('claude', 'perplexity', 'gemini')),
    canonical_url TEXT NOT NULL,
    window_hours INTEGER,
    rank INTEGER,
    found_at TEXT,
    decision TEXT CHECK (decision IN ('Apply', 'Skip')),
    verification TEXT NOT NULL CHECK (verification IN ('verified', 'verified_no_date',
        'reachable', 'reachable_no_date', 'aggregator', 'unsupported', 'unverifiable',
        'not_on_index', 'page_closed', 'out_of_window')),
    ats_date TEXT,
    ats_date_kind TEXT CHECK (ats_date_kind IN ('updated', 'created', 'published')),
    model TEXT,
    effort TEXT,
    PRIMARY KEY (run_date, agent, canonical_url)
)
"""
LEGACY_RUNS = """
CREATE TABLE search_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date TEXT,
    trigger TEXT CHECK (trigger IN ('scheduled', 'hand', 'smoke')),
    agent TEXT,
    window_hours INTEGER,
    mode TEXT,
    model TEXT,
    effort TEXT,
    started_at TEXT,
    finished_at TEXT,
    outcome TEXT CHECK (outcome IN ('ok', 'failed')),
    error TEXT,
    summary TEXT,
    warnings TEXT
)
"""
FINDING_COLUMNS = (
    "run_date, agent, canonical_url, window_hours, rank, found_at, decision, "
    "verification, ats_date, ats_date_kind, model, effort"
)
RUN_COLUMNS = (
    "id, run_date, trigger, agent, window_hours, mode, model, effort, started_at, "
    "finished_at, outcome, error, summary, warnings"
)
FINDINGS = [
    (
        "2026-10-01",
        "claude",
        "https://x.example/a",
        24,
        1,
        "2026-10-01T11:00:00.000Z",
        "Apply",
        "verified",
        "2026-09-30T00:00:00Z",
        "updated",
        "m1",
        "high",
    ),
    (
        "2026-10-01",
        "gemini",
        "https://x.example/a",
        48,
        3,
        "2026-10-01T11:05:00.000Z",
        None,
        "reachable_no_date",
        None,
        None,
        "m2",
        None,
    ),
    (
        "2026-10-02",
        "claude",
        "https://x.example/b",
        24,
        2,
        "2026-10-02T11:00:00.000Z",
        "Skip",
        "page_closed",
        None,
        None,
        None,
        None,
    ),
]
RUNS = [
    (
        1,
        "2026-10-01",
        "scheduled",
        "claude",
        24,
        "strict",
        "m1",
        "high",
        "2026-10-01T11:00:00.000Z",
        "2026-10-01T11:09:00.000Z",
        "ok",
        None,
        '{"found": 3}',
        None,
    ),
    (
        2,
        "2026-10-02",
        "hand",
        "gemini",
        48,
        "best_effort",
        "m2",
        None,
        "2026-10-02T11:00:00.000Z",
        None,
        None,
        None,
        None,
        None,
    ),
]


@pytest.fixture
def db_url(both_transports_url):
    return both_transports_url


def drop_everything(url):
    """Drop every table, including any a half-done rebuild parked under another name."""
    conn = raw_connect(url)
    try:
        names = [
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for name in names:
            conn.execute(f"DROP TABLE IF EXISTS {name}")
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def clean(db_url):
    drop_everything(db_url)
    yield
    drop_everything(db_url)


def legacy_database(url):
    """A database as the version before search families left it, holding findings and runs."""
    db.connect().close()
    drop_everything(url)
    conn = raw_connect(url)
    try:
        conn.execute(LEGACY_FINDINGS)
        conn.execute(LEGACY_RUNS)
        conn.executemany(
            f"INSERT INTO search_findings ({FINDING_COLUMNS}) VALUES ({', '.join('?' * 12)})",
            FINDINGS,
        )
        conn.executemany(
            f"INSERT INTO search_runs ({RUN_COLUMNS}) VALUES ({', '.join('?' * 14)})",
            RUNS,
        )
    finally:
        conn.close()


def tables(url):
    conn = raw_connect(url)
    try:
        return {
            name
            for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
    finally:
        conn.close()


def query(url, sql):
    conn = raw_connect(url)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def findings(url):
    return query(
        url,
        f"SELECT {FINDING_COLUMNS}, search_family FROM search_findings "
        "ORDER BY run_date, agent, canonical_url",
    )


def runs(url):
    return query(
        url, f"SELECT {RUN_COLUMNS}, search_family FROM search_runs ORDER BY id"
    )


def snapshot(url):
    return query(url, "SELECT name, sql FROM sqlite_master ORDER BY name")


def shape(url, table):
    return {
        row[1:] for row in query(url, f"SELECT * FROM pragma_table_info('{table}')")
    }


def new_finding(conn, run_date, family, agent="claude", url="https://x.example/z"):
    conn.execute(
        "INSERT INTO search_findings (run_date, search_family, agent, canonical_url, verification) "
        "VALUES (?, ?, ?, ?, 'verified')",
        (run_date, family, agent, url),
    )


# --- a database made before search families ------------------------------------------


def test_existing_runs_gain_a_null_search_family_and_keep_every_other_value(db_url):
    legacy_database(db_url)
    db.connect().close()
    assert runs(db_url) == [(*run, None) for run in RUNS]


def test_existing_findings_are_rebuilt_with_every_row_intact_and_a_null_family(db_url):
    legacy_database(db_url)
    db.connect().close()
    assert findings(db_url) == [(*row, None) for row in FINDINGS]


def test_the_rebuilt_findings_table_has_the_new_primary_key(db_url):
    legacy_database(db_url)
    conn = db.connect()
    new_finding(conn, "2026-10-01", "a")
    new_finding(conn, "2026-10-01", "b")
    new_finding(conn, "2026-10-01", None)
    new_finding(conn, "2026-10-01", None)  # NULLs are distinct as SQL treats them
    with pytest.raises(REJECTED):
        new_finding(conn, "2026-10-01", "a")
    conn.close()


def test_a_rebuild_leaves_no_table_parked_under_another_name(db_url):
    legacy_database(db_url)
    db.connect().close()
    assert tables(db_url) - {"sqlite_sequence"} == set(TABLES)


def test_running_it_again_changes_nothing(db_url):
    legacy_database(db_url)
    db.connect().close()
    before = (snapshot(db_url), findings(db_url), runs(db_url))
    for _ in range(2):
        db.connect().close()
    assert (snapshot(db_url), findings(db_url), runs(db_url)) == before


def test_a_migrated_database_has_the_shape_of_a_fresh_one(db_url):
    db.connect().close()
    fresh = {t: shape(db_url, t) for t in ("search_findings", "search_runs")}
    drop_everything(db_url)
    legacy_database(db_url)
    db.connect().close()
    assert {t: shape(db_url, t) for t in fresh} == fresh


def test_a_fresh_database_is_created_with_the_family_columns_and_no_rebuild(db_url):
    db.connect().close()
    for table in ("search_findings", "search_runs"):
        names = [
            row[1]
            for row in query(db_url, f"SELECT * FROM pragma_table_info('{table}')")
        ]
        assert "search_family" in names, table
    assert tables(db_url) - {"sqlite_sequence"} == set(TABLES)


def test_new_rows_after_a_migration_carry_their_family(db_url):
    legacy_database(db_url)
    conn = db.connect()
    db.record_finding(
        conn,
        run_date="2026-10-03",
        search_family="charter",
        agent="claude",
        canonical_url="https://x.example/c",
        window_hours=24,
        rank=1,
        verification="verified",
        ats_date=None,
        ats_date_kind=None,
        model=None,
        effort=None,
    )
    run_id = db.open_search_run(
        conn,
        trigger="hand",
        run_date="2026-10-03",
        search_family="charter",
        agent="claude",
        window_hours=24,
        mode="strict",
    )
    conn.close()
    assert findings(db_url)[-1][-1] == "charter"
    assert runs(db_url)[-1][0] == run_id
    assert runs(db_url)[-1][-1] == "charter"
    assert len(findings(db_url)) == len(FINDINGS) + 1


# --- a rebuild interrupted at any statement ---------------------------------------------


class DiesAtStatement:
    """A connection whose `n`th statement fails, as when the process or the network dies."""

    def __init__(self, conn, n):
        self._conn = conn
        self._left = n

    def execute(self, *args, **kwargs):
        self._left -= 1
        if self._left == 0:
            raise ConnectionError("the connection was lost")
        return self._conn.execute(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


@pytest.mark.parametrize("n", range(1, 21))
def test_a_rebuild_interrupted_at_any_statement_is_finished_on_the_next_open(
    db_url, monkeypatch, n
):
    legacy_database(db_url)
    ensure_schema = db.ensure_schema
    with monkeypatch.context() as interrupting:
        interrupting.setattr(
            db, "ensure_schema", lambda conn: ensure_schema(DiesAtStatement(conn, n))
        )
        try:
            db.connect().close()
        except ConnectionError:
            pass
    db.connect().close()
    assert findings(db_url) == [(*row, None) for row in FINDINGS]
    assert runs(db_url) == [(*run, None) for run in RUNS]
    assert tables(db_url) - {"sqlite_sequence"} == set(TABLES)
    conn = db.connect()
    new_finding(conn, "2026-10-01", "a")
    new_finding(conn, "2026-10-01", "b")
    with pytest.raises(REJECTED):
        new_finding(conn, "2026-10-01", "a")
    conn.close()


@pytest.mark.parametrize("n", range(1, 21))
def test_an_interrupted_rebuild_applied_twice_more_changes_nothing(
    db_url, monkeypatch, n
):
    legacy_database(db_url)
    ensure_schema = db.ensure_schema
    with monkeypatch.context() as interrupting:
        interrupting.setattr(
            db, "ensure_schema", lambda conn: ensure_schema(DiesAtStatement(conn, n))
        )
        try:
            db.connect().close()
        except ConnectionError:
            pass
    db.connect().close()
    settled = (snapshot(db_url), findings(db_url), runs(db_url))
    db.connect().close()
    assert (snapshot(db_url), findings(db_url), runs(db_url)) == settled


def test_the_decision_sync_reaches_the_findings_of_every_search_family(db_url):
    conn = db.connect()
    url = "https://x.example/shared-find"
    posting_id = db.insert_posting(
        conn, company="Acme", title="Engineer", url=url, search_agent="claude"
    )
    assert posting_id
    for family in ("a", "b"):
        new_finding(conn, "2026-10-01", family, url=url)
    db.set_decision(conn, url, "Apply")
    assert conn.execute("SELECT decision FROM search_findings").fetchall() == [
        ("Apply",),
        ("Apply",),
    ]
    conn.close()
