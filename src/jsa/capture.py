"""Full-JD capture (PRD 01): the one home for the four ATS's detail-record shapes and the `JobPosting` fallback."""

import html
import json
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, Tag
from markdownify import markdownify

from jsa.ats import AtsRef, ashby_board_url, rippling_detail_url
from jsa.errors import JsaError
from jsa.http import get_json, get_lever_json


class CaptureError(JsaError):
    """The posting's detail record could not be fetched or held no job description."""


@dataclass(frozen=True)
class Capture:
    jd_markdown: str
    title: str | None
    location: str | None
    # Only the `JobPosting` fallback knows it; ATS postings derive it from the board slug.
    company: str | None = None


@dataclass(frozen=True)
class JobPosting:
    """The schema.org `JobPosting` fields a page publishes; every one is optional."""

    jd_markdown: str | None
    title: str | None
    company: str | None
    location: str | None
    date_posted: str | None
    valid_through: str | None


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


def _record(data: object) -> dict:
    if not isinstance(data, dict):
        raise CaptureError("the ATS returned an unexpected response shape")
    return data


def _build(
    jd_markdown: str,
    title: str | None,
    location: object,
    company: str | None = None,
) -> Capture:
    if not jd_markdown:
        raise CaptureError("the posting has no job description")
    return Capture(
        jd_markdown,
        (title or "").strip() or None,
        normalize_location(location),
        company,
    )


def _capture(jd_html: str | None, title: str | None, location: object) -> Capture:
    return _build(html_to_markdown(jd_html or ""), title, location)


def _greenhouse(client: httpx.Client, ref: AtsRef) -> Capture:
    record = _record(
        get_json(
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
    record = _record(
        get_lever_json(client, f"/v0/postings/{ref.board}/{ref.job_id}?mode=json")
    )
    location = (record.get("categories") or {}).get("location")
    if record.get("description"):
        return _capture(record["description"], record.get("text"), location)
    # Plain text is already valid Markdown; converting it as HTML would mangle it.
    plain = (record.get("descriptionPlain") or "").strip()
    return _build(plain, record.get("text"), location)


def _ashby(client: httpx.Client, ref: AtsRef) -> Capture:
    board = _record(get_json(client, ashby_board_url(ref.board)))
    for job in board.get("jobs") or []:
        if _record(job).get("id") == ref.job_id:
            return _capture(
                job.get("descriptionHtml"), job.get("title"), job.get("location")
            )
    raise CaptureError(f"job {ref.job_id} is not on the {ref.board} board")


def _rippling(client: httpx.Client, ref: AtsRef) -> Capture:
    record = _record(get_json(client, rippling_detail_url(ref.board, ref.job_id)))
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

# Greenhouse and Lever publish no `JobPosting` data, so their fetchers are the only source.
_NO_JOBPOSTING_DATA = frozenset({"greenhouse", "lever"})

_JOBPOSTING_TYPE = "JobPosting"


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _listed(value: object) -> list:
    return value if isinstance(value, list) else [value]


def _is_jobposting_type(value: object) -> bool:
    # `@type` is a name, a schema.org URL, or a list of either.
    return any(
        isinstance(kind, str) and kind.rstrip("/").endswith(_JOBPOSTING_TYPE)
        for kind in _listed(value)
    )


def _find_jobposting(data: object) -> dict | None:
    for node in _listed(data):
        if not isinstance(node, dict):
            continue
        if _is_jobposting_type(node.get("@type")):
            return node
        if found := _find_jobposting(node.get("@graph") or []):
            return found
    return None


def _json_ld_jobposting(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            # Sites often leave raw newlines inside JSON strings; they are harmless.
            data = json.loads(script.get_text(), strict=False)
        except ValueError:
            continue
        if found := _find_jobposting(data):
            return found
    return None


def _microdata_scope(scope: Tag) -> dict:
    properties: dict[str, list] = {}
    for element in scope.find_all(attrs={"itemprop": True}):
        owner = element.find_parent(attrs={"itemscope": True})
        if owner is not scope:
            continue
        for name in element["itemprop"].split():
            properties.setdefault(name, []).append(_microdata_value(element, name))
    return {
        name: values[0] if len(values) == 1 else values
        for name, values in properties.items()
    }


def _microdata_value(element: Tag, name: str) -> object:
    if element.has_attr("itemscope"):
        return _microdata_scope(element)
    if element.name == "meta":
        return element.get("content")
    if element.name in ("a", "link", "area"):
        return element.get("href")
    if element.name == "time":
        return element.get("datetime") or element.get_text(strip=True)
    if name == "description":
        return element.decode_contents()
    return element.get_text(" ", strip=True)


def _microdata_jobposting(soup: BeautifulSoup) -> dict | None:
    for scope in soup.find_all(attrs={"itemscope": True, "itemtype": True}):
        if _is_jobposting_type(scope["itemtype"]):
            return _microdata_scope(scope)
    return None


def _organization_name(value: object) -> str | None:
    for item in _listed(value):
        name = item.get("name") if isinstance(item, dict) else item
        if text := _text(name):
            return text
    return None


def _place(value: object) -> str | None:
    """One `jobLocation` Place or PostalAddress as a display string."""
    if isinstance(value, dict):
        if "address" in value:
            return _place(value["address"]) or _text(value.get("name"))
        parts = (
            _place(value.get(key))
            for key in ("addressLocality", "addressRegion", "addressCountry")
        )
        return ", ".join(filter(None, parts)) or _text(value.get("name"))
    return _text(value)


def _description_markdown(value: object) -> str | None:
    description = _text(value)
    if not description:
        return None
    if "<" not in description and "&lt;" in description:
        # Some sites publish the markup entity-escaped, so the markup itself is text.
        description = html.unescape(description)
    return html_to_markdown(description) or None


def extract_job_posting(markup: str) -> JobPosting | None:
    """Read schema.org `JobPosting` data (JSON-LD, else microdata) from a page's HTML. Pure."""
    soup = BeautifulSoup(markup, "html.parser")
    node = _json_ld_jobposting(soup) or _microdata_jobposting(soup)
    if node is None:
        return None
    return JobPosting(
        jd_markdown=_description_markdown(node.get("description")),
        title=_text(node.get("title")) or _text(node.get("name")),
        company=_organization_name(node.get("hiringOrganization")),
        location=normalize_location(
            [_place(place) for place in _listed(node.get("jobLocation"))]
        ),
        date_posted=_text(node.get("datePosted")),
        valid_through=_text(node.get("validThrough")),
    )


def _failure(source: str, error: Exception) -> CaptureError:
    detail = (str(error).splitlines() or [type(error).__name__])[0]
    return CaptureError(f"{source} capture failed: {detail}")


def capture(client: httpx.Client, ref: AtsRef) -> Capture:
    try:
        return _FETCHERS[ref.platform](client, ref)
    except (httpx.HTTPError, ValueError) as error:
        raise _failure(ref.platform, error) from error


def capture_page(client: httpx.Client, url: str) -> Capture:
    """One GET of the posting page, read for its `JobPosting` data."""
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise _failure("page", error) from error
    posting = extract_job_posting(response.text)
    if posting is None:
        raise CaptureError("the page carries no schema.org JobPosting data")
    return _build(
        posting.jd_markdown or "", posting.title, posting.location, posting.company
    )


def capture_posting(client: httpx.Client, url: str, ref: AtsRef | None) -> Capture:
    """Capture order (XC-5): supported ATS fetcher, then `JobPosting` data; the caller stores NULL."""
    if ref is None:
        return capture_page(client, url)
    try:
        return capture(client, ref)
    except CaptureError as ats_error:
        if ref.platform in _NO_JOBPOSTING_DATA:
            raise
        try:
            return capture_page(client, url)
        except CaptureError as page_error:
            raise CaptureError(f"{ats_error}; {page_error}") from page_error
