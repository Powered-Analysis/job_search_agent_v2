"""The Perplexity runner (PRD 01): the Agent API's `xhigh` preset, streamed and folded into a final answer."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace

import httpx

from jsa.config import api_key
from jsa.http import post_sse
from jsa.runners import Deadline, RunnerError, RunnerResult
from jsa.search_output import output_json_schema

log = logging.getLogger(__name__)

URL = "https://api.perplexity.ai/v1/agent"
KEY_NAME = "PERPLEXITY_API_KEY"
_FAILURE_EVENTS = frozenset({"response.failed", "error"})


@dataclass(frozen=True)
class StreamState:
    """Everything the stream tells us, folded one event at a time."""

    deltas: tuple[str, ...] = ()
    final_text: str | None = None
    sandbox_steps: int = 0
    cost: float | None = None
    model: str | None = None

    @property
    def text(self) -> str:
        # The done event is the complete answer; deltas are only the fallback.
        return self.final_text if self.final_text is not None else "".join(self.deltas)


def request_body(prompt: str) -> dict:
    # No `model`, `max_steps`, or `tools`: any override replaces part of the preset's bundle.
    return {
        "preset": "xhigh",
        "input": prompt,
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "search_output", "schema": output_json_schema()},
        },
    }


def fold(state: StreamState, event: str, data: dict) -> StreamState:
    """Pure: the stream state after one more event."""
    match event:
        case "response.output_text.delta":
            return replace(state, deltas=(*state.deltas, data.get("delta") or ""))
        case "response.output_text.done":
            return replace(state, final_text=data.get("text"))
        case "response.sandbox.results":
            return replace(state, sandbox_steps=state.sandbox_steps + 1)
        case "response.completed":
            response = data.get("response") or {}
            cost = ((response.get("usage") or {}).get("cost") or {}).get("total_cost")
            return replace(state, cost=cost, model=response.get("model"))
    return state


def fold_events(events: Iterable[tuple[str, dict]]) -> StreamState:
    state = StreamState()
    for event, data in events:
        state = fold(state, event, data)
    return state


class PerplexityRunner:
    def __init__(self, client: httpx.Client) -> None:
        # Validated here, so a missing key raises before any request is made.
        self._key = api_key(KEY_NAME)
        self._client = client

    def run(self, prompt: str) -> RunnerResult:
        deadline = Deadline()
        state = StreamState()
        events = post_sse(
            self._client,
            URL,
            request_body(prompt),
            headers={"Authorization": f"Bearer {self._key}"},
            read_timeout=deadline.request_timeout(),
        )
        for event, data in events:
            deadline.check()
            if event in _FAILURE_EVENTS:
                raise RunnerError(f"Perplexity reported a failure: {data}")
            before = state.sandbox_steps
            state = fold(state, event, data)
            if state.sandbox_steps != before:
                log.info(
                    "sandbox step %d (%.0fs)", state.sandbox_steps, deadline.elapsed
                )
            if deadline.heartbeat_due():
                log.info(
                    "still searching: %d sandbox steps, %.0fs",
                    state.sandbox_steps,
                    deadline.elapsed,
                )
        deadline.check()
        if not state.text:
            raise RunnerError("Perplexity's stream ended without a final answer")
        log.info(
            "perplexity done: %d sandbox steps, %.0fs, cost %s, model %s",
            state.sandbox_steps,
            deadline.elapsed,
            "unknown" if state.cost is None else f"${state.cost:.2f}",
            state.model,
        )
        return RunnerResult(state.text, state.model, None, state.cost)
