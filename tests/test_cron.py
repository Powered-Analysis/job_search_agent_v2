"""`jsa cron`: the self-gating hourly entrypoint (issue #10; PRD 01 "Self-gating hourly cron"; PRD 02 "Cron claim table"; XC-9).

The Claude Agent SDK is replaced at its one entry point, `agent_loop.query`; the clock is the real one,
so every test that depends on the time of day picks a timezone whose local time it can rely on.
"""

import builtins
import socket
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import drop_all_tables, existing_tables
from profile_helpers import WEEKDAYS, copy_example, schedule_toml, write_search_toml
from search_helpers import rows
from test_claude_runner import result_message

from jsa import agent_loop, cli, db
from jsa.cron import gate
from jsa.profile import load_search_config

# Whole-hour offsets six hours apart: exactly one of them has local time in [03:00, 09:00),
# far from midnight and from any run_at the tests choose.
ZONES = ("Etc/GMT+12", "Etc/GMT+6", "Etc/GMT", "Etc/GMT-6")
DUE = "00:00"
NOT_YET = "23:59"


def quiet_zone() -> str:
    for zone in ZONES:
        if 3 <= datetime.now(ZoneInfo(zone)).hour < 9:
            return zone
    raise AssertionError("no zone is in its early morning")


def local_today(zone: str):
    return datetime.now(ZoneInfo(zone)).date()


def schedule_on(zone, offsets_to_searches, run_at=DUE):
    """A `search.toml` scheduling `{days from today: [(agent, window)]}` in `zone`."""
    today = local_today(zone)
    days = {day: [] for day in WEEKDAYS}
    for offset, searches in offsets_to_searches.items():
        days[WEEKDAYS[(today + timedelta(days=offset)).weekday()]] = searches
    return schedule_toml(zone, run_at, days)


# --- the gate: pure --------------------------------------------------------------------

NEW_YORK = schedule_toml(
    "America/New_York",
    "07:00",
    {
        "monday": [("perplexity", 72), ("claude", 48)],
        "tuesday": [("gemini", 24)],
    },
)


@pytest.fixture
def config_from(tmp_path, monkeypatch):
    def load(toml):
        profile = tmp_path / "profile"
        if not profile.exists():
            copy_example(profile)
        write_search_toml(profile, toml)
        monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
        return load_search_config()

    return load


def ordered(searches):
    return [(s.agent, s.window_hours) for s in searches]


MONDAY_SEARCHES = [("perplexity", 72), ("claude", 48)]


def test_the_gate_returns_nothing_on_an_unscheduled_weekday(config_from):
    wednesday_noon_in_new_york = datetime(2026, 10, 7, 16, 0, tzinfo=UTC)
    assert gate(config_from(NEW_YORK), wednesday_noon_in_new_york) == []


def test_the_gate_returns_nothing_before_run_at_on_a_scheduled_day(config_from):
    six_am_in_new_york = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    assert gate(config_from(NEW_YORK), six_am_in_new_york) == []


@pytest.mark.parametrize(
    "moment",
    [
        datetime(2026, 10, 5, 11, 0, tzinfo=UTC),
        datetime(2026, 10, 5, 19, 0, tzinfo=UTC),
    ],
    ids=["exactly at run_at", "well after run_at"],
)
def test_the_gate_returns_the_days_searches_in_order_at_or_after_run_at(
    config_from, moment
):
    assert ordered(gate(config_from(NEW_YORK), moment)) == MONDAY_SEARCHES


def test_late_monday_night_in_new_york_is_still_monday_though_it_is_tuesday_in_utc(
    config_from,
):
    half_past_eleven = datetime(
        2026, 10, 5, 23, 30, tzinfo=ZoneInfo("America/New_York")
    )
    assert half_past_eleven.astimezone(UTC).weekday() == 1
    assert ordered(gate(config_from(NEW_YORK), half_past_eleven)) == MONDAY_SEARCHES
    assert ordered(gate(config_from(NEW_YORK), half_past_eleven.astimezone(UTC))) == (
        MONDAY_SEARCHES
    )


def test_the_time_of_day_is_judged_in_the_profile_timezone_not_utc(config_from):
    # 10:00 UTC is past 07:00 on a UTC clock, but only 06:00 in New York.
    assert gate(config_from(NEW_YORK), datetime(2026, 10, 5, 10, 0, tzinfo=UTC)) == []


def test_a_timezone_ahead_of_utc_moves_the_weekday_forward(config_from):
    tokyo = schedule_toml("Asia/Tokyo", "04:00", {"tuesday": [("claude", 24)]})
    monday_evening_utc = datetime(2026, 10, 5, 20, 0, tzinfo=UTC)
    assert ordered(gate(config_from(tokyo), monday_evening_utc)) == [("claude", 24)]


@pytest.fixture
def host_clock_in_tokyo(monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_the_host_zone_never_decides_the_weekday_or_the_time(
    config_from, host_clock_in_tokyo
):
    config = config_from(NEW_YORK)
    late_monday = datetime(2026, 10, 6, 3, 30, tzinfo=UTC)
    early_monday = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)
    assert ordered(gate(config, late_monday)) == MONDAY_SEARCHES
    assert gate(config, early_monday) == []


def test_the_gate_makes_no_io(config_from, monkeypatch):
    config = config_from(NEW_YORK)
    before = config.model_copy(deep=True)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the gate did I/O")

    with monkeypatch.context() as refusing:
        refusing.setattr(builtins, "open", refuse)
        refusing.setattr(Path, "read_text", refuse)
        refusing.setattr(Path, "exists", refuse)
        refusing.setattr(socket.socket, "connect", refuse)
        refusing.setattr(db, "connect", refuse)
        refusing.setenv("JSA_PROFILE_DIR", "/nonexistent")
        due = gate(config, datetime(2026, 10, 5, 15, 0, tzinfo=UTC))
        again = gate(config, datetime(2026, 10, 5, 15, 0, tzinfo=UTC))
    assert ordered(due) == MONDAY_SEARCHES
    assert due == again
    assert config == before


# --- the command ----------------------------------------------------------------------


class Claude:
    """Stands in for the SDK's `query`; each call takes the next scripted answer (an exception fails it)."""

    def __init__(self):
        self.script = []
        self.calls = 0

    async def query(self, *, prompt, options=None, **_ignored):
        self.calls += 1
        step = self.script.pop(0) if self.script else None
        if isinstance(step, Exception):
            raise step
        yield result_message(result='{"postings": []}')


@pytest.fixture
def world(db_url, tmp_path, monkeypatch):
    drop_all_tables(db_url)
    claude = Claude()
    monkeypatch.setattr(agent_loop, "query", claude.query)
    for name in (
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "JSA_SEARCH_ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    requests = []

    def refuse_network(_transport, request):
        requests.append(request)
        raise AssertionError(f"unexpected request to {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse_network)
    profile = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    monkeypatch.chdir(tmp_path)
    zone = quiet_zone()

    class World:
        url = db_url

        def schedule(self, days, run_at=DUE):
            write_search_toml(profile, schedule_on(zone, days, run_at))

        today = local_today(zone).isoformat()
        sdk = claude

    World.zone = zone
    return World()


def jsa(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["jsa", *args])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


TWO = [("claude", 24), ("claude", 48)]


def app_tables(url):
    # SQLite's own bookkeeping table outlives a dropped AUTOINCREMENT table.
    return existing_tables(url) - {"sqlite_sequence"}


def search_runs(world):
    return rows(
        world.url,
        "SELECT trigger, run_date, window_hours, outcome, error FROM search_runs ORDER BY id",
    )


def claims(world):
    return [row[0] for row in rows(world.url, "SELECT run_date FROM cron_runs")]


def test_an_unscheduled_day_prints_why_exits_zero_and_never_touches_the_database(
    world, monkeypatch, capsys
):
    world.schedule({1: TWO})
    monkeypatch.setattr(
        db, "connect", lambda: pytest.fail("cron opened a database connection")
    )
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "no search scheduled today" in out + err
    assert app_tables(world.url) == set()
    assert world.sdk.calls == 0


def test_before_run_at_prints_why_exits_zero_and_never_touches_the_database(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO}, run_at=NOT_YET)
    monkeypatch.setattr(
        db, "connect", lambda: pytest.fail("cron opened a database connection")
    )
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "before run_at" in out + err
    assert app_tables(world.url) == set()
    assert world.sdk.calls == 0


def test_the_first_due_wake_claims_the_day_and_runs_its_searches_in_order(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    code, _out, _err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert claims(world) == [world.today]
    assert [(r[0], r[1], r[2], r[3]) for r in search_runs(world)] == [
        ("scheduled", world.today, 24, "ok"),
        ("scheduled", world.today, 48, "ok"),
    ]
    assert world.sdk.calls == 2


def test_a_second_due_wake_the_same_day_runs_nothing_and_exits_zero(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    jsa(monkeypatch, capsys, "cron")
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "already ran today" in out + err
    assert len(search_runs(world)) == 2
    assert claims(world) == [world.today]
    assert world.sdk.calls == 2


def test_the_claim_is_taken_before_the_searches_so_a_failed_day_is_not_retried(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    world.sdk.script = [RuntimeError("search broke"), RuntimeError("search broke")]
    code, _out, _err = jsa(monkeypatch, capsys, "cron")
    assert code != 0
    assert claims(world) == [world.today]
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "already ran today" in out + err
    assert world.sdk.calls == 2


def test_when_the_first_search_fails_the_second_still_runs_and_cron_exits_non_zero(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    world.sdk.script = [RuntimeError("the first search broke")]
    code, _out, _err = jsa(monkeypatch, capsys, "cron")
    assert code != 0
    first, second = search_runs(world)
    assert (first[2], first[3]) == (24, "failed")
    assert "the first search broke" in first[4]
    assert (second[2], second[3]) == (48, "ok")
    assert first[1] == second[1] == world.today
    assert world.sdk.calls == 2


def test_the_failure_of_a_search_is_reported_on_stderr_not_as_a_traceback(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    world.sdk.script = [RuntimeError("the first search broke")]
    _code, _out, err = jsa(monkeypatch, capsys, "cron")
    assert "the first search broke" in err
    assert "Traceback" not in err


def test_ungated_runs_before_run_at_writes_no_claim_and_records_smoke(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO}, run_at=NOT_YET)
    code, _out, _err = jsa(monkeypatch, capsys, "cron", "--ungated")
    assert code == 0
    assert [(r[0], r[2], r[3]) for r in search_runs(world)] == [
        ("smoke", 24, "ok"),
        ("smoke", 48, "ok"),
    ]
    assert claims(world) == []


def test_ungated_never_consumes_the_days_scheduled_run(world, monkeypatch, capsys):
    world.schedule({0: TWO})
    jsa(monkeypatch, capsys, "cron", "--ungated")
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "already ran" not in out + err
    assert [r[0] for r in search_runs(world)] == ["smoke"] * 2 + ["scheduled"] * 2
    assert claims(world) == [world.today]


def test_ungated_still_runs_after_the_day_was_claimed(world, monkeypatch, capsys):
    world.schedule({0: TWO})
    jsa(monkeypatch, capsys, "cron")
    code, _out, _err = jsa(monkeypatch, capsys, "cron", "--ungated")
    assert code == 0
    assert [r[0] for r in search_runs(world)] == ["scheduled"] * 2 + ["smoke"] * 2
    assert claims(world) == [world.today]


def test_ungated_with_nothing_today_runs_the_next_scheduled_days_searches(
    world, monkeypatch, capsys
):
    world.schedule({2: [("claude", 36)], 4: [("claude", 60)]})
    code, _out, _err = jsa(monkeypatch, capsys, "cron", "--ungated")
    assert code == 0
    assert [(r[0], r[2]) for r in search_runs(world)] == [("smoke", 36)]
    assert claims(world) == []


def test_ungated_with_no_search_on_any_weekday_runs_nothing_and_fails_saying_so(
    world, monkeypatch, capsys
):
    world.schedule({})
    code, _out, err = jsa(monkeypatch, capsys, "cron", "--ungated")
    assert code != 0
    assert "schedule" in err.lower()
    assert "Traceback" not in err
    assert world.sdk.calls == 0
    assert "cron_runs" not in existing_tables(world.url) or claims(world) == []


# --- hand runs --------------------------------------------------------------------------


def test_a_hand_search_never_writes_a_claim_and_ignores_the_gate(
    world, monkeypatch, capsys
):
    world.schedule({1: TWO}, run_at=NOT_YET)
    code, _out, _err = jsa(
        monkeypatch, capsys, "search", "--agent", "claude", "--window-hours", "24"
    )
    assert code == 0
    assert [(r[0], r[3]) for r in search_runs(world)] == [("hand", "ok")]
    assert claims(world) == []


def test_a_hand_search_does_not_use_up_the_days_scheduled_run(
    world, monkeypatch, capsys
):
    world.schedule({0: TWO})
    jsa(monkeypatch, capsys, "search", "--agent", "claude", "--window-hours", "24")
    code, out, err = jsa(monkeypatch, capsys, "cron")
    assert code == 0
    assert "already ran" not in out + err
    assert [r[0] for r in search_runs(world)] == ["hand", "scheduled", "scheduled"]


def test_the_claim_is_one_idempotent_insert_per_day(db_url):
    drop_all_tables(db_url)
    conn = db.connect()
    assert db.claim_cron_day(conn, "2026-10-05") is True
    assert db.claim_cron_day(conn, "2026-10-05") is False
    assert db.claim_cron_day(conn, "2026-10-06") is True
    conn.close()
    assert sorted(r[0] for r in rows(db_url, "SELECT run_date FROM cron_runs")) == [
        "2026-10-05",
        "2026-10-06",
    ]
