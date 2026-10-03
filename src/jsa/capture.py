"""Full-JD capture from the four supported ATS (PRD 01): the one home for their detail-record shapes."""

import html
from dataclasses import dataclass

import httpx
from markdownify import markdownify

from jsa.ats import AtsRef
from jsa.errors import JsaError


class CaptureError(JsaError):
    """The posting's detail record could not be fetched or held no job description."""


@dataclass(frozen=True)
class Capture:
    jd_markdown: str
    title: str | None
    location: str | None


def html_to_markdown(markup: str) -> str:
    return markdownify(markup, heading_style="ATX").strip()


def normalize_location(value: object) -> str | None:
    """Reduce a location given as a string, a dict, or a list to one display string."""
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return normalize_location(value.get("name") or value.get("label"))
    if isinstance(value, list):
        names = dict.fromkeys(filter(None, map(normalize_location, value)))
        return "; ".join(names) or None
    return None


def _get_json(client: httpx.Client, url: str) -> object:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def _record(data: object) -> dict:
    if not isinstance(data, dict):
        raise CaptureError("the ATS returned an unexpected response shape")
    return data


def _capture(jd_html: str | None, title: str | None, location: object) -> Capture:
    if not jd_html or not (jd_markdown := html_to_markdown(jd_html)):
        raise CaptureError("the ATS record has no job description")
    return Capture(
        jd_markdown, (title or "").strip() or None, normalize_location(location)
    )


def _greenhouse(client: httpx.Client, ref: AtsRef) -> Capture:
    record = _record(
        _get_json(
            client,
            f"https://boards-api.greenhouse.io/v1/boards/{ref.board}/jobs/{ref.job_id}",
        )
    )
    # `content` arrives entity-escaped: the markup itself is text.
    return _capture(
        html.unescape(record.get("content") or ""),
        record.get("title"),
        record.get("location"),
    )


def _lever(client: httpx.Client, ref: AtsRef) -> Capture:
    path = f"/v0/postings/{ref.board}/{ref.job_id}?mode=json"
    try:
        data = _get_json(client, f"https://api.lever.co{path}")
    except httpx.HTTPError:
        # EU-hosted boards answer only on their own host.
        data = _get_json(client, f"https://api.eu.lever.co{path}")
    record = _record(data)
    location = (record.get("categories") or {}).get("location")
    if record.get("description"):
        return _capture(record["description"], record.get("text"), location)
    # Plain text is already valid Markdown; converting it as HTML would mangle it.
    plain = (record.get("descriptionPlain") or "").strip()
    if not plain:
        raise CaptureError("the ATS record has no job description")
    return Capture(plain, record.get("text") or None, normalize_location(location))


def _ashby(client: httpx.Client, ref: AtsRef) -> Capture:
    board = _record(
        _get_json(client, f"https://api.ashbyhq.com/posting-api/job-board/{ref.board}")
    )
    for job in board.get("jobs") or []:
        if _record(job).get("id") == ref.job_id:
            return _capture(
                job.get("descriptionHtml"), job.get("title"), job.get("location")
            )
    raise CaptureError(f"job {ref.job_id} is not on the {ref.board} board")


def _rippling(client: httpx.Client, ref: AtsRef) -> Capture:
    record = _record(
        _get_json(
            client,
            f"https://ats.rippling.com/api/v2/board/{ref.board}/jobs/{ref.job_id}",
        )
    )
    description = record.get("description")
    if isinstance(description, dict):
        # The role comes first, the company blurb after it.
        description = "".join(
            description.get(part) or "" for part in ("role", "company")
        )
    return _capture(
        description,
        record.get("name") or record.get("title"),
        record.get("workLocations") or record.get("locations"),
    )


_FETCHERS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "ashby": _ashby,
    "rippling": _rippling,
}


def capture(client: httpx.Client, ref: AtsRef) -> Capture:
    try:
        return _FETCHERS[ref.platform](client, ref)
    except (httpx.HTTPError, ValueError) as error:
        raise CaptureError(
            f"{ref.platform} capture failed: {str(error).splitlines()[0]}"
        ) from error
