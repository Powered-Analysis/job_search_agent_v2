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
