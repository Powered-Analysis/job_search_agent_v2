"""The Perplexity runner (PRD 01): the Agent API's `high` preset on the `flex` tier, streamed and folded into a final answer."""

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace

import httpx

from jsa.config import api_key
from jsa.http import post_sse
from jsa.runners import READ_TIMEOUT_SECONDS, Deadline, RunnerError, RunnerResult
from jsa.search_output import output_json_schema

log = logging.getLogger(__name__)

URL = "https://api.perplexity.ai/v1/agent"
KEY_NAME = "PERPLEXITY_API_KEY"
_FAILURE_EVENTS = frozenset({"response.failed", "error"})
# The preset's searches and page fetches: its research steps.
_STEP_EVENTS = frozenset(
    {"response.reasoning.search_results", "response.reasoning.fetch_url_results"}
)
SERVICE_TIER = "flex"
# Double the preset's own step budget: the search template asks for per-posting index checks.
MAX_STEPS = 30


@dataclass(frozen=True)
class StreamState:
    """Everything the stream tells us, folded one event at a time."""

    deltas: tuple[str, ...] = ()
    final_text: str | None = None
    steps: int = 0
    cost: float | None = None
    model: str | None = None
    service_tier: str | None = None

    @property
    def text(self) -> str:
        # The done event is the complete answer; deltas are only the fallback.
        return self.final_text if self.final_text is not None else "".join(self.deltas)


def request_body(prompt: str) -> dict:
    # No `model` or `tools`: any further override replaces part of the preset's bundle.
    return {
        "preset": "high",
        "service_tier": SERVICE_TIER,
        "max_steps": MAX_STEPS,
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
        case _ if event in _STEP_EVENTS:
            return replace(state, steps=state.steps + 1)
        case "response.completed":
            response = data.get("response") or {}
            cost = ((response.get("usage") or {}).get("cost") or {}).get("total_cost")
            return replace(
                state,
                cost=cost,
                model=response.get("model"),
                service_tier=response.get("service_tier"),
            )
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
            read_timeout=READ_TIMEOUT_SECONDS,
        )
        for event, data in events:
            deadline.check()
            if event in _FAILURE_EVENTS:
                raise RunnerError(f"Perplexity reported a failure: {data}")
            before = state.steps
            state = fold(state, event, data)
            if state.steps != before:
                log.info("research step %d (%.0fs)", state.steps, deadline.elapsed)
            if deadline.heartbeat_due():
                log.info(
                    "still searching: %d research steps, %.0fs",
                    state.steps,
                    deadline.elapsed,
                )
        deadline.check()
        if not state.text:
            raise RunnerError("Perplexity's stream ended without a final answer")
        log.info(
            "perplexity done: %d research steps, %.0fs, cost %s, model %s, tier %s",
            state.steps,
            deadline.elapsed,
            "unknown" if state.cost is None else f"${state.cost:.2f}",
            state.model,
            state.service_tier,
        )
        return RunnerResult(state.text, state.model, None, state.cost)
