"""The Perplexity runner in background mode (issue #186; PRD 01 "Runners → Perplexity runner", "Edge cases", "Timeouts/limits"; XC-9).

Perplexity is replaced at the injected `httpx` transport. The runner's HTTP calls are scripted
per route: the create POST, the reconnect GET (`stream=true`), the status GET, and the cancel POST.
"""

import json
import logging

import httpx
import pytest

from jsa.errors import JsaError
from jsa.http import make_client
from jsa.perplexity import PerplexityRunner, StreamState, fold, fold_events
from jsa.runners import Deadline, RunnerError, WallClockExceeded

ID = "resp-1"
MODEL = "anthropic/claude-opus-5-5"
COST = 2.5
ANSWER = json.dumps({"postings": []})
RUN_URL = "/v1/agent"
RESPONSE_URL = f"/v1/agent/{ID}"
CANCEL_URL = f"/v1/agent/{ID}/cancel"


# --- scripted events and snapshots ---------------------------------------------------


def created(seq=0):
    return (
        "response.created",
        {"sequence_number": seq, "response": {"id": ID, "status": "in_progress"}},
    )


def step(seq):
    return "response.sandbox.results", {"sequence_number": seq}


def answer(seq, text=ANSWER):
    return "response.output_text.done", {"sequence_number": seq, "text": text}


def terminal(seq, status="completed", **response):
    fields = {"id": ID, "status": status, "model": MODEL, **response}
    if status == "completed":
        fields["usage"] = {"cost": {"total_cost": COST}}
    return f"response.{status}", {"sequence_number": seq, "response": fields}


def snapshot(status, text=None, **fields):
    output = [{"type": "reasoning", "summary": []}]
    if text is not None:
        output += [
            {"type": "message", "content": [{"type": "output_text", "text": "draft"}]},
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
        ]
    return {"id": ID, "status": status, "model": MODEL, "output": output, **fields}


def sse(events):
    return [
        f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events
    ]


class Body(httpx.SyncByteStream):
    """A server-sent-event body that serves one event per chunk, then ends or fails."""

    def __init__(self, events, *, then=None, on_chunk=lambda _n: None):
        self.chunks = sse(events)
        self.then = then
        self.on_chunk = on_chunk

    def __iter__(self):
        for number, chunk in enumerate(self.chunks, 1):
            self.on_chunk(number)
            yield chunk
        if self.then:
            raise self.then

    def close(self):
        pass


def stream(events, **kwargs):
    return httpx.Response(
        200,
        stream=Body(events, **kwargs),
        headers={"content-type": "text/event-stream"},
    )


def dropped(events):
    """A stream whose connection fails after its events."""
    return stream(events, then=httpx.ReadError("connection reset"))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Perplexity:
    """The Agent API. `create` is the response to the POST; `reconnects` and `snapshots` are served in turn (a snapshot list's last entry repeats)."""

    def __init__(self):
        self.create = stream([created(), answer(1), terminal(2)])
        self.reconnects = []
        self.snapshots = []
        self.cancel = httpx.Response(200, json={"id": ID, "status": "cancelled"})
        self.requests = []
        self.sleeps = []
        self.clock = None

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        if self.clock is not None:
            self.clock.now += seconds

    def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST" and path == RUN_URL:
            return self.create
        if request.method == "POST" and path == CANCEL_URL:
            return self.cancel
        if request.method == "GET" and path == RESPONSE_URL:
            if request.url.params.get("stream") == "true":
                return self.reconnects.pop(0)
            if len(self.snapshots) > 1:
                return self._json(self.snapshots.pop(0))
            return self._json(self.snapshots[0])
        return httpx.Response(404, json={"error": "not found"})

    @staticmethod
    def _json(value):
        if isinstance(value, httpx.Response):
            return value
        return httpx.Response(200, json=value)

    def of(self, method, path=None):
        return [
            r
            for r in self.requests
            if r.method == method and path in (None, r.url.path)
        ]

    def reconnect_requests(self):
        return [
            r
            for r in self.of("GET", RESPONSE_URL)
            if r.url.params.get("stream") == "true"
        ]

    def poll_requests(self):
        return [
            r
            for r in self.of("GET", RESPONSE_URL)
            if r.url.params.get("stream") != "true"
        ]

    def run(self, prompt="a prompt"):
        with make_client(httpx.MockTransport(self.handle)) as client:
            return PerplexityRunner(client, sleep=self.sleep).run(prompt)


@pytest.fixture
def api(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    # No stray `.env` in the working directory may supply a key.
    monkeypatch.chdir(tmp_path)
    caplog.set_level(logging.INFO, logger="jsa")
    return Perplexity()


def with_clock(api, monkeypatch):
    api.clock = Clock()
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, api.clock))
    return api.clock


def messages(caplog, level=logging.INFO):
    return [r.getMessage() for r in caplog.records if r.levelno >= level]


# --- folding: the response id and the latest sequence number ---------------------------


def test_the_response_id_comes_from_the_created_event():
    state = fold_events([created()])
    assert state.response_id == ID


def test_the_latest_sequence_number_is_tracked_across_events():
    state = fold_events([created(0), step(1), step(2), answer(3)])
    assert state.sequence_number == 3


def test_a_terminal_event_sets_the_status():
    state = fold_events([created(), terminal(1, "incomplete")])
    assert state.status == "incomplete"


def test_folding_a_sequenced_event_leaves_the_previous_state_unchanged():
    start = fold_events([created()])
    fold(start, *step(5))
    assert start == fold_events([created()])
    assert StreamState().sequence_number is None


# --- the happy path ----------------------------------------------------------------


def test_a_completed_run_returns_its_answer_model_and_cost(api):
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    assert result.model == MODEL
    assert result.effort is None
    assert result.cost == COST


def test_a_completed_run_is_not_cancelled_and_needs_no_reconnect(api):
    api.run()
    assert api.of("POST", CANCEL_URL) == []
    assert api.reconnect_requests() == []
    assert api.poll_requests() == []


def test_the_response_id_is_logged_as_soon_as_it_arrives(api, caplog):
    seen = []

    def at_second_event(number):
        if number == 2:
            seen.append(any(ID in line for line in messages(caplog)))

    api.create = stream(
        [created(), step(1), answer(2), terminal(3)], on_chunk=at_second_event
    )
    api.run()
    assert seen == [True]


# --- reconnecting a dropped stream -----------------------------------------------------


def test_a_stream_that_closes_without_a_terminal_event_resumes_after_the_last_sequence_number(
    api,
):
    api.create = stream([created(0), step(1), step(2)])
    api.reconnects = [stream([answer(3), terminal(4)])]
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    (reconnect,) = api.reconnect_requests()
    assert reconnect.url.params["starting_after"] == "2"
    assert reconnect.url.params["stream"] == "true"


def test_the_run_is_created_once_however_many_times_the_stream_drops(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [stream([step(2)]), stream([answer(3), terminal(4)])]
    api.run()
    assert len(api.of("POST", RUN_URL)) == 1


def test_each_reconnect_resumes_after_the_latest_sequence_number_seen(api):
    api.create = stream([created(0), step(1), step(2)])
    api.reconnects = [stream([step(3), step(4)]), stream([answer(5), terminal(6)])]
    api.run()
    resumed = [r.url.params["starting_after"] for r in api.reconnect_requests()]
    assert resumed == ["2", "4"]


def test_a_connection_error_mid_stream_is_resumed_not_raised(api):
    api.create = dropped([created(0), step(1)])
    api.reconnects = [stream([answer(2), terminal(3)])]
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    (reconnect,) = api.reconnect_requests()
    assert reconnect.url.params["starting_after"] == "1"


def test_a_run_resumed_after_a_drop_keeps_the_terminal_cost_and_model(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [stream([answer(2), terminal(3)])]
    result = api.run()
    assert (result.model, result.cost) == (MODEL, COST)


def test_a_drop_that_loses_no_work_is_not_an_error_and_does_not_cancel(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [stream([answer(2), terminal(3)])]
    api.run()
    assert api.of("POST", CANCEL_URL) == []


# --- polling once the reconnect window has passed --------------------------------------


def window_passed(api, *snapshots):
    api.create = stream([created(0), step(1)])
    api.reconnects = [httpx.Response(400, json={"error": "cannot resume"})]
    api.snapshots = list(snapshots)


def test_a_400_on_reconnect_falls_back_to_polling_the_run(api):
    window_passed(api, snapshot("completed", ANSWER))
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    assert len(api.reconnect_requests()) == 1
    assert len(api.poll_requests()) >= 1


def test_polling_takes_the_text_of_the_last_output_message(api):
    window_passed(api, snapshot("completed", '{"postings": [], "marker": "last"}'))
    result = api.run()
    assert json.loads(result.text)["marker"] == "last"


def test_polling_continues_until_the_status_is_terminal(api):
    window_passed(
        api,
        snapshot("in_progress"),
        snapshot("in_progress"),
        snapshot("completed", ANSWER),
    )
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    assert len(api.poll_requests()) == 3
    assert len(api.sleeps) >= 2


def test_polling_after_the_window_has_passed_does_not_reconnect_again(api):
    window_passed(
        api,
        snapshot("in_progress"),
        snapshot("in_progress"),
        snapshot("completed", ANSWER),
    )
    api.run()
    assert len(api.reconnect_requests()) == 1


def test_a_snapshot_that_ends_cancelled_raises_with_the_status_and_response_id(api):
    window_passed(api, snapshot("cancelled"))
    with pytest.raises(RunnerError) as raised:
        api.run()
    assert "cancelled" in str(raised.value)
    assert ID in str(raised.value)


def test_polling_stops_at_the_wall_clock_ceiling_and_cancels_the_run(api, monkeypatch):
    with_clock(api, monkeypatch)
    window_passed(api, snapshot("in_progress"))
    with pytest.raises(WallClockExceeded):
        api.run()
    assert len(api.of("POST", CANCEL_URL)) == 1


# --- terminal statuses ------------------------------------------------------------------


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_a_run_ending_failed_or_cancelled_raises_naming_status_and_id(api, status):
    api.create = stream([created(0), step(1), terminal(2, status)])
    with pytest.raises(RunnerError) as raised:
        api.run()
    assert status in str(raised.value)
    assert ID in str(raised.value)


def test_a_run_ending_incomplete_without_text_names_status_reason_and_id(api):
    api.create = stream(
        [
            created(0),
            step(1),
            terminal(2, "incomplete", incomplete_details={"reason": "max_steps"}),
        ]
    )
    with pytest.raises(RunnerError) as raised:
        api.run()
    message = str(raised.value)
    assert "incomplete" in message
    assert "max_steps" in message
    assert ID in message


def test_a_run_ending_incomplete_without_text_is_a_jsa_error(api):
    api.create = stream([created(0), terminal(1, "incomplete")])
    with pytest.raises(JsaError):
        api.run()


def test_an_incomplete_run_with_text_returns_the_text_and_warns_with_the_status(
    api, caplog
):
    api.create = stream(
        [
            created(0),
            step(1),
            answer(2),
            terminal(3, "incomplete", incomplete_details={"reason": "max_steps"}),
        ]
    )
    result = api.run()
    assert json.loads(result.text) == {"postings": []}
    warnings = messages(caplog, logging.WARNING)
    assert any("incomplete" in line for line in warnings)


def test_a_completed_run_logs_no_warning(api, caplog):
    api.run()
    assert messages(caplog, logging.WARNING) == []


def test_a_run_that_ended_on_its_own_is_not_cancelled(api):
    api.create = stream([created(0), terminal(1, "failed")])
    with pytest.raises(RunnerError):
        api.run()
    assert api.of("POST", CANCEL_URL) == []


# --- cancelling when the app stops waiting -----------------------------------------------


def test_passing_the_ceiling_cancels_the_run_before_returning(api, monkeypatch):
    clock = with_clock(api, monkeypatch)

    def pass_the_ceiling(number):
        if number == 2:
            clock.now = 3601.0

    api.create = stream(
        [created(0), step(1), step(2), step(3), answer(4), terminal(5)],
        on_chunk=pass_the_ceiling,
    )
    with pytest.raises(WallClockExceeded):
        api.run()
    (cancel,) = api.of("POST", CANCEL_URL)
    assert cancel.headers["authorization"] == "Bearer test-perplexity-key"


def test_an_error_event_cancels_the_run_and_raises(api):
    api.create = stream(
        [created(0), step(1), ("error", {"sequence_number": 2, "message": "boom"})]
    )
    with pytest.raises(RunnerError):
        api.run()
    assert len(api.of("POST", CANCEL_URL)) == 1


def test_an_interrupt_cancels_the_run(api):
    def interrupt(number):
        if number == 2:
            raise KeyboardInterrupt

    api.create = stream([created(0), step(1), step(2)], on_chunk=interrupt)
    with pytest.raises(KeyboardInterrupt):
        api.run()
    assert len(api.of("POST", CANCEL_URL)) == 1


def test_a_run_cancelled_after_a_reconnect_is_cancelled_by_the_same_id(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [
        stream([step(2), ("error", {"sequence_number": 3, "message": "boom"})])
    ]
    with pytest.raises(RunnerError):
        api.run()
    assert len(api.of("POST", CANCEL_URL)) == 1


@pytest.mark.parametrize(
    "cancel",
    [httpx.Response(500, json={"error": "down"}), httpx.Response(404, json={})],
    ids=["server-error", "not-found"],
)
def test_a_failing_cancel_is_logged_with_the_id_and_does_not_mask_the_error(
    api, caplog, cancel
):
    api.create = stream(
        [created(0), ("error", {"sequence_number": 1, "message": "original failure"})]
    )
    api.cancel = cancel
    with pytest.raises(RunnerError) as raised:
        api.run()
    assert "original failure" in str(raised.value)
    assert any(ID in line for line in messages(caplog, logging.WARNING))


def test_a_cancel_that_cannot_connect_does_not_mask_the_error(api, caplog):
    api.create = stream(
        [created(0), ("error", {"sequence_number": 1, "message": "original failure"})]
    )

    def handle(request):
        if request.url.path == CANCEL_URL:
            raise httpx.ConnectError("no route")
        return Perplexity.handle(api, request)

    with (
        make_client(httpx.MockTransport(handle)) as client,
        pytest.raises(RunnerError) as raised,
    ):
        PerplexityRunner(client, sleep=api.sleep).run("p")
    assert "original failure" in str(raised.value)
    assert any(ID in line for line in messages(caplog, logging.WARNING))


def test_an_error_before_the_run_has_an_id_raises_without_a_cancel(api):
    api.create = stream([("error", {"sequence_number": 0, "message": "refused"})])
    with pytest.raises(RunnerError):
        api.run()
    assert api.of("POST", CANCEL_URL) == []


def test_the_ceiling_spans_reconnects(api, monkeypatch):
    clock = with_clock(api, monkeypatch)

    def tick(number):
        clock.now += 1500.0

    api.create = stream([created(0), step(1)], on_chunk=tick)
    api.reconnects = [stream([step(2), step(3)], on_chunk=tick) for _ in range(5)]
    with pytest.raises(WallClockExceeded):
        api.run()
    assert len(api.of("POST", CANCEL_URL)) == 1
    assert len(api.reconnect_requests()) < 5


# --- what every request carries -----------------------------------------------------------


def test_every_request_is_authorised(api):
    window_passed(api, snapshot("in_progress"), snapshot("completed", ANSWER))
    api.run()
    assert {r.method for r in api.requests} == {"POST", "GET"}
    for request in api.requests:
        assert request.headers["authorization"] == "Bearer test-perplexity-key"


def test_every_connection_allows_an_1800_second_read_timeout(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [stream([answer(2), terminal(3)])]
    api.run()
    assert len(api.requests) == 2
    for request in api.requests:
        assert request.extensions["timeout"]["read"] == 1800


def test_the_create_request_carries_effort_and_background_and_no_preset(api):
    api.run("the prompt")
    (request,) = api.of("POST", RUN_URL)
    body = json.loads(request.content)
    assert body["effort"] == "xhigh"
    assert body["background"] is True
    assert body["input"] == "the prompt"
    for forbidden in ("preset", "model", "max_steps", "tools"):
        assert forbidden not in body


def test_no_request_of_a_reconnecting_polling_cancelled_run_mentions_a_preset(api):
    api.create = stream([created(0), step(1)])
    api.reconnects = [httpx.Response(400, json={"error": "cannot resume"})]
    api.snapshots = [snapshot("cancelled")]
    with pytest.raises(RunnerError):
        api.run()
    assert api.reconnect_requests() and api.poll_requests()
    for request in api.requests:
        assert "preset" not in str(request.url)
        assert b"preset" not in request.content
