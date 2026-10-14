"""The redline's validators, output parsing and tracked-change document (issues #82, #132, #152; PRD 04 "Redline").

Validation and parsing are pure. The document tests drive `redline_resume` with Claude replaced at the
shared agent loop (`agent_loop.query`) and read the written .docx back as Word would: accepting or
rejecting every tracked change is computed from the document XML.
"""

import json
import re
import zipfile
from dataclasses import asdict
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
    Flag,
    assemble_redline_prompt,
    edits_applied,
    parse_skills,
    redline_resume,
    validate_edits,
    validate_flags,
)
from jsa.redline_docx import skills_section

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
AUTHOR = "Claude (Redline)"
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
        "goal": "ats",
        "jd_quote": "Quuxlate system",
        "reason": "the posting's name for the same thing",
    }
    return Edit(**{**fields, **overrides})


def results(*edits: Edit, paragraphs=PARAGRAPHS, jd=JD):
    return validate_edits(list(edits), paragraphs, jd)


# --- edit validation (PRD 04, "Validation") ------------------------------------


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
    dropped = edit(reason="")
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


LONG = (
    "Built the Quuxlate platform from scratch and ran SQL reports nightly for finance."
)


def test_find_has_no_length_limit_and_may_be_a_whole_paragraph():
    # Thirteen words, the whole paragraph.
    replace = LONG.replace("platform", "system")
    assert results(
        edit(paragraph=0, find=LONG, replace=replace), paragraphs=[LONG]
    ) == [None]
    # A long stretch of the paragraph passes too.
    long_find = "Built the Quuxlate platform from scratch and ran SQL reports"
    assert results(
        edit(
            paragraph=0, find=long_find, replace=long_find.replace("platform", "system")
        ),
        paragraphs=[LONG],
    ) == [None]


def test_words_are_whitespace_separated_tokens_so_punctuation_does_not_make_a_word():
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


@pytest.mark.parametrize("separator", ["|", "•", "-", "/", "·"])
def test_a_token_with_no_letter_or_digit_is_never_an_inserted_word(separator):
    bulleted = edit(replace=f"Quuxlate {separator} system")
    assert results(bulleted) == [None]


@pytest.mark.parametrize("goal", ["ats", "narrative"])
def test_ats_and_narrative_edits_pass_the_same_rules(goal):
    assert results(edit(goal=goal)) == [None]
    assert results(edit(goal=goal, jd_quote=None))[0]
    assert results(edit(goal=goal, jd_quote=""))[0]
    assert results(edit(goal=goal, jd_quote="Quuxlate framework"))[0]
    assert results(edit(goal=goal, replace="Quuxlate framework"))[0]


@pytest.mark.parametrize("goal", ["style", "", "both", "ATS", "Narrative", None, 1])
def test_a_goal_that_is_not_one_of_the_three_values_is_dropped(goal):
    assert results(edit(goal=goal))[0]


# --- corrections (PRD 04, "Validation": goal `correction`) -----------------------

TYPO_PARAGRAPHS = [
    "Riley Resumeperson",
    "Built the Quuxlate platfrom from scratch.",
    "Helped recieve funding across the teem.",
    "Reduced costs by 30% in 2O20.",
]


def correction(**overrides) -> Edit:
    fields = {
        "paragraph": 1,
        "find": "platfrom",
        "replace": "platform",
        "goal": "correction",
        "jd_quote": None,
        "reason": "spelling",
    }
    return Edit(**{**fields, **overrides})


def typo_results(*edits: Edit):
    return validate_edits(list(edits), TYPO_PARAGRAPHS, JD)


def test_a_correction_needs_no_posting_quote_and_its_word_need_not_be_in_the_posting():
    assert "platform" not in JD
    assert typo_results(correction()) == [None]


def test_a_correction_with_a_posting_quote_is_dropped():
    assert typo_results(correction(jd_quote="Quuxlate system"))[0]
    assert typo_results(correction(jd_quote=""))[0]


def test_a_correction_that_adds_a_word_is_dropped():
    assert typo_results(correction(replace="platform now"))[0]


def test_a_correction_that_removes_a_word_is_dropped():
    assert typo_results(correction(find="platfrom from", replace="platform"))[0]


def test_a_correction_that_keeps_the_word_count_but_swaps_in_a_new_word_is_dropped():
    assert typo_results(correction(replace="system"))[0]


def test_a_changed_word_within_two_single_character_edits_passes():
    one = correction(paragraph=2, find="teem", replace="team")
    two = correction(paragraph=2, find="recieve", replace="receive")
    assert typo_results(one, two) == [None, None]


def test_a_changed_word_three_single_character_edits_away_is_dropped():
    assert typo_results(correction(paragraph=2, find="teem", replace="crew"))[0]


def test_each_changed_word_of_a_correction_is_held_to_the_limit():
    fixes = correction(
        paragraph=2,
        find="recieve funding across the teem",
        replace="receive funding across the team",
    )
    assert typo_results(fixes) == [None]
    one_bad = correction(
        paragraph=2,
        find="recieve funding across the teem",
        replace="receive funding across the crew",
    )
    assert typo_results(one_bad)[0]


def test_a_correction_may_not_change_a_digit_bearing_word():
    assert typo_results(correction(paragraph=3, find="2O20", replace="2020"))[0]


def test_a_correction_must_still_change_something_and_give_a_reason():
    assert typo_results(correction(replace="platfrom"))[0]
    assert typo_results(correction(reason=""))[0]


def test_a_correction_still_needs_one_unambiguous_place_and_may_not_overlap():
    assert typo_results(correction(find="platfroms", replace="platforms"))[0]
    assert typo_results(correction(paragraph=9))[0]
    first = correction(find="the Quuxlate platfrom", replace="the Quuxlate platform")
    second = correction()
    out = typo_results(first, second)
    assert out[0] is None and out[1]


# --- flag validation (PRD 04, "Validation") --------------------------------------


def flag(**overrides) -> Flag:
    fields = {
        "paragraph": 1,
        "jd_quote": "Own the Quuxlate system end to end",
        "suggestion": "Lead the bullet with owning the whole system",
        "reason": "unsure the role was owner-level; high value if so",
    }
    return Flag(**{**fields, **overrides})


def flag_results(*flags: Flag, paragraphs=PARAGRAPHS, jd=JD):
    return validate_flags(list(flags), paragraphs, jd)


def test_a_flag_that_meets_every_rule_passes():
    assert flag_results(flag()) == [None]


def test_each_flag_result_lines_up_with_its_flag_in_proposal_order():
    out = flag_results(flag(paragraph=99), flag(), flag(paragraph=-1))
    assert len(out) == 3
    assert out[0] and out[1] is None and out[2]


@pytest.mark.parametrize("index", [4, 99, -1])
def test_a_flag_paragraph_outside_the_resume_is_dropped(index):
    assert flag_results(flag(paragraph=index))[0]


def test_the_last_paragraph_index_is_valid_for_a_flag():
    assert flag_results(flag(paragraph=3)) == [None]


def test_a_flag_quote_must_be_a_substring_of_the_job_description():
    assert flag_results(flag(jd_quote="Own the Quuxlate framework"))[0]
    assert flag_results(flag(jd_quote="own the quuxlate system"))[0]


@pytest.mark.parametrize("quote", ["", "   ", "\n"])
def test_an_empty_or_blank_flag_quote_is_dropped(quote):
    assert flag_results(flag(jd_quote=quote))[0]


def test_whitespace_runs_are_collapsed_in_a_flag_quote_and_the_posting():
    quote = "Use PostgreSQL and Machine Learning (ML) every day."
    assert flag_results(flag(jd_quote=quote)) == [None]
    assert flag_results(flag(jd_quote="end to end. Use   PostgreSQL")) == [None]


@pytest.mark.parametrize("field", ["suggestion", "reason"])
@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_suggestion_or_reason_drops_a_flag(field, value):
    assert flag_results(flag(**{field: value}))[0]


def test_a_flag_suggestion_may_use_words_the_posting_and_resume_lack():
    suggestion = "Rewrite the bullet around Kubernetes orchestration"
    assert flag_results(flag(suggestion=suggestion)) == [None]


def test_only_the_first_three_flags_that_pass_are_kept():
    out = flag_results(*[flag() for _ in range(5)])
    assert out[:3] == [None, None, None]
    assert out[3] and out[4]


def test_a_dropped_flag_does_not_use_up_one_of_the_three():
    out = flag_results(flag(paragraph=99), flag(), flag(), flag(), flag())
    assert out[0] and out[1:4] == [None, None, None] and out[4]


def test_every_dropped_flag_carries_a_non_empty_reason():
    out = flag_results(
        flag(paragraph=9),
        flag(jd_quote="not in the posting"),
        flag(jd_quote=""),
        flag(suggestion=""),
        flag(reason=""),
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_an_empty_or_blank_reason_is_dropped(reason):
    assert results(edit(reason=reason))[0]


def test_every_dropped_edit_carries_a_non_empty_reason():
    out = results(
        edit(paragraph=9),
        edit(find="nope"),
        edit(jd_quote="not in the posting"),
        edit(replace="Quuxlate framework"),
        edit(replace=""),
        edit(reason=""),
        edit(goal="style"),
        edit(goal="correction"),
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


# --- the agent's output (PRD 04, "Output contract") ------------------------------


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


def reply_object(edits, explanation=None, flags=()):
    return json.dumps(
        {"edits": list(edits), "flags": list(flags), "explanation": explanation}
    )


def run(tmp_path, stand_in, reply, *, build=None, jd=JD, skills=(), flags=()):
    """`reply` is raw text, or a list of edit dicts sent as `{edits, flags, explanation}`."""
    if isinstance(reply, list):
        reply = reply_object(reply, None if reply or flags else EXPLANATION, flags)
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


def as_dict(proposal: Edit | Flag, **overrides) -> dict:
    return {**asdict(proposal), **overrides}


def record_of(ran):
    return json.loads(ran.edits_file.read_text(encoding="utf-8"))


def test_a_no_proposal_result_records_the_explanation_and_empty_lists(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [])
    assert record_of(ran) == {"explanation": EXPLANATION, "edits": [], "flags": []}
    assert ran.copy.read_bytes() == ran.before
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 0)


def test_a_result_with_edits_records_a_null_explanation(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    record = record_of(ran)
    assert set(record) == {"explanation", "edits", "flags"}
    assert record["explanation"] is None
    assert len(record["edits"]) == 1 and record["flags"] == []


def test_a_result_with_only_a_flag_is_accepted_with_a_null_explanation(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [], flags=[as_dict(flag())])
    record = record_of(ran)
    assert record["explanation"] is None
    assert record["edits"] == [] and len(record["flags"]) == 1


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
        '{"edits": [], "flags": [], "explanation": "x"',
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
def test_text_that_is_not_an_edits_flags_and_explanation_object_raises(
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
        json.dumps({"edits": [], "flags": []}),
        reply_object([], 7),
        reply_object([], "  \n "),
        json.dumps({"explanation": EXPLANATION}),
        json.dumps({"edits": None, "flags": [], "explanation": EXPLANATION}),
        json.dumps({"edits": {}, "flags": [], "explanation": EXPLANATION}),
        reply_object([as_dict(edit())], EXPLANATION),
        reply_object([as_dict(edit())], ""),
        json.dumps({"edits": [as_dict(edit())], "flags": []}),
        reply_object([as_dict(edit())], 7),
        json.dumps({"edits": [], "explanation": EXPLANATION}),
        json.dumps({"edits": [as_dict(edit())], "explanation": None}),
        json.dumps({"edits": [], "flags": None, "explanation": EXPLANATION}),
        json.dumps({"edits": [], "flags": {}, "explanation": EXPLANATION}),
        reply_object([], EXPLANATION, [as_dict(flag())]),
        reply_object([], "", [as_dict(flag())]),
        json.dumps(
            {"edits": [], "skills_edits": [], "flags": [], "explanation": EXPLANATION}
        ),
        json.dumps({"edits": [], "skills_edits": [], "explanation": EXPLANATION}),
        json.dumps(
            {"edits": [], "flags": [], "removals": [], "explanation": EXPLANATION}
        ),
    ],
    ids=[
        "nothing-null-explanation",
        "nothing-empty-explanation",
        "nothing-missing-explanation",
        "nothing-numeric-explanation",
        "nothing-blank-explanation",
        "missing-edits",
        "null-edits",
        "object-edits",
        "edits-with-an-explanation",
        "edits-with-an-empty-explanation",
        "edits-missing-explanation",
        "edits-with-numeric-explanation",
        "no-flags-key",
        "edits-without-the-flags-key",
        "null-flags",
        "object-flags",
        "flags-with-an-explanation",
        "flags-with-an-empty-explanation",
        "an-extra-skills-edits-key",
        "the-old-shape",
        "an-extra-removals-key",
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


EDIT_FIELDS = ["paragraph", "find", "replace", "goal", "jd_quote", "reason"]
FLAG_FIELDS = ["paragraph", "jd_quote", "suggestion", "reason"]


@pytest.mark.parametrize("missing", EDIT_FIELDS)
def test_every_edit_field_is_required_in_the_reply(tmp_path, stand_in, missing):
    copy = make_resume(tmp_path / "resume.docx")
    partial = as_dict(edit())
    del partial[missing]
    stand_in.reply = reply_object([partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize("missing", FLAG_FIELDS)
def test_every_flag_field_is_required_in_the_reply(tmp_path, stand_in, missing):
    copy = make_resume(tmp_path / "resume.docx")
    partial = as_dict(flag())
    del partial[missing]
    stand_in.reply = reply_object([], None, [partial])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


def test_an_edit_in_the_old_shape_raises(tmp_path, stand_in):
    copy = make_resume(tmp_path / "resume.docx")
    old = {
        "paragraph": 1,
        "find": "Quuxlate platform",
        "replace": "Quuxlate system",
        "jd_quote": "Quuxlate system",
        "why_same_meaning": "the posting's name for the same thing",
    }
    stand_in.reply = reply_object([old])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [("jd_quote", 7), ("jd_quote", ["Quuxlate system"]), ("goal", None)],
    ids=["number-quote", "list-quote", "null-goal"],
)
def test_an_edit_field_of_the_wrong_type_raises(tmp_path, stand_in, field, value):
    copy = make_resume(tmp_path / "resume.docx")
    stand_in.reply = reply_object([as_dict(edit(), **{field: value})])
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()


def test_a_correction_with_an_explicit_null_quote_is_recorded_not_rejected(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Built the Quuxlate platfrom.")

    ran = run(tmp_path, stand_in, [as_dict(correction(paragraph=0))], build=build)
    (entry,) = record_of(ran)["edits"]
    assert entry["jd_quote"] is None and entry["validation"] is None
    assert ran.outcome.applied == 1


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


def test_a_wholly_bold_paragraph_is_tagged_bold_in_the_prompt_and_no_other_is(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Plain header ZEBRA")
        document.add_paragraph().add_run("Bold header OTTER").bold = True
        mixed = document.add_paragraph()
        mixed.add_run("Mixed ").bold = True
        mixed.add_run("line WALRUS")

    run(tmp_path, stand_in, [], build=build)
    (call,) = stand_in.calls
    lines = {
        marker: next(line for line in call.prompt.splitlines() if marker in line)
        for marker in ("ZEBRA", "OTTER", "WALRUS")
    }
    assert "bold" in lines["OTTER"].lower()
    assert "bold" not in lines["ZEBRA"].lower()
    assert "bold" not in lines["WALRUS"].lower()
    # The text to copy `find` from is plain: no tag or marker inside it.
    assert "Bold header OTTER" in lines["OTTER"]


# --- redline_edits.json (PRD 04, "Invalid proposals are dropped") ----------------


def test_the_record_lists_every_edit_in_order_with_its_six_fields_and_validation(
    tmp_path, stand_in
):
    good = edit()
    bad = edit(paragraph=99, jd_quote="Quuxlate system")
    ran = run(tmp_path, stand_in, [as_dict(bad), as_dict(good)])
    record = record_of(ran)["edits"]
    assert isinstance(record, list) and len(record) == 2
    fields = set(EDIT_FIELDS)
    for entry, original in zip(record, [bad, good], strict=True):
        assert set(entry) == fields | {"validation"}
        assert {name: entry[name] for name in fields} == as_dict(original)
    assert isinstance(record[0]["validation"], str) and record[0]["validation"]
    assert record[1]["validation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)


def test_the_record_lists_every_flag_in_order_with_its_four_fields_and_validation(
    tmp_path, stand_in
):
    good = flag()
    bad = flag(paragraph=99)
    ran = run(tmp_path, stand_in, [], flags=[as_dict(bad), as_dict(good)])
    record = record_of(ran)["flags"]
    assert len(record) == 2
    fields = set(FLAG_FIELDS)
    for entry, original in zip(record, [bad, good], strict=True):
        assert set(entry) == fields | {"validation"}
        assert {name: entry[name] for name in fields} == as_dict(original)
    assert isinstance(record[0]["validation"], str) and record[0]["validation"]
    assert record[1]["validation"] is None


def test_applied_and_dropped_counts_cover_edits_and_flags_together(tmp_path, stand_in):
    ran = run(
        tmp_path,
        stand_in,
        [as_dict(edit()), as_dict(edit(paragraph=99))],
        flags=[as_dict(flag()), as_dict(flag(paragraph=99)), as_dict(flag(reason=""))],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 3)


def test_dropped_edits_are_absent_from_the_document_but_present_in_the_record(
    tmp_path, stand_in
):
    good = edit()
    bad = edit(
        paragraph=2,
        find="SQL daily",
        replace="Elasticsearch daily",
        jd_quote="Use PostgreSQL and",
        reason="NOT-IN-DOC-MARKER",
    )
    ran = run(tmp_path, stand_in, [as_dict(bad), as_dict(good)])
    with zipfile.ZipFile(ran.copy) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "Elasticsearch" not in body
    assert "NOT-IN-DOC-MARKER" not in comments
    record = record_of(ran)["edits"]
    assert record[0]["replace"] == "Elasticsearch daily"
    assert record[0]["validation"]
    assert record[1]["validation"] is None


def test_the_copy_is_untouched_when_every_proposal_fails_but_the_record_is_written(
    tmp_path, stand_in
):
    ran = run(
        tmp_path,
        stand_in,
        [as_dict(edit(paragraph=99))],
        flags=[as_dict(flag(paragraph=99))],
    )
    assert ran.copy.read_bytes() == ran.before
    record = record_of(ran)
    assert record["explanation"] is None
    assert len(record["edits"]) == 1 and len(record["flags"]) == 1
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 2)
    assert not edits_applied(ran.edits_file)


def test_the_record_is_written_after_the_copy_is_replaced(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit())])
    assert ran.copy.stat().st_mtime_ns <= ran.edits_file.stat().st_mtime_ns


# --- reading a record back (PRD 04, "Re-entry") ----------------------------------


def write_record(tmp_path, record):
    path = tmp_path / "redline_edits.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def entry(validation):
    return {**as_dict(edit()), "validation": validation}


def flag_entry(validation):
    return {**as_dict(flag()), "validation": validation}


@pytest.mark.parametrize(
    ("edits", "flags", "applied"),
    [
        ([], [], False),
        ([entry("dropped")], [flag_entry("dropped")], False),
        ([entry(None)], [], True),
        ([], [flag_entry(None)], True),
        ([entry("dropped")], [flag_entry(None)], True),
        ([entry(None), entry("dropped")], [], True),
    ],
    ids=[
        "nothing",
        "all-dropped",
        "an-edit-applied",
        "a-flag-applied",
        "only-the-flag-applied",
        "one-of-two-edits-applied",
    ],
)
def test_the_record_says_whether_any_edit_or_flag_was_applied(
    tmp_path, edits, flags, applied
):
    path = write_record(tmp_path, {"explanation": None, "edits": edits, "flags": flags})
    assert edits_applied(path) is applied


@pytest.mark.parametrize(
    "record",
    [
        {"explanation": None, "edits": [entry(None)], "skills_edits": []},
        {"explanation": None, "edits": [], "skills_edits": [entry(None)]},
        {"explanation": None, "edits": [entry(None)]},
        {"edits": [entry(None)], "flags": []},
        {"explanation": None, "edits": [], "flags": [], "skills_edits": []},
        [entry(None)],
        [],
        "applied",
        None,
    ],
    ids=[
        "the-previous-shape",
        "previous-shape-with-a-skills-edit-applied",
        "no-flags",
        "no-explanation",
        "an-extra-key",
        "a-bare-array",
        "an-empty-array",
        "a-string",
        "null",
    ],
)
def test_a_record_in_any_other_shape_raises_instead_of_being_read(tmp_path, record):
    path = write_record(tmp_path, record)
    with pytest.raises(JsaError, match="is not a redline record"):
        edits_applied(path)


def test_a_record_that_is_not_json_raises(tmp_path):
    path = tmp_path / "redline_edits.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(JsaError):
        edits_applied(path)


# --- the redline document (PRD 04, "Written into the resume copy") --------------


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
        reason="the posting's longer name for the same work",
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


def test_every_tracked_change_is_authored_claude_redline_and_dated_now(
    tmp_path, stand_in
):
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
        [as_dict(edit(reason="WHY-MARKER-ONE"))],
    )
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) >= 1
    for comment in comments:
        text = "".join(comment.itertext())
        assert "ats" in text.lower()
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
        reason="MULTI-WHY",
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
        reason="THIRD-WHY",
    )
    second = edit(
        paragraph=0,
        find="ran Zorblat jobs",
        replace="ran Zorblat pipelines",
        jd_quote="Zorblat pipelines",
        reason="SECOND-WHY",
    )
    first = edit(
        paragraph=0,
        find="Built Quuxlate platform fast",
        replace="Constructed Quuxlate system fast",
        jd_quote="Constructed the Quuxlate system",
        reason="FIRST-WHY",
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


def comment_texts(path):
    return [
        "".join(comment.itertext())
        for comment in comments_of(path).findall("w:comment", NS)
    ]


def test_a_whole_paragraph_find_still_deletes_and_inserts_only_the_differing_words(
    tmp_path, stand_in
):
    whole = edit(
        paragraph=0,
        find=LONG,
        replace=LONG.replace("platform", "system"),
        reason="WHOLE-PARAGRAPH-MARKER",
    )

    def build(document):
        document.add_paragraph(LONG)

    ran = run(tmp_path, stand_in, [as_dict(whole)], build=build)
    assert ran.outcome.applied == 1
    body = body_of(ran.copy)
    deleted = "".join(body.xpath("//w:del//w:delText/text()", namespaces=NS))
    inserted = "".join(body.xpath("//w:ins//w:t/text()", namespaces=NS))
    assert (deleted.strip(), inserted.strip()) == ("platform", "system")
    assert view_of(ran.copy, "reject") == [LONG]
    assert view_of(ran.copy, "accept") == [LONG.replace("platform", "system")]


def test_a_narrative_edits_comment_names_its_goal(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [as_dict(edit(goal="narrative"))])
    (text,) = comment_texts(ran.copy)
    assert "narrative" in text.lower() and "Quuxlate system" in text


def test_a_corrections_comment_names_its_goal_gives_its_reason_and_quotes_nothing(
    tmp_path, stand_in
):
    def build(document):
        document.add_paragraph("Built the Quuxlate platfrom from scratch.")

    ran = run(
        tmp_path,
        stand_in,
        [as_dict(correction(paragraph=0, reason="CORRECTION-REASON-MARKER"))],
        build=build,
    )
    assert ran.outcome.applied == 1
    (text,) = comment_texts(ran.copy)
    assert "correction" in text.lower()
    assert "CORRECTION-REASON-MARKER" in text
    assert "None" not in text and "null" not in text
    assert view_of(ran.copy, "reject") == ["Built the Quuxlate platfrom from scratch."]
    assert view_of(ran.copy, "accept") == ["Built the Quuxlate platform from scratch."]


# --- flags are comments only (PRD 04, "Written into the resume copy") -------------


def flag_run(tmp_path, stand_in, *flags, edits=(), **kwargs):
    return run(
        tmp_path,
        stand_in,
        [as_dict(e) for e in edits],
        flags=[as_dict(f) for f in flags],
        **kwargs,
    )


def test_a_flag_changes_no_text_and_adds_no_tracked_change(tmp_path, stand_in):
    ran = flag_run(tmp_path, stand_in, flag())
    assert ran.outcome.applied == 1
    assert view_of(ran.copy, "accept") == PARAGRAPHS
    assert view_of(ran.copy, "reject") == PARAGRAPHS
    body = body_of(ran.copy)
    assert not body.xpath("//w:ins | //w:del", namespaces=NS)


def test_an_applied_flag_replaces_the_copy_and_leaves_it_with_a_comment(
    tmp_path, stand_in
):
    ran = flag_run(tmp_path, stand_in, flag())
    assert ran.copy.read_bytes() != ran.before
    assert len(comment_texts(ran.copy)) == 1
    assert edits_applied(ran.edits_file)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "original.docx",
        "redline_edits.json",
        "resume.docx",
    ]
    assert len(docx.Document(str(ran.copy)).paragraphs) == len(PARAGRAPHS)


def test_a_flags_comment_says_it_is_a_decision_for_the_user_and_gives_its_parts(
    tmp_path, stand_in
):
    ran = flag_run(
        tmp_path,
        stand_in,
        flag(suggestion="SUGGESTION-MARKER", reason="FLAG-REASON-MARKER"),
    )
    (text,) = comment_texts(ran.copy)
    lowered = text.lower()
    assert "decision" in lowered or "decide" in lowered
    assert "SUGGESTION-MARKER" in text
    assert "Own the Quuxlate system end to end" in text
    assert "FLAG-REASON-MARKER" in text


def test_a_flags_comment_is_anchored_on_its_own_paragraph(tmp_path, stand_in):
    ran = flag_run(tmp_path, stand_in, flag(paragraph=2))
    body = body_of(ran.copy).find("w:body", NS)
    paragraphs = body.findall("w:p", NS)
    for index, paragraph in enumerate(paragraphs):
        anchors = paragraph.xpath(
            ".//w:commentRangeStart | .//w:commentRangeEnd | .//w:commentReference",
            namespaces=NS,
        )
        assert bool(anchors) is (index == 2)
    (comment,) = comments_of(ran.copy).findall("w:comment", NS)
    comment_id = comment.get(f"{{{W}}}id")
    for marker in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        assert paragraphs[2].xpath(f".//w:{marker}/@w:id", namespaces=NS) == [
            comment_id
        ]


def test_each_applied_flag_gets_one_comment_with_a_unique_id(tmp_path, stand_in):
    ran = flag_run(
        tmp_path,
        stand_in,
        flag(paragraph=1, suggestion="FIRST-FLAG"),
        flag(paragraph=2, suggestion="SECOND-FLAG"),
        flag(paragraph=3, suggestion="THIRD-FLAG"),
    )
    assert ran.outcome.applied == 3
    comments = comments_of(ran.copy).findall("w:comment", NS)
    ids = [comment.get(f"{{{W}}}id") for comment in comments]
    assert len(comments) == 3 and len(set(ids)) == 3
    body = body_of(ran.copy)
    assert sorted(body.xpath("//w:commentReference/@w:id", namespaces=NS)) == sorted(
        ids
    )
    for index, marker in ((1, "FIRST-FLAG"), (2, "SECOND-FLAG"), (3, "THIRD-FLAG")):
        (comment,) = [c for c in comments if marker in "".join(c.itertext())]
        paragraph = body.find("w:body", NS).findall("w:p", NS)[index]
        assert paragraph.xpath(".//w:commentReference/@w:id", namespaces=NS) == [
            comment.get(f"{{{W}}}id")
        ]


def test_only_the_first_three_passing_flags_are_written(tmp_path, stand_in):
    flags = [flag(paragraph=99)] + [flag(suggestion=f"FLAG-{n}") for n in range(1, 6)]
    ran = flag_run(tmp_path, stand_in, *flags)
    assert (ran.outcome.applied, ran.outcome.dropped) == (3, 3)
    texts = " ".join(comment_texts(ran.copy))
    assert all(f"FLAG-{n}" in texts for n in (1, 2, 3))
    assert "FLAG-4" not in texts and "FLAG-5" not in texts
    validations = [entry["validation"] for entry in record_of(ran)["flags"]]
    assert [v is None for v in validations] == [False, True, True, True, False, False]
    assert all(isinstance(v, str) and v for v in validations if v is not None)


def test_a_dropped_flag_leaves_no_comment(tmp_path, stand_in):
    ran = flag_run(tmp_path, stand_in, flag(jd_quote="not in the posting"))
    assert ran.copy.read_bytes() == ran.before
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)


def test_an_edit_and_a_flag_on_the_same_paragraph_each_keep_their_own_comment(
    tmp_path, stand_in
):
    ran = flag_run(
        tmp_path,
        stand_in,
        flag(paragraph=1, suggestion="FLAG-SAME-PARAGRAPH"),
        edits=[edit(reason="EDIT-SAME-PARAGRAPH")],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 0)
    texts = comment_texts(ran.copy)
    assert len(texts) == 2
    assert any("EDIT-SAME-PARAGRAPH" in t for t in texts)
    assert any("FLAG-SAME-PARAGRAPH" in t for t in texts)
    ids = comments_of(ran.copy).findall("w:comment", NS)
    assert len({c.get(f"{{{W}}}id") for c in ids}) == 2
    assert view_of(ran.copy, "reject") == PARAGRAPHS
    expected = list(PARAGRAPHS)
    expected[1] = "Built the Quuxlate system from scratch."
    assert view_of(ran.copy, "accept") == expected


def test_a_flag_on_a_paragraph_the_edits_do_not_touch_leaves_the_edits_unchanged(
    tmp_path, stand_in
):
    ran = flag_run(tmp_path, stand_in, flag(paragraph=3), edits=[edit()])
    body = body_of(ran.copy)
    assert "".join(body.xpath("//w:ins//w:t/text()", namespaces=NS)).strip() == "system"
    assert "".join(body.xpath("//w:del//w:delText/text()", namespaces=NS)).strip() == (
        "platform"
    )
    assert len(comment_texts(ran.copy)) == 2


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


# --- editing the skills section (PRD 04 "Skills section", "Validation") --------------

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


def skills_edit(**overrides) -> Edit:
    fields = {
        "paragraph": 3,
        "find": "Looker",
        "replace": "Looker, Airflow",
        "goal": "ats",
        "jd_quote": "Airflow pipelines",
        "reason": "the posting names Airflow",
    }
    return Edit(**{**fields, **overrides})


def skills_results(
    *edits, paragraphs=SK_PARAS, jd=SK_JD, skills=SK_LIST, section=SK_SECTION
):
    return validate_edits(
        list(edits), paragraphs, jd, skills=skills, skills_section=section
    )


def test_a_skills_edit_that_meets_every_rule_passes():
    assert skills_results(skills_edit()) == [None]


def test_each_skills_result_lines_up_with_its_edit_in_proposal_order():
    out = skills_results(
        skills_edit(paragraph=99), skills_edit(), skills_edit(goal="style")
    )
    assert len(out) == 3
    assert out[0] and out[1] is None and out[2]


def test_every_dropped_skills_edit_carries_a_non_empty_reason():
    out = skills_results(
        skills_edit(paragraph=99),
        skills_edit(find="Nope"),
        skills_edit(goal="style"),
        skills_edit(reason=""),
        skills_edit(replace="Looker"),
        skills_edit(jd_quote=None),
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


@pytest.mark.parametrize(
    ("paragraph", "latitude"),
    [(0, False), (1, False), (2, True), (3, True), (4, False), (5, False)],
    ids=["name", "heading", "first", "last", "next-heading", "after-section"],
)
def test_the_skills_latitude_applies_by_paragraph_inside_the_section_only(
    paragraph, latitude
):
    # Every paragraph holds "Looker", so only the section's rules decide the result.
    paragraphs = [
        "Looker",
        "Looker",
        "Excel, Looker",
        "Excel, Looker",
        "Looker",
        "Looker",
    ]
    # "Tableau" is on the list but in neither the quote nor the find.
    adds_a_listed_skill = skills_edit(
        paragraph=paragraph, replace="Looker, Tableau", jd_quote="Airflow pipelines"
    )
    removes_a_skill = skills_edit(
        paragraph=paragraph, find="Looker", replace="", jd_quote="Airflow pipelines"
    )
    for proposal in (adds_a_listed_skill, removes_a_skill):
        (result,) = skills_results(proposal, paragraphs=paragraphs)
        assert (result is None) is latitude


@pytest.mark.parametrize("paragraph", [-1, 6, 99])
def test_a_skills_edit_outside_the_document_is_dropped(paragraph):
    (result,) = skills_results(skills_edit(paragraph=paragraph))
    assert result


@pytest.mark.parametrize("skills", [[], ()], ids=["empty-list", "empty-tuple"])
def test_with_no_skills_list_in_use_the_skills_paragraphs_are_held_to_the_ordinary_rules(
    skills,
):
    listed_word = skills_edit(replace="Looker, Tableau")
    removal = skills_edit(replace="")
    number = skills_edit(
        paragraph=2, find="Python", replace="Python 4", jd_quote="Python 4"
    )
    out = skills_results(listed_word, removal, number, skills=skills, section=())
    assert all(isinstance(reason, str) and reason.strip() for reason in out)
    assert len(out) == 3


def test_with_no_skills_section_the_ordinary_rules_apply_to_every_paragraph():
    out = skills_results(skills_edit(replace="Looker, Tableau"), section=())
    assert out[0]
    assert skills_results(skills_edit(), section=()) == [None]


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
    dropped = skills_edit(paragraph=2, find="Python, SQL", replace="Python", reason="")
    second = skills_edit(paragraph=2, find="SQL | Notion", replace="Notion")
    out = skills_results(dropped, second)
    assert out[0] and out[1] is None


def test_skills_edits_in_one_paragraph_that_do_not_overlap_both_pass():
    first = skills_edit(paragraph=2, find="Python,", replace="")
    second = skills_edit(paragraph=2, find="Notion", replace="")
    assert skills_results(first, second) == [None, None]


def test_skills_edit_overlap_is_per_paragraph():
    first = skills_edit(paragraph=3, find="Looker", replace="")
    second = skills_edit(paragraph=2, find="Python", replace="")
    assert skills_results(first, second) == [None, None]


def test_replace_must_differ_from_find_for_a_skills_edit():
    (result,) = skills_results(skills_edit(replace="Looker"))
    assert result


def test_removing_a_skill_with_an_empty_replace_is_allowed_in_the_section():
    assert skills_results(skills_edit(find="Excel, ", replace="")) == [None]


def test_an_empty_replace_is_dropped_outside_the_section():
    edit_ = edit(paragraph=5, find="platform ", replace="", jd_quote="Quuxlate system")
    assert validate_edits(
        [edit_], SK_PARAS, SK_JD, skills=SK_LIST, skills_section=SK_SECTION
    )[0]


def test_a_shorter_replace_is_allowed_in_the_section():
    assert skills_results(
        skills_edit(paragraph=2, find="Python, SQL", replace="SQL")
    ) == [None]


@pytest.mark.parametrize("goal", ["ats", "narrative"])
def test_both_goals_are_accepted_in_the_section(goal):
    assert skills_results(skills_edit(goal=goal)) == [None]


@pytest.mark.parametrize("goal", ["style", "", "both", None, "ATS", 1])
def test_a_goal_that_is_not_one_of_the_three_values_is_dropped_in_the_section(goal):
    (result,) = skills_results(skills_edit(goal=goal))
    assert result


@pytest.mark.parametrize("goal", ["ats", "narrative"])
@pytest.mark.parametrize("quote", [None, "", "   "])
def test_an_ats_or_narrative_skills_edit_needs_a_posting_quote_even_if_every_word_is_listed(
    goal, quote
):
    (result,) = skills_results(
        skills_edit(goal=goal, jd_quote=quote, replace="Looker, Tableau")
    )
    assert result


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
    assert skills_results(skills_edit(replace="Looker Tableau")) == [None]


def test_an_inserted_word_in_the_quote_passes_without_being_on_the_list():
    assert skills_results(skills_edit()) == [None]


def test_an_inserted_word_in_find_passes_without_being_on_the_list():
    paragraphs = [*SK_PARAS[:3], "Excel, Looker, Rust", *SK_PARAS[4:]]
    edit_ = skills_edit(find="Excel, Looker, Rust", replace="Rust, Excel, Looker")
    assert skills_results(edit_, paragraphs=paragraphs) == [None]


def test_an_inserted_word_on_the_list_comparison_ignores_case_and_edge_punctuation():
    assert skills_results(skills_edit(replace="Looker, (tableau)")) == [None]


def test_an_inserted_word_found_nowhere_is_dropped():
    (result,) = skills_results(skills_edit(replace="Looker, Rust"))
    assert result


def test_a_word_in_the_posting_but_not_in_the_cited_quote_or_list_is_dropped():
    (result,) = skills_results(
        skills_edit(replace="Looker, Airflow", jd_quote="Looker Studio")
    )
    assert result


def test_a_word_on_the_list_in_another_form_is_not_a_match():
    (result,) = skills_results(skills_edit(replace="Looker, Tableaus"))
    assert result


def test_one_bad_inserted_word_drops_an_edit_whose_other_words_are_fine():
    (result,) = skills_results(skills_edit(replace="Looker, Tableau, Rust"))
    assert result


@pytest.mark.parametrize("separator", ["|", "•", "-", "/", "·"])
def test_a_separator_with_no_letter_or_digit_is_never_an_inserted_word(separator):
    assert skills_results(
        skills_edit(paragraph=2, find="Python, SQL", replace=f"Python, {separator} SQL")
    ) == [None]


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_an_empty_or_blank_reason_is_dropped_in_the_section(reason):
    (result,) = skills_results(skills_edit(reason=reason))
    assert result


def test_there_is_no_length_limit_on_a_skills_edits_find():
    paragraphs = [
        *SK_PARAS[:2],
        "Python, SQL, Notion, Excel, Looker, Tableau, Airflow, dbt",
        *SK_PARAS[3:],
    ]
    long = skills_edit(
        paragraph=2,
        find="Python, SQL, Notion, Excel, Looker, Tableau, Airflow, dbt",
        replace="Python, SQL",
    )
    assert skills_results(long, paragraphs=paragraphs) == [None]


def test_the_number_rule_does_not_apply_in_the_section_but_does_outside_it():
    paragraphs = [*SK_PARAS[:2], "Python 3, SQL", *SK_PARAS[3:]]
    changed = skills_edit(
        paragraph=2, find="Python 3", replace="Python 4", jd_quote="Python 4"
    )
    dropped = skills_edit(paragraph=2, find="SQL", replace="")
    assert skills_results(changed, dropped, paragraphs=paragraphs) == [None, None]
    outside = ["Used Python 3 daily", *SK_PARAS[1:]]
    renumbered = edit(
        paragraph=0, find="Python 3", replace="Python 4", jd_quote="Python 4"
    )
    assert skills_results(renumbered, paragraphs=outside)[0]


def test_a_number_inserted_into_a_skills_edit_still_has_to_come_from_the_list_quote_or_find():
    paragraphs = [*SK_PARAS[:2], "Python 3, SQL", *SK_PARAS[3:]]
    (result,) = skills_results(
        skills_edit(
            paragraph=2, find="Python 3", replace="Python 5", jd_quote="Python 4"
        ),
        paragraphs=paragraphs,
    )
    assert result


def test_a_correction_in_the_section_is_still_a_correction():
    paragraphs = [*SK_PARAS[:2], "Pyhton, SQL | Notion", *SK_PARAS[3:]]
    fix = skills_edit(
        paragraph=2, find="Pyhton", replace="Python", goal="correction", jd_quote=None
    )
    swap = skills_edit(
        paragraph=2, find="Pyhton", replace="Tableau", goal="correction", jd_quote=None
    )
    assert skills_results(fix, swap, paragraphs=paragraphs)[0] is None
    assert skills_results(fix, swap, paragraphs=paragraphs)[1]


# --- the same edit, ordinary paragraph or skills paragraph --------------------------


def test_an_edit_outside_the_skills_section_is_unaffected_by_it():
    outside = edit(paragraph=5)
    assert validate_edits(
        [outside], SK_PARAS, SK_JD, skills=SK_LIST, skills_section=SK_SECTION
    ) == [None]


def test_the_ordinary_rules_still_apply_to_the_paragraphs_around_a_skills_section():
    unquoted = edit(paragraph=5, jd_quote="text that is not in the posting")
    (result,) = validate_edits(
        [unquoted], SK_PARAS, SK_JD, skills=SK_LIST, skills_section=SK_SECTION
    )
    assert result
    listed = edit(paragraph=5, replace="Quuxlate Tableau")
    (result,) = validate_edits(
        [listed], SK_PARAS, SK_JD, skills=SK_LIST, skills_section=SK_SECTION
    )
    assert result


# --- the redline document with a skills list -----------------------------------------


def skills_resume(document):
    for text in SK_PARAS[:4]:
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
    tmp_path, stand_in, *, edits=(), flags=(), build=skills_resume, skills=SK_LIST
):
    reply = reply_object(
        [as_dict(e) for e in edits],
        None if edits or flags else EXPLANATION,
        [as_dict(f) for f in flags],
    )
    return run(tmp_path, stand_in, reply, build=build, jd=SK_JD, skills=skills)


def test_a_passing_skills_edit_is_a_tracked_change_that_accepts_and_rejects_cleanly(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit()])
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 0)
    assert view_of(ran.copy, "reject") == SK_PARAS
    accepted = view_of(ran.copy, "accept")
    assert accepted[3] == "Excel, Looker, Airflow"
    assert [p for i, p in enumerate(accepted) if i != 3] == [
        p for i, p in enumerate(SK_PARAS) if i != 3
    ]
    body = body_of(ran.copy)
    revisions = body.xpath("//w:ins | //w:del", namespaces=NS)
    assert revisions
    assert {node.get(f"{{{W}}}author") for node in revisions} == {AUTHOR}


def test_a_skill_removal_is_written_as_a_tracked_deletion(tmp_path, stand_in):
    removal = skills_edit(find="Excel, ", replace="")
    ran = run_skills(tmp_path, stand_in, edits=[removal])
    assert ran.outcome.applied == 1
    assert view_of(ran.copy, "accept")[3] == "Looker"
    assert view_of(ran.copy, "reject")[3] == "Excel, Looker"


def test_a_skills_edits_comment_names_the_goal_quotes_the_posting_and_gives_the_reason(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit(reason="SK-REASON-MARKER")])
    (text,) = comment_texts(ran.copy)
    assert "ats" in text.lower()
    assert "Airflow pipelines" in text
    assert "SK-REASON-MARKER" in text


def test_a_narrative_skills_edits_comment_names_its_goal(tmp_path, stand_in):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit(goal="narrative")])
    (text,) = comment_texts(ran.copy)
    assert "narrative" in text.lower() and "Airflow pipelines" in text


def test_a_skills_edit_and_an_ordinary_edit_each_get_a_comment(tmp_path, stand_in):
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[
            edit(paragraph=5, reason="ORD-WHY-MARKER"),
            skills_edit(reason="SK-WHY-MARKER"),
        ],
    )
    texts = comment_texts(ran.copy)
    assert len(texts) == 2
    assert any("ORD-WHY-MARKER" in t for t in texts)
    assert any("SK-WHY-MARKER" in t for t in texts)
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 0)


def test_the_record_holds_skills_paragraph_edits_among_the_edits(tmp_path, stand_in):
    good_skill = skills_edit()
    bad_skill = skills_edit(replace="Looker, Rust")
    ran = run_skills(
        tmp_path,
        stand_in,
        edits=[edit(paragraph=5), bad_skill, good_skill],
    )
    record = record_of(ran)
    assert set(record) == {"explanation", "edits", "flags"}
    assert record["explanation"] is None
    assert [e["validation"] is None for e in record["edits"]] == [True, False, True]
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 1)


def test_a_dropped_skills_edit_is_in_the_record_and_absent_from_the_document(
    tmp_path, stand_in
):
    bad = skills_edit(replace="Looker, Rust", reason="DROPPED-SK-MARKER")
    ran = run_skills(tmp_path, stand_in, edits=[edit(paragraph=5), bad])
    with zipfile.ZipFile(ran.copy) as archive:
        body = archive.read("word/document.xml").decode("utf-8")
        comments = archive.read("word/comments.xml").decode("utf-8")
    assert "Rust" not in body and "DROPPED-SK-MARKER" not in comments
    assert record_of(ran)["edits"][1]["validation"]


def test_a_copy_whose_every_skills_edit_failed_is_untouched_but_the_record_is_written(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit(replace="Looker, Rust")])
    assert ran.copy.read_bytes() == ran.before
    assert record_of(ran)["explanation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert not edits_applied(ran.edits_file)


def test_an_applied_skills_edit_alone_marks_the_redline_as_having_applied(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit()])
    assert edits_applied(ran.edits_file)


def test_with_no_skills_list_the_skills_paragraphs_are_held_to_the_ordinary_rules(
    tmp_path, stand_in
):
    listed_word = skills_edit(replace="Looker, Tableau")
    ordinary = skills_edit(replace="Looker Studio", jd_quote="Looker Studio")
    ran = run_skills(tmp_path, stand_in, edits=[listed_word, ordinary], skills=())
    record = record_of(ran)
    assert record["edits"][0]["validation"]
    assert record["edits"][1]["validation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)
    assert view_of(ran.copy, "accept")[3] == "Excel, Looker Studio"


def test_with_a_skills_list_the_same_listed_word_passes_in_the_section(
    tmp_path, stand_in
):
    ran = run_skills(tmp_path, stand_in, edits=[skills_edit(replace="Looker, Tableau")])
    assert ran.outcome.applied == 1
    assert view_of(ran.copy, "accept")[3] == "Excel, Looker, Tableau"


def test_a_listed_word_outside_the_section_is_dropped_even_with_a_list(
    tmp_path, stand_in
):
    outside = edit(paragraph=5, replace="Quuxlate Tableau")
    ran = run_skills(tmp_path, stand_in, edits=[outside])
    assert record_of(ran)["edits"][0]["validation"]
    assert ran.copy.read_bytes() == ran.before


def test_a_resume_with_no_skills_paragraph_has_no_skills_section_and_ordinary_rules_apply_everywhere(
    tmp_path, stand_in
):
    listed = edit(
        paragraph=2,
        find="SQL daily",
        replace="SQL daily, Tableau",
        jd_quote="Use PostgreSQL",
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
        [as_dict(listed), as_dict(ordinary)],
        jd=SK_JD,
        skills=SK_LIST,
    )
    record = record_of(ran)
    assert record["edits"][0]["validation"]
    assert record["edits"][1]["validation"] is None
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)


def test_a_resume_with_two_skills_paragraphs_has_no_skills_section(tmp_path, stand_in):
    listed = skills_edit(paragraph=4, find="Looker", replace="Looker, Tableau")
    ran = run_skills(tmp_path, stand_in, edits=[listed], build=two_skills_resume)
    assert record_of(ran)["edits"][0]["validation"]
    assert ran.copy.read_bytes() == ran.before


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


def test_the_template_states_the_goals_the_fields_the_output_keys_and_the_flag_cap(
    tmp_path, stand_in
):
    run_skills(tmp_path, stand_in)
    (call,) = stand_in.calls
    lowered = call.prompt.lower()
    for term in (
        "edits",
        "flags",
        "explanation",
        "paragraph",
        "find",
        "replace",
        "goal",
        "ats",
        "narrative",
        "correction",
        "jd_quote",
        "reason",
        "suggestion",
        "headline",
        "summary",
        "skills",
    ):
        assert term in lowered, term
    assert lowered.index("ats") < lowered.index("narrative")
    assert re.search(r"\bthree\b|\b3\b", lowered)


def test_the_template_has_none_of_the_removed_or_later_contract(tmp_path, stand_in):
    run_skills(tmp_path, stand_in)
    (call,) = stand_in.calls
    lowered = call.prompt.lower()
    for term in (
        "skills_edits",
        "why_same_meaning",
        "checklist",
        "removals",
        "reorders",
    ):
        assert term not in lowered, term
    assert "{{" not in call.prompt and "}}" not in call.prompt


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
