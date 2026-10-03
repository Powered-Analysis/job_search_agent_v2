from datetime import UTC, datetime, timedelta

import pytest
from conftest import REJECTED, unique_url

from jsa import db


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
