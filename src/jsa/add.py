"""Manual add (PRD 03): the user supplies a posting's URL, which is itself the Apply decision."""

import httpx

from jsa import db, prompts
from jsa.ats import resolve_ats
from jsa.capture import Capture, CaptureError, capture
from jsa.errors import JsaError
from jsa.naming import company_from_board
from jsa.urls import is_aggregator

NO_PACKET_JD = "no job description was captured; its packet will have no job_posting.md"


def _confirm(label: str, derived: str | None) -> str:
    while not (value := prompts.ask(label, derived or "").strip()):
        pass
    return value


def _promote(
    conn: db.Connection, url: str, posting_id: int, decision: str | None
) -> None:
    if decision == "Apply":
        print(f"Posting {posting_id}: already Apply; no change.")
        return
    db.set_decision(conn, url, "Apply")
    print(f"Posting {posting_id}: {decision or 'undecided'} → Apply")


def add_posting(
    client: httpx.Client,
    url: str,
    *,
    company: str | None,
    title: str | None,
    date_posted: str | None,
    no_input: bool,
) -> None:
    if is_aggregator(url):
        raise JsaError(
            "that URL is on a job aggregator; give the employer's own posting URL"
        )
    conn = db.connect()
    if existing := db.find_posting(conn, url):
        _promote(conn, url, *existing)
        return

    ref = resolve_ats(url)
    captured: Capture | None = None
    if ref:
        try:
            captured = capture(client, ref)
        except CaptureError as error:
            print(f"Capture failed: {error}")

    derived_company = company_from_board(ref.board) if ref else None
    derived_title = captured.title if captured else None
    if no_input:
        company = company or derived_company
        title = title or derived_title
        missing = [
            name
            for name, value in (("company", company), ("title", title))
            if not value
        ]
        if missing:
            flags = " and ".join(f"--{name}" for name in missing)
            raise JsaError(
                f"cannot derive the {' and '.join(missing)}; pass {flags} or drop --no-input"
            )
    else:
        company = company or _confirm("Company", derived_company)
        title = title or _confirm("Title", derived_title)

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
        _promote(conn, url, *db.find_posting(conn, url))
        return
    if captured:
        # No title: the one the user confirmed stands.
        db.capture_jd(
            conn,
            posting_id,
            jd_markdown=captured.jd_markdown,
            location=captured.location,
        )
        print(f"Added posting {posting_id}: {company} — {title} (Apply)")
    else:
        print(
            f"Added posting {posting_id}: {company} — {title} (Apply); {NO_PACKET_JD}"
        )
