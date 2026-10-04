"""What every search runner shares (PRD 01): its result and the wall-clock ceiling."""

import time
from collections.abc import Callable
from dataclasses import dataclass

from jsa.errors import JsaError

# A hard stop, so a hung run can neither bill indefinitely nor block the day's later searches.
WALL_CLOCK_CEILING_SECONDS = 3600
# How often a streamed runner logs that it is still working.
HEARTBEAT_SECONDS = 5


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

    def check(self) -> None:
        if self.elapsed > self._seconds:
            raise WallClockExceeded(
                f"the search ran past its {self._seconds:.0f}-second ceiling"
            )

    def heartbeat_due(self) -> bool:
        """True at most once per heartbeat interval, so the caller logs when it returns True."""
        if self.elapsed - self._last_heartbeat < HEARTBEAT_SECONDS:
            return False
        self._last_heartbeat = self.elapsed
        return True
