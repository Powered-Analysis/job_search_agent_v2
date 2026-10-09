import io
import os
import re
import subprocess
import sys
from itertools import count

import httpx
import pytest
from conftest import REPO_ROOT, drop_all_tables, unique_url, venv_script

from jsa import cli, db
from jsa.review import parse_feedback
from jsa.urls import canonicalize_url

LONG_AGO = "2020-01-01T00:00:00.000Z"
FEEDBACK_PROMPT = "Feedback (Enter to skip, :a/:s to change the decision): "
EMPTY_MESSAGE = "No postings awaiting review. 🎉"
INTERRUPT = object()
_serial = count(1)


class Script:
    """A non-TTY stdin that feeds scripted lines and can act between reads.

    `hooks` maps a zero-based read number to a callback run just before that
    read, which is how a test changes the world mid-session. `INTERRUPT` in the
    lines is a Ctrl-C; running out of lines is a Ctrl-D.
    """

    def __init__(self, lines, hooks=None):
        self.lines = list(lines)
        self.hooks = hooks or {}
        self.reads = 0

    def readline(self, *_):
        number, self.reads = self.reads, self.reads + 1
        if number in self.hooks:
            self.hooks[number]()
        if not self.lines:
            return ""
        line = self.lines.pop(0)
        if line is INTERRUPT:
            raise KeyboardInterrupt
        return line + "\n"

    def isatty(self):
        return False

    def fileno(self):
        raise io.UnsupportedOperation("fileno")


@pytest.fixture(autouse=True)
def chrome(tmp_path, monkeypatch):
    """Put a recording stand-in for macOS `open` first (and only) on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "open.log"
    script = bin_dir / "open"
    script.write_text(f'#!/bin/sh\necho "$*" >> "{log}"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("OPEN_BIN_DIR", str(bin_dir))

    def calls():
        return log.read_text().splitlines() if log.exists() else []

    return calls


class Web:
    """The outside world as review's re-check sees it: every page is live unless a test says otherwise.

    `routes` maps a full URL to an `httpx.Response` or a callable raising or answering.
    """

    def __init__(self):
        self.routes = {}
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        answer = self.routes.get(str(request.url))
        if callable(answer):
            return answer(request)
        if answer is not None:
            answer.request = request
            return answer
        return httpx.Response(
            200, html="<html><body>Open</body></html>", request=request
        )

    def gone(self, url):
        self.routes[url] = httpx.Response(404)

    def blocked(self, url):
        self.routes[url] = httpx.Response(403)

    def index(self, board, job_ids):
        url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
        jobs = [
            {"id": job_id, "updated_at": "2026-10-01T00:00:00Z"} for job_id in job_ids
        ]
        self.routes[url] = httpx.Response(200, json={"jobs": jobs})

    def offline(self):
        def refuse(request):
            raise httpx.ConnectError("offline", request=request)

        self.routes = _Everything(refuse)

    def count(self, host):
        return sum(1 for request in self.requests if request.url.host == host)


class _Everything(dict):
    def get(self, _key, _default=None):
        return self.answer

    def __init__(self, answer):
        super().__init__()
        self.answer = answer


@pytest.fixture(autouse=True)
def web(monkeypatch):
    """No test here reaches the network: review's re-check talks to this stand-in."""
    stand_in = Web()

    def handle(_transport, request):
        return stand_in.handle(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return stand_in


@pytest.fixture
def rdb(db_url):
    """A connection to a database holding no postings but the test's own."""
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


def seed(
    conn,
    company,
    *,
    order,
    title="Staff Platform Engineer",
    location="Remote, US",
    decision=None,
    closed=False,
    url=None,
):
    """Insert an undecided posting first seen on day `order`; returns its URL."""
    url = url or unique_url()
    posting_id = db.insert_posting(
        conn, company=company, title=title, url=url, search_agent="claude"
    )
    conn.execute(
        "UPDATE postings SET first_seen_at = ?, location = ?, decision = ?, "
        "decided_at = ?, closed_at = ? WHERE id = ?",
        (
            f"2026-01-{order:02d}T00:00:00.000Z",
            location,
            decision,
            LONG_AGO if decision else None,
            LONG_AGO if closed else None,
            posting_id,
        ),
    )
    return url


def name():
    return f"Company{next(_serial):03d}"


def row(conn, url):
    found = conn.execute(
        "SELECT decision, fit_feedback, decided_at FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()
    return dict(zip(("decision", "feedback", "decided_at"), found, strict=True))


def add_finding(conn, url, agent="claude", run_date="2026-01-01"):
    conn.execute(
        "INSERT INTO search_findings (run_date, agent, canonical_url, verification) "
        "VALUES (?, ?, ?, 'verified')",
        (run_date, agent, canonicalize_url(url)),
    )


def finding_decisions(conn, url):
    rows = conn.execute(
        "SELECT decision FROM search_findings WHERE canonical_url = ? ORDER BY agent, run_date",
        (canonicalize_url(url),),
    ).fetchall()
    return [r[0] for r in rows]


def jsa_review(monkeypatch, capsys, lines, hooks=None):
    """Run `jsa review` in-process with scripted input; returns (exit code, output)."""
    monkeypatch.setattr(sys, "argv", ["jsa", "review"])
    monkeypatch.setattr(sys, "stdin", Script(lines, hooks))
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


# --- backlog ------------------------------------------------------------------


@pytest.mark.parametrize("rows", ["none", "only decided and closed"])
def test_an_empty_backlog_says_so_and_exits_zero(rdb, monkeypatch, capsys, rows):
    if rows != "none":
        seed(rdb, name(), order=1, decision="Apply")
        seed(rdb, name(), order=2, decision="Skip")
        seed(rdb, name(), order=3, closed=True)
    code, output = jsa_review(monkeypatch, capsys, [])
    assert code == 0
    assert EMPTY_MESSAGE in output


def test_the_backlog_is_undecided_unclosed_postings_oldest_first(
    rdb, monkeypatch, capsys
):
    newest, oldest, middle = name(), name(), name()
    decided, closed = name(), name()
    seed(rdb, newest, order=9)
    seed(rdb, decided, order=2, decision="Skip")
    seed(rdb, oldest, order=1)
    seed(rdb, closed, order=3, closed=True)
    seed(rdb, middle, order=5)
    code, output = jsa_review(monkeypatch, capsys, ["s", "", "s", "", "s", "", "q"])
    assert code == 0
    assert decided not in output
    assert closed not in output
    assert output.index(oldest) < output.index(middle) < output.index(newest)
    assert EMPTY_MESSAGE not in output


def test_the_list_being_worked_does_not_change_during_the_session(
    rdb, monkeypatch, capsys
):
    first, second, latecomer = name(), name(), name()
    seed(rdb, first, order=1)
    second_url = seed(rdb, second, order=2)

    def the_world_moves_on():
        other = db.connect()
        seed(other, latecomer, order=3)
        db.record_decision(other, second_url, "Skip", "decided elsewhere")
        other.close()

    _, output = jsa_review(
        monkeypatch,
        capsys,
        ["a", "", "s", "", "q"],
        hooks={0: the_world_moves_on},
    )
    assert second in output
    assert latecomer not in output
    assert row(rdb, second_url)["decision"] == "Skip"


# --- each posting -------------------------------------------------------------


def test_each_posting_shows_company_title_location_and_url(rdb, monkeypatch, capsys):
    company, other = name(), name()
    url = seed(
        rdb, company, order=1, title="Principal Gadget Wrangler", location="Oslo"
    )
    seed(rdb, other, order=2, title="Hinge Auditor", location="Lisbon, Portugal")
    _, output = jsa_review(monkeypatch, capsys, ["a", "", "a", "", "q"])
    for text in (company, "Principal Gadget Wrangler", "Oslo", url):
        assert text in output
    for text in (other, "Hinge Auditor", "Lisbon, Portugal"):
        assert text in output


def test_each_posting_is_opened_in_google_chrome_with_macos_open(
    rdb, monkeypatch, capsys, chrome
):
    first = seed(rdb, name(), order=1)
    second = seed(rdb, name(), order=2)
    jsa_review(monkeypatch, capsys, ["a", "", "a", "", "q"])
    calls = chrome()
    assert any("Google Chrome" in call and first in call for call in calls)
    assert any("Google Chrome" in call and second in call for call in calls)


@pytest.mark.parametrize("failure", ["not installed", "exits nonzero"])
def test_a_chrome_launch_failure_does_not_stop_the_loop(
    rdb, monkeypatch, capsys, tmp_path, failure
):
    first, second = name(), name()
    first_url = seed(rdb, first, order=1)
    second_url = seed(rdb, second, order=2)
    bin_dir = tmp_path / "bin"
    if failure == "not installed":
        (bin_dir / "open").unlink()
    else:
        (bin_dir / "open").write_text("#!/bin/sh\nexit 1\n")
    code, output = jsa_review(monkeypatch, capsys, ["a", "", "s", "", "q"])
    assert code == 0
    assert "Traceback" not in output
    assert first_url in output and second_url in output
    assert row(rdb, first_url)["decision"] == "Apply"
    assert row(rdb, second_url)["decision"] == "Skip"


# --- decision prompt ----------------------------------------------------------


@pytest.mark.parametrize(("key", "stored"), [("a", "Apply"), ("s", "Skip")])
def test_a_and_s_store_the_exact_decision_strings(
    rdb, monkeypatch, capsys, key, stored
):
    url = seed(rdb, name(), order=1)
    jsa_review(monkeypatch, capsys, [key, "", "q"])
    assert row(rdb, url)["decision"] == stored


def test_other_input_reprompts_and_bare_enter_is_not_a_decision(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    code, output = jsa_review(
        monkeypatch, capsys, ["", "x", "apply", "yes", "1", "a", "", "q"]
    )
    assert code == 0
    assert "Traceback" not in output
    assert row(rdb, url)["decision"] == "Apply"


def test_input_that_never_becomes_a_valid_choice_decides_nothing(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    jsa_review(monkeypatch, capsys, ["", "x", "apply"])
    assert row(rdb, url) == {"decision": None, "feedback": None, "decided_at": None}


def test_bare_enter_keeps_the_existing_decision_and_feedback(rdb, monkeypatch, capsys):
    first_url = seed(rdb, name(), order=1)
    seed(rdb, name(), order=2)

    def age_the_first_decision():
        db.connect().execute(
            "UPDATE postings SET decided_at = ? WHERE canonical_url = ?",
            (LONG_AGO, canonicalize_url(first_url)),
        )

    jsa_review(
        monkeypatch,
        capsys,
        ["a", "great fit", "b", "", "", "q"],
        hooks={2: age_the_first_decision},
    )
    kept = row(rdb, first_url)
    assert kept["decision"] == "Apply"
    assert kept["feedback"] == "great fit"
    assert kept["decided_at"] > LONG_AGO


# --- feedback prompt ----------------------------------------------------------


def test_the_feedback_prompt_has_the_exact_wording(rdb, monkeypatch, capsys):
    seed(rdb, name(), order=1)
    _, output = jsa_review(monkeypatch, capsys, ["a", "", "q"])
    assert FEEDBACK_PROMPT in output


def test_the_feedback_prompt_is_prefilled_with_the_existing_note(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    seed(rdb, name(), order=2)
    _, output = jsa_review(monkeypatch, capsys, ["a", "remote only", "b", "s", "", "q"])
    assert "remote only" in output.split(FEEDBACK_PROMPT.rstrip(": "), 2)[2]
    amended = row(rdb, url)
    assert amended["decision"] == "Skip"
    assert amended["feedback"] == "remote only"


@pytest.mark.parametrize(
    ("first_key", "typed", "decision", "feedback"),
    [
        ("a", ":s too junior", "Skip", "too junior"),
        ("s", ":APPLY", "Apply", None),
        ("a", ":skip", "Skip", None),
        ("s", ":Apply  strong match ", "Apply", "strong match"),
        ("a", "a plain note", "Apply", "a plain note"),
        ("s", "", "Skip", None),
    ],
    ids=[
        "flip-to-skip",
        "flip-upper-apply",
        "flip-word",
        "flip-spaced",
        "note",
        "empty",
    ],
)
def test_feedback_commands_set_the_decision_and_keep_the_rest_as_the_note(
    rdb, monkeypatch, capsys, first_key, typed, decision, feedback
):
    url = seed(rdb, name(), order=1)
    jsa_review(monkeypatch, capsys, [first_key, typed, "q"])
    stored = row(rdb, url)
    assert stored["decision"] == decision
    assert stored["feedback"] == (
        feedback or stored["feedback"] if not feedback else feedback
    )
    if not feedback:
        assert stored["feedback"] in (None, "")


def test_back_at_the_feedback_prompt_writes_nothing(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    add_finding(rdb, url)
    jsa_review(monkeypatch, capsys, ["a", ":b", "q"])
    assert row(rdb, url) == {"decision": None, "feedback": None, "decided_at": None}
    assert finding_decisions(rdb, url) == [None]


@pytest.mark.parametrize("command", [":b", ":back", ":BACK"])
def test_back_returns_to_the_decision_prompt_for_the_same_posting(
    rdb, monkeypatch, capsys, command
):
    first_url = seed(rdb, name(), order=1)
    second_url = seed(rdb, name(), order=2)
    jsa_review(monkeypatch, capsys, ["a", command, "s", "needs a visa", "q"])
    assert row(rdb, first_url)["decision"] == "Skip"
    assert row(rdb, first_url)["feedback"] == "needs a visa"
    assert row(rdb, second_url)["decision"] is None


# --- persistence --------------------------------------------------------------


def test_submitting_feedback_writes_decision_note_and_time_together(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    jsa_review(monkeypatch, capsys, ["a", "love the mission", "q"])
    stored = row(rdb, url)
    assert stored["decision"] == "Apply"
    assert stored["feedback"] == "love the mission"
    assert stored["decided_at"] > LONG_AGO
    assert stored["decided_at"].endswith("Z")


def test_each_decision_is_committed_before_the_next_posting_is_shown(
    rdb, monkeypatch, capsys
):
    first_url = seed(rdb, name(), order=1)
    second_url = seed(rdb, name(), order=2)
    seen = {}

    def peek_from_another_connection():
        other = db.connect()
        seen.update(row(other, first_url))
        other.close()

    jsa_review(
        monkeypatch,
        capsys,
        ["a", "note", "s", "", "q"],
        hooks={2: peek_from_another_connection},
    )
    assert seen["decision"] == "Apply"
    assert seen["feedback"] == "note"
    assert row(rdb, second_url)["decision"] == "Skip"


def test_a_decision_sets_the_telemetry_decision_for_that_canonical_url(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    other_url = seed(rdb, name(), order=2)
    add_finding(rdb, url, "claude", "2026-01-01")
    add_finding(rdb, url, "gemini", "2026-01-02")
    add_finding(rdb, other_url, "claude", "2026-01-01")
    jsa_review(monkeypatch, capsys, ["a", "", "q"])
    assert finding_decisions(rdb, url) == ["Apply", "Apply"]
    assert finding_decisions(rdb, other_url) == [None]


def test_amending_a_decision_updates_the_telemetry_decision(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    seed(rdb, name(), order=2)
    add_finding(rdb, url)
    jsa_review(monkeypatch, capsys, ["a", "", "b", "s", "", "q"])
    assert finding_decisions(rdb, url) == ["Skip"]


# --- amending within a session ------------------------------------------------


def test_back_returns_to_the_previous_posting_shown_as_amending_and_refreshes_it(
    rdb, monkeypatch, capsys
):
    first, second = name(), name()
    first_url = seed(rdb, first, order=1)
    second_url = seed(rdb, second, order=2)

    def age_the_first_decision():
        db.connect().execute(
            "UPDATE postings SET decided_at = ? WHERE canonical_url = ?",
            (LONG_AGO, canonicalize_url(first_url)),
        )

    _, output = jsa_review(
        monkeypatch,
        capsys,
        ["a", "", "b", "s", "", "a", "", "q"],
        hooks={2: age_the_first_decision},
    )
    assert "(amending)" in output
    assert output.count(first) == 2
    amended = row(rdb, first_url)
    assert amended["decision"] == "Skip"
    assert amended["decided_at"] > LONG_AGO
    assert row(rdb, second_url)["decision"] == "Apply"


def test_a_posting_is_not_marked_as_amending_the_first_time(rdb, monkeypatch, capsys):
    seed(rdb, name(), order=1)
    _, output = jsa_review(monkeypatch, capsys, ["q"])
    assert "(amending)" not in output


def test_deciding_the_last_posting_ends_the_session_without_a_further_prompt(
    rdb, monkeypatch, capsys
):
    company = name()
    url = seed(rdb, company, order=1)
    stdin = Script(["a", "", "s", "changed my mind"])
    monkeypatch.setattr(sys, "argv", ["jsa", "review"])
    monkeypatch.setattr(sys, "stdin", stdin)
    cli.main()
    output = capsys.readouterr().out
    assert output.count(company) == 1
    assert "(amending)" not in output
    assert row(rdb, url)["decision"] == "Apply"
    assert stdin.lines == ["s", "changed my mind"]


def test_the_last_posting_is_shown_once_and_later_stdin_lines_stay_unread(
    rdb, monkeypatch, capsys
):
    company = name()
    url = seed(rdb, company, order=1)
    stdin = Script(["a", "", "", "", "never read"])
    monkeypatch.setattr(sys, "argv", ["jsa", "review"])
    monkeypatch.setattr(sys, "stdin", stdin)
    cli.main()
    output = capsys.readouterr().out
    assert output.count(company) == 1
    assert row(rdb, url)["decision"] == "Apply"
    assert stdin.lines == ["", "", "never read"]


# --- quitting -----------------------------------------------------------------


def test_q_quits_and_leaves_the_rest_of_the_backlog_untouched(rdb, monkeypatch, capsys):
    first_url = seed(rdb, name(), order=1)
    second_url = seed(rdb, name(), order=2)
    third_url = seed(rdb, name(), order=3)
    code, output = jsa_review(monkeypatch, capsys, ["a", "", "q"])
    assert code == 0
    assert "Traceback" not in output
    assert row(rdb, first_url)["decision"] == "Apply"
    assert row(rdb, second_url)["decision"] is None
    assert row(rdb, third_url)["decision"] is None


@pytest.mark.parametrize("ending", ["ctrl-d", "ctrl-c"])
@pytest.mark.parametrize(
    "where",
    ["decision prompt", "feedback prompt", "amending prompt", "after a commit"],
)
def test_ctrl_c_and_ctrl_d_quit_cleanly_and_keep_earlier_state(
    rdb, monkeypatch, capsys, where, ending
):
    first_url = seed(rdb, name(), order=1)
    second_url = seed(rdb, name(), order=2)
    end = [INTERRUPT] if ending == "ctrl-c" else []
    script = {
        "decision prompt": end,
        "feedback prompt": ["a", *end],
        "amending prompt": ["a", "kept note", "b", "s", *end],
        "after a commit": ["a", "kept note", *end],
    }[where]
    _, output = jsa_review(monkeypatch, capsys, script)
    assert "Traceback" not in output
    first, second = row(rdb, first_url), row(rdb, second_url)
    assert second["decision"] is None
    if where in ("decision prompt", "feedback prompt"):
        assert first == {"decision": None, "feedback": None, "decided_at": None}
    else:
        assert first["decision"] == "Apply"
        assert first["feedback"] == "kept note"


# --- clearing and the other decision writes -----------------------------------


def test_clearing_a_decision_nulls_it_and_leaves_telemetry_alone(rdb):
    url = seed(rdb, name(), order=1)
    add_finding(rdb, url)
    db.record_decision(rdb, url, "Skip", "too junior")
    db.clear_decision(rdb, url)
    assert row(rdb, url) == {"decision": None, "feedback": None, "decided_at": None}
    assert finding_decisions(rdb, url) == ["Skip"]


def test_a_cleared_posting_is_back_in_the_review_backlog(rdb):
    url = seed(rdb, name(), order=1)
    db.record_decision(rdb, url, "Apply", "note")
    assert db.review_backlog(rdb) == []
    db.clear_decision(rdb, url)
    assert [entry[-1] for entry in db.review_backlog(rdb)] == [url]


def test_recording_a_decision_stores_it_with_its_feedback_and_syncs_telemetry(rdb):
    url = seed(rdb, name(), order=1)
    other_url = seed(rdb, name(), order=2)
    add_finding(rdb, url)
    add_finding(rdb, other_url)
    db.record_decision(rdb, url, "Apply", "great fit")
    stored = row(rdb, url)
    assert (stored["decision"], stored["feedback"]) == ("Apply", "great fit")
    assert stored["decided_at"] > LONG_AGO
    assert finding_decisions(rdb, url) == ["Apply"]
    assert finding_decisions(rdb, other_url) == [None]


def test_recording_a_decision_with_no_feedback_replaces_the_old_note(rdb):
    url = seed(rdb, name(), order=1)
    db.record_decision(rdb, url, "Apply", "first thought")
    db.record_decision(rdb, url, "Skip", None)
    stored = row(rdb, url)
    assert stored["decision"] == "Skip"
    assert stored["feedback"] in (None, "")


def test_changing_a_decision_keeps_the_note_refreshes_the_time_and_syncs_telemetry(
    rdb,
):
    url = seed(rdb, name(), order=1)
    add_finding(rdb, url)
    db.record_decision(rdb, url, "Apply", "keep me")
    rdb.execute(
        "UPDATE postings SET decided_at = ? WHERE canonical_url = ?",
        (LONG_AGO, canonicalize_url(url)),
    )
    db.set_decision(rdb, url, "Skip")
    stored = row(rdb, url)
    assert (stored["decision"], stored["feedback"]) == ("Skip", "keep me")
    assert stored["decided_at"] > LONG_AGO
    assert finding_decisions(rdb, url) == ["Skip"]


def test_a_decision_write_finds_the_posting_by_its_canonical_url(rdb):
    url = seed(rdb, name(), order=1)
    db.record_decision(rdb, f"{url}?utm_source=newsletter", "Apply", None)
    assert row(rdb, url)["decision"] == "Apply"


# --- parsing feedback (pure) --------------------------------------------------


@pytest.mark.parametrize(
    ("current", "typed", "action", "decision", "feedback"),
    [
        ("Apply", "", "save", "Apply", None),
        ("Skip", "   ", "save", "Skip", None),
        ("Apply", "plain note", "save", "Apply", "plain note"),
        ("Apply", ":s too junior", "save", "Skip", "too junior"),
        ("Apply", ":skip too junior", "save", "Skip", "too junior"),
        ("Skip", ":a", "save", "Apply", None),
        ("Skip", ":APPLY", "save", "Apply", None),
        ("Skip", ":Apply worth a shot", "save", "Apply", "worth a shot"),
        ("Apply", "mentions :s in passing", "save", "Apply", "mentions :s in passing"),
        ("Apply", ":b", "back", "Apply", None),
        ("Skip", ":BACK", "back", "Skip", None),
    ],
    ids=[
        "empty",
        "blank",
        "note",
        "short-skip",
        "long-skip",
        "short-apply",
        "upper-apply",
        "apply-with-note",
        "command-not-first",
        "short-back",
        "upper-back",
    ],
)
def test_parse_feedback(current, typed, action, decision, feedback):
    parsed = parse_feedback(current, typed)
    assert parsed.action == action
    assert parsed.decision == decision
    assert parsed.feedback == feedback


# --- no TTY, no model ---------------------------------------------------------


def test_review_runs_end_to_end_on_piped_input_and_reprompts_on_invalid_choices(
    db_url, tmp_path
):
    drop_all_tables(db_url)
    conn = db.connect()
    # A refused loopback connection: the re-check can't reach it, so it is shown (PRD 03).
    url = seed(conn, name(), order=1, url="http://127.0.0.1:1/jobs/1")
    env = {**os.environ, "TURSO_DATABASE_URL": db_url}
    result = subprocess.run(
        [venv_script("jsa"), "review"],
        input="x\n\nnope\ns\n:a looks good\nq\n",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert row(conn, url)["decision"] == "Apply"
    assert row(conn, url)["feedback"] == "looks good"
    conn.close()


def test_review_with_an_empty_backlog_exits_zero_as_a_real_command(db_url, tmp_path):
    drop_all_tables(db_url)
    env = {**os.environ, "TURSO_DATABASE_URL": db_url}
    result = subprocess.run(
        [venv_script("jsa"), "review"],
        input="",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert EMPTY_MESSAGE in result.stdout


def test_review_is_listed_in_the_help():
    result = subprocess.run(
        [venv_script("jsa"), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0
    assert "review" in result.stdout


def test_a_review_session_makes_no_model_call_only_the_recheck_http(
    rdb, web, monkeypatch, capsys
):
    # A None entry makes any import of the SDK raise, so a model call cannot happen.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    seed(rdb, name(), order=1)
    seed(rdb, name(), order=2)
    code, _ = jsa_review(monkeypatch, capsys, ["a", "note", "s", "", "q"])
    assert code == 0
    assert web.requests
    assert {request.url.host for request in web.requests} <= {
        "job-boards.greenhouse.io",
        "boards-api.greenhouse.io",
    }


def test_the_review_module_does_not_load_the_claude_sdk():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, jsa.review; "
                "bad = [m for m in sys.modules if m.startswith('claude_agent_sdk')]; "
                "sys.exit(1 if bad else 0)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# --- liveness re-check at the start of a session (PRD 03, PRD 01 re-check) -------------


def gh_url(job_id):
    return f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"


def closed_at(conn, url):
    return conn.execute(
        "SELECT closed_at FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()[0]


def test_a_posting_that_closed_while_waiting_is_marked_closed_and_never_shown(
    rdb, web, monkeypatch, capsys
):
    dead, alive = name(), name()
    dead_url = seed(rdb, dead, order=1, url=gh_url(101))
    alive_url = seed(rdb, alive, order=2, url=gh_url(102))
    web.index("acme", [102])
    code, output = jsa_review(monkeypatch, capsys, ["s", "", "q"])
    assert code == 0
    assert dead not in output
    assert alive in output
    assert closed_at(rdb, dead_url) is not None
    assert closed_at(rdb, alive_url) is None
    assert row(rdb, dead_url)["decision"] is None
    assert row(rdb, alive_url)["decision"] == "Skip"


def test_the_session_opens_by_saying_how_many_postings_closed(
    rdb, web, monkeypatch, capsys
):
    first = name()
    seed(rdb, name(), order=1, url=gh_url(201))
    seed(rdb, name(), order=2, url=gh_url(202))
    seed(rdb, first, order=3, url=gh_url(203))
    web.index("acme", [203])
    _, output = jsa_review(monkeypatch, capsys, ["q"])
    report = re.search(r"2\b[^\n]*clos", output)
    assert report is not None, output
    assert report.start() < output.index(first)


def test_the_closed_count_is_reported_even_when_it_is_zero(
    rdb, web, monkeypatch, capsys
):
    seed(rdb, name(), order=1, url=gh_url(301))
    web.index("acme", [301])
    _, output = jsa_review(monkeypatch, capsys, ["q"])
    assert re.search(r"\b0\b[^\n]*clos", output), output


def test_the_re_check_happens_before_the_first_posting_is_prompted_for(
    rdb, web, monkeypatch, capsys
):
    dead_url = seed(rdb, name(), order=1, url=gh_url(401))
    seed(rdb, name(), order=2, url=gh_url(402))
    web.index("acme", [402])
    seen = []
    hooks = {0: lambda: seen.append(closed_at(rdb, dead_url))}
    jsa_review(monkeypatch, capsys, ["q"], hooks)
    assert seen and seen[0] is not None


def test_when_every_posting_closed_it_reports_the_count_then_the_empty_message(
    rdb, web, monkeypatch, capsys
):
    seed(rdb, name(), order=1, url=gh_url(501))
    seed(rdb, name(), order=2, url=gh_url(502))
    web.index("acme", [])
    code, output = jsa_review(monkeypatch, capsys, [])
    assert code == 0
    report = re.search(r"2\b[^\n]*clos", output)
    assert report is not None, output
    assert report.start() < output.index(EMPTY_MESSAGE)


def test_a_posting_the_re_check_could_not_reach_is_shown_marked_not_re_checked(
    rdb, web, monkeypatch, capsys
):
    unreachable, reachable = name(), name()
    blocked_url = seed(rdb, unreachable, order=1, url=gh_url(601))
    seed(rdb, reachable, order=2, url="https://careers.example.com/jobs/601")
    web.routes["https://boards-api.greenhouse.io/v1/boards/acme/jobs"] = httpx.Response(
        429
    )
    _, output = jsa_review(monkeypatch, capsys, ["q"])
    shown = output[output.index(unreachable) :]
    assert "not re-checked" in shown.splitlines()[0].lower()
    assert closed_at(rdb, blocked_url) is None


def test_only_the_unreachable_posting_is_marked_not_re_checked(
    rdb, web, monkeypatch, capsys
):
    unreachable, reachable = name(), name()
    seed(rdb, unreachable, order=1, url=gh_url(701))
    seed(rdb, reachable, order=2, url="https://careers.example.com/jobs/701")
    web.blocked("https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    web.routes["https://boards-api.greenhouse.io/v1/boards/acme/jobs"] = httpx.Response(
        503
    )
    _, output = jsa_review(monkeypatch, capsys, ["s", "", "q"])
    second = output[output.index(reachable) :]
    assert "not re-checked" not in second.splitlines()[0].lower()


def test_a_403_or_429_from_the_page_is_a_block_not_a_closure(
    rdb, web, monkeypatch, capsys
):
    names = [name(), name()]
    urls = [
        seed(rdb, names[0], order=1, url="https://careers.example.com/jobs/801"),
        seed(rdb, names[1], order=2, url="https://careers.example.com/jobs/802"),
    ]
    web.blocked(urls[0])
    web.routes[urls[1]] = httpx.Response(429)
    _, output = jsa_review(monkeypatch, capsys, ["q"])
    assert names[0] in output
    assert all(closed_at(rdb, url) is None for url in urls)


def test_offline_every_posting_is_shown_and_none_is_closed(
    rdb, web, monkeypatch, capsys
):
    names = [name(), name(), name()]
    urls = [
        seed(rdb, names[0], order=1, url=gh_url(901)),
        seed(rdb, names[1], order=2, url="https://careers.example.com/jobs/902"),
        seed(rdb, names[2], order=3, url=gh_url(903)),
    ]
    web.offline()
    code, output = jsa_review(monkeypatch, capsys, ["s", "", "s", "", "s", "", "q"])
    assert code == 0
    assert all(n in output for n in names)
    assert all(closed_at(rdb, url) is None for url in urls)
    assert output.lower().count("not re-checked") >= 3


def test_a_page_that_redirects_away_from_the_job_closes_it(
    rdb, web, monkeypatch, capsys
):
    url = "https://careers.example.com/jobs/senior-engineer-1001"
    dead = name()
    seed(rdb, dead, order=1, url=url)
    web.routes[url] = httpx.Response(
        302, headers={"location": "https://careers.example.com/"}
    )
    web.routes["https://careers.example.com/"] = httpx.Response(200, html="<html/>")
    _, output = jsa_review(monkeypatch, capsys, [])
    assert dead not in output
    assert closed_at(rdb, url) is not None


def test_each_board_index_is_fetched_once_for_the_whole_session(
    rdb, web, monkeypatch, capsys
):
    for number in range(1, 5):
        seed(rdb, name(), order=number, url=gh_url(1100 + number))
    web.index("acme", [1101, 1102])
    jsa_review(monkeypatch, capsys, ["q"])
    assert web.count("boards-api.greenhouse.io") == 1


def test_the_re_check_leaves_decided_postings_and_their_decisions_alone(
    rdb, web, monkeypatch, capsys
):
    decided_url = seed(rdb, name(), order=1, decision="Apply", url=gh_url(1201))
    seed(rdb, name(), order=2, url=gh_url(1202))
    web.index("acme", [1202])
    jsa_review(monkeypatch, capsys, ["q"])
    assert row(rdb, decided_url)["decision"] == "Apply"
    assert closed_at(rdb, decided_url) is None


def test_the_re_check_writes_no_search_findings_row(rdb, web, monkeypatch, capsys):
    dead_url = seed(rdb, name(), order=1, url=gh_url(1301))
    seed(rdb, name(), order=2, url=gh_url(1302))
    add_finding(rdb, dead_url)
    web.index("acme", [1302])
    before = rdb.execute("SELECT COUNT(*) FROM search_findings").fetchone()[0]
    jsa_review(monkeypatch, capsys, ["q"])
    after = rdb.execute("SELECT COUNT(*) FROM search_findings").fetchone()[0]
    assert after == before
    assert finding_decisions(rdb, dead_url) == [None]


def test_a_closed_posting_stays_closed_for_the_next_session(
    rdb, web, monkeypatch, capsys
):
    dead = name()
    dead_url = seed(rdb, dead, order=1, url=gh_url(1401))
    web.index("acme", [])
    jsa_review(monkeypatch, capsys, [])
    web.index("acme", [1401])
    _, output = jsa_review(monkeypatch, capsys, [])
    assert dead not in output
    assert closed_at(rdb, dead_url) is not None


# --- jsa revise ---------------------------------------------------------------


def posting_id(conn, url):
    return conn.execute(
        "SELECT id FROM postings WHERE canonical_url = ?", (canonicalize_url(url),)
    ).fetchone()[0]


def decide(conn, url, decision, feedback=None, decided_at=LONG_AGO):
    """Give a seeded posting a decision decided at a chosen time."""
    db.record_decision(conn, url, decision, feedback)
    conn.execute(
        "UPDATE postings SET decided_at = ? WHERE canonical_url = ?",
        (decided_at, canonicalize_url(url)),
    )


def jsa_revise(monkeypatch, capsys, args, lines=()):
    """Run `jsa revise <args>` in-process with scripted input; returns (exit code, output, stdin)."""
    stdin = Script(lines)
    monkeypatch.setattr(sys, "argv", ["jsa", "revise", *args])
    monkeypatch.setattr(sys, "stdin", stdin)
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err, stdin


def test_revise_by_id_prefills_the_note_and_offers_the_decision_as_the_default(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "remote only")
    code, output, _ = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["", ""]
    )
    assert code == 0
    assert "Enter keeps Apply" in output
    assert FEEDBACK_PROMPT.split(":")[0] in output
    assert "[remote only]" in output.split("Enter keeps Apply", 1)[1]


def test_revise_with_bare_enters_keeps_the_decision_and_note(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Skip", "too junior")
    jsa_revise(monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["", ""])
    kept = row(rdb, url)
    assert kept["decision"] == "Skip"
    assert kept["feedback"] == "too junior"
    assert kept["decided_at"] > LONG_AGO


def test_revise_updates_decision_note_and_decided_at_together(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "great fit")
    add_finding(rdb, url)
    code, _, _ = jsa_revise(
        monkeypatch,
        capsys,
        ["--id", str(posting_id(rdb, url))],
        ["s", "on reflection, too senior"],
    )
    assert code == 0
    amended = row(rdb, url)
    assert amended["decision"] == "Skip"
    assert amended["feedback"] == "on reflection, too senior"
    assert amended["decided_at"] > LONG_AGO
    assert finding_decisions(rdb, url) == ["Skip"]


@pytest.mark.parametrize(
    ("typed", "decision", "feedback"),
    [
        (":s changed my mind", "Skip", "changed my mind"),
        (":APPLY", "Apply", None),
        ("a fresh note", "Skip", "a fresh note"),
        ("", "Skip", None),
    ],
    ids=["flip-with-note", "flip-alone", "note-only", "cleared-note"],
)
def test_revise_feedback_commands_work_as_in_review(
    rdb, monkeypatch, capsys, typed, decision, feedback
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Skip", "old note")
    jsa_revise(monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["", typed])
    amended = row(rdb, url)
    assert amended["decision"] == decision
    assert amended["feedback"] == (feedback if typed != "" else "old note")


def test_revise_amends_only_the_named_posting(rdb, monkeypatch, capsys):
    target_url = seed(rdb, name(), order=1)
    other_url = seed(rdb, name(), order=2)
    decide(rdb, target_url, "Apply", "one")
    decide(rdb, other_url, "Apply", "two")
    jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, target_url))], ["s", ""]
    )
    assert row(rdb, target_url)["decision"] == "Skip"
    assert row(rdb, other_url) == {
        "decision": "Apply",
        "feedback": "two",
        "decided_at": LONG_AGO,
    }


def test_revise_shows_the_posting_marked_amending_and_opens_it_in_chrome(
    rdb, monkeypatch, capsys, chrome
):
    company = name()
    url = seed(rdb, company, order=1, title="Principal Engineer", location="Berlin")
    decide(rdb, url, "Apply")
    _, output, _ = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["q"]
    )
    assert company in output
    assert "Principal Engineer" in output
    assert "Berlin" in output
    assert url in output
    assert "(amending)" in output
    assert chrome() == [f"-a Google Chrome {url}"]


def test_revise_does_not_stop_when_chrome_cannot_be_opened(
    rdb, monkeypatch, capsys, tmp_path
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply")
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    code, _, _ = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["s", "note"]
    )
    assert code == 0
    assert row(rdb, url)["decision"] == "Skip"


def test_revise_last_picks_the_newest_decided_at(rdb, monkeypatch, capsys):
    older_url = seed(rdb, name(), order=1)
    newest = name()
    newest_url = seed(rdb, newest, order=2)
    newer_url = seed(rdb, name(), order=3)
    decide(rdb, older_url, "Apply", decided_at="2026-02-01T00:00:00.000Z")
    decide(rdb, newest_url, "Apply", decided_at="2026-03-01T00:00:00.000Z")
    decide(rdb, newer_url, "Apply", decided_at="2026-02-15T00:00:00.000Z")
    _, output, _ = jsa_revise(monkeypatch, capsys, ["--last"], ["s", "second thoughts"])
    assert newest in output
    assert row(rdb, newest_url)["decision"] == "Skip"
    assert row(rdb, newest_url)["feedback"] == "second thoughts"
    assert row(rdb, older_url)["decision"] == "Apply"
    assert row(rdb, newer_url)["decision"] == "Apply"


def test_revise_last_breaks_ties_with_the_highest_id(rdb, monkeypatch, capsys):
    stamp = "2026-02-01T00:00:00.000Z"
    urls = [seed(rdb, name(), order=order) for order in (1, 2, 3)]
    for url in urls:
        decide(rdb, url, "Apply", decided_at=stamp)
    highest = max(urls, key=lambda url: posting_id(rdb, url))
    jsa_revise(monkeypatch, capsys, ["--last"], ["s", ""])
    assert [row(rdb, url)["decision"] for url in urls if url != highest] == [
        "Apply",
        "Apply",
    ]
    assert row(rdb, highest)["decision"] == "Skip"


def test_revise_last_ignores_undecided_postings(rdb, monkeypatch, capsys):
    decided = name()
    decided_url = seed(rdb, decided, order=1)
    decide(rdb, decided_url, "Apply")
    undecided_url = seed(rdb, name(), order=2)
    _, output, _ = jsa_revise(monkeypatch, capsys, ["--last"], ["q"])
    assert decided in output
    assert row(rdb, undecided_url)["decision"] is None


def test_revise_last_picks_the_posting_just_amended_by_revise(rdb, monkeypatch, capsys):
    first = name()
    first_url = seed(rdb, first, order=1)
    second_url = seed(rdb, name(), order=2)
    decide(rdb, first_url, "Apply", decided_at="2026-02-01T00:00:00.000Z")
    decide(rdb, second_url, "Apply", decided_at="2026-03-01T00:00:00.000Z")
    jsa_revise(monkeypatch, capsys, ["--id", str(posting_id(rdb, first_url))], ["", ""])
    _, output, _ = jsa_revise(monkeypatch, capsys, ["--last"], ["q"])
    assert first in output
    assert row(rdb, second_url)["decided_at"] == "2026-03-01T00:00:00.000Z"


def test_revise_last_picks_the_posting_just_decided_by_review(rdb, monkeypatch, capsys):
    old_url = seed(rdb, name(), order=1, decision="Skip")
    fresh = name()
    seed(rdb, fresh, order=2)
    jsa_review(monkeypatch, capsys, ["a", "", "q"])
    _, output, _ = jsa_revise(monkeypatch, capsys, ["--last"], ["q"])
    assert fresh in output
    assert row(rdb, old_url)["decided_at"] == LONG_AGO


@pytest.mark.parametrize("args", [["--id", "1", "--last"], []], ids=["both", "neither"])
def test_revise_needs_exactly_one_of_id_and_last(rdb, monkeypatch, capsys, args):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "keep")
    code, output, stdin = jsa_revise(monkeypatch, capsys, args, ["s", "changed"])
    assert code == 2
    assert "usage" in output.lower()
    assert "Traceback" not in output
    assert stdin.reads == 0
    assert row(rdb, url)["feedback"] == "keep"
    assert row(rdb, url)["decided_at"] == LONG_AGO


@pytest.mark.parametrize("bad_id", ["abc", "1.5"])
def test_revise_rejects_an_id_that_is_not_a_number(rdb, monkeypatch, capsys, bad_id):
    code, output, stdin = jsa_revise(monkeypatch, capsys, ["--id", bad_id], ["s"])
    assert code != 0
    assert "Traceback" not in output
    assert stdin.reads == 0


def test_revise_of_an_undecided_posting_fails_pointing_to_review(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    code, output, stdin = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["a", "note"]
    )
    assert code == 1
    assert "jsa review" in output
    assert len(output.strip().splitlines()) == 1
    assert "Traceback" not in output
    assert stdin.reads == 0
    assert row(rdb, url) == {"decision": None, "feedback": None, "decided_at": None}


def test_revise_of_an_unknown_id_fails_in_one_line_and_changes_no_row(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "keep")
    unknown = posting_id(rdb, url) + 1000
    code, output, stdin = jsa_revise(
        monkeypatch, capsys, ["--id", str(unknown)], ["s", "changed"]
    )
    assert code == 1
    assert len(output.strip().splitlines()) == 1
    assert str(unknown) in output
    assert "Traceback" not in output
    assert stdin.reads == 0
    assert row(rdb, url) == {
        "decision": "Apply",
        "feedback": "keep",
        "decided_at": LONG_AGO,
    }


def test_revise_last_with_no_decided_postings_fails_in_one_line(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    code, output, stdin = jsa_revise(monkeypatch, capsys, ["--last"], ["a", "note"])
    assert code == 1
    assert len(output.strip().splitlines()) == 1
    assert "Traceback" not in output
    assert stdin.reads == 0
    assert row(rdb, url) == {"decision": None, "feedback": None, "decided_at": None}


def test_revise_last_on_an_empty_database_fails_in_one_line(rdb, monkeypatch, capsys):
    code, output, _ = jsa_revise(monkeypatch, capsys, ["--last"])
    assert code == 1
    assert len(output.strip().splitlines()) == 1
    assert "Traceback" not in output


@pytest.mark.parametrize("ending", ["ctrl-d", "ctrl-c"])
@pytest.mark.parametrize(
    "where",
    ["decision prompt", "feedback prompt", "quit key", "back at feedback then quit"],
)
def test_revise_quitting_leaves_the_posting_as_it_was(
    rdb, monkeypatch, capsys, where, ending
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "original note")
    end = [INTERRUPT] if ending == "ctrl-c" else []
    script = {
        "decision prompt": end,
        "feedback prompt": ["s", *end],
        "quit key": ["q"],
        "back at feedback then quit": ["s", ":b", "q"],
    }[where]
    code, output, _ = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], script
    )
    assert code == 0
    assert "Traceback" not in output
    assert row(rdb, url) == {
        "decision": "Apply",
        "feedback": "original note",
        "decided_at": LONG_AGO,
    }


def test_revise_reprompts_on_invalid_choices(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "note")
    jsa_revise(
        monkeypatch,
        capsys,
        ["--id", str(posting_id(rdb, url))],
        ["x", "nope", "s", "new note"],
    )
    assert row(rdb, url)["decision"] == "Skip"
    assert row(rdb, url)["feedback"] == "new note"


def test_revise_back_at_the_feedback_prompt_returns_to_the_decision_prompt(
    rdb, monkeypatch, capsys
):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply", "note")
    jsa_revise(
        monkeypatch,
        capsys,
        ["--id", str(posting_id(rdb, url))],
        ["s", ":b", "s", "second try"],
    )
    assert row(rdb, url)["decision"] == "Skip"
    assert row(rdb, url)["feedback"] == "second try"


def test_a_revised_posting_stays_out_of_the_review_backlog(rdb, monkeypatch, capsys):
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply")
    jsa_revise(monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["s", "x"])
    _, output = jsa_review(monkeypatch, capsys, [])
    assert EMPTY_MESSAGE in output


def test_revise_runs_end_to_end_as_a_real_command(db_url, tmp_path):
    drop_all_tables(db_url)
    conn = db.connect()
    url = seed(conn, name(), order=1)
    decide(conn, url, "Apply", "piped note")
    env = {**os.environ, "TURSO_DATABASE_URL": db_url}
    result = subprocess.run(
        [venv_script("jsa"), "revise", "--last"],
        input="x\ns\n:a still good\n",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Traceback" not in result.stderr
    assert row(conn, url)["decision"] == "Apply"
    assert row(conn, url)["feedback"] == "still good"
    assert row(conn, url)["decided_at"] > LONG_AGO
    conn.close()


def test_revise_exits_non_zero_as_a_real_command_for_an_undecided_posting(
    db_url, tmp_path
):
    drop_all_tables(db_url)
    conn = db.connect()
    url = seed(conn, name(), order=1)
    env = {**os.environ, "TURSO_DATABASE_URL": db_url}
    result = subprocess.run(
        [venv_script("jsa"), "revise", "--id", str(posting_id(conn, url))],
        input="a\n\n",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert "jsa review" in result.stderr
    assert "Traceback" not in result.stderr
    assert row(conn, url)["decision"] is None
    conn.close()


def test_revise_is_listed_in_the_help():
    result = subprocess.run(
        [venv_script("jsa"), "--help"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0
    assert "revise" in result.stdout


def test_revise_makes_no_model_call_and_no_http(rdb, web, monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    url = seed(rdb, name(), order=1)
    decide(rdb, url, "Apply")
    code, _, _ = jsa_revise(
        monkeypatch, capsys, ["--id", str(posting_id(rdb, url))], ["s", "x"]
    )
    assert code == 0
    assert web.requests == []
