"""PRD 04 "Resume redline" optional `wall_clock_seconds`, `XC-14`, `XC-12` (issues #121, #150).

The redline reads an optional hard stop from `[agents.redline]` in `profile/config.toml` and passes it to the
shared agent loop (the Claude seam, `XC-12`). Unset means unbounded. `[agents.checklist]` is no longer a key.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest
from profile_helpers import copy_example, write_config_toml, write_search_toml
from test_claude_runner import result_message

from jsa import agent_loop, redline
from jsa.agent_loop import AgentResult
from jsa.errors import JsaError
from jsa.profile import (
    load_config,
    load_search_config,
    redline_settings,
)
from jsa.runners import WallClockExceeded

REDLINE_JSON = json.dumps(
    {
        "edits": [],
        "removals": [],
        "reorders": [],
        "flags": [],
        "explanation": "Nothing to change.",
    }
)


def config_text(redline_extra="", refine_extra=""):
    return f"""\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-id"

[agents.redline]
model = "claude-fable-5-1"
effort = "medium"
{redline_extra}

[agents.refine]
model = "claude-opus-5-5"
effort = "high"
{refine_extra}
"""


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


@pytest.fixture
def loop_calls(monkeypatch):
    """Records what each caller hands the shared loop (`run_agent`), and answers with `answer`."""
    calls = []
    answer = SimpleNamespace(text="the answer")

    def run_agent(prompt, settings, **kwargs):
        calls.append(SimpleNamespace(prompt=prompt, settings=settings, **kwargs))
        return AgentResult(answer.text, 1, 0.0, None)

    monkeypatch.setattr(agent_loop, "run_agent", run_agent)
    return SimpleNamespace(calls=calls, answer=answer)


def hang(prompt_log):
    async def query(*, prompt, options=None, **_ignored):
        prompt_log.append(prompt)
        await asyncio.sleep(3600)
        yield result_message()

    return query


# --- config: where the key is read from -----------------------------------------------


@pytest.mark.parametrize("seconds", ["600", "0.5", "90.25", "1"])
def test_the_redline_reads_its_own_limit(profile, seconds):
    write_config_toml(profile, config_text(f"wall_clock_seconds = {seconds}"))
    config = load_config()
    assert config.agents.redline.wall_clock_seconds == float(seconds)
    assert redline_settings(config).wall_clock_seconds == float(seconds)


def test_an_unset_limit_leaves_the_agent_unbounded_with_no_default_number(profile):
    write_config_toml(profile, config_text())
    assert redline_settings(load_config()).wall_clock_seconds is None


def test_the_example_profile_sets_no_limit_by_default(profile, tmp_path, monkeypatch):
    example = copy_example(tmp_path / "example")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(example))
    config = load_config()
    assert config.agents.redline.wall_clock_seconds is None


# --- config: what is rejected ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", '"600"', "true", "[]"])
def test_a_limit_that_is_not_a_positive_number_raises_at_load(profile, bad):
    write_config_toml(profile, config_text(f"wall_clock_seconds = {bad}"))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_a_config_that_still_has_the_checklist_table_fails_on_the_unknown_key(profile):
    text = config_text() + '\n[agents.checklist]\nmodel = "m"\neffort = "low"\n'
    write_config_toml(profile, text)
    with pytest.raises(JsaError, match=r"checklist"):
        load_config()


def test_refine_has_no_time_limit_setting(profile):
    write_config_toml(profile, config_text(refine_extra="wall_clock_seconds = 60"))
    with pytest.raises(JsaError, match=r"config\.toml"):
        load_config()


def test_the_claude_search_runner_has_no_time_limit_setting(profile):
    write_search_toml(
        profile,
        """\
timezone = "America/New_York"
run_at = "07:00"

[schedule]
monday = [{ agent = "claude", window_hours = 24 }]

[runners.claude]
model = "claude-opus-5-5"
effort = "high"
wall_clock_seconds = 60

[verification]
mode = "strict"
""",
    )
    with pytest.raises(JsaError, match=r"search\.toml"):
        load_search_config()


# --- the caller passes the limit to the shared loop -----------------------------------


def test_the_redline_passes_its_limit_to_the_loop_as_wall_clock_seconds(
    profile, loop_calls
):
    write_config_toml(profile, config_text(redline_extra="wall_clock_seconds = 75.5"))
    loop_calls.answer.text = REDLINE_JSON
    redline.run_redline("p", redline_settings(load_config()))
    (call,) = loop_calls.calls
    assert call.wall_clock_seconds == 75.5


def test_an_unset_limit_hands_the_loop_no_limit(profile, loop_calls):
    write_config_toml(profile, config_text())
    loop_calls.answer.text = REDLINE_JSON
    redline.run_redline("p", redline_settings(load_config()))
    (call,) = loop_calls.calls
    assert call.wall_clock_seconds is None


def test_a_hung_run_past_the_configured_limit_raises_wall_clock_exceeded(
    profile, monkeypatch
):
    write_config_toml(profile, config_text("wall_clock_seconds = 0.05"))
    config = load_config()
    prompts = []
    monkeypatch.setattr(agent_loop, "query", hang(prompts))
    with pytest.raises(WallClockExceeded):
        redline.run_redline("p", redline_settings(config))
    assert prompts == ["p"]
