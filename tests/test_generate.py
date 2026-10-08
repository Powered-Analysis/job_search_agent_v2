"""`jsa generate` (issue #13; PRD 04 "Resume checklist", "Base resume" and "Packet directory"; PRD 02 "Packet queue"; XC-9 to XC-14).

The database is the libSQL container (and a `file:` database). The outside world is replaced at the
documented seams: HTTP at the transport, Claude at `agent_loop.query`, and `gws` by a stub script
reached through `JSA_GWS_BIN`. The profile and the packets directory are temporary directories.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path
from types import SimpleNamespace

import docx
import httpx
import pytest
from claude_agent_sdk import ResultMessage
from conftest import drop_all_tables
from docx.oxml import parse_xml
from pandoc_helpers import install_pandoc, pandoc_calls
from profile_helpers import copy_example, write_config_toml
from test_claude_runner import result_message
from test_review import Web
from test_tracker import STUB, appended_rows, calls

from jsa import agent_loop, cli, db
from jsa.resume import render_document, render_resume

LONG_AGO = "2020-01-01T00:00:00.000Z"
JD = "# Staff Engineer\n\nBuild the Frobnicator platform.\n"
PLAIN = "Acme Widgets - Staff Engineer"
PDF = "resume_checklist.pdf"
RESUME_COPY = "PatExample_Resume_StaffEngineer_AcmeWidgets.docx"
BASE_LINE = "Built the Quuxlate platform from scratch."
CHECKLIST_TEXT = "## Lead with\n\n- the Quuxlate platform\n"
CONFIG = """\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-123"
packets_dir = "{packets}"

[agents.checklist]
model = "claude-sonnet-5-5"
effort = "low"

[agents.redline]
model = "claude-opus-5-5"
effort = "medium"
"""
REDLINE_MODEL = "claude-opus-5-5"
REDLINE_EDITS = "redline_edits.json"
# One edit the validator drops (no such paragraph): a well-formed, non-empty redline result.
REDLINE_JSON = json.dumps(
    [
        {
            "paragraph": 99,
            "find": "nothing here",
            "replace": "nothing there",
            "jd_quote": "Frobnicator platform",
            "why_same_meaning": "the same thing",
        }
    ]
)


class Agent:
    """Stands in for the SDK's `query`.

    `respond` maps a checklist prompt, and `respond_redline` a redline prompt, to the result message
    the run ends with. The two agents are told apart by the model each is configured with.
    """

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.in_flight = 0
        self.peak = 0
        self.respond = lambda prompt: result_message(result=CHECKLIST_TEXT)
        self.respond_redline = lambda prompt: result_message(result=REDLINE_JSON)

    async def query(self, *, prompt, options=None, **_ignored):
        with self.lock:
            self.calls.append(SimpleNamespace(prompt=prompt, options=options))
            self.in_flight += 1
            self.peak = max(self.peak, self.in_flight)
        try:
            respond = (
                self.respond_redline if options.model == REDLINE_MODEL else self.respond
            )
            message = respond(prompt)
        finally:
            with self.lock:
                self.in_flight -= 1
        yield message

    @property
    def prompts(self):
        return [call.prompt for call in self.calls]

    @property
    def checklist_calls(self):
        return [call for call in self.calls if call.options.model != REDLINE_MODEL]

    @property
    def redline_calls(self):
        return [call for call in self.calls if call.options.model == REDLINE_MODEL]

    @property
    def checklist_prompts(self):
        return [call.prompt for call in self.checklist_calls]

    @property
    def redline_prompts(self):
        return [call.prompt for call in self.redline_calls]


def failed(**fields) -> ResultMessage:
    return result_message(is_error=True, subtype="error_during_execution", **fields)


@pytest.fixture(autouse=True)
def agent(monkeypatch):
    stand_in = Agent()
    monkeypatch.setattr(agent_loop, "query", stand_in.query)
    return stand_in


@pytest.fixture(autouse=True)
def web(monkeypatch):
    """Every page is live unless a test says otherwise; nothing reaches the network."""
    stand_in = Web()

    def handle(_transport, request):
        return stand_in.handle(request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return stand_in


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """A profile with a distinctive base resume, and its packets directory."""
    profile = copy_example(tmp_path / "profile")
    packets = tmp_path / "packets"
    write_config_toml(profile, CONFIG.format(packets=packets))
    document = docx.Document()
    document.add_paragraph().add_run("Riley Resumeperson").bold = True
    document.add_paragraph(BASE_LINE)
    document.save(profile / "resume.docx")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    monkeypatch.delenv("JSA_GENERATE_WORKERS", raising=False)
    return profile, packets


@pytest.fixture(autouse=True)
def gws(tmp_path, monkeypatch):
    """A stub `gws` whose invocations are logged; returns the log's path."""
    stub = tmp_path / "stub-gws"
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "gws.log"
    monkeypatch.setenv("JSA_GWS_BIN", str(stub))
    monkeypatch.setenv("STUB_LOG", str(log))
    monkeypatch.delenv("STUB_FAIL_IDS", raising=False)
    monkeypatch.delenv("STUB_FAIL_MODE", raising=False)
    return log


@pytest.fixture(autouse=True)
def pandoc(tmp_path, monkeypatch):
    """A stub `pandoc` whose invocations are logged; returns the log's path."""
    return install_pandoc(tmp_path, monkeypatch)


@pytest.fixture
def gdb(db_url):
    """A connection to a database holding no postings but the test's own."""
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


def seed(
    conn,
    *,
    company="Acme Widgets, Inc.",
    title="Staff Engineer",
    decision="Apply",
    tracked=False,
    closed=False,
    jd=JD,
):
    posting_id = db.insert_posting(
        conn,
        company=company,
        title=title,
        url=f"https://careers.example.com/jobs/{uuid.uuid4().hex}",
        search_agent="claude",
    )
    conn.execute(
        "UPDATE postings SET decision = ?, decided_at = ?, added_to_tracker = ?, "
        "closed_at = ?, jd_markdown = ? WHERE id = ?",
        (
            decision,
            LONG_AGO if decision else None,
            int(tracked),
            LONG_AGO if closed else None,
            jd,
            posting_id,
        ),
    )
    return posting_id


def url_of(conn, posting_id) -> str:
    return conn.execute(
        "SELECT url FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()[0]


def column(conn, posting_id, name):
    return conn.execute(
        f"SELECT {name} FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()[0]


def jsa(monkeypatch, capsys, command, *args):
    monkeypatch.setattr(sys, "argv", ["jsa", command, *map(str, args)])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def jsa_generate(monkeypatch, capsys, *args):
    return jsa(monkeypatch, capsys, "generate", *args)


def entries(directory: Path) -> set[str]:
    return {path.name for path in directory.iterdir()} if directory.exists() else set()


def edit_copy(path: Path, text: str) -> None:
    document = docx.Document(str(path))
    document.add_paragraph(text)
    document.save(path)


def numbered(conn, count, **fields):
    return [seed(conn, company=f"Org{n} Labs", **fields) for n in range(count)]


# --- resume rendering (pure, XC-9) --------------------------------------------


def test_rendering_keeps_paragraph_order():
    document = docx.Document()
    for text in ("first", "second", "third"):
        document.add_paragraph(text)
    lines = render_document(document).splitlines()
    assert [line for line in lines if line] == ["first", "second", "third"]


def test_rendering_wraps_a_bold_run_in_double_asterisks_and_leaves_the_rest_plain():
    document = docx.Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("Led ")
    paragraph.add_run("the bold part").bold = True
    paragraph.add_run(" and more")
    document.add_paragraph("entirely plain")
    rendered = render_document(document)
    assert "Led **the bold part** and more" in rendered
    assert "entirely plain" in rendered
    assert "**entirely plain**" not in rendered


def test_rendering_a_fully_bold_paragraph_marks_all_of_it():
    document = docx.Document()
    document.add_paragraph().add_run("EXPERIENCE").bold = True
    assert "**EXPERIENCE**" in render_document(document)


def test_rendering_a_file_matches_rendering_the_loaded_document(tmp_path):
    document = docx.Document()
    document.add_paragraph().add_run("Heading").bold = True
    document.add_paragraph("Body text.")
    path = tmp_path / "r.docx"
    document.save(path)
    assert render_resume(path) == render_document(docx.Document(str(path)))


# --- the happy path -----------------------------------------------------------


def test_an_open_apply_posting_gets_a_full_packet_and_a_tracker_row(
    gdb, env, gws, monkeypatch, capsys
):
    profile, packets = env
    posting_id = seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    folder = packets / PLAIN
    assert entries(folder) == {
        "job_posting.md",
        RESUME_COPY,
        "resume_checklist.md",
        PDF,
        REDLINE_EDITS,
    }
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == JD
    assert (folder / RESUME_COPY).read_bytes() == (profile / "resume.docx").read_bytes()
    checklist = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert checklist.strip() == CHECKLIST_TEXT.strip()
    assert [row[0] for row in appended_rows(gws)] == [posting_id]
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_a_second_run_finds_nothing_to_do(gdb, agent, gws, monkeypatch, capsys):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.calls == []
    assert len(calls(gws)) == 1


def test_the_tracker_row_is_what_jsa_track_would_have_written(
    gdb, gws, tmp_path, monkeypatch, capsys
):
    first = seed(gdb, company="Beta Labs")
    second = seed(gdb, company="Gamma Labs")
    jsa_generate(monkeypatch, capsys, "--id", first)
    generated = appended_rows(gws)
    gws.unlink()
    jsa(monkeypatch, capsys, "track", "--id", second)
    tracked = appended_rows(gws)
    assert len(generated) == len(tracked) == 1
    # Same A:H shape: the ID, company, title, URL, then the blank user-owned cells.
    assert len(generated[0]) == len(tracked[0])
    assert generated[0][2] == tracked[0][2] == "Staff Engineer"
    assert generated[0][6:] == tracked[0][6:]


@pytest.mark.parametrize(
    "state",
    [{"decision": "Skip"}, {"decision": None}, {"tracked": True}, {"closed": True}],
    ids=["skip", "undecided", "tracked", "closed"],
)
def test_only_apply_untracked_open_postings_are_queued(
    gdb, env, agent, state, monkeypatch, capsys
):
    _, packets = env
    seed(gdb, **state)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert entries(packets) == set()
    assert agent.calls == []


def test_an_id_for_a_non_apply_posting_builds_nothing(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb, decision="Skip")
    jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert entries(packets) == set()
    assert agent.calls == [] and not gws.exists()


def test_each_row_is_built_and_tracked_with_its_own_checklist(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    ids = [seed(gdb, company="Beta Labs"), seed(gdb, company="Gamma Labs")]
    agent.respond = lambda prompt: result_message(
        result="for Beta" if "Beta Labs" in prompt else "for Gamma"
    )
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    beta = packets / "Beta Labs - Staff Engineer" / "resume_checklist.md"
    gamma = packets / "Gamma Labs - Staff Engineer" / "resume_checklist.md"
    assert beta.read_text(encoding="utf-8").strip() == "for Beta"
    assert gamma.read_text(encoding="utf-8").strip() == "for Gamma"
    assert all(column(gdb, i, "added_to_tracker") == 1 for i in ids)
    assert len(calls(gws)) == 2


def test_two_postings_sharing_a_name_get_separate_packets(
    gdb, env, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    second = seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert entries(packets) == {PLAIN, f"{PLAIN} ({second})"}
    for name in entries(packets):
        assert "resume_checklist.md" in entries(packets / name)


# --- the liveness re-check ----------------------------------------------------


def test_a_posting_found_closed_is_marked_closed_and_nothing_is_built(
    gdb, env, web, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    web.gone(url_of(gdb, posting_id))
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert column(gdb, posting_id, "closed_at") is not None
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert entries(packets) == set()
    assert agent.calls == []
    assert not gws.exists()


def test_the_report_counts_the_postings_that_closed(gdb, env, web, monkeypatch, capsys):
    _, packets = env
    dead = [seed(gdb, company="Dead One"), seed(gdb, company="Dead Two")]
    seed(gdb, company="Alive Labs")
    for posting_id in dead:
        web.gone(url_of(gdb, posting_id))
    code, output = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert re.search(r"\b2\b[^\n]*clos", output), output
    assert entries(packets) == {"Alive Labs - Staff Engineer"}


def test_the_closed_count_is_reported_even_when_it_is_zero(gdb, monkeypatch, capsys):
    seed(gdb)
    _, output = jsa_generate(monkeypatch, capsys)
    assert re.search(r"\b0\b[^\n]*clos", output), output


def test_an_unreachable_posting_is_not_closed_and_is_still_built(
    gdb, env, web, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    web.blocked(url_of(gdb, posting_id))
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert column(gdb, posting_id, "closed_at") is None
    assert "resume_checklist.md" in entries(packets / PLAIN)


def test_an_id_builds_a_closed_posting_anyway(gdb, env, web, gws, monkeypatch, capsys):
    _, packets = env
    posting_id = seed(gdb, closed=True)
    web.gone(url_of(gdb, posting_id))
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert "resume_checklist.md" in entries(packets / PLAIN)
    assert len(calls(gws)) == 1
    assert column(gdb, posting_id, "added_to_tracker") == 1


# --- re-entering a packet -----------------------------------------------------


def test_a_bare_directory_left_by_jsa_packet_is_completed_not_skipped(
    gdb, env, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    jsa(monkeypatch, capsys, "packet")
    assert "resume_checklist.md" not in entries(packets / PLAIN)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert entries(packets / PLAIN) == {
        "job_posting.md",
        RESUME_COPY,
        "resume_checklist.md",
        PDF,
        REDLINE_EDITS,
    }
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_an_empty_directory_is_completed_with_the_job_posting_and_resume_copy(
    gdb, env, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    (packets / PLAIN).mkdir(parents=True)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert entries(packets / PLAIN) == {
        "job_posting.md",
        RESUME_COPY,
        "resume_checklist.md",
        PDF,
        REDLINE_EDITS,
    }


def test_the_queue_path_keeps_an_existing_checklist_and_goes_on_to_track(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "resume_checklist.md").write_text("MY OLD CHECKLIST", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert (folder / "resume_checklist.md").read_text(encoding="utf-8") == (
        "MY OLD CHECKLIST"
    )
    assert agent.checklist_calls == []
    assert len(agent.redline_calls) == 1
    assert len(calls(gws)) == 1
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_an_id_rewrites_an_existing_checklist(gdb, env, agent, monkeypatch, capsys):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "resume_checklist.md").write_text("MY OLD CHECKLIST", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert len(agent.checklist_calls) == 1
    text = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert text.strip() == CHECKLIST_TEXT.strip()


def test_an_id_on_a_tracked_posting_rewrites_the_checklist_but_adds_no_second_row(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb, tracked=True)
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert "resume_checklist.md" in entries(packets / PLAIN)
    assert not gws.exists()


# --- job_posting.md -----------------------------------------------------------


def test_the_queue_path_never_overwrites_an_existing_job_posting(
    gdb, env, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text("hand edited", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == "hand edited"


def test_an_id_overwrites_the_job_posting_with_the_stored_description(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text("stale text", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == JD
    assert "Frobnicator" in agent.checklist_prompts[0]
    assert "stale text" not in agent.checklist_prompts[0]


def test_an_id_on_a_null_description_never_overwrites_a_hand_filled_file(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb, jd=None)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text(
        "Zebrafish wrangler duties", encoding="utf-8"
    )
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == (
        "Zebrafish wrangler duties"
    )
    assert "Zebrafish wrangler duties" in agent.checklist_prompts[0]


# --- never review blind -------------------------------------------------------


def test_a_null_jd_without_a_job_posting_gets_its_head_only_and_stays_queued(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb, jd=None)
    _, output = jsa_generate(monkeypatch, capsys)
    assert entries(packets / PLAIN) == {RESUME_COPY}
    assert agent.calls == []
    assert not gws.exists()
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert str(posting_id) in output


def test_a_null_jd_flagged_on_its_own_leaves_the_exit_status_zero(
    gdb, monkeypatch, capsys
):
    seed(gdb, jd=None)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0


def test_a_hand_filled_job_posting_is_the_jd_and_is_left_unchanged(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb, jd=None)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text(
        "Zebrafish wrangler duties", encoding="utf-8"
    )
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert "Zebrafish wrangler duties" in agent.checklist_prompts[0]
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == (
        "Zebrafish wrangler duties"
    )
    assert "resume_checklist.md" in entries(folder)
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_a_blind_row_does_not_stop_the_others_and_is_not_a_failure(
    gdb, env, gws, monkeypatch, capsys
):
    _, packets = env
    blind = seed(gdb, company="Blind Labs", jd=None)
    sighted = seed(gdb, company="Sighted Labs")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert "resume_checklist.md" not in entries(packets / "Blind Labs - Staff Engineer")
    assert column(gdb, blind, "added_to_tracker") == 0
    assert column(gdb, sighted, "added_to_tracker") == 1


# --- what the agent is given --------------------------------------------------


def test_the_prompt_holds_the_posting_and_the_rendered_resume_and_no_markers(
    gdb, agent, monkeypatch, capsys
):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (prompt,) = agent.checklist_prompts
    assert "Staff Engineer" in prompt
    assert "Acme" in prompt
    assert "Build the Frobnicator platform." in prompt
    assert "**Riley Resumeperson**" in prompt
    assert BASE_LINE in prompt
    assert "{{" not in prompt and "}}" not in prompt


def test_the_prompt_carries_no_other_profile_content(
    gdb, env, agent, monkeypatch, capsys
):
    profile, _ = env
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (prompt,) = agent.checklist_prompts
    for path in (profile / "search").glob("*.md"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if len(line.strip()) > 30:
                assert line.strip() not in prompt, path.name
    for private in ("Pat Example", "Jordan Example", "sheet-123"):
        assert private not in prompt


def test_the_shipped_template_has_exactly_the_four_documented_slots():
    template = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "jsa"
        / "templates"
        / "checklist.md"
    ).read_text(encoding="utf-8")
    assert set(re.findall(r"\{\{(\w+)\}\}", template)) == {
        "JOB_TITLE",
        "COMPANY",
        "JOB_DESCRIPTION",
        "RESUME",
    }


def test_the_shared_loop_is_called_with_the_checklist_settings_no_tools_and_one_turn(
    gdb, agent, monkeypatch, capsys
):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (call,) = agent.checklist_calls
    assert call.options.model == "claude-sonnet-5-5"
    assert call.options.effort == "low"
    assert call.options.tools == []
    assert not call.options.allowed_tools
    assert call.options.max_turns == 1


def test_the_checklist_run_loads_no_settings_files(gdb, agent, monkeypatch, capsys):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (call,) = agent.checklist_calls
    assert call.options.setting_sources == []


def test_the_agent_is_given_the_edited_copy_on_an_id_not_the_base(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    jsa_generate(monkeypatch, capsys)
    edit_copy(packets / PLAIN / RESUME_COPY, "REVISED: led the Zorblax migration")
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    (prompt,) = agent.checklist_prompts
    assert "REVISED: led the Zorblax migration" in prompt
    assert BASE_LINE in prompt


def test_the_base_resume_changing_later_does_not_reach_an_existing_copys_checklist(
    gdb, env, agent, monkeypatch, capsys
):
    profile, _ = env
    posting_id = seed(gdb)
    jsa_generate(monkeypatch, capsys)
    document = docx.Document()
    document.add_paragraph("BASE ONLY: a different base resume entirely")
    document.save(profile / "resume.docx")
    agent.calls.clear()
    jsa_generate(monkeypatch, capsys, "--id", posting_id)
    (prompt,) = agent.checklist_prompts
    assert "BASE ONLY" not in prompt
    assert BASE_LINE in prompt


# --- the resume copy ----------------------------------------------------------


def test_an_existing_resume_copy_is_never_modified(gdb, env, monkeypatch, capsys):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / RESUME_COPY).write_bytes(b"not even a docx, just my revision")
    jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert (folder / RESUME_COPY).read_bytes() == b"not even a docx, just my revision"


def test_a_deleted_resume_copy_is_restored_from_the_base_before_assessing(
    gdb, env, agent, monkeypatch, capsys
):
    profile, packets = env
    seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text(JD, encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert (folder / RESUME_COPY).read_bytes() == (profile / "resume.docx").read_bytes()
    assert BASE_LINE in agent.checklist_prompts[0]


# --- failures -----------------------------------------------------------------


def test_an_api_error_result_flags_the_row_with_its_status_and_exits_non_zero(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    agent.respond = lambda prompt: failed(api_error_status=529, result="overloaded")
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert "529" in output
    assert "resume_checklist.md" not in entries(packets / PLAIN)
    assert not gws.exists()
    assert column(gdb, posting_id, "added_to_tracker") == 0


@pytest.mark.parametrize("text", ["", "   \n"], ids=["empty", "blank"])
def test_an_empty_result_flags_the_row_and_exits_non_zero(
    gdb, env, agent, gws, text, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    agent.respond = lambda prompt: result_message(result=text)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert not (packets / PLAIN / "resume_checklist.md").exists()
    assert not gws.exists()
    assert column(gdb, posting_id, "added_to_tracker") == 0


def test_one_failed_row_does_not_stop_the_rest_but_the_exit_is_non_zero(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    bad = seed(gdb, company="Bad Labs")
    good = seed(gdb, company="Good Labs")
    agent.respond = lambda prompt: (
        failed(api_error_status=500)
        if "Bad Labs" in prompt
        else result_message(result="ok")
    )
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(bad) in output
    assert column(gdb, good, "added_to_tracker") == 1
    assert column(gdb, bad, "added_to_tracker") == 0
    assert "resume_checklist.md" in entries(packets / "Good Labs - Staff Engineer")


@pytest.mark.parametrize("mode", ["exit", "garbage", "no-rows"])
def test_a_failed_track_exits_non_zero_leaves_the_row_untracked_and_rolls_nothing_back(
    gdb, env, gws, mode, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    monkeypatch.setenv("STUB_FAIL_IDS", str(posting_id))
    monkeypatch.setenv("STUB_FAIL_MODE", mode)
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(posting_id) in output
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert entries(packets / PLAIN) == {
        "job_posting.md",
        RESUME_COPY,
        "resume_checklist.md",
        PDF,
        REDLINE_EDITS,
    }


def test_a_failed_track_leaves_the_checklist_so_a_rerun_resumes_at_the_append(
    gdb, env, agent, gws, monkeypatch, capsys
):
    posting_id = seed(gdb)
    monkeypatch.setenv("STUB_FAIL_IDS", str(posting_id))
    jsa_generate(monkeypatch, capsys)
    monkeypatch.delenv("STUB_FAIL_IDS")
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.calls == []
    assert column(gdb, posting_id, "added_to_tracker") == 1


# --- concurrency --------------------------------------------------------------


def test_three_checklist_runs_are_in_flight_together_by_default(
    gdb, agent, monkeypatch, capsys
):
    numbered(gdb, 3)
    barrier = threading.Barrier(3, timeout=20)

    def respond(prompt):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            return failed(result="the other runs never started")
        return result_message(result="ok")

    agent.respond = respond
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.peak == 3


@pytest.mark.parametrize("workers", [None, "1", "2"], ids=["default", "one", "two"])
def test_no_more_than_the_configured_number_of_runs_are_in_flight(
    gdb, agent, workers, monkeypatch, capsys
):
    if workers:
        monkeypatch.setenv("JSA_GENERATE_WORKERS", workers)
    limit = int(workers or 3)
    ids = numbered(gdb, 7)

    def respond(prompt):
        time.sleep(0.1)
        return result_message(result="ok")

    agent.respond = respond
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert len(agent.checklist_calls) == 7
    assert len(agent.redline_calls) == 7
    assert len(agent.calls) == 14
    assert agent.peak <= limit
    assert all(column(gdb, i, "added_to_tracker") == 1 for i in ids)


def test_tracker_appends_never_overlap(gdb, agent, tmp_path, monkeypatch, capsys):
    numbered(gdb, 3)
    log = tmp_path / "overlap.log"
    stub = tmp_path / "slow-gws"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, sys, time\n"
        f"log = open({str(log)!r}, 'a')\n"
        "log.write('start\\n'); log.flush()\n"
        "time.sleep(0.3)\n"
        "log.write('end\\n'); log.flush()\n"
        "print(json.dumps({'updates': {'updatedRows': 1}}))\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("JSA_GWS_BIN", str(stub))
    barrier = threading.Barrier(3, timeout=20)

    def respond(prompt):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return result_message(result="ok")

    agent.respond = respond
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert log.read_text().split() == ["start", "end"] * 3


def test_each_finished_row_is_appended_right_away_not_at_the_end(
    gdb, agent, gws, monkeypatch, capsys
):
    fast = seed(gdb, company="FastLabs")
    slow = seed(gdb, company="SlowLabs")

    def respond(prompt):
        if "SlowLabs" in prompt:
            deadline = time.monotonic() + 30
            while not gws.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            if not gws.exists():
                return failed(result="the fast row was never appended")
        return result_message(result="ok")

    agent.respond = respond
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert column(gdb, fast, "added_to_tracker") == 1
    assert column(gdb, slow, "added_to_tracker") == 1
    assert appended_rows(gws)[0][0] == fast


# --- resume_checklist.pdf (issue #77) -----------------------------------------


def test_the_checklist_is_rendered_to_a_pdf_with_pandoc_and_typst(
    gdb, env, pandoc, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    folder = packets / PLAIN
    [argv] = pandoc_calls(pandoc)
    assert argv[0] == str(folder / "resume_checklist.md")
    assert argv[argv.index("-o") + 1] == str(folder / PDF)
    assert "--pdf-engine=typst" in argv
    checklist = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert (folder / PDF).read_text(encoding="utf-8") == f"PDF OF: {checklist}"


def test_the_pandoc_binary_is_the_one_named_by_jsa_pandoc_bin(
    gdb, env, pandoc, tmp_path, monkeypatch, capsys
):
    _, packets = env
    other = tmp_path / "other-pandoc"
    other.write_text(
        f"#!{sys.executable}\nimport sys\nopen(sys.argv[sys.argv.index('-o') + 1], 'w')"
        ".write('FROM OTHER')\n",
        encoding="utf-8",
    )
    other.chmod(0o755)
    monkeypatch.setenv("JSA_PANDOC_BIN", str(other))
    seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert (packets / PLAIN / PDF).read_text(encoding="utf-8") == "FROM OTHER"
    assert pandoc_calls(pandoc) == []


def test_pandoc_is_found_on_the_path_when_jsa_pandoc_bin_is_unset(
    gdb, env, pandoc, tmp_path, monkeypatch, capsys
):
    _, packets = env
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pandoc").symlink_to(tmp_path / "stub-pandoc")
    monkeypatch.delenv("JSA_PANDOC_BIN")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    folder = packets / PLAIN
    [argv] = pandoc_calls(pandoc)
    assert argv[0] == str(folder / "resume_checklist.md")
    assert argv[argv.index("-o") + 1] == str(folder / PDF)
    assert "--pdf-engine=typst" in argv


def test_a_pandoc_failure_flags_the_row_and_the_other_rows_continue(
    gdb, env, pandoc, gws, tmp_path, monkeypatch, capsys
):
    _, packets = env
    bad = seed(gdb, company="Bad Labs")
    good = seed(gdb, company="Good Labs")
    flaky = tmp_path / "flaky-pandoc"
    flaky.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "text = open(sys.argv[1], encoding='utf-8').read()\n"
        "if 'Bad Labs' in sys.argv[1]:\n"
        "    print('typst: boom', file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "open(sys.argv[sys.argv.index('-o') + 1], 'w').write(text)\n",
        encoding="utf-8",
    )
    flaky.chmod(0o755)
    monkeypatch.setenv("JSA_PANDOC_BIN", str(flaky))
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(bad) in output
    assert column(gdb, bad, "added_to_tracker") == 0
    assert column(gdb, good, "added_to_tracker") == 1
    assert PDF in entries(packets / "Good Labs - Staff Engineer")
    assert PDF not in entries(packets / "Bad Labs - Staff Engineer")
    assert [row[0] for row in appended_rows(gws)] == [good]


def test_a_missing_pandoc_flags_the_row_like_any_failed_step(
    gdb, env, gws, tmp_path, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    monkeypatch.setenv("JSA_PANDOC_BIN", str(tmp_path / "no-such-pandoc"))
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(posting_id) in output
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert not gws.exists()
    assert PDF not in entries(packets / PLAIN)


def test_a_rerun_after_a_pandoc_failure_renders_the_pdf_without_rewriting_the_checklist(
    gdb, env, agent, pandoc, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    monkeypatch.setenv("PANDOC_FAIL", "1")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code != 0
    folder = packets / PLAIN
    assert (folder / "resume_checklist.md").exists()
    assert not (folder / PDF).exists()
    monkeypatch.delenv("PANDOC_FAIL")
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.checklist_calls == []
    checklist = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert checklist.strip() == CHECKLIST_TEXT.strip()
    assert (folder / PDF).read_text(encoding="utf-8") == f"PDF OF: {checklist}"
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_a_checklist_without_a_pdf_gets_one_and_the_checklist_is_left_alone(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "resume_checklist.md").write_text("MY OLD CHECKLIST", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.checklist_calls == []
    assert len(agent.redline_calls) == 1
    assert (folder / "resume_checklist.md").read_text(encoding="utf-8") == (
        "MY OLD CHECKLIST"
    )
    assert (folder / PDF).read_text(encoding="utf-8") == "PDF OF: MY OLD CHECKLIST"


def test_an_existing_pdf_is_not_rendered_again_on_a_rerun(
    gdb, pandoc, monkeypatch, capsys
):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    assert len(pandoc_calls(pandoc)) == 1
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert len(pandoc_calls(pandoc)) == 1


def test_an_id_that_rewrites_the_checklist_renders_its_pdf_again(
    gdb, env, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "resume_checklist.md").write_text("MY OLD CHECKLIST", encoding="utf-8")
    (folder / PDF).write_text("STALE PDF", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    checklist = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert checklist.strip() == CHECKLIST_TEXT.strip()
    assert (folder / PDF).read_text(encoding="utf-8") == f"PDF OF: {checklist}"


def test_a_failed_checklist_run_renders_no_pdf(
    gdb, env, agent, pandoc, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    agent.respond = lambda prompt: failed(result="overloaded")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert pandoc_calls(pandoc) == []
    assert PDF not in entries(packets / PLAIN)


def test_a_dry_run_renders_no_pdf(gdb, pandoc, monkeypatch, capsys):
    seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert pandoc_calls(pandoc) == []


# --- --dry-run ----------------------------------------------------------------


def test_a_dry_run_names_the_folders_and_does_nothing_else(
    gdb, env, web, agent, gws, monkeypatch, capsys
):
    _, packets = env
    open_id = seed(gdb, company="Open Labs")
    dead_id = seed(gdb, company="Dead Labs")
    web.gone(url_of(gdb, dead_id))
    code, output = jsa_generate(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "Open Labs - Staff Engineer" in output
    assert agent.calls == []
    assert not gws.exists()
    assert not packets.exists() or entries(packets) == set()
    for posting_id in (open_id, dead_id):
        assert column(gdb, posting_id, "added_to_tracker") == 0
        assert column(gdb, posting_id, "closed_at") is None


def test_a_dry_run_with_an_id_writes_nothing_even_to_an_existing_packet(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "job_posting.md").write_text("stale text", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys, "--dry-run", "--id", posting_id)
    assert code == 0
    assert entries(folder) == {"job_posting.md"}
    assert (folder / "job_posting.md").read_text(encoding="utf-8") == "stale text"
    assert agent.calls == []


# --- requiredness -------------------------------------------------------------


def _break_resume(profile, state):
    if state == "missing":
        (profile / "resume.docx").unlink()
    else:
        (profile / "resume.docx").write_bytes(b"")


def _break_tracker_id(profile, state):
    packets = profile.parent / "packets"
    text = CONFIG.format(packets=packets)
    if state == "missing":
        text = text.replace('tracker_spreadsheet_id = "sheet-123"\n', "")
    else:
        text = text.replace("sheet-123", "")
    write_config_toml(profile, text)


def _break_checklist_table(profile, state):
    packets = profile.parent / "packets"
    write_config_toml(
        profile, CONFIG.format(packets=packets).split("[agents.checklist]")[0]
    )


def _break_redline_table(profile, state):
    packets = profile.parent / "packets"
    write_config_toml(
        profile, CONFIG.format(packets=packets).split("[agents.redline]")[0]
    )


@pytest.mark.parametrize(
    "breaker, state",
    [
        (_break_resume, "missing"),
        (_break_resume, "empty"),
        (_break_tracker_id, "missing"),
        (_break_tracker_id, "empty"),
        (_break_checklist_table, "missing"),
        (_break_redline_table, "missing"),
    ],
    ids=[
        "resume-missing",
        "resume-empty",
        "tracker-id-missing",
        "tracker-id-empty",
        "checklist-agent-missing",
        "redline-agent-missing",
    ],
)
def test_a_missing_requirement_fails_before_any_row_is_processed(
    gdb, env, web, agent, gws, breaker, state, monkeypatch, capsys
):
    profile, packets = env
    posting_id = seed(gdb)
    breaker(profile, state)
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert "profile.example" in output
    assert entries(packets) == set()
    assert agent.calls == []
    assert not gws.exists()
    assert web.requests == []
    assert column(gdb, posting_id, "closed_at") is None
    assert column(gdb, posting_id, "added_to_tracker") == 0


def test_the_requirements_apply_even_when_the_queue_is_empty(
    gdb, env, monkeypatch, capsys
):
    profile, _ = env
    (profile / "resume.docx").unlink()
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert "profile.example" in output


def test_the_command_is_registered_with_id_and_dry_run(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jsa", "generate", "--help"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    output = capsys.readouterr().out
    assert "--id" in output and "--dry-run" in output


# --- the ATS redline (issue #82; PRD 04 "ATS redline") ------------------------

QUUX_JD = "# Staff Engineer\n\nOwn the Quuxlate system end to end.\n"
QUUX_EDIT = {
    "paragraph": 1,
    "find": "Quuxlate platform",
    "replace": "Quuxlate system",
    "jd_quote": "Quuxlate system",
    "why_same_meaning": "WHY-SAME-MEANING-MARKER",
}
REDLINE_DOC = RESUME_COPY.replace(".docx", "_redline.docx")


def redline_reply(*edits):
    return lambda prompt: result_message(result=json.dumps(list(edits)))


def proposed(folder):
    return json.loads((folder / REDLINE_EDITS).read_text(encoding="utf-8"))


def test_each_row_gets_a_checklist_then_a_redline_before_its_tracker_append(
    gdb, agent, gws, monkeypatch, capsys
):
    seed(gdb)
    log_at_redline = []

    def respond_redline(prompt):
        log_at_redline.append(gws.exists())
        return result_message(result=REDLINE_JSON)

    agent.respond_redline = respond_redline
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert [call.options.model for call in agent.calls] == [
        "claude-sonnet-5-5",
        REDLINE_MODEL,
    ]
    assert log_at_redline == [False]
    assert gws.exists()


def test_the_redline_run_uses_the_redline_settings_no_tools_one_turn_and_no_settings_files(
    gdb, agent, monkeypatch, capsys
):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (call,) = agent.redline_calls
    assert call.options.model == REDLINE_MODEL
    assert call.options.effort == "medium"
    assert call.options.tools == []
    assert not call.options.allowed_tools
    assert call.options.max_turns == 1
    assert call.options.setting_sources == []


def test_the_redline_prompt_holds_the_posting_and_the_indexed_resume_paragraphs(
    gdb, agent, monkeypatch, capsys
):
    seed(gdb)
    jsa_generate(monkeypatch, capsys)
    (prompt,) = agent.redline_prompts
    assert "Build the Frobnicator platform." in prompt
    assert "Riley Resumeperson" in prompt
    assert BASE_LINE in prompt
    assert "{{" not in prompt and "}}" not in prompt
    for private in ("Pat Example", "Jordan Example", "sheet-123"):
        assert private not in prompt


def test_the_redline_and_the_checklist_do_not_see_each_others_output(
    gdb, agent, monkeypatch, capsys
):
    seed(gdb)
    agent.respond = lambda prompt: result_message(result="CHECKLIST-ONLY-MARKER")
    agent.respond_redline = lambda prompt: result_message(
        result=REDLINE_JSON.replace("nothing here", "REDLINE-ONLY-MARKER")
    )
    jsa_generate(monkeypatch, capsys)
    assert "REDLINE-ONLY-MARKER" not in agent.checklist_prompts[0]
    assert "CHECKLIST-ONLY-MARKER" not in agent.redline_prompts[0]


def test_every_proposed_edit_is_recorded_with_its_validation_result(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    agent.respond_redline = redline_reply(
        {**QUUX_EDIT, "paragraph": 99}, {**QUUX_EDIT, "why_same_meaning": ""}
    )
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    records = proposed(packets / PLAIN)
    assert len(records) == 2
    assert all(record["validation"] for record in records)
    assert not (packets / PLAIN / REDLINE_DOC).exists()


def test_a_run_with_no_valid_edit_writes_the_record_but_no_redline_document(
    gdb, env, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert REDLINE_EDITS in entries(packets / PLAIN)
    assert REDLINE_DOC not in entries(packets / PLAIN)
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_a_valid_edit_becomes_a_tracked_change_with_a_comment_in_a_copy(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    seed(gdb, jd=QUUX_JD)
    agent.respond_redline = redline_reply(QUUX_EDIT)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    folder = packets / PLAIN
    (record,) = proposed(folder)
    assert record["validation"] is None
    assert REDLINE_DOC in entries(folder)
    with zipfile.ZipFile(folder / REDLINE_DOC) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "<w:ins " in body and "<w:del " in body
    assert "Claude (ATS)" in body
    assert "Quuxlate system" in comments and "WHY-SAME-MEANING-MARKER" in comments


def test_the_redline_never_touches_the_resume_copy(
    gdb, env, agent, monkeypatch, capsys
):
    profile, packets = env
    seed(gdb, jd=QUUX_JD)
    agent.respond_redline = redline_reply(QUUX_EDIT)
    jsa_generate(monkeypatch, capsys)
    copy = packets / PLAIN / RESUME_COPY
    assert copy.read_bytes() == (profile / "resume.docx").read_bytes()
    assert (profile / "resume.docx").read_bytes() == copy.read_bytes()


@pytest.mark.parametrize(
    "text",
    ["not json at all", '{"paragraph": 1}', "", "   \n"],
    ids=["prose", "object", "empty", "blank"],
)
def test_a_redline_result_that_is_not_an_edit_array_flags_the_row_and_exits_non_zero(
    gdb, env, agent, gws, text, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    agent.respond_redline = lambda prompt: result_message(result=text)
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(posting_id) in output
    assert not gws.exists()
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert REDLINE_EDITS not in entries(packets / PLAIN)


def test_a_failed_redline_run_flags_the_row_and_keeps_the_checklist(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    agent.respond_redline = lambda prompt: failed(
        api_error_status=529, result="overloaded"
    )
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert "529" in output
    assert not gws.exists()
    assert column(gdb, posting_id, "added_to_tracker") == 0
    assert "resume_checklist.md" in entries(packets / PLAIN)


def test_a_failed_redline_on_one_row_does_not_stop_the_others(
    gdb, env, agent, gws, monkeypatch, capsys
):
    bad = seed(gdb, company="Bad Labs", jd="# Role\n\nBAD-JD-MARKER\n")
    good = seed(gdb, company="Good Labs")
    agent.respond_redline = lambda prompt: (
        failed(result="overloaded")
        if "BAD-JD-MARKER" in prompt
        else result_message(result=REDLINE_JSON)
    )
    code, output = jsa_generate(monkeypatch, capsys)
    assert code != 0
    assert str(bad) in output
    assert column(gdb, bad, "added_to_tracker") == 0
    assert column(gdb, good, "added_to_tracker") == 1


def test_a_rerun_after_a_failed_redline_writes_no_second_checklist_and_resumes(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    agent.respond_redline = lambda prompt: result_message(result="nope")
    jsa_generate(monkeypatch, capsys)
    agent.respond_redline = lambda prompt: result_message(result=REDLINE_JSON)
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.checklist_calls == []
    assert len(agent.redline_calls) == 1
    assert REDLINE_EDITS in entries(packets / PLAIN)
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_the_queue_path_skips_the_redline_when_the_record_exists(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / "resume_checklist.md").write_text("MY OLD CHECKLIST", encoding="utf-8")
    (folder / REDLINE_EDITS).write_text("[]", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.calls == []
    assert column(gdb, posting_id, "added_to_tracker") == 1


def test_an_id_reruns_both_steps_when_no_redline_document_exists(
    gdb, agent, monkeypatch, capsys
):
    posting_id = seed(gdb)
    jsa_generate(monkeypatch, capsys)
    agent.calls.clear()
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert len(agent.checklist_calls) == 1
    assert len(agent.redline_calls) == 1


def test_an_id_leaves_an_existing_redline_and_its_record_alone_but_rewrites_the_checklist(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    (folder / REDLINE_DOC).write_bytes(b"MY REVIEW IN PROGRESS")
    (folder / REDLINE_EDITS).write_text("MY RECORD", encoding="utf-8")
    code, _ = jsa_generate(monkeypatch, capsys, "--id", posting_id)
    assert code == 0
    assert len(agent.checklist_calls) == 1
    assert agent.redline_calls == []
    assert (folder / REDLINE_DOC).read_bytes() == b"MY REVIEW IN PROGRESS"
    assert (folder / REDLINE_EDITS).read_text(encoding="utf-8") == "MY RECORD"
    checklist = (folder / "resume_checklist.md").read_text(encoding="utf-8")
    assert checklist.strip() == CHECKLIST_TEXT.strip()


def test_the_redline_reads_the_resume_copy_as_it_stands_on_an_id(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    jsa_generate(monkeypatch, capsys)
    edit_copy(packets / PLAIN / RESUME_COPY, "REVISED: led the Zorblax migration")
    agent.calls.clear()
    jsa_generate(monkeypatch, capsys, "--id", posting_id)
    (prompt,) = agent.redline_prompts
    assert "REVISED: led the Zorblax migration" in prompt


def test_a_blind_row_gets_no_redline(gdb, env, agent, monkeypatch, capsys):
    _, packets = env
    seed(gdb, jd=None)
    code, _ = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert agent.calls == []
    assert REDLINE_EDITS not in entries(packets / "Acme Widgets - Staff Engineer")


def test_jsa_packet_writes_no_redline_and_makes_no_model_call(
    gdb, env, agent, monkeypatch, capsys
):
    _, packets = env
    seed(gdb)
    jsa(monkeypatch, capsys, "packet")
    assert agent.calls == []
    assert REDLINE_EDITS not in entries(packets / PLAIN)
    assert REDLINE_DOC not in entries(packets / PLAIN)


def test_a_dry_run_makes_no_redline_call(gdb, env, agent, monkeypatch, capsys):
    _, packets = env
    seed(gdb)
    code, _ = jsa_generate(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert agent.calls == []
    assert entries(packets) == set()


def test_a_resume_copy_with_unresolved_tracked_changes_is_not_redlined_and_the_row_is_tracked(
    gdb, env, agent, gws, monkeypatch, capsys
):
    _, packets = env
    posting_id = seed(gdb)
    folder = packets / PLAIN
    folder.mkdir(parents=True)
    copy = folder / RESUME_COPY
    document = docx.Document()
    paragraph = document.add_paragraph("Built the ")
    tracked = parse_xml(
        '<w:ins xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        ' w:id="901" w:author="Someone" w:date="2026-01-01T00:00:00Z">'
        "<w:r><w:t>Quuxlate platform</w:t></w:r></w:ins>"
    )
    paragraph._p.append(tracked)
    document.save(copy)
    code, output = jsa_generate(monkeypatch, capsys)
    assert code == 0
    assert "warning" in output.lower()
    assert REDLINE_DOC not in entries(folder)
    assert column(gdb, posting_id, "added_to_tracker") == 1
