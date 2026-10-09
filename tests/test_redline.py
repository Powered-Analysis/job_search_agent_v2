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
from jsa.redline import Edit, redline_resume, validate_edits

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


def reply_object(edits, explanation=None):
    return json.dumps({"edits": edits, "explanation": explanation})


def run(tmp_path, stand_in, reply, *, build=None, jd=JD):
    """`reply` is raw text, or a list of edit dicts sent as `{edits, explanation}`."""
    if isinstance(reply, list):
        reply = reply_object(reply, None if reply else EXPLANATION)
    stand_in.reply = reply
    copy = make_resume(tmp_path / "resume.docx", build)
    redline = tmp_path / "resume_redline.docx"
    edits_file = tmp_path / "redline_edits.json"
    outcome = redline_resume(copy, redline, edits_file, jd, SETTINGS)
    return SimpleNamespace(
        outcome=outcome, copy=copy, redline=redline, edits_file=edits_file
    )


def as_dict(e: Edit, **overrides) -> dict:
    return {**e.__dict__, **overrides}


def test_a_no_edit_result_records_the_explanation_and_an_empty_edit_list(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert record == {"explanation": EXPLANATION, "edits": []}
    assert not ran.redline.exists()
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 0)


def test_a_result_with_edits_records_a_null_explanation(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert set(record) == {"explanation", "edits"}
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
        '{"edits": [], "explanation": "x"',
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
    redline = tmp_path / "r.docx"
    edits_file = tmp_path / "e.json"
    with pytest.raises(JsaError):
        redline_resume(copy, redline, edits_file, JD, SETTINGS)
    assert not edits_file.exists()
    assert not redline.exists()


def test_a_bare_array_of_real_edits_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = json.dumps([as_dict(edit())])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "r.docx", tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


def test_a_fenced_object_of_real_edits_also_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = "```json\n" + reply_object([as_dict(edit())]) + "\n```"
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "r.docx", tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize(
    "reply",
    [
        reply_object([], None),
        reply_object([], ""),
        json.dumps({"edits": []}),
        reply_object([], 7),
        json.dumps({"explanation": EXPLANATION}),
        json.dumps({"edits": None, "explanation": EXPLANATION}),
        json.dumps({"edits": {}, "explanation": EXPLANATION}),
        reply_object([as_dict(edit())], EXPLANATION),
        reply_object([as_dict(edit())], ""),
        json.dumps({"edits": [as_dict(edit())]}),
        reply_object([as_dict(edit())], 7),
    ],
    ids=[
        "no-edits-null-explanation",
        "no-edits-empty-explanation",
        "no-edits-missing-explanation",
        "no-edits-numeric-explanation",
        "missing-edits",
        "null-edits",
        "object-edits",
        "edits-with-an-explanation",
        "edits-with-an-empty-explanation",
        "edits-missing-explanation",
        "edits-with-numeric-explanation",
    ],
)
def test_an_object_that_breaks_the_explanation_rule_or_shape_raises(
    tmp_path, stand_in, reply
):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = reply
    redline = tmp_path / "r.docx"
    edits_file = tmp_path / "e.json"
    with pytest.raises(JsaError):
        redline_resume(copy, redline, edits_file, JD, SETTINGS)
    assert not edits_file.exists()
    assert not redline.exists()


def test_an_edit_object_missing_a_field_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    partial = as_dict(edit())
    del partial["why_same_meaning"]
    stand_in.reply = reply_object([partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "r.docx", tmp_path / "e.json", JD, SETTINGS)
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
    with zipfile.ZipFile(ran.redline) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "Elasticsearch" not in body
    assert "NOT-IN-DOC-MARKER" not in comments
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))["edits"]
    assert record[0]["replace"] == "Elasticsearch daily"
    assert record[0]["validation"]
    assert record[1]["validation"] is None


def test_no_redline_is_written_when_every_edit_fails_but_the_record_is(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [as_dict(edit(paragraph=99))])
    assert not ran.redline.exists()
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
    assert view_of(ran.redline, "reject") == PARAGRAPHS
    expected = list(PARAGRAPHS)
    expected[1] = "Built the Quuxlate system from scratch."
    expected[2] = "Led data engineering work using SQL daily."
    assert view_of(ran.redline, "accept") == expected


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
    assert view_of(ran.redline, "reject") == ["Built Quuxlate platform fast."]
    assert view_of(ran.redline, "accept") == ["Built Quuxlate system fast."]


def test_every_tracked_change_is_authored_claude_ats_and_dated_now(tmp_path, stand_in):
    started = datetime.now(UTC)
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    body = body_of(ran.redline)
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
    comments = comments_of(ran.redline).findall("w:comment", NS)
    assert len(comments) >= 1
    for comment in comments:
        text = "".join(comment.itertext())
        assert "Quuxlate system" in text
        assert "WHY-MARKER-ONE" in text
    body = body_of(ran.redline)
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
    comments = comments_of(ran.redline).findall("w:comment", NS)
    assert len(comments) == 1
    text = "".join(comments[0].itertext())
    assert "Constructed the Quuxlate system" in text
    assert "MULTI-WHY" in text
    body = body_of(ran.redline)
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
    assert view_of(ran.redline, "reject") == [
        "Built Quuxlate platform fast, then shipped."
    ]
    assert view_of(ran.redline, "accept") == [
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
    comments = comments_of(ran.redline).findall("w:comment", NS)
    assert len(comments) == 3
    ids = [comment.get(f"{{{W}}}id") for comment in comments]
    assert len(set(ids)) == 3
    body = body_of(ran.redline)
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
    first_text = revision_text_inside_comment(ran.redline, by_why["FIRST-WHY"])
    second_text = revision_text_inside_comment(ran.redline, by_why["SECOND-WHY"])
    third_text = revision_text_inside_comment(ran.redline, by_why["THIRD-WHY"])
    assert "".join(first_text).count("Constructed") == 1
    assert "system" in "".join(first_text)
    assert "Zorblat" not in "".join(first_text)
    assert "pipelines" in "".join(second_text)
    assert "Constructed" not in "".join(second_text)
    assert "Authored" in "".join(third_text)
    assert "customer" in "".join(third_text)
    assert "Zorblat" not in "".join(third_text)
    assert view_of(ran.redline, "reject") == [
        "Built Quuxlate platform fast, then ran Zorblat jobs nightly.",
        "Wrote Frobnicate scripts for churn.",
    ]
    assert view_of(ran.redline, "accept") == [
        "Constructed Quuxlate system fast, then ran Zorblat pipelines nightly.",
        "Authored Frobnicate tooling for customer churn.",
    ]


def test_a_dropped_edit_leaves_no_comment_beside_an_applied_one(tmp_path, stand_in):
    dropped = edit(
        paragraph=2, find="not in this paragraph", jd_quote="Quuxlate system"
    )
    ran = run(tmp_path, stand_in, [as_dict(dropped), as_dict(edit())])
    assert ran.outcome.applied == 1
    comments = comments_of(ran.redline).findall("w:comment", NS)
    assert len(comments) == 1


def test_only_the_words_that_differ_are_deleted_and_inserted(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    body = body_of(ran.redline)
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
    body = body_of(ran.redline)
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
    original = body_of(ran.copy).find("w:body", NS).findall("w:p", NS)
    redlined = body_of(ran.redline).find("w:body", NS).findall("w:p", NS)
    assert len(original) == len(redlined)
    for index, (before, after) in enumerate(zip(original, redlined, strict=True)):
        if index == 1:
            continue
        assert etree.tostring(before) == etree.tostring(after)


def test_the_resume_copy_is_byte_identical_after_a_run(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    before = copy.read_bytes()
    stand_in.reply = reply_object([as_dict(edit())])
    redline_resume(
        copy, tmp_path / "resume_redline.docx", tmp_path / "e.json", JD, SETTINGS
    )
    assert copy.read_bytes() == before
    assert (tmp_path / "resume_redline.docx").exists()


def test_text_in_a_table_is_not_addressable_by_a_body_paragraph_index(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Body line.")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "Built the Quuxlate platform from scratch."

    ran = run(tmp_path, stand_in, [as_dict(edit(paragraph=1))], build=build)
    assert not ran.redline.exists()
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
    outcome = redline_resume(
        copy, tmp_path / "r.docx", tmp_path / "e.json", JD, SETTINGS
    )
    assert outcome is None
    assert stand_in.calls == []
    assert not (tmp_path / "r.docx").exists()
    assert not (tmp_path / "e.json").exists()


def test_the_redline_document_is_a_valid_docx_python_docx_can_reopen(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    reopened = docx.Document(str(ran.redline))
    assert len(reopened.paragraphs) == len(PARAGRAPHS)
    assert re.search(r"Riley", reopened.paragraphs[0].text)
