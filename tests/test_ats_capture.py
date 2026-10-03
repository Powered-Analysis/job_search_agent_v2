import re
import uuid
from importlib.metadata import version

import httpx
import pytest

from jsa import capture as capture_module
from jsa.ats import resolve_ats
from jsa.capture import CaptureError, capture, normalize_location
from jsa.http import make_client
from jsa.urls import is_aggregator

USER_AGENT = f"job-search-agent/{version('jsa')}"
LEVER_ID = str(uuid.uuid4())
ASHBY_ID = str(uuid.uuid4())
RIPPLING_ID = str(uuid.uuid4())


def ref_of(url):
    ref = resolve_ats(url)
    assert ref is not None, url
    return ref


class Recorder:
    """An httpx transport handler that records requests and answers from a route table."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        key = (request.url.host, request.url.path)
        answer = self.routes.get(key)
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    @property
    def hosts(self):
        return [request.url.host for request in self.requests]

    @property
    def paths(self):
        return [request.url.path for request in self.requests]


def client_for(recorder):
    return make_client(httpx.MockTransport(recorder))


def headings(markdown):
    return re.findall(r"^#{1,6} \S.*$", markdown, flags=re.MULTILINE)


# --- ATS resolution ----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "platform", "board", "job_id"),
    [
        (
            "https://job-boards.greenhouse.io/acme-widgets/jobs/4012345",
            "greenhouse",
            "acme-widgets",
            "4012345",
        ),
        (
            "https://boards.greenhouse.io/acme-widgets/jobs/4012345",
            "greenhouse",
            "acme-widgets",
            "4012345",
        ),
        (
            f"https://jobs.lever.co/acme-widgets/{LEVER_ID}",
            "lever",
            "acme-widgets",
            LEVER_ID,
        ),
        (
            f"https://jobs.ashbyhq.com/acme-widgets/{ASHBY_ID}",
            "ashby",
            "acme-widgets",
            ASHBY_ID,
        ),
        (
            f"https://ats.rippling.com/acme-widgets/jobs/{RIPPLING_ID}",
            "rippling",
            "acme-widgets",
            RIPPLING_ID,
        ),
    ],
)
def test_resolution_returns_platform_board_and_id(url, platform, board, job_id):
    ref = resolve_ats(url)
    assert ref is not None
    assert ref.platform.casefold() == platform
    assert ref.board == board
    assert str(ref.job_id) == job_id


def test_resolution_ignores_tracking_parameters_on_a_board_url():
    ref = resolve_ats("https://boards.greenhouse.io/acme/jobs/77?gh_src=abc123")
    assert ref is not None
    assert (ref.board, str(ref.job_id)) == ("acme", "77")


@pytest.mark.parametrize(
    "url",
    [
        "https://careers.example.com/open-roles?gh_jid=4012345",
        "https://www.example.com/jobs/4012345",
        "https://www.linkedin.com/jobs/view/4012345",
        "https://example.com/acme/jobs/4012345",
        "https://notgreenhouse.io/acme/jobs/4012345",
        "https://example.com/redirect?to=https://boards.greenhouse.io/acme/jobs/1",
    ],
)
def test_resolution_returns_nothing_for_other_urls(url):
    assert resolve_ats(url) is None


# --- aggregator list ---------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/4012345",
        "https://linkedin.com/jobs/view/4012345",
        "https://www.indeed.com/viewjob?jk=abc123",
        "https://uk.indeed.com/viewjob?jk=abc123",
    ],
)
def test_aggregator_hosts_are_recognized(url):
    assert is_aggregator(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.greenhouse.io/acme/jobs/1",
        "https://jobs.lever.co/acme/" + LEVER_ID,
        "https://notlinkedin.com/jobs/1",
        "https://boards.greenhouse.io/acme/jobs/1?ref=linkedin.com",
    ],
)
def test_employer_hosts_are_not_aggregators(url):
    assert is_aggregator(url) is False


# --- shared HTTP client ------------------------------------------------------


def test_client_sends_the_versioned_user_agent():
    recorder = Recorder({("example.test", "/x"): {}})
    with client_for(recorder) as client:
        client.get("https://example.test/x")
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


def test_client_follows_redirects():
    def handler(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "https://example.test/new"})
        return httpx.Response(200, json={"ok": True})

    with make_client(httpx.MockTransport(handler)) as client:
        response = client.get("https://example.test/old")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# --- capture: Greenhouse -----------------------------------------------------

GH_URL = "https://job-boards.greenhouse.io/acme-widgets/jobs/4012345"
GH_DETAIL = ("boards-api.greenhouse.io", "/v1/boards/acme-widgets/jobs/4012345")


def greenhouse_record(**overrides):
    record = {
        "title": "Staff Platform Engineer",
        "content": "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;Build the platform.&lt;/p&gt;"
        "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;/ul&gt;",
        "location": {"name": "Remote, US"},
    }
    return {**record, **overrides}


def test_greenhouse_capture_returns_markdown_title_and_location():
    recorder = Recorder({GH_DETAIL: greenhouse_record()})
    with client_for(recorder) as client:
        result = capture(client, ref_of(GH_URL))
    assert result.title == "Staff Platform Engineer"
    assert result.location == "Remote, US"
    assert "## About the role" in result.jd_markdown
    assert "Build the platform." in result.jd_markdown
    assert "Python" in result.jd_markdown


def test_greenhouse_html_entities_are_unescaped():
    recorder = Recorder({GH_DETAIL: greenhouse_record()})
    with client_for(recorder) as client:
        result = capture(client, ref_of(GH_URL))
    assert "&lt;" not in result.jd_markdown
    assert "<h2>" not in result.jd_markdown
    assert "<p>" not in result.jd_markdown


def test_greenhouse_capture_uses_get_on_the_detail_record_with_the_user_agent():
    recorder = Recorder({GH_DETAIL: greenhouse_record()})
    with client_for(recorder) as client:
        capture(client, ref_of(GH_URL))
    assert [(r.method, r.url.host, r.url.path) for r in recorder.requests] == [
        ("GET", *GH_DETAIL)
    ]
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


def test_greenhouse_capture_follows_redirects_to_the_record():
    def handler(request):
        if request.url.path.endswith("/4012345"):
            return httpx.Response(
                301, headers={"location": "https://boards-api.greenhouse.io/moved"}
            )
        return httpx.Response(200, json=greenhouse_record())

    with make_client(httpx.MockTransport(handler)) as client:
        result = capture(client, ref_of(GH_URL))
    assert result.title == "Staff Platform Engineer"


# --- capture: Lever ----------------------------------------------------------

LEVER_URL = f"https://jobs.lever.co/acme-widgets/{LEVER_ID}"
LEVER_DETAIL = ("api.lever.co", f"/v0/postings/acme-widgets/{LEVER_ID}")
LEVER_EU_DETAIL = ("api.eu.lever.co", f"/v0/postings/acme-widgets/{LEVER_ID}")


def lever_record(**overrides):
    record = {
        "text": "Senior Data Engineer",
        "description": "<h1>The team</h1><p>We move data.</p>",
        "descriptionPlain": "Plain fallback body text",
        "categories": {"location": "Berlin, Germany"},
    }
    record = {**record, **overrides}
    return {key: value for key, value in record.items() if value is not None}


def test_lever_capture_returns_markdown_title_and_location():
    recorder = Recorder({LEVER_DETAIL: lever_record()})
    with client_for(recorder) as client:
        result = capture(client, ref_of(LEVER_URL))
    assert result.title == "Senior Data Engineer"
    assert result.location == "Berlin, Germany"
    assert "# The team" in result.jd_markdown
    assert "We move data." in result.jd_markdown
    assert "Plain fallback" not in result.jd_markdown
    request = recorder.requests[0]
    assert request.method == "GET"
    assert request.url.params.get("mode") == "json"
    assert request.headers["user-agent"] == USER_AGENT


def test_lever_without_description_falls_back_to_description_plain():
    recorder = Recorder({LEVER_DETAIL: lever_record(description=None)})
    with client_for(recorder) as client:
        result = capture(client, ref_of(LEVER_URL))
    assert "Plain fallback body text" in result.jd_markdown


@pytest.mark.parametrize("status", [404, 500])
def test_lever_retries_on_the_eu_host_after_an_http_error(status):
    recorder = Recorder(
        {
            LEVER_DETAIL: httpx.Response(status, json={"error": "nope"}),
            LEVER_EU_DETAIL: lever_record(text="EU Engineer"),
        }
    )
    with client_for(recorder) as client:
        result = capture(client, ref_of(LEVER_URL))
    assert result.title == "EU Engineer"
    assert recorder.hosts == ["api.lever.co", "api.eu.lever.co"]
    eu_request = recorder.requests[1]
    assert eu_request.url.params.get("mode") == "json"
    assert all(r.headers["user-agent"] == USER_AGENT for r in recorder.requests)


def test_lever_does_not_touch_the_eu_host_when_the_first_fetch_succeeds():
    recorder = Recorder({LEVER_DETAIL: lever_record()})
    with client_for(recorder) as client:
        capture(client, ref_of(LEVER_URL))
    assert recorder.hosts == ["api.lever.co"]


def test_lever_capture_fails_when_both_hosts_fail():
    recorder = Recorder({})
    with client_for(recorder) as client, pytest.raises(CaptureError):
        capture(client, ref_of(LEVER_URL))


# --- capture: Ashby ----------------------------------------------------------

ASHBY_URL = f"https://jobs.ashbyhq.com/acme-widgets/{ASHBY_ID}"
ASHBY_BOARD = ("api.ashbyhq.com", "/posting-api/job-board/acme-widgets")


def ashby_board():
    return {
        "jobs": [
            {
                "id": str(uuid.uuid4()),
                "title": "Some Other Role",
                "descriptionHtml": "<h2>Other</h2><p>Not this one.</p>",
                "location": "Paris",
            },
            {
                "id": ASHBY_ID,
                "title": "Product Designer",
                "descriptionHtml": "<h2>Design things</h2><p>Make it pretty.</p>",
                "location": "Remote - Europe",
            },
            {
                "id": str(uuid.uuid4()),
                "title": "Yet Another Role",
                "descriptionHtml": "<p>Nope.</p>",
                "location": "Oslo",
            },
        ]
    }


def test_ashby_capture_finds_the_job_by_uuid_in_the_board_response():
    recorder = Recorder({ASHBY_BOARD: ashby_board()})
    with client_for(recorder) as client:
        result = capture(client, ref_of(ASHBY_URL))
    assert result.title == "Product Designer"
    assert result.location == "Remote - Europe"
    assert "## Design things" in result.jd_markdown
    assert "Make it pretty." in result.jd_markdown
    assert "Not this one." not in result.jd_markdown


def test_ashby_capture_makes_one_board_request_and_no_per_id_request():
    recorder = Recorder({ASHBY_BOARD: ashby_board()})
    with client_for(recorder) as client:
        capture(client, ref_of(ASHBY_URL))
    assert [(r.method, r.url.host, r.url.path) for r in recorder.requests] == [
        ("GET", *ASHBY_BOARD)
    ]
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


def test_ashby_capture_fails_when_the_job_is_not_on_the_board():
    board = ashby_board()
    board["jobs"] = [job for job in board["jobs"] if job["id"] != ASHBY_ID]
    recorder = Recorder({ASHBY_BOARD: board})
    with client_for(recorder) as client, pytest.raises(CaptureError):
        capture(client, ref_of(ASHBY_URL))


# --- capture: Rippling -------------------------------------------------------

RIPPLING_URL = f"https://ats.rippling.com/acme-widgets/jobs/{RIPPLING_ID}"
RIPPLING_DETAIL = (
    "ats.rippling.com",
    f"/api/v2/board/acme-widgets/jobs/{RIPPLING_ID}",
)
RIPPLING_LIST_PATH = "/api/v2/board/acme-widgets/jobs"


def rippling_record(**overrides):
    record = {
        "name": "Support Lead",
        "title": "Fallback Title",
        "description": {
            "role": "<h2>The role</h2><p>Lead support.</p>",
            "company": "<h2>The company</h2><p>We make widgets.</p>",
        },
        "workLocations": ["Austin, TX", "Remote"],
    }
    record = {**record, **overrides}
    return {key: value for key, value in record.items() if value is not None}


def test_rippling_capture_renders_role_before_company():
    recorder = Recorder({RIPPLING_DETAIL: rippling_record()})
    with client_for(recorder) as client:
        result = capture(client, ref_of(RIPPLING_URL))
    markdown = result.jd_markdown
    assert "## The role" in markdown and "## The company" in markdown
    assert markdown.index("Lead support.") < markdown.index("We make widgets.")
    assert markdown.index("## The role") < markdown.index("## The company")
    assert result.title == "Support Lead"
    assert "Austin, TX" in result.location and "Remote" in result.location


def test_rippling_title_falls_back_to_title_when_name_is_absent():
    recorder = Recorder({RIPPLING_DETAIL: rippling_record(name=None)})
    with client_for(recorder) as client:
        result = capture(client, ref_of(RIPPLING_URL))
    assert result.title == "Fallback Title"


def test_rippling_detail_is_requested_with_get():
    recorder = Recorder({RIPPLING_DETAIL: rippling_record()})
    with client_for(recorder) as client:
        capture(client, ref_of(RIPPLING_URL))
    assert [(r.method, r.url.host, r.url.path) for r in recorder.requests] == [
        ("GET", *RIPPLING_DETAIL)
    ]
    assert recorder.requests[0].headers["user-agent"] == USER_AGENT


@pytest.mark.parametrize("status", [404, 500])
def test_rippling_failed_detail_fails_capture_without_requesting_the_list(status):
    list_response = {
        "items": [{"id": RIPPLING_ID, "name": "Support Lead", "url": RIPPLING_URL}]
    }
    recorder = Recorder(
        {
            RIPPLING_DETAIL: httpx.Response(status, json={"error": "nope"}),
            ("ats.rippling.com", RIPPLING_LIST_PATH): list_response,
        }
    )
    with client_for(recorder) as client, pytest.raises(CaptureError):
        capture(client, ref_of(RIPPLING_URL))
    assert RIPPLING_LIST_PATH not in recorder.paths
    assert len(recorder.requests) == 1


# --- capture: failures and shared conversion ---------------------------------


def test_unparseable_detail_response_is_a_capture_error():
    recorder = Recorder({GH_DETAIL: httpx.Response(200, content=b"<html>not json")})
    with client_for(recorder) as client, pytest.raises(CaptureError):
        capture(client, ref_of(GH_URL))


def test_http_error_status_is_a_capture_error():
    recorder = Recorder({GH_DETAIL: httpx.Response(500, json={})})
    with client_for(recorder) as client, pytest.raises(CaptureError):
        capture(client, ref_of(GH_URL))


def test_capture_error_is_raised_through_the_shared_error_type():
    from jsa.errors import JsaError

    assert issubclass(CaptureError, JsaError)


def test_html_to_markdown_uses_atx_headings():
    markdown = capture_module.html_to_markdown(
        "<h1>One</h1><p>a</p><h2>Two</h2><p>b</p><h3>Three</h3>"
    )
    assert headings(markdown) == ["# One", "## Two", "### Three"]
    assert "====" not in markdown and "----" not in markdown


@pytest.mark.parametrize(
    ("value", "expected_parts"),
    [
        ("Berlin, Germany", ["Berlin, Germany"]),
        ({"name": "Remote, US"}, ["Remote, US"]),
        (["Austin, TX", "Remote"], ["Austin, TX", "Remote"]),
    ],
)
def test_normalize_location_handles_string_dict_and_list(value, expected_parts):
    result = normalize_location(value)
    assert isinstance(result, str)
    for part in expected_parts:
        assert part in result


def test_normalize_location_of_nothing_is_none():
    assert normalize_location(None) is None


def test_title_is_the_platforms_text_not_a_markdown_rendering():
    recorder = Recorder({GH_DETAIL: greenhouse_record(title="R&D Engineer (Staff)")})
    with client_for(recorder) as client:
        result = capture(client, ref_of(GH_URL))
    assert result.title == "R&D Engineer (Staff)"
