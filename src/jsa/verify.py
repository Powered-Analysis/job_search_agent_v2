"""Liveness, recency, and age verification (PRD 01, XC-5): pure classification, the fetches around it, and the stored-posting re-check."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import unquote, urlsplit

import httpx

from jsa import db
from jsa.ats import AtsRef, ashby_board_url, resolve_ats, rippling_detail_url
from jsa.capture import extract_job_posting
from jsa.http import get_json, get_lever_json
from jsa.urls import is_aggregator

type Mode = Literal["strict", "best_effort"]
# A board's jobs: job id -> the platform's raw recency value (None where the list carries none).
type Index = Mapping[str, object]

# Absorbs timezone and clock skew between agent, ATS, and pipeline (PRD 01).
WINDOW_SLACK = timedelta(hours=24)
# A posting created longer ago than this is dropped however recently it was updated (PRD 01).
# The search prompt template states the same 60 days, because its slot table is fixed (XC-13).
AGE_CEILING = timedelta(days=60)

# Only a definite closed signal marks a stored posting closed (PRD 01).
CLOSED_OUTCOMES = frozenset({"not_on_index", "page_closed"})
# The outcomes whose postings are inserted (PRD 01).
ADMITTED_OUTCOMES = frozenset(
    {"verified", "verified_no_date", "reachable", "reachable_no_date"}
)

_ATS_DATE_KINDS = {
    "greenhouse": "updated",
    "lever": "created",
    "ashby": "published",
    "rippling": "created",
}
_PAGE_DATE_KIND = "published"
_CREATED_KIND = "created"
_GONE_STATUSES = (404, 410)


class UnexpectedShape(ValueError):
    """An ATS index answered with a shape this check can't read."""


@dataclass(frozen=True)
class Verdict:
    outcome: str
    ats_date: str | None = None
    ats_date_kind: str | None = None


@dataclass(frozen=True)
class Page:
    status: int
    final_url: str
    redirected: bool
    html: str


@dataclass(frozen=True)
class Checked:
    verdict: Verdict
    # Kept so capture can reuse them instead of fetching again (PRD 01).
    page: Page | None = None
    # The Ashby board or the Rippling detail the check fetched.
    record: Mapping | None = None


@dataclass(frozen=True)
class Fetched:
    """One board's index fetch."""

    index: Index
    # Job id -> raw creation value, for a platform whose recency value is not one.
    created: Index = field(default_factory=dict)
    # The Ashby board, which is also its capture source (PRD 01).
    record: object = None


def _parse_timestamp(value: object, *, epoch_ms: bool = False) -> datetime | None:
    try:
        if epoch_ms and isinstance(value, int | float) and not isinstance(value, bool):
            return datetime.fromtimestamp(value / 1000, UTC)
        if isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip())
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError, OverflowError, OSError:
        pass
    return None


def _needs_detail(ref: AtsRef, window_start: datetime | None) -> bool:
    # Rippling's list carries no timestamp, so only the window test needs the detail record.
    return ref.platform == "rippling" and window_start is not None


def _judge(
    raw: object,
    kind: str,
    *,
    epoch_ms: bool = False,
    created: object,
    window_start: datetime | None,
    now: datetime,
    open_outcome: str,
    no_date_outcome: str,
) -> Verdict:
    """`raw` is the recency value; `created` is the creation value the age ceiling reads."""
    moment = _parse_timestamp(raw, epoch_ms=epoch_ms)
    if moment is not None:
        ats_date = db.format_timestamp(moment)
        if window_start is not None and moment < window_start - WINDOW_SLACK:
            return Verdict("out_of_window", ats_date, kind)
    # A missing creation value skips the ceiling, and the window alone judges (PRD 01).
    born = _parse_timestamp(created, epoch_ms=epoch_ms)
    if (
        window_start is not None
        and born is not None
        and born < now - AGE_CEILING - WINDOW_SLACK
    ):
        return Verdict("out_of_window", db.format_timestamp(born), _CREATED_KIND)
    if moment is None:
        return Verdict(no_date_outcome)
    return Verdict(open_outcome, ats_date, kind)


def _classify_ats(
    ref: AtsRef,
    index: Index | None,
    detail: Mapping | None,
    created: object,
    window_start: datetime | None,
    now: datetime,
) -> Verdict:
    if index is None:
        return Verdict("unverifiable")
    if ref.job_id not in index:
        return Verdict("not_on_index")
    if ref.platform == "rippling":
        if _needs_detail(ref, window_start) and detail is None:
            return Verdict("unverifiable")
        raw = (detail or {}).get("createdOn")
    else:
        raw = index[ref.job_id]
    return _judge(
        raw,
        _ATS_DATE_KINDS[ref.platform],
        epoch_ms=ref.platform == "lever",
        # Only Greenhouse's recency value (a last edit) isn't a creation or publication date.
        created=created if ref.platform == "greenhouse" else raw,
        window_start=window_start,
        now=now,
        open_outcome="verified",
        no_date_outcome="verified_no_date",
    )


def _last_segment(url: str) -> str:
    return urlsplit(url).path.rstrip("/").rpartition("/")[2]


def _valid_through_passed(value: str | None, now: datetime) -> bool:
    moment = _parse_timestamp(value)
    if moment is None:
        return False
    if len(value.strip()) == 10:
        # A bare date is valid through the end of that day.
        moment += timedelta(days=1)
    return moment < now


def _classify_page(
    url: str, page: Page | None, window_start: datetime | None, now: datetime
) -> Verdict:
    if page is None:
        return Verdict("unverifiable")
    if page.status in _GONE_STATUSES:
        return Verdict("page_closed")
    if not 200 <= page.status < 300:
        return Verdict("unverifiable")
    # A closed job typically redirects to the careers home or a search page. Both sides are
    # decoded because the client percent-encodes the final URL but not the stored one.
    if page.redirected and unquote(_last_segment(url)) not in unquote(page.final_url):
        return Verdict("page_closed")
    posting = extract_job_posting(page.html)
    if posting and _valid_through_passed(posting.valid_through, now):
        return Verdict("page_closed")
    return _judge(
        posting.date_posted if posting else None,
        _PAGE_DATE_KIND,
        created=posting.date_posted if posting else None,
        window_start=window_start,
        now=now,
        open_outcome="reachable",
        no_date_outcome="reachable_no_date",
    )


def classify(
    url: str,
    mode: Mode,
    ref: AtsRef | None,
    *,
    index: Index | None = None,
    detail: Mapping | None = None,
    created: object = None,
    page: Page | None = None,
    window_start: datetime | None = None,
    now: datetime,
) -> Verdict:
    """Pure (XC-9). `None` for the index, detail, or page means a fetch the check needs failed.

    `created` is a Greenhouse posting's `first_published`, which its index recency value lacks."""
    if is_aggregator(url):
        return Verdict("aggregator")
    if ref is not None:
        return _classify_ats(ref, index, detail, created, window_start, now)
    if mode == "strict":
        return Verdict("unsupported")
    return _classify_page(url, page, window_start, now)


def _listing(items: object, date_key: str | None) -> dict[str, object]:
    if not isinstance(items, list):
        raise UnexpectedShape("the index is not a list of jobs")
    jobs: dict[str, object] = {}
    for item in items:
        if not isinstance(item, dict) or item.get("id") is None:
            raise UnexpectedShape("an index entry has no id")
        jobs[str(item["id"])] = item.get(date_key) if date_key else None
    return jobs


def _jobs_of(data: object) -> object:
    return data.get("jobs") if isinstance(data, dict) else None


def _greenhouse_index(client: httpx.Client, board: str) -> Fetched:
    data = get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    jobs = _jobs_of(data)
    return Fetched(
        _listing(jobs, "updated_at"), created=_listing(jobs, "first_published")
    )


def _lever_index(client: httpx.Client, board: str) -> Fetched:
    data = get_lever_json(client, f"/v0/postings/{board}?mode=json")
    return Fetched(_listing(data, "createdAt"))


def _ashby_index(client: httpx.Client, board: str) -> Fetched:
    data = get_json(client, ashby_board_url(board))
    return Fetched(_listing(_jobs_of(data), "publishedAt"), record=data)


def _rippling_index(client: httpx.Client, board: str) -> Fetched:
    jobs: dict[str, object] = {}
    page = 0
    while True:
        data = get_json(
            client, f"https://ats.rippling.com/api/v2/board/{board}/jobs?page={page}"
        )
        if not isinstance(data, dict) or not isinstance(data.get("totalPages"), int):
            raise UnexpectedShape("the index page has no page count")
        jobs |= _listing(data.get("items"), None)
        page += 1
        if page >= data["totalPages"]:
            return Fetched(jobs)


_INDEX_FETCHERS: dict[str, Callable[[httpx.Client, str], Fetched]] = {
    "greenhouse": _greenhouse_index,
    "lever": _lever_index,
    "ashby": _ashby_index,
    "rippling": _rippling_index,
}


class Verifier:
    """Fetches what `classify` needs; each board's index is fetched once, for this object's life."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._fetched: dict[tuple[str, str], Fetched | None] = {}

    def _board(self, ref: AtsRef) -> Fetched | None:
        key = (ref.platform, ref.board)
        if key not in self._fetched:
            try:
                self._fetched[key] = _INDEX_FETCHERS[ref.platform](
                    self._client, ref.board
                )
            except httpx.HTTPError, ValueError:
                self._fetched[key] = None
        return self._fetched[key]

    def _rippling_detail(self, ref: AtsRef) -> Mapping | None:
        try:
            data = get_json(self._client, rippling_detail_url(ref.board, ref.job_id))
        except httpx.HTTPError, ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _page(self, url: str) -> Page | None:
        try:
            response = self._client.get(url)
        except httpx.HTTPError:
            return None
        return Page(
            response.status_code,
            str(response.url),
            bool(response.history),
            response.text,
        )

    def check(
        self, url: str, *, mode: Mode, window_start: datetime | None = None
    ) -> Checked:
        ref = resolve_ats(url)
        index = detail = page = None
        created = board = None
        if ref:
            fetched = self._board(ref)
            if fetched is not None:
                index = fetched.index
                created = fetched.created.get(ref.job_id)
                board = fetched.record
            if (
                index is not None
                and ref.job_id in index
                and _needs_detail(ref, window_start)
            ):
                detail = self._rippling_detail(ref)
        elif mode == "best_effort" and not is_aggregator(url):
            page = self._page(url)
        verdict = classify(
            url,
            mode,
            ref,
            index=index,
            detail=detail,
            created=created,
            page=page,
            window_start=window_start,
            now=datetime.now(UTC),
        )
        record = board if ref and ref.platform == "ashby" else detail
        return Checked(verdict, page, record if isinstance(record, dict) else None)


def recheck(
    conn: db.Connection, verifier: Verifier, postings: Sequence[tuple[int, str]]
) -> dict[int, str]:
    """Re-check stored (id, url) postings without a window; returns each one's outcome."""
    outcomes: dict[int, str] = {}
    for posting_id, url in postings:
        # Each posting gets its own platform's check whatever the search mode (PRD 01).
        outcome = verifier.check(url, mode="best_effort").verdict.outcome
        if outcome in CLOSED_OUTCOMES:
            db.mark_closed(conn, posting_id)
        outcomes[posting_id] = outcome
    return outcomes
