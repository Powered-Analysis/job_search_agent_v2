"""PRD 04 "Resume checklist" and "Resume redline" optional `wall_clock_seconds`, `XC-14`, `XC-12` (issue #121).

The checklist and redline read an optional hard stop from `[agents.checklist]` and `[agents.redline]` in
`profile/config.toml` and pass it to the shared agent loop (the Claude seam, `XC-12`). Unset means unbounded.
"""

import asyncio
from types import SimpleNamespace

import pytest
from profile_helpers import copy_example, write_config_toml, write_search_toml
from test_claude_runner import result_message

from jsa import agent_loop, checklist, redline
from jsa.agent_loop import AgentResult
from jsa.errors import JsaError
from jsa.profile import (
    checklist_settings,
    load_config,
    load_search_config,
    packet_agents,
    redline_settings,
)
from jsa.runners import WallClockExceeded

REDLINE_JSON = '{"edits": [], "skills_edits": [], "explanation": "Nothing to change."}'


def config_text(checklist_extra="", redline_extra="", refine_extra=""):
    return f"""\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-id"

[agents.checklist]
model = "claude-fable-5-1"
effort = "medium"
{checklist_extra}

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
def test_the_checklist_and_redline_each_read_their_own_limit(profile, seconds):
    write_config_toml(
        profile,
        config_text(
            f"wall_clock_seconds = {seconds}", f"wall_clock_seconds = {seconds}0"
        ),
    )
    config = load_config()
    assert config.agents.checklist.wall_clock_seconds == float(seconds)
    assert config.agents.redline.wall_clock_seconds == float(f"{seconds}0")


def test_the_two_agents_limits_are_independent(profile):
    write_config_toml(profile, config_text("wall_clock_seconds = 30"))
    agents = packet_agents(load_config())
    assert agents.checklist.wall_clock_seconds == 30
    assert agents.redline.wall_clock_seconds is None
    write_config_toml(profile, config_text(redline_extra="wall_clock_seconds = 45"))
    agents = packet_agents(load_config())
    assert agents.checklist.wall_clock_seconds is None
    assert agents.redline.wall_clock_seconds == 45


def test_an_unset_limit_leaves_the_agent_unbounded_with_no_default_number(profile):
    write_config_toml(profile, config_text())
    config = load_config()
    assert checklist_settings(config).wall_clock_seconds is None
    assert redline_settings(config).wall_clock_seconds is None


def test_the_example_profile_sets_no_limit_by_default(profile, tmp_path, monkeypatch):
    example = copy_example(tmp_path / "example")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(example))
    config = load_config()
    assert config.agents.checklist.wall_clock_seconds is None
    assert config.agents.redline.wall_clock_seconds is None


# --- config: what is rejected ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5", '"600"', "true", "[]"])
@pytest.mark.parametrize("agent", ["checklist", "redline"])
def test_a_limit_that_is_not_a_positive_number_raises_at_load(profile, agent, bad):
    extra = {agent + "_extra": f"wall_clock_seconds = {bad}"}
    write_config_toml(profile, config_text(**extra))
    with pytest.raises(JsaError, match=r"config\.toml"):
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


# --- the callers pass the limit to the shared loop ------------------------------------


def test_the_checklist_passes_its_limit_to_the_loop_as_wall_clock_seconds(
    profile, loop_calls
):
    write_config_toml(profile, config_text("wall_clock_seconds = 120"))
    text = checklist.run_checklist("p", checklist_settings(load_config()))
    assert text == "the answer"
    (call,) = loop_calls.calls
    assert call.wall_clock_seconds == 120


def test_the_redline_passes_its_limit_to_the_loop_as_wall_clock_seconds(
    profile, loop_calls
):
    write_config_toml(profile, config_text(redline_extra="wall_clock_seconds = 75.5"))
    loop_calls.answer.text = REDLINE_JSON
    redline.run_redline("p", redline_settings(load_config()))
    (call,) = loop_calls.calls
    assert call.wall_clock_seconds == 75.5


def test_each_agent_gets_its_own_limit_not_the_others(profile, loop_calls):
    write_config_toml(
        profile,
        config_text("wall_clock_seconds = 10", "wall_clock_seconds = 20"),
    )
    agents = packet_agents(load_config())
    checklist.run_checklist("p", agents.checklist)
    loop_calls.answer.text = REDLINE_JSON
    redline.run_redline("p", agents.redline)
    assert [call.wall_clock_seconds for call in loop_calls.calls] == [10, 20]


@pytest.mark.parametrize("who", ["checklist", "redline"])
def test_an_unset_limit_hands_the_loop_no_limit(profile, loop_calls, who):
    write_config_toml(profile, config_text())
    config = load_config()
    loop_calls.answer.text = REDLINE_JSON
    if who == "checklist":
        checklist.run_checklist("p", checklist_settings(config))
    else:
        redline.run_redline("p", redline_settings(config))
    (call,) = loop_calls.calls
    assert call.wall_clock_seconds is None


@pytest.mark.parametrize("who", ["checklist", "redline"])
def test_a_hung_run_past_the_configured_limit_raises_wall_clock_exceeded(
    profile, monkeypatch, who
):
    write_config_toml(
        profile,
        config_text("wall_clock_seconds = 0.05", "wall_clock_seconds = 0.05"),
    )
    config = load_config()
    prompts = []
    monkeypatch.setattr(agent_loop, "query", hang(prompts))
    with pytest.raises(WallClockExceeded):
        if who == "checklist":
            checklist.run_checklist("p", checklist_settings(config))
        else:
            redline.run_redline("p", redline_settings(config))
    assert prompts == ["p"]
