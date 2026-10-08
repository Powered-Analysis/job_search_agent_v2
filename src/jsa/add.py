"""Manual add (PRD 03): the user supplies a posting's URL, which is itself the Apply decision."""

from collections.abc import Callable
from typing import Literal, NamedTuple

import httpx

from jsa import db
from jsa.ats import resolve_ats
from jsa.capture import (
    Capture,
    CaptureError,
    JobPosting,
    NoDescriptionError,
    capture_posting,
)
from jsa.errors import JsaError
from jsa.naming import company_from_board
from jsa.urls import is_aggregator

NO_PACKET_JD = "no job description was captured; its packet will have no job_posting.md"


class AggregatorUrlError(JsaError):
    """The URL is on a job aggregator; manual add stores only the employer's own."""


class MissingFieldsError(JsaError):
    """The company or title can't be derived; `missing` names which."""

    def __init__(self, missing: list[str]):
        super().__init__(f"cannot derive the {' and '.join(missing)}")
        self.missing = missing


class Derived(NamedTuple):
    """What capture and the URL yield for a new row, and why a capture failed."""

    company: str | None
    title: str | None
    capture_failure: str | None


class AddOutcome(NamedTuple):
    posting_id: int
    # "added", "promoted" (an existing row moved to Apply), or "already_apply".
    kind: Literal["added", "promoted", "already_apply"]
    # The decision a promoted row had before, None if undecided.
    previous_decision: str | None
    # The new row's company and title; None unless added.
    company: str | None
    title: str | None
    # Whether the row now holds a job description.
    has_jd: bool


def require_fields(company: str | None, title: str | None) -> tuple[str, str]:
    """The company and title, or MissingFieldsError naming the empty ones."""
    missing = [
        name for name, value in (("company", company), ("title", title)) if not value
    ]
    if missing:
        raise MissingFieldsError(missing)
    return company, title


def accept_derived(derived: Derived) -> tuple[str, str]:
    return require_fields(derived.company, derived.title)


def _promote(
    conn: db.Connection,
    url: str,
    posting_id: int,
    decision: str | None,
    description: str | None,
) -> AddOutcome:
    if description:
        db.supply_jd(conn, posting_id, description)
    has_jd = db.has_jd(conn, posting_id)
    if decision == "Apply":
        return AddOutcome(posting_id, "already_apply", None, None, None, has_jd)
    db.set_decision(conn, url, "Apply")
    return AddOutcome(posting_id, "promoted", decision, None, None, has_jd)


def add_posting(
    client: httpx.Client,
    url: str,
    *,
    date_posted: str | None,
    description: str | None = None,
    fields: Callable[[Derived], tuple[str, str]] = accept_derived,
) -> AddOutcome:
    """Manual add (PRD 03): `fields` turns the derived company and title into the stored pair.

    `description` is the job description the caller already holds; it fills in only
    where capture yielded none (XC-5).
    """
    if is_aggregator(url):
        raise AggregatorUrlError(
            "that URL is on a job aggregator; give the employer's own posting URL"
        )
    conn = db.connect()
    if existing := db.find_posting(conn, url):
        return _promote(conn, url, *existing, description)

    ref = resolve_ats(url)
    # What the page published, even when it had no description to capture.
    published: Capture | JobPosting | None = None
    capture_failure: str | None = None
    try:
        published = capture_posting(client, url, ref)
    except NoDescriptionError as error:
        capture_failure = str(error)
        published = error.posting
    except CaptureError as error:
        capture_failure = str(error)

    if ref:
        derived_company = company_from_board(ref.board)
    else:
        derived_company = published.company if published else None
    derived_title = published.title if published else None
    company, title = fields(Derived(derived_company, derived_title, capture_failure))

    posting_id = db.insert_posting(
        conn,
        company=company,
        title=title,
        url=url,
        search_agent="manual",
        date_posted=date_posted,
    )
    if posting_id is None:
        # Another writer stored it between the lookup and the insert.
        return _promote(conn, url, *db.find_posting(conn, url), description)
    jd_markdown = (published.jd_markdown if published else None) or description
    if published or jd_markdown:
        # No title: the one the caller confirmed stands.
        db.capture_jd(
            conn,
            posting_id,
            jd_markdown=jd_markdown,
            location=published.location if published else None,
        )
    return AddOutcome(posting_id, "added", None, company, title, bool(jd_markdown))
