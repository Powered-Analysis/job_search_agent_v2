"""PRD 01 "Search prompt contract" (XC-13): the one prompt every runner sends."""

import re
from datetime import UTC, datetime, timedelta, timezone

import pytest
from profile_helpers import (
    EXAMPLE_DIR,
    FRAGMENTS,
    SEARCH_TOML,
    copy_example,
    write_fragment,
    write_search_toml,
)

from jsa.assemble import OPTIONAL_PLACEHOLDER, app_template
from jsa.errors import JsaError
from jsa.profile import load_search_config
from jsa.search_prompt import (
    assemble_search_prompt,
    assemble_search_prompt_for,
    search_window,
)

REQUIRED = ("candidate", "target_roles", "filters")
OPTIONAL = ("positive_signals", "negative_signals", "hard_exclusions")
NOW = datetime(2026, 3, 10, 3, 30, tzinfo=UTC)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def marked(profile):
    """Replace every fragment with text unique to it."""
    for name in FRAGMENTS:
        write_fragment(
            profile, name, f"UNIQUE-{name.upper()}-TEXT about something specific."
        )


def set_mode(profile, mode):
    path = profile / "search" / "search.toml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^mode\s*=.*$", f'mode = "{mode}"', text, flags=re.MULTILINE)
    path.write_text(text, encoding="utf-8")


def assembled(hours=24, now=NOW):
    return assemble_search_prompt(load_search_config(), hours, now)


# --- the assembled prompt ---------------------------------------------------


def test_the_assembled_prompt_has_no_leftover_markers(profile):
    assert "{{" not in assembled()
    assert "}}" not in assembled()


def test_the_assembled_prompt_contains_each_fragments_text(profile):
    marked(profile)
    prompt = assembled()
    for name in FRAGMENTS:
        assert f"UNIQUE-{name.upper()}-TEXT about something specific." in prompt, name


def test_the_example_profiles_own_fragments_appear_in_the_prompt(profile):
    prompt = assembled()
    for name in FRAGMENTS:
        text = (
            (EXAMPLE_DIR / "search" / f"{name}.md").read_text(encoding="utf-8").strip()
        )
        assert text in prompt, name


def test_the_strict_prompt_carries_the_strict_liveness_rules_and_not_the_other(profile):
    set_mode(profile, "strict")
    prompt = assembled()
    assert app_template("liveness_strict.md").strip() in prompt
    assert app_template("liveness_best_effort.md").strip() not in prompt


def test_the_best_effort_prompt_carries_the_best_effort_liveness_rules_and_not_the_other(
    profile,
):
    set_mode(profile, "best_effort")
    prompt = assembled()
    assert app_template("liveness_best_effort.md").strip() in prompt
    assert app_template("liveness_strict.md").strip() not in prompt


def test_the_two_liveness_rule_files_differ():
    assert (
        app_template("liveness_strict.md").strip()
        != app_template("liveness_best_effort.md").strip()
    )


def test_the_prompt_is_the_same_for_the_same_inputs(profile):
    assert assembled() == assembled()


def test_fragment_text_is_not_trimmed_into_the_wrong_slot(profile):
    marked(profile)
    prompt = assembled()
    # Each fragment appears exactly once: a slot is filled once, from its own file.
    for name in FRAGMENTS:
        assert prompt.count(f"UNIQUE-{name.upper()}-TEXT") == 1, name


# --- required and optional fragments ----------------------------------------


@pytest.mark.parametrize("name", REQUIRED)
def test_a_missing_required_fragment_raises_naming_the_file_and_the_example(
    profile, name
):
    (profile / "search" / f"{name}.md").unlink()
    with pytest.raises(JsaError) as raised:
        assembled()
    message = str(raised.value)
    assert f"{name}.md" in message
    assert "profile.example/search" in message


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("content", ["", "  \n\n \t"], ids=["empty", "blank"])
def test_an_empty_required_fragment_raises_naming_the_file_and_the_example(
    profile, name, content
):
    write_fragment(profile, name, content)
    with pytest.raises(JsaError) as raised:
        assembled()
    message = str(raised.value)
    assert f"{name}.md" in message
    assert "profile.example/search" in message


@pytest.mark.parametrize("name", OPTIONAL)
def test_a_missing_optional_fragment_fills_its_slot_with_the_placeholder(profile, name):
    marked(profile)
    (profile / "search" / f"{name}.md").unlink()
    prompt = assembled()
    assert OPTIONAL_PLACEHOLDER in prompt
    assert "{{" not in prompt
    assert f"UNIQUE-{name.upper()}-TEXT" not in prompt


@pytest.mark.parametrize("name", OPTIONAL)
def test_an_empty_optional_fragment_fills_its_slot_with_the_placeholder(profile, name):
    write_fragment(profile, name, "")
    assert OPTIONAL_PLACEHOLDER in assembled()


def test_with_every_optional_fragment_present_no_placeholder_appears(profile):
    marked(profile)
    assert OPTIONAL_PLACEHOLDER not in assembled()


def test_with_all_optional_fragments_absent_the_prompt_still_assembles(profile):
    marked(profile)
    for name in OPTIONAL:
        (profile / "search" / f"{name}.md").unlink()
    prompt = assembled()
    assert prompt.count(OPTIONAL_PLACEHOLDER) >= len(OPTIONAL)
    for name in REQUIRED:
        assert f"UNIQUE-{name.upper()}-TEXT" in prompt


# --- the search window ------------------------------------------------------


def test_the_window_reads_the_last_n_hours_from_through(profile):
    window = search_window(24, NOW, load_search_config())
    assert window.startswith("the last 24 hours (from ")
    assert " through " in window
    assert window.endswith(")")


def test_the_window_is_in_the_profiles_timezone_not_utc(profile):
    # 03:30 UTC on 2026-03-10 is 23:30 on 2026-03-09 in New York (already on daylight time).
    window = search_window(24, NOW, load_search_config())
    assert "2026-03-09" in window  # the end
    assert "2026-03-08" in window  # the start, 24 hours earlier
    assert "2026-03-10" not in window


def test_the_window_follows_a_different_profile_timezone(profile):
    write_search_toml(
        profile, SEARCH_TOML.replace("America/New_York", "Pacific/Auckland")
    )
    window = search_window(24, NOW, load_search_config())
    # 03:30 UTC is 16:30 on the 10th in Auckland; 24 hours earlier is the 9th.
    assert "2026-03-10" in window
    assert "2026-03-09" in window
    assert "2026-03-08" not in window


def test_the_window_does_not_depend_on_the_instants_own_offset(profile):
    config = load_search_config()
    other_zone = timezone(timedelta(hours=9))
    assert search_window(24, NOW, config) == search_window(
        24, NOW.astimezone(other_zone), config
    )


def test_the_window_start_is_n_hours_before_now(profile):
    config = load_search_config()
    window = search_window(72, NOW, config)
    assert window.startswith("the last 72 hours (from ")
    # 03:30 UTC on the 10th less 72 hours is 03:30 UTC on the 7th, i.e. the 6th in New York.
    assert "2026-03-06" in window
    assert "2026-03-09" in window


def test_the_assembled_prompt_contains_the_rendered_window(profile):
    window = search_window(48, NOW, load_search_config())
    assert window in assembled(hours=48)


def test_the_window_in_the_prompt_changes_with_the_hours(profile):
    assert "the last 24 hours" in assembled(hours=24)
    assert "the last 96 hours" in assembled(hours=96)


# --- the app templates ------------------------------------------------------


def test_the_search_template_has_a_marker_for_every_slot_in_the_prd_table():
    template = app_template("search.md")
    for slot in (
        "SEARCH_WINDOW",
        "LIVENESS_RULES",
        "CANDIDATE",
        "TARGET_ROLES",
        "FILTERS",
        "POSITIVE_SIGNALS",
        "NEGATIVE_SIGNALS",
        "HARD_EXCLUSIONS",
    ):
        assert "{{" + slot + "}}" in template, slot


def test_the_search_template_has_no_marker_beyond_the_prd_table():
    markers = set(re.findall(r"\{\{([A-Z0-9_]+)\}\}", app_template("search.md")))
    assert markers == {
        "SEARCH_WINDOW",
        "LIVENESS_RULES",
        "CANDIDATE",
        "TARGET_ROLES",
        "FILTERS",
        "POSITIVE_SIGNALS",
        "NEGATIVE_SIGNALS",
        "HARD_EXCLUSIONS",
    }


def test_the_search_template_holds_no_candidate_specific_content():
    template = app_template("search.md")
    for name in FRAGMENTS:
        text = (EXAMPLE_DIR / "search" / f"{name}.md").read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip(" -*#\t")
            if len(line) >= 20:
                assert line not in template, line
    assert "Jordan Example" not in template


@pytest.mark.parametrize(
    "name", ["search.md", "liveness_strict.md", "liveness_best_effort.md"]
)
def test_no_app_prompt_file_holds_candidate_specific_content(name):
    text = app_template(name)
    assert "Jordan Example" not in text
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)


@pytest.mark.parametrize("mode", ["strict", "best_effort"])
def test_the_prompt_holds_the_liveness_gates_in_both_modes(profile, mode):
    set_mode(profile, mode)
    prompt = assembled().lower()
    assert "aggregator" in prompt
    assert "job-boards.greenhouse.io" in prompt


def test_the_search_template_states_the_recall_first_fit_policy_and_the_override():
    text = app_template("search.md").lower()
    assert "recall" in text
    assert "override" in text


def test_the_search_template_tells_a_thin_window_to_broaden_discovery():
    assert "broaden" in app_template("search.md").lower()


def test_the_search_template_states_the_output_contract_it_owns():
    text = app_template("search.md")
    assert "postings" in text
    assert "date_posted" in text


# --- a directory of replacement fragments ------------------------------------


@pytest.fixture
def replacements(tmp_path):
    path = tmp_path / "replacements"
    path.mkdir()
    return path


def assembled_with(replacements):
    return assemble_search_prompt_for(
        load_search_config(), "a test window", replacements
    )


def test_a_fragment_in_the_replacements_directory_is_used_in_place_of_the_live_one(
    profile, replacements
):
    marked(profile)
    (replacements / "filters.md").write_text("REPLACED-FILTERS-TEXT", encoding="utf-8")
    prompt = assembled_with(replacements)
    assert "REPLACED-FILTERS-TEXT" in prompt
    assert "UNIQUE-FILTERS-TEXT" not in prompt


def test_a_fragment_absent_from_the_replacements_directory_comes_from_the_live_profile(
    profile, replacements
):
    marked(profile)
    (replacements / "filters.md").write_text("REPLACED-FILTERS-TEXT", encoding="utf-8")
    prompt = assembled_with(replacements)
    for name in FRAGMENTS:
        if name != "filters":
            assert f"UNIQUE-{name.upper()}-TEXT" in prompt, name


def test_replacing_a_fragment_leaves_the_live_file_untouched(profile, replacements):
    marked(profile)
    live = profile / "search" / "filters.md"
    before = live.read_bytes()
    (replacements / "filters.md").write_text("REPLACED-FILTERS-TEXT", encoding="utf-8")
    assembled_with(replacements)
    assert live.read_bytes() == before


def test_an_empty_replacements_directory_assembles_the_same_prompt_as_none(
    profile, replacements
):
    window = "a test window"
    assert assembled_with(replacements) == assemble_search_prompt_for(
        load_search_config(), window
    )


def test_a_replacement_supplies_a_required_fragment_the_live_profile_lacks(
    profile, replacements
):
    (profile / "search" / "filters.md").unlink()
    (replacements / "filters.md").write_text("REPLACED-FILTERS-TEXT", encoding="utf-8")
    assert "REPLACED-FILTERS-TEXT" in assembled_with(replacements)


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("content", ["", "  \n\n \t"], ids=["empty", "blank"])
def test_an_empty_replacement_of_a_required_fragment_raises_naming_the_replacement_file(
    profile, replacements, name, content
):
    marked(profile)
    (replacements / f"{name}.md").write_text(content, encoding="utf-8")
    with pytest.raises(JsaError) as raised:
        assembled_with(replacements)
    message = str(raised.value)
    assert str(replacements / f"{name}.md") in message
    assert str(profile / "search" / f"{name}.md") not in message


@pytest.mark.parametrize("name", OPTIONAL)
def test_an_empty_replacement_of_an_optional_fragment_fills_its_slot_with_the_placeholder(
    profile, replacements, name
):
    marked(profile)
    (replacements / f"{name}.md").write_text("", encoding="utf-8")
    prompt = assembled_with(replacements)
    assert OPTIONAL_PLACEHOLDER in prompt
    assert f"UNIQUE-{name.upper()}-TEXT" not in prompt


@pytest.mark.parametrize("name", REQUIRED)
def test_a_live_required_fragment_that_is_missing_is_still_named_by_its_live_path(
    profile, replacements, name
):
    (profile / "search" / f"{name}.md").unlink()
    with pytest.raises(JsaError) as raised:
        assembled_with(replacements)
    assert str(profile / "search" / f"{name}.md") in str(raised.value)
