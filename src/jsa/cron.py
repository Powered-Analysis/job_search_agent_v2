"""The cloud machine's only entrypoint (PRD 01 "Scheduling & cadence"): gate, claim, run the day's searches."""

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import httpx

from jsa import db
from jsa.errors import JsaError, one_line
from jsa.profile import ScheduledSearch, SearchConfig, load_search_config
from jsa.search import Summary, search

log = logging.getLogger(__name__)


@dataclass
class CronRun:
    skipped: str | None = None
    results: list[tuple[Summary, list[str]]] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def gate(config: SearchConfig, now: datetime) -> list[ScheduledSearch]:
    """The day's ordered searches when they are due, else none. Pure: no I/O (XC-9)."""
    local = now.astimezone(config.tz)
    if local.time() < config.run_at:
        return []
    return config.schedule.on(local.date())


def _next_scheduled(config: SearchConfig, today: date) -> list[ScheduledSearch]:
    for offset in range(7):
        searches = config.schedule.on(today + timedelta(days=offset))
        if searches:
            return searches
    return []


def _not_due_reason(config: SearchConfig, today: date) -> str:
    if not config.schedule.on(today):
        return "no search scheduled today"
    return f"before run_at ({config.run_at:%H:%M} {config.timezone})"


def _run_searches(
    client: httpx.Client,
    searches: list[ScheduledSearch],
    trigger: str,
    now: datetime,
) -> CronRun:
    run = CronRun()
    for scheduled in searches:
        try:
            run.results.append(
                search(
                    client,
                    scheduled.agent,
                    scheduled.window_hours,
                    trigger=trigger,
                    now=now,
                )
            )
        # Any failure, whatever its type, must not stop the day's later searches (PRD 01).
        except Exception as error:  # noqa: BLE001
            message = (
                f"{scheduled.agent} ({scheduled.window_hours}h): {one_line(error)}"
            )
            log.error("Search failed: %s", message)
            run.failures.append(message)
    return run


def cron(client: httpx.Client, *, ungated: bool) -> CronRun:
    config = load_search_config()
    now = datetime.now(UTC)
    today = now.astimezone(config.tz).date()
    if ungated:
        # The smoke test skips the time gate and the claim, so it never uses up the day's run.
        searches = _next_scheduled(config, today)
        if not searches:
            raise JsaError("search.toml schedules no search on any weekday")
        return _run_searches(client, searches, "smoke", now)
    searches = gate(config, now)
    if not searches:
        return CronRun(skipped=_not_due_reason(config, today))
    conn = db.connect()
    claimed = db.claim_cron_day(conn, today.isoformat())
    conn.close()
    if not claimed:
        return CronRun(skipped="already ran today")
    return _run_searches(client, searches, "scheduled", now)
