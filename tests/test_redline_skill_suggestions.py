"""The redline's fifth proposal kind, `skill_suggestion` (issue #236; PRD 04 "Redline", `XC-9`, `XC-14`).

Validation is pure, so most tests call `validate_skill_suggestions` directly. The document tests drive
`redline_resume` with Claude replaced at the shared agent loop and read the comments back from the .docx.
"""

import json
import re
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from profile_helpers import copy_example, write_config_toml
from test_claude_runner import result_message
from test_redline import (
    EXPLANATION,
    NS,
    SETTINGS,
    SK_LIST,
    SK_PARAS,
    SK_SECTION,
    W,
    body_of,
    comment_texts,
    comments_of,
    make_resume,
    skills_resume,
    view_of,
)
from test_wall_clock_config import config_text

from jsa import agent_loop
from jsa.errors import JsaError
from jsa.profile import AgentSettings, load_config, redline_settings
from jsa.redline import (
    SkillSuggestion,
    edits_applied,
    parse_proposal,
    redline_resume,
    validate_skill_suggestions,
)
from jsa.redline_docx import skills_section

JOB = (
    "Build dashboards in Looker.\n"
    "Turn findings into polished slide decks in Canva.\n"
    "Write R and Java daily; maintain the JavaScript front end.\n"
    "Own the Quuxlate system end to end."
)
SK_LINES = [
    "Riley Resumeperson",
    "Skills",
    "Python, SQL | Notion",
    "Excel, Looker, Tableau, JavaScript",
    "Experience",
    "Built the Quuxlate platform from scratch.",
]
SK_LINES_LIST = ["Python", "SQL", "Notion", "Excel", "Looker", "Tableau", "JavaScript"]
SECTION = range(2, 4)


def suggestion(**overrides) -> SkillSuggestion:
    fields = {
        "skill": "Canva",
        "jd_quote": "polished slide decks in Canva",
        "reason": "the dashboard and reporting work makes design tools plausible",
    }
    return SkillSuggestion(**{**fields, **overrides})


def verdicts(
    *suggestions, paragraphs=SK_LINES, jd=JOB, skills=None, section=SECTION, **kw
):
    return validate_skill_suggestions(
        list(suggestions),
        paragraphs,
        jd,
        SK_LINES_LIST if skills is None else skills,
        section,
        **kw,
    )


# --- validation: what passes (acceptance criterion 1) ------------------------------------


def test_a_suggestion_naming_a_skill_the_resume_never_mentions_passes():
    assert verdicts(suggestion()) == [None]


def test_each_result_lines_up_with_its_suggestion_in_proposal_order():
    out = verdicts(suggestion(skill="Tableau"), suggestion(), suggestion(reason=""))
    assert out[0] and out[1] is None and out[2]
    assert len(out) == 3


def test_no_suggestions_gives_no_results():
    assert verdicts() == []


# --- already on the list or in the section (criterion 2) ---------------------------------


@pytest.mark.parametrize("skill", ["Python", "python", "PYTHON", "  Notion  "])
def test_a_skill_that_is_an_entry_of_the_skills_list_is_dropped_whatever_its_case(
    skill,
):
    (result,) = verdicts(suggestion(skill=skill))
    assert isinstance(result, str) and result.strip()


def test_a_skill_in_the_skills_sections_text_but_not_on_the_list_is_dropped():
    paragraphs = [*SK_LINES[:3], "Excel, Looker, Canva", *SK_LINES[4:]]
    (result,) = verdicts(suggestion(), paragraphs=paragraphs)
    assert isinstance(result, str) and result.strip()


def test_a_skill_in_the_skills_section_is_matched_ignoring_case():
    paragraphs = [*SK_LINES[:3], "Excel, looker, CANVA", *SK_LINES[4:]]
    assert verdicts(suggestion(skill="canva"), paragraphs=paragraphs)[0]


def test_a_multi_word_skill_already_in_the_section_is_dropped():
    paragraphs = [*SK_LINES[:3], "Excel, Data Modeling", *SK_LINES[4:]]
    (result,) = verdicts(
        suggestion(skill="data modeling", jd_quote="Build dashboards in Looker"),
        paragraphs=paragraphs,
    )
    assert isinstance(result, str) and result.strip()


@pytest.mark.parametrize(
    ("skill", "quote"),
    [
        ("R", "Write R and Java daily"),
        ("Java", "Write R and Java daily"),
    ],
    ids=["R-inside-Tableau-and-others", "Java-inside-JavaScript"],
)
def test_a_skill_that_only_appears_inside_a_longer_word_is_not_already_present(
    skill, quote
):
    # "R" is inside "Tableau"/"Looker"/"Riley"; "Java" is inside "JavaScript".
    assert verdicts(suggestion(skill=skill, jd_quote=quote)) == [None]


@pytest.mark.parametrize(
    "line",
    ["Excel, R2D2", "Excel, 2R", "Excel, R_", "Excel, xR"],
    ids=["digit-after", "digit-before", "underscore-after", "letter-before"],
)
def test_letters_or_digits_touching_either_side_do_not_make_the_skill_present(line):
    paragraphs = [*SK_LINES[:3], line, *SK_LINES[4:]]
    assert verdicts(
        suggestion(skill="R", jd_quote="Write R and Java daily"),
        paragraphs=paragraphs,
    ) == [None]


@pytest.mark.parametrize(
    "line",
    ["Excel, R", "Excel (R)", "Excel/R", "R, Excel", "Excel; R."],
    ids=["after-comma", "in-brackets", "after-slash", "leading", "before-full-stop"],
)
def test_a_skill_set_off_by_punctuation_or_spaces_is_present(line):
    paragraphs = [*SK_LINES[:3], line, *SK_LINES[4:]]
    (result,) = verdicts(
        suggestion(skill="R", jd_quote="Write R and Java daily"),
        paragraphs=paragraphs,
    )
    assert isinstance(result, str) and result.strip()


def test_only_the_skills_section_counts_not_the_rest_of_the_resume():
    paragraphs = [*SK_LINES[:5], "Made slide decks in Canva for leaders."]
    assert verdicts(suggestion(), paragraphs=paragraphs) == [None]


def test_a_skill_with_regex_characters_is_matched_literally():
    paragraphs = [*SK_LINES[:3], "Excel, C++", *SK_LINES[4:]]
    jd = JOB + "\nWe use C++ and C#."
    (present,) = verdicts(
        suggestion(skill="C++", jd_quote="We use C++ and C#."),
        paragraphs=paragraphs,
        jd=jd,
    )
    assert isinstance(present, str)
    assert verdicts(suggestion(skill="C#", jd_quote="We use C++ and C#."), jd=jd) == [
        None
    ]


# --- invalid quote, reason or skill (criterion 3) -----------------------------------------


@pytest.mark.parametrize(
    "quote",
    ["", "   ", "\n", "Not in the posting at all", "polished SLIDE decks in canva"],
    ids=["empty", "spaces", "newline", "absent", "wrong-case"],
)
def test_a_quote_that_is_empty_or_not_in_the_posting_is_dropped(quote):
    (result,) = verdicts(suggestion(jd_quote=quote))
    assert isinstance(result, str) and result.strip()


def test_whitespace_runs_in_the_quote_and_the_posting_are_collapsed():
    jd = "Turn findings into   polished\n\nslide decks in Canva."
    assert verdicts(suggestion(jd_quote="polished slide decks in Canva"), jd=jd) == [
        None
    ]


@pytest.mark.parametrize(
    "reason", ["", "   ", "\n\t"], ids=["empty", "spaces", "blank"]
)
def test_an_empty_or_blank_reason_is_dropped(reason):
    (result,) = verdicts(suggestion(reason=reason))
    assert isinstance(result, str) and result.strip()


@pytest.mark.parametrize("skill", ["", "   ", "\t"], ids=["empty", "spaces", "tab"])
def test_an_empty_or_blank_skill_is_dropped(skill):
    (result,) = verdicts(suggestion(skill=skill))
    assert isinstance(result, str) and result.strip()


def test_every_dropped_suggestion_carries_a_non_empty_reason_string():
    out = verdicts(
        suggestion(skill=""),
        suggestion(skill="python"),
        suggestion(skill="Looker"),
        suggestion(jd_quote=""),
        suggestion(jd_quote="nowhere"),
        suggestion(reason=""),
    )
    assert all(isinstance(r, str) and r.strip() for r in out)


# --- no skills section or no skills list (criterion 4) ------------------------------------


@pytest.mark.parametrize(
    ("skills", "section"),
    [
        ((), SECTION),
        ([], SECTION),
        (SK_LINES_LIST, ()),
        (SK_LINES_LIST, []),
        ((), ()),
    ],
    ids=[
        "no-list-tuple",
        "no-list-empty",
        "no-section-tuple",
        "no-section-empty",
        "neither",
    ],
)
def test_with_no_skills_section_or_no_list_every_suggestion_is_dropped(skills, section):
    out = validate_skill_suggestions(
        [
            suggestion(),
            suggestion(skill="Figma", jd_quote="Build dashboards in Looker"),
        ],
        SK_LINES,
        JOB,
        skills,
        section,
    )
    assert len(out) == 2
    assert all(isinstance(r, str) and r.strip() for r in out)


def test_the_defaults_mean_no_list_and_no_section_so_nothing_passes():
    out = validate_skill_suggestions([suggestion()], SK_LINES, JOB)
    assert isinstance(out[0], str) and out[0].strip()


# --- the cap (criterion 5) ----------------------------------------------------------------

MANY = [
    suggestion(skill=name, jd_quote="Build dashboards in Looker")
    for name in ("Canva", "Figma", "Miro", "Notion2", "Slack", "Jira")
]


def test_by_default_only_the_first_three_passing_suggestions_are_kept():
    out = verdicts(*MANY)
    assert [r is None for r in out] == [True, True, True, False, False, False]
    assert all(isinstance(r, str) and r.strip() for r in out[3:])


def test_a_dropped_suggestion_does_not_use_up_one_of_the_cap():
    out = verdicts(suggestion(skill="python"), suggestion(reason=""), *MANY)
    assert [r is None for r in out] == [
        False,
        False,
        True,
        True,
        True,
        False,
        False,
        False,
    ]


@pytest.mark.parametrize("cap", [1, 2, 4, 6, 10])
def test_the_cap_argument_keeps_that_many_in_listed_order(cap):
    out = verdicts(*MANY, cap=cap)
    kept = min(cap, len(MANY))
    assert [r is None for r in out] == [True] * kept + [False] * (len(MANY) - kept)


# --- the cap setting (criteria 5 and 6; XC-14) --------------------------------------------


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def test_an_unset_cap_loads_as_three(profile):
    write_config_toml(profile, config_text())
    assert redline_settings(load_config()).skill_suggestion_cap == 3


@pytest.mark.parametrize("cap", [1, 2, 3, 7, 50])
def test_the_cap_is_read_from_the_redline_table(profile, cap):
    write_config_toml(profile, config_text(f"skill_suggestion_cap = {cap}"))
    config = load_config()
    assert config.agents.redline.skill_suggestion_cap == cap
    assert redline_settings(config).skill_suggestion_cap == cap


def test_the_example_profile_loads_with_the_default_cap(profile, tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_PROFILE_DIR", str(copy_example(tmp_path / "example")))
    assert redline_settings(load_config()).skill_suggestion_cap == 3


@pytest.mark.parametrize(
    "bad",
    ["0", "-1", "-5", "1.5", "2.0", '"3"', "true", "[]"],
    ids=[
        "zero",
        "negative",
        "very-negative",
        "fraction",
        "float-integer",
        "string",
        "bool",
        "array",
    ],
)
def test_a_cap_that_is_not_a_positive_integer_raises_at_load(profile, bad):
    write_config_toml(profile, config_text(f"skill_suggestion_cap = {bad}"))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_the_cap_is_not_a_setting_of_refine(profile):
    write_config_toml(profile, config_text(refine_extra="skill_suggestion_cap = 2"))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_the_cap_alone_does_not_set_a_time_limit(profile):
    write_config_toml(profile, config_text("skill_suggestion_cap = 2"))
    assert redline_settings(load_config()).wall_clock_seconds is None


# --- the agent's output (criterion 7) -----------------------------------------------------


def reply(*, suggestions=(), explanation=None, **lists):
    body = {
        "edits": [],
        "removals": [],
        "reorders": [],
        "flags": [],
        "skill_suggestions": list(suggestions),
        "explanation": explanation,
        **lists,
    }
    return json.dumps(body)


def test_a_reply_with_only_suggestions_is_accepted_with_a_null_explanation():
    proposal = parse_proposal(reply(suggestions=[asdict(suggestion())]))
    assert proposal.skill_suggestions == [suggestion()]
    assert proposal.explanation is None
    assert not (
        proposal.edits or proposal.removals or proposal.reorders or proposal.flags
    )


def test_a_reply_with_no_proposals_and_an_explanation_is_accepted():
    proposal = parse_proposal(reply(explanation=EXPLANATION))
    assert proposal.skill_suggestions == [] and proposal.explanation == EXPLANATION


def test_the_suggestions_arrive_in_the_agents_listed_order():
    listed = [suggestion(skill=name) for name in ("B", "A", "C")]
    proposal = parse_proposal(reply(suggestions=[asdict(s) for s in listed]))
    assert [s.skill for s in proposal.skill_suggestions] == ["B", "A", "C"]


def test_a_suggestion_alone_with_an_explanation_breaks_the_explanation_rule():
    with pytest.raises(JsaError):
        parse_proposal(reply(suggestions=[asdict(suggestion())], explanation="x"))


def test_no_proposals_and_no_explanation_breaks_the_explanation_rule():
    with pytest.raises(JsaError):
        parse_proposal(reply())


@pytest.mark.parametrize("missing", ["skill", "jd_quote", "reason"])
def test_every_suggestion_field_is_required(missing):
    fields = asdict(suggestion())
    del fields[missing]
    with pytest.raises(JsaError):
        parse_proposal(reply(suggestions=[fields]))


@pytest.mark.parametrize("field", ["skill", "jd_quote", "reason"])
@pytest.mark.parametrize("value", [None, 3, ["x"]], ids=["null", "number", "list"])
def test_a_suggestion_field_of_the_wrong_type_raises(field, value):
    with pytest.raises(JsaError):
        parse_proposal(reply(suggestions=[{**asdict(suggestion()), field: value}]))


def test_a_reply_without_the_skill_suggestions_key_raises():
    body = json.loads(reply(explanation=EXPLANATION))
    del body["skill_suggestions"]
    with pytest.raises(JsaError):
        parse_proposal(json.dumps(body))


# --- the redline document (criteria 1, 5, 7, 8) -------------------------------------------


@pytest.fixture
def stand_in(monkeypatch):
    state = SimpleNamespace(reply=reply(explanation=EXPLANATION), calls=[])

    async def query(*, prompt, options=None, **_ignored):
        state.calls.append(SimpleNamespace(prompt=prompt, options=options))
        yield result_message(result=state.reply)

    monkeypatch.setattr(agent_loop, "query", query)
    return state


def run(
    tmp_path,
    stand_in,
    suggestions=(),
    *,
    settings=SETTINGS,
    skills=SK_LIST,
    build=skills_resume,
    jd=JOB,
    **lists,
):
    stand_in.reply = reply(
        suggestions=[asdict(s) for s in suggestions],
        explanation=None if suggestions or any(lists.values()) else EXPLANATION,
        **lists,
    )
    copy = make_resume(tmp_path / "resume.docx", build)
    before = copy.read_bytes()
    record = tmp_path / "redline_edits.json"
    outcome = redline_resume(copy, record, jd, settings, skills)
    return SimpleNamespace(
        outcome=outcome, copy=copy, before=before, record=record, tmp=tmp_path
    )


def record_of(ran):
    return json.loads(ran.record.read_text(encoding="utf-8"))


def test_a_passing_suggestion_becomes_one_comment_and_changes_no_text(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion(reason="REASON-MARKER")])
    assert (ran.outcome.applied, ran.outcome.dropped) == (1, 0)
    assert view_of(ran.copy, "accept") == SK_PARAS
    assert view_of(ran.copy, "reject") == SK_PARAS
    assert not body_of(ran.copy).xpath("//w:ins | //w:del", namespaces=NS)
    assert edits_applied(ran.record)
    (text,) = comment_texts(ran.copy)
    assert "Canva" in text
    assert "polished slide decks in Canva" in text
    assert "REASON-MARKER" in text


def test_the_comment_is_a_question_not_a_claim(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [suggestion()])
    (text,) = comment_texts(ran.copy)
    assert "?" in text
    assert "None" not in text and "null" not in text


def test_the_comment_is_anchored_inside_the_skills_section_and_nowhere_else(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion()])
    paragraphs = body_of(ran.copy).find("w:body", NS).findall("w:p", NS)
    (comment,) = comments_of(ran.copy).findall("w:comment", NS)
    comment_id = comment.get(f"{{{W}}}id")
    anchored = [
        index
        for index, paragraph in enumerate(paragraphs)
        if paragraph.xpath(".//w:commentReference/@w:id", namespaces=NS)
    ]
    assert len(anchored) == 1 and anchored[0] in SK_SECTION
    for marker in ("commentRangeStart", "commentRangeEnd", "commentReference"):
        assert paragraphs[anchored[0]].xpath(f".//w:{marker}/@w:id", namespaces=NS) == [
            comment_id
        ]


def test_the_section_the_comment_lands_in_is_the_one_the_resume_marks_as_skills(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion()])
    import docx

    assert list(skills_section(docx.Document(str(ran.copy)))) == list(SK_SECTION)


def test_the_skills_text_is_untouched_when_a_suggestion_is_written(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, [suggestion()])
    document_text = "\n".join(view_of(ran.copy, "accept"))
    assert "Canva" not in document_text


def test_each_suggestion_gets_its_own_comment_with_a_unique_id(tmp_path, stand_in):
    ran = run(
        tmp_path,
        stand_in,
        [
            suggestion(skill="Canva", reason="FIRST-MARKER"),
            suggestion(
                skill="Figma",
                jd_quote="Build dashboards in Looker",
                reason="SECOND-MARKER",
            ),
        ],
    )
    assert ran.outcome.applied == 2
    comments = comments_of(ran.copy).findall("w:comment", NS)
    ids = [c.get(f"{{{W}}}id") for c in comments]
    assert len(set(ids)) == 2
    texts = comment_texts(ran.copy)
    assert any("FIRST-MARKER" in t for t in texts)
    assert any("SECOND-MARKER" in t for t in texts)


def test_a_suggestion_whose_skill_never_appears_in_the_resume_body_is_still_written(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion(skill="Canva")])
    assert "Canva" not in " ".join(SK_PARAS)
    assert ran.outcome.applied == 1


def test_a_suggestion_for_a_skill_inside_a_longer_word_is_written(tmp_path, stand_in):
    build = skills_resume
    ran = run(
        tmp_path,
        stand_in,
        [suggestion(skill="R", jd_quote="Write R and Java daily")],
        skills=SK_LIST,
        build=build,
    )
    assert ran.outcome.applied == 1


def test_a_dropped_suggestion_is_counted_and_leaves_the_copy_byte_identical(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion(skill="Looker")])
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before


def test_with_no_skills_list_in_use_a_suggestion_is_dropped_and_recorded(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion()], skills=())
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before
    (entry,) = record_of(ran)["skill_suggestions"]
    assert isinstance(entry["validation"], str) and entry["validation"].strip()


def test_with_no_skills_section_a_suggestion_is_dropped_and_recorded(
    tmp_path, stand_in
):
    def no_skills_heading(document):
        for text in (
            "Riley Resumeperson",
            "Python, SQL",
            "Built the Quuxlate platform.",
        ):
            document.add_paragraph(text)

    ran = run(tmp_path, stand_in, [suggestion()], build=no_skills_heading)
    assert (ran.outcome.applied, ran.outcome.dropped) == (0, 1)
    assert ran.copy.read_bytes() == ran.before
    (entry,) = record_of(ran)["skill_suggestions"]
    assert entry["validation"]


def test_by_default_three_of_four_passing_suggestions_are_written(tmp_path, stand_in):
    ran = run(tmp_path, stand_in, MANY[:4])
    assert (ran.outcome.applied, ran.outcome.dropped) == (3, 1)
    assert len(comment_texts(ran.copy)) == 3
    texts = " ".join(comment_texts(ran.copy))
    for name in ("Canva", "Figma", "Miro"):
        assert name in texts
    assert "Notion2" not in texts
    entries = record_of(ran)["skill_suggestions"]
    assert [e["validation"] is None for e in entries] == [True, True, True, False]


@pytest.mark.parametrize("cap", [1, 2, 5])
def test_a_configured_cap_sets_how_many_comments_are_written(tmp_path, stand_in, cap):
    settings = AgentSettings(
        model="claude-opus-5-5", effort="medium", skill_suggestion_cap=cap
    )
    ran = run(tmp_path, stand_in, MANY[:5], settings=settings)
    assert ran.outcome.applied == min(cap, 5)
    assert ran.outcome.dropped == 5 - min(cap, 5)
    assert len(comment_texts(ran.copy)) == min(cap, 5)


def test_the_flag_cap_and_the_suggestion_cap_are_counted_separately(tmp_path, stand_in):
    flags = [
        {
            "paragraph": 5,
            "jd_quote": "Own the Quuxlate system end to end.",
            "suggestion": f"FLAG-{n}",
            "reason": "unsure",
        }
        for n in range(3)
    ]
    ran = run(tmp_path, stand_in, MANY[:3], flags=flags)
    assert ran.outcome.applied == 6 and ran.outcome.dropped == 0
    assert len(comment_texts(ran.copy)) == 6


def test_the_record_carries_every_suggestion_with_its_validation_result(
    tmp_path, stand_in
):
    ran = run(
        tmp_path,
        stand_in,
        [suggestion(), suggestion(skill="python"), suggestion(reason="")],
    )
    record = record_of(ran)
    assert set(record) == {
        "explanation",
        "edits",
        "removals",
        "reorders",
        "flags",
        "skill_suggestions",
    }
    assert record["explanation"] is None
    first, second, third = record["skill_suggestions"]
    assert first == {**asdict(suggestion()), "validation": None}
    assert second["skill"] == "python" and second["validation"]
    assert third["reason"] == "" and third["validation"]


def test_a_run_with_nothing_to_propose_records_an_empty_skill_suggestions_array(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [])
    record = record_of(ran)
    assert record["skill_suggestions"] == []
    assert record["explanation"] == EXPLANATION
    assert ran.copy.read_bytes() == ran.before


def test_a_suggestion_alone_counts_as_a_proposal_for_the_applied_count(
    tmp_path, stand_in
):
    ran = run(tmp_path, stand_in, [suggestion()])
    assert record_of(ran)["explanation"] is None
    assert ran.outcome.applied == 1


def test_the_prompt_the_agent_receives_names_the_kind_and_asks_for_a_question(
    tmp_path, stand_in
):
    run(tmp_path, stand_in, [])
    (call,) = stand_in.calls
    lowered = call.prompt.lower()
    assert "skill_suggestions" in call.prompt
    assert "skill suggestion" in lowered
    assert "never assert" in lowered or "never as a claim" in lowered
    assert re.search(r"all five", lowered)
    assert "{{" not in call.prompt


# --- the skills file is the user's alone (criterion 8) ------------------------------------


def test_a_redline_run_never_writes_the_skills_file(tmp_path, stand_in):
    skills_file = tmp_path / "resume_skills.md"
    skills_file.write_text("\n".join(SK_LIST), encoding="utf-8")
    before = (skills_file.read_bytes(), skills_file.stat().st_mtime_ns)
    run(tmp_path, stand_in, [suggestion()])
    assert (skills_file.read_bytes(), skills_file.stat().st_mtime_ns) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "redline_edits.json",
        "resume.docx",
        "resume_skills.md",
    ]


# --- the example profile's live-check case (criterion 9) ----------------------------------


def test_the_example_profile_has_a_posting_naming_a_skill_its_resumes_lack(tmp_path):
    import docx
    from profile_helpers import EXAMPLE_DIR

    posting = (EXAMPLE_DIR / "postings" / "skill_suggestion.md").read_text(
        encoding="utf-8"
    )
    skills = (EXAMPLE_DIR / "resume_skills.md").read_text(encoding="utf-8")
    assert re.search(r"\bCanva\b", posting)
    assert not re.search(r"canva", skills, re.IGNORECASE)
    resumes = sorted((EXAMPLE_DIR / "role_families").glob("*/resume.docx"))
    assert resumes
    for path in resumes:
        document = docx.Document(str(path))
        paragraphs = [p.text for p in document.paragraphs]
        section = skills_section(document)
        text = "\n".join(paragraphs[i] for i in (section or ()))
        assert not re.search(r"canva", text, re.IGNORECASE), path


def test_the_example_postings_skill_passes_validation_against_the_example_resumes(
    tmp_path,
):
    import docx
    from profile_helpers import EXAMPLE_DIR

    from jsa.redline import parse_skills

    posting = (EXAMPLE_DIR / "postings" / "skill_suggestion.md").read_text(
        encoding="utf-8"
    )
    skills = parse_skills(
        (EXAMPLE_DIR / "resume_skills.md").read_text(encoding="utf-8")
    )
    for path in sorted((EXAMPLE_DIR / "role_families").glob("*/resume.docx")):
        document = docx.Document(str(path))
        section = skills_section(document)
        if section is None:
            continue
        paragraphs = [p.text for p in document.paragraphs]
        quote = next(line for line in posting.splitlines() if "Canva" in line)
        (result,) = validate_skill_suggestions(
            [suggestion(jd_quote=quote.strip("- ").strip())],
            paragraphs,
            posting,
            skills,
            section,
        )
        assert result is None, path
