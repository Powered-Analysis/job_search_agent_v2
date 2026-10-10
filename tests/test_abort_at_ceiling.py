"""A stream blocked in a read has its connection dropped at the ceiling (issue #119; PRD 01 "Timeouts/limits").

The provider keeps working, and billing, until the connection ends, so ending the app's wait must end the connection too.
"""

import socket
import threading
import time
from contextlib import suppress

import httpx
import pytest

from jsa.http import SseEvents
from jsa.runners import Deadline, WallClockExceeded, within

CEILING = 0.4
# "Within a few seconds of the ceiling" (issue #119), with room for a loaded CI machine.
FEW_SECONDS = 5
URL = "https://stream.example/events"


class AbortableSource:
    """An iterator that serves `items`, then blocks until released, and notes whether it was aborted or closed."""

    def __init__(self, items=(), *, blocks=True):
        self.items = list(items)
        self.blocks = blocks
        self.release = threading.Event()
        self.aborted = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self.items:
            return self.items.pop(0)
        if self.blocks:
            # Capped, so a regression fails the test instead of hanging it.
            self.release.wait(30)
        raise StopIteration

    def abort(self):
        self.aborted.set()

    def close(self):
        self.closed.set()


@pytest.fixture
def source():
    made = []

    def make(*args, **kwargs):
        made.append(AbortableSource(*args, **kwargs))
        return made[-1]

    yield make
    for each in made:
        each.release.set()


def test_a_source_blocked_in_a_read_is_aborted_within_a_few_seconds_of_the_ceiling(
    source,
):
    stalled = source()
    started = time.monotonic()
    with pytest.raises(WallClockExceeded):
        list(within(stalled, Deadline(CEILING)))
    assert stalled.aborted.wait(FEW_SECONDS)
    assert time.monotonic() - started < CEILING + FEW_SECONDS
    # Aborted while its read was still blocked, not once the read returned.
    assert not stalled.release.is_set()


def test_a_source_that_goes_silent_after_events_is_aborted_at_the_ceiling(source):
    stalled = source(["a", "b"])
    events = within(stalled, Deadline(CEILING))
    assert [next(events), next(events)] == ["a", "b"]
    with pytest.raises(WallClockExceeded):
        next(events)
    assert stalled.aborted.wait(FEW_SECONDS)


def test_a_source_that_finishes_in_time_is_not_aborted(source):
    finished = source(["a", "b"], blocks=False)
    assert list(within(finished, Deadline())) == ["a", "b"]
    assert finished.closed.is_set()
    assert not finished.aborted.is_set()


def test_a_source_that_fails_on_its_own_is_not_aborted(source):
    class Failing(AbortableSource):
        def __next__(self):
            raise ValueError("the connection fell over")

    failing = Failing()
    with pytest.raises(ValueError):
        list(within(failing, Deadline()))
    assert failing.closed.is_set()
    assert not failing.aborted.is_set()


def test_a_source_without_an_abort_still_raises_at_the_ceiling():
    release = threading.Event()

    class Plain:
        def __iter__(self):
            return self

        def __next__(self):
            release.wait(30)
            raise StopIteration

    try:
        started = time.monotonic()
        with pytest.raises(WallClockExceeded):
            list(within(Plain(), Deadline(CEILING)))
        assert time.monotonic() - started < CEILING + FEW_SECONDS
    finally:
        release.set()


# --- SseEvents over an injected transport -----------------------------------------


class Body(httpx.SyncByteStream):
    """A response body that serves some chunks, then blocks until released, and notes when it was closed."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.release = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        yield from self.chunks
        self.release.wait(30)

    def close(self):
        self.closed.set()


@pytest.fixture
def stream():
    bodies = []

    def serve(chunks=(), *, status=200):
        bodies.append(Body(chunks))
        client = httpx.Client(
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    status,
                    stream=bodies[-1],
                    headers={"content-type": "text/event-stream"},
                )
            )
        )
        events = SseEvents(client, URL, {"q": 1}, headers={}, read_timeout=30)
        return events, bodies[-1]

    yield serve
    for body in bodies:
        body.release.set()


def read_in_thread(events, into):
    def read():
        # An aborted read may end in a transport error; the tests look at what was read, not how it ended.
        with suppress(Exception):
            into.extend(events)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    return reader


def test_sse_events_yield_each_event_with_its_name_and_json_data(stream):
    events, body = stream([b'event: a\ndata: {"n": 1}\n\nevent: b\ndata: {"n": 2}\n\n'])
    body.release.set()
    assert list(events) == [("a", {"n": 1}), ("b", {"n": 2})]


def test_sse_events_has_an_abort_that_the_ceiling_can_call(stream):
    events, _body = stream()
    assert callable(events.abort)
    assert callable(events.close)


def test_aborting_before_any_read_is_harmless(stream):
    events, _body = stream()
    events.abort()
    events.close()


def test_aborting_a_stream_blocked_in_a_read_closes_its_response_while_the_read_is_blocked(
    stream,
):
    events, body = stream([b'event: a\ndata: {"n": 1}\n\n'])
    got = []
    reader = read_in_thread(events, got)
    deadline = time.monotonic() + FEW_SECONDS
    while not got and time.monotonic() < deadline:
        time.sleep(0.01)
    assert got == [("a", {"n": 1})]
    events.abort()
    assert body.closed.wait(FEW_SECONDS)
    assert not body.release.is_set()
    body.release.set()
    reader.join(FEW_SECONDS)


def test_a_stalled_perplexity_style_read_is_stopped_at_the_ceiling_by_within(stream):
    events, body = stream([b'event: a\ndata: {"n": 1}\n\n'])
    started = time.monotonic()
    stopped = within(events, Deadline(CEILING))
    assert next(stopped) == ("a", {"n": 1})
    with pytest.raises(WallClockExceeded):
        next(stopped)
    assert body.closed.wait(FEW_SECONDS)
    assert not body.release.is_set()
    assert time.monotonic() - started < CEILING + FEW_SECONDS


def test_an_error_response_is_still_raised_with_its_status(stream):
    events, body = stream([b"nope"], status=500)
    body.release.set()
    with pytest.raises(httpx.HTTPStatusError):
        list(events)


# --- the socket is shut down, not only the response closed ------------------------


class SocketStream(httpx.SyncByteStream):
    """A response body whose read blocks in `recv` on a socket, as a stalled connection does."""

    def __init__(self, reader):
        self.reader = reader
        self.closed = threading.Event()

    def __iter__(self):
        yield b'event: a\ndata: {"n": 1}\n\n'
        # Capped, so a regression fails the test instead of hanging it.
        self.reader.settimeout(30)
        while self.reader.recv(1024):
            pass

    def close(self):
        # As with a real connection, closing the response does not wake a read that is blocked.
        self.closed.set()


class SocketBacked(httpx.BaseTransport):
    """A transport whose responses expose their connection's socket the way httpcore does."""

    def __init__(self, reader):
        self.reader = reader

    def handle_request(self, request):
        reader = self.reader

        class Network:
            def get_extra_info(self, name):
                return reader if name == "socket" else None

        return httpx.Response(
            200,
            stream=SocketStream(reader),
            headers={"content-type": "text/event-stream"},
            extensions={"network_stream": Network()},
        )


def test_aborting_wakes_a_read_blocked_on_the_connections_socket():
    reader, writer = socket.socketpair()
    with reader, writer, httpx.Client(transport=SocketBacked(reader)) as client:
        events = SseEvents(client, URL, {}, headers={}, read_timeout=30)
        got = []
        done = read_in_thread(events, got)
        deadline = time.monotonic() + FEW_SECONDS
        while not got and time.monotonic() < deadline:
            time.sleep(0.01)
        assert got == [("a", {"n": 1})]
        assert done.is_alive()
        started = time.monotonic()
        events.abort()
        done.join(FEW_SECONDS)
        assert not done.is_alive()
        assert time.monotonic() - started < FEW_SECONDS
