"""Review's search health line (PRD 03): what the unattended search did, read from the run log and the claim table."""

import json
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from jsa import db
from jsa.errors import JsaError
from jsa.profile import SearchConfig, load_search_config

LOOKBACK = timedelta(days=7)
# The runner's wall-clock ceiling is an hour (PRD 01), so a search silent past this died.
DEAD_AFTER = timedelta(minutes=90)


class Run(NamedTuple):
    """A `search_runs` row, in the column order of `db.health_search_runs`."""

    id: int
    run_date: str
    trigger: str
    agent: str
    started_at: str
    outcome: str | None
    error: str | None
    summary: str | None
    warnings: str | None


def _started(run: Run) -> datetime:
    return datetime.fromisoformat(run.started_at)


def _is_dead(run: Run, now: datetime) -> bool:
    return run.outcome is None and now - _started(run) > DEAD_AFTER


def _latest(run: Run, now: datetime) -> str:
    status = run.outcome or ("dead" if _is_dead(run, now) else "running")
    text = f"{run.agent} {run.run_date} {status}"
    if run.outcome == "ok":
        counts = json.loads(run.summary) if run.summary else {}
        cost = counts.get("cost")
        text += f", {counts.get('inserted', 0)} inserted, "
        text += "cost unknown" if cost is None else f"${cost:.2f}"
    return text


def health_lines(
    config: SearchConfig, runs: list[Run], claimed_days: set[str], now: datetime
) -> list[str]:
    """The health line: the latest run per scheduled agent, then one line per problem. Pure (XC-9)."""
    agents = dict.fromkeys(search.agent for search in config.schedule.searches())
    latest = {run.agent: run for run in runs}
    summary = [
        _latest(latest[agent], now) if agent in latest else f"{agent} has not run"
        for agent in agents
    ]
    lines = ["Search health: " + ("; ".join(summary) or "no searches scheduled")]
    since = now - LOOKBACK
    for run in runs:
        label = f"{run.agent} search of {run.run_date}"
        if _is_dead(run, now):
            lines.append(f"  dead: {label} started {run.started_at} and has no outcome")
        if _started(run) < since:
            continue
        if run.outcome == "failed":
            lines.append(f"  failed: {label}: {run.error}")
        lines.extend(
            f"  warning: {label}: {warning}"
            for warning in (run.warnings or "").splitlines()
        )
    today = now.astimezone(config.tz).date()
    for days_back in range(LOOKBACK.days, 0, -1):
        day = today - timedelta(days=days_back)
        if config.schedule.on(day) and day.isoformat() not in claimed_days:
            lines.append(f"  missed: no scheduled search ran on {day:%A} {day}")
    return lines


def search_health(conn: db.Connection, now: datetime | None = None) -> list[str]:
    """The health line for the live database; an unreadable `search.toml` is reported, not raised."""
    now = now or datetime.now(UTC)
    try:
        config = load_search_config()
    except JsaError as error:
        return [f"Search health: not available. {error}"]
    runs = [Run(*row) for row in db.health_search_runs(conn, now - LOOKBACK)]
    return health_lines(config, runs, db.cron_run_dates(conn), now)
