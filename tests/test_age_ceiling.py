"""PRD 01 "Pipeline verification", the 60-day age ceiling (issue #170): a posting created more than 60 days before the search, plus 24h slack, is `out_of_window` however recently it was updated."""

import json
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from conftest import drop_all_tables
from profile_helpers import copy_example

from jsa import db
from jsa.ats import resolve_ats
from jsa.http import make_client
from jsa.profile import load_search_config
from jsa.search_prompt import assemble_search_prompt
from jsa.verify import Page, Verifier, classify, recheck

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
WINDOW_START = NOW - timedelta(hours=24)
# The ceiling plus the window's 24h slack (PRD 01).
CEILING_EDGE = NOW - timedelta(days=60, hours=24)
WIDE_WINDOW_START = NOW - timedelta(days=120)
GH_URL = "https://job-boards.greenhouse.io/acme/jobs/4001"
LEVER_URL = "https://jobs.lever.co/acme/3f2a9c1e-7b4d-4e8a-9c56-1d0e2f3a4b5c"
ASHBY_URL = "https://jobs.ashbyhq.com/acme/8d7c6b5a-4f3e-4d2c-b1a0-9e8f7a6b5c4d"
RIPPLING_URL = "https://ats.rippling.com/acme/jobs/c4b5a697-8f0e-4a1b-8c2d-3e4f5a6b7c8d"
OFF_FOUR_URL = "https://careers.example.com/jobs/senior-engineer-77"
SECOND = timedelta(seconds=1)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def epoch_ms(moment):
    return int(moment.timestamp() * 1000)


def parsed(stamp):
    return datetime.fromisoformat(stamp)


def greenhouse(*, created, updated, window=WINDOW_START):
    ref = resolve_ats(GH_URL)
    return classify(
        GH_URL,
        "strict",
        ref,
        index={ref.job_id: updated},
        created=created,
        window_start=window,
        now=NOW,
    )


# --- Greenhouse: the platform the ceiling binds on --------------------------------


def test_a_greenhouse_job_created_long_ago_but_updated_in_the_window_is_dropped():
    created = NOW - timedelta(days=200)
    verdict = greenhouse(created=iso(created), updated=iso(NOW - timedelta(hours=2)))
    assert verdict.outcome == "out_of_window"


def test_the_dropped_job_records_its_creation_timestamp_with_kind_created():
    created = NOW - timedelta(days=200)
    verdict = greenhouse(created=iso(created), updated=iso(NOW))
    assert verdict.ats_date_kind == "created"
    assert parsed(verdict.ats_date) == created
    assert verdict.ats_date.endswith("Z")


def test_a_greenhouse_job_created_and_updated_inside_the_window_is_verified():
    verdict = greenhouse(
        created=iso(NOW - timedelta(hours=5)), updated=iso(NOW - timedelta(hours=2))
    )
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "updated"
    assert parsed(verdict.ats_date) == NOW - timedelta(hours=2)


def test_a_greenhouse_job_created_30_days_ago_and_updated_recently_is_verified():
    verdict = greenhouse(created=iso(NOW - timedelta(days=30)), updated=iso(NOW))
    assert verdict.outcome == "verified"


def test_a_creation_exactly_60_days_and_24_hours_before_the_search_passes():
    verdict = greenhouse(created=iso(CEILING_EDGE), updated=iso(NOW))
    assert verdict.outcome == "verified"


def test_a_creation_one_second_past_that_is_dropped():
    verdict = greenhouse(created=iso(CEILING_EDGE - SECOND), updated=iso(NOW))
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "created"


def test_a_creation_61_days_ago_is_inside_the_slack_and_passes():
    verdict = greenhouse(created=iso(NOW - timedelta(days=61)), updated=iso(NOW))
    assert verdict.outcome == "verified"


def test_a_creation_with_an_offset_is_compared_as_the_same_instant_and_stored_as_utc():
    created = datetime(2026, 6, 1, 8, 0, tzinfo=UTC)
    local = created.astimezone(timezone(timedelta(hours=5)))
    verdict = greenhouse(created=local.isoformat(), updated=iso(NOW))
    assert verdict.outcome == "out_of_window"
    assert parsed(verdict.ats_date) == created
    assert verdict.ats_date.endswith("Z")


def test_a_greenhouse_job_with_no_creation_timestamp_is_judged_by_the_window_alone():
    verdict = greenhouse(created=None, updated=iso(NOW))
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "updated"


def test_an_unparseable_creation_timestamp_skips_the_ceiling():
    verdict = greenhouse(created="a while back", updated=iso(NOW))
    assert verdict.outcome == "verified"


def test_a_missing_creation_timestamp_does_not_turn_a_dated_job_into_no_date():
    verdict = greenhouse(created=None, updated=iso(NOW))
    assert verdict.outcome != "verified_no_date"


def test_a_job_with_no_creation_and_no_recency_timestamp_is_verified_no_date():
    verdict = greenhouse(created=None, updated=None)
    assert verdict.outcome == "verified_no_date"
    assert verdict.ats_date is None


def test_without_a_window_the_ceiling_does_not_apply():
    # Manual adds, the email side door and the re-check carry no window.
    verdict = greenhouse(
        created=iso(NOW - timedelta(days=900)), updated=iso(NOW), window=None
    )
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "updated"


def test_a_job_updated_before_the_window_is_out_of_window_whatever_its_creation():
    updated = NOW - timedelta(days=10)
    verdict = greenhouse(created=iso(NOW - timedelta(days=300)), updated=iso(updated))
    assert verdict.outcome == "out_of_window"


def test_the_ceiling_does_not_override_not_on_index():
    ref = resolve_ats(GH_URL)
    verdict = classify(
        GH_URL,
        "strict",
        ref,
        index={"some-other-job": iso(NOW)},
        created=iso(NOW - timedelta(days=300)),
        window_start=WINDOW_START,
        now=NOW,
    )
    assert verdict.outcome == "not_on_index"


def test_the_ceiling_does_not_override_a_failed_index_fetch():
    ref = resolve_ats(GH_URL)
    verdict = classify(
        GH_URL, "strict", ref, index=None, window_start=WINDOW_START, now=NOW
    )
    assert verdict.outcome == "unverifiable"


# --- the other platforms: their recency timestamp is already a creation date -------

PLATFORMS = {
    "lever": (LEVER_URL, epoch_ms, "created"),
    "ashby": (ASHBY_URL, iso, "published"),
    "rippling": (RIPPLING_URL, iso, "created"),
}


def classify_other(platform, created, *, window):
    url, wire, _kind = PLATFORMS[platform]
    ref = resolve_ats(url)
    stamp = wire(created)
    return classify(
        url,
        "strict",
        ref,
        index={ref.job_id: None if platform == "rippling" else stamp},
        detail={"createdOn": stamp} if platform == "rippling" else None,
        window_start=window,
        now=NOW,
    )


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_a_posting_inside_the_window_is_unaffected_by_the_ceiling(platform):
    verdict = classify_other(platform, NOW - timedelta(hours=3), window=WINDOW_START)
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == PLATFORMS[platform][2]


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_a_posting_older_than_the_ceiling_is_dropped_when_the_window_is_wider(platform):
    created = NOW - timedelta(days=90)
    verdict = classify_other(platform, created, window=WIDE_WINDOW_START)
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "created"
    assert abs(parsed(verdict.ats_date) - created) < timedelta(seconds=1)


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_a_posting_younger_than_the_ceiling_passes_a_wide_window(platform):
    verdict = classify_other(
        platform, NOW - timedelta(days=40), window=WIDE_WINDOW_START
    )
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == PLATFORMS[platform][2]


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_the_ceiling_edge_is_the_same_on_every_platform(platform):
    on_edge = classify_other(platform, CEILING_EDGE, window=WIDE_WINDOW_START)
    past_edge = classify_other(
        platform, CEILING_EDGE - SECOND, window=WIDE_WINDOW_START
    )
    assert on_edge.outcome == "verified"
    assert past_edge.outcome == "out_of_window"


def page_of(posting):
    html = (
        '<html><head><script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "JobPosting",
                "title": "Senior Engineer",
                **posting,
            }
        )
        + "</script></head><body>Apply now</body></html>"
    )
    return Page(200, OFF_FOUR_URL, False, html)


def classify_off_four(created, *, window):
    page = page_of({"datePosted": iso(created)})
    return classify(
        OFF_FOUR_URL, "best_effort", None, page=page, window_start=window, now=NOW
    )


def test_an_off_four_page_inside_the_window_is_unaffected():
    verdict = classify_off_four(NOW - timedelta(hours=3), window=WINDOW_START)
    assert verdict.outcome == "reachable"
    assert verdict.ats_date_kind == "published"


def test_an_off_four_page_posted_over_60_days_ago_is_dropped_under_a_wide_window():
    created = NOW - timedelta(days=90)
    verdict = classify_off_four(created, window=WIDE_WINDOW_START)
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "created"
    assert abs(parsed(verdict.ats_date) - created) < timedelta(seconds=1)


def test_an_off_four_page_posted_under_60_days_ago_passes_a_wide_window():
    verdict = classify_off_four(NOW - timedelta(days=40), window=WIDE_WINDOW_START)
    assert verdict.outcome == "reachable"


def test_an_off_four_page_without_date_posted_skips_the_ceiling():
    page = page_of({"title": "No date"})
    verdict = classify(
        OFF_FOUR_URL,
        "best_effort",
        None,
        page=page,
        window_start=WIDE_WINDOW_START,
        now=NOW,
    )
    assert verdict.outcome == "reachable_no_date"


@pytest.mark.parametrize("platform", list(PLATFORMS))
def test_without_a_window_no_platform_applies_the_ceiling(platform):
    verdict = classify_other(platform, NOW - timedelta(days=900), window=None)
    assert verdict.outcome == "verified"


# --- the fetch: Greenhouse's first_published comes from the index already fetched ---


class Web:
    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        answer = self.routes.get((request.url.host, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        return httpx.Response(200, json=answer)

    def verifier(self):
        return Verifier(make_client(httpx.MockTransport(self)))


def board(*jobs):
    """Jobs are (id, updated_at, first_published); a None field is left out."""
    listing = []
    for job_id, updated, first_published in jobs:
        record = {"id": job_id}
        if updated is not None:
            record["updated_at"] = updated
        if first_published is not None:
            record["first_published"] = first_published
        listing.append(record)
    return {("boards-api.greenhouse.io", "/v1/boards/acme/jobs"): {"jobs": listing}}


def gh(job_id):
    return f"https://job-boards.greenhouse.io/acme/jobs/{job_id}"


def ago(**delta):
    return datetime.now(UTC) - timedelta(**delta)


def search_check(web, url, **kwargs):
    kwargs.setdefault("window_start", ago(hours=24))
    return web.verifier().check(url, mode="strict", **kwargs).verdict


def test_the_fetched_board_drops_an_old_creation_and_keeps_a_new_one():
    old_created = ago(days=100)
    web = Web(
        board(
            (1, iso(ago(hours=1)), iso(old_created)),
            (2, iso(ago(hours=1)), iso(ago(hours=3))),
        )
    )
    old = search_check(web, gh(1))
    new = search_check(web, gh(2))
    assert (old.outcome, old.ats_date_kind) == ("out_of_window", "created")
    assert abs(parsed(old.ats_date) - old_created) < timedelta(seconds=1)
    assert (new.outcome, new.ats_date_kind) == ("verified", "updated")


def test_each_job_is_judged_by_its_own_first_published_on_one_shared_index_fetch():
    web = Web(
        board(
            (1, iso(ago(hours=1)), iso(ago(days=100))),
            (2, iso(ago(hours=1)), iso(ago(hours=3))),
            (3, iso(ago(hours=1)), iso(ago(days=200))),
        )
    )
    verifier = web.verifier()
    window_start = ago(hours=24)
    outcomes = [
        verifier.check(gh(i), mode="strict", window_start=window_start).verdict.outcome
        for i in (1, 2, 3)
    ]
    assert outcomes == ["out_of_window", "verified", "out_of_window"]
    assert len(web.requests) == 1


def test_a_job_without_first_published_in_the_index_is_verified():
    web = Web(board((1, iso(ago(hours=1)), None)))
    verdict = search_check(web, gh(1))
    assert verdict.outcome == "verified"
    assert verdict.ats_date_kind == "updated"


def test_a_job_with_a_null_first_published_is_verified():
    routes = board((1, iso(ago(hours=1)), None))
    routes[("boards-api.greenhouse.io", "/v1/boards/acme/jobs")]["jobs"][0][
        "first_published"
    ] = None
    assert search_check(Web(routes), gh(1)).outcome == "verified"


def test_a_board_whose_jobs_all_lack_first_published_behaves_as_before():
    web = Web(board((1, iso(ago(hours=1)), None), (2, iso(ago(days=10)), None)))
    verifier = web.verifier()
    window_start = ago(hours=24)
    fresh = verifier.check(gh(1), mode="strict", window_start=window_start).verdict
    stale = verifier.check(gh(2), mode="strict", window_start=window_start).verdict
    assert (fresh.outcome, stale.outcome) == ("verified", "out_of_window")
    assert stale.ats_date_kind == "updated"


def test_without_a_window_an_old_greenhouse_job_is_still_verified():
    # `jsa add` and the email side door call the check with no window.
    web = Web(board((1, iso(ago(hours=1)), iso(ago(days=400)))))
    verdict = search_check(web, gh(1), window_start=None)
    assert verdict.outcome == "verified"


def test_a_lever_posting_created_over_60_days_ago_is_dropped_under_a_wide_window():
    ref = resolve_ats(LEVER_URL)
    created = ago(days=90)
    web = Web(
        {
            ("api.lever.co", "/v0/postings/acme"): [
                {"id": ref.job_id, "createdAt": epoch_ms(created)}
            ]
        }
    )
    verdict = search_check(web, LEVER_URL, window_start=ago(days=120))
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "created"


def test_an_ashby_posting_published_over_60_days_ago_is_dropped_under_a_wide_window():
    ref = resolve_ats(ASHBY_URL)
    web = Web(
        {
            ("api.ashbyhq.com", "/posting-api/job-board/acme"): {
                "jobs": [{"id": ref.job_id, "publishedAt": iso(ago(days=90))}]
            }
        }
    )
    verdict = search_check(web, ASHBY_URL, window_start=ago(days=120))
    assert verdict.outcome == "out_of_window"
    assert verdict.ats_date_kind == "created"


# --- the re-check carries no window, so no ceiling ----------------------------------


@pytest.fixture
def vdb(db_url):
    drop_all_tables(db_url)
    connection = db.connect()
    yield connection
    connection.close()


def test_recheck_keeps_open_a_posting_created_long_ago(vdb):
    url = gh(111)
    posting_id = db.insert_posting(
        vdb, company="Acme", title="Engineer", url=url, search_agent="claude"
    )
    web = Web(board((111, iso(ago(hours=1)), iso(ago(days=400)))))
    outcomes = recheck(vdb, web.verifier(), [(posting_id, url)])
    assert outcomes[posting_id] == "verified"
    closed = vdb.execute(
        "SELECT closed_at FROM postings WHERE id = ?", (posting_id,)
    ).fetchone()[0]
    assert closed is None


# --- the search prompt states the ceiling (PRD 01 "Search prompt contract") ----------


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def prompt_text():
    return assemble_search_prompt(load_search_config(), "analytics", 24, NOW)


def test_the_assembled_prompt_states_the_60_day_age_ceiling(profile):
    prompt = prompt_text()
    assert "60 days" in prompt
    assert "first published" in prompt


def test_the_prompt_says_an_old_posting_is_out_of_scope_however_recently_updated(
    profile,
):
    prompt = " ".join(prompt_text().split())
    assert "however recently it was updated" in prompt


def test_the_ceiling_sits_in_the_search_window_section_beside_the_window(profile):
    prompt = prompt_text()
    start = prompt.index("# Search window")
    section = prompt[start : prompt.index("\n# ", start + 1)]
    assert "60 days" in section
