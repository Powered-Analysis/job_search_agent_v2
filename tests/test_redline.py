"""The ATS redline's validator, edit parsing and tracked-change document (issue #82; PRD 04 "ATS redline").

Validation and parsing are pure. The document tests drive `redline_resume` with Claude replaced at the
shared agent loop (`agent_loop.query`) and read the written .docx back as Word would: accepting or
rejecting every tracked change is computed from the document XML.
"""

import json
import re
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import docx
import pytest
from lxml import etree
from test_claude_runner import result_message

from jsa import agent_loop
from jsa.errors import JsaError
from jsa.profile import AgentSettings
from jsa.redline import (
    Edit,
    SkillsEdit,
    assemble_redline_prompt,
    edits_applied,
    parse_skills,
    redline_resume,
    validate_edits,
    validate_skills_edits,
)
from jsa.redline_docx import skills_section

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
AUTHOR = "Claude (ATS)"
SETTINGS = AgentSettings(model="claude-opus-5-5", effort="medium")

PARAGRAPHS = [
    "Riley Resumeperson",
    "Built the Quuxlate platform from scratch.",
    "Led data work using SQL daily.",
    "Reduced costs by 30% in 2020.",
]
JD = (
    "Own the Quuxlate system end to end.\n\n"
    "Use PostgreSQL and  Machine   Learning (ML) every day.\n"
    "Reduced costs matter. Constructed the Quuxlate system. We value data engineering work."
)


def edit(**overrides) -> Edit:
    fields = {
        "paragraph": 1,
        "find": "Quuxlate platform",
        "replace": "Quuxlate system",
        "jd_quote": "Quuxlate system",
        "why_same_meaning": "the posting's name for the same thing",
    }
    return Edit(**{**fields, **overrides})


def results(*edits: Edit, paragraphs=PARAGRAPHS, jd=JD):
    return validate_edits(list(edits), paragraphs, jd)


# --- validation rules (PRD 04, "Validation") -----------------------------------


def test_an_edit_that_meets_every_rule_passes():
    assert results(edit()) == [None]


def test_each_result_lines_up_with_its_edit_in_proposal_order():
    out = results(edit(paragraph=99), edit(), edit(paragraph=-1))
    assert len(out) == 3
    assert out[0] and out[1] is None and out[2]


@pytest.mark.parametrize("index", [4, 99, -1])
def test_a_paragraph_index_outside_the_resume_is_dropped(index):
    assert results(edit(paragraph=index))[0]


def test_the_last_paragraph_index_is_valid():
    out = results(
        edit(
            paragraph=3,
            find="Reduced costs by",
            replace="Reduced costs",
            jd_quote="Reduced costs",
        )
    )
    assert out == [None]


def test_find_must_occur_in_the_paragraph():
    assert results(edit(find="Quuxlate framework"))[0]


def test_find_is_matched_exactly_not_case_folded():
    assert results(edit(find="quuxlate platform"))[0]


def test_find_that_occurs_twice_in_the_paragraph_is_dropped():
    paragraphs = ["Ran SQL jobs and SQL reports.", *PARAGRAPHS]
    out = results(
        edit(
            paragraph=0,
            find="SQL",
            replace="SQL",
            jd_quote="Use PostgreSQL",
        ),
        paragraphs=paragraphs,
    )
    assert out[0]
    out = results(
        edit(
            paragraph=0,
            find="SQL jobs",
            replace="SQL tasks",
            jd_quote="SQL tasks",
        ),
        paragraphs=paragraphs,
        jd="Run SQL tasks.",
    )
    assert out == [None]


def test_an_edit_overlapping_an_earlier_passing_edit_is_dropped():
    first = edit()
    second = edit(
        find="platform from",
        replace="system from",
        jd_quote="Quuxlate system",
    )
    out = results(first, second)
    assert out[0] is None
    assert out[1]


def test_an_edit_overlapping_an_earlier_dropped_edit_still_passes():
    dropped = edit(why_same_meaning="")
    second = edit()
    out = results(dropped, second)
    assert out[0]
    assert out[1] is None


def test_edits_in_one_paragraph_that_do_not_overlap_both_pass():
    first = edit(
        find="Built", replace="Constructed", jd_quote="Constructed the Quuxlate system"
    )
    second = edit()
    assert results(first, second) == [None, None]


def test_overlap_is_per_paragraph():
    paragraphs = ["Built the Quuxlate platform.", "Built the Quuxlate platform."]
    out = results(edit(paragraph=0), edit(paragraph=1), paragraphs=paragraphs)
    assert out == [None, None]


def test_jd_quote_must_be_a_substring_of_the_job_description():
    assert results(edit(jd_quote="Quuxlate framework"))[0]


def test_jd_quote_is_matched_case_sensitively():
    assert results(edit(jd_quote="quuxlate system"))[0]


def test_whitespace_runs_are_collapsed_in_both_quote_and_job_description():
    # The posting has a double space and line breaks; the quote uses single spaces.
    quote = "PostgreSQL and Machine Learning (ML) every day."
    paragraphs = ["Used PostgreSQL daily."]
    assert results(
        edit(
            paragraph=0,
            find="Used PostgreSQL daily.",
            replace="Used PostgreSQL and Machine Learning (ML) daily.",
            jd_quote=quote,
        ),
        paragraphs=paragraphs,
    ) == [None]
    # A quote spanning a paragraph break in the posting also collapses to one space.
    assert results(
        edit(jd_quote="Own the Quuxlate system end to end. Use PostgreSQL"),
        paragraphs=PARAGRAPHS,
    ) == [None]
    # And whitespace in the quote itself is collapsed.
    assert results(edit(jd_quote="Quuxlate    system\n end"))[0] is None


def test_an_empty_jd_quote_is_dropped_though_empty_text_is_in_every_posting():
    assert results(edit(jd_quote=""))[0]


def test_a_blank_jd_quote_is_dropped():
    assert results(edit(jd_quote="   "))[0]


def test_an_inserted_word_found_in_neither_quote_nor_find_is_dropped():
    assert results(edit(replace="Quuxlate framework"))[0]


def test_an_inserted_word_from_the_quote_passes_and_one_from_find_passes():
    # "Quuxlate" is in find, "system" is in the quote.
    assert results(edit()) == [None]
    # Words moved around within find are not new.
    assert results(
        edit(
            find="Built the Quuxlate platform",
            replace="platform Built the Quuxlate",
            jd_quote="Own the Quuxlate system",
        )
    ) == [None]


def test_word_comparison_ignores_case_and_edge_punctuation():
    # "SYSTEM," carries a capital and a comma; the quote has "system" and ends in a period.
    assert results(edit(replace="Quuxlate (SYSTEM),"))[0] is None


def test_a_word_in_the_posting_but_not_in_the_cited_quote_is_dropped():
    # "PostgreSQL" is in the posting, just not in this edit's quote.
    assert results(edit(replace="Quuxlate PostgreSQL"))[0]


def test_an_empty_replace_is_dropped_as_a_deletion():
    assert results(edit(replace=""))[0]


def test_a_replace_equal_to_find_is_dropped():
    assert results(edit(replace="Quuxlate platform"))[0]


def test_a_replace_that_differs_from_find_only_in_whitespace_is_dropped():
    assert results(edit(replace="Quuxlate  platform"))[0]
    assert results(edit(replace="Quuxlate\tplatform"))[0]
    assert results(edit(replace="Quuxlate platform "))[0]


def test_a_non_empty_replace_may_leave_out_some_of_finds_words():
    out = results(edit(find="Quuxlate platform from", replace="Quuxlate from"))
    assert out == [None]


def test_changing_a_number_is_dropped_even_if_the_new_number_is_in_the_quote():
    jd = "Reduced costs by 40% in 2020."
    base = {
        "paragraph": 3,
        "find": "Reduced costs by 30% in 2020",
        "replace": "Reduced costs by 40% in 2020",
        "jd_quote": "Reduced costs by 40% in 2020",
    }
    assert results(edit(**base), jd=jd)[0]


def test_dropping_a_digit_bearing_word_is_dropped():
    jd = "Cut costs by 30% in 2020."
    assert results(
        edit(
            paragraph=3,
            find="Reduced costs by 30% in 2020",
            replace="Cut costs by 30%",
            jd_quote="Cut costs by 30%",
        ),
        jd=jd,
    )[0]


def test_adding_a_digit_bearing_word_is_dropped():
    jd = "Ship in 2021 or 2022."
    assert results(
        edit(
            paragraph=2,
            find="Led data work",
            replace="Led data work 2021",
            jd_quote="Ship in 2021",
        ),
        jd=jd,
    )[0]


def test_unchanged_numbers_do_not_block_an_edit():
    jd = "Cut costs by 30% in 2020."
    assert results(
        edit(
            paragraph=3,
            find="Reduced costs by 30% in 2020",
            replace="Cut costs by 30% in 2020",
            jd_quote="Cut costs by 30% in 2020",
        ),
        jd=jd,
    ) == [None]


def test_find_of_six_words_passes_and_seven_is_dropped():
    paragraphs = ["One two three four five six seven eight."]
    jd = "Alpha two three four five six."
    six = edit(
        paragraph=0,
        find="One two three four five six",
        replace="Alpha two three four five six",
        jd_quote="Alpha two three four five six",
    )
    seven = edit(
        paragraph=0,
        find="One two three four five six seven",
        replace="Alpha two three four five six seven",
        jd_quote="Alpha two three four five six",
    )
    assert results(six, paragraphs=paragraphs, jd=jd) == [None]
    assert results(seven, paragraphs=paragraphs, jd=jd)[0]


def test_words_are_whitespace_separated_tokens_so_punctuation_does_not_add_to_the_count():
    paragraphs = ["(One), two; three: four! five? six. seven"]
    jd = "Alpha two three four five six."
    out = results(
        edit(
            paragraph=0,
            find="(One), two; three: four! five? six.",
            replace="Alpha two three four five six.",
            jd_quote="Alpha two three four five six.",
        ),
        paragraphs=paragraphs,
        jd=jd,
    )
    assert out == [None]


def test_an_empty_why_same_meaning_is_dropped():
    assert results(edit(why_same_meaning=""))[0]


def test_every_dropped_edit_carries_a_non_empty_reason():
    out = results(
        edit(paragraph=9),
        edit(find="nope"),
        edit(jd_quote="not in the posting"),
        edit(replace="Quuxlate framework"),
        edit(replace=""),
        edit(why_same_meaning=""),
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


# --- the agent's output (PRD 04, "Agent") --------------------------------------


@pytest.fixture
def stand_in(monkeypatch):
    state = SimpleNamespace(reply=reply_object([], EXPLANATION), calls=[])

    async def query(*, prompt, options=None, **_ignored):
        state.calls.append(SimpleNamespace(prompt=prompt, options=options))
        yield result_message(result=state.reply)

    monkeypatch.setattr(agent_loop, "query", query)
    return state


def make_resume(path, build=None):
    document = docx.Document()
    if build is None:
        for text in PARAGRAPHS:
            document.add_paragraph(text)
    else:
        build(document)
    document.save(path)
    return path


EXPLANATION = "The resume already uses the posting's terms."


def reply_object(edits, explanation=None, skills_edits=()):
    return json.dumps(
        {"edits": edits, "skills_edits": list(skills_edits), "explanation": explanation}
    )


def run(tmp_path, stand_in, reply, *, build=None, jd=JD, skills=()):
    """`reply` is raw text, or a list of edit dicts sent as `{edits, skills_edits, explanation}`."""
    if isinstance(reply, list):
        reply = reply_object(reply, None if reply else EXPLANATION)
    stand_in.reply = reply
    copy = make_resume(tmp_path / "resume.docx", build)
    before = copy.read_bytes()
    original = tmp_path / "original.docx"
    original.write_bytes(before)
    edits_file = tmp_path / "redline_edits.json"
    outcome = redline_resume(copy, edits_file, jd, SETTINGS, skills)
    return SimpleNamespace(
        outcome=outcome,
        copy=copy,
        before=before,
        original=original,
        edits_file=edits_file,
    )


def as_dict(e: Edit, **overrides) -> dict:
    return {**e.__dict__, **overrides}


def test_a_no_edit_result_records_the_explanation_and_an_empty_edit_list(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert record == {"explanation": EXPLANATION, "edits": [], "skills_edits": []}
    assert ran.copy.read_bytes() == ran.before
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 0)


def test_a_result_with_edits_records_a_null_explanation(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert set(record) == {"explanation", "edits", "skills_edits"}
    assert record["explanation"] is None
    assert len(record["edits"]) == 1


@pytest.mark.parametrize(
    "reply",
    [
        "",
        "   \n",
        "I found nothing to change.",
        '{"paragraph": 1}',
        '"[]"',
        "null",
        "[]",
        "[1, 2]",
        '["Quuxlate"]',
        "[[]]",
        json.dumps([{"edits": []}]),
        "```json\n" + reply_object([], EXPLANATION) + "\n```",
        "```\n" + reply_object([], EXPLANATION) + "\n```",
        "Here you go: " + reply_object([], EXPLANATION),
        reply_object([], EXPLANATION) + "\nThanks!",
        '{"edits": [], "skills_edits": [], "explanation": "x"',
    ],
    ids=[
        "empty",
        "blank",
        "prose",
        "wrong-object",
        "string",
        "null",
        "bare-empty-array",
        "numbers",
        "strings",
        "nested-empty-array",
        "array-holding-an-object",
        "json-fence",
        "plain-fence",
        "text-before",
        "text-after",
        "truncated",
    ],
)
def test_text_that_is_not_an_edits_and_explanation_object_raises(
    tmp_path, stand_in, reply
):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = reply
    before = copy.read_bytes()
    edits_file = tmp_path / "e.json"
    with pytest.raises(JsaError):
        redline_resume(copy, edits_file, JD, SETTINGS)
    assert not edits_file.exists()
    assert copy.read_bytes() == before


def test_a_bare_array_of_real_edits_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = json.dumps([as_dict(edit())])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


def test_a_fenced_object_of_real_edits_also_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = "```json\n" + reply_object([as_dict(edit())]) + "\n```"
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize(
    "reply",
    [
        reply_object([], None),
        reply_object([], ""),
        json.dumps({"edits": [], "skills_edits": []}),
        reply_object([], 7),
        reply_object([], "  \n "),
        json.dumps({"explanation": EXPLANATION}),
        json.dumps({"edits": None, "skills_edits": [], "explanation": EXPLANATION}),
        json.dumps({"edits": {}, "skills_edits": [], "explanation": EXPLANATION}),
        reply_object([as_dict(edit())], EXPLANATION),
        reply_object([as_dict(edit())], ""),
        json.dumps({"edits": [as_dict(edit())], "skills_edits": []}),
        reply_object([as_dict(edit())], 7),
        json.dumps({"edits": [], "explanation": EXPLANATION}),
        json.dumps({"edits": [as_dict(edit())], "explanation": None}),
        json.dumps({"edits": [], "skills_edits": None, "explanation": EXPLANATION}),
        json.dumps({"edits": [], "skills_edits": {}, "explanation": EXPLANATION}),
    ],
    ids=[
        "no-edits-null-explanation",
        "no-edits-empty-explanation",
        "no-edits-missing-explanation",
        "no-edits-numeric-explanation",
        "no-edits-blank-explanation",
        "missing-edits",
        "null-edits",
        "object-edits",
        "edits-with-an-explanation",
        "edits-with-an-empty-explanation",
        "edits-missing-explanation",
        "edits-with-numeric-explanation",
        "no-skills-edits-key",
        "edits-without-the-skills-edits-key",
        "null-skills-edits",
        "object-skills-edits",
    ],
)
def test_an_object_that_breaks_the_explanation_rule_or_shape_raises(
    tmp_path, stand_in, reply
):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = reply
    before = copy.read_bytes()
    edits_file = tmp_path / "e.json"
    with pytest.raises(JsaError):
        redline_resume(copy, edits_file, JD, SETTINGS)
    assert not edits_file.exists()
    assert copy.read_bytes() == before


def test_the_prompt_tells_the_agent_to_return_edits_and_an_explanation_for_an_empty_redline(
    tmp_path, stand_in
):
    run(tmp_path, stand_in, [])
    (call,) = stand_in.calls
    assert "explanation" in call.prompt
    assert "edits" in call.prompt
    assert "skills_edits" in call.prompt
    assert "null" in call.prompt
    assert "{{" not in call.prompt


def test_an_edit_object_missing_a_field_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    partial = as_dict(edit())
    del partial["why_same_meaning"]
    stand_in.reply = reply_object([partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


def test_the_agent_gets_no_tools_and_one_turn_with_the_profiles_model_and_effort(
    tmp_path, stand_in
):
    run(tmp_path, stand_in, [])
    (call,) = stand_in.calls
    assert call.options.model == "claude-opus-5-5"
    assert call.options.effort == "medium"
    assert call.options.tools == []
    assert call.options.max_turns == 1


def test_the_prompt_numbers_body_paragraphs_keeps_indices_across_blanks_and_skips_tables(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("First line TABLELESS")
        document.add_paragraph("")
        document.add_paragraph("Third line")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "TABLE-ONLY-TEXT"

    run(tmp_path, stand_in, [], build=build)
    (call,) = stand_in.calls
    assert "0: First line TABLELESS" in call.prompt
    assert "2: Third line" in call.prompt
    assert "1:" not in call.prompt
    assert "TABLE-ONLY-TEXT" not in call.prompt
    assert JD.split("\n")[0] in call.prompt


# --- redline_edits.json (PRD 04, "Invalid edits are dropped") ------------------


def test_the_record_lists_every_proposal_in_order_with_its_five_fields_and_validation(
    tmp_path, stand_in
):
    good = edit()
    bad = edit(paragraph=99, jd_quote="Quuxlate system")
    ran = run(tmp_path, stand_in, [as_dict(bad), as_dict(good)])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))["edits"]
    assert isinstance(record, list) and len(record) == 2
    fields = {"paragraph", "find", "replace", "jd_quote", "why_same_meaning"}
    for entry, original in zip(record, [bad, good], strict=True):
        assert set(entry) == fields | {"validation"}
        assert {name: entry[name] for name in fields} == as_dict(original)
    assert isinstance(record[0]["validation"], str) and record[0]["validation"]
    assert record[1]["validation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)


def test_dropped_edits_are_absent_from_the_document_but_present_in_the_record(
    tmp_path, stand_in
):
    good = edit()
    bad = edit(
        paragraph=2,
        find="SQL daily",
        replace="Elasticsearch daily",
        jd_quote="Use PostgreSQL and",
        why_same_meaning="NOT-IN-DOC-MARKER",
    )
    ran = run(tmp_path, stand_in, [as_dict(bad), as_dict(good)])
    with zipfile.ZipFile(ran.copy) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "Elasticsearch" not in body
    assert "NOT-IN-DOC-MARKER" not in comments
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))["edits"]
    assert record[0]["replace"] == "Elasticsearch daily"
    assert record[0]["validation"]
    assert record[1]["validation"] is None


def test_the_copy_is_untouched_when_every_edit_fails_but_the_record_is_written(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [as_dict(edit(paragraph=99))])
    assert ran.copy.read_bytes() == ran.before
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert record["explanation"] is None
    assert len(record["edits"]) == 1
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)


# --- the redline document (PRD 04, "The redline document") ---------------------


def body_of(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def comments_of(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/comments.xml"))


def text_of(paragraph, *, view):
    """A paragraph's text with every tracked change accepted or rejected, as Word shows it."""
    parts = []
    for node in paragraph.iter():
        tag = etree.QName(node).localname
        if tag not in ("t", "delText"):
            continue
        ancestors = {etree.QName(a).localname for a in node.iterancestors()}
        if "ins" in ancestors and view == "reject":
            continue
        if "del" in ancestors and view == "accept":
            continue
        parts.append(node.text or "")
    return "".join(parts)


def view_of(path, view):
    body = body_of(path).find("w:body", NS)
    return [text_of(p, view=view) for p in body.findall("w:p", NS)]


def test_accepting_every_change_gives_the_edited_text_and_rejecting_gives_the_original(
    tmp_path, stand_in
):
    second = edit(
        paragraph=2,
        find="Led data work",
        replace="Led data engineering work",
        jd_quote="data engineering work",
        why_same_meaning="the posting's longer name for the same work",
    )
    ran = run(tmp_path, stand_in, [as_dict(edit()), as_dict(second)])
    assert ran.outcome.applied == 2
    assert view_of(ran.copy, "reject") == PARAGRAPHS
    expected = list(PARAGRAPHS)
    expected[1] = "Built the Quuxlate system from scratch."
    expected[2] = "Led data engineering work using SQL daily."
    assert view_of(ran.copy, "accept") == expected


def test_a_run_that_straddles_several_formatted_runs_still_round_trips(
    tmp_path, stand_in
):
    def build(document):
        paragraph = document.add_paragraph()
        paragraph.add_run("Built ")
        paragraph.add_run("Quuxlate").bold = True
        paragraph.add_run(" platform").italic = True
        paragraph.add_run(" fast.")

    ran = run(
        tmp_path,
        stand_in,
        [as_dict(edit(paragraph=0))],
        build=build,
    )
    assert view_of(ran.copy, "reject") == ["Built Quuxlate platform fast."]
    assert view_of(ran.copy, "accept") == ["Built Quuxlate system fast."]


def test_every_tracked_change_is_authored_claude_ats_and_dated_now(tmp_path, stand_in):
    started = datetime.now(UTC)
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    body = body_of(ran.copy)
    revisions = body.xpath("//w:ins | //w:del", namespaces=NS)
    assert revisions
    for revision in revisions:
        assert revision.get(f"{{{W}}}author") == AUTHOR
        stamp = revision.get(f"{{{W}}}date")
        moment = datetime.fromisoformat(stamp)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        assert (
            started - timedelta(minutes=2)
            <= moment
            <= datetime.now(UTC) + timedelta(minutes=2)
        )


def test_each_change_has_a_comment_with_its_quote_and_its_reason(tmp_path, stand_in):
    ran = run(
        tmp_path,
        stand_in,
        [as_dict(edit(why_same_meaning="WHY-MARKER-ONE"))],
    )
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) >= 1
    for comment in comments:
        text = "".join(comment.itertext())
        assert "Quuxlate system" in text
        assert "WHY-MARKER-ONE" in text
    body = body_of(ran.copy)
    anchored = {
        node.get(f"{{{W}}}id")
        for node in body.xpath("//w:commentRangeStart", namespaces=NS)
    }
    ids = {comment.get(f"{{{W}}}id") for comment in comments}
    assert ids and ids <= anchored


def test_one_edit_with_several_separate_changes_gets_one_comment_around_all_of_them(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Built Quuxlate platform fast, then shipped.")

    multi = edit(
        paragraph=0,
        find="Built Quuxlate platform fast",
        replace="Constructed Quuxlate system fast",
        jd_quote="Constructed the Quuxlate system",
        why_same_meaning="MULTI-WHY",
    )
    jd = "We want someone who Constructed the Quuxlate system."
    ran = run(tmp_path, stand_in, [as_dict(multi)], build=build, jd=jd)
    assert ran.outcome.applied == 1
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) == 1
    text = "".join(comments[0].itertext())
    assert "Constructed the Quuxlate system" in text
    assert "MULTI-WHY" in text
    body = body_of(ran.copy)
    comment_id = comments[0].get(f"{{{W}}}id")
    order = list(body.iter())
    revisions = [
        order.index(node) for node in body.xpath("//w:ins | //w:del", namespaces=NS)
    ]
    assert len(revisions) >= 2
    [start] = body.xpath(f"//w:commentRangeStart[@w:id='{comment_id}']", namespaces=NS)
    [end] = body.xpath(f"//w:commentRangeEnd[@w:id='{comment_id}']", namespaces=NS)
    assert order.index(start) < min(revisions)
    assert order.index(end) > max(revisions)
    assert view_of(ran.copy, "reject") == [
        "Built Quuxlate platform fast, then shipped."
    ]
    assert view_of(ran.copy, "accept") == [
        "Constructed Quuxlate system fast, then shipped."
    ]


def revision_text_inside_comment(path, comment_id):
    """The deleted and inserted text between a comment's range markers, in document order."""
    body = body_of(path)
    inside = False
    parts = []
    for node in body.iter():
        tag = etree.QName(node).localname
        if node.get(f"{{{W}}}id") == comment_id and tag == "commentRangeStart":
            inside = True
        elif node.get(f"{{{W}}}id") == comment_id and tag == "commentRangeEnd":
            return parts
        elif inside and tag in ("t", "delText"):
            ancestors = {etree.QName(a).localname for a in node.iterancestors()}
            if ancestors & {"ins", "del"}:
                parts.append(node.text or "")
    raise AssertionError(f"comment {comment_id} has no closed range")


def test_every_applied_edit_gets_exactly_one_comment_with_a_unique_id_around_its_own_changes(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph(
            "Built Quuxlate platform fast, then ran Zorblat jobs nightly."
        )
        document.add_paragraph("Wrote Frobnicate scripts for churn.")

    # Out of document order on purpose; the first and third each split into several tracked changes.
    third = edit(
        paragraph=1,
        find="Wrote Frobnicate scripts for churn",
        replace="Authored Frobnicate tooling for customer churn",
        jd_quote="Authored Frobnicate tooling for customer churn",
        why_same_meaning="THIRD-WHY",
    )
    second = edit(
        paragraph=0,
        find="ran Zorblat jobs",
        replace="ran Zorblat pipelines",
        jd_quote="Zorblat pipelines",
        why_same_meaning="SECOND-WHY",
    )
    first = edit(
        paragraph=0,
        find="Built Quuxlate platform fast",
        replace="Constructed Quuxlate system fast",
        jd_quote="Constructed the Quuxlate system",
        why_same_meaning="FIRST-WHY",
    )
    jd = (
        "Constructed the Quuxlate system. Zorblat pipelines. "
        "Authored Frobnicate tooling for customer churn."
    )
    ran = run(
        tmp_path,
        stand_in,
        [as_dict(third), as_dict(second), as_dict(first)],
        build=build,
        jd=jd,
    )
    assert ran.outcome.applied == 3
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) == 3
    ids = [comment.get(f"{{{W}}}id") for comment in comments]
    assert len(set(ids)) == 3
    body = body_of(ran.copy)
    for marker in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        anchored = body.xpath(f"//w:{marker}/@w:id", namespaces=NS)
        assert sorted(anchored) == sorted(ids)
    by_why = {
        why: comment.get(f"{{{W}}}id")
        for comment in comments
        for why in ("FIRST-WHY", "SECOND-WHY", "THIRD-WHY")
        if why in "".join(comment.itertext())
    }
    assert set(by_why) == {"FIRST-WHY", "SECOND-WHY", "THIRD-WHY"}
    first_text = revision_text_inside_comment(ran.copy, by_why["FIRST-WHY"])
    second_text = revision_text_inside_comment(ran.copy, by_why["SECOND-WHY"])
    third_text = revision_text_inside_comment(ran.copy, by_why["THIRD-WHY"])
    assert "".join(first_text).count("Constructed") == 1
    assert "system" in "".join(first_text)
    assert "Zorblat" not in "".join(first_text)
    assert "pipelines" in "".join(second_text)
    assert "Constructed" not in "".join(second_text)
    assert "Authored" in "".join(third_text)
    assert "customer" in "".join(third_text)
    assert "Zorblat" not in "".join(third_text)
    assert view_of(ran.copy, "reject") == [
        "Built Quuxlate platform fast, then ran Zorblat jobs nightly.",
        "Wrote Frobnicate scripts for churn.",
    ]
    assert view_of(ran.copy, "accept") == [
        "Constructed Quuxlate system fast, then ran Zorblat pipelines nightly.",
        "Authored Frobnicate tooling for customer churn.",
    ]


def test_a_dropped_edit_leaves_no_comment_beside_an_applied_one(tmp_path, stand_in):
    dropped = edit(
        paragraph=2, find="not in this paragraph", jd_quote="Quuxlate system"
    )
    ran = run(tmp_path, stand_in, [as_dict(dropped), as_dict(edit())])
    assert ran.outcome.applied == 1
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) == 1


def test_only_the_words_that_differ_are_deleted_and_inserted(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    body = body_of(ran.copy)
    deleted = "".join(body.xpath("//w:del//w:delText/text()", namespaces=NS))
    inserted = "".join(body.xpath("//w:ins//w:t/text()", namespaces=NS))
    assert deleted.strip() == "platform"
    assert inserted.strip() == "system"


def test_inserted_text_keeps_the_formatting_of_the_text_it_replaces(tmp_path, stand_in):
    def build(document):
        paragraph = document.add_paragraph()
        paragraph.add_run("Built the ")
        run_ = paragraph.add_run("Quuxlate platform")
        run_.bold = True
        run_.italic = True
        run_.font.name = "Courier New"
        paragraph.add_run(" from scratch.")

    ran = run(tmp_path, stand_in, [as_dict(edit(paragraph=0))], build=build)
    body = body_of(ran.copy)
    inserted_runs = body.xpath("//w:ins/w:r", namespaces=NS)
    assert inserted_runs
    for inserted in inserted_runs:
        assert inserted.xpath("w:rPr/w:b", namespaces=NS)
        assert inserted.xpath("w:rPr/w:i", namespaces=NS)
        assert inserted.xpath("w:rPr/w:rFonts[@w:ascii='Courier New']", namespaces=NS)
    # The unchanged lead-in keeps no emphasis.
    lead = body.xpath("//w:p/w:r[w:t='Built the ']", namespaces=NS)
    assert lead and not lead[0].xpath("w:rPr/w:b", namespaces=NS)


def test_paragraphs_the_edits_do_not_touch_are_unchanged(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    original = body_of(ran.original).find("w:body", NS).findall("w:p", NS)
    redlined = body_of(ran.copy).find("w:body", NS).findall("w:p", NS)
    assert len(original) == len(redlined)
    for index, (before, after) in enumerate(zip(original, redlined, strict=True)):
        if index == 1:
            continue
        assert etree.tostring(before) == etree.tostring(after)


def test_the_redlined_document_replaces_the_resume_copy_in_place(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    assert ran.copy.read_bytes() != ran.before
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "original.docx",
        "redline_edits.json",
        "resume.docx",
    ]
    assert view_of(ran.copy, "reject") == PARAGRAPHS


def test_a_copy_with_no_applied_edit_stays_byte_identical(tmp_path, stand_in):
    for position, reply in enumerate(([], [as_dict(edit(paragraph=99))]), start=1):
        directory = tmp_path / str(position)
        directory.mkdir()
        ran = run(directory, stand_in, reply)
        assert ran.copy.read_bytes() == ran.before


def test_text_in_a_table_is_not_addressable_by_a_body_paragraph_index(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Body line.")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "Built the Quuxlate platform from scratch."

    ran = run(tmp_path, stand_in, [as_dict(edit(paragraph=1))], build=build)
    assert ran.copy.read_bytes() == ran.before
    (entry,) = json.loads(ran.edits_file.read_text(encoding="utf-8"))["edits"]
    assert entry["validation"]


def test_a_resume_with_unresolved_tracked_changes_is_skipped_and_the_agent_never_runs(
    tmp_path, stand_in
):
    from docx.oxml import parse_xml

    def build(document):
        paragraph = document.add_paragraph("Built the ")
        paragraph._p.append(
            parse_xml(
                f'<w:ins xmlns:w="{W}" w:id="901" w:author="Someone"'
                ' w:date="2026-01-01T00:00:00Z"><w:r><w:t>Quuxlate platform</w:t></w:r></w:ins>'
            )
        )

    stand_in.reply = reply_object([as_dict(edit(paragraph=0))])
    copy = make_resume(tmp_path / "resume.docx", build)
    before = copy.read_bytes()
    outcome = redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert outcome is None
    assert stand_in.calls == []
    assert copy.read_bytes() == before
    assert not (tmp_path / "e.json").exists()


def test_a_failure_while_writing_leaves_the_previous_copy_intact_and_no_record(
    tmp_path, stand_in, monkeypatch
):
    from docx.document import Document

    def broken_save(self, path_or_stream):
        with open(path_or_stream, "wb") as partial:
            partial.write(b"PK-half-a-docx")
        raise OSError("disk full")

    stand_in.reply = reply_object([as_dict(edit())])
    copy = make_resume(tmp_path / "resume.docx")
    before = copy.read_bytes()
    monkeypatch.setattr(Document, "save", broken_save)
    with pytest.raises(OSError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert copy.read_bytes() == before
    assert not (tmp_path / "e.json").exists()
    assert docx.Document(str(copy)).paragraphs[1].text == PARAGRAPHS[1]


def test_a_failure_while_swapping_the_copy_in_leaves_the_previous_copy_intact_and_no_record(
    tmp_path, stand_in, monkeypatch
):
    from pathlib import Path

    def broken_replace(self, target):
        raise OSError("interrupted")

    stand_in.reply = reply_object([as_dict(edit())])
    copy = make_resume(tmp_path / "resume.docx")
    before = copy.read_bytes()
    monkeypatch.setattr(Path, "replace", broken_replace)
    with pytest.raises(OSError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert copy.read_bytes() == before
    assert not (tmp_path / "e.json").exists()


def test_a_redline_that_applies_edits_leaves_only_the_copy_and_the_record(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    assert ran.copy.read_bytes() != ran.before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "original.docx",
        "redline_edits.json",
        "resume.docx",
    ]


def test_the_redline_document_is_a_valid_docx_python_docx_can_reopen(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    reopened = docx.Document(str(ran.copy))
    assert len(reopened.paragraphs) == len(PARAGRAPHS)
    assert re.search(r"Riley", reopened.paragraphs[0].text)


# --- the skills list and the skills section (issue #132; PRD 04 "Skills section") ----


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Python\nSQL, RDMS\nNotion\n", ["Python", "SQL", "RDMS", "Notion"]),
        ("Python, SQL, Notion", ["Python", "SQL", "Notion"]),
        ("Python\r\nSQL\r\nNotion", ["Python", "SQL", "Notion"]),
        ("  Python  ,\t SQL \n   Notion   ", ["Python", "SQL", "Notion"]),
        ("- Python\n* SQL\n+ Notion", ["Python", "SQL", "Notion"]),
        ("-   Python, - SQL", ["Python", "SQL"]),
        ("Python,,\n\n,SQL,\n   \n", ["Python", "SQL"]),
        ("Python\npython\nPYTHON, SQL", ["Python", "SQL"]),
        ("Data modeling, data Modeling", ["Data modeling"]),
        ("A/B testing, CI/CD", ["A/B testing", "CI/CD"]),
        ("", []),
        ("   \n\n , ,\n", []),
        ("-\n*\n", []),
    ],
    ids=[
        "the-prd-example",
        "commas-only",
        "crlf-line-breaks",
        "whitespace-trimmed",
        "list-markers-dropped",
        "marker-after-a-comma",
        "empty-items-dropped",
        "case-only-repeats-kept-once",
        "case-only-repeat-of-a-phrase",
        "slashes-stay-inside-an-item",
        "empty-file",
        "only-separators",
        "only-markers",
    ],
)
def test_the_skills_file_is_parsed_into_trimmed_unique_items(text, expected):
    assert parse_skills(text) == expected


def test_the_prd_example_file_gives_four_items():
    assert len(parse_skills("Python\nSQL, RDMS\nNotion\n")) == 4


def bold_paragraph(document, text):
    document.add_paragraph().add_run(text).bold = True


def doc_with(*blocks):
    """A document from `(kind, text)` blocks: `p` plain, `h` Word heading, `b` wholly bold."""
    document = docx.Document()
    for kind, text in blocks:
        if kind == "h":
            document.add_heading(text, level=1)
        elif kind == "b":
            bold_paragraph(document, text)
        else:
            document.add_paragraph(text)
    return document


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        (
            [("p", "Name"), ("p", "Skills"), ("p", "A"), ("p", "B"), ("h", "Jobs")],
            [2, 3],
        ),
        ([("p", "Name"), ("p", "Skills"), ("p", "A"), ("p", "B")], [2, 3]),
        ([("p", "SKILLS"), ("p", "A"), ("h", "Jobs"), ("p", "C")], [1]),
        ([("p", "skills:"), ("p", "A"), ("p", "B")], [1, 2]),
        ([("p", "  Skills:  "), ("p", "A")], [1]),
        ([("h", "Skills"), ("p", "A"), ("p", "B"), ("h", "Jobs")], [1, 2]),
        ([("b", "Skills"), ("p", "A"), ("b", "Jobs"), ("p", "C")], [1]),
        ([("p", "Skills"), ("p", "A"), ("b", "Jobs")], [1]),
        ([("p", "Skills"), ("h", "Jobs"), ("p", "C")], []),
    ],
    ids=[
        "ends-at-a-heading",
        "ends-at-the-end-of-the-document",
        "case-ignored",
        "trailing-colon",
        "surrounding-spaces",
        "the-skills-paragraph-may-itself-be-a-heading",
        "ends-at-a-wholly-bold-paragraph",
        "bold-paragraph-ends-it-directly",
        "empty-section",
    ],
)
def test_the_skills_section_runs_from_the_skills_paragraph_to_the_next_heading_or_bold_paragraph(
    blocks, expected
):
    section = skills_section(doc_with(*blocks))
    assert section is not None and list(section) == expected


def test_a_paragraph_only_partly_bold_does_not_end_the_section():
    document = doc_with(("p", "Skills"), ("p", "A"))
    mixed = document.add_paragraph()
    mixed.add_run("Languages: ").bold = True
    mixed.add_run("Python")
    document.add_paragraph("C")
    assert list(skills_section(document)) == [1, 2, 3]


def test_a_non_bold_paragraph_with_a_bold_run_among_blank_runs_is_not_wholly_bold():
    document = doc_with(("p", "Skills"))
    paragraph = document.add_paragraph()
    paragraph.add_run("Python, ")
    paragraph.add_run("SQL").bold = True
    document.add_paragraph("C")
    assert list(skills_section(document)) == [1, 2]


@pytest.mark.parametrize(
    "blocks",
    [
        [("p", "Name"), ("p", "A")],
        [("p", "Name"), ("p", "Technical Skills"), ("p", "A")],
        [("p", "Skills and tools"), ("p", "A")],
        [("p", "Skills"), ("p", "A"), ("p", "Skills"), ("p", "B")],
        [("p", "Skills"), ("p", "A"), ("h", "Jobs"), ("p", "skills:"), ("p", "B")],
        [],
    ],
    ids=[
        "no-skills-paragraph",
        "heading-text-is-not-the-whole-paragraph",
        "skills-with-more-words",
        "two-skills-paragraphs",
        "two-skills-paragraphs-differing-in-case-and-colon",
        "empty-document",
    ],
)
def test_a_resume_without_exactly_one_skills_paragraph_has_no_skills_section(blocks):
    assert skills_section(doc_with(*blocks)) is None


# --- validating skills edits (PRD 04 "Skills section", "Validation") ------------------

SK_LIST = ["Python", "SQL", "Notion", "Excel", "Looker", "Tableau"]
SK_PARAS = [
    "Riley Resumeperson",
    "Skills",
    "Python, SQL | Notion",
    "Excel, Looker",
    "Experience",
    "Built the Quuxlate platform from scratch.",
]
SK_SECTION = range(2, 4)
SK_JD = JD + "\nBuild Airflow pipelines with Looker Studio, using Python 4."


def skills_edit(**overrides) -> SkillsEdit:
    fields = {
        "paragraph": 3,
        "find": "Looker",
        "replace": "Looker, Airflow",
        "goal": "ats",
        "jd_quote": "Airflow pipelines",
        "reason": "the posting names Airflow",
    }
    return SkillsEdit(**{**fields, **overrides})


def sk_dict(e: SkillsEdit, **overrides) -> dict:
    return {**e.__dict__, **overrides}


def skills_results(
    *edits, paragraphs=SK_PARAS, jd=SK_JD, skills=SK_LIST, section=SK_SECTION
):
    return validate_skills_edits(list(edits), paragraphs, jd, skills, section)


def test_a_skills_edit_that_meets_every_rule_passes():
    assert skills_results(skills_edit()) == [None]


def test_each_skills_result_lines_up_with_its_edit_in_proposal_order():
    out = skills_results(
        skills_edit(paragraph=5), skills_edit(), skills_edit(goal="style")
    )
    assert len(out) == 3
    assert out[0] and out[1] is None and out[2]


def test_every_dropped_skills_edit_carries_a_non_empty_reason():
    out = skills_results(
        skills_edit(paragraph=5),
        skills_edit(find="Nope"),
        skills_edit(goal="style"),
        skills_edit(reason=""),
        skills_edit(replace="Looker"),
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


@pytest.mark.parametrize(
    ("paragraph", "passes"),
    [(0, False), (1, False), (2, True), (3, True), (4, False), (5, False)],
    ids=["name", "heading", "first", "last", "next-heading", "after-section"],
)
def test_a_skills_edit_must_name_a_paragraph_in_the_skills_section(paragraph, passes):
    paragraphs = [*SK_PARAS[:2], "Excel, Looker", "Excel, Looker", *SK_PARAS[4:]]
    (result,) = skills_results(skills_edit(paragraph=paragraph), paragraphs=paragraphs)
    assert (result is None) is passes


@pytest.mark.parametrize("paragraph", [-1, 6, 99])
def test_a_skills_edit_outside_the_document_is_dropped(paragraph):
    (result,) = skills_results(skills_edit(paragraph=paragraph))
    assert result


@pytest.mark.parametrize("skills", [[], ()], ids=["empty-list", "empty-tuple"])
def test_every_skills_edit_is_dropped_while_no_skills_list_is_in_use(skills):
    out = skills_results(
        skills_edit(), skills_edit(goal="narrative", jd_quote=None), skills=skills
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)
    assert len(out) == 2


def test_every_skills_edit_is_dropped_when_the_resume_has_no_skills_section():
    (result,) = skills_results(skills_edit(), section=())
    assert result


def test_find_must_occur_in_the_paragraph_for_a_skills_edit():
    (result,) = skills_results(skills_edit(find="Kubernetes"))
    assert result


def test_a_skills_edits_find_is_matched_exactly_not_case_folded():
    (result,) = skills_results(skills_edit(find="looker"))
    assert result


def test_a_skills_edits_find_that_occurs_twice_is_dropped_and_a_single_one_passes():
    paragraphs = [*SK_PARAS[:2], "Python, SQL, Python", *SK_PARAS[3:]]
    twice = skills_edit(paragraph=2, find="Python", replace="")
    once = skills_edit(paragraph=2, find="SQL", replace="")
    out = skills_results(twice, once, paragraphs=paragraphs)
    assert out[0] and out[1] is None


def test_a_skills_edit_overlapping_an_earlier_passing_edit_is_dropped():
    first = skills_edit(paragraph=2, find="Python, SQL", replace="Python")
    second = skills_edit(paragraph=2, find="SQL | Notion", replace="Notion")
    out = skills_results(first, second)
    assert out[0] is None and out[1]


def test_a_skills_edit_overlapping_an_earlier_dropped_edit_still_passes():
    dropped = skills_edit(
        paragraph=2, find="Python, SQL", replace="Python", jd_quote=None
    )
    second = skills_edit(paragraph=2, find="SQL | Notion", replace="Notion")
    out = skills_results(dropped, second)
    assert out[0] and out[1] is None


def test_skills_edits_in_one_paragraph_that_do_not_overlap_both_pass():
    first = skills_edit(
        paragraph=2, find="Python,", replace="", goal="narrative", jd_quote=None
    )
    second = skills_edit(
        paragraph=2, find="Notion", replace="", goal="narrative", jd_quote=None
    )
    assert skills_results(first, second) == [None, None]


def test_skills_edit_overlap_is_per_paragraph():
    first = skills_edit(paragraph=3, find="Looker", replace="")
    second = skills_edit(paragraph=2, find="Python", replace="")
    assert skills_results(first, second) == [None, None]


def test_replace_must_differ_from_find_for_a_skills_edit():
    (result,) = skills_results(skills_edit(replace="Looker"))
    assert result


def test_removing_a_skill_with_an_empty_replace_is_allowed():
    assert skills_results(
        skills_edit(find="Excel, ", replace="", goal="narrative", jd_quote=None)
    ) == [None]


def test_a_shorter_replace_is_allowed():
    assert skills_results(
        skills_edit(
            paragraph=2,
            find="Python, SQL",
            replace="SQL",
            goal="narrative",
            jd_quote=None,
        )
    ) == [None]


@pytest.mark.parametrize("goal", ["ats", "narrative"])
def test_both_goals_are_accepted(goal):
    assert skills_results(skills_edit(goal=goal)) == [None]


@pytest.mark.parametrize("goal", ["style", "", "both", None, "ATS", 1])
def test_a_goal_that_is_neither_ats_nor_narrative_is_dropped(goal):
    (result,) = skills_results(skills_edit(goal=goal))
    assert result


def test_an_ats_edit_needs_a_jd_quote():
    (result,) = skills_results(skills_edit(jd_quote=None, replace="Looker, Tableau"))
    assert result


def test_a_narrative_edit_may_have_no_jd_quote_when_every_word_is_on_the_list():
    assert skills_results(
        skills_edit(goal="narrative", jd_quote=None, replace="Looker, Tableau")
    ) == [None]


def test_a_narrative_edit_may_cite_a_quote():
    assert skills_results(skills_edit(goal="narrative")) == [None]


@pytest.mark.parametrize("goal", ["ats", "narrative"])
def test_a_jd_quote_that_is_not_in_the_posting_is_dropped_for_either_goal(goal):
    (result,) = skills_results(skills_edit(goal=goal, jd_quote="Spark clusters"))
    assert result


def test_a_skills_edits_jd_quote_is_matched_case_sensitively():
    (result,) = skills_results(skills_edit(jd_quote="airflow pipelines"))
    assert result


def test_whitespace_runs_are_collapsed_in_a_skills_edits_quote_and_the_posting():
    jd = "Build   Airflow\n\npipelines   every day."
    quote = "Airflow   pipelines every\tday"
    assert skills_results(skills_edit(jd_quote=quote), jd=jd) == [None]


def test_an_inserted_word_on_the_list_passes_without_being_in_the_quote_or_find():
    assert skills_results(
        skills_edit(goal="narrative", jd_quote=None, replace="Looker Tableau")
    ) == [None]


def test_an_inserted_word_in_the_quote_passes_without_being_on_the_list():
    assert skills_results(skills_edit()) == [None]


def test_an_inserted_word_in_find_passes_without_being_on_the_list():
    paragraphs = [*SK_PARAS[:3], "Excel, Looker, Rust", *SK_PARAS[4:]]
    edit_ = skills_edit(
        find="Excel, Looker, Rust",
        replace="Rust, Excel, Looker",
        goal="narrative",
        jd_quote=None,
    )
    assert skills_results(edit_, paragraphs=paragraphs) == [None]


def test_an_inserted_word_on_the_list_comparison_ignores_case_and_edge_punctuation():
    assert skills_results(
        skills_edit(goal="narrative", jd_quote=None, replace="Looker, (tableau)")
    ) == [None]


def test_an_inserted_word_found_nowhere_is_dropped():
    (result,) = skills_results(skills_edit(replace="Looker, Rust"))
    assert result


def test_a_word_in_the_posting_but_not_in_the_cited_quote_or_list_is_dropped():
    (result,) = skills_results(
        skills_edit(replace="Looker, Airflow", jd_quote="Looker Studio")
    )
    assert result


def test_a_word_on_the_list_in_another_form_is_not_a_match():
    (result,) = skills_results(
        skills_edit(goal="narrative", jd_quote=None, replace="Looker, Tableaus")
    )
    assert result


def test_one_bad_inserted_word_drops_an_edit_whose_other_words_are_fine():
    (result,) = skills_results(
        skills_edit(goal="narrative", jd_quote=None, replace="Looker, Tableau, Rust")
    )
    assert result


@pytest.mark.parametrize("separator", ["|", "•", "-", "/", "·"])
def test_a_separator_with_no_letter_or_digit_is_never_an_inserted_word(separator):
    assert skills_results(
        skills_edit(
            paragraph=2,
            find="Python, SQL",
            replace=f"Python, {separator} SQL",
            goal="narrative",
            jd_quote=None,
        )
    ) == [None]


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_an_empty_or_blank_reason_is_dropped(reason):
    (result,) = skills_results(skills_edit(reason=reason))
    assert result


def test_the_six_word_limit_does_not_apply_to_a_skills_edit():
    paragraphs = [
        *SK_PARAS[:2],
        "Python, SQL, Notion, Excel, Looker, Tableau, Airflow, dbt",
        *SK_PARAS[3:],
    ]
    long = skills_edit(
        paragraph=2,
        find="Python, SQL, Notion, Excel, Looker, Tableau, Airflow, dbt",
        replace="Python, SQL",
        goal="narrative",
        jd_quote=None,
    )
    assert skills_results(long, paragraphs=paragraphs) == [None]


def test_the_number_rule_does_not_apply_to_a_skills_edit():
    paragraphs = [*SK_PARAS[:2], "Python 3, SQL", *SK_PARAS[3:]]
    changed = skills_edit(
        paragraph=2, find="Python 3", replace="Python 4", jd_quote="Python 4"
    )
    dropped = skills_edit(
        paragraph=2, find="SQL", replace="", goal="narrative", jd_quote=None
    )
    assert skills_results(changed, dropped, paragraphs=paragraphs) == [None, None]


def test_a_number_inserted_into_a_skills_edit_still_has_to_come_from_the_list_quote_or_find():
    paragraphs = [*SK_PARAS[:2], "Python 3, SQL", *SK_PARAS[3:]]
    (result,) = skills_results(
        skills_edit(
            paragraph=2, find="Python 3", replace="Python 5", jd_quote="Python 4"
        ),
        paragraphs=paragraphs,
    )
    assert result


# --- which rules apply is decided by the paragraph index ----------------------------


def ordinary_in_section(**overrides) -> Edit:
    return edit(
        paragraph=3,
        find="Looker",
        replace="Looker Studio",
        jd_quote="Looker Studio",
        **overrides,
    )


def test_an_edits_entry_naming_a_skills_section_paragraph_is_dropped_while_the_list_is_in_use():
    (result,) = validate_edits([ordinary_in_section()], SK_PARAS, SK_JD, SK_SECTION)
    assert isinstance(result, str) and result.strip()


def test_the_same_edit_passes_under_the_ordinary_rules_when_no_skills_section_is_given():
    assert validate_edits([ordinary_in_section()], SK_PARAS, SK_JD) == [None]
    assert validate_edits([ordinary_in_section()], SK_PARAS, SK_JD, ()) == [None]


def test_an_edits_entry_outside_the_skills_section_is_unaffected_by_it():
    outside = edit(paragraph=5)
    assert validate_edits([outside], SK_PARAS, SK_JD, SK_SECTION) == [None]


def test_the_ordinary_rules_still_apply_to_the_paragraphs_around_a_skills_section():
    unquoted = edit(paragraph=5, jd_quote="text that is not in the posting")
    (result,) = validate_edits([unquoted], SK_PARAS, SK_JD, SK_SECTION)
    assert result


def test_a_separator_token_is_not_a_word_under_the_ordinary_rules_either():
    bulleted = edit(paragraph=5, find="Quuxlate platform", replace="Quuxlate • system")
    assert validate_edits([bulleted], SK_PARAS, SK_JD) == [None]


# --- the redline document with a skills list ----------------------------------------


def skills_resume(document):
    for text in SK_PARAS[:2]:
        document.add_paragraph(text)
    for text in SK_PARAS[2:4]:
        document.add_paragraph(text)
    document.add_heading(SK_PARAS[4], level=1)
    document.add_paragraph(SK_PARAS[5])


def two_skills_resume(document):
    for text in [
        "Riley Resumeperson",
        "Skills",
        "Python, SQL | Notion",
        "Skills:",
        "Excel, Looker",
    ]:
        document.add_paragraph(text)


def run_skills(
    tmp_path,
    stand_in,
    *,
    edits=(),
    skills_edits=(),
    build=skills_resume,
    skills=SK_LIST,
):
    reply = reply_object(
        list(edits), None if edits or skills_edits else EXPLANATION, skills_edits
    )
    return run(tmp_path, stand_in, reply, build=build, jd=SK_JD, skills=skills)


def record_of(ran):
    return json.loads(ran.edits_file.read_text(encoding="utf-8"))


def test_a_passing_skills_edit_is_a_tracked_change_that_accepts_and_rejects_cleanly(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(skills_edit())])
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 0)
    original = SK_PARAS
    assert view_of(ran.copy, "reject") == original
    accepted = view_of(ran.copy, "accept")
    assert accepted[3] == "Excel, Looker, Airflow"
    assert [p for i, p in enumerate(accepted) if i != 3] == [
        p for i, p in enumerate(original) if i != 3
    ]
    body = body_of(ran.copy)
    inserted = body.xpath("//w:ins", namespaces=NS)
    deleted = body.xpath("//w:del", namespaces=NS) + inserted
    assert inserted
    assert {node.get(f"{{{W}}}author") for node in deleted} == {AUTHOR}


def test_a_removal_is_written_as_a_tracked_deletion(tmp_path, stand_in):
    removal = skills_edit(find="Excel, ", replace="", goal="narrative", jd_quote=None)
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(removal)])
    assert ran.outcome.applied == 1
    assert view_of(ran.copy, "accept")[3] == "Looker"
    assert view_of(ran.copy, "reject")[3] == "Excel, Looker"


def comment_texts(path):
    return [
        "".join(comment.itertext())
        for comment in comments_of(path).findall("w:comment", NS)
    ]


def test_an_ats_skills_edits_comment_names_the_goal_quotes_the_posting_and_gives_the_reason(
    tmp_path, stand_in
):
    ran = run_skills(
        tmp_path,
        stand_in,
        skills_edits=[sk_dict(skills_edit(reason="SK-REASON-MARKER"))],
    )
    (text,) = comment_texts(ran.copy)
    assert "ats" in text.lower()
    assert "Airflow pipelines" in text
    assert "SK-REASON-MARKER" in text


def test_a_narrative_skills_edit_without_a_quote_gets_a_comment_with_its_goal_and_reason_only(
    tmp_path, stand_in
):
    narrative = skills_edit(
        replace="Looker, Tableau",
        goal="narrative",
        jd_quote=None,
        reason="SK-NARRATIVE-MARKER",
    )
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(narrative)])
    (text,) = comment_texts(ran.copy)
    assert "narrative" in text.lower()
    assert "SK-NARRATIVE-MARKER" in text
    assert "None" not in text and "null" not in text


def test_a_narrative_skills_edit_with_a_quote_has_it_in_the_comment(tmp_path, stand_in):
    ran = run_skills(
        tmp_path,
        stand_in,
        skills_edits=[sk_dict(skills_edit(goal="narrative"))],
    )
    (text,) = comment_texts(ran.copy)
    assert "narrative" in text.lower() and "Airflow pipelines" in text


def test_a_skills_edit_and_an_ordinary_edit_each_get_a_comment(tmp_path, stand_in):
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[as_dict(edit(paragraph=5, why_same_meaning="ORD-WHY-MARKER"))],
        skills_edits=[sk_dict(skills_edit(reason="SK-WHY-MARKER"))],
    )
    texts = comment_texts(ran.copy)
    assert len(texts) == 2
    assert any("ORD-WHY-MARKER" in t for t in texts)
    assert any("SK-WHY-MARKER" in t for t in texts)
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 0)


def test_the_record_holds_skills_edits_with_each_entrys_validation_and_counts_both_kinds(
    tmp_path, stand_in
):
    good_skill = skills_edit()
    bad_skill = skills_edit(paragraph=5)
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[as_dict(edit(paragraph=5))],
        skills_edits=[sk_dict(bad_skill), sk_dict(good_skill)],
    )
    record = record_of(ran)
    assert set(record) == {"explanation", "edits", "skills_edits"}
    assert record["explanation"] is None
    assert len(record["edits"]) == 1 and record["edits"][0]["validation"] is None
    fields = {"paragraph", "find", "replace", "goal", "jd_quote", "reason"}
    entries_ = record["skills_edits"]
    assert len(entries_) == 2
    for entry, original in zip(entries_, [bad_skill, good_skill], strict=True):
        assert set(entry) == fields | {"validation"}
        assert {name: entry[name] for name in fields} == sk_dict(original)
    assert isinstance(entries_[0]["validation"], str) and entries_[0]["validation"]
    assert entries_[1]["validation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 1)


def test_dropped_skills_edits_are_in_the_record_and_absent_from_the_document(
    tmp_path, stand_in
):
    bad = skills_edit(replace="Looker, Rust", reason="DROPPED-SK-MARKER")
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[as_dict(edit(paragraph=5))],
        skills_edits=[sk_dict(bad)],
    )
    with zipfile.ZipFile(ran.copy) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "Rust" not in body and "DROPPED-SK-MARKER" not in comments
    assert record_of(ran)["skills_edits"][0]["validation"]


def test_a_copy_whose_every_skills_edit_failed_is_untouched_but_the_record_is_written(
    tmp_path, stand_in
):
    ran = run_skills(
        tmp_path, stand_in, skills_edits=[sk_dict(skills_edit(replace="Looker, Rust"))]
    )
    assert ran.copy.read_bytes() == ran.before
    assert record_of(ran)["explanation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert not edits_applied(ran.edits_file)


def test_an_applied_skills_edit_alone_marks_the_redline_as_having_applied_an_edit(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(skills_edit())])
    assert edits_applied(ran.edits_file)


def test_only_the_resume_copy_and_the_record_are_left_after_a_skills_redline(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(skills_edit())])
    assert {p.name for p in tmp_path.iterdir()} == {
        "resume.docx",
        "original.docx",
        "redline_edits.json",
    }
    assert docx.Document(str(ran.copy)).paragraphs


def test_a_skills_only_result_needs_no_explanation(tmp_path, stand_in):
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(skills_edit())])
    assert record_of(ran)["explanation"] is None


def test_every_skills_edit_is_dropped_with_no_skills_list_and_the_section_is_held_to_the_ordinary_rules(
    tmp_path, stand_in
):
    ordinary = edit(
        paragraph=3, find="Looker", replace="Looker Studio", jd_quote="Looker Studio"
    )
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[as_dict(ordinary)],
        skills_edits=[sk_dict(skills_edit())],
        skills=(),
    )
    record = record_of(ran)
    assert record["edits"][0]["validation"] is None
    assert record["skills_edits"][0]["validation"]
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)
    assert view_of(ran.copy, "accept")[3] == "Excel, Looker Studio"


def test_with_a_skills_list_an_edits_entry_in_the_section_is_dropped_and_one_outside_applies(
    tmp_path, stand_in
):
    inside = edit(
        paragraph=3, find="Looker", replace="Looker Studio", jd_quote="Looker Studio"
    )
    ran = run_skills(
        tmp_path, stand_in, edits=[as_dict(inside), as_dict(edit(paragraph=5))]
    )
    record = record_of(ran)
    assert record["edits"][0]["validation"]
    assert record["edits"][1]["validation"] is None
    assert view_of(ran.copy, "accept")[3] == "Excel, Looker"


def test_a_skills_edit_naming_a_paragraph_outside_the_section_is_dropped(
    tmp_path, stand_in
):
    outside = skills_edit(
        paragraph=5,
        find="Quuxlate platform",
        replace="Quuxlate system",
        jd_quote="Quuxlate system",
    )
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(outside)])
    assert record_of(ran)["skills_edits"][0]["validation"]
    assert ran.copy.read_bytes() == ran.before


def test_a_resume_with_no_skills_paragraph_has_no_skills_section_and_ordinary_rules_apply_everywhere(
    tmp_path, stand_in
):
    skill = skills_edit(
        paragraph=2,
        find="SQL daily",
        replace="SQL daily, Tableau",
        goal="narrative",
        jd_quote=None,
    )
    ordinary = edit(
        paragraph=2,
        find="Led data work",
        replace="Led data engineering work",
        jd_quote="data engineering work",
    )
    ran = run(
        tmp_path,
        stand_in,
        reply_object([as_dict(ordinary)], None, [sk_dict(skill)]),
        jd=SK_JD,
        skills=SK_LIST,
    )
    record = record_of(ran)
    assert record["edits"][0]["validation"] is None
    assert record["skills_edits"][0]["validation"]
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)


def test_a_resume_with_two_skills_paragraphs_has_no_skills_section(tmp_path, stand_in):
    skill = skills_edit(paragraph=4)
    ordinary = edit(
        paragraph=4, find="Looker", replace="Looker Studio", jd_quote="Looker Studio"
    )
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[as_dict(ordinary)],
        skills_edits=[sk_dict(skill)],
        build=two_skills_resume,
    )
    record = record_of(ran)
    assert record["edits"][0]["validation"] is None
    assert record["skills_edits"][0]["validation"]


# --- the agent's output with skills edits --------------------------------------------


def test_a_skills_edits_only_result_with_a_null_explanation_is_accepted(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(skills_edit())])
    assert ran.outcome is not None


@pytest.mark.parametrize(
    "explanation", [EXPLANATION, "", 7], ids=["text", "empty", "number"]
)
def test_skills_edits_with_an_explanation_or_an_empty_one_raise(
    tmp_path, stand_in, explanation
):
    copy = make_resume(tmp_path / "resume.docx", skills_resume)
    stand_in.reply = reply_object([], explanation, [sk_dict(skills_edit())])
    edits_file = tmp_path / "e.json"
    with pytest.raises(JsaError):
        redline_resume(copy, edits_file, SK_JD, SETTINGS, SK_LIST)
    assert not edits_file.exists()


@pytest.mark.parametrize(
    "missing", ["paragraph", "find", "replace", "goal", "jd_quote", "reason"]
)
def test_a_skills_edit_object_missing_a_field_raises(tmp_path, stand_in, missing):
    copy = make_resume(tmp_path / "resume.docx", skills_resume)
    partial = sk_dict(skills_edit())
    del partial[missing]
    stand_in.reply = reply_object([], None, [partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", SK_JD, SETTINGS, SK_LIST)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize(
    "missing", ["paragraph", "find", "replace", "jd_quote", "why_same_meaning"]
)
def test_every_ordinary_edit_field_is_required_in_the_reply(
    tmp_path, stand_in, missing
):
    copy = make_resume(tmp_path / "resume.docx")
    partial = as_dict(edit())
    del partial[missing]
    stand_in.reply = reply_object([partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("jd_quote", 7), ("jd_quote", ["Airflow pipelines"]), ("goal", None)],
    ids=["number-quote", "list-quote", "null-goal"],
)
def test_a_skills_edit_field_of_the_wrong_type_raises(tmp_path, stand_in, field, value):
    copy = make_resume(tmp_path / "resume.docx", skills_resume)
    stand_in.reply = reply_object([], None, [sk_dict(skills_edit(), **{field: value})])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", SK_JD, SETTINGS, SK_LIST)
    assert not (tmp_path / "e.json").exists()


def test_a_skills_edit_with_an_explicit_null_quote_is_recorded_not_rejected(
    tmp_path, stand_in
):
    narrative = skills_edit(goal="narrative", jd_quote=None, replace="Looker, Tableau")
    ran = run_skills(tmp_path, stand_in, skills_edits=[sk_dict(narrative)])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    (entry,) = record["skills_edits"]
    assert entry["jd_quote"] is None
    assert ran.outcome.applied == 1


# --- the redline prompt (PRD 04 "Prompt") --------------------------------------------


def test_the_prompt_carries_the_skills_list_one_item_per_line():
    prompt = assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [2, 3])
    assert set(SK_LIST) <= set(prompt.splitlines())
    assert "{{" not in prompt and "}}" not in prompt


def test_the_prompt_has_no_unfilled_marker_and_no_skills_without_a_list_or_a_section():
    prompt = assemble_redline_prompt(SK_JD, SK_PARAS)
    assert "{{" not in prompt and "}}" not in prompt
    assert "Tableau" not in prompt
    assert prompt != assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [2, 3])


def test_the_prompt_changes_with_the_skills_sections_paragraphs():
    one = assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [2, 3])
    other = assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [5])
    assert one != other
    assert "{{" not in other


def test_the_prompt_degrades_when_only_the_section_or_only_the_list_is_missing():
    no_section = assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [])
    no_list = assemble_redline_prompt(SK_JD, SK_PARAS, [], [2, 3])
    for prompt in (no_section, no_list):
        assert "{{" not in prompt and "}}" not in prompt


def test_the_template_states_both_goals_the_skills_edit_fields_and_the_output_keys(
    tmp_path, stand_in
):
    run_skills(tmp_path, stand_in)
    (call,) = stand_in.calls
    prompt = call.prompt
    lowered = prompt.lower()
    for term in (
        "skills_edits",
        "goal",
        "ats",
        "narrative",
        "jd_quote",
        "reason",
        "explanation",
    ):
        assert term in lowered
    assert lowered.index("ats") < lowered.index("narrative")


def test_the_prompt_the_agent_receives_carries_the_list_and_the_located_section(
    tmp_path, stand_in
):
    run_skills(tmp_path, stand_in)
    (call,) = stand_in.calls
    assert set(SK_LIST) <= set(call.prompt.splitlines())
    assert call.prompt != assemble_redline_prompt(SK_JD, SK_PARAS, SK_LIST, [])
    assert "{{" not in call.prompt


def test_the_prompt_the_agent_receives_without_a_list_holds_none_of_it(
    tmp_path, stand_in
):
    run_skills(tmp_path, stand_in, skills=())
    (call,) = stand_in.calls
    assert "Tableau" not in call.prompt
    assert "{{" not in call.prompt
