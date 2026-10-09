"""The Gemini runner (PRD 01): the Deep Research agent through the Interactions API, streamed and folded into a final answer."""

import logging
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from functools import partial

import httpx
from google import genai

from jsa.config import SEARCH_AGENT_KEYS, api_key
from jsa.runners import (
    READ_TIMEOUT_SECONDS,
    Deadline,
    RunnerError,
    RunnerResult,
    clip,
    within,
)

log = logging.getLogger(__name__)

# Pinned (PRD 01, XC-14): the only other Deep Research agent runs past the wall-clock ceiling on this search.
AGENT = "deep-research-preview-04-2026"
POLL_SECONDS = 10
# The cancel is sent after the run's own deadline may have passed, so it has its own short one.
CANCEL_TIMEOUT_SECONDS = 30
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
    # HttpOptions.timeout is in milliseconds. It is only the default: each call passes its own, cut to the time left.
    return genai.Client(
        api_key=key,
        http_options=genai.types.HttpOptions(timeout=READ_TIMEOUT_SECONDS * 1000),
    )


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
    def ended(self) -> bool:
        """True once Gemini itself has stopped working on the interaction."""
        return self.completed or self.status in _FAILED_STATUSES

    @property
    def failure(self) -> str | None:
        if self.error is not None:
            return self.error
        if self.status in _FAILED_STATUSES:
            return f"the interaction ended {self.status}"
        return None


def thought_summary(event: dict) -> str | None:
    """Pure: the text of a thought-summary event, the agent's own account of what it is doing."""
    delta = event.get("delta") or {}
    if (
        event.get("event_type") != "step.delta"
        or delta.get("type") != "thought_summary"
    ):
        return None
    return (delta.get("content") or {}).get("text") or None


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
    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        # Validated here, so a missing key raises before any request is made.
        self._client = make_client(api_key(SEARCH_AGENT_KEYS["gemini"]))
        self._sleep = sleep
        # The latest stream state, kept here so a run that raises still knows which interaction to cancel.
        self._latest = StreamState()

    def run(self, prompt: str) -> RunnerResult:
        try:
            return self._research(prompt)
        except BaseException:
            # A background interaction outlives the connection: left alone it keeps working and billing (PRD 01).
            self._cancel_unfinished()
            raise

    def _research(self, prompt: str) -> RunnerResult:
        deadline = Deadline()
        state = self._consume(
            StreamState(), partial(self._create, prompt, deadline), deadline
        )
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
                    partial(self._reconnect, state.interaction_id, before, deadline),
                    deadline,
                )
            if not state.completed and state.last_event_id == before:
                # A reconnect that gains nothing: ask the status instead of spinning on it.
                state = self._poll(state, deadline)
                if not state.completed:
                    self._sleep(POLL_SECONDS)
        state = self._read_interaction(state, deadline)
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
            state.text, AGENT, None, cost, cost_is_estimate=cost is not None
        )

    def _create(self, prompt: str, deadline: Deadline):
        return self._client.interactions.create(
            agent=AGENT,
            background=True,
            stream=True,
            input=prompt,
            agent_config=AGENT_CONFIG,
            tools=TOOLS,
            timeout=deadline.request_timeout(),
        )

    def _reconnect(self, interaction_id: str, last_event_id: str, deadline: Deadline):
        return self._client.interactions.get(
            interaction_id,
            stream=True,
            last_event_id=last_event_id,
            timeout=deadline.request_timeout(),
        )

    def _consume(
        self,
        state: StreamState,
        open_events: Callable[[], Iterable],
        deadline: Deadline,
    ) -> StreamState:
        """Fold events until the stream ends or drops; a drop is not an error, the run reconnects."""
        try:
            with closing(within(open_events(), deadline)) as stream:
                for raw in stream:
                    deadline.check()
                    event = raw.model_dump(mode="json", exclude_none=True)
                    state = self._track(fold(state, event))
                    if summary := thought_summary(event):
                        log.info("gemini: %s", clip(summary))
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

    def _read_interaction(self, state: StreamState, deadline: Deadline) -> StreamState:
        interaction = self._client.interactions.get(
            state.interaction_id, timeout=deadline.request_timeout()
        )
        return self._track(
            fold_interaction(
                state, interaction.model_dump(mode="json", exclude_none=True)
            )
        )

    def _track(self, state: StreamState) -> StreamState:
        if state.interaction_id and not self._latest.interaction_id:
            # Logged at once, so a run that dies uncancelled can still be found and cancelled by hand.
            log.info("gemini interaction %s", state.interaction_id)
        self._latest = state
        return state

    def _cancel_unfinished(self) -> None:
        interaction_id = self._latest.interaction_id
        if interaction_id is None or self._latest.ended:
            return
        try:
            self._client.interactions.cancel(
                interaction_id, timeout=CANCEL_TIMEOUT_SECONDS
            )
        except Exception:
            # Never let a failed cancel hide the error that ended the run.
            log.exception(
                "Could not cancel Gemini interaction %s, so it may still be running",
                interaction_id,
            )
        else:
            log.info("cancelled Gemini interaction %s", interaction_id)

    def _poll(self, state: StreamState, deadline: Deadline) -> StreamState:
        try:
            state = self._read_interaction(state, deadline)
        except Exception as error:
            if not _dropped(error):
                raise
            log.warning("Gemini status check failed: %s", error)
        if state.failure:
            raise RunnerError(f"Gemini reported a failure: {state.failure}")
        return state
