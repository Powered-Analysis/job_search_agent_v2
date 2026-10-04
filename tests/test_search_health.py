"""Review's search health line (issue #10; PRD 03 "Search health line"; PRD 02 "Search-run log").

Runs and claims are written straight into the tables, so the clock is fixed: `NOW` is a Wednesday,
11:00 in New York, and the schedule runs Monday to Friday.
"""

import json
import sys
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from conftest import drop_all_tables
from profile_helpers import copy_example, schedule_toml, write_search_toml
from test_review import EMPTY_MESSAGE, Script, name, seed

from jsa import agent_loop, cli, db
from jsa.health import search_health

NOW = datetime(2026, 10, 14, 15, 0, tzinfo=UTC)
TODAY = "2026-10-14"
WEEKDAY_SEARCHES = [("perplexity", 24), ("claude", 24)]
SCHEDULE = schedule_toml(
    "America/New_York",
    "07:00",
    {
        day: WEEKDAY_SEARCHES
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday")
    },
)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = copy_example(tmp_path / "profile")
    write_search_toml(path, SCHEDULE)
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    monkeypatch.chdir(tmp_path)
    return path


@pytest.fixture
def health_db(db_url, profile):
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


def ago(**delta):
    return NOW - timedelta(**delta)


def add_run(
    conn,
    *,
    agent="claude",
    started=None,
    trigger="scheduled",
    outcome="ok",
    error=None,
    summary=None,
    warnings=None,
):
    started = started or ago(hours=4)
    if outcome == "ok" and summary is None:
        summary = {"inserted": 3, "cost": 0.5}
    conn.execute(
        "INSERT INTO search_runs (run_date, trigger, agent, window_hours, mode, started_at, "
        "finished_at, outcome, error, summary, warnings) VALUES (?, ?, ?, 24, 'strict', ?, ?, ?, ?, ?, ?)",
        (
            started.astimezone(UTC).date().isoformat(),
            trigger,
            agent,
            db.format_timestamp(started),
            db.format_timestamp(started + timedelta(minutes=20)) if outcome else None,
            outcome,
            error,
            json.dumps(summary) if summary is not None else None,
            warnings,
        ),
    )


def claim_days(conn, *days_back):
    for back in days_back:
        day = (NOW - timedelta(days=back)).date().isoformat()
        db.claim_cron_day(conn, day)


def health(conn):
    return search_health(conn, NOW)


def text(lines):
    return "\n".join(lines)


def healthy(conn):
    """Both scheduled agents ran fine today and every earlier day was claimed."""
    add_run(conn, agent="perplexity", started=ago(hours=4))
    add_run(conn, agent="claude", started=ago(hours=3))
    claim_days(conn, 1, 2, 3, 4, 5, 6, 7)


# --- the healthy line ------------------------------------------------------------------


def test_with_nothing_to_flag_it_is_one_line_naming_each_scheduled_agents_latest_run(
    health_db,
):
    add_run(
        health_db,
        agent="perplexity",
        started=ago(hours=4),
        summary={"inserted": 12, "cost": 1.75},
    )
    add_run(
        health_db,
        agent="claude",
        started=ago(hours=3),
        summary={"inserted": 7, "cost": 0.5},
    )
    claim_days(health_db, 1, 2, 3, 4, 5, 6, 7)
    (line,) = health(health_db)
    assert "perplexity" in line
    assert "claude" in line
    assert TODAY in line
    assert "ok" in line
    assert "12" in line
    assert "1.75" in line
    assert "7" in line
    assert "0.5" in line


def test_it_is_each_agents_latest_search_not_an_earlier_one(health_db):
    add_run(
        health_db,
        agent="claude",
        started=ago(days=3),
        summary={"inserted": 41, "cost": 9.99},
    )
    add_run(
        health_db,
        agent="claude",
        started=ago(hours=3),
        summary={"inserted": 58, "cost": 0.5},
    )
    add_run(health_db, agent="perplexity", started=ago(hours=4))
    claim_days(health_db, 1, 2, 3, 4, 5, 6, 7)
    (line,) = health(health_db)
    assert "58" in line
    assert "41" not in line
    assert "9.99" not in line


def test_a_scheduled_agent_with_no_search_yet_is_still_named(health_db):
    add_run(health_db, agent="claude", started=ago(hours=3))
    claim_days(health_db, 1, 2, 3, 4, 5, 6, 7)
    assert "perplexity" in text(health(health_db))


def test_an_agent_the_schedule_does_not_use_is_not_named(health_db):
    healthy(health_db)
    add_run(health_db, agent="gemini", started=ago(hours=2))
    assert "gemini" not in text(health(health_db))


def test_the_latest_search_counts_whatever_started_it(health_db):
    healthy(health_db)
    add_run(
        health_db,
        agent="claude",
        started=ago(hours=1),
        trigger="hand",
        summary={"inserted": 71, "cost": 0.5},
    )
    assert "71" in text(health(health_db))
    add_run(
        health_db,
        agent="claude",
        started=ago(minutes=10),
        trigger="smoke",
        summary={"inserted": 83, "cost": 0.5},
    )
    assert "83" in text(health(health_db))


# --- failures ---------------------------------------------------------------------------


def test_a_search_that_failed_in_the_last_seven_days_is_named_with_its_error(
    health_db,
):
    healthy(health_db)
    add_run(
        health_db,
        agent="perplexity",
        started=ago(days=2),
        outcome="failed",
        error="Perplexity returned 503",
    )
    lines = health(health_db)
    assert any("Perplexity returned 503" in line for line in lines)
    assert len(lines) > 1


def test_a_failed_hand_or_smoke_search_is_reported_like_a_scheduled_one(health_db):
    healthy(health_db)
    add_run(
        health_db,
        started=ago(days=1),
        trigger="hand",
        outcome="failed",
        error="hand run broke",
    )
    add_run(
        health_db,
        started=ago(days=2),
        trigger="smoke",
        outcome="failed",
        error="smoke run broke",
    )
    output = text(health(health_db))
    assert "hand run broke" in output
    assert "smoke run broke" in output


def test_a_failure_older_than_seven_days_is_not_reported(health_db):
    healthy(health_db)
    add_run(
        health_db,
        started=ago(days=12),
        outcome="failed",
        error="long ago failure",
    )
    assert "long ago failure" not in text(health(health_db))


def test_a_failed_latest_search_is_what_the_agent_line_shows(health_db):
    add_run(health_db, agent="perplexity", started=ago(hours=4))
    add_run(
        health_db,
        agent="claude",
        started=ago(hours=3),
        outcome="failed",
        error="the model refused",
    )
    claim_days(health_db, 1, 2, 3, 4, 5, 6, 7)
    assert "the model refused" in text(health(health_db))


# --- warnings ---------------------------------------------------------------------------


def test_every_warning_a_run_logged_is_reported(health_db):
    healthy(health_db)
    add_run(
        health_db,
        agent="claude",
        started=ago(hours=1),
        warnings="malformed: 2 postings broke the contract\nreachable_no_date: 5 pages",
    )
    output = text(health(health_db))
    assert "malformed: 2 postings broke the contract" in output
    assert "reachable_no_date: 5 pages" in output


def test_warnings_of_every_search_in_the_last_seven_days_are_reported_not_just_the_latest(
    health_db,
):
    healthy(health_db)
    add_run(
        health_db,
        agent="claude",
        started=ago(days=5),
        trigger="hand",
        warnings="verified_no_date: Lever returned no date",
    )
    assert "verified_no_date: Lever returned no date" in text(health(health_db))


def test_warnings_of_a_search_older_than_seven_days_are_not_reported(health_db):
    healthy(health_db)
    add_run(
        health_db,
        agent="claude",
        started=ago(days=11),
        warnings="malformed: stale warning",
    )
    assert "stale warning" not in text(health(health_db))


# --- dead searches ----------------------------------------------------------------------


def test_a_search_with_no_outcome_after_ninety_minutes_is_reported_dead(health_db):
    healthy(health_db)
    add_run(health_db, agent="perplexity", started=ago(minutes=91), outcome=None)
    lines = health(health_db)
    assert any("dead" in line.lower() for line in lines)


def test_a_search_still_inside_ninety_minutes_is_not_dead(health_db):
    healthy(health_db)
    add_run(health_db, agent="perplexity", started=ago(minutes=89), outcome=None)
    assert "dead" not in text(health(health_db)).lower()


def test_a_dead_search_is_reported_however_long_ago_it_started(health_db):
    healthy(health_db)
    add_run(
        health_db,
        agent="claude",
        started=ago(days=40),
        trigger="hand",
        outcome=None,
    )
    assert "dead" in text(health(health_db)).lower()


def test_a_search_that_finished_is_never_dead(health_db):
    healthy(health_db)
    add_run(health_db, started=ago(days=3), outcome="ok")
    assert "dead" not in text(health(health_db)).lower()


# --- missed days ------------------------------------------------------------------------


def test_a_scheduled_day_in_the_last_week_with_no_claim_is_reported_missed(health_db):
    add_run(health_db, agent="perplexity", started=ago(hours=4))
    add_run(health_db, agent="claude", started=ago(hours=3))
    claim_days(health_db, 1, 3, 4, 5, 6, 7)  # Monday 2026-10-12 is left out
    lines = health(health_db)
    missed = [line for line in lines if "2026-10-12" in line]
    assert missed
    assert "missed" in text(missed).lower()


def test_unscheduled_days_with_no_claim_are_not_missed(health_db):
    healthy(health_db)
    # Saturday 10 and Sunday 11 have no schedule and no claim.
    health_db.execute(
        "DELETE FROM cron_runs WHERE run_date IN (?, ?)", ("2026-10-10", "2026-10-11")
    )
    output = text(health(health_db))
    assert "2026-10-10" not in output
    assert "2026-10-11" not in output
    assert "missed" not in output.lower()


def test_today_is_never_missed_even_without_a_claim(health_db):
    healthy(health_db)
    assert "missed" not in text(health(health_db)).lower()


def test_a_scheduled_day_older_than_a_week_is_not_reported_missed(health_db):
    healthy(health_db)
    assert "2026-10-02" not in text(health(health_db))
    assert "2026-10-01" not in text(health(health_db))


def test_a_day_that_was_claimed_but_whose_search_failed_is_not_missed(health_db):
    healthy(health_db)
    add_run(
        health_db,
        started=ago(days=1),
        outcome="failed",
        error="boom",
    )
    assert "missed" not in text(health(health_db)).lower()


def test_with_no_runs_and_no_claims_every_scheduled_day_is_missed(health_db):
    output = text(health(health_db))
    for day in ("2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12", "2026-10-13"):
        assert day in output


# --- search.toml missing or invalid -------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    ["missing", "unknown key", "not toml"],
    ids=["missing", "invalid", "garbage"],
)
def test_a_missing_or_invalid_search_toml_is_reported_in_the_line(
    health_db, profile, state
):
    target = profile / "search" / "search.toml"
    if state == "missing":
        target.unlink()
    elif state == "unknown key":
        target.write_text(SCHEDULE + "\nsurprise = 1\n")
    else:
        target.write_text("this is [not toml")
    lines = health(health_db)
    assert lines
    assert "search.toml" in text(lines)


def test_review_continues_to_the_backlog_when_search_toml_is_missing(
    health_db, profile, monkeypatch, capsys, web
):
    (profile / "search" / "search.toml").unlink()
    code, output = jsa_review(monkeypatch, capsys, [])
    assert code == 0
    assert "search.toml" in output
    assert "Traceback" not in output
    assert EMPTY_MESSAGE in output


# --- review prints it first -------------------------------------------------------------------


@pytest.fixture
def web(monkeypatch):
    def live(_transport, request):
        return httpx.Response(
            200, html="<html><body>Open</body></html>", request=request
        )

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", live)


def jsa_review(monkeypatch, capsys, lines):
    monkeypatch.setattr(sys, "argv", ["jsa", "review"])
    monkeypatch.setattr(sys, "stdin", Script(lines))
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def test_review_prints_the_health_line_before_the_backlog(
    health_db, monkeypatch, capsys, web
):
    company = name()
    seed(health_db, company, order=1)
    add_run(health_db, agent="claude", started=datetime.now(UTC) - timedelta(hours=1))
    code, output = jsa_review(monkeypatch, capsys, ["q"])
    assert code == 0
    first = output.splitlines()[0]
    assert "claude" in first
    assert output.index(first) < output.index(company)


def test_review_prints_the_health_line_even_when_there_is_no_backlog(
    health_db, monkeypatch, capsys, web
):
    add_run(health_db, agent="claude", started=datetime.now(UTC) - timedelta(hours=1))
    code, output = jsa_review(monkeypatch, capsys, [])
    assert code == 0
    assert output.index("claude") < output.index(EMPTY_MESSAGE)


def test_review_reports_a_failed_search_it_finds(health_db, monkeypatch, capsys, web):
    add_run(
        health_db,
        agent="claude",
        started=datetime.now(UTC) - timedelta(hours=2),
        outcome="failed",
        error="the model refused to search",
    )
    _code, output = jsa_review(monkeypatch, capsys, [])
    assert "the model refused to search" in output


# --- no model call ----------------------------------------------------------------------------


def test_the_health_line_makes_no_model_call_and_no_request(health_db, monkeypatch):
    healthy(health_db)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the health line reached the outside world")

    monkeypatch.setattr(agent_loop, "query", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    assert health(health_db)


def test_the_health_line_is_read_only(health_db):
    healthy(health_db)
    before = [
        health_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("search_runs", "cron_runs", "postings", "search_findings")
    ]
    health(health_db)
    after = [
        health_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("search_runs", "cron_runs", "postings", "search_findings")
    ]
    assert before == after
