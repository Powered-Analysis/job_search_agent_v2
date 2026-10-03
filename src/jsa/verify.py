"""Liveness and recency verification (PRD 01, XC-5): pure classification, the fetches around it, and the stored-posting re-check."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import httpx

from jsa import db
from jsa.ats import AtsRef, resolve_ats
from jsa.capture import extract_job_posting
from jsa.http import get_json, get_lever_json
from jsa.urls import is_aggregator

type Mode = Literal["strict", "best_effort"]
# A board's jobs: job id -> the platform's raw recency value (None where the list carries none).
type Index = Mapping[str, object]

# Absorbs timezone and clock skew between agent, ATS, and pipeline (PRD 01).
WINDOW_SLACK = timedelta(hours=24)

# Only a definite closed signal marks a stored posting closed (PRD 01).
CLOSED_OUTCOMES = frozenset({"not_on_index", "page_closed"})

_ATS_DATE_KINDS = {
    "greenhouse": "updated",
    "lever": "created",
    "ashby": "published",
    "rippling": "created",
}
_PAGE_DATE_KIND = "published"
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
    html: str


@dataclass(frozen=True)
class Checked:
    verdict: Verdict
    # Kept so capture can reuse it instead of fetching again (PRD 01).
    page: Page | None = None


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


def _format_timestamp(moment: datetime) -> str:
    # The one stored-timestamp format, matching db.NOW (PRD 02).
    return (
        moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _needs_detail(ref: AtsRef, window_start: datetime | None) -> bool:
    # Rippling's list carries no timestamp, so only the window test needs the detail record.
    return ref.platform == "rippling" and window_start is not None


def _judge(
    raw: object,
    kind: str,
    *,
    epoch_ms: bool = False,
    window_start: datetime | None,
    open_outcome: str,
    no_date_outcome: str,
) -> Verdict:
    moment = _parse_timestamp(raw, epoch_ms=epoch_ms)
    if moment is None:
        return Verdict(no_date_outcome)
    ats_date = _format_timestamp(moment)
    if window_start is not None and moment < window_start - WINDOW_SLACK:
        return Verdict("out_of_window", ats_date, kind)
    return Verdict(open_outcome, ats_date, kind)


def _classify_ats(
    ref: AtsRef,
    index: Index | None,
    detail: Mapping | None,
    window_start: datetime | None,
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
        window_start=window_start,
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
    # A closed job typically redirects to the careers home or a search page.
    if _last_segment(url) not in page.final_url:
        return Verdict("page_closed")
    posting = extract_job_posting(page.html)
    if posting and _valid_through_passed(posting.valid_through, now):
        return Verdict("page_closed")
    return _judge(
        posting.date_posted if posting else None,
        _PAGE_DATE_KIND,
        window_start=window_start,
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
    page: Page | None = None,
    window_start: datetime | None = None,
    now: datetime,
) -> Verdict:
    """Pure (XC-9). `None` for the index, detail, or page means a fetch the check needs failed."""
    if is_aggregator(url):
        return Verdict("aggregator")
    if ref is not None:
        return _classify_ats(ref, index, detail, window_start)
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


def _greenhouse_index(client: httpx.Client, board: str) -> Index:
    data = get_json(client, f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs")
    return _listing(_jobs_of(data), "updated_at")


def _lever_index(client: httpx.Client, board: str) -> Index:
    return _listing(
        get_lever_json(client, f"/v0/postings/{board}?mode=json"), "createdAt"
    )


def _ashby_index(client: httpx.Client, board: str) -> Index:
    data = get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{board}")
    return _listing(_jobs_of(data), "publishedAt")


def _rippling_index(client: httpx.Client, board: str) -> Index:
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
            return jobs


_INDEX_FETCHERS: dict[str, Callable[[httpx.Client, str], Index]] = {
    "greenhouse": _greenhouse_index,
    "lever": _lever_index,
    "ashby": _ashby_index,
    "rippling": _rippling_index,
}


class Verifier:
    """Fetches what `classify` needs; each board's index is fetched once, for this object's life."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._indexes: dict[tuple[str, str], Index | None] = {}

    def _index(self, ref: AtsRef) -> Index | None:
        key = (ref.platform, ref.board)
        if key not in self._indexes:
            try:
                self._indexes[key] = _INDEX_FETCHERS[ref.platform](
                    self._client, ref.board
                )
            except httpx.HTTPError, ValueError:
                self._indexes[key] = None
        return self._indexes[key]

    def _rippling_detail(self, ref: AtsRef) -> Mapping | None:
        try:
            data = get_json(
                self._client,
                f"https://ats.rippling.com/api/v2/board/{ref.board}/jobs/{ref.job_id}",
            )
        except httpx.HTTPError, ValueError:
            return None
        return data if isinstance(data, dict) else None

    def _page(self, url: str) -> Page | None:
        try:
            response = self._client.get(url)
        except httpx.HTTPError:
            return None
        return Page(response.status_code, str(response.url), response.text)

    def check(
        self, url: str, *, mode: Mode, window_start: datetime | None = None
    ) -> Checked:
        ref = resolve_ats(url)
        index = detail = page = None
        if ref:
            index = self._index(ref)
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
            page=page,
            window_start=window_start,
            now=datetime.now(UTC),
        )
        return Checked(verdict, page)


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
