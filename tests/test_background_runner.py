"""The one background-run driver both provider runners share (issue #189; PRD 01 "Runners", "Edge cases", "Timeouts/limits"; convention 1).

The driver's reconnect, poll, and cancel-on-exit policy is exercised through a scripted runner that supplies
only the four provider operations, so the policy is asserted once, apart from either provider.
"""

import logging
from dataclasses import dataclass

import pytest

from jsa import gemini, perplexity, runners
from jsa.runners import (
    POLL_SECONDS,
    BackgroundRunner,
    Deadline,
    ReconnectWindowClosed,
    RunnerError,
    RunnerResult,
    WallClockExceeded,
)

POLICY = ("run", "_drive", "_track", "_cancel_unfinished")
OPERATIONS = ("_open", "_reconnect", "_poll", "_cancel")


@dataclass(frozen=True)
class State:
    run_id: str | None = None
    cursor: int | None = None
    ended: bool = False


class Scripted(BackgroundRunner):
    """Serves one scripted state per call of each operation and records the calls made."""

    provider = "Acme"
    unit = "job"

    def __init__(self, *, opened, reconnects=(), polls=(), cancel_error=None):
        self.slept = []
        super().__init__(self.slept.append)
        self.opened = opened
        self.reconnects = list(reconnects)
        self.polls = list(polls)
        self.cancel_error = cancel_error
        self.calls = []
        self.cancelled = []
        self.deadline = Deadline()

    def _research(self, prompt):
        state = self._drive(prompt, self.deadline)
        return RunnerResult(f"done {state.run_id}", None, None, None)

    def _open(self, prompt, deadline):
        self.calls.append("open")
        return self._track(self.opened)

    def _reconnect(self, state, deadline):
        self.calls.append("reconnect")
        step = self.reconnects.pop(0)
        if isinstance(step, BaseException):
            raise step
        return self._track(step)

    def _poll(self, state, deadline):
        self.calls.append("poll")
        step = self.polls.pop(0)
        if isinstance(step, BaseException):
            raise step
        return self._track(step)

    def _cancel(self, run_id):
        self.cancelled.append(run_id)
        if self.cancel_error:
            raise self.cancel_error


# --- one definition, two users -----------------------------------------------------------


@pytest.mark.parametrize(
    "runner", [perplexity.PerplexityRunner, gemini.GeminiAgentRunner]
)
def test_each_provider_runner_is_a_background_runner(runner):
    assert issubclass(runner, BackgroundRunner)


@pytest.mark.parametrize(
    "runner", [perplexity.PerplexityRunner, gemini.GeminiAgentRunner]
)
@pytest.mark.parametrize("name", POLICY)
def test_neither_provider_runner_redefines_the_policy(runner, name):
    assert name not in vars(runner)
    assert getattr(runner, name) is getattr(BackgroundRunner, name)


@pytest.mark.parametrize(
    "runner", [perplexity.PerplexityRunner, gemini.GeminiAgentRunner]
)
@pytest.mark.parametrize("name", OPERATIONS)
def test_each_provider_runner_supplies_the_four_provider_operations(runner, name):
    assert name in vars(runner)


def test_the_provider_runners_name_their_provider_and_unit():
    assert (perplexity.PerplexityRunner.provider, perplexity.PerplexityRunner.unit) == (
        "Perplexity",
        "response",
    )
    assert gemini.GeminiAgentRunner.provider == "Gemini"
    assert gemini.GeminiAgentRunner.unit


def test_the_reconnect_window_closing_is_a_runner_error():
    assert issubclass(ReconnectWindowClosed, RunnerError)
    assert runners.ReconnectWindowClosed is ReconnectWindowClosed


@pytest.mark.parametrize(
    ("state", "run_id", "cursor"),
    [
        (perplexity.StreamState(), None, None),
        (perplexity.StreamState(response_id="r1", sequence_number=7), "r1", 7),
        (gemini.StreamState(), None, None),
        (gemini.StreamState(interaction_id="i1", last_event_id="e9"), "i1", "e9"),
    ],
)
def test_each_stream_state_reads_a_run_id_and_a_cursor_alike(state, run_id, cursor):
    assert state.run_id == run_id
    assert state.cursor == cursor


def test_a_stream_state_that_ended_reports_it_to_the_driver():
    assert perplexity.StreamState(status="completed").ended
    assert not perplexity.StreamState(status="in_progress").ended
    assert gemini.StreamState(status="completed").ended
    assert not gemini.StreamState(status="in_progress").ended


# --- the shared policy -----------------------------------------------------------------


def test_a_run_that_ends_on_the_first_stream_is_not_reconnected_or_polled():
    runner = Scripted(opened=State("a", 1, ended=True))
    assert runner.run("p").text == "done a"
    assert runner.calls == ["open"]
    assert runner.slept == []
    assert runner.cancelled == []


def test_a_dropped_stream_reconnects_until_the_run_ends():
    runner = Scripted(
        opened=State("a", 1),
        reconnects=[State("a", 5), State("a", 9, ended=True)],
    )
    assert runner.run("p").text == "done a"
    assert runner.calls == ["open", "reconnect", "reconnect"]
    assert runner.slept == []


def test_a_reconnect_that_gains_nothing_polls_and_waits_before_the_next_try():
    runner = Scripted(
        opened=State("a", 1),
        reconnects=[State("a", 1), State("a", 4, ended=True)],
        polls=[State("a", 1)],
    )
    runner.run("p")
    assert runner.calls == ["open", "reconnect", "poll", "reconnect"]
    assert runner.slept == [POLL_SECONDS]


def test_a_stream_with_no_cursor_is_polled_not_reconnected():
    runner = Scripted(
        opened=State("a", None), polls=[State("a", None), State("a", None, ended=True)]
    )
    runner.run("p")
    assert runner.calls == ["open", "poll", "poll"]
    assert runner.slept == [POLL_SECONDS]


def test_a_closed_reconnect_window_switches_to_polling_for_good(caplog):
    runner = Scripted(
        opened=State("a", 1),
        reconnects=[ReconnectWindowClosed()],
        polls=[State("a", 3), State("a", 3), State("a", 8, ended=True)],
    )
    with caplog.at_level(logging.INFO):
        runner.run("p")
    assert runner.calls == ["open", "reconnect", "poll", "poll", "poll"]
    assert runner.slept == [POLL_SECONDS, POLL_SECONDS]
    assert "reconnect window has passed" in caplog.text


def test_a_poll_that_finds_the_run_ended_does_not_sleep():
    runner = Scripted(
        opened=State("a", 1),
        reconnects=[ReconnectWindowClosed()],
        polls=[State("a", 3, ended=True)],
    )
    runner.run("p")
    assert runner.slept == []


def test_a_stream_that_dropped_before_the_run_began_is_an_error_naming_the_unit():
    runner = Scripted(opened=State(None, None))
    with pytest.raises(RunnerError, match="Acme stream dropped before the job began"):
        runner.run("p")
    assert runner.calls == ["open"]
    assert runner.cancelled == []


def test_a_reconnect_is_logged_with_where_it_resumes(caplog):
    runner = Scripted(opened=State("a", 41), reconnects=[State("a", 50, ended=True)])
    with caplog.at_level(logging.INFO):
        runner.run("p")
    assert "reconnecting after event 41" in caplog.text


def test_the_run_id_is_logged_once_as_soon_as_it_is_known(caplog):
    runner = Scripted(
        opened=State("a", 1), reconnects=[State("a", 2), State("a", 3, ended=True)]
    )
    with caplog.at_level(logging.INFO):
        runner.run("p")
    assert caplog.text.count("acme job a") == 1


def test_the_ceiling_stops_the_drive_before_another_reconnect():
    runner = Scripted(opened=State("a", 1), reconnects=[State("a", 2)])
    runner.deadline = Deadline(seconds=-1)
    with pytest.raises(WallClockExceeded):
        runner.run("p")
    assert runner.calls == ["open"]


# --- cancel-on-exit --------------------------------------------------------------------


def test_a_run_that_fails_midway_cancels_its_unfinished_run_and_still_raises():
    runner = Scripted(opened=State("a", 1), reconnects=[RunnerError("boom")])
    with pytest.raises(RunnerError, match="boom"):
        runner.run("p")
    assert runner.cancelled == ["a"]


def test_the_ceiling_passing_cancels_the_unfinished_run():
    runner = Scripted(opened=State("a", 1))
    runner.deadline = Deadline(seconds=-1)
    with pytest.raises(WallClockExceeded):
        runner.run("p")
    assert runner.cancelled == ["a"]


def test_an_interrupt_cancels_the_unfinished_run():
    runner = Scripted(opened=State("a", 1), reconnects=[KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        runner.run("p")
    assert runner.cancelled == ["a"]


def test_a_run_that_already_ended_is_not_cancelled_when_a_later_step_fails():
    class FailsAfterEnd(Scripted):
        def _research(self, prompt):
            self._drive(prompt, self.deadline)
            raise RunnerError("no final answer")

    runner = FailsAfterEnd(opened=State("a", 1, ended=True))
    with pytest.raises(RunnerError, match="no final answer"):
        runner.run("p")
    assert runner.cancelled == []


def test_a_run_that_succeeds_is_never_cancelled():
    runner = Scripted(opened=State("a", 1), reconnects=[State("a", 2, ended=True)])
    runner.run("p")
    assert runner.cancelled == []


def test_a_failed_cancel_is_logged_and_never_hides_the_error_that_ended_the_run(caplog):
    runner = Scripted(
        opened=State("a", 1),
        reconnects=[RunnerError("the real failure")],
        cancel_error=ConnectionError("cancel refused"),
    )
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(RunnerError, match="the real failure"),
    ):
        runner.run("p")
    assert "Could not cancel Acme job a" in caplog.text


def test_a_run_with_no_run_id_has_nothing_to_cancel():
    runner = Scripted(opened=State(None, None))
    with pytest.raises(RunnerError):
        runner.run("p")
    assert runner.cancelled == []


def test_each_run_starts_clean_so_an_earlier_run_is_not_cancelled_by_a_later_failure():
    class Boom(Scripted):
        def _research(self, prompt):
            raise RunnerError("before anything opened")

    runner = Boom(opened=State("a", 1))
    runner._latest = State("old", 1)
    with pytest.raises(RunnerError):
        runner.run("p")
    assert runner.cancelled == []
