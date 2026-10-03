import json
import re
import socket

import httpx
import pytest

from jsa import capture as capture_module
from jsa import db
from jsa.ats import resolve_ats
from jsa.capture import CaptureError, capture_posting, extract_job_posting
from jsa.http import make_client

ASHBY_ID = "8d7c6b5a-4f3e-4d2c-b1a0-9e8f7a6b5c4d"
GH_URL = "https://job-boards.greenhouse.io/acme-widgets/jobs/4012345"
LEVER_URL = "https://jobs.lever.co/acme-widgets/3f2a9c1e-7b4d-4e8a-9c56-1d0e2f3a4b5c"
ASHBY_URL = f"https://jobs.ashbyhq.com/acme-widgets/{ASHBY_ID}"
OFF_FOUR_URL = "https://careers.example.com/openings/7788"

JD_HTML = "<h2>About the role</h2><p>Build the platform.</p><ul><li>Python</li></ul>"


def job_posting(**overrides):
    node = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Staff Platform Engineer",
        "description": JD_HTML,
        "datePosted": "2026-09-30",
        "validThrough": "2026-12-31T00:00:00Z",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
        "jobLocation": {
            "@type": "Place",
            "address": {
                "@type": "PostalAddress",
                "addressLocality": "Austin",
                "addressRegion": "TX",
                "addressCountry": "US",
            },
        },
    }
    return {**node, **overrides}


def json_ld_page(data):
    return (
        "<html><head><title>Careers</title>"
        f'<script type="application/ld+json">{json.dumps(data)}</script>'
        "</head><body><h1>Careers</h1></body></html>"
    )


MICRODATA_PAGE = """
<html><body>
<div itemscope itemtype="https://schema.org/JobPosting">
  <h1 itemprop="title">Support Lead</h1>
  <div itemprop="description"><h2>The role</h2><p>Lead support.</p></div>
  <meta itemprop="datePosted" content="2026-09-28">
  <meta itemprop="validThrough" content="2026-11-30">
  <div itemprop="hiringOrganization" itemscope itemtype="https://schema.org/Organization">
    <span itemprop="name">Micro Widgets</span>
  </div>
  <div itemprop="jobLocation" itemscope itemtype="https://schema.org/Place">
    <div itemprop="address" itemscope itemtype="https://schema.org/PostalAddress">
      <span itemprop="addressLocality">Denver</span>,
      <span itemprop="addressRegion">CO</span>
    </div>
  </div>
</div>
</body></html>
"""


def headings(markdown):
    return re.findall(r"^#{1,6} \S.*$", markdown, flags=re.MULTILINE)


# --- extractor: JSON-LD ------------------------------------------------------


def test_json_ld_jobposting_yields_every_field():
    result = extract_job_posting(json_ld_page(job_posting()))
    assert result is not None
    assert result.title == "Staff Platform Engineer"
    assert result.company == "Example Corp"
    assert "Austin" in result.location
    assert result.date_posted == "2026-09-30"
    assert result.valid_through == "2026-12-31T00:00:00Z"
    assert headings(result.jd_markdown) == ["## About the role"]
    assert "Build the platform." in result.jd_markdown
    assert "Python" in result.jd_markdown
    assert "<p>" not in result.jd_markdown and "<h2>" not in result.jd_markdown


def test_json_ld_description_is_converted_by_the_shared_html_to_markdown():
    result = extract_job_posting(json_ld_page(job_posting()))
    assert result.jd_markdown == capture_module.html_to_markdown(JD_HTML)


def test_json_ld_jobposting_in_a_list_is_found():
    other = {"@context": "https://schema.org", "@type": "Organization", "name": "X"}
    page = json_ld_page([other, job_posting(title="Listed Role")])
    result = extract_job_posting(page)
    assert result is not None
    assert result.title == "Listed Role"


def test_json_ld_jobposting_inside_a_graph_is_found():
    graph = {
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebSite", "name": "Careers"},
            {
                k: v
                for k, v in job_posting(title="Graph Role").items()
                if k != "@context"
            },
        ],
    }
    result = extract_job_posting(json_ld_page(graph))
    assert result is not None
    assert result.title == "Graph Role"
    assert result.company == "Example Corp"


def test_json_ld_jobposting_fields_are_each_optional():
    page = json_ld_page({"@context": "https://schema.org", "@type": "JobPosting"})
    result = extract_job_posting(page)
    assert result is not None
    assert result.title is None
    assert result.company is None
    assert result.date_posted is None
    assert result.valid_through is None


# --- extractor: microdata ----------------------------------------------------


def test_microdata_jobposting_yields_every_field():
    result = extract_job_posting(MICRODATA_PAGE)
    assert result is not None
    assert result.title == "Support Lead"
    assert result.company == "Micro Widgets"
    assert "Denver" in result.location
    assert result.date_posted == "2026-09-28"
    assert result.valid_through == "2026-11-30"
    assert headings(result.jd_markdown) == ["## The role"]
    assert "Lead support." in result.jd_markdown


# --- extractor: absence and malformed data -----------------------------------


def script_block(body):
    return (
        f'<html><head><script type="application/ld+json">{body}</script></head></html>'
    )


NO_JOBPOSTING_PAGES = {
    "empty": "",
    "no-structured-data": "<html><body><p>No structured data.</p></body></html>",
    "other-type": script_block('{"@type": "Organization", "name": "Example"}'),
    "malformed-json": script_block("{not json at all"),
    "empty-block": script_block(""),
    "non-object-json": script_block('[1, "two", null]'),
    "other-microdata-type": (
        "<div itemscope itemtype='https://schema.org/Organization'>"
        "<span itemprop='name'>Example</span></div>"
    ),
}


@pytest.mark.parametrize(
    "markup", NO_JOBPOSTING_PAGES.values(), ids=NO_JOBPOSTING_PAGES
)
def test_page_without_usable_jobposting_yields_none_and_does_not_raise(markup):
    assert extract_job_posting(markup) is None


def test_extractor_makes_no_network_or_database_call(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the extractor reached the outside world")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(db, "connect", refuse)
    assert extract_job_posting(json_ld_page(job_posting())) is not None
    assert extract_job_posting(MICRODATA_PAGE) is not None
    assert extract_job_posting("<html></html>") is None


# --- capture order -----------------------------------------------------------


class Pages:
    """An httpx transport handler answering from a (host, path) table; unrouted is 404."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.routes.get((request.url.host, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(answer, str):
            return httpx.Response(
                200, text=answer, headers={"content-type": "text/html"}
            )
        return httpx.Response(200, json=answer)

    def fetched(self, host):
        return [r for r in self.requests if r.url.host == host]


def page_route(url):
    parsed = httpx.URL(url)
    return (parsed.host, parsed.path)


def run_capture(pages, url):
    ref = resolve_ats(url)
    with make_client(httpx.MockTransport(pages)) as client:
        return capture_posting(client, url, ref)


def test_off_four_url_is_captured_from_its_page_jobposting_data():
    pages = Pages({page_route(OFF_FOUR_URL): json_ld_page(job_posting())})
    result = run_capture(pages, OFF_FOUR_URL)
    assert result.title == "Staff Platform Engineer"
    assert result.company == "Example Corp"
    assert "Austin" in result.location
    assert "## About the role" in result.jd_markdown
    assert [(r.method, str(r.url)) for r in pages.requests] == [("GET", OFF_FOUR_URL)]


def test_off_four_page_without_jobposting_data_is_a_capture_error():
    pages = Pages({page_route(OFF_FOUR_URL): "<html><body>Hello</body></html>"})
    with pytest.raises(CaptureError):
        run_capture(pages, OFF_FOUR_URL)


def test_off_four_page_that_errors_is_a_capture_error():
    with pytest.raises(CaptureError):
        run_capture(Pages({}), OFF_FOUR_URL)


def test_supported_fetcher_wins_over_the_page_and_the_page_is_not_fetched():
    ashby_board = ("api.ashbyhq.com", "/posting-api/job-board/acme-widgets")
    pages = Pages(
        {
            ashby_board: {
                "jobs": [
                    {
                        "id": ASHBY_ID,
                        "title": "ATS Title",
                        "descriptionHtml": "<h2>From the ATS</h2>",
                        "location": "Remote",
                    }
                ]
            },
            page_route(ASHBY_URL): json_ld_page(job_posting(title="Page Title")),
        }
    )
    result = run_capture(pages, ASHBY_URL)
    assert result.title == "ATS Title"
    assert "From the ATS" in result.jd_markdown
    assert pages.fetched("jobs.ashbyhq.com") == []


def test_ashby_with_a_failed_ats_fetch_falls_back_to_the_page_jobposting():
    pages = Pages({page_route(ASHBY_URL): json_ld_page(job_posting())})
    result = run_capture(pages, ASHBY_URL)
    assert result.title == "Staff Platform Engineer"
    assert "## About the role" in result.jd_markdown
    assert len(pages.fetched("jobs.ashbyhq.com")) == 1


def test_ashby_with_a_failed_ats_fetch_and_no_page_data_is_a_capture_error():
    pages = Pages({page_route(ASHBY_URL): "<html><body>Hello</body></html>"})
    with pytest.raises(CaptureError):
        run_capture(pages, ASHBY_URL)


def test_rippling_with_a_failed_ats_fetch_falls_back_to_the_page_jobposting():
    url = "https://ats.rippling.com/acme-widgets/jobs/c4b5a697-8f0e-4a1b-8c2d-3e4f5a6b7c8d"
    pages = Pages({page_route(url): json_ld_page(job_posting())})
    result = run_capture(pages, url)
    assert result.title == "Staff Platform Engineer"
    assert [r.url.path for r in pages.requests][-1] == httpx.URL(url).path


@pytest.mark.parametrize("url", [GH_URL, LEVER_URL], ids=["greenhouse", "lever"])
def test_greenhouse_and_lever_never_fall_back_to_the_page(url):
    pages = Pages({page_route(url): json_ld_page(job_posting())})
    with pytest.raises(CaptureError):
        run_capture(pages, url)
    assert pages.fetched(httpx.URL(url).host) == []
