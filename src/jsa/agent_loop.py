"""The shared agent loop (XC-12): the app's one call into the Claude Agent SDK."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    Message,
    PermissionMode,
    ResultError,
    ResultMessage,
    TextBlock,
    query,
)

from jsa.errors import JsaError
from jsa.profile import AgentSettings
from jsa.runners import WALL_CLOCK_CEILING_SECONDS, Deadline, WallClockExceeded


class AgentError(JsaError):
    """The run ended in an error result, so nothing from it may be recorded."""


@dataclass(frozen=True)
class AgentResult:
    text: str
    turns: int
    seconds: float
    cost: float | None


def _failure(
    subtype: str | None,
    api_error_status: int | None,
    errors: list[str] | None,
    result: str | None,
) -> AgentError:
    # An API failure after a completed loop reports subtype "success", which says nothing.
    details = [subtype] if subtype not in (None, "success") else []
    if api_error_status is not None:
        details.append(f"HTTP {api_error_status}")
    details.extend(errors or [])
    if result:
        details.append(result)
    return AgentError("Claude run failed: " + "; ".join(details))


async def _drive(
    prompt: str,
    options: ClaudeAgentOptions,
    on_message: Callable[[Message], None] | None,
) -> AgentResult:
    deadline = Deadline()
    narration: list[str] = []
    final: ResultMessage | None = None
    try:
        # A hung CLI sends no messages, so only a timeout can stop it.
        async with asyncio.timeout(WALL_CLOCK_CEILING_SECONDS):
            async for message in query(prompt=prompt, options=options):
                if on_message:
                    on_message(message)
                if isinstance(message, AssistantMessage):
                    narration.extend(
                        block.text
                        for block in message.content
                        if isinstance(block, TextBlock)
                    )
                elif isinstance(message, ResultMessage):
                    final = message
    except TimeoutError:
        raise WallClockExceeded(
            f"the Claude run ran past its {WALL_CLOCK_CEILING_SECONDS}-second ceiling"
        ) from None
    except ResultError as error:
        # The SDK raises this itself after an error result, carrying the real HTTP status.
        raise _failure(
            error.subtype, error.api_error_status, error.errors, error.result
        ) from None
    except ClaudeSDKError as error:
        raise AgentError(f"Claude run failed: {error}") from error
    if final is None:
        raise AgentError("Claude run ended without a result message")
    if final.is_error or final.subtype != "success":
        raise _failure(
            final.subtype, final.api_error_status, final.errors, final.result
        )
    text = final.result if final.result is not None else "\n".join(narration)
    return AgentResult(text, final.num_turns, deadline.elapsed, final.total_cost_usd)


def run_agent(
    prompt: str,
    settings: AgentSettings,
    *,
    tools: list[str],
    max_turns: int,
    permission_mode: PermissionMode,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    on_message: Callable[[Message], None] | None = None,
) -> AgentResult:
    """Run one headless Claude session; raises AgentError on an error result."""
    options = ClaudeAgentOptions(
        model=settings.model,
        effort=settings.effort,
        # `tools` removes every other built-in; `allowed_tools` would only skip prompts for these.
        tools=tools,
        # Settings or project files must not bring in MCP servers: they would be more tools.
        strict_mcp_config=True,
        # A user or project settings file could widen permissions past `tools` and the cwd.
        setting_sources=[],
        max_turns=max_turns,
        permission_mode=permission_mode,
        cwd=cwd,
        env=env or {},
    )
    return asyncio.run(_drive(prompt, options, on_message))
