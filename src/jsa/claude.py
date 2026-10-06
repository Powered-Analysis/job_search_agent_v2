"""The Claude search runner (PRD 01): the shared agent loop with web search and fetch as its only tools."""

import json
import logging

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from jsa import agent_loop
from jsa.config import search_anthropic_api_key
from jsa.profile import AgentSettings
from jsa.runners import RunnerResult, clip

log = logging.getLogger(__name__)

# The agent reads untrusted pages with the app's keys in its environment, so it gets nothing else.
TOOLS = ["WebSearch", "WebFetch"]
MAX_TURNS = 120


def credential_env() -> dict[str, str]:
    """XC-1: the search key as the CLI's only credential; the OAuth token is blanked, not just omitted."""
    key = search_anthropic_api_key()
    if key is None:
        return {}
    return {"ANTHROPIC_API_KEY": key, "CLAUDE_CODE_OAUTH_TOKEN": ""}


def _trace(message: Message) -> None:
    blocks = []
    if isinstance(message, AssistantMessage | UserMessage) and isinstance(
        message.content, list
    ):
        blocks = message.content
    for block in blocks:
        match block:
            case ToolUseBlock():
                log.info("tool %s %s", block.name, clip(json.dumps(block.input)))
            case TextBlock():
                log.info("claude: %s", clip(block.text))
            case ThinkingBlock() if block.thinking:
                log.info("thinking: %s", clip(block.thinking))
            case ToolResultBlock(is_error=True):
                log.warning("tool error: %s", clip(str(block.content)))


class ClaudeRunner:
    def __init__(self, settings: AgentSettings) -> None:
        self._settings = settings

    def run(self, prompt: str) -> RunnerResult:
        result = agent_loop.run_agent(
            prompt,
            self._settings,
            tools=TOOLS,
            max_turns=MAX_TURNS,
            permission_mode="bypassPermissions",
            env=credential_env(),
            on_message=_trace,
        )
        log.info(
            "claude done: %d turns, %.0fs, cost %s",
            result.turns,
            result.seconds,
            "unknown" if result.cost is None else f"${result.cost:.2f}",
        )
        return RunnerResult(
            result.text, self._settings.model, self._settings.effort, result.cost
        )
