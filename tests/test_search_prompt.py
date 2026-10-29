"""PRD 01 "Search prompt contract" (XC-13): the one prompt every runner sends."""

import re
import shutil
from datetime import UTC, datetime, timedelta, timezone

import pytest
from profile_helpers import (
    FAMILY,
    FRAGMENTS,
    OTHER_FAMILY,
    SEARCH_TOML,
    SHARED,
    copy_example,
    example_fragment,
    fragment_file,
    remove_fragment,
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


def assembled(hours=24, now=NOW, family=FAMILY):
    return assemble_search_prompt(load_search_config(), family, hours, now)


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
        assert example_fragment(name).strip() in prompt, name


def test_each_example_families_prompt_holds_its_own_fragments(profile):
    for family in (FAMILY, OTHER_FAMILY):
        prompt = assembled(family=family)
        for name in FRAGMENTS:
            assert example_fragment(name, family).strip() in prompt, (family, name)
    assert assembled(family=FAMILY) != assembled(family=OTHER_FAMILY)


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
    remove_fragment(profile, name)
    with pytest.raises(JsaError) as raised:
        assembled()
    message = str(raised.value)
    assert str(fragment_file(profile, name)) in message
    assert str(fragment_file(profile, name, SHARED)) in message
    assert "profile.example/search" in message


@pytest.mark.parametrize("name", REQUIRED)
@pytest.mark.parametrize("content", ["", "  \n\n \t"], ids=["empty", "blank"])
def test_an_empty_required_fragment_raises_naming_the_file_and_the_example(
    profile, name, content
):
    write_fragment(profile, name, content)
    write_fragment(profile, name, content, SHARED)
    with pytest.raises(JsaError) as raised:
        assembled()
    message = str(raised.value)
    assert f"{name}.md" in message
    assert "profile.example/search" in message


@pytest.mark.parametrize("name", OPTIONAL)
def test_a_missing_optional_fragment_fills_its_slot_with_the_placeholder(profile, name):
    marked(profile)
    remove_fragment(profile, name)
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
        remove_fragment(profile, name)
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


# --- the result target (PRD 01 "Result target is user config") --------------


def set_results_per_run(profile, value):
    path = profile / "search" / "search.toml"
    text = path.read_text(encoding="utf-8")
    text = re.sub(
        r"^results_per_run\s*=.*$",
        f"results_per_run = {value}",
        text,
        flags=re.MULTILINE,
    )
    path.write_text(text, encoding="utf-8")


def test_the_example_profile_prompt_states_the_target_of_ten(profile):
    assert "Return no more than 10 postings that best fit the candidate" in assembled()


@pytest.mark.parametrize("value", [1, 4, 25])
def test_the_prompt_states_the_users_result_target(profile, value):
    set_results_per_run(profile, value)
    prompt = assembled()
    assert f"Return no more than {value} postings that best fit the candidate" in prompt
    assert "{{" not in prompt


def test_changing_the_result_target_changes_nothing_else_in_the_prompt(profile):
    ten = assembled()
    set_results_per_run(profile, 4)
    four = assembled()
    assert ten != four
    # The template restates the target; only the number differs between the two.
    assert re.sub(r"\b10 postings", "4 postings", ten) == four


def test_the_result_target_reaches_the_prompt_in_every_verification_mode(profile):
    set_results_per_run(profile, 7)
    for mode in ("strict", "best_effort"):
        set_mode(profile, mode)
        assert (
            "Return no more than 7 postings that best fit the candidate" in assembled()
        )


def test_the_search_template_hard_codes_no_result_target():
    template = app_template("search.md")
    assert "Return no more than {{RESULTS_PER_RUN}} postings that best fit" in template
    assert "no more than 10 postings" not in template


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
        "RESULTS_PER_RUN",
        "SKILLS",
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
        "RESULTS_PER_RUN",
        "SKILLS",
    }


def test_the_search_template_holds_no_candidate_specific_content():
    template = app_template("search.md")
    for name in FRAGMENTS:
        for family in (FAMILY, OTHER_FAMILY):
            text = example_fragment(name, family)
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
        load_search_config(), FAMILY, "a test window", replacements
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
    live = fragment_file(profile, "filters")
    before = live.read_bytes()
    (replacements / "filters.md").write_text("REPLACED-FILTERS-TEXT", encoding="utf-8")
    assembled_with(replacements)
    assert live.read_bytes() == before


def test_an_empty_replacements_directory_assembles_the_same_prompt_as_none(
    profile, replacements
):
    window = "a test window"
    assert assembled_with(replacements) == assemble_search_prompt_for(
        load_search_config(), FAMILY, window
    )


def test_a_replacement_supplies_a_required_fragment_the_live_profile_lacks(
    profile, replacements
):
    remove_fragment(profile, "filters")
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
    assert str(fragment_file(profile, name)) not in message


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
    remove_fragment(profile, name)
    with pytest.raises(JsaError) as raised:
        assembled_with(replacements)
    assert str(fragment_file(profile, name)) in str(raised.value)


# --- the image's layout (issue #198; PRD 06 Fly image) -------------------------


def skills_section(prompt):
    """The text under the prompt's Skills heading, up to the next heading."""
    return prompt.split("\n## Skills\n", 1)[1].split("\n## ", 1)[0]


@pytest.fixture
def image_profile(profile):
    """The profile as the image carries it: only `search/` and `resume_skills.md`."""
    for entry in profile.iterdir():
        if entry.name not in ("search", "resume_skills.md"):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    return profile


def test_a_profile_holding_only_search_and_the_redlines_list_puts_the_searchs_skills_under_skills(
    image_profile,
):
    write_fragment(
        image_profile, "skills", "UNIQUE-SKILL-ALPHA\nUNIQUE-SKILL-BETA\n", SHARED
    )
    (image_profile / "resume_skills.md").write_text(
        "UNIQUE-REDLINE-SKILL\n", encoding="utf-8"
    )
    assert {p.name for p in image_profile.iterdir()} == {"search", "resume_skills.md"}
    prompt = assembled()
    section = skills_section(prompt)
    assert "UNIQUE-SKILL-ALPHA\nUNIQUE-SKILL-BETA" in section
    assert OPTIONAL_PLACEHOLDER not in section
    assert "UNIQUE-REDLINE-SKILL" not in prompt
    assert "{{" not in prompt


def test_the_image_layout_prompt_is_the_one_a_local_run_sends(profile):
    local = assembled()
    for entry in profile.iterdir():
        if entry.name not in ("search", "resume_skills.md"):
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
    assert assembled() == local


def test_without_a_skills_fragment_the_skills_section_gets_the_placeholder_note(
    image_profile,
):
    remove_fragment(image_profile, "skills")
    prompt = assembled()
    assert OPTIONAL_PLACEHOLDER in skills_section(prompt)
    assert "{{" not in prompt


def test_the_redlines_list_alone_does_not_fill_the_skills_section(image_profile):
    remove_fragment(image_profile, "skills")
    (image_profile / "resume_skills.md").write_text(
        "UNIQUE-REDLINE-SKILL\n", encoding="utf-8"
    )
    prompt = assembled()
    assert OPTIONAL_PLACEHOLDER in skills_section(prompt)
    assert "UNIQUE-REDLINE-SKILL" not in prompt


# --- search families (issue #199; PRD 01 "Search families") -------------------


def test_a_familys_own_fragment_beats_the_shared_one(profile):
    write_fragment(profile, "target_roles", "SHARED-ROLES-TEXT", SHARED)
    write_fragment(profile, "target_roles", "OWN-ROLES-TEXT")
    prompt = assembled()
    assert "OWN-ROLES-TEXT" in prompt
    assert "SHARED-ROLES-TEXT" not in prompt


def test_deleting_the_familys_own_fragment_falls_back_to_the_shared_one(profile):
    write_fragment(profile, "target_roles", "SHARED-ROLES-TEXT", SHARED)
    write_fragment(profile, "target_roles", "OWN-ROLES-TEXT")
    fragment_file(profile, "target_roles").unlink()
    prompt = assembled()
    assert "SHARED-ROLES-TEXT" in prompt
    assert "OWN-ROLES-TEXT" not in prompt


def test_a_family_with_no_own_file_uses_the_shared_one_and_another_family_keeps_its_own(
    profile,
):
    write_fragment(profile, "filters", "SHARED-FILTERS-TEXT", SHARED)
    fragment_file(profile, "filters").unlink(missing_ok=True)
    write_fragment(profile, "filters", "CHARTER-FILTERS-TEXT", OTHER_FAMILY)
    assert "SHARED-FILTERS-TEXT" in assembled(family=FAMILY)
    prompt = assembled(family=OTHER_FAMILY)
    assert "CHARTER-FILTERS-TEXT" in prompt
    assert "SHARED-FILTERS-TEXT" not in prompt


@pytest.mark.parametrize("name", OPTIONAL)
def test_an_empty_own_optional_fragment_turns_it_off_even_when_shared_has_text(
    profile, name
):
    write_fragment(profile, name, f"SHARED-{name.upper()}-TEXT", SHARED)
    write_fragment(profile, name, "")
    prompt = assembled()
    assert f"SHARED-{name.upper()}-TEXT" not in prompt
    assert OPTIONAL_PLACEHOLDER in prompt


@pytest.mark.parametrize("name", OPTIONAL)
def test_the_shared_optional_fragment_is_used_when_the_family_has_none(profile, name):
    write_fragment(profile, name, f"SHARED-{name.upper()}-TEXT", SHARED)
    fragment_file(profile, name).unlink(missing_ok=True)
    assert f"SHARED-{name.upper()}-TEXT" in assembled()


@pytest.mark.parametrize("name", REQUIRED)
def test_an_empty_own_required_fragment_raises_even_when_shared_has_text(profile, name):
    write_fragment(profile, name, f"SHARED-{name.upper()}-TEXT", SHARED)
    write_fragment(profile, name, "")
    with pytest.raises(JsaError) as raised:
        assembled()
    assert str(fragment_file(profile, name)) in str(raised.value)


@pytest.mark.parametrize("name", REQUIRED)
def test_a_required_fragment_missing_from_both_places_names_both_paths(profile, name):
    remove_fragment(profile, name)
    with pytest.raises(JsaError) as raised:
        assembled()
    message = str(raised.value)
    assert str(fragment_file(profile, name)) in message
    assert str(fragment_file(profile, name, SHARED)) in message


@pytest.mark.parametrize("name", REQUIRED)
def test_a_required_fragment_empty_in_both_places_raises(profile, name):
    write_fragment(profile, name, "", SHARED)
    write_fragment(profile, name, "")
    with pytest.raises(JsaError):
        assembled()


def test_a_profile_with_no_shared_folder_assembles_from_the_familys_own_fragments(
    profile,
):
    marked(profile)
    shutil.rmtree(profile / "search" / SHARED)
    prompt = assembled()
    for name in FRAGMENTS:
        assert f"UNIQUE-{name.upper()}-TEXT about something specific." in prompt, name


def test_a_shared_skills_fragment_fills_the_skills_section_of_every_family(profile):
    for family in (FAMILY, OTHER_FAMILY):
        remove_fragment(profile, "skills", family)
    write_fragment(profile, "skills", "UNIQUE-SKILL-GAMMA\n", SHARED)
    for family in (FAMILY, OTHER_FAMILY):
        assert "UNIQUE-SKILL-GAMMA" in skills_section(assembled(family=family))


def test_a_familys_own_skills_beat_the_shared_ones_and_an_empty_own_file_turns_them_off(
    profile,
):
    write_fragment(profile, "skills", "SHARED-SKILLS-TEXT", SHARED)
    write_fragment(profile, "skills", "OWN-SKILLS-TEXT")
    section = skills_section(assembled())
    assert "OWN-SKILLS-TEXT" in section
    assert "SHARED-SKILLS-TEXT" not in section
    write_fragment(profile, "skills", "")
    section = skills_section(assembled())
    assert "SHARED-SKILLS-TEXT" not in section
    assert OPTIONAL_PLACEHOLDER in section


def test_changing_or_removing_the_redlines_skills_list_changes_no_search_prompt(
    profile,
):
    before = {f: assembled(family=f) for f in (FAMILY, OTHER_FAMILY)}
    skills_file = profile / "resume_skills.md"
    skills_file.write_text("UNIQUE-REDLINE-SKILL\n", encoding="utf-8")
    changed = {f: assembled(family=f) for f in (FAMILY, OTHER_FAMILY)}
    skills_file.unlink()
    removed = {f: assembled(family=f) for f in (FAMILY, OTHER_FAMILY)}
    assert changed == before == removed


def test_a_family_with_no_folder_raises_before_assembling(profile):
    with pytest.raises(JsaError) as raised:
        assembled(family="no-such-family")
    assert "no-such-family" in str(raised.value)


def test_shared_is_not_a_search_family(profile):
    with pytest.raises(JsaError):
        assembled(family=SHARED)


# --- employer URLs carrying ashby_jid / gh_jid (issue #108) ------------------


@pytest.mark.parametrize("param", ["ashby_jid", "gh_jid"])
def test_the_prompt_names_the_job_id_parameters_of_employer_urls(profile, param):
    assert param in assembled()


@pytest.mark.parametrize("param", ["ashby_jid", "gh_jid"])
def test_the_prompt_says_to_find_the_board_and_emit_the_ats_url(profile, param):
    prompt = assembled()
    start = prompt.index(param)
    nearby = prompt[max(0, start - 400) : start + 800].lower()
    assert "employer" in nearby
    assert "board" in nearby
    assert "never emit" in nearby or "do not emit" in nearby or "not emit" in nearby


def test_the_prompt_still_requires_the_job_boards_greenhouse_form(profile):
    prompt = assembled()
    assert "https://job-boards.greenhouse.io/{board token}/jobs/{job id}" in prompt
    assert "jobUrl" in prompt
