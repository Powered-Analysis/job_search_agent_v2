import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from conftest import drop_all_tables, unique_url

from jsa import db
from jsa.ats import AtsRef, resolve_ats
from jsa.http import make_client
from jsa.verify import Page, Verifier, classify, recheck

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
WINDOW_START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
GH_URL = "https://job-boards.greenhouse.io/acme/jobs/4001"
LEVER_URL = "https://jobs.lever.co/acme/3f2a9c1e-7b4d-4e8a-9c56-1d0e2f3a4b5c"
ASHBY_URL = "https://jobs.ashbyhq.com/acme/8d7c6b5a-4f3e-4d2c-b1a0-9e8f7a6b5c4d"
RIPPLING_URL = "https://ats.rippling.com/acme/jobs/c4b5a697-8f0e-4a1b-8c2d-3e4f5a6b7c8d"
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/3999"
OFF_FOUR_URL = "https://careers.example.com/jobs/senior-engineer-77"
GH_EMPLOYER_URL = "https://careers.example.com/open-roles?gh_jid=4001"
KINDS = {
    "greenhouse": "updated",
    "lever": "created",
    "ashby": "published",
    "rippling": "created",
}
ATS_URLS = {
    "greenhouse": GH_URL,
    "lever": LEVER_URL,
    "ashby": ASHBY_URL,
    "rippling": RIPPLING_URL,
}


def ref_of(url):
    ref = resolve_ats(url)
    assert ref is not None, url
    return ref


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def epoch_ms(moment):
    return int(moment.timestamp() * 1000)


def raw_date(platform, moment):
    """The platform's own wire form of a timestamp."""
    return epoch_ms(moment) if platform == "lever" else iso(moment)


def classify_ats(platform, *, date=None, window=WINDOW_START, mode="strict", **extra):
    """Classify a posting listed on its board, with `date` as its platform timestamp."""
    ref = ref_of(ATS_URLS[platform])
    index = {ref.job_id: None if platform == "rippling" else date}
    detail = {"createdOn": date} if platform == "rippling" else None
    return classify(
        ATS_URLS[platform],
        mode,
        ref,
        index=extra.pop("index", index),
        detail=extra.pop("detail", detail),
        window_start=window,
        now=NOW,
        **extra,
    )


def page_html(posting=None):
    if posting is None:
        return "<html><body><h1>Senior Engineer</h1></body></html>"
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps({'@context': 'https://schema.org', '@type': 'JobPosting', 'title': 'Senior Engineer', **posting})}"
        "</script></head><body>Apply now</body></html>"
    )


def open_page(posting=None, url=OFF_FOUR_URL):
    return Page(200, url, False, page_html(posting))


def classify_page(page, *, window=WINDOW_START, mode="best_effort", url=OFF_FOUR_URL):
    return classify(url, mode, None, page=page, window_start=window, now=NOW)


# --- classification: ordering and the dropped outcomes --------------------------


@pytest.mark.parametrize("mode", ["strict", "best_effort"])
def test_an_aggregator_url_is_dropped_before_any_other_outcome(mode):
    # Nothing was fetched, which would otherwise read as unverifiable.
    verdict = classify(LINKEDIN_URL, mode, None, now=NOW, window_start=WINDOW_START)
    assert verdict.outcome == "aggregator"


@pytest.mark.parametrize("mode", ["strict", "best_effort"])
def test_an_aggregator_is_dropped_even_when_its_page_looks_open(mode):
    verdict = classify(
        LINKEDIN_URL, mode, None, page=open_page(url=LINKEDIN_URL), now=NOW
    )
    assert verdict.outcome == "aggregator"


@pytest.mark.parametrize("url", [OFF_FOUR_URL, GH_EMPLOYER_URL])
def test_under_strict_a_url_that_resolves_to_none_of_the_four_is_unsupported(url):
    assert resolve_ats(url) is None
    verdict = classify(url, "strict", None, page=open_page(), now=NOW)
    assert verdict.outcome == "unsupported"


def test_under_best_effort_the_same_url_gets_the_page_check():
    verdict = classify(OFF_FOUR_URL, "best_effort", None, page=open_page(), now=NOW)
    assert verdict.outcome == "reachable_no_date"


def test_an_employer_domain_greenhouse_url_gets_the_page_check_under_best_effort():
    page = open_page(url=GH_EMPLOYER_URL)
    verdict = classify(GH_EMPLOYER_URL, "best_effort", None, page=page, now=NOW)
    assert verdict.outcome == "reachable_no_date"


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_a_failed_index_fetch_makes_the_posting_unverifiable(platform):
    verdict = classify_ats(platform, index=None)
    assert verdict.outcome == "unverifiable"


def test_a_failed_rippling_detail_fetch_makes_the_posting_unverifiable():
    verdict = classify_ats("rippling", detail=None)
    assert verdict.outcome == "unverifiable"


def test_a_failed_page_fetch_makes_an_off_four_posting_unverifiable():
    assert classify_page(None).outcome == "unverifiable"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_a_non_2xx_page_that_is_not_a_closed_signal_is_unverifiable(status):
    page = Page(status, OFF_FOUR_URL, False, "blocked")
    assert classify_page(page).outcome == "unverifiable"


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_a_job_id_missing_from_the_fetched_index_is_not_on_index(platform):
    verdict = classify_ats(platform, index={"some-other-job": None})
    assert verdict.outcome == "not_on_index"


def test_an_empty_fetched_index_is_not_on_index_and_not_unverifiable():
    assert classify_ats("greenhouse", index={}).outcome == "not_on_index"


@pytest.mark.parametrize("status", [404, 410])
def test_a_gone_page_is_page_closed(status):
    page = Page(status, OFF_FOUR_URL, False, "gone")
    assert classify_page(page).outcome == "page_closed"


def test_a_redirect_that_loses_the_last_path_segment_is_page_closed():
    page = Page(200, "https://careers.example.com/", True, page_html())
    assert classify_page(page).outcome == "page_closed"


def test_a_redirect_to_a_search_page_is_page_closed():
    page = Page(200, "https://careers.example.com/search?q=eng", True, page_html())
    assert classify_page(page).outcome == "page_closed"


def test_a_redirect_that_keeps_the_last_path_segment_stays_open():
    page = Page(
        200,
        "https://careers.example.org/en/jobs/senior-engineer-77",
        True,
        page_html(),
    )
    assert classify_page(page).outcome == "reachable_no_date"


def test_a_past_valid_through_is_page_closed():
    page = open_page({"validThrough": "2026-09-01T00:00:00Z"})
    assert classify_page(page).outcome == "page_closed"


def test_a_future_valid_through_does_not_close_the_page():
    page = open_page({"validThrough": "2026-12-31T00:00:00Z"})
    assert classify_page(page).outcome != "page_closed"


def test_a_bare_date_valid_through_is_valid_through_the_end_of_that_day():
    # NOW is 2026-10-03 12:00 UTC: that day is still running.
    page = open_page({"validThrough": "2026-10-03"})
    assert classify_page(page).outcome != "page_closed"


def test_a_bare_date_valid_through_closes_once_that_day_has_passed():
    page = open_page({"validThrough": "2026-10-02"})
    assert classify_page(page).outcome == "page_closed"


def test_a_bare_date_valid_through_is_judged_in_utc():
    just_after_midnight = datetime(2026, 10, 4, 0, 0, 1, tzinfo=UTC)
    page = open_page({"validThrough": "2026-10-03"})
    verdict = classify(
        OFF_FOUR_URL, "best_effort", None, page=page, now=just_after_midnight
    )
    assert verdict.outcome == "page_closed"


def test_page_closed_is_reported_only_under_best_effort():
    page = Page(404, OFF_FOUR_URL, False, "gone")
    assert classify(OFF_FOUR_URL, "strict", None, page=page, now=NOW).outcome == (
        "unsupported"
    )


# --- classification: window test and dates ---------------------------------------


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_a_timestamp_exactly_24_hours_before_the_window_start_passes(platform):
    moment = WINDOW_START - timedelta(hours=24)
    verdict = classify_ats(platform, date=raw_date(platform, moment))
    assert verdict.outcome == "verified"


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_a_timestamp_one_second_earlier_is_out_of_window(platform):
    moment = WINDOW_START - timedelta(hours=24, seconds=1)
    verdict = classify_ats(platform, date=raw_date(platform, moment))
    assert verdict.outcome == "out_of_window"


def test_out_of_window_still_reports_the_date_and_kind():
    moment = WINDOW_START - timedelta(days=30)
    verdict = classify_ats("greenhouse", date=iso(moment))
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "updated"
    assert datetime.fromisoformat(verdict.ats_date) == moment


def test_a_timestamp_inside_the_window_is_verified():
    verdict = classify_ats("greenhouse", date=iso(WINDOW_START + timedelta(hours=1)))
    assert verdict.outcome == "verified"


def test_a_timestamp_in_the_future_is_verified():
    verdict = classify_ats("ashby", date=iso(NOW + timedelta(days=1)))
    assert verdict.outcome == "verified"


def test_without_a_window_an_old_timestamp_is_still_verified():
    verdict = classify_ats(
        "greenhouse", date=iso(datetime(2019, 1, 1, tzinfo=UTC)), window=None
    )
    assert verdict.outcome == "verified"


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_an_open_posting_with_no_timestamp_is_verified_no_date(platform):
    verdict = classify_ats(platform, date=None)
    assert verdict.outcome == "verified_no_date"
    assert verdict.ats_date is None


@pytest.mark.parametrize("platform", ["greenhouse", "ashby", "rippling"])
def test_an_unparseable_timestamp_counts_as_no_usable_timestamp(platform):
    verdict = classify_ats(platform, date="sometime last week")
    assert verdict.outcome == "verified_no_date"


@pytest.mark.parametrize("platform", list(ATS_URLS))
def test_ats_date_kind_follows_the_platform(platform):
    verdict = classify_ats(platform, date=raw_date(platform, NOW))
    assert verdict.ats_date_kind == KINDS[platform]


def test_a_lever_epoch_millisecond_timestamp_is_stored_as_utc_iso_8601():
    moment = datetime(2026, 10, 2, 15, 30, 45, 123000, tzinfo=UTC)
    verdict = classify_ats("lever", date=epoch_ms(moment))
    assert verdict.ats_date.endswith("Z")
    assert datetime.fromisoformat(verdict.ats_date) == moment


def test_a_timestamp_with_an_offset_is_stored_as_utc():
    local = datetime(2026, 10, 2, 20, 0, tzinfo=timezone(timedelta(hours=5)))
    verdict = classify_ats("greenhouse", date=local.isoformat())
    assert datetime.fromisoformat(verdict.ats_date) == local
    assert verdict.ats_date.endswith("Z")


def test_an_open_off_four_page_with_an_in_window_date_posted_is_reachable():
    page = open_page({"datePosted": "2026-10-03"})
    verdict = classify_page(page)
    assert verdict.outcome == "reachable"
    assert verdict.ats_date_kind == "published"
    assert verdict.ats_date.startswith("2026-10-03")


def test_an_open_off_four_page_without_date_posted_is_reachable_no_date():
    verdict = classify_page(open_page({"title": "No date here"}))
    assert verdict.outcome == "reachable_no_date"


def test_an_off_four_page_with_no_job_posting_data_is_reachable_no_date():
    assert classify_page(open_page()).outcome == "reachable_no_date"


def test_an_off_four_page_with_an_old_date_posted_is_out_of_window():
    verdict = classify_page(open_page({"datePosted": "2026-08-01"}))
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "published"


def test_the_page_window_test_has_the_same_24_hour_slack():
    on_edge = open_page({"datePosted": "2026-10-01T12:00:00Z"})
    past_edge = open_page({"datePosted": "2026-10-01T11:59:59Z"})
    assert classify_page(on_edge).outcome == "reachable"
    assert classify_page(past_edge).outcome == "out_of_window"


def test_a_closed_signal_outranks_the_window_test():
    page = open_page({"datePosted": "2020-01-01", "validThrough": "2026-09-01"})
    assert classify_page(page).outcome == "page_closed"


def test_not_on_index_outranks_the_window_test():
    verdict = classify_ats("greenhouse", index={"1": iso(NOW)})
    assert verdict.outcome == "not_on_index"


def test_classification_is_deterministic_and_makes_no_network_or_database_call(
    monkeypatch,
):
    def refuse(*_args, **_kwargs):
        raise AssertionError("classification reached outside the function")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    monkeypatch.setattr(httpx.Client, "send", refuse)
    monkeypatch.setattr(db, "connect", refuse)
    first = classify_ats("lever", date=epoch_ms(NOW))
    second = classify_ats("lever", date=epoch_ms(NOW))
    assert first == second
    assert classify_page(open_page({"datePosted": "2026-10-03"})).outcome == "reachable"


# --- the fetches: one index per board --------------------------------------------


class Web:
    """A mock transport that answers from a route table and records every request.

    A route key is `(host, path)`; a value is JSON, an `httpx.Response`, or a
    callable taking the request. Unrouted requests are 404.
    """

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.routes.get((request.url.host, request.url.path))
        if callable(answer):
            return answer(request)
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def client(self):
        return make_client(httpx.MockTransport(self))

    def count(self, host, path=None):
        return sum(
            1
            for request in self.requests
            if request.url.host == host and path in (None, request.url.path)
        )

    @property
    def hosts(self):
        return [request.url.host for request in self.requests]


def greenhouse_board(*jobs):
    return {
        ("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): {
            "jobs": [{"id": job_id, "updated_at": updated} for job_id, updated in jobs]
        }
    }


def check(web, url, **kwargs):
    kwargs.setdefault("mode", "strict")
    return Verifier(web.client()).check(url, **kwargs).verdict


def test_greenhouse_is_checked_against_its_board_index_with_the_updated_kind():
    web = Web(greenhouse_board((4001, iso(NOW))))
    verdict = check(web, GH_URL, window_start=WINDOW_START)
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "updated"
    assert web.hosts == ["boards-api.greenhouse.io"]
    assert web.requests[0].url.path == "/v1/boards/acme/jobs"
    assert web.requests[0].method == "GET"


def test_greenhouse_job_missing_from_the_index_is_not_on_index():
    web = Web(greenhouse_board((9999, iso(NOW))))
    assert check(web, GH_URL, window_start=WINDOW_START).outcome == "not_on_index"


def test_lever_is_checked_against_its_postings_list_with_the_created_kind():
    lever_id = ref_of(LEVER_URL).job_id
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): [
                {"id": lever_id, "createdAt": epoch_ms(NOW)}
            ]
        }
    )
    verdict = check(web, LEVER_URL, window_start=WINDOW_START)
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "created"
    assert datetime.fromisoformat(verdict.ats_date) == NOW
    assert web.requests[0].url.params["mode"] == "json"


def test_ashby_is_checked_against_its_job_board_with_the_published_kind():
    ashby_id = ref_of(ASHBY_URL).job_id
    web = Web(
        {
            ("api.ashbyhq.com", "/posting-api/job-board/acme"): {
                "jobs": [{"id": ashby_id, "publishedAt": iso(NOW)}]
            }
        }
    )
    verdict = check(web, ASHBY_URL, window_start=WINDOW_START)
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "published"


def rippling_web(job_id, *, created=None, pages=(), detail=None):
    """A Rippling board whose list is `pages` (each a list of job ids)."""
    routes = {}
    pages = pages or ([job_id],)

    def listing(request):
        number = int(request.url.params.get("page", 0))
        return httpx.Response(
            200,
            json={
                "items": [{"id": i} for i in pages[number]],
                "page": number,
                "pageSize": 20,
                "totalItems": sum(len(p) for p in pages),
                "totalPages": len(pages),
            },
        )

    routes[("ats.rippling.com", "/api/v2/board/acme/jobs")] = listing
    routes[("ats.rippling.com", f"/api/v2/board/acme/jobs/{job_id}")] = (
        detail if detail is not None else {"createdOn": created}
    )
    return Web(routes)


def test_rippling_presence_comes_from_the_list_and_the_date_from_the_detail():
    job_id = ref_of(RIPPLING_URL).job_id
    web = rippling_web(job_id, created=iso(NOW))
    verdict = check(web, RIPPLING_URL, window_start=WINDOW_START)
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "created"
    assert datetime.fromisoformat(verdict.ats_date) == NOW


def test_rippling_follows_the_paginated_list_to_find_a_job_on_a_later_page():
    job_id = ref_of(RIPPLING_URL).job_id
    web = rippling_web(job_id, created=iso(NOW), pages=(["a", "b"], ["c"], [job_id]))
    assert check(web, RIPPLING_URL, window_start=WINDOW_START).outcome == "verified"


def test_rippling_job_absent_from_every_page_is_not_on_index():
    job_id = ref_of(RIPPLING_URL).job_id
    web = rippling_web(job_id, created=iso(NOW), pages=(["a"], ["b"]))
    assert check(web, RIPPLING_URL, window_start=WINDOW_START).outcome == "not_on_index"


def test_rippling_detail_is_fetched_only_for_the_posting_checked():
    job_id = ref_of(RIPPLING_URL).job_id
    web = rippling_web(job_id, created=iso(NOW), pages=(["x", "y", job_id],))
    check(web, RIPPLING_URL, window_start=WINDOW_START)
    detail_paths = [
        r.url.path
        for r in web.requests
        if r.url.path.startswith("/api/v2/board/acme/jobs/")
    ]
    assert detail_paths == [f"/api/v2/board/acme/jobs/{job_id}"]


def test_a_rippling_detail_failure_is_unverifiable_for_the_window_test():
    job_id = ref_of(RIPPLING_URL).job_id
    web = rippling_web(job_id, detail=httpx.Response(500))
    verdict = check(web, RIPPLING_URL, window_start=WINDOW_START)
    assert verdict.outcome == "unverifiable"


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(500), id="5xx"),
        pytest.param(httpx.Response(503), id="503"),
        pytest.param(httpx.Response(403), id="403"),
        pytest.param(httpx.Response(429), id="429"),
        pytest.param(httpx.Response(404), id="404"),
        pytest.param(
            httpx.Response(200, text="<html>not json</html>"), id="unparseable"
        ),
        pytest.param(
            httpx.Response(200, json={"unexpected": "shape"}), id="wrong-shape"
        ),
        pytest.param(httpx.Response(200, json="a string"), id="not-an-object"),
    ],
)
def test_a_greenhouse_index_that_cannot_be_read_makes_the_posting_unverifiable(
    response,
):
    web = Web({("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): response})
    assert check(web, GH_URL, window_start=WINDOW_START).outcome == "unverifiable"


def test_a_greenhouse_index_timeout_makes_the_posting_unverifiable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    web = Web({("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): timeout})
    assert check(web, GH_URL, window_start=WINDOW_START).outcome == "unverifiable"


def test_an_index_connection_failure_makes_the_posting_unverifiable():
    def refuse(request):
        raise httpx.ConnectError("offline", request=request)

    web = Web({("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): refuse})
    assert check(web, GH_URL, window_start=WINDOW_START).outcome == "unverifiable"


def test_an_unreadable_index_makes_every_posting_on_that_board_unverifiable():
    web = Web(
        {("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): httpx.Response(503)}
    )
    verifier = Verifier(web.client())
    urls = [f"https://job-boards.greenhouse.io/acme/jobs/{n}" for n in (1, 2, 3)]
    outcomes = [
        verifier.check(url, mode="strict", window_start=WINDOW_START).verdict.outcome
        for url in urls
    ]
    assert outcomes == ["unverifiable"] * 3


def test_a_blocked_index_is_not_read_as_a_closed_job():
    web = Web(
        {("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): httpx.Response(429)}
    )
    verdict = check(web, GH_URL, window_start=WINDOW_START)
    assert verdict.outcome not in {"not_on_index", "page_closed"}


def test_several_postings_on_the_same_board_fetch_the_index_once():
    web = Web(greenhouse_board((1, iso(NOW)), (2, iso(NOW)), (3, iso(NOW))))
    verifier = Verifier(web.client())
    for number in (1, 2, 3, 4):
        verifier.check(
            f"https://job-boards.greenhouse.io/acme/jobs/{number}",
            mode="strict",
            window_start=WINDOW_START,
        )
    assert web.count("boards-api.greenhouse.io", "/v1/boards/acme/jobs") == 1


def test_a_failed_index_fetch_is_not_retried_for_the_next_posting_on_the_board():
    web = Web(
        {("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): httpx.Response(500)}
    )
    verifier = Verifier(web.client())
    for number in (1, 2):
        verifier.check(
            f"https://job-boards.greenhouse.io/acme/jobs/{number}", mode="strict"
        )
    assert web.count("boards-api.greenhouse.io") == 1


def test_different_boards_are_fetched_separately():
    web = Web(
        {
            ("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): {"jobs": []},
            ("boards-api.greenhouse.io", "/v1/boards/globex/jobs"): {"jobs": []},
        }
    )
    verifier = Verifier(web.client())
    for board in ("acme", "globex", "acme", "globex"):
        verifier.check(
            f"https://job-boards.greenhouse.io/{board}/jobs/1", mode="strict"
        )
    assert web.count("boards-api.greenhouse.io") == 2


def test_a_posting_the_verifier_resolves_to_none_of_the_four_makes_no_ats_call_in_strict():
    web = Web()
    verdict = check(web, OFF_FOUR_URL, window_start=WINDOW_START)
    assert verdict.outcome == "unsupported"
    assert web.requests == []


def test_an_aggregator_url_makes_no_request_in_either_mode():
    web = Web()
    for mode in ("strict", "best_effort"):
        assert check(web, LINKEDIN_URL, mode=mode).outcome == "aggregator"
    assert web.requests == []


def test_requests_are_plain_and_unauthenticated():
    web = Web(greenhouse_board((4001, iso(NOW))))
    check(web, GH_URL, window_start=WINDOW_START)
    for request in web.requests:
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers


# --- Lever's EU retry --------------------------------------------------------------


def lever_job():
    return [{"id": ref_of(LEVER_URL).job_id, "createdAt": epoch_ms(NOW)}]


@pytest.mark.parametrize("failure", [500, 404, 403])
def test_the_lever_index_retries_on_the_eu_host_after_an_http_error(failure):
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): httpx.Response(failure),
            ("api.eu.lever.co", "/v0/postings/acme"): lever_job(),
        }
    )
    verdict = check(web, LEVER_URL, window_start=WINDOW_START)
    assert verdict.outcome == "verified"
    assert web.hosts == ["api.lever.co", "api.eu.lever.co"]


def test_the_lever_index_does_not_use_the_eu_host_when_the_first_answers():
    web = Web({("api.lever.co", "/v0/postings/acme"): lever_job()})
    check(web, LEVER_URL, window_start=WINDOW_START)
    assert web.hosts == ["api.lever.co"]


def test_a_lever_board_failing_on_both_hosts_is_unverifiable():
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): httpx.Response(500),
            ("api.eu.lever.co", "/v0/postings/acme"): httpx.Response(500),
        }
    )
    assert check(web, LEVER_URL, window_start=WINDOW_START).outcome == "unverifiable"
    assert web.hosts == ["api.lever.co", "api.eu.lever.co"]


def test_the_lever_eu_retry_keeps_the_same_path_and_query():
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): httpx.Response(500),
            ("api.eu.lever.co", "/v0/postings/acme"): lever_job(),
        }
    )
    check(web, LEVER_URL, window_start=WINDOW_START)
    first, second = web.requests
    assert first.url.path == second.url.path
    assert second.url.params["mode"] == "json"


def test_the_lever_eu_board_is_fetched_once_for_several_postings():
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): httpx.Response(500),
            ("api.eu.lever.co", "/v0/postings/acme"): lever_job(),
        }
    )
    verifier = Verifier(web.client())
    for _ in range(3):
        verifier.check(LEVER_URL, mode="strict")
    assert web.hosts == ["api.lever.co", "api.eu.lever.co"]


# --- the page check, end to end ----------------------------------------------------


def page_web(response):
    return Web({(httpx.URL(OFF_FOUR_URL).host, httpx.URL(OFF_FOUR_URL).path): response})


def html_response(posting=None, status=200):
    return httpx.Response(status, html=page_html(posting))


def test_the_page_check_is_one_get_of_the_url():
    web = page_web(html_response({"datePosted": iso(NOW)}))
    verdict = check(web, OFF_FOUR_URL, mode="best_effort", window_start=WINDOW_START)
    assert verdict.outcome == "reachable"
    assert [r.method for r in web.requests] == ["GET"]
    assert str(web.requests[0].url) == OFF_FOUR_URL


@pytest.mark.parametrize("status", [404, 410])
def test_a_404_or_410_page_is_page_closed_end_to_end(status):
    web = page_web(httpx.Response(status))
    assert check(web, OFF_FOUR_URL, mode="best_effort").outcome == "page_closed"


@pytest.mark.parametrize("status", [403, 429, 500])
def test_a_blocked_or_failing_page_is_unverifiable_end_to_end(status):
    web = page_web(httpx.Response(status))
    assert check(web, OFF_FOUR_URL, mode="best_effort").outcome == "unverifiable"


def test_a_page_that_times_out_is_unverifiable():
    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    assert check(page_web(timeout), OFF_FOUR_URL, mode="best_effort").outcome == (
        "unverifiable"
    )


def test_the_page_check_follows_redirects_and_a_redirect_to_the_careers_home_is_closed():
    web = Web(
        {
            ("careers.example.com", "/jobs/senior-engineer-77"): httpx.Response(
                302, headers={"location": "https://careers.example.com/"}
            ),
            ("careers.example.com", "/"): html_response(),
        }
    )
    assert check(web, OFF_FOUR_URL, mode="best_effort").outcome == "page_closed"


def test_a_redirect_that_still_names_the_job_is_not_closed():
    web = Web(
        {
            ("careers.example.com", "/jobs/senior-engineer-77"): httpx.Response(
                301,
                headers={
                    "location": "https://careers.example.com/en/jobs/senior-engineer-77"
                },
            ),
            ("careers.example.com", "/en/jobs/senior-engineer-77"): html_response(),
        }
    )
    assert check(web, OFF_FOUR_URL, mode="best_effort").outcome == "reachable_no_date"


def test_a_live_page_with_a_space_or_non_ascii_path_segment_is_not_closed():
    url = "https://careers.example.com/jobs/Senior%20Ingénieur"
    web = Web({("careers.example.com", "/jobs/Senior Ingénieur"): html_response()})
    assert check(web, url, mode="best_effort").outcome == "reachable_no_date"


def test_the_fetched_page_is_kept_for_capture():
    web = page_web(
        html_response({"datePosted": iso(NOW), "description": "<p>Build</p>"})
    )
    checked = Verifier(web.client()).check(
        OFF_FOUR_URL, mode="best_effort", window_start=WINDOW_START
    )
    assert checked.page is not None
    assert checked.page.status == 200
    assert "Build" in checked.page.html


def test_an_off_four_page_under_strict_is_never_fetched():
    web = page_web(html_response())
    assert check(web, OFF_FOUR_URL, mode="strict").outcome == "unsupported"
    assert web.requests == []


# --- re-check of stored postings ----------------------------------------------------


@pytest.fixture
def vdb(db_url):
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


def store(conn, url, *, decision=None, agent="claude"):
    posting_id = db.insert_posting(
        conn, company="Acme", title="Engineer", url=url, search_agent=agent
    )
    if decision:
        conn.execute(
            "UPDATE postings SET decision = ?, decided_at = ? WHERE id = ?",
            (decision, "2026-01-01T00:00:00.000Z", posting_id),
        )
    return posting_id


def state(conn, posting_id):
    found = conn.execute(
        "SELECT decision, closed_at FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()
    return {"decision": found[0], "closed_at": found[1]}


def finding_count(conn):
    return conn.execute("SELECT COUNT(*) FROM search_findings").fetchone()[0]


def test_mark_closed_sets_closed_at_in_the_stored_timestamp_format(vdb):
    posting_id = store(vdb, unique_url())
    db.mark_closed(vdb, posting_id)
    closed_at = state(vdb, posting_id)["closed_at"]
    assert closed_at is not None
    parsed = datetime.strptime(closed_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs(datetime.now(UTC) - parsed) < timedelta(minutes=5)


@pytest.mark.parametrize("decision", [None, "Apply", "Skip"])
def test_mark_closed_never_touches_the_decision(vdb, decision):
    posting_id = store(vdb, unique_url(), decision=decision)
    db.mark_closed(vdb, posting_id)
    assert state(vdb, posting_id)["decision"] == decision
    assert state(vdb, posting_id)["closed_at"] is not None


def test_mark_closed_leaves_other_postings_alone(vdb):
    closed, other = store(vdb, unique_url()), store(vdb, unique_url())
    db.mark_closed(vdb, closed)
    assert state(vdb, other)["closed_at"] is None


def test_closed_is_final_a_re_found_posting_no_ops_on_insert(vdb):
    url = unique_url()
    posting_id = store(vdb, url)
    db.mark_closed(vdb, posting_id)
    again = db.insert_posting(
        vdb, company="Acme", title="Engineer", url=url, search_agent="claude"
    )
    assert again is None
    assert state(vdb, posting_id)["closed_at"] is not None


def test_a_closed_posting_leaves_the_review_backlog_with_no_decision(vdb):
    posting_id = store(vdb, unique_url())
    other = store(vdb, unique_url())
    db.mark_closed(vdb, posting_id)
    ids = [row[0] for row in db.review_backlog(vdb)]
    assert posting_id not in ids
    assert other in ids
    assert state(vdb, posting_id)["decision"] is None


def stored_gh(conn, job_id, **kwargs):
    url = f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"
    return store(conn, url, **kwargs), url


def test_recheck_marks_a_posting_not_on_the_index_closed(vdb):
    posting_id, url = stored_gh(vdb, 111)
    web = Web(greenhouse_board((222, iso(NOW))))
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "not_on_index"
    assert state(vdb, posting_id)["closed_at"] is not None


def test_recheck_leaves_an_open_posting_open(vdb):
    posting_id, url = stored_gh(vdb, 111)
    web = Web(greenhouse_board((111, iso(NOW))))
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "verified"
    assert state(vdb, posting_id)["closed_at"] is None


def test_recheck_leaves_an_unverifiable_posting_untouched(vdb):
    posting_id, url = stored_gh(vdb, 111, decision="Apply")
    web = Web(
        {("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): httpx.Response(503)}
    )
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "unverifiable"
    assert state(vdb, posting_id) == {"decision": "Apply", "closed_at": None}


@pytest.mark.parametrize("decision", [None, "Apply", "Skip"])
def test_recheck_leaves_the_decision_unchanged_when_it_closes_a_posting(vdb, decision):
    posting_id, url = stored_gh(vdb, 111, decision=decision)
    web = Web(greenhouse_board())
    recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert state(vdb, posting_id)["decision"] == decision
    assert state(vdb, posting_id)["closed_at"] is not None


def test_recheck_marks_an_off_four_page_closed_on_a_404(vdb):
    url = unique_url("https://careers.example.com/jobs/")
    posting_id = store(vdb, url)
    web = Web()  # every unrouted request is a 404
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "page_closed"
    assert state(vdb, posting_id)["closed_at"] is not None


def test_recheck_does_not_close_on_a_blocked_page(vdb):
    url = unique_url("https://careers.example.com/jobs/")
    posting_id = store(vdb, url)
    host, path = httpx.URL(url).host, httpx.URL(url).path
    web = Web({(host, path): httpx.Response(403)})
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "unverifiable"
    assert state(vdb, posting_id)["closed_at"] is None


def test_recheck_applies_no_window(vdb):
    posting_id, url = stored_gh(vdb, 111)
    ancient = iso(datetime(2015, 1, 1, tzinfo=UTC))
    web = Web(greenhouse_board((111, ancient)))
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "verified"
    assert state(vdb, posting_id)["closed_at"] is None


def test_recheck_gives_each_posting_its_own_platforms_check(vdb):
    gh_id, gh_url = stored_gh(vdb, 111)
    off_url = unique_url("https://careers.example.com/jobs/")
    off_id = store(vdb, off_url)
    host, path = httpx.URL(off_url).host, httpx.URL(off_url).path
    web = Web({**greenhouse_board((111, iso(NOW))), (host, path): html_response()})
    outcomes = recheck(
        vdb, Verifier(web.client()), [(gh_id, gh_url), (off_id, off_url)]
    )
    assert outcomes == {gh_id: "verified", off_id: "reachable_no_date"}


def test_recheck_checks_an_aggregator_posting_without_closing_it(vdb):
    url = unique_url("https://www.linkedin.com/jobs/view/")
    posting_id = store(vdb, url)
    web = Web()
    outcomes = recheck(vdb, Verifier(web.client()), [(posting_id, url)])
    assert outcomes[posting_id] == "aggregator"
    assert state(vdb, posting_id)["closed_at"] is None


def test_recheck_fetches_each_board_index_once_per_session(vdb):
    postings = [stored_gh(vdb, n) for n in (1, 2, 3, 4)]
    web = Web(greenhouse_board((1, None), (2, None), (3, None)))
    outcomes = recheck(vdb, Verifier(web.client()), postings)
    assert web.count("boards-api.greenhouse.io") == 1
    assert [outcomes[pid] for pid, _ in postings] == [
        "verified_no_date",
        "verified_no_date",
        "verified_no_date",
        "not_on_index",
    ]


def test_recheck_writes_no_search_findings_row(vdb):
    closed_id, closed_url = stored_gh(vdb, 1)
    open_id, open_url = stored_gh(vdb, 2)
    web = Web(greenhouse_board((2, iso(NOW))))
    recheck(vdb, Verifier(web.client()), [(closed_id, closed_url), (open_id, open_url)])
    assert finding_count(vdb) == 0


def test_recheck_is_idempotent_for_an_already_closed_posting(vdb):
    posting_id, url = stored_gh(vdb, 1)
    web = Web(greenhouse_board())
    verifier = Verifier(web.client())
    recheck(vdb, verifier, [(posting_id, url)])
    first = state(vdb, posting_id)["closed_at"]
    recheck(vdb, verifier, [(posting_id, url)])
    assert state(vdb, posting_id)["closed_at"] == first


def test_recheck_of_nothing_makes_no_request(vdb):
    web = Web()
    assert recheck(vdb, Verifier(web.client()), []) == {}
    assert web.requests == []


def test_recheck_marks_closed_in_a_database_visible_to_another_connection(http_conn):
    # XC-8: an autocommit slip would leave the closure invisible to everyone else.
    posting_id, url = stored_gh(http_conn, 5000 + int(unique_url()[-4:], 16))
    web = Web(greenhouse_board())
    recheck(http_conn, Verifier(web.client()), [(posting_id, url)])
    other = db.connect()
    try:
        found = other.execute(
            "SELECT closed_at FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
    finally:
        other.close()
    assert found[0] is not None


def test_the_four_platform_urls_all_resolve_to_the_expected_refs():
    assert ref_of(GH_URL) == AtsRef("greenhouse", "acme", "4001")
    assert ref_of(LEVER_URL).platform == "lever"
    assert ref_of(ASHBY_URL).platform == "ashby"
    assert ref_of(RIPPLING_URL).platform == "rippling"
