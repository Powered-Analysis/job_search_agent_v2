"""The Perplexity runner (PRD 01): the Agent API's `xhigh` effort, run in the background, streamed and folded into a final answer."""

import logging
import time
from collections.abc import Callable, Iterable
from contextlib import closing
from dataclasses import dataclass, replace
from functools import partial

import httpx

from jsa.config import SEARCH_AGENT_KEYS, api_key
from jsa.http import SseEvents
from jsa.runners import (
    CANCEL_TIMEOUT_SECONDS,
    POLL_SECONDS,
    Deadline,
    RunnerError,
    RunnerResult,
    within,
)
from jsa.search_output import output_json_schema

log = logging.getLogger(__name__)

URL = "https://api.perplexity.ai/v1/agent"
# Statuses after which the run can never change.
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "incomplete"})
# What a terminal event means when its response object does not say.
_EVENT_STATUSES = {
    "response.completed": "completed",
    "response.failed": "failed",
    "response.cancelled": "cancelled",
    "response.incomplete": "incomplete",
}


@dataclass(frozen=True)
class StreamState:
    """Everything the stream tells us, folded one event at a time."""

    deltas: tuple[str, ...] = ()
    final_text: str | None = None
    response_id: str | None = None
    sequence_number: int | None = None
    sandbox_steps: int = 0
    status: str | None = None
    reason: str | None = None
    cost: float | None = None
    model: str | None = None

    @property
    def text(self) -> str:
        # The done event is the complete answer; deltas are only the fallback.
        return self.final_text if self.final_text is not None else "".join(self.deltas)

    @property
    def ended(self) -> bool:
        """True once Perplexity itself has stopped working on the run."""
        return self.status in _TERMINAL_STATUSES


def request_body(prompt: str) -> dict:
    # No `model`, `max_steps`, or `tools`: any override replaces part of the effort's bundle.
    return {
        "effort": "xhigh",
        "input": prompt,
        "background": True,
        "stream": True,
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "search_output", "schema": output_json_schema()},
        },
    }


def message_text(response: dict) -> str | None:
    """Pure: the text of the last output message of a response object, which carries the final answer."""
    texts = [
        "".join(
            part.get("text") or ""
            for part in item.get("content") or []
            if part.get("type") == "output_text"
        )
        for item in response.get("output") or []
        if item.get("type") == "message"
    ]
    return texts[-1] if texts else None


def fold_response(
    state: StreamState, response: dict, status: str | None
) -> StreamState:
    """Pure: the state after reading a response object, from a stream event or a snapshot."""
    cost = ((response.get("usage") or {}).get("cost") or {}).get("total_cost")
    details = response.get("incomplete_details") or response.get("error") or {}
    return replace(
        state,
        response_id=response.get("id") or state.response_id,
        status=response.get("status") or status or state.status,
        reason=details.get("reason") or details.get("message") or state.reason,
        final_text=message_text(response) or state.final_text,
        cost=cost if cost is not None else state.cost,
        model=response.get("model") or state.model,
    )


def fold(state: StreamState, event: str, data: dict) -> StreamState:
    """Pure: the stream state after one more event."""
    if isinstance(data.get("sequence_number"), int):
        state = replace(state, sequence_number=data["sequence_number"])
    match event:
        case "response.output_text.delta":
            return replace(state, deltas=(*state.deltas, data.get("delta") or ""))
        case "response.output_text.done":
            return replace(state, final_text=data.get("text"))
        case "response.sandbox.results":
            # `xhigh` does its searching from inside the sandbox, so these are its research steps.
            return replace(state, sandbox_steps=state.sandbox_steps + 1)
        case "response.created" | "response.in_progress":
            return fold_response(state, data.get("response") or {}, None)
        case (
            "response.completed"
            | "response.failed"
            | "response.cancelled"
            | "response.incomplete"
        ):
            return fold_response(
                state, data.get("response") or {}, _EVENT_STATUSES[event]
            )
    return state


def fold_events(events: Iterable[tuple[str, dict]]) -> StreamState:
    state = StreamState()
    for event, data in events:
        state = fold(state, event, data)
    return state


class PerplexityRunner:
    def __init__(
        self, client: httpx.Client, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        # Validated here, so a missing key raises before any request is made.
        self._key = api_key(SEARCH_AGENT_KEYS["perplexity"])
        self._client = client
        self._sleep = sleep
        self._headers = {"Authorization": f"Bearer {self._key}"}
        # The latest stream state, kept here so a run that raises still knows which response to cancel.
        self._latest = StreamState()
        self._reconnects = 0

    def run(self, prompt: str) -> RunnerResult:
        self._latest = StreamState()
        self._reconnects = 0
        try:
            return self._research(prompt)
        except BaseException:
            # A background run outlives the connection: left alone it keeps working and billing (PRD 01).
            self._cancel_unfinished()
            raise

    def _research(self, prompt: str) -> RunnerResult:
        deadline = Deadline()
        state = self._consume(
            StreamState(), partial(self._create, prompt, deadline), deadline
        )
        # Once the reconnect window has passed (HTTP 400) the run is polled instead.
        window_open = True
        while not state.ended:
            deadline.check()
            if state.response_id is None:
                raise RunnerError("the Perplexity stream dropped before the run began")
            before = state.sequence_number
            if window_open and before is not None:
                log.info("stream dropped; reconnecting after event %s", before)
                self._reconnects += 1
                try:
                    state = self._consume(
                        state,
                        partial(self._reconnect, state.response_id, before, deadline),
                        deadline,
                    )
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 400:
                        raise
                    log.info("reconnect window has passed; polling the run")
                    window_open = False
            if not state.ended and (not window_open or state.sequence_number == before):
                # A reconnect that gains nothing, or none left: ask the status instead of spinning on it.
                state = self._poll(state, deadline)
                if not state.ended:
                    self._sleep(POLL_SECONDS)
        return self._outcome(state, deadline)

    def _create(self, prompt: str, deadline: Deadline) -> SseEvents:
        return SseEvents(
            self._client,
            URL,
            request_body(prompt),
            headers=self._headers,
            read_timeout=deadline.request_timeout(),
        )

    def _reconnect(
        self, response_id: str, sequence_number: int, deadline: Deadline
    ) -> SseEvents:
        return SseEvents(
            self._client,
            f"{URL}/{response_id}",
            method="GET",
            params={"stream": "true", "starting_after": sequence_number},
            headers=self._headers,
            read_timeout=deadline.request_timeout(),
        )

    def _outcome(self, state: StreamState, deadline: Deadline) -> RunnerResult:
        if state.status == "completed" or (state.status == "incomplete" and state.text):
            if not state.text:
                raise RunnerError(
                    f"Perplexity's run completed without a final answer (response {state.response_id})"
                )
            if state.status == "incomplete":
                log.warning(
                    "Perplexity's run ended incomplete (%s); using the text it produced (response %s)",
                    state.reason or "no reason given",
                    state.response_id,
                )
            log.info(
                "perplexity done: %d sandbox steps, %.0fs, %d reconnects, cost %s, model %s",
                state.sandbox_steps,
                deadline.elapsed,
                self._reconnects,
                "unknown" if state.cost is None else f"${state.cost:.2f}",
                state.model,
            )
            return RunnerResult(state.text, state.model, None, state.cost)
        raise self._failure(state)

    @staticmethod
    def _failure(state: StreamState) -> RunnerError:
        return RunnerError(
            f"Perplexity's run ended {state.status} "
            f"({state.reason or 'no reason given'}), response {state.response_id}"
        )

    def _consume(
        self,
        state: StreamState,
        open_events: Callable[[], SseEvents],
        deadline: Deadline,
    ) -> StreamState:
        """Fold events until the stream ends or drops; a drop is not an error, the run reconnects."""
        try:
            with closing(within(open_events(), deadline)) as events:
                for event, data in events:
                    deadline.check()
                    if event == "error":
                        raise RunnerError(f"Perplexity reported an error: {data}")
                    before = state.sandbox_steps
                    state = self._track(fold(state, event, data))
                    if state.status in {"failed", "cancelled"}:
                        raise self._failure(state)
                    if state.ended:
                        break
                    if state.sandbox_steps != before:
                        log.info(
                            "sandbox step %d (%.0fs)",
                            state.sandbox_steps,
                            deadline.elapsed,
                        )
                    if deadline.heartbeat_due():
                        log.info(
                            "still searching: %d sandbox steps, %.0fs",
                            state.sandbox_steps,
                            deadline.elapsed,
                        )
        except httpx.TransportError as error:
            log.warning("Perplexity stream dropped: %s", error)
        return state

    def _poll(self, state: StreamState, deadline: Deadline) -> StreamState:
        try:
            response = self._client.get(
                f"{URL}/{state.response_id}",
                headers=self._headers,
                timeout=deadline.request_timeout(),
            )
            response.raise_for_status()
        except httpx.TransportError as error:
            log.warning("Perplexity status check failed: %s", error)
            return state
        state = self._track(fold_response(state, response.json(), None))
        if state.status in {"failed", "cancelled"}:
            raise self._failure(state)
        return state

    def _track(self, state: StreamState) -> StreamState:
        if state.response_id and not self._latest.response_id:
            # Logged at once, so a run that dies uncancelled can still be found and cancelled by hand.
            log.info("perplexity response %s", state.response_id)
        self._latest = state
        return state

    def _cancel_unfinished(self) -> None:
        response_id = self._latest.response_id
        if response_id is None or self._latest.ended:
            return
        try:
            response = self._client.post(
                f"{URL}/{response_id}/cancel",
                headers=self._headers,
                timeout=CANCEL_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except Exception:
            # Never let a failed cancel hide the error that ended the run.
            log.exception(
                "Could not cancel Perplexity response %s, so it may still be running",
                response_id,
            )
        else:
            log.info("cancelled Perplexity response %s", response_id)
