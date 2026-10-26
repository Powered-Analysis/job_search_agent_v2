"""What every search runner shares (PRD 01): its result, the wall-clock ceiling, and the driver for provider-side background runs."""

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import Protocol

from jsa.errors import JsaError

log = logging.getLogger(__name__)

# A hard stop, so a hung run can neither bill indefinitely nor block the day's later searches.
WALL_CLOCK_CEILING_SECONDS = 3600
# The longest a streamed runner waits for its connection to send anything, so a half-open one
# surfaces as a transport error. Each request is also capped by the time left on the ceiling,
# and `within` abandons a read still blocked when the ceiling passes, aborting its connection.
READ_TIMEOUT_SECONDS = 1800
# How long a runner waits between status checks once streaming has stopped helping.
POLL_SECONDS = 10
# The cancel is sent after the run's own deadline may have passed, so it has its own short one.
CANCEL_TIMEOUT_SECONDS = 30
# How often a streamed runner logs that it is still working.
HEARTBEAT_SECONDS = 5


# The most of one narration or tool line a live trace prints.
_CLIP = 300


def clip(text: str) -> str:
    """One trace line: whitespace collapsed, long text cut short."""
    text = " ".join(text.split())
    return text if len(text) <= _CLIP else text[:_CLIP] + "…"


class RunnerError(JsaError):
    """A runner could not produce a final answer."""


class WallClockExceeded(RunnerError):
    """The runner ran past the ceiling; nothing from that search is inserted."""


@dataclass(frozen=True)
class RunnerResult:
    text: str
    # What actually ran, for the findings telemetry (XC-14); `effort` is None where the runner has none.
    model: str | None
    effort: str | None
    cost: float | None
    # True where the runner could only estimate the USD from token counts.
    cost_is_estimate: bool = False


class Deadline:
    def __init__(
        self,
        seconds: float = WALL_CLOCK_CEILING_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._clock = clock
        self._start = clock()
        self._seconds = seconds
        self._last_heartbeat = 0.0

    @property
    def elapsed(self) -> float:
        return self._clock() - self._start

    def exceeded(self) -> WallClockExceeded:
        return WallClockExceeded(
            f"the search ran past its {self._seconds:.0f}-second ceiling"
        )

    def check(self) -> None:
        if self.elapsed > self._seconds:
            raise self.exceeded()

    def remaining(self) -> float:
        """Seconds left on the ceiling; raises once it has passed."""
        left = self._seconds - self.elapsed
        if left < 0:
            raise self.exceeded()
        return left

    def request_timeout(self) -> float:
        """How long one request may wait on the connection: the read timeout, cut to the time left, so a stall cannot outlast the ceiling."""
        return min(READ_TIMEOUT_SECONDS, self.remaining())

    def heartbeat_due(self) -> bool:
        """True at most once per heartbeat interval, so the caller logs when it returns True."""
        if self.elapsed - self._last_heartbeat < HEARTBEAT_SECONDS:
            return False
        self._last_heartbeat = self.elapsed
        return True


def within[T](events: Iterable[T], deadline: Deadline) -> Iterator[T]:
    """Yield `events`, raising WallClockExceeded the moment the ceiling passes, even while the source is blocked in a read.

    A stalled stream sends nothing, so the ceiling cannot be checked between its events. The source is read in a
    worker thread that the caller waits on only until the ceiling. If the read never returns, the thread is
    abandoned (it is a daemon) and the source's `abort`, where it has one, drops the connection at once so the
    provider stops working; the source is closed once that read ends.
    """
    source = iter(events)
    pulls: queue.SimpleQueue[bool] = queue.SimpleQueue()
    results: queue.SimpleQueue[tuple[bool, object]] = queue.SimpleQueue()

    def serve() -> None:
        # True asks for the next event; False closes the source and ends the thread.
        while pulls.get():
            try:
                results.put((True, next(source)))
            except StopIteration:
                results.put((False, None))
            except BaseException as error:  # noqa: BLE001  # handed to the caller, who re-raises it
                results.put((False, error))
        if close := getattr(source, "close", None):
            close()

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    reading = False
    try:
        while True:
            timeout = deadline.remaining()
            pulls.put(True)
            reading = True
            try:
                more, value = results.get(timeout=timeout)
            except queue.Empty:
                raise deadline.exceeded() from None
            reading = False
            if more:
                yield value
            elif value is None:
                return
            else:
                raise value
    finally:
        pulls.put(False)
        if reading:
            if abort := getattr(source, "abort", None):
                abort()
        else:
            # Idle, so the source is closed before the caller goes on (a stream left open keeps the provider working).
            worker.join()


class ReconnectWindowClosed(RunnerError):
    """The provider no longer lets a dropped stream resume, so the run can only be polled."""


class RunState(Protocol):
    """What the background-run driver reads from a runner's folded stream state."""

    @property
    def run_id(self) -> str | None: ...

    @property
    def cursor(self) -> object | None:
        """Where a reconnect resumes; None until the stream has sent anything resumable."""
        ...

    @property
    def ended(self) -> bool: ...


class BackgroundRunner:
    """The reconnect, poll, and cancel-on-exit policy of a run that continues at the provider when its connection drops (PRD 01).

    A runner supplies the provider's four operations: `_open` starts the run and reads its stream, `_reconnect`
    resumes a dropped stream, `_poll` reads the run's status, and `_cancel` stops it. Each of the first three
    returns the state it has reached, and a dropped connection is not an error in any of them.
    """

    # How logs and errors name the provider and its unit of work, e.g. "Perplexity" and "response".
    provider: str
    unit: str

    def __init__(self, sleep: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleep
        # The latest state, kept here so a run that raises still knows which run to cancel.
        self._latest: RunState | None = None
        self._reconnects = 0

    def _research(self, prompt: str) -> RunnerResult:
        raise NotImplementedError

    def _open(self, prompt: str, deadline: Deadline) -> RunState:
        raise NotImplementedError

    def _reconnect(self, state: RunState, deadline: Deadline) -> RunState:
        """Resume after `state.cursor`; raises ReconnectWindowClosed once the provider refuses."""
        raise NotImplementedError

    def _poll(self, state: RunState, deadline: Deadline) -> RunState:
        raise NotImplementedError

    def _cancel(self, run_id: str) -> None:
        raise NotImplementedError

    def run(self, prompt: str) -> RunnerResult:
        self._latest = None
        self._reconnects = 0
        try:
            return self._research(prompt)
        except BaseException:
            # A background run outlives the connection: left alone it keeps working and billing (PRD 01).
            self._cancel_unfinished()
            raise

    def _drive(self, prompt: str, deadline: Deadline) -> RunState:
        """Open the run and carry it to the provider's own end, reconnecting or polling as the connection allows."""
        state = self._open(prompt, deadline)
        # Once the reconnect window has passed the run is polled instead.
        window_open = True
        while not state.ended:
            deadline.check()
            if state.run_id is None:
                raise RunnerError(
                    f"the {self.provider} stream dropped before the {self.unit} began"
                )
            before = state.cursor
            if window_open and before is not None:
                log.info("stream dropped; reconnecting after event %s", before)
                self._reconnects += 1
                try:
                    state = self._reconnect(state, deadline)
                except ReconnectWindowClosed:
                    log.info("reconnect window has passed; polling the run")
                    window_open = False
            if not state.ended and (not window_open or state.cursor == before):
                # A reconnect that gains nothing, or none left: ask the status instead of spinning on it.
                state = self._poll(state, deadline)
                if not state.ended:
                    self._sleep(POLL_SECONDS)
        return state

    def _track(self, state):
        if state.run_id and not (self._latest and self._latest.run_id):
            # Logged at once, so a run that dies uncancelled can still be found and cancelled by hand.
            log.info("%s %s %s", self.provider.lower(), self.unit, state.run_id)
        self._latest = state
        return state

    def _cancel_unfinished(self) -> None:
        latest = self._latest
        if latest is None or latest.run_id is None or latest.ended:
            return
        try:
            self._cancel(latest.run_id)
        except Exception:
            # Never let a failed cancel hide the error that ended the run.
            log.exception(
                "Could not cancel %s %s %s, so it may still be running",
                self.provider,
                self.unit,
                latest.run_id,
            )
        else:
            log.info("cancelled %s %s %s", self.provider, self.unit, latest.run_id)
