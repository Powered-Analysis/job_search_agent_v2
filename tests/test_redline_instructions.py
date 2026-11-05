"""The redline's candidate `instructions` (issue #238; PRD 04 "Redline", "Prompt", "Configuration and secrets"; `XC-13`, `XC-14`).

The config tests read `[agents.redline]` through `load_config`. The prompt tests fill the real template. The
run tests drive `redline_resume` with Claude replaced at the shared agent loop and read `redline_edits.json`.
"""

import json
from types import SimpleNamespace

import pytest
from profile_helpers import copy_example, write_config_toml
from test_claude_runner import result_message
from test_redline import JD, PARAGRAPHS, SETTINGS, as_dict, edit, make_resume
from test_wall_clock_config import config_text

from jsa import agent_loop
from jsa.assemble import OPTIONAL_PLACEHOLDER, app_template
from jsa.errors import JsaError
from jsa.profile import load_config, redline_settings
from jsa.redline import assemble_redline_prompt, redline_resume

RULE = (
    "Lead the headline with data storytelling when the posting stresses communication."
)
HEADING = "# The candidate's own instructions"
NEXT_HEADING = "# What you may propose"
BOUNDARY_HEADING = "# The boundary"


def prompt_with(instructions=None, **kw) -> str:
    return assemble_redline_prompt(
        JD, PARAGRAPHS, ["SQL"], [2], instructions=instructions, **kw
    )


def instructions_section(prompt: str) -> str:
    start = prompt.index(HEADING)
    return prompt[start : prompt.index(NEXT_HEADING, start)]


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


# --- config: where the key is read from (criterion 1, 2) -----------------------------------


def test_an_unset_instructions_key_loads_as_none(profile):
    write_config_toml(profile, config_text())
    assert redline_settings(load_config()).instructions is None


def test_the_instructions_are_read_from_the_redline_table(profile):
    write_config_toml(profile, config_text(f'instructions = "{RULE}"'))
    assert redline_settings(load_config()).instructions == RULE


def test_a_multi_line_instructions_value_is_kept_whole(profile):
    write_config_toml(
        profile, config_text('instructions = """\nFirst rule.\n\nSecond rule.\n"""')
    )
    text = redline_settings(load_config()).instructions
    assert "First rule." in text and "Second rule." in text


def test_the_example_profile_loads_with_no_instructions(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_PROFILE_DIR", str(copy_example(tmp_path / "example")))
    assert redline_settings(load_config()).instructions is None


@pytest.mark.parametrize("bad", ["5", "true", "[]", '["a"]', "1.5"])
def test_instructions_that_are_not_text_raise_at_load(profile, bad):
    write_config_toml(profile, config_text(f"instructions = {bad}"))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_instructions_are_not_a_setting_of_refine(profile):
    write_config_toml(profile, config_text(refine_extra='instructions = "x"'))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_instructions_alone_set_no_time_limit_and_keep_the_default_cap(profile):
    write_config_toml(profile, config_text(f'instructions = "{RULE}"'))
    settings = redline_settings(load_config())
    assert settings.wall_clock_seconds is None
    assert settings.skill_suggestion_cap == 3


# --- the prompt: unset (criterion 1) ---------------------------------------------------------


def test_unset_instructions_leave_a_fixed_placeholder_in_the_section():
    section = instructions_section(prompt_with())
    assert OPTIONAL_PLACEHOLDER in section


@pytest.mark.parametrize("unset", [None, "", "   ", "\n\t \n"])
def test_text_that_is_missing_or_blank_gives_the_placeholder(unset):
    assert prompt_with(unset) == prompt_with()


def test_the_prompt_is_the_same_whatever_stands_in_the_slot_except_that_slot():
    marker = "ZZ-INSTRUCTIONS-MARKER"
    assert prompt_with(marker).replace(marker, OPTIONAL_PLACEHOLDER) == prompt_with()


def test_the_template_keeps_the_slot_marker_exactly_once():
    assert app_template("redline.md").count("{{CANDIDATE_INSTRUCTIONS}}") == 1


def test_no_unfilled_marker_remains_in_the_prompt():
    assert "{{" not in prompt_with() and "{{" not in prompt_with(RULE)


def test_the_slot_is_optional_so_the_prompt_assembles_without_it():
    assert assemble_redline_prompt(JD, PARAGRAPHS, ["SQL"], [2])


# --- the prompt: set (criterion 2) -----------------------------------------------------------


def test_the_instructions_text_appears_in_the_prompt_in_its_own_section():
    section = instructions_section(prompt_with(RULE))
    assert RULE in section
    assert OPTIONAL_PLACEHOLDER not in section
    assert prompt_with(RULE).count(RULE) == 1


def test_the_instructions_are_read_before_any_proposal_kind_is_described():
    prompt = prompt_with(RULE)
    assert prompt.index(RULE) < prompt.index(NEXT_HEADING)
    assert prompt.index(RULE) < prompt.index(BOUNDARY_HEADING)


def test_the_section_states_the_candidates_instruction_wins_over_a_conflicting_template_rule():
    lowered = instructions_section(prompt_with(RULE)).lower()
    assert "conflict" in lowered
    assert "instruction wins" in lowered or "instructions win" in lowered
    assert "template" in lowered


def test_the_section_limits_that_to_judgment_and_excludes_the_boundary_and_validation():
    lowered = instructions_section(prompt_with(RULE)).lower()
    assert "judgment" in lowered
    assert "boundary" in lowered
    assert "dropped" in lowered


def test_the_section_says_what_to_do_when_the_candidate_wrote_nothing():
    lowered = instructions_section(prompt_with()).lower()
    assert "no instructions" in lowered or "written no" in lowered


def test_instructions_are_stripped_of_surrounding_whitespace():
    assert f"\n\n{RULE}\n\n" in prompt_with(f"\n\n  {RULE}  \n\n")


def test_instructions_with_slot_markers_of_their_own_are_not_expanded():
    prompt = prompt_with("Use {{SKILLS}} and {{NOPE}} literally.")
    assert "Use {{SKILLS}} and {{NOPE}} literally." in prompt


def test_the_other_slots_are_unchanged_by_the_instructions():
    plain, set_ = prompt_with(), prompt_with(RULE)
    for needle in ("Own the Quuxlate system end to end.", "1: Built the Quuxlate"):
        assert needle in plain and needle in set_
    assert set_.replace(RULE, OPTIONAL_PLACEHOLDER) == plain


def test_a_multi_line_instruction_is_kept_verbatim():
    text = "Rule one.\n\n- Rule two\n- Rule three"
    assert text in prompt_with(text)


# --- the template names no candidate (criterion 3) -------------------------------------------


@pytest.mark.parametrize(
    "needle",
    [
        "Nicky",
        "Bell",
        "SPECIAL INSTRUCTION",
        "Regulated Industries",
        "If and only if the candidate",
    ],
)
def test_the_redline_template_has_no_candidate_specific_rule(needle):
    assert needle not in app_template("redline.md")


def test_the_example_profile_still_assembles_a_redline_prompt():
    assert OPTIONAL_PLACEHOLDER in prompt_with()


# --- other templates untouched (criterion 5) -------------------------------------------------


@pytest.mark.parametrize("name", ["search.md", "refine.md"])
def test_the_search_and_refine_templates_have_no_candidate_instructions_slot(name):
    assert "CANDIDATE_INSTRUCTIONS" not in app_template(name)


# --- a redline run (criteria 2 and 4) --------------------------------------------------------

BOUNDARY_JD = (
    "Own the Quuxlate system end to end. Bring 5+ years of experience.\n"
    "Use SQL every day."
)


@pytest.fixture
def claude(monkeypatch):
    state = SimpleNamespace(reply="", calls=[])

    async def query(*, prompt, options=None, **_ignored):
        state.calls.append(SimpleNamespace(prompt=prompt, options=options))
        yield result_message(result=state.reply)

    monkeypatch.setattr(agent_loop, "query", query)
    return state


def run_with(tmp_path, claude, edits, *, instructions, jd=JD):
    claude.reply = json.dumps(
        {
            "edits": edits,
            "removals": [],
            "reorders": [],
            "flags": [],
            "skill_suggestions": [],
            "explanation": None if edits else "Nothing to change.",
        }
    )
    copy = make_resume(tmp_path / "resume.docx")
    edits_file = tmp_path / "redline_edits.json"
    settings = SETTINGS.model_copy(update={"instructions": instructions})
    outcome = redline_resume(copy, edits_file, jd, settings, ())
    return outcome, json.loads(edits_file.read_text(encoding="utf-8"))


def test_the_prompt_sent_to_claude_carries_the_configured_instructions(
    tmp_path, claude
):
    run_with(tmp_path, claude, [], instructions=RULE)
    (call,) = claude.calls
    assert RULE in instructions_section(call.prompt)


def test_the_prompt_sent_to_claude_carries_the_placeholder_when_none_are_set(
    tmp_path, claude
):
    run_with(tmp_path, claude, [], instructions=None)
    (call,) = claude.calls
    assert OPTIONAL_PLACEHOLDER in instructions_section(call.prompt)


def test_an_edit_an_instruction_motivates_that_adds_a_claimed_length_of_experience_is_dropped_and_recorded(
    tmp_path, claude
):
    bad = edit(
        paragraph=0,
        find="Riley Resumeperson",
        replace="Riley Resumeperson, 5+ years of experience",
        goal="narrative",
        jd_quote="Bring 5+ years of experience.",
        reason="the candidate's instruction says to always add it",
    )
    outcome, record = run_with(
        tmp_path,
        claude,
        [as_dict(bad)],
        instructions="Always add 5+ years of experience to the headline.",
        jd=BOUNDARY_JD,
    )
    assert (outcome.applied, outcome.dropped) == (0, 1)
    (entry,) = record["edits"]
    assert entry["replace"] == bad.replace
    assert isinstance(entry["validation"], str) and entry["validation"].strip()


def test_an_instruction_does_not_save_an_edit_that_changes_a_number_of_record(
    tmp_path, claude
):
    bad = edit(
        paragraph=3,
        find="30%",
        replace="50%",
        goal="narrative",
        jd_quote="Reduced costs matter",
        reason="the candidate's instruction asks for bigger numbers",
    )
    outcome, record = run_with(
        tmp_path,
        claude,
        [as_dict(bad)],
        instructions="Make every metric as strong as possible.",
    )
    assert (outcome.applied, outcome.dropped) == (0, 1)
    assert record["edits"][0]["validation"]


def test_a_valid_edit_beside_a_boundary_breaking_one_still_applies_under_instructions(
    tmp_path, claude
):
    bad = edit(
        paragraph=0,
        find="Riley Resumeperson",
        replace="Riley Resumeperson, 5+ years of experience",
        goal="narrative",
        jd_quote="Bring 5+ years of experience.",
    )
    outcome, record = run_with(
        tmp_path,
        claude,
        [as_dict(bad), as_dict(edit())],
        instructions="Always add 5+ years of experience to the headline.",
        jd=BOUNDARY_JD,
    )
    assert (outcome.applied, outcome.dropped) == (1, 1)
    assert [e["validation"] is None for e in record["edits"]] == [False, True]


def test_the_same_boundary_breaking_edit_is_dropped_with_and_without_instructions(
    tmp_path, claude
):
    bad = edit(
        paragraph=0,
        find="Riley Resumeperson",
        replace="Riley Resumeperson, 5+ years of experience",
        goal="narrative",
        jd_quote="Bring 5+ years of experience.",
    )
    outcomes = []
    for index, instructions in enumerate((None, "Always add 5+ years.")):
        sub = tmp_path / str(index)
        sub.mkdir()
        outcomes.append(
            run_with(
                sub, claude, [as_dict(bad)], instructions=instructions, jd=BOUNDARY_JD
            )[1]["edits"][0]["validation"]
        )
    assert outcomes[0] and outcomes[1]
    assert outcomes[0] == outcomes[1]
