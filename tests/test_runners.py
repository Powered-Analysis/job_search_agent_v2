"""The shared wall-clock ceiling holds even when a stream goes silent (issue #30; PRD 01 "Timeouts/limits").

These tests use the real clock with a ceiling of a fraction of a second, because the behavior under test
is that a blocked read cannot outlast the ceiling.
"""

import threading
import time

import pytest

from jsa.runners import Deadline, WallClockExceeded, within

CEILING = 0.4
# "Within a few seconds of the ceiling" (issue #30), with room for a loaded CI machine.
FEW_SECONDS = 5


class Source:
    """An iterator that serves `items`, then blocks until released or ends, and notes whether it was closed."""

    def __init__(self, items=(), *, then="ends", error=None):
        self.items = list(items)
        self.then = then
        self.error = error
        self.release = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        if self.items:
            return self.items.pop(0)
        if self.error:
            raise self.error
        if self.then == "blocks":
            # Capped, so a regression fails the test instead of hanging it.
            self.release.wait(30)
        raise StopIteration

    def close(self):
        self.closed.set()


@pytest.fixture
def silent():
    sources = []

    def make(items=()):
        sources.append(Source(items, then="blocks"))
        return sources[-1]

    yield make
    for source in sources:
        source.release.set()


def test_within_yields_every_event_in_order_and_closes_the_source():
    source = Source(["a", "b", "c"])
    assert list(within(source, Deadline())) == ["a", "b", "c"]
    assert source.closed.is_set()


def test_within_an_empty_stream_yields_nothing():
    source = Source()
    assert list(within(source, Deadline())) == []
    assert source.closed.is_set()


def test_within_passes_the_sources_own_error_through_unchanged_and_closes_it():
    failure = ValueError("the connection fell over")
    source = Source(["a"], error=failure)
    events = within(source, Deadline())
    assert next(events) == "a"
    with pytest.raises(ValueError) as raised:
        next(events)
    assert raised.value is failure
    assert source.closed.is_set()


def test_within_closing_the_consumer_early_closes_the_source():
    source = Source(["a", "b", "c"])
    events = within(source, Deadline())
    assert next(events) == "a"
    events.close()
    assert source.closed.is_set()


def test_a_source_that_is_silent_from_the_start_raises_at_the_ceiling(silent):
    source = silent()
    started = time.monotonic()
    with pytest.raises(WallClockExceeded):
        list(within(source, Deadline(CEILING)))
    waited = time.monotonic() - started
    assert CEILING * 0.9 <= waited < CEILING + FEW_SECONDS


def test_a_source_that_goes_silent_after_some_events_raises_at_the_ceiling(silent):
    source = silent(["a", "b"])
    events = within(source, Deadline(CEILING))
    assert [next(events), next(events)] == ["a", "b"]
    started = time.monotonic()
    with pytest.raises(WallClockExceeded):
        next(events)
    assert time.monotonic() - started < CEILING + FEW_SECONDS


def test_a_source_that_keeps_talking_is_still_stopped_at_the_ceiling():
    def chatter():
        while True:
            time.sleep(0.05)
            yield "tick"

    started = time.monotonic()
    with pytest.raises(WallClockExceeded):
        for _event in within(chatter(), Deadline(CEILING)):
            pass
    assert time.monotonic() - started < CEILING + FEW_SECONDS


def test_the_blocked_source_is_closed_once_its_read_ends(silent):
    source = silent()
    with pytest.raises(WallClockExceeded):
        list(within(source, Deadline(CEILING)))
    source.release.set()
    assert source.closed.wait(FEW_SECONDS)


def test_a_slow_source_that_finishes_in_time_is_not_cut_short():
    def slow():
        for event in "abc":
            time.sleep(0.05)
            yield event

    assert list(within(slow(), Deadline(30))) == ["a", "b", "c"]
