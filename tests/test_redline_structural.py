"""The redline's removals and reorders: bullets, validation, tracked deletions and moves (issue #153; PRD 04 "Redline").

The validators and the bullet definition are pure. The document tests drive `redline_resume` with Claude
replaced at the shared agent loop, then read the written .docx as Word would: accepting or rejecting every
tracked change is computed from the document XML, paragraph marks included.
"""

import json
import re
import zipfile
from types import SimpleNamespace

import docx
import pytest
from lxml import etree
from test_claude_runner import result_message
from test_redline import (
    AUTHOR,
    EXPLANATION,
    JD,
    NS,
    SETTINGS,
    W,
    as_dict,
    bold_paragraph,
    comments_of,
    edit,
    make_resume,
    record_of,
    reply_object,
    run,
)

from jsa import agent_loop
from jsa.errors import JsaError
from jsa.redline import (
    Flag,
    Removal,
    Reorder,
    assemble_redline_prompt,
    redline_resume,
    validate_edits,
    validate_flags,
    validate_removals,
    validate_reorders,
)
from jsa.redline_docx import bullet_paragraphs, is_bullet

QUOTE = "Quuxlate system"


@pytest.fixture
def stand_in(monkeypatch):
    state = SimpleNamespace(reply=reply_object([], EXPLANATION), calls=[])

    async def query(*, prompt, options=None, **_ignored):
        state.calls.append(SimpleNamespace(prompt=prompt, options=options))
        yield result_message(result=state.reply)

    monkeypatch.setattr(agent_loop, "query", query)
    return state


# --- the bullet definition (PRD 04, "Redline", Bullets) --------------------------


def numbered(paragraph, num_id):
    properties = paragraph._p.get_or_add_pPr()
    num_pr = etree.SubElement(properties, f"{{{W}}}numPr")
    etree.SubElement(num_pr, f"{{{W}}}ilvl").set(f"{{{W}}}val", "0")
    etree.SubElement(num_pr, f"{{{W}}}numId").set(f"{{{W}}}val", str(num_id))


def test_a_paragraph_in_a_word_list_style_is_a_bullet():
    document = docx.Document()
    paragraph = document.add_paragraph("Built things.", style="List Bullet")
    assert is_bullet(paragraph)


def test_a_paragraph_with_its_own_list_setting_is_a_bullet():
    document = docx.Document()
    paragraph = document.add_paragraph("Built things.")
    numbered(paragraph, 1)
    assert is_bullet(paragraph)


def test_a_paragraph_whose_list_setting_is_zero_is_not_a_bullet():
    document = docx.Document()
    paragraph = document.add_paragraph("Built things.", style="List Bullet")
    numbered(paragraph, 0)
    assert not is_bullet(paragraph)


@pytest.mark.parametrize("character", ["•", "●", "▪"])
def test_a_paragraph_that_begins_with_a_bullet_character_is_a_bullet(character):
    document = docx.Document()
    assert is_bullet(document.add_paragraph(f"{character} Built things."))


def test_a_plain_paragraph_is_not_a_bullet_and_a_bullet_character_inside_the_text_does_not_count():
    document = docx.Document()
    assert not is_bullet(document.add_paragraph("Built things."))
    assert not is_bullet(document.add_paragraph("Built • things."))
    assert not is_bullet(document.add_paragraph(""))


def test_bullet_paragraphs_gives_the_indices_of_the_body_bullets_only():
    document = docx.Document()
    document.add_paragraph("Header")
    document.add_paragraph("one", style="List Bullet")
    document.add_paragraph("• two")
    document.add_paragraph("")
    document.add_paragraph("three", style="List Bullet")
    table = document.add_table(rows=1, cols=1)
    table.cell(0, 0).paragraphs[0].style = "List Bullet"
    table.cell(0, 0).paragraphs[0].add_run("in a table")
    assert bullet_paragraphs(document) == {1, 2, 4}


# --- the layout the validator and document tests share ----------------------------
#
#  0 Riley Resumeperson (bold)         6 bullet  Ran the Frobnicator rollout.
#  1 Fabrikam Logistics (bold)         7 bullet  Wrote weekly stakeholder reports.
#  2 bullet  Built the Quuxlate...     8 Skills (bold)
#  3 bullet  Led data work...          9, 10, 11 the skills lines
#  4 bullet  Reduced costs...          12 Education (bold)
#  5 Northwind Traders (bold)          13 BS in Statistics
BULLETS = {2, 3, 4, 6, 7}
SKILLS_LINES = {9, 10, 11}
SKILLS = ["SQL", "Excel", "Python"]
TEXTS = [
    "Riley Resumeperson",
    "Fabrikam Logistics",
    "Built the Quuxlate platform from scratch.",
    "Led data work using SQL daily.",
    "Reduced costs by 30% in 2020.",
    "Northwind Traders",
    "Ran the Frobnicator rollout.",
    "Wrote weekly stakeholder reports.",
    "Skills",
    "SQL, Excel",
    "Python, R",
    "Tableau, Looker",
    "Education",
    "BS in Statistics",
]


def build(document):
    for index, text in enumerate(TEXTS):
        if index in BULLETS:
            document.add_paragraph(text, style="List Bullet")
        elif index in (0, 1, 5, 8, 12):
            bold_paragraph(document, text)
        else:
            document.add_paragraph(text)


def removal(paragraph, **overrides):
    return Removal(
        **{
            "paragraph": paragraph,
            "jd_quote": QUOTE,
            "reason": "it detracts",
            **overrides,
        }
    )


def reorder(*paragraphs, **overrides):
    fields = {
        "paragraphs": list(paragraphs),
        "jd_quote": QUOTE,
        "reason": "leads with it",
    }
    return Reorder(**{**fields, **overrides})


def removals_of(*removals, bullets=BULLETS, section=()):
    return validate_removals(list(removals), JD, bullets, section)


def reorders_of(*reorders, bullets=BULLETS, section=(), removed=()):
    return validate_reorders(list(reorders), JD, bullets, section, removed)


def passes(results):
    return [result is None for result in results]


# --- removal validation (PRD 04, "Validation") -------------------------------------


def test_a_removal_that_meets_every_rule_passes():
    assert removals_of(removal(3)) == [None]


def test_each_removal_result_lines_up_with_its_removal_in_proposal_order():
    out = removals_of(removal(3), removal(1), removal(6))
    assert passes(out) == [True, False, True]


@pytest.mark.parametrize("paragraph", [0, 1, 5, 8, 12, 99, -1])
def test_a_removal_of_a_paragraph_that_is_not_a_bullet_is_dropped(paragraph):
    assert isinstance(removals_of(removal(paragraph))[0], str)


def test_a_removal_in_the_skills_section_is_dropped_even_if_it_is_a_bullet():
    out = removals_of(
        removal(10),
        bullets=BULLETS | {10, 11},
        section=SKILLS_LINES,
    )
    assert isinstance(out[0], str)


def test_a_bullet_just_outside_the_skills_section_can_still_be_removed():
    assert removals_of(removal(7), section=SKILLS_LINES) == [None]


@pytest.mark.parametrize(
    "quote",
    ["not in the posting", "", "   ", "quuxlate system"],
    ids=["absent", "empty", "blank", "wrong-case"],
)
def test_a_removal_needs_a_valid_posting_quote(quote):
    assert isinstance(removals_of(removal(3, jd_quote=quote))[0], str)


def test_whitespace_runs_are_collapsed_in_a_removals_quote_and_the_posting():
    assert removals_of(removal(3, jd_quote="data   engineering\nwork")) == [None]


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_a_removal_needs_a_reason(reason):
    assert isinstance(removals_of(removal(3, reason=reason))[0], str)


def test_a_removal_already_removed_by_an_earlier_removal_is_dropped():
    out = removals_of(removal(3), removal(3))
    assert passes(out) == [True, False]
    assert out[1].strip()


def test_a_removal_that_would_empty_its_run_is_dropped_and_earlier_removals_win():
    out = removals_of(removal(2), removal(3), removal(4))
    assert passes(out) == [True, True, False]
    assert out[2].strip()


def test_removals_are_kept_first_come_first_served_whatever_the_paragraph_order():
    out = removals_of(removal(4), removal(2), removal(3))
    assert passes(out) == [True, True, False]


def test_a_dropped_removal_does_not_count_against_the_runs_last_bullet():
    out = removals_of(
        removal(2, jd_quote="nowhere in the posting"), removal(3), removal(4)
    )
    assert passes(out) == [False, True, True]


def test_a_run_of_one_bullet_cannot_lose_it():
    out = removals_of(removal(2), bullets={2, 4})
    assert isinstance(out[0], str)


def test_a_non_bullet_paragraph_ends_a_run_so_each_role_keeps_its_own_last_bullet():
    out = removals_of(
        removal(2), removal(3), removal(5), removal(6), bullets={2, 3, 5, 6}
    )
    assert passes(out) == [True, False, True, False]


def test_a_resume_with_no_bullet_has_no_runs_so_no_removal_passes():
    out = removals_of(removal(2), removal(3), bullets=set())
    assert passes(out) == [False, False]


# --- reorder validation (PRD 04, "Validation") --------------------------------------


def test_a_reorder_that_meets_every_rule_passes():
    assert reorders_of(reorder(3, 2)) == [None]


def test_each_reorder_result_lines_up_with_its_reorder_in_proposal_order():
    out = reorders_of(reorder(3, 2), reorder(1, 0), reorder(7, 6))
    assert passes(out) == [True, False, True]


@pytest.mark.parametrize(
    "paragraphs",
    [[4, 3, 2], [3, 2, 4], [4, 2, 3], [3, 4, 2]],
)
def test_a_reorder_of_a_whole_run_in_any_new_order_passes(paragraphs):
    assert reorders_of(reorder(*paragraphs)) == [None]


@pytest.mark.parametrize(
    "paragraphs",
    [[], [2], [2, 2], [3, 3, 2]],
    ids=["none", "one", "twice-the-same", "repeated-index"],
)
def test_a_reorder_needs_at_least_two_distinct_indices(paragraphs):
    assert isinstance(reorders_of(reorder(*paragraphs))[0], str)


@pytest.mark.parametrize("paragraphs", [[2, 3], [2, 3, 4], [6, 7]])
def test_a_reorder_that_keeps_the_current_order_is_dropped(paragraphs):
    assert isinstance(reorders_of(reorder(*paragraphs))[0], str)


@pytest.mark.parametrize(
    "paragraphs", [[4, 2], [2, 4]], ids=["skips-one", "skips-one-ascending"]
)
def test_a_reorder_must_be_one_consecutive_stretch(paragraphs):
    assert isinstance(reorders_of(reorder(*paragraphs))[0], str)


@pytest.mark.parametrize(
    "paragraphs",
    [[4, 5, 6], [7, 6, 5], [0, 1], [1, 2], [99, 2], [-1, 2]],
    ids=[
        "across-two-roles",
        "across-two-roles-reversed",
        "headers",
        "header-and-bullet",
        "outside",
        "negative",
    ],
)
def test_a_reorder_that_is_not_all_bullets_of_one_role_is_dropped(paragraphs):
    assert isinstance(reorders_of(reorder(*paragraphs))[0], str)


def test_a_reorder_across_two_roles_is_dropped_even_when_no_header_sits_between_them():
    out = reorders_of(reorder(4, 3, 2, 5, 6), bullets={2, 3, 4, 5, 6, 7})
    assert out == [None]
    # With the header between them the same indices form two runs, so nothing passes.
    assert isinstance(reorders_of(reorder(4, 3, 2, 5, 6))[0], str)


def test_removed_paragraphs_are_skipped_when_judging_the_stretch():
    assert reorders_of(reorder(4, 2), removed={3}) == [None]


def test_a_reorder_that_matches_the_order_once_removals_are_skipped_is_dropped():
    assert isinstance(reorders_of(reorder(2, 4), removed={3})[0], str)


@pytest.mark.parametrize("paragraphs", [[3, 2], [4, 3, 2], [4, 3]])
def test_a_reorder_listing_a_removed_paragraph_is_dropped(paragraphs):
    assert isinstance(reorders_of(reorder(*paragraphs), removed={3})[0], str)


def test_a_reorder_does_not_skip_a_removed_paragraph_that_ends_a_run_edge():
    assert reorders_of(reorder(4, 3), removed={2}) == [None]


@pytest.mark.parametrize("paragraphs", [[10, 9], [11, 9, 10], [9, 11, 10], [11, 10, 9]])
def test_a_consecutive_stretch_of_skills_lines_can_be_reordered(paragraphs):
    out = reorders_of(reorder(*paragraphs), section=SKILLS_LINES)
    assert out == [None]


def test_skills_lines_need_not_be_bullets_or_in_a_run():
    assert BULLETS.isdisjoint(SKILLS_LINES)
    assert reorders_of(reorder(10, 9), bullets=set(), section=SKILLS_LINES) == [None]


@pytest.mark.parametrize(
    "paragraphs",
    [[9, 11], [8, 9], [11, 12], [7, 8], [4, 9], [9, 10, 99]],
    ids=[
        "non-consecutive",
        "heading-and-line",
        "line-and-next-section",
        "bullet-and-heading",
        "bullet-and-line",
        "outside",
    ],
)
def test_a_reorder_that_mixes_the_skills_section_with_other_paragraphs_or_has_gaps_is_dropped(
    paragraphs,
):
    assert isinstance(reorders_of(reorder(*paragraphs), section=SKILLS_LINES)[0], str)


def test_skills_lines_get_no_reorder_when_no_skills_section_is_in_use():
    assert isinstance(reorders_of(reorder(10, 9))[0], str)


def test_a_bullet_stretch_is_dropped_if_part_of_it_is_in_the_skills_section():
    out = reorders_of(
        reorder(8, 7),
        bullets={6, 7, 8},
        section={8, 9},
    )
    assert isinstance(out[0], str)


def test_a_reorder_sharing_a_paragraph_with_an_earlier_passing_reorder_is_dropped():
    out = reorders_of(reorder(3, 2), reorder(4, 3))
    assert passes(out) == [True, False]
    assert out[1].strip()


def test_a_reorder_sharing_a_paragraph_with_an_earlier_dropped_reorder_still_passes():
    out = reorders_of(reorder(3, 2, jd_quote="not in the posting"), reorder(4, 3))
    assert passes(out) == [False, True]


def test_reorders_that_share_no_paragraph_both_pass():
    out = reorders_of(reorder(3, 2), reorder(7, 6))
    assert out == [None, None]


@pytest.mark.parametrize(
    "quote",
    ["not in the posting", "", "   ", "quuxlate system"],
    ids=["absent", "empty", "blank", "wrong-case"],
)
def test_a_reorder_needs_a_valid_posting_quote(quote):
    assert isinstance(reorders_of(reorder(3, 2, jd_quote=quote))[0], str)


def test_whitespace_runs_are_collapsed_in_a_reorders_quote_and_the_posting():
    assert reorders_of(reorder(3, 2, jd_quote="data   engineering\nwork")) == [None]


@pytest.mark.parametrize("reason", ["", "   ", "\n"])
def test_a_reorder_needs_a_reason(reason):
    assert isinstance(reorders_of(reorder(3, 2, reason=reason))[0], str)


def test_a_resume_with_no_bullet_has_no_runs_so_no_bullet_reorder_passes():
    assert passes(reorders_of(reorder(3, 2), reorder(7, 6), bullets=set())) == [
        False,
        False,
    ]


def test_every_dropped_removal_and_reorder_carries_a_non_empty_reason():
    out = removals_of(removal(1), removal(3, reason=""), removal(3, jd_quote="nope"))
    out += reorders_of(
        reorder(2), reorder(2, 3), reorder(4, 2), reorder(3, 2, reason="")
    )
    assert all(isinstance(reason, str) and reason.strip() for reason in out)


# --- the removal beats an edit or a flag (PRD 04, "Validation") ---------------------

PARAGRAPHS = TEXTS


def test_an_edit_on_a_paragraph_a_passing_removal_deletes_is_dropped():
    target = edit(paragraph=2)
    assert validate_edits([target], PARAGRAPHS, JD) == [None]
    out = validate_edits([target], PARAGRAPHS, JD, removed={2})
    assert isinstance(out[0], str) and out[0].strip()


def test_an_edit_on_another_paragraph_is_unaffected_by_a_removal():
    assert validate_edits([edit(paragraph=2)], PARAGRAPHS, JD, removed={3}) == [None]


def test_a_flag_on_a_paragraph_a_passing_removal_deletes_is_dropped():
    flag = Flag(
        paragraph=2, jd_quote=QUOTE, suggestion="Reword it", reason="It may matter"
    )
    assert validate_flags([flag], PARAGRAPHS, JD) == [None]
    out = validate_flags([flag], PARAGRAPHS, JD, removed={2})
    assert isinstance(out[0], str) and out[0].strip()


def test_a_dropped_flag_does_not_use_up_one_of_the_three_when_a_removal_drops_it():
    flags = [
        Flag(paragraph=index, jd_quote=QUOTE, suggestion="Reword", reason="matters")
        for index in (2, 3, 4, 6)
    ]
    out = validate_flags(flags, PARAGRAPHS, JD, removed={2})
    assert passes(out) == [False, True, True, True]


# --- the contract of removals and reorders in the reply (PRD 04, "Output contract") --

REMOVAL = {"paragraph": 3, "jd_quote": QUOTE, "reason": "it detracts"}
REORDER = {"paragraphs": [4, 2], "jd_quote": QUOTE, "reason": "leads with it"}


def ran_with(
    tmp_path,
    stand_in,
    *,
    edits=(),
    removals=(),
    reorders=(),
    flags=(),
    skills=(),
    jd=JD,
):
    explanation = None if edits or removals or reorders or flags else EXPLANATION
    reply = reply_object(edits, explanation, flags, removals, reorders)
    return run(tmp_path, stand_in, reply, build=build, jd=jd, skills=skills)


def raises_on(tmp_path, stand_in, reply):
    copy = make_resume(tmp_path / "resume.docx", build)
    stand_in.reply = reply
    before = copy.read_bytes()
    with pytest.raises(JsaError):
        redline_resume(copy, tmp_path / "e.json", JD, SETTINGS)
    assert not (tmp_path / "e.json").exists()
    assert copy.read_bytes() == before


@pytest.mark.parametrize("missing", list(REMOVAL))
def test_every_removal_field_is_required_in_the_reply(tmp_path, stand_in, missing):
    partial = {key: value for key, value in REMOVAL.items() if key != missing}
    raises_on(tmp_path, stand_in, reply_object([], None, removals=[partial]))


@pytest.mark.parametrize("missing", list(REORDER))
def test_every_reorder_field_is_required_in_the_reply(tmp_path, stand_in, missing):
    partial = {key: value for key, value in REORDER.items() if key != missing}
    raises_on(tmp_path, stand_in, reply_object([], None, reorders=[partial]))


@pytest.mark.parametrize(
    ("field", "value"),
    [("paragraph", "3"), ("paragraph", None), ("jd_quote", 7), ("reason", None)],
)
def test_a_removal_field_of_the_wrong_type_raises(tmp_path, stand_in, field, value):
    raises_on(
        tmp_path,
        stand_in,
        reply_object([], None, removals=[{**REMOVAL, "paragraph": 3, field: value}]),
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("paragraphs", 4),
        ("paragraphs", "4,2"),
        ("paragraphs", None),
        ("jd_quote", ["x"]),
        ("reason", 5),
    ],
)
def test_a_reorder_field_of_the_wrong_type_raises(tmp_path, stand_in, field, value):
    raises_on(
        tmp_path, stand_in, reply_object([], None, reorders=[{**REORDER, field: value}])
    )


def test_removals_and_reorders_in_the_reply_are_recorded_with_their_fields_and_validation(
    tmp_path, stand_in
):
    bad = {**REMOVAL, "paragraph": 1}
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL, bad],
        reorders=[REORDER, {**REORDER, "paragraphs": [4]}],
    )
    record = record_of(ran)
    assert list(record) == ["explanation", "edits", "removals", "reorders", "flags"]
    assert record["explanation"] is None
    first, second = record["removals"]
    assert {k: first[k] for k in REMOVAL} == REMOVAL and first["validation"] is None
    assert (
        second["paragraph"] == 1
        and isinstance(second["validation"], str)
        and second["validation"]
    )
    first, second = record["reorders"]
    assert {k: first[k] for k in REORDER} == REORDER and first["validation"] is None
    assert (
        second["paragraphs"] == [4]
        and isinstance(second["validation"], str)
        and second["validation"]
    )


def test_applied_and_dropped_counts_include_every_kind(tmp_path, stand_in):
    ran = ran_with(
        tmp_path,
        stand_in,
        edits=[as_dict(edit(paragraph=2)), as_dict(edit(paragraph=99))],
        removals=[REMOVAL, {**REMOVAL, "paragraph": 1}, {**REMOVAL, "reason": ""}],
        reorders=[{**REORDER, "paragraphs": [7, 6]}, {**REORDER, "paragraphs": [2]}],
        flags=[
            {
                "paragraph": 6,
                "jd_quote": QUOTE,
                "suggestion": "Reword",
                "reason": "matters",
            },
            {
                "paragraph": 99,
                "jd_quote": QUOTE,
                "suggestion": "Reword",
                "reason": "matters",
            },
        ],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (4, 5)


def test_the_prompt_the_agent_receives_tags_bullets_and_bold_paragraphs(
    tmp_path, stand_in
):
    ran_with(tmp_path, stand_in)
    (call,) = stand_in.calls
    lines = {
        int(match.group(1)): match.group(0)
        for line in call.prompt.splitlines()
        if (match := re.fullmatch(r"(\d+)((?: \[(?:bullet|bold)\])*): .*", line))
    }
    assert set(lines) == set(range(len(TEXTS)))
    for index, line in lines.items():
        assert ("[bullet]" in line) == (index in BULLETS), line
        assert ("[bold]" in line) == (index in (0, 1, 5, 8, 12)), line


def test_assembling_the_prompt_tags_the_given_bullets_beside_bold():
    prompt = assemble_redline_prompt(JD, TEXTS, [], [], {1}, {2, 3})
    by_index = {
        int(line.split(":")[0].split()[0]): line.split(":")[0]
        for line in prompt.splitlines()
        if re.match(r"\d+[ :]", line)
    }
    assert "[bullet]" in by_index[2] and "[bullet]" in by_index[3]
    assert "[bullet]" not in by_index[1] and "[bold]" in by_index[1]
    assert "[bullet]" not in by_index[4]


# --- the redline document (PRD 04, "Written into the resume copy") -------------------


def body_of(path):
    with zipfile.ZipFile(path) as archive:
        return etree.fromstring(archive.read("word/document.xml"))


def text_in(paragraph, view):
    hidden = {"accept": {"del"}, "reject": {"ins"}}[view]
    parts = []
    for node in paragraph.iter(f"{{{W}}}t", f"{{{W}}}delText"):
        if hidden & {etree.QName(a).localname for a in node.iterancestors()}:
            continue
        parts.append(node.text or "")
    return "".join(parts)


def view_of(path, view):
    """`(text, style)` of each paragraph once every tracked change is accepted or rejected.

    A paragraph whose mark goes away (deleted on accept, inserted on reject) joins the next one.
    """
    gone = {"accept": {"del"}, "reject": {"ins"}}[view]
    out = []
    pending = ""
    for paragraph in body_of(path).find("w:body", NS).findall("w:p", NS):
        marks = {
            etree.QName(child).localname
            for child in paragraph.findall("w:pPr/w:rPr/*", NS)
        }
        style = paragraph.find("w:pPr/w:pStyle", NS)
        pending += text_in(paragraph, view)
        if marks & gone:
            continue
        out.append((pending, None if style is None else style.get(f"{{{W}}}val")))
        pending = ""
    assert pending == ""
    return out


def texts(view):
    return [text for text, _ in view]


def original_view(ran):
    return view_of(ran.original, "accept")


def assert_no_move_markup(path):
    """PRD 04: a reorder is written as a move in place, with no move markup."""
    assert not body_of(path).xpath(
        "//w:moveFrom | //w:moveTo | //w:moveFromRangeStart | //w:moveFromRangeEnd"
        " | //w:moveToRangeStart | //w:moveToRangeEnd",
        namespaces=NS,
    )


def swapped(texts_, *pairs):
    """`texts_` with each `(low, high)` run of indices reversed in place."""
    out = list(texts_)
    for low, high in pairs:
        out[low : high + 1] = reversed(out[low : high + 1])
    return out


def test_the_resume_used_here_has_bullets_in_a_word_list_style(tmp_path):
    path = make_resume(tmp_path / "resume.docx", build)
    document = docx.Document(str(path))
    assert bullet_paragraphs(document) == BULLETS


def test_a_removal_is_a_tracked_deletion_of_the_text_and_the_paragraph_mark(
    tmp_path, stand_in
):
    ran = ran_with(tmp_path, stand_in, removals=[REMOVAL])
    assert ran.outcome.applied == 1
    paragraph = body_of(ran.copy).find("w:body", NS).findall("w:p", NS)[3]
    assert paragraph.find("w:pPr/w:rPr/w:del", NS) is not None
    deleted = "".join(paragraph.xpath("./w:del//w:delText/text()", namespaces=NS))
    assert deleted == TEXTS[3]
    assert text_in(paragraph, "accept") == ""


def test_accepting_a_removal_leaves_no_empty_line_and_rejecting_restores_the_bullet(
    tmp_path, stand_in
):
    ran = ran_with(tmp_path, stand_in, removals=[REMOVAL])
    accepted = view_of(ran.copy, "accept")
    assert texts(accepted) == [t for i, t in enumerate(TEXTS) if i != 3]
    assert "" not in texts(accepted)
    assert view_of(ran.copy, "reject") == original_view(ran)


def test_a_removed_paragraph_keeps_its_list_formatting_so_rejecting_restores_it(
    tmp_path, stand_in
):
    ran = ran_with(tmp_path, stand_in, removals=[REMOVAL])
    document = docx.Document(str(ran.copy))
    assert document.paragraphs[3].style.name == "List Bullet"
    assert [text for text, _ in view_of(ran.copy, "reject")][3] == TEXTS[3]
    assert view_of(ran.copy, "reject")[3][1] == original_view(ran)[3][1] is not None


def test_several_removals_each_leave_their_own_deletion(tmp_path, stand_in):
    removals = [REMOVAL, {**REMOVAL, "paragraph": 7}, {**REMOVAL, "paragraph": 2}]
    ran = ran_with(tmp_path, stand_in, removals=removals)
    assert ran.outcome.applied == 3
    assert texts(view_of(ran.copy, "accept")) == [
        t for i, t in enumerate(TEXTS) if i not in (2, 3, 7)
    ]
    assert view_of(ran.copy, "reject") == original_view(ran)


def test_a_dropped_removal_changes_nothing_and_a_run_keeps_one_bullet(
    tmp_path, stand_in
):
    removals = [{**REMOVAL, "paragraph": index} for index in (2, 3, 4)]
    ran = ran_with(tmp_path, stand_in, removals=removals)
    assert ran.outcome.applied == 2 and ran.outcome.dropped == 1
    assert texts(view_of(ran.copy, "accept"))[2:5] == [TEXTS[4], TEXTS[5], TEXTS[6]]
    assert record_of(ran)["removals"][2]["validation"]
    assert view_of(ran.copy, "reject") == original_view(ran)


def test_a_reorder_moves_the_paragraphs_in_place_with_no_tracked_change_or_move_markup(
    tmp_path, stand_in
):
    ran = ran_with(tmp_path, stand_in, reorders=[{**REORDER, "paragraphs": [4, 3, 2]}])
    assert ran.outcome.applied == 1
    assert_no_move_markup(ran.copy)
    assert not body_of(ran.copy).xpath("//w:ins | //w:del", namespaces=NS)
    expected = list(TEXTS)
    expected[2:5] = [TEXTS[4], TEXTS[3], TEXTS[2]]
    assert texts(view_of(ran.copy, "accept")) == expected
    assert texts(view_of(ran.copy, "reject")) == expected


def test_moved_paragraphs_keep_their_list_formatting(tmp_path, stand_in):
    ran = ran_with(tmp_path, stand_in, reorders=[{**REORDER, "paragraphs": [4, 3, 2]}])
    accepted = view_of(ran.copy, "accept")
    original = original_view(ran)
    assert [style for _, style in accepted] == [style for _, style in original]
    assert accepted[2][1] is not None
    assert view_of(ran.copy, "reject") == accepted


def test_a_reorder_of_the_second_role_leaves_the_first_alone(tmp_path, stand_in):
    ran = ran_with(tmp_path, stand_in, reorders=[{**REORDER, "paragraphs": [7, 6]}])
    expected = swapped(TEXTS, (6, 7))
    assert texts(view_of(ran.copy, "accept")) == expected
    assert texts(view_of(ran.copy, "reject")) == expected
    assert_no_move_markup(ran.copy)


def test_two_reorders_in_one_run_both_move(tmp_path, stand_in):
    reorders = [{**REORDER, "paragraphs": [3, 2]}, {**REORDER, "paragraphs": [4, 5]}]
    ran = ran_with(tmp_path, stand_in, reorders=reorders)
    assert ran.outcome.applied == 1 and ran.outcome.dropped == 1
    expected = swapped(TEXTS, (2, 3))
    assert texts(view_of(ran.copy, "accept")) == expected
    assert texts(view_of(ran.copy, "reject")) == expected
    assert_no_move_markup(ran.copy)


def test_the_skills_sections_lines_can_be_reordered_in_place(tmp_path, stand_in):
    ran = ran_with(
        tmp_path,
        stand_in,
        reorders=[{**REORDER, "paragraphs": [11, 9, 10]}],
        skills=SKILLS,
    )
    assert ran.outcome.applied == 1
    expected = list(TEXTS)
    expected[9:12] = [TEXTS[11], TEXTS[9], TEXTS[10]]
    assert texts(view_of(ran.copy, "accept")) == expected
    assert texts(view_of(ran.copy, "reject")) == expected
    assert_no_move_markup(ran.copy)


def test_without_a_skills_list_a_reorder_of_the_skills_lines_is_dropped(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path, stand_in, reorders=[{**REORDER, "paragraphs": [11, 9, 10]}]
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before
    assert record_of(ran)["reorders"][0]["validation"]


def test_a_reorder_mixing_the_skills_section_with_other_paragraphs_is_dropped(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path, stand_in, reorders=[{**REORDER, "paragraphs": [9, 8]}], skills=SKILLS
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_a_removal_in_the_skills_section_is_dropped_even_with_a_skills_list(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path, stand_in, removals=[{**REMOVAL, "paragraph": 10}], skills=SKILLS
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_a_paragraph_that_is_both_edited_and_moved_keeps_its_own_tracked_edit(
    tmp_path, stand_in
):
    change = as_dict(
        edit(paragraph=6, find="Frobnicator", replace="Quuxlate", jd_quote=QUOTE)
    )
    ran = ran_with(
        tmp_path, stand_in, edits=[change], reorders=[{**REORDER, "paragraphs": [7, 6]}]
    )
    assert ran.outcome.applied == 2
    edited = "Ran the Quuxlate rollout."
    expected = swapped(TEXTS, (6, 7))
    assert texts(view_of(ran.copy, "reject")) == expected
    expected[7] = edited
    assert texts(view_of(ran.copy, "accept")) == expected
    assert_no_move_markup(ran.copy)
    body = body_of(ran.copy)
    assert {
        etree.QName(n).localname for n in body.xpath("//w:ins | //w:del", namespaces=NS)
    } == {"ins", "del"}
    moved = body.find("w:body", NS).findall("w:p", NS)[7]
    assert moved.xpath("./w:ins | ./w:del", namespaces=NS)


def test_a_reorder_with_a_word_edit_in_a_moved_bullet_tracks_only_the_word_edit(
    tmp_path, stand_in
):
    change = as_dict(
        edit(paragraph=3, find="data work", replace="Quuxlate system", jd_quote=QUOTE)
    )
    ran = ran_with(
        tmp_path,
        stand_in,
        edits=[change],
        reorders=[{**REORDER, "paragraphs": [4, 3, 2], "reason": "REORDER-WHY"}],
    )
    assert ran.outcome.applied == 2
    body = body_of(ran.copy)
    tracked = body.xpath("//w:ins | //w:del", namespaces=NS)
    assert sorted(etree.QName(n).localname for n in tracked) == ["del", "ins"]
    assert (
        "".join(t for n in tracked for t in n.itertext()) == "data workQuuxlate system"
    )
    assert_no_move_markup(ran.copy)
    reject = list(TEXTS)
    reject[2:5] = [TEXTS[4], TEXTS[3], TEXTS[2]]
    assert texts(view_of(ran.copy, "reject")) == reject
    comments = comments_of(ran.copy).findall("w:comment", NS)
    reorder_comments = [c for c in comments if "REORDER-WHY" in "".join(c.itertext())]
    assert len(comments) == 2 and len(reorder_comments) == 1


def test_a_removed_paragraph_inside_a_reordered_stretch_is_deleted_in_place(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [4, 2]}],
    )
    assert ran.outcome.applied == 2 and ran.outcome.dropped == 0
    assert texts(view_of(ran.copy, "accept")) == (
        TEXTS[:2] + [TEXTS[4], TEXTS[2]] + TEXTS[5:]
    )
    assert texts(view_of(ran.copy, "reject")) == (
        TEXTS[:2] + [TEXTS[4], TEXTS[3], TEXTS[2]] + TEXTS[5:]
    )
    assert_no_move_markup(ran.copy)


def test_a_reorder_listing_a_paragraph_a_removal_deletes_is_dropped_with_the_removal_applied(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [3, 2]}],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 1)
    assert texts(view_of(ran.copy, "accept")) == [
        t for i, t in enumerate(TEXTS) if i != 3
    ]


def test_an_edit_and_a_flag_on_a_removed_paragraph_are_dropped_and_the_removal_wins(
    tmp_path, stand_in
):
    flag = {
        "paragraph": 3,
        "jd_quote": QUOTE,
        "suggestion": "Reword it",
        "reason": "It may matter",
    }
    ran = ran_with(
        tmp_path,
        stand_in,
        edits=[
            as_dict(
                edit(
                    paragraph=3,
                    find="data work",
                    replace="Quuxlate system",
                    jd_quote=QUOTE,
                )
            )
        ],
        removals=[REMOVAL],
        flags=[flag],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 2)
    record = record_of(ran)
    assert record["edits"][0]["validation"] and record["flags"][0]["validation"]
    assert record["removals"][0]["validation"] is None
    assert texts(view_of(ran.copy, "accept")) == [
        t for i, t in enumerate(TEXTS) if i != 3
    ]
    assert len(comments_of(ran.copy).findall("w:comment", NS)) == 1


def test_a_dropped_removal_does_not_stop_an_edit_or_flag_on_its_paragraph(
    tmp_path, stand_in
):
    flag = {
        "paragraph": 3,
        "jd_quote": QUOTE,
        "suggestion": "Reword it",
        "reason": "It may matter",
    }
    ran = ran_with(
        tmp_path,
        stand_in,
        edits=[
            as_dict(
                edit(
                    paragraph=3,
                    find="data work",
                    replace="Quuxlate system",
                    jd_quote=QUOTE,
                )
            )
        ],
        removals=[{**REMOVAL, "jd_quote": "not in the posting"}],
        flags=[flag],
    )
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 1)
    assert (
        texts(view_of(ran.copy, "accept"))[3] == "Led Quuxlate system using SQL daily."
    )


def test_a_removal_or_reorder_in_a_resume_with_no_bullet_is_dropped_while_edits_and_flags_apply(
    tmp_path, stand_in
):
    flag = {
        "paragraph": 1,
        "jd_quote": QUOTE,
        "suggestion": "Reword it",
        "reason": "It may matter",
    }
    reply = reply_object(
        [as_dict(edit(paragraph=1))],
        None,
        [flag],
        [{**REMOVAL, "paragraph": 2}],
        [{**REORDER, "paragraphs": [2, 1]}],
    )
    ran = run(tmp_path, stand_in, reply)
    assert (ran.outcome.applied, ran.outcome.dropped) == (2, 2)
    record = record_of(ran)
    assert record["removals"][0]["validation"] and record["reorders"][0]["validation"]
    assert (
        record["edits"][0]["validation"] is None
        and record["flags"][0]["validation"] is None
    )


def test_each_removal_and_reorder_carries_one_comment_by_the_redline_with_its_quote_and_reason(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[{**REMOVAL, "reason": "REMOVAL-WHY"}],
        reorders=[{**REORDER, "paragraphs": [7, 6], "reason": "REORDER-WHY"}],
    )
    comments = comments_of(ran.copy).findall("w:comment", NS)
    assert len(comments) == 2
    for comment in comments:
        assert comment.get(f"{{{W}}}author") == AUTHOR
    bodies = ["".join(comment.itertext()) for comment in comments]
    assert sum("REMOVAL-WHY" in text for text in bodies) == 1
    assert sum("REORDER-WHY" in text for text in bodies) == 1
    assert all(QUOTE in text for text in bodies)
    ids = [comment.get(f"{{{W}}}id") for comment in comments]
    assert len(set(ids)) == 2
    anchored = {
        node.get(f"{{{W}}}id")
        for node in body_of(ran.copy).xpath("//w:commentRangeStart", namespaces=NS)
    }
    assert set(ids) <= anchored


def test_comments_never_alter_the_text(tmp_path, stand_in):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [7, 6]}],
    )
    assert texts(view_of(ran.copy, "reject")) == swapped(TEXTS, (6, 7))


def test_a_reorders_comment_spans_the_moved_paragraphs_in_their_new_places(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        reorders=[{**REORDER, "paragraphs": [4, 3, 2], "reason": "REORDER-WHY"}],
    )
    (comment,) = comments_of(ran.copy).findall("w:comment", NS)
    assert "REORDER-WHY" in "".join(comment.itertext())
    comment_id = comment.get(f"{{{W}}}id")
    body = body_of(ran.copy).find("w:body", NS)
    spanned = []
    inside = False
    for paragraph in body.findall("w:p", NS):
        touched = inside
        for marker in paragraph.iter(
            f"{{{W}}}commentRangeStart", f"{{{W}}}commentRangeEnd"
        ):
            if marker.get(f"{{{W}}}id") == comment_id:
                inside = etree.QName(marker).localname == "commentRangeStart"
                touched = True
        if touched:
            spanned.append(paragraph)
    assert [text_in(p, "accept") for p in spanned] == [TEXTS[4], TEXTS[3], TEXTS[2]]
    assert len(body.xpath("//w:commentRangeEnd", namespaces=NS)) == 1


def test_every_tracked_change_of_a_removal_and_a_reorder_is_authored_by_the_redline(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [7, 6]}],
    )
    tracked = body_of(ran.copy).xpath("//w:del | //w:ins", namespaces=NS)
    assert_no_move_markup(ran.copy)
    assert tracked
    assert {node.get(f"{{{W}}}author") for node in tracked} == {AUTHOR}
    assert all(node.get(f"{{{W}}}date") for node in tracked)


def test_paragraphs_no_proposal_touches_are_unchanged(tmp_path, stand_in):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [7, 6]}],
    )
    old = body_of(ran.original).find("w:body", NS).findall("w:p", NS)
    new = {
        etree.tostring(paragraph, method="c14n")
        for paragraph in body_of(ran.copy).find("w:body", NS).findall("w:p", NS)
    }
    for index in set(range(len(old))) - {3, 6, 7}:
        assert etree.tostring(old[index], method="c14n") in new, index


def test_the_redlined_document_replaces_the_copy_and_python_docx_can_reopen_it(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[REMOVAL],
        reorders=[{**REORDER, "paragraphs": [7, 6]}],
    )
    assert ran.copy.read_bytes() != ran.before
    assert docx.Document(str(ran.copy)).paragraphs
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "original.docx",
        "redline_edits.json",
        "resume.docx",
    ]


def test_a_copy_whose_removals_and_reorders_all_fail_is_untouched_but_the_record_is_written(
    tmp_path, stand_in
):
    ran = ran_with(
        tmp_path,
        stand_in,
        removals=[{**REMOVAL, "paragraph": 1}],
        reorders=[{**REORDER, "paragraphs": [2, 3]}],
    )
    assert ran.copy.read_bytes() == ran.before
    assert ran.edits_file.exists()
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 2)


def test_the_record_is_written_only_after_the_copy_is_replaced(tmp_path, stand_in):
    ran = ran_with(tmp_path, stand_in, removals=[REMOVAL])
    record = json.loads(ran.edits_file.read_text(encoding="utf-8"))
    assert record["removals"][0]["validation"] is None
    assert ran.copy.read_bytes() != ran.before


# --- README (PRD 04, "Redline"; issue #153) ------------------------------------------


def test_the_readme_describes_removals_reorders_and_the_lone_docx_review():
    from conftest import REPO_ROOT

    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    lowered = " ".join(text.lower().split())
    assert "removal" in lowered and "reorder" in lowered
    assert "tracked deletion" in lowered
    assert "tracked move" not in lowered
    assert "lone `.docx`" in lowered
    after = lowered[lowered.index("lone `.docx`") :]
    assert "tracked change" in after[:400] and "review" in after[:400]


# --- skills lines separated by blank lines (issue #163; PRD 04, "Skills section") ----
#
#  0 Riley Resumeperson (bold)   4 Python, R           8 Education (bold)
#  1 Skills                      5 (blank)             9 BS in Statistics
#  2 SQL, Excel                  6 Tableau, Looker
#  3 (blank)                     7 (blank)
GAPPED_TEXTS = [
    "Riley Resumeperson",
    "Skills",
    "SQL, Excel",
    "",
    "Python, R",
    "",
    "Tableau, Looker",
    "",
    "Education",
    "BS in Statistics",
]
GAPPED_BLANKS = {3, 5, 7}
GAPPED_LINES = {2, 4, 6}


def build_gapped(document):
    for index, text in enumerate(GAPPED_TEXTS):
        if index in (0, 8):
            bold_paragraph(document, text)
        else:
            document.add_paragraph(text)


def gapped_ran(tmp_path, stand_in, *paragraphs, skills=SKILLS):
    reorders = [{**REORDER, "paragraphs": list(paragraphs)}]
    reply = reply_object([], None, reorders=reorders)
    return run(tmp_path, stand_in, reply, build=build_gapped, jd=JD, skills=skills)


def test_a_skills_reorder_may_span_the_blank_lines_between_categories():
    out = validate_reorders(
        [reorder(6, 2, 4)],
        JD,
        bullets=set(),
        skills_section=GAPPED_LINES | GAPPED_BLANKS,
        empty=GAPPED_BLANKS,
    )
    assert out == [None]


def test_a_skills_reorder_across_a_blank_line_is_applied_in_place(tmp_path, stand_in):
    ran = gapped_ran(tmp_path, stand_in, 4, 2)
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 0)
    assert record_of(ran)["reorders"][0]["validation"] is None
    expected = list(GAPPED_TEXTS)
    expected[2], expected[4] = GAPPED_TEXTS[4], GAPPED_TEXTS[2]
    assert texts(view_of(ran.copy, "accept")) == expected
    assert texts(view_of(ran.copy, "reject")) == expected
    assert_no_move_markup(ran.copy)


def test_a_skills_reorder_spanning_two_blank_lines_keeps_the_blanks_where_they_are(
    tmp_path, stand_in
):
    ran = gapped_ran(tmp_path, stand_in, 6, 2, 4)
    assert ran.outcome.applied == 1
    accepted = texts(view_of(ran.copy, "accept"))
    assert [text for text in accepted if text] == [
        "Riley Resumeperson",
        "Skills",
        "Tableau, Looker",
        "SQL, Excel",
        "Python, R",
        "Education",
        "BS in Statistics",
    ]
    assert {i for i, text in enumerate(accepted) if not text} == GAPPED_BLANKS
    assert len(accepted) == len(GAPPED_TEXTS)


def test_a_skills_reorder_across_a_blank_line_still_must_list_every_line_of_its_stretch(
    tmp_path, stand_in
):
    ran = gapped_ran(tmp_path, stand_in, 6, 2)
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert record_of(ran)["reorders"][0]["validation"]
    assert ran.copy.read_bytes() == ran.before


def test_a_skills_reorder_across_a_blank_line_that_keeps_the_order_is_dropped(
    tmp_path, stand_in
):
    ran = gapped_ran(tmp_path, stand_in, 2, 4, 6)
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert record_of(ran)["reorders"][0]["validation"]


def test_a_blank_line_does_not_let_a_reorder_reach_past_the_skills_section(
    tmp_path, stand_in
):
    ran = gapped_ran(tmp_path, stand_in, 6, 9)
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_without_a_skills_list_a_reorder_across_a_blank_line_is_dropped(
    tmp_path, stand_in
):
    ran = gapped_ran(tmp_path, stand_in, 4, 2, skills=())
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_a_blank_line_between_bullets_still_ends_the_run_so_a_bullet_reorder_across_it_is_dropped(
    tmp_path, stand_in
):
    def build_bullets(document):
        bold_paragraph(document, "Fabrikam Logistics")
        document.add_paragraph(
            "Built the Quuxlate platform from scratch.", "List Bullet"
        )
        document.add_paragraph("")
        document.add_paragraph("Led data work using SQL daily.", style="List Bullet")

    reorders = [{**REORDER, "paragraphs": [3, 1]}]
    reply = reply_object([], None, reorders=reorders)
    ran = run(tmp_path, stand_in, reply, build=build_bullets)
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_the_prompt_omits_blank_paragraphs_but_keeps_every_index(tmp_path, stand_in):
    gapped_ran(tmp_path, stand_in, 4, 2)
    (call,) = stand_in.calls
    shown = {
        int(match.group(1))
        for line in call.prompt.splitlines()
        if (match := re.fullmatch(r"(\d+)(?: \[(?:bullet|bold)\])*: .*", line))
    }
    assert shown == set(range(len(GAPPED_TEXTS))) - GAPPED_BLANKS
