"""The redline's prompt template and the edge inputs of its validators (issue #152; PRD 04 "Redline", "Prompt").

The prompt is what the model is given, so its stated rules are asserted from the filled template.
"""

import re

import pytest
from test_redline import JD, PARAGRAPHS, edit, results

from jsa.redline import assemble_redline_prompt


@pytest.fixture(scope="module")
def prompt() -> str:
    return assemble_redline_prompt(JD, PARAGRAPHS, ["SQL"], [2])


def test_the_prompt_states_it_is_not_a_resume_critique_and_that_proposals_cite_the_posting(
    prompt,
):
    lowered = prompt.lower()
    assert "not a resume critique" in lowered
    assert "verbatim" in lowered and "posting" in lowered


def test_the_prompt_names_what_is_never_changed(prompt):
    lowered = prompt.lower()
    for held in ("employer", "job title", "date", "degree", "number"):
        assert held in lowered, held


def test_the_prompt_says_the_headline_never_claims_unsupported_seniority_or_credentials(
    prompt,
):
    lowered = prompt.lower()
    assert "headline" in lowered
    assert "seniority" in lowered and "credential" in lowered


def test_the_prompt_tells_the_agent_how_to_recognise_the_headline_and_the_summary(
    prompt,
):
    for heading in ("Summary", "Profile", "About"):
        assert heading in prompt
    assert "under the name and contact details" in prompt


def test_the_prompt_says_headline_summary_and_bullet_edits_bring_in_no_new_facts(
    prompt,
):
    lowered = prompt.lower()
    assert "wording and emphasis" in lowered
    assert "new fact" in lowered


def test_the_prompt_tells_the_agent_to_flag_only_when_unsure_and_high_in_value(prompt):
    lowered = prompt.lower()
    assert "unsure" in lowered
    assert re.search(r"matter|valu", lowered)
    assert "change nothing" in lowered or "never changes" in lowered


def test_the_prompt_names_the_two_causes_an_empty_result_chooses_between(prompt):
    lowered = prompt.lower()
    assert "well aligned" in lowered
    assert "share so little" in lowered or "little in common" in lowered


def test_the_prompt_says_zero_proposals_is_a_valid_answer(prompt):
    assert "zero proposals" in prompt.lower()


def test_the_prompt_holds_the_posting_and_each_numbered_paragraph(prompt):
    assert "Own the Quuxlate system end to end." in prompt
    assert "1: Built the Quuxlate platform from scratch." in prompt


def test_the_prompt_names_no_candidate_from_the_example_profile(prompt):
    for name in ("Jordan", "Alex", "Taylor"):
        assert name not in prompt


# --- edge inputs the spec promises a drop for ------------------------------------


def test_an_empty_find_is_dropped():
    assert results(edit(find="", replace="Quuxlate"))[0]


def test_an_edit_is_validated_against_its_own_paragraph_not_a_neighbours():
    assert results(edit(paragraph=2))[0]


# --- removals and reorders (issue #153; PRD 04 "Redline", "Prompt") ------------------


def section_of(prompt: str, field: str, following: str) -> str:
    start = prompt.rindex(f"- `{field}`:")
    return prompt[start : prompt.index(f"- `{following}`:", start + 1)]


def test_the_prompt_names_removals_and_reorders_as_kinds_of_proposal(prompt):
    lowered = prompt.lower()
    assert "removal" in lowered and "reorder" in lowered
    assert "tracked" in lowered


def test_the_prompt_says_a_removal_cuts_what_detracts_and_a_reorder_leads_with_what_the_posting_rewards(
    prompt,
):
    lowered = prompt.lower()
    assert "detract" in lowered
    assert "narrative" in lowered and "positioning" in lowered
    assert re.search(r"lead\w* with|first", lowered)


def test_the_prompt_says_a_bullet_never_leaves_its_role_and_a_role_is_never_left_bare(
    prompt,
):
    lowered = prompt.lower()
    assert re.search(r"(out of|leave|leaves|outside) (the|its|a|their) role", lowered)
    assert re.search(
        r"never left bare|no bullet|every bullet|all (of )?its bullets", lowered
    )


def test_the_prompt_says_a_reorder_lists_only_the_bullets_that_survive_the_agents_own_removals(
    prompt,
):
    lowered = prompt.lower()
    assert re.search(r"surviv|remain|not remov|never one you remove", lowered)
    assert "removals" in lowered


def test_the_prompt_says_a_removal_is_bullets_only_and_not_in_the_skills_section(
    prompt,
):
    lowered = prompt.lower()
    assert re.search(r"removals? (apply|applies|don't apply|do not apply)", lowered)
    assert "skills" in lowered


def test_the_prompt_explains_the_bullet_and_bold_tags(prompt):
    assert "[bullet]" in prompt and "[bold]" in prompt
    lowered = prompt.lower()
    assert re.search(r"bold[^.]*(role|section|head)", lowered)


def test_the_prompt_asks_for_the_five_fields_in_its_output_contract(prompt):
    output = (
        prompt[prompt.rindex("# Your output") :]
        if "# Your output" in prompt
        else prompt
    )
    for field in ("edits", "removals", "reorders", "flags", "explanation"):
        assert f"`{field}`" in output, field


def test_a_removal_is_asked_for_a_paragraph_a_quote_and_a_reason(prompt):
    block = section_of(prompt, "removals", "reorders")
    for field in ("paragraph", "jd_quote", "reason"):
        assert field in block, field


def test_a_reorder_is_asked_for_the_paragraphs_in_their_new_order_a_quote_and_a_reason(
    prompt,
):
    block = section_of(prompt, "reorders", "flags")
    for field in ("paragraphs", "jd_quote", "reason"):
        assert field in block, field
    assert "order" in block.lower()


def test_the_explanation_is_empty_exactly_when_all_four_arrays_are(prompt):
    block = prompt[prompt.rindex("`explanation`") :].lower()
    assert "null" in block
    assert re.search(r"all four|all of", block)
    assert "well aligned" in block
    assert re.search(r"share so little|little in common", block)


def test_the_filled_prompt_has_no_unfilled_marker(prompt):
    assert "{{" not in prompt and "}}" not in prompt


def test_the_prompt_tags_bullets_it_is_given():
    tagged = assemble_redline_prompt(JD, PARAGRAPHS, [], [], [], [2, 3])
    lines = {
        line.split(":")[0]: line
        for line in tagged.splitlines()
        if re.match(r"\d+[ :]", line)
    }
    assert "[bullet]" in lines["2 [bullet]"] and "[bullet]" in lines["3 [bullet]"]
    assert "1" in lines and "[bullet]" not in lines["1"]
