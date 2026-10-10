"""What every search runner shares (PRD 01): its result and the wall-clock ceiling."""

import queue
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass

from jsa.errors import JsaError

# A hard stop, so a hung run can neither bill indefinitely nor block the day's later searches.
WALL_CLOCK_CEILING_SECONDS = 3600
# The longest a streamed runner waits for its connection to send anything, so a half-open one
# surfaces as a transport error. Each request is also capped by the time left on the ceiling,
# and `within` abandons a read still blocked when the ceiling passes, aborting its connection.
READ_TIMEOUT_SECONDS = 1800
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
