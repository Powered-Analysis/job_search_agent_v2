from datetime import UTC, datetime, timedelta

import pytest
from conftest import REJECTED, drop_all_tables, raw_connect, unique_url

from jsa import db


@pytest.fixture
def db_url(both_transports_url):
    return both_transports_url


def _row(conn, posting_id, columns):
    return conn.execute(
        f"SELECT {columns} FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()


def _insert(conn, **overrides):
    args = {
        "company": "Acme Widgets, Inc.",
        "title": "Senior Engineer",
        "url": unique_url(),
        "search_agent": "claude",
    }
    args.update(overrides)
    return db.insert_posting(conn, **args), args


def test_insert_returns_integer_id(conn):
    posting_id, _ = _insert(conn)
    assert isinstance(posting_id, int)


def test_same_canonical_url_returns_none_and_keeps_one_row(conn):
    posting_id, args = _insert(conn)
    again = db.insert_posting(
        conn, company="Other", title="Other", url=args["url"], search_agent="perplexity"
    )
    assert again is None
    count = conn.execute(
        "SELECT COUNT(*) FROM postings WHERE canonical_url = ?", (args["url"],)
    ).fetchone()[0]
    assert count == 1
    # the first write stands
    assert _row(conn, posting_id, "company, search_agent") == (
        "Acme Widgets, Inc.",
        "claude",
    )


def test_different_urls_get_different_ids(conn):
    first, _ = _insert(conn)
    second, _ = _insert(conn)
    assert first != second


def test_url_variants_converge_on_one_row(conn):
    base = unique_url("https://boards.greenhouse.io/acme/jobs/")
    first = db.insert_posting(
        conn, company="A", title="T", url=base, search_agent="claude"
    )
    assert isinstance(first, int)
    variants = [
        base + "?utm_source=x",
        base + "/",
        base + "#apply",
        base.replace("boards.greenhouse.io", "job-boards.greenhouse.io"),
        base.replace("https://", "HTTPS://").replace(
            "boards.greenhouse.io", "Boards.Greenhouse.io"
        ),
    ]
    for variant in variants:
        assert (
            db.insert_posting(
                conn, company="A", title="T", url=variant, search_agent="gemini"
            )
            is None
        ), variant


def test_canonical_url_is_stored_canonicalized(conn):
    from jsa.urls import canonicalize_url

    raw = unique_url("https://Boards.Greenhouse.io/acme/jobs/") + "?utm_source=x#frag"
    posting_id, _ = _insert(conn, url=raw)
    stored_canonical, stored_url = _row(conn, posting_id, "canonical_url, url")
    assert stored_canonical == canonicalize_url(raw)
    assert "utm_source" not in stored_canonical and "#" not in stored_canonical
    assert stored_url == raw


def test_gh_jid_variants_are_distinct_rows(conn):
    base = f"https://acme.com/{unique_url('careers-')[-32:]}"
    first = db.insert_posting(
        conn, company="A", title="T", url=base + "?gh_jid=1", search_agent="claude"
    )
    second = db.insert_posting(
        conn, company="A", title="T", url=base + "?gh_jid=2", search_agent="claude"
    )
    assert isinstance(first, int) and isinstance(second, int) and first != second


def test_naming_fields_computed_at_insert(conn):
    posting_id, _ = _insert(
        conn, company="Acme Widgets, Inc.", title="Senior Engineer / Platform: Backend"
    )
    normalized_company, slug = _row(conn, posting_id, "normalized_company, title_slug")
    assert normalized_company == "Acme Widgets"
    assert slug
    assert not set(slug) & set('/\\:*?"<>|')
    assert len(slug) <= 80


def test_date_posted_stored(conn):
    posting_id, _ = _insert(conn, date_posted="2026-05-01")
    assert _row(conn, posting_id, "date_posted") == ("2026-05-01",)


@pytest.mark.parametrize("agent", ["claude", "perplexity", "gemini"])
def test_searched_insert_leaves_decision_and_decided_at_null(conn, agent):
    posting_id, _ = _insert(conn, search_agent=agent)
    assert _row(conn, posting_id, "search_agent, decision, decided_at") == (
        agent,
        None,
        None,
    )


def test_manual_insert_is_decided_apply_with_timestamp(conn):
    posting_id, _ = _insert(conn, search_agent="manual")
    search_agent, decision, decided_at = _row(
        conn, posting_id, "search_agent, decision, decided_at"
    )
    assert (search_agent, decision) == ("manual", "Apply")
    assert decided_at


@pytest.mark.parametrize("agent", ["claude", "perplexity", "gemini", "manual"])
def test_new_row_defaults(conn, agent):
    posting_id, _ = _insert(conn, search_agent=agent)
    added, closed, first_seen = _row(
        conn, posting_id, "added_to_tracker, closed_at, first_seen_at"
    )
    assert added == 0
    assert closed is None
    seen = datetime.fromisoformat(first_seen)
    assert seen.tzinfo is not None and seen.utcoffset() == timedelta(0)
    assert abs(datetime.now(UTC) - seen) < timedelta(minutes=5)


def test_manual_decided_at_and_first_seen_share_one_format(conn):
    import re

    posting_id, _ = _insert(conn, search_agent="manual")
    first_seen, decided_at = _row(conn, posting_id, "first_seen_at, decided_at")
    shape = lambda v: re.sub(r"\d", "9", v)
    assert shape(first_seen) == shape(decided_at)
    assert datetime.fromisoformat(decided_at).utcoffset() == timedelta(0)


def test_invalid_search_agent_rejected(conn):
    with pytest.raises(REJECTED):
        _insert(conn, search_agent="bogus")


# --- JD capture ---------------------------------------------------------------


def test_capture_jd_sets_jd_and_location_leaving_title(conn):
    posting_id, _ = _insert(conn, title="Original Title")
    before = _row(conn, posting_id, "title, title_slug")
    db.capture_jd(
        conn, posting_id, jd_markdown="# Role\n\nDo things.", location="Remote, US"
    )
    assert _row(conn, posting_id, "jd_markdown, location") == (
        "# Role\n\nDo things.",
        "Remote, US",
    )
    assert _row(conn, posting_id, "title, title_slug") == before


def test_capture_jd_with_title_replaces_title_and_slug(conn):
    posting_id, _ = _insert(conn, title="Agent Transcribed Title")
    _, old_slug = _row(conn, posting_id, "title, title_slug")
    db.capture_jd(
        conn,
        posting_id,
        jd_markdown="jd",
        location="NYC",
        title="Canonical ATS Title / Platform",
    )
    title, slug = _row(conn, posting_id, "title, title_slug")
    assert title == "Canonical ATS Title / Platform"
    assert slug != old_slug
    assert slug.lower().startswith("canonical")
    assert not set(slug) & set('/\\:*?"<>|')
    assert len(slug) <= 80


@pytest.mark.parametrize("empty_title", [None, ""])
def test_capture_jd_with_none_or_empty_title_leaves_title_and_slug(conn, empty_title):
    posting_id, _ = _insert(conn, title="Good Captured Title")
    before = _row(conn, posting_id, "title, title_slug")
    db.capture_jd(conn, posting_id, jd_markdown="jd", location=None, title=empty_title)
    assert _row(conn, posting_id, "title, title_slug") == before
    assert _row(conn, posting_id, "jd_markdown, location") == ("jd", None)


def test_capture_jd_only_touches_its_own_row(conn):
    first, _ = _insert(conn, title="One")
    second, _ = _insert(conn, title="Two")
    before = _row(conn, second, "title, title_slug, jd_markdown, location")
    db.capture_jd(conn, first, jd_markdown="jd", location="X", title="Changed")
    assert _row(conn, second, "title, title_slug, jd_markdown, location") == before


def test_capture_jd_does_not_change_decision_or_identity(conn):
    posting_id, _ = _insert(conn, search_agent="manual")
    before = _row(conn, posting_id, "canonical_url, decision, decided_at, search_agent")
    db.capture_jd(conn, posting_id, jd_markdown="jd", location="X", title="New")
    assert (
        _row(conn, posting_id, "canonical_url, decision, decided_at, search_agent")
        == before
    )


# --- role families (PRD 02 "The postings table", "Query surface") ------------------


def _family(conn, posting_id):
    return _row(conn, posting_id, "role_family")[0]


def _decided(conn, url, decision="Apply", role_family="Data Analysis"):
    db.record_decision(conn, url, decision, "note", role_family)


def test_a_new_row_has_no_role_family_unless_the_insert_gives_one(conn):
    searched, _ = _insert(conn)
    manual, _ = _insert(conn, search_agent="manual", role_family="Data Analysis")
    manual_without, _ = _insert(conn, search_agent="manual")
    assert _family(conn, searched) is None
    assert _family(conn, manual) == "Data Analysis"
    assert _family(conn, manual_without) is None


def test_a_manual_insert_stores_the_role_family_with_its_apply_decision(conn):
    posting_id, _ = _insert(conn, search_agent="manual", role_family="Data Analysis")
    assert _row(conn, posting_id, "decision, role_family") == ("Apply", "Data Analysis")


def test_record_decision_stores_the_role_family_of_an_apply(conn):
    posting_id, args = _insert(conn)
    db.record_decision(conn, args["url"], "Apply", "great fit", "Data Analysis")
    assert _row(conn, posting_id, "decision, fit_feedback, role_family") == (
        "Apply",
        "great fit",
        "Data Analysis",
    )


def test_record_decision_without_a_role_family_stores_an_apply_with_none(conn):
    posting_id, args = _insert(conn)
    db.record_decision(conn, args["url"], "Apply", None)
    assert _row(conn, posting_id, "decision, role_family") == ("Apply", None)


def test_record_decision_replaces_the_role_family_of_a_decided_apply(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"], role_family="Data Analysis")
    _decided(conn, args["url"], role_family="Analytics Engineering")
    assert _family(conn, posting_id) == "Analytics Engineering"
    _decided(conn, args["url"], role_family=None)
    assert _family(conn, posting_id) is None


def test_a_skip_never_stores_a_role_family(conn):
    posting_id, args = _insert(conn)
    db.record_decision(conn, args["url"], "Skip", "too junior", "Data Analysis")
    assert _row(conn, posting_id, "decision, role_family") == ("Skip", None)


def test_a_skip_clears_the_role_family_of_a_row_that_was_apply(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    db.record_decision(conn, args["url"], "Skip", "changed my mind")
    assert _row(conn, posting_id, "decision, role_family") == ("Skip", None)


def test_changing_a_decision_to_skip_clears_the_role_family(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    db.set_decision(conn, args["url"], "Skip")
    assert _row(conn, posting_id, "decision, fit_feedback, role_family") == (
        "Skip",
        "note",
        None,
    )


def test_clearing_a_decision_clears_the_role_family(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    db.clear_decision(conn, args["url"])
    assert _row(
        conn, posting_id, "decision, fit_feedback, decided_at, role_family"
    ) == (None, None, None, None)


def test_the_role_family_write_sets_it_and_leaves_decided_at_alone(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"], role_family="Data Analysis")
    conn.execute(
        "UPDATE postings SET decided_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (posting_id,),
    )
    db.set_role_family(conn, posting_id, "Analytics Engineering")
    assert _row(conn, posting_id, "role_family, decided_at, decision") == (
        "Analytics Engineering",
        "2020-01-01T00:00:00.000Z",
        "Apply",
    )


def test_the_role_family_write_can_set_none(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    db.set_role_family(conn, posting_id, None)
    assert _family(conn, posting_id) is None


def test_the_role_family_write_of_the_current_value_changes_nothing(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    conn.execute(
        "UPDATE postings SET decided_at = '2020-01-01T00:00:00.000Z' WHERE id = ?",
        (posting_id,),
    )
    before = _row(conn, posting_id, "role_family, decided_at, fit_feedback")
    db.set_role_family(conn, posting_id, "Data Analysis")
    assert _row(conn, posting_id, "role_family, decided_at, fit_feedback") == before


def test_the_role_family_write_touches_only_its_own_row(conn):
    first, first_args = _insert(conn)
    second, second_args = _insert(conn)
    _decided(conn, first_args["url"])
    _decided(conn, second_args["url"])
    db.set_role_family(conn, first, "Analytics Engineering")
    assert _family(conn, second) == "Data Analysis"


def test_a_decision_write_leaves_other_rows_role_families_alone(conn):
    _, first_args = _insert(conn)
    second, second_args = _insert(conn)
    _decided(conn, first_args["url"])
    _decided(conn, second_args["url"])
    db.record_decision(conn, first_args["url"], "Skip", None)
    db.clear_decision(conn, first_args["url"])
    assert _family(conn, second) == "Data Analysis"


def test_the_lookup_returns_the_role_family_with_id_decision_and_refinement(conn):
    posting_id, args = _insert(conn)
    assert db.find_posting(conn, args["url"]) == (posting_id, None, True, None)
    _decided(conn, args["url"])
    assert db.find_posting(conn, args["url"]) == (
        posting_id,
        "Apply",
        True,
        "Data Analysis",
    )


def test_the_revise_reads_return_the_role_family(conn):
    posting_id, args = _insert(conn)
    _decided(conn, args["url"])
    by_id = db.posting_by_id(conn, posting_id)
    latest = db.last_decided_posting(conn)
    assert by_id is not None and latest is not None
    assert by_id[-1] == "Data Analysis"
    assert latest[-1] == "Data Analysis"
    assert by_id[0] == latest[0] == posting_id


def _without_role_family(url):
    """Leave the database as a version without the column left it, holding three rows."""
    drop_all_tables(url)
    db.connect().close()
    conn = raw_connect(url)
    try:
        conn.execute("ALTER TABLE postings DROP COLUMN role_family")
        for n, decision in enumerate(("Apply", "Skip", None)):
            conn.execute(
                "INSERT INTO postings (company, title, url, canonical_url, search_agent, decision, decided_at) "
                "VALUES ('Acme', 'Engineer', ?, ?, 'claude', ?, '2020-01-01T00:00:00.000Z')",
                (f"https://x.example/{n}", f"https://x.example/{n}", decision),
            )
    finally:
        conn.close()


def _schema_and_rows(url):
    conn = raw_connect(url)
    try:
        return (
            conn.execute(
                "SELECT name, sql FROM sqlite_master ORDER BY name"
            ).fetchall(),
            conn.execute("SELECT * FROM postings ORDER BY id").fetchall(),
        )
    finally:
        conn.close()


def test_opening_a_database_made_before_the_column_adds_it_with_every_row_none(
    db_url,
):
    _without_role_family(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    rows = conn.execute("SELECT decision, role_family FROM postings ORDER BY id")
    assert rows.fetchall() == [("Apply", None), ("Skip", None), (None, None)]
    conn.close()


def test_the_role_family_migration_is_a_no_op_once_applied(db_url):
    _without_role_family(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    conn.execute("UPDATE postings SET role_family = 'Data Analysis' WHERE id = 1")
    conn.close()
    before = _schema_and_rows(db_url)
    for _ in range(2):
        db.connect().close()
    assert _schema_and_rows(db_url) == before


def test_a_migrated_table_gets_the_same_column_as_a_fresh_one(db_url):
    drop_all_tables(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    fresh = conn.execute(
        "SELECT name, type, \"notnull\", dflt_value FROM pragma_table_info('postings') ORDER BY name"
    ).fetchall()
    conn.close()
    _without_role_family(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    migrated = conn.execute(
        "SELECT name, type, \"notnull\", dflt_value FROM pragma_table_info('postings') ORDER BY name"
    ).fetchall()
    conn.close()
    assert migrated == fresh
    assert ("role_family", "TEXT", 0, None) in fresh
