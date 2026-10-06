"""PRD 06 "Configuration surface", PRD 01 "Scheduling & cadence" and "Runner and verification
configuration" (XC-11, XC-14): the profile's two TOML files load into typed config."""

import re
import tomllib
import zipfile
from datetime import time
from xml.etree import ElementTree

import pytest
from profile_helpers import (
    CONFIG_TOML,
    EXAMPLE_DIR,
    FRAGMENTS,
    SEARCH_TOML,
    copy_example,
    write_config_toml,
    write_search_toml,
)

from jsa.errors import JsaError
from jsa.profile import load_config, load_search_config, profile_dir


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "my_profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def search_with(profile, replacements: dict[str, str]):
    text = SEARCH_TOML
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    write_search_toml(profile, text)


def config_with(profile, replacements: dict[str, str]):
    text = CONFIG_TOML
    for old, new in replacements.items():
        assert old in text, old
        text = text.replace(old, new)
    write_config_toml(profile, text)


# --- profile.example/ -------------------------------------------------------


def test_the_example_profile_has_both_toml_files_and_the_six_fragments():
    assert (EXAMPLE_DIR / "config.toml").is_file()
    assert (EXAMPLE_DIR / "search" / "search.toml").is_file()
    for name in FRAGMENTS:
        fragment = EXAMPLE_DIR / "search" / f"{name}.md"
        assert fragment.is_file(), name
        assert fragment.read_text(encoding="utf-8").strip(), name


def test_a_copy_of_the_example_profile_loads_without_error(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_PROFILE_DIR", str(copy_example(tmp_path / "profile")))
    config = load_config()
    search = load_search_config()
    assert config.candidate_name
    assert search.timezone


def test_the_example_profile_schedules_searches_with_positive_windows(monkeypatch):
    monkeypatch.setenv("JSA_PROFILE_DIR", str(EXAMPLE_DIR))
    search = load_search_config()
    assert search.schedule.monday
    assert all(item.window_hours > 0 for item in search.schedule.monday)


def test_the_example_search_toml_carries_the_prd_blocks_and_comments():
    text = (EXAMPLE_DIR / "search" / "search.toml").read_text(encoding="utf-8")
    for required in (
        "[runners.claude]",
        "[verification]",
        "Any Claude model that accepts an effort setting will run",
        "We recommend an Opus model at effort",
        "Perplexity and Gemini have no settings",
        "Which postings the pipeline admits. Required.",
        '"strict"',
        '"best_effort"',
        "`reachable`/`reachable_no_date`",
        'mode = "strict"',
    ):
        assert required in text, required


def test_the_example_search_toml_names_the_pinned_perplexity_preset_and_gemini_agent():
    text = (EXAMPLE_DIR / "search" / "search.toml").read_text(encoding="utf-8")
    assert '"xhigh"' in text
    assert '"deep-research-preview-04-2026"' in text
    for retired in ("flex", "30-step", "deep-research-max"):
        assert retired not in text, retired


def test_the_example_search_toml_has_no_gemini_runner_table():
    text = (EXAMPLE_DIR / "search" / "search.toml").read_text(encoding="utf-8")
    assert "gemini" not in tomllib.loads(text).get("runners", {})


def test_the_example_config_toml_names_the_recommended_checklist_and_refine_models():
    text = (EXAMPLE_DIR / "config.toml").read_text(encoding="utf-8")
    assert "claude-fable-5-1" in text
    assert "claude-opus-5-5" in text


WORD_NAMESPACE = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def docx_text(path) -> str:
    """Everything a reader of the .docx could see: body, headers, footers, and link targets."""
    lines = []
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            data = archive.read(name)
            if name.startswith("word/_rels/"):
                lines.append(data.decode("utf-8"))
            elif name.startswith("word/") and name.endswith(".xml"):
                for paragraph in ElementTree.fromstring(data).iter(
                    WORD_NAMESPACE + "p"
                ):
                    runs = paragraph.iter(WORD_NAMESPACE + "t")
                    lines.append("".join(t.text or "" for t in runs))
    return "\n".join(lines)


def test_the_example_profile_describes_no_real_person():
    email = re.compile(r"[\w.+-]+@(?!example\.(com|org|net)\b)[\w-]+\.[\w.-]+")
    phone = re.compile(r"\(?\b\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b")
    for path in EXAMPLE_DIR.rglob("*"):
        if path.is_file():
            if path.suffix == ".docx":
                text = docx_text(path)
            else:
                text = path.read_text(encoding="utf-8")
            assert not email.search(text), path
            assert not phone.search(text), path
            assert "linkedin.com/in/" not in text, path


# --- location ---------------------------------------------------------------


def test_jsa_profile_dir_changes_where_the_profile_is_read_from(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_config_toml(first, CONFIG_TOML.replace("Pat Example", "First Person"))
    write_config_toml(second, CONFIG_TOML.replace("Pat Example", "Second Person"))
    monkeypatch.setenv("JSA_PROFILE_DIR", str(first))
    assert load_config().candidate_name == "First Person"
    monkeypatch.setenv("JSA_PROFILE_DIR", str(second))
    assert load_config().candidate_name == "Second Person"


def test_jsa_profile_dir_also_moves_the_search_config(tmp_path, monkeypatch):
    write_search_toml(tmp_path / "a", SEARCH_TOML.replace("America/New_York", "UTC"))
    monkeypatch.setenv("JSA_PROFILE_DIR", str(tmp_path / "a"))
    assert load_search_config().timezone == "UTC"


def test_without_jsa_profile_dir_the_profile_is_read_from_profile_in_the_working_directory(
    tmp_path, monkeypatch
):
    write_config_toml(
        tmp_path / "profile", CONFIG_TOML.replace("Pat Example", "Local Person")
    )
    monkeypatch.delenv("JSA_PROFILE_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    assert load_config().candidate_name == "Local Person"
    assert profile_dir().resolve() == (tmp_path / "profile").resolve()


def test_profile_files_are_only_data_never_executed(profile):
    sentinel = profile.parent / "executed"
    for hook in ("__init__.py", "sitecustomize.py", "conftest.py"):
        (profile / hook).write_text(f"open({str(sentinel)!r}, 'w').close()\n")
    write_config_toml(profile)
    write_search_toml(profile)
    load_config()
    load_search_config()
    assert not sentinel.exists()


# --- a valid profile --------------------------------------------------------


def test_a_valid_search_profile_loads_typed_values(profile):
    write_search_toml(profile)
    search = load_search_config()
    assert search.timezone == "America/New_York"
    assert search.run_at == time(7, 0)
    assert search.verification.mode == "strict"
    assert search.runners.claude.model == "claude-opus-5-5"
    assert search.runners.claude.effort == "high"


def test_each_weekday_keeps_its_searches_in_order(profile):
    write_search_toml(profile)
    search = load_search_config()
    assert [(s.agent, s.window_hours) for s in search.schedule.tuesday] == [
        ("claude", 24),
        ("gemini", 48),
    ]
    assert [(s.agent, s.window_hours) for s in search.schedule.monday] == [
        ("perplexity", 72)
    ]


def test_omitted_weekdays_run_nothing(profile):
    write_search_toml(profile)
    search = load_search_config()
    for day in ("wednesday", "thursday", "friday", "saturday", "sunday"):
        assert not getattr(search.schedule, day), day


def test_an_empty_schedule_is_valid(profile):
    search_with(
        profile,
        {
            '[schedule]\nmonday = [{ agent = "perplexity", window_hours = 72 }]\n'
            'tuesday = [\n  { agent = "claude", window_hours = 24 },\n'
            '  { agent = "gemini", window_hours = 48 },\n]\n': "[schedule]\n"
        },
    )
    assert load_search_config().schedule.monday == []


def test_a_perplexity_only_schedule_needs_no_runner_entries(profile):
    write_search_toml(
        profile,
        'timezone = "UTC"\nrun_at = "06:30"\n'
        '[schedule]\nmonday = [{ agent = "perplexity", window_hours = 24 }]\n'
        '[verification]\nmode = "best_effort"\n',
    )
    search = load_search_config()
    assert search.verification.mode == "best_effort"
    assert search.run_at == time(6, 30)


def test_a_valid_config_loads_typed_values(profile):
    write_config_toml(profile)
    config = load_config()
    assert config.candidate_name == "Pat Example"
    assert config.tracker_spreadsheet_id == "sheet-id"
    assert config.fly.app == "jsa-example"
    assert config.fly.region == "iad"
    assert config.agents.checklist.model == "claude-fable-5-1"
    assert config.agents.checklist.effort == "medium"
    assert config.agents.refine.model == "claude-opus-5-5"
    assert config.agents.refine.effort == "high"


def test_candidate_name_is_optional(profile):
    config_with(profile, {'candidate_name = "Pat Example"\n': ""})
    assert load_config().candidate_name is None


def test_packets_dir_defaults_to_documents_job_applications(profile):
    write_config_toml(profile)
    packets_dir = load_config().packets_dir
    assert packets_dir.parts[-2:] == ("Documents", "Job Applications")


def test_the_default_packets_dir_is_expanded_under_the_home_directory(
    profile, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    write_config_toml(profile)
    packets_dir = load_config().packets_dir
    assert packets_dir == tmp_path / "Documents" / "Job Applications"
    assert "~" not in packets_dir.parts


def test_a_tilde_in_packets_dir_expands_to_the_home_directory(
    profile, monkeypatch, tmp_path
):
    monkeypatch.setenv("HOME", str(tmp_path))
    config_with(
        profile, {"candidate_name": 'packets_dir = "~/Packets"\ncandidate_name'}
    )
    assert load_config().packets_dir == tmp_path / "Packets"


def test_packets_dir_can_be_set(profile, tmp_path):
    config_with(
        profile, {"candidate_name": f'packets_dir = "{tmp_path}"\ncandidate_name'}
    )
    assert load_config().packets_dir == tmp_path


# --- unknown keys -----------------------------------------------------------


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param(
            {'run_at = "07:00"': 'run_at = "07:00"\nrun_att = "08:00"'}, id="top-level"
        ),
        pytest.param(
            {"[verification]": "[verification]\nmodee = 'strict'"}, id="verification"
        ),
        pytest.param(
            {'effort = "high"': 'effort = "high"\ntemperature = 1'}, id="runner"
        ),
        pytest.param(
            {
                "[runners.claude]": '[runners.gemini]\nagent = "deep-research-preview-04-2026"\n[runners.claude]'
            },
            id="gemini-runner-table",
        ),
        pytest.param(
            {"[runners.claude]": "[runners.perplexity]\nx = 1\n[runners.claude]"},
            id="runner-table",
        ),
        pytest.param(
            {
                "monday = [": "funday = [{ agent = 'claude', window_hours = 1 }]\nmonday = ["
            },
            id="weekday",
        ),
        pytest.param(
            {
                '{ agent = "perplexity", window_hours = 72 }': '{ agent = "perplexity", window_hours = 72, extra = 1 }'
            },
            id="scheduled-search",
        ),
    ],
)
def test_an_unknown_key_in_search_toml_raises_naming_the_file(profile, replacements):
    search_with(profile, replacements)
    with pytest.raises(JsaError, match=r"search\.toml"):
        load_search_config()


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param(
            {'candidate_name = "Pat Example"': 'candidate_nam = "Pat Example"'},
            id="top-level",
        ),
        pytest.param({'region = "iad"': 'region = "iad"\nzone = "a"'}, id="fly"),
        pytest.param(
            {'effort = "medium"': 'effort = "medium"\nextra = 1'}, id="checklist"
        ),
        pytest.param(
            {"[agents.refine]": "[agents.rewrite]\nmodel = 'x'\n[agents.refine]"},
            id="agents",
        ),
    ],
)
def test_an_unknown_key_in_config_toml_raises_naming_the_file(profile, replacements):
    config_with(profile, replacements)
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


# --- missing keys and files -------------------------------------------------


def test_a_missing_search_toml_raises_naming_the_file_and_pointing_to_the_example(
    profile,
):
    with pytest.raises(JsaError) as raised:
        load_search_config()
    assert "search.toml" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_a_missing_config_toml_raises_naming_the_file_and_pointing_to_the_example(
    profile,
):
    with pytest.raises(JsaError) as raised:
        load_config()
    assert "config.toml" in str(raised.value)
    assert "profile.example/" in str(raised.value)


@pytest.mark.parametrize(
    "removed",
    [
        pytest.param('timezone = "America/New_York"\n', id="timezone"),
        pytest.param('run_at = "07:00"\n', id="run_at"),
        pytest.param('[verification]\nmode = "strict"\n', id="verification-table"),
        pytest.param('mode = "strict"\n', id="mode"),
    ],
)
def test_a_key_search_needs_but_the_profile_lacks_raises_with_a_pointer(
    profile, removed
):
    search_with(profile, {removed: ""})
    with pytest.raises(JsaError) as raised:
        load_search_config()
    assert "search.toml" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_a_scheduled_claude_without_its_runner_entry_raises_with_a_pointer(profile):
    search_with(
        profile,
        {'[runners.claude]\nmodel = "claude-opus-5-5"\neffort = "high"\n': ""},
    )
    with pytest.raises(JsaError) as raised:
        load_search_config()
    assert "search.toml" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_a_scheduled_gemini_needs_no_runner_entry(profile):
    write_search_toml(profile)
    assert any(s.agent == "gemini" for s in load_search_config().schedule.tuesday)


def test_a_gemini_only_schedule_needs_no_runners_at_all(profile):
    write_search_toml(
        profile,
        'timezone = "America/New_York"\nrun_at = "07:00"\n\n'
        '[schedule]\nfriday = [{ agent = "gemini", window_hours = 24 }]\n\n'
        '[verification]\nmode = "strict"\n',
    )
    assert load_search_config().schedule.friday[0].agent == "gemini"


def test_invalid_toml_raises_an_app_error_naming_the_file(profile):
    write_search_toml(profile, "timezone = \n[[[")
    with pytest.raises(JsaError, match=r"search\.toml"):
        load_search_config()


# --- validation at load -----------------------------------------------------


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param({'agent = "perplexity"': 'agent = "bing"'}, id="unknown-agent"),
        pytest.param({'agent = "perplexity"': 'agent = "Perplexity"'}, id="agent-case"),
        pytest.param({"window_hours = 72": "window_hours = 0"}, id="zero-window"),
        pytest.param({"window_hours = 72": "window_hours = -24"}, id="negative-window"),
        pytest.param({'run_at = "07:00"': 'run_at = "7am"'}, id="run-at-words"),
        pytest.param({'run_at = "07:00"': 'run_at = "0700"'}, id="run-at-no-colon"),
        pytest.param({'run_at = "07:00"': 'run_at = "25:00"'}, id="run-at-hour"),
        pytest.param({'run_at = "07:00"': 'run_at = "07:60"'}, id="run-at-minute"),
        pytest.param({'run_at = "07:00"': 'run_at = ""'}, id="run-at-empty"),
        pytest.param({'model = "claude-opus-5-5"': 'model = ""'}, id="empty-model"),
        pytest.param({'effort = "high"': 'effort = "extreme"'}, id="bad-effort"),
        pytest.param({'effort = "high"': 'effort = ""'}, id="empty-effort"),
        pytest.param({'effort = "high"': 'effort = "HIGH"'}, id="effort-case"),
        pytest.param({'mode = "strict"': 'mode = "lenient"'}, id="bad-mode"),
        pytest.param({'mode = "strict"': 'mode = ""'}, id="empty-mode"),
        pytest.param({'mode = "strict"': 'mode = "best-effort"'}, id="mode-hyphen"),
        pytest.param(
            {'timezone = "America/New_York"': 'timezone = "Not/AZone"'},
            id="bad-timezone",
        ),
        pytest.param(
            {'timezone = "America/New_York"': 'timezone = ""'}, id="empty-timezone"
        ),
    ],
)
def test_an_invalid_search_value_raises_at_load(profile, replacements):
    search_with(profile, replacements)
    with pytest.raises(JsaError, match=r"search\.toml"):
        load_search_config()


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_every_effort_level_is_accepted(profile, effort):
    search_with(profile, {'effort = "high"': f'effort = "{effort}"'})
    assert load_search_config().runners.claude.effort == effort


@pytest.mark.parametrize("mode", ["strict", "best_effort"])
def test_both_verification_modes_are_accepted(profile, mode):
    search_with(profile, {'mode = "strict"': f'mode = "{mode}"'})
    assert load_search_config().verification.mode == mode


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-5-5",
        "some-future-model-9",
        "x",
        "gpt-whatever",
        "a model with spaces",
    ],
)
def test_any_non_empty_model_string_is_accepted(profile, model):
    search_with(profile, {'model = "claude-opus-5-5"': f'model = "{model}"'})
    assert load_search_config().runners.claude.model == model


@pytest.mark.parametrize("window_hours", [1, 24, 72, 720])
def test_any_positive_window_is_accepted(profile, window_hours):
    search_with(profile, {"window_hours = 72": f"window_hours = {window_hours}"})
    assert load_search_config().schedule.monday[0].window_hours == window_hours


@pytest.mark.parametrize("run_at", ["00:00", "07:00", "12:30", "23:59"])
def test_every_well_formed_run_at_is_accepted(profile, run_at):
    search_with(profile, {'run_at = "07:00"': f'run_at = "{run_at}"'})
    hours, minutes = map(int, run_at.split(":"))
    assert load_search_config().run_at == time(hours, minutes)


@pytest.mark.parametrize(
    "replacements",
    [
        pytest.param(
            {'effort = "medium"': 'effort = "extreme"'}, id="checklist-effort"
        ),
        pytest.param(
            {'model = "claude-fable-5-1"': 'model = ""'}, id="checklist-model"
        ),
        pytest.param({'effort = "high"': 'effort = ""'}, id="refine-effort"),
        pytest.param({'model = "claude-opus-5-5"': 'model = ""'}, id="refine-model"),
        pytest.param({'app = "jsa-example"': 'app = ""'}, id="fly-app"),
    ],
)
def test_an_invalid_config_value_raises_at_load(profile, replacements):
    config_with(profile, replacements)
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


@pytest.mark.parametrize("model", ["claude-fable-5-1", "anything-the-provider-accepts"])
def test_any_non_empty_agent_model_is_accepted_in_config(profile, model):
    config_with(profile, {'model = "claude-fable-5-1"': f'model = "{model}"'})
    assert load_config().agents.checklist.model == model
