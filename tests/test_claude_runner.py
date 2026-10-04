"""`jsa search --agent claude` and the shared agent loop (issue #8; PRD 01 "Runners"; XC-1, XC-12, XC-14).

The Claude Agent SDK is replaced at its one entry point, `agent_loop.query`, so the loop itself runs.
"""

import ast
import logging
import os
import re
import sys
from types import SimpleNamespace

import httpx
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ResultError,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from conftest import REPO_ROOT, drop_all_tables
from profile_helpers import SEARCH_TOML, copy_example, write_search_toml
from search_helpers import Boards, rows

from jsa import agent_loop, cli, db
from jsa.agent_loop import AgentError
from jsa.claude import ClaudeRunner
from jsa.profile import AgentSettings
from jsa.runners import RunnerResult

SETTINGS = AgentSettings(model="claude-sonnet-5-5", effort="low")
SEARCH_KEY = "sk-ant-search-key"
OAUTH_TOKEN = "oauth-token-from-the-environment"


def result_message(**fields):
    defaults = {
        "subtype": "success",
        "duration_ms": 1000,
        "duration_api_ms": 900,
        "is_error": False,
        "num_turns": 3,
        "session_id": "session",
        "total_cost_usd": 0.25,
        "result": '{"postings": []}',
    }
    return ResultMessage(**{**defaults, **fields})


def assistant(*blocks):
    return AssistantMessage(content=list(blocks), model="claude-sonnet-5-5")


class Sdk:
    """Stands in for the SDK's `query`: replays `messages`, then raises `raises` if set."""

    def __init__(self):
        self.messages = [result_message()]
        self.raises = None
        self.calls = []

    async def query(self, *, prompt, options=None, **_ignored):
        self.calls.append(SimpleNamespace(prompt=prompt, options=options))
        for message in self.messages:
            yield message
        if self.raises is not None:
            raise self.raises

    @property
    def options(self):
        (call,) = self.calls
        return call.options

    def answers(self, text):
        self.messages = [result_message(result=text)]


@pytest.fixture
def sdk(monkeypatch):
    stand_in = Sdk()
    monkeypatch.setattr(agent_loop, "query", stand_in.query)
    for name in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("JSA_SEARCH_ANTHROPIC_API_KEY", raising=False)
    return stand_in


def effective_env(options):
    """What the spawned CLI sees: the SDK layers `options.env` over the inherited environment."""
    return {**os.environ, **(options.env or {})}


# --- the runner: what it hands to the shared loop -----------------------------------


def test_the_prompt_reaches_the_sdk_and_the_runner_returns_its_result_string(sdk):
    sdk.answers('{"postings": [1]}')
    result = ClaudeRunner(SETTINGS).run("find me roles")
    assert sdk.calls[0].prompt == "find me roles"
    assert isinstance(result, RunnerResult)
    assert result.text == '{"postings": [1]}'


def test_web_search_and_web_fetch_are_the_only_available_tools(sdk):
    ClaudeRunner(SETTINGS).run("p")
    assert isinstance(sdk.options.tools, list)
    assert sorted(sdk.options.tools) == ["WebFetch", "WebSearch"]
    assert set(sdk.options.allowed_tools or []) <= {"WebSearch", "WebFetch"}


def test_the_run_is_capped_at_120_turns_and_skips_permission_prompts(sdk):
    ClaudeRunner(SETTINGS).run("p")
    assert sdk.options.max_turns is not None
    assert 0 < sdk.options.max_turns <= 120
    assert sdk.options.permission_mode == "bypassPermissions"


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_the_configured_model_and_effort_are_passed_explicitly(sdk, effort):
    ClaudeRunner(AgentSettings(model="claude-opus-5-5", effort=effort)).run("p")
    assert sdk.options.model == "claude-opus-5-5"
    assert sdk.options.effort == effort


def test_the_runner_reports_the_model_effort_and_cost_it_ran_with(sdk):
    sdk.messages = [result_message(total_cost_usd=1.5)]
    result = ClaudeRunner(SETTINGS).run("p")
    assert (result.model, result.effort, result.cost) == (
        "claude-sonnet-5-5",
        "low",
        1.5,
    )


def test_an_unknown_cost_is_reported_as_none(sdk):
    sdk.messages = [result_message(total_cost_usd=None)]
    assert ClaudeRunner(SETTINGS).run("p").cost is None


# --- auth (XC-1) -----------------------------------------------------------------------


def test_with_the_search_key_set_it_is_the_clis_only_credential(sdk, monkeypatch):
    monkeypatch.setenv("JSA_SEARCH_ANTHROPIC_API_KEY", SEARCH_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN)
    ClaudeRunner(SETTINGS).run("p")
    env = effective_env(sdk.options)
    assert env["ANTHROPIC_API_KEY"] == SEARCH_KEY
    assert not env.get("CLAUDE_CODE_OAUTH_TOKEN")


def test_the_search_key_replaces_an_inherited_api_key(sdk, monkeypatch):
    monkeypatch.setenv("JSA_SEARCH_ANTHROPIC_API_KEY", SEARCH_KEY)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "some-other-key")
    ClaudeRunner(SETTINGS).run("p")
    assert effective_env(sdk.options)["ANTHROPIC_API_KEY"] == SEARCH_KEY


def test_the_oauth_token_is_cleared_for_the_run_not_just_omitted(sdk, monkeypatch):
    monkeypatch.setenv("JSA_SEARCH_ANTHROPIC_API_KEY", SEARCH_KEY)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN)
    ClaudeRunner(SETTINGS).run("p")
    assert "CLAUDE_CODE_OAUTH_TOKEN" in (sdk.options.env or {})
    assert OAUTH_TOKEN not in effective_env(sdk.options).values()


def test_with_the_search_key_unset_the_runner_passes_no_credential_override(
    sdk, monkeypatch
):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN)
    ClaudeRunner(SETTINGS).run("p")
    env = sdk.options.env or {}
    assert "ANTHROPIC_API_KEY" not in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert effective_env(sdk.options)["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH_TOKEN


def test_an_inherited_api_key_is_left_alone_when_the_search_key_is_unset(
    sdk, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "developers-own-key")
    ClaudeRunner(SETTINGS).run("p")
    assert effective_env(sdk.options)["ANTHROPIC_API_KEY"] == "developers-own-key"


# --- the shared loop: final text and errors -------------------------------------------


def test_the_loop_returns_the_result_string_over_the_assistant_text(sdk):
    sdk.messages = [
        assistant(TextBlock("narration that is not the answer")),
        result_message(result="the final answer"),
    ]
    assert ClaudeRunner(SETTINGS).run("p").text == "the final answer"


def test_without_a_result_string_the_assistant_text_is_joined(sdk):
    sdk.messages = [
        assistant(TextBlock("first-part"), ToolUseBlock("t1", "WebSearch", {})),
        assistant(ThinkingBlock("private thoughts", "sig"), TextBlock("second-part")),
        result_message(result=None),
    ]
    text = ClaudeRunner(SETTINGS).run("p").text
    assert "first-part" in text
    assert "second-part" in text
    assert text.index("first-part") < text.index("second-part")
    assert "private thoughts" not in text


def test_an_error_result_raises_with_the_real_http_status(sdk):
    sdk.messages = [
        result_message(
            subtype="success",
            is_error=True,
            api_error_status=429,
            result="API Error: rate limited",
        )
    ]
    with pytest.raises(AgentError, match="429"):
        ClaudeRunner(SETTINGS).run("p")


def test_a_max_turns_error_result_raises(sdk):
    sdk.messages = [
        assistant(TextBlock("half an answer")),
        result_message(subtype="error_max_turns", is_error=True, result=None),
    ]
    with pytest.raises(AgentError):
        ClaudeRunner(SETTINGS).run("p")


def test_the_sdks_own_result_error_raises_from_the_loop_with_its_status(sdk):
    sdk.raises = ResultError(
        "CLI exited",
        data={
            "subtype": "success",
            "api_error_status": 529,
            "result": "API Error: overloaded",
        },
        exit_code=1,
    )
    sdk.messages = []
    with pytest.raises(AgentError, match="529"):
        ClaudeRunner(SETTINGS).run("p")


def test_run_agent_raises_agent_error_directly_on_an_error_result(sdk):
    sdk.messages = [result_message(is_error=True, subtype="error_during_execution")]
    with pytest.raises(AgentError):
        agent_loop.run_agent(
            "p",
            SETTINGS,
            tools=["WebSearch"],
            max_turns=5,
            permission_mode="bypassPermissions",
        )


def test_run_agent_passes_each_callers_own_tools_limits_and_mode(sdk):
    agent_loop.run_agent(
        "p",
        AgentSettings(model="claude-fable-5-1", effort="medium"),
        tools=["Read", "Edit"],
        max_turns=80,
        permission_mode="acceptEdits",
    )
    assert sorted(sdk.options.tools) == ["Edit", "Read"]
    assert sdk.options.max_turns == 80
    assert sdk.options.permission_mode == "acceptEdits"
    assert (sdk.options.model, sdk.options.effort) == ("claude-fable-5-1", "medium")


def test_run_agent_loads_no_settings_files_whatever_the_sdk_default_is(sdk):
    agent_loop.run_agent(
        "p",
        SETTINGS,
        tools=["Read"],
        max_turns=1,
        permission_mode="acceptEdits",
    )
    assert sdk.options.setting_sources == []


def test_the_search_runner_loads_no_settings_files(sdk):
    ClaudeRunner(SETTINGS).run("p")
    assert sdk.options.setting_sources == []


def test_run_agent_lets_a_caller_observe_every_message(sdk):
    sdk.messages = [assistant(TextBlock("hello")), result_message()]
    seen = []
    agent_loop.run_agent(
        "p",
        SETTINGS,
        tools=[],
        max_turns=1,
        permission_mode="bypassPermissions",
        on_message=seen.append,
    )
    assert seen == sdk.messages


def test_run_agent_returns_text_turns_and_cost(sdk):
    sdk.messages = [result_message(result="done", num_turns=9, total_cost_usd=0.5)]
    result = agent_loop.run_agent(
        "p", SETTINGS, tools=[], max_turns=1, permission_mode="bypassPermissions"
    )
    assert (result.text, result.turns, result.cost) == ("done", 9, 0.5)
    assert result.seconds >= 0


# --- the live trace ----------------------------------------------------------------------


def test_the_trace_shows_tool_calls_narration_thinking_and_tool_errors(sdk, caplog):
    sdk.messages = [
        assistant(
            ThinkingBlock("weighing-the-careers-page", "sig"),
            TextBlock("narrating-a-search"),
            ToolUseBlock("t1", "WebFetch", {"url": "https://acme.example/careers"}),
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    "t1", content="blocked-by-robots-sentinel", is_error=True
                )
            ]
        ),
        result_message(),
    ]
    with caplog.at_level(logging.DEBUG):
        ClaudeRunner(SETTINGS).run("p")
    for expected in (
        "weighing-the-careers-page",
        "narrating-a-search",
        "WebFetch",
        "https://acme.example/careers",
        "blocked-by-robots-sentinel",
    ):
        assert expected in caplog.text


def test_the_closing_line_reports_turns_seconds_and_usd(sdk, caplog):
    sdk.messages = [result_message(num_turns=37, total_cost_usd=0.4)]
    with caplog.at_level(logging.DEBUG):
        ClaudeRunner(SETTINGS).run("p")
    (line,) = [
        record.getMessage() for record in caplog.records if "37" in record.getMessage()
    ]
    assert "$0.4" in line
    assert re.search(r"\d\s?s\b|second", line)


# --- only the shared loop calls the SDK (XC-12) ----------------------------------------


def sdk_entry_point_uses(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    entry_points = {"query", "ClaudeSDKClient"}
    uses = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "claude_agent_sdk"
        ):
            uses += [a.name for a in node.names if a.name in entry_points]
        elif (
            isinstance(node, ast.Attribute)
            and node.attr in entry_points
            and isinstance(node.value, ast.Name)
            and node.value.id == "claude_agent_sdk"
        ):
            uses.append(node.attr)
    return uses


def test_only_the_shared_loop_imports_the_sdks_query_entry_points():
    sources = sorted((REPO_ROOT / "src" / "jsa").rglob("*.py"))
    users = {p.name for p in sources if sdk_entry_point_uses(p)}
    assert users == {"agent_loop.py"}


# --- jsa search --agent claude ------------------------------------------------------------


@pytest.fixture
def world(db_url, sdk, tmp_path, monkeypatch):
    drop_all_tables(db_url)
    db.connect().close()
    boards = Boards()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda _self, r: boards.handle(r)
    )
    profile = copy_example(tmp_path / "profile")
    write_search_toml(
        profile,
        SEARCH_TOML.replace(
            'model = "claude-opus-5-5"\neffort = "high"',
            'model = "claude-sonnet-5-5"\neffort = "low"',
        ),
    )
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    # Claude searches need no Perplexity key, and no stray `.env` may supply anything.
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(url=db_url, boards=boards, sdk=sdk, profile=profile)


def jsa_search_claude(monkeypatch, capsys, window="24"):
    monkeypatch.setattr(
        sys, "argv", ["jsa", "search", "--agent", "claude", "--window-hours", window]
    )
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_search_offers_claude_as_an_agent(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jsa", "search", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "claude" in capsys.readouterr().out


def test_a_claude_search_records_findings_with_the_configured_model_and_effort(
    world, monkeypatch, capsys
):
    live = world.boards.job()
    world.sdk.answers(
        '{"postings": [{"company": "Acme", "title": "Staff Engineer", '
        f'"url": "{live}"}}]}}'
    )
    code, out, _err = jsa_search_claude(monkeypatch, capsys, window="48")
    assert code == 0
    assert world.sdk.options.model == "claude-sonnet-5-5"
    assert world.sdk.options.effort == "low"
    assert rows(
        world.url, "SELECT agent, window_hours, model, effort FROM search_findings"
    ) == [("claude", 48, "claude-sonnet-5-5", "low")]
    assert rows(world.url, "SELECT search_agent FROM postings") == [("claude",)]
    assert rows(
        world.url, "SELECT agent, window_hours, model, effort, outcome FROM search_runs"
    ) == [("claude", 48, "claude-sonnet-5-5", "low", "ok")]
    assert "agent: claude" in out


def test_a_claude_search_with_no_postings_closes_ok_and_inserts_nothing(
    world, monkeypatch, capsys
):
    code, _out, _err = jsa_search_claude(monkeypatch, capsys)
    assert code == 0
    assert rows(world.url, "SELECT outcome FROM search_runs") == [("ok",)]
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(0,)]


@pytest.mark.parametrize(
    "failure",
    ["error result", "sdk error"],
)
def test_an_errored_claude_run_closes_the_run_failed_and_inserts_nothing(
    world, monkeypatch, capsys, failure
):
    live = world.boards.job()
    answer = (
        '{"postings": [{"company": "Acme", "title": "Staff Engineer", '
        f'"url": "{live}"}}]}}'
    )
    if failure == "error result":
        world.sdk.messages = [
            assistant(TextBlock(answer)),
            result_message(subtype="success", is_error=True, api_error_status=529),
        ]
    else:
        world.sdk.messages = [assistant(TextBlock(answer))]
        world.sdk.raises = ResultError(
            "CLI exited",
            data={"subtype": "error_during_execution", "api_error_status": 500},
            exit_code=1,
        )
    code, _out, _err = jsa_search_claude(monkeypatch, capsys)
    assert code != 0
    assert [r[0] for r in rows(world.url, "SELECT outcome FROM search_runs")] == [
        "failed"
    ]
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(0,)]
    assert rows(world.url, "SELECT COUNT(*) FROM search_findings") == [(0,)]


def test_an_unparseable_claude_answer_fails_the_run_and_inserts_nothing(
    world, monkeypatch, capsys
):
    world.sdk.answers("I could not find anything, sorry.")
    code, _out, _err = jsa_search_claude(monkeypatch, capsys)
    assert code != 0
    assert [r[0] for r in rows(world.url, "SELECT outcome FROM search_runs")] == [
        "failed"
    ]
    assert rows(world.url, "SELECT COUNT(*) FROM search_findings") == [(0,)]


def test_a_profile_without_the_claude_table_fails_before_any_model_call_or_run_row(
    world, monkeypatch, capsys
):
    toml = world.profile / "search" / "search.toml"
    text = toml.read_text()
    toml.write_text(re.sub(r"\[runners\.claude\]\n(?:[^\[\n]*\n)*", "", text, count=1))
    code, _out, err = jsa_search_claude(monkeypatch, capsys)
    assert code != 0
    assert "search.toml" in err
    assert world.sdk.calls == []
    assert rows(world.url, "SELECT COUNT(*) FROM search_runs") == [(0,)]
