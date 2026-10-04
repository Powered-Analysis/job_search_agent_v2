"""One search-and-capture cycle (PRD 01): run a runner, verify and record every posting, insert and capture the admitted."""

import json
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx

from jsa import db
from jsa.ats import resolve_ats
from jsa.capture import CaptureError, capture_posting
from jsa.claude import ClaudeRunner
from jsa.perplexity import PerplexityRunner
from jsa.profile import SearchConfig, claude_settings, load_search_config
from jsa.runners import RunnerResult
from jsa.search_output import parse_search_output
from jsa.search_prompt import assemble_search_prompt
from jsa.urls import canonicalize_url
from jsa.verify import ADMITTED_OUTCOMES, Verifier

log = logging.getLogger(__name__)


class Runner(Protocol):
    def run(self, prompt: str) -> RunnerResult: ...


def _claude_runner(client: httpx.Client, config: SearchConfig) -> Runner:
    return ClaudeRunner(claude_settings(config))


# The agents whose runner exists; the CLI offers exactly these.
RUNNERS: dict[str, Callable[[httpx.Client, SearchConfig], Runner]] = {
    "perplexity": lambda client, config: PerplexityRunner(client),
    "claude": _claude_runner,
}


@dataclass
class Summary:
    """The run summary, in the order PRD 01 lists it."""

    agent: str
    model: str | None = None
    effort: str | None = None
    mode: str = ""
    cost: float | None = None
    found: int = 0
    verified: int = 0
    verified_no_date: int = 0
    reachable: int = 0
    reachable_no_date: int = 0
    aggregator: int = 0
    unsupported: int = 0
    unverifiable: int = 0
    not_on_index: int = 0
    page_closed: int = 0
    out_of_window: int = 0
    malformed: int = 0
    inserted: int = 0
    already_present: int = 0
    jd_captured: int = 0
    fetch_failed: int = 0
    errors: list[str] = field(default_factory=list)


def _warnings(summary: Summary, no_date_platforms: Counter[str]) -> list[str]:
    warnings = [
        f"verified_no_date: {platform} returned no posted date for {count} "
        f"{'posting' if count == 1 else 'postings'}, so their recency was not checked"
        for platform, count in no_date_platforms.items()
    ]
    if summary.reachable_no_date:
        warnings.append(
            f"reachable_no_date: {summary.reachable_no_date} postings' pages publish no "
            "posted date, so their recency rests on the agent's claim"
        )
    if summary.malformed:
        warnings.append(
            f"malformed: {summary.malformed} postings broke the output contract and were dropped"
        )
    return warnings


def _process(
    conn: db.Connection,
    client: httpx.Client,
    config: SearchConfig,
    agent: str,
    window_hours: int,
    now: datetime,
    result: RunnerResult,
) -> tuple[Summary, list[str]]:
    summary = Summary(
        agent, result.model, result.effort, config.verification.mode, result.cost
    )
    parsed = parse_search_output(result.text)
    summary.malformed = parsed.malformed
    run_date = now.astimezone(config.tz).date().isoformat()
    window_start = now - timedelta(hours=window_hours)
    verifier = Verifier(client)
    no_date_platforms: Counter[str] = Counter()
    seen: set[str] = set()
    for rank, posting in enumerate(parsed.postings, start=1):
        canonical = canonicalize_url(posting.url)
        if canonical in seen:
            continue
        seen.add(canonical)
        summary.found += 1
        checked = verifier.check(
            posting.url, mode=config.verification.mode, window_start=window_start
        )
        verdict = checked.verdict
        setattr(summary, verdict.outcome, getattr(summary, verdict.outcome) + 1)
        db.record_finding(
            conn,
            run_date=run_date,
            agent=agent,
            canonical_url=canonical,
            window_hours=window_hours,
            rank=rank,
            verification=verdict.outcome,
            ats_date=verdict.ats_date,
            ats_date_kind=verdict.ats_date_kind,
            model=result.model,
            effort=result.effort,
        )
        if verdict.outcome == "verified_no_date":
            no_date_platforms[resolve_ats(posting.url).platform] += 1
        if verdict.outcome not in ADMITTED_OUTCOMES:
            continue
        posting_id = db.insert_posting(
            conn,
            company=posting.company,
            title=posting.title,
            url=posting.url,
            search_agent=agent,
            date_posted=posting.date_posted,
        )
        if posting_id is None:
            summary.already_present += 1
            continue
        summary.inserted += 1
        ref = resolve_ats(posting.url)
        try:
            captured = capture_posting(
                client,
                posting.url,
                ref,
                known=checked.record,
                page_html=checked.page.html if checked.page else None,
            )
        except CaptureError as error:
            # XC-6: a failed capture never excludes the row.
            summary.fetch_failed += 1
            summary.errors.append(f"{posting.url}: {error}")
            log.warning("Capture failed for %s: %s", posting.url, error)
            continue
        db.capture_jd(
            conn,
            posting_id,
            jd_markdown=captured.jd_markdown,
            location=captured.location,
            # The ATS's own title replaces the agent's transcription (PRD 01).
            title=captured.title if ref else None,
        )
        summary.jd_captured += 1
    return summary, _warnings(summary, no_date_platforms)


def _one_line(error: Exception) -> str:
    return (str(error).splitlines() or [""])[0] or type(error).__name__


def search(
    client: httpx.Client, agent: str, window_hours: int
) -> tuple[Summary, list[str]]:
    """Run one hand search; returns its summary and closing warnings."""
    config = load_search_config()
    now = datetime.now(UTC)
    # Everything that can fail on setup fails here, before a model call or a run row.
    prompt = assemble_search_prompt(config, window_hours, now)
    runner = RUNNERS[agent](client, config)
    conn = db.connect()
    run_id = db.open_search_run(
        conn,
        trigger="hand",
        run_date=now.astimezone(config.tz).date().isoformat(),
        agent=agent,
        window_hours=window_hours,
        mode=config.verification.mode,
    )
    conn.close()
    result: RunnerResult | None = None
    try:
        result = runner.run(prompt)
        # The runner takes minutes, long enough for the server to drop an idle connection.
        conn = db.connect()
        summary, warnings = _process(
            conn, client, config, agent, window_hours, now, result
        )
    except Exception as error:
        conn = db.connect()
        db.close_search_run(
            conn,
            run_id,
            outcome="failed",
            error=_one_line(error),
            summary=None,
            warnings=None,
            model=result.model if result else None,
            effort=result.effort if result else None,
        )
        raise
    counts = json.dumps(asdict(summary))
    log.info("search summary: %s", counts)
    db.close_search_run(
        conn,
        run_id,
        outcome="ok",
        error=None,
        summary=counts,
        warnings="\n".join(warnings) or None,
        model=summary.model,
        effort=summary.effort,
    )
    return summary, warnings
