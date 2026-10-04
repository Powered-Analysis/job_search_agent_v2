"""The Gemini runner (PRD 01): the Deep Research agent through the Interactions API, streamed and folded into a final answer."""

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial

import httpx
from google import genai

from jsa.config import api_key
from jsa.profile import GeminiRunner as GeminiSettings
from jsa.runners import Deadline, RunnerError, RunnerResult

log = logging.getLogger(__name__)

KEY_NAME = "GEMINI_API_KEY"
POLL_SECONDS = 10
AGENT_CONFIG = {
    "type": "deep-research",
    # Without summaries the stream carries no progress; headless, so no plan to approve (PRD 01).
    "thinking_summaries": "auto",
    "visualization": "off",
    "collaborative_planning": False,
}
TOOLS = [{"type": "google_search"}, {"type": "url_context"}]

# Statuses after which the interaction can never complete.
_FAILED_STATUSES = frozenset({"failed", "cancelled", "incomplete", "budget_exceeded"})

# USD per million tokens at Gemini 3.1 Pro list rates, the model's standard tier. The API
# returns no billed amount, so the cost is an estimate; retrieved pages count as input.
INPUT_RATE = 2.00
CACHED_INPUT_RATE = 0.20
OUTPUT_RATE = 12.00


def make_client(key: str) -> genai.Client:
    # The one replaceable point for Gemini (XC-9): tests substitute a client that never reaches the network.
    return genai.Client(api_key=key)


@dataclass(frozen=True)
class StreamState:
    """Everything the stream tells us, folded one event at a time."""

    thought_steps: int = 0
    deltas: tuple[str, ...] = ()
    final_text: str | None = None
    interaction_id: str | None = None
    last_event_id: str | None = None
    status: str | None = None
    error: str | None = None
    usage: dict | None = None

    @property
    def text(self) -> str:
        # The completed interaction is the whole answer; deltas are only the fallback.
        return self.final_text if self.final_text is not None else "".join(self.deltas)

    @property
    def completed(self) -> bool:
        return self.status == "completed"

    @property
    def failure(self) -> str | None:
        if self.error is not None:
            return self.error
        if self.status in _FAILED_STATUSES:
            return f"the interaction ended {self.status}"
        return None


def fold(state: StreamState, event: dict) -> StreamState:
    """Pure: the stream state after one more event."""
    if event.get("event_id"):
        state = replace(state, last_event_id=event["event_id"])
    match event.get("event_type"):
        case "interaction.created" | "interaction.completed":
            interaction = event.get("interaction") or {}
            return replace(
                state,
                interaction_id=interaction.get("id") or state.interaction_id,
                status=interaction.get("status", state.status),
            )
        case "interaction.status_update":
            return replace(
                state,
                interaction_id=event.get("interaction_id") or state.interaction_id,
                status=event.get("status", state.status),
            )
        case "step.delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "thought_summary":
                return replace(state, thought_steps=state.thought_steps + 1)
            if delta.get("type") == "text":
                return replace(state, deltas=(*state.deltas, delta.get("text") or ""))
        case "error":
            error = event.get("error") or {}
            message = error.get("message") or error.get("code") or "no detail given"
            return replace(state, error=str(message))
    return state


def fold_events(events: Iterable[dict]) -> StreamState:
    state = StreamState()
    for event in events:
        state = fold(state, event)
    return state


def fold_interaction(state: StreamState, interaction: dict) -> StreamState:
    """Pure: the state after reading the whole interaction, which alone carries the final text and usage."""
    return replace(
        state,
        status=interaction.get("status", state.status),
        final_text=interaction.get("output_text") or state.final_text,
        usage=interaction.get("usage") or state.usage,
    )


def estimate_cost(usage: dict | None) -> float | None:
    """USD estimated from the interaction's token counts at the pinned rates."""
    if not usage:
        return None
    prompt = usage.get("total_input_tokens") or 0
    cached = usage.get("total_cached_tokens") or 0
    generated = (usage.get("total_output_tokens") or 0) + (
        usage.get("total_thought_tokens") or 0
    )
    tool_use = usage.get("total_tool_use_tokens") or 0
    return (
        (prompt - cached + tool_use) * INPUT_RATE
        + cached * CACHED_INPUT_RATE
        + generated * OUTPUT_RATE
    ) / 1_000_000


def _dropped(error: Exception) -> bool:
    # The SDK re-raises transport failures as its own exception types, with the cause attached.
    return isinstance(error, httpx.TransportError) or isinstance(
        error.__cause__, httpx.TransportError
    )


class GeminiAgentRunner:
    def __init__(
        self,
        settings: GeminiSettings,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        # Validated here, so a missing key raises before any request is made.
        self._client = make_client(api_key(KEY_NAME))
        self._agent = settings.agent
        self._sleep = sleep

    def run(self, prompt: str) -> RunnerResult:
        deadline = Deadline()
        state = self._consume(StreamState(), partial(self._create, prompt), deadline)
        while not state.completed:
            deadline.check()
            if state.interaction_id is None:
                raise RunnerError(
                    "the Gemini stream dropped before the interaction began"
                )
            before = state.last_event_id
            if before is not None:
                log.info("stream dropped; reconnecting from event %s", before)
                state = self._consume(
                    state,
                    partial(self._reconnect, state.interaction_id, before),
                    deadline,
                )
            if not state.completed and state.last_event_id == before:
                # A reconnect that gains nothing: ask the status instead of spinning on it.
                state = self._poll(state)
                if not state.completed:
                    self._sleep(POLL_SECONDS)
        state = self._read_interaction(state)
        if not state.text:
            raise RunnerError("Gemini's interaction completed without a final answer")
        cost = estimate_cost(state.usage)
        log.info(
            "gemini done: %d thought steps, %.0fs, %s",
            state.thought_steps,
            deadline.elapsed,
            "cost unknown"
            if cost is None
            else f"estimated cost ${cost:.2f} from token usage {state.usage}",
        )
        return RunnerResult(
            state.text, self._agent, None, cost, cost_is_estimate=cost is not None
        )

    def _create(self, prompt: str):
        return self._client.interactions.create(
            agent=self._agent,
            background=True,
            stream=True,
            input=prompt,
            agent_config=AGENT_CONFIG,
            tools=TOOLS,
        )

    def _reconnect(self, interaction_id: str, last_event_id: str):
        return self._client.interactions.get(
            interaction_id, stream=True, last_event_id=last_event_id
        )

    def _consume(
        self,
        state: StreamState,
        open_events: Callable[[], Iterable],
        deadline: Deadline,
    ) -> StreamState:
        """Fold events until the stream ends or drops; a drop is not an error, the run reconnects."""
        try:
            for raw in open_events():
                deadline.check()
                state = fold(state, raw.model_dump(mode="json", exclude_none=True))
                if state.failure:
                    raise RunnerError(f"Gemini reported a failure: {state.failure}")
                if deadline.heartbeat_due():
                    log.info(
                        "still researching: %d thought steps, %.0fs",
                        state.thought_steps,
                        deadline.elapsed,
                    )
        except Exception as error:
            if not _dropped(error):
                raise
            log.warning("Gemini stream dropped: %s", error)
        return state

    def _read_interaction(self, state: StreamState) -> StreamState:
        interaction = self._client.interactions.get(state.interaction_id)
        return fold_interaction(
            state, interaction.model_dump(mode="json", exclude_none=True)
        )

    def _poll(self, state: StreamState) -> StreamState:
        try:
            state = self._read_interaction(state)
        except Exception as error:
            if not _dropped(error):
                raise
            log.warning("Gemini status check failed: %s", error)
        if state.failure:
            raise RunnerError(f"Gemini reported a failure: {state.failure}")
        return state
