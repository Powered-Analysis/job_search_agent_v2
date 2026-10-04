import builtins
import io
import json
import random
import sys
import uuid
from datetime import UTC, datetime
from importlib.metadata import version
from types import SimpleNamespace

import httpx
import pytest

from jsa import cli, db
from jsa.naming import company_from_board, normalize_company, title_slug
from jsa.urls import canonicalize_url

USER_AGENT = f"job-search-agent/{version('jsa')}"
LOCAL_DATABASE_HOSTS = {"127.0.0.1", "localhost"}
LONG_AGO = "2020-01-01T00:00:00.000Z"


def gh_url(token="acme-widgets", **query):
    suffix = "".join(
        f"{'?' if i == 0 else '&'}{k}={v}" for i, (k, v) in enumerate(query.items())
    )
    return f"https://job-boards.greenhouse.io/{token}/jobs/{random.randint(10**9, 10**10)}{suffix}"


def gh_routes(url, **overrides):
    """The Greenhouse detail record for `url`, as the platform would answer it."""
    parts = url.split("?")[0].split("/")
    token, job_id = parts[3], parts[5]
    record = {
        "title": "Staff Platform Engineer",
        "content": "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;Build the platform.&lt;/p&gt;",
        "location": {"name": "Remote, US"},
        **overrides,
    }
    return {("boards-api.greenhouse.io", f"/v1/boards/{token}/jobs/{job_id}"): record}


@pytest.fixture
def web(monkeypatch):
    """Route every outside HTTP call to `web.routes`; anything unrouted is a 404.

    Replaces the outside world beneath the app's shared client (XC-9), so the
    CLI is exercised end to end without a network.
    """

    state = SimpleNamespace(routes={}, requests=[])

    def handle(self, request):
        if request.url.host in LOCAL_DATABASE_HOSTS:
            raise AssertionError(f"unexpected local request {request.url}")
        state.requests.append(request)
        answer = state.routes.get((request.url.host, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return state


def jsa_add(monkeypatch, capsys, *args, stdin=None):
    """Run `jsa add ...` in-process; returns (exit code, stdout + stderr)."""
    monkeypatch.setattr(sys, "argv", ["jsa", "add", *args])
    if stdin is not None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


COLUMNS = (
    "id, company, title, url, date_posted, normalized_company, title_slug, "
    "jd_markdown, location, search_agent, decision, fit_feedback, decided_at"
)


def posting(conn, url):
    row = conn.execute(
        f"SELECT {COLUMNS} FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()
    if row is None:
        return None
    return dict(zip(COLUMNS.split(", "), row, strict=True))


def count(conn, table, url=None):
    if url is None:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()[0]


def seed_decided(conn, url, decision, feedback=None, agent="claude"):
    posting_id = db.insert_posting(
        conn, company="Acme Widgets", title="Old Title", url=url, search_agent=agent
    )
    conn.execute(
        "UPDATE postings SET decision = ?, fit_feedback = ?, decided_at = ? WHERE id = ?",
        (decision, feedback, LONG_AGO, posting_id),
    )
    return posting_id


def seed_finding(conn, url, decision):
    conn.execute(
        "INSERT INTO search_findings (run_date, agent, canonical_url, verification, decision) "
        "VALUES ('2026-01-01', 'claude', ?, 'verified', ?)",
        (canonicalize_url(url), decision),
    )


def finding_decisions(conn, url):
    rows = conn.execute(
        "SELECT decision FROM search_findings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchall()
    return [row[0] for row in rows]


# --- derivation --------------------------------------------------------------


@pytest.mark.parametrize(
    ("board", "company"),
    [
        ("acme", "Acme"),
        ("acme-widgets", "Acme Widgets"),
        ("acme_widgets", "Acme Widgets"),
        ("acme.widgets", "Acme Widgets"),
        ("acme+widgets", "Acme Widgets"),
        ("big-blue_sky.labs", "Big Blue Sky Labs"),
    ],
)
def test_company_is_derived_from_the_board_slug(board, company):
    assert company_from_board(board) == company


# --- aggregator refusal ------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/3999000111",
        "https://www.indeed.com/viewjob?jk=0123456789abcdef",
    ],
)
def test_aggregator_url_is_refused_and_nothing_is_written(
    db_url, conn, web, monkeypatch, capsys, url
):
    postings_before = count(conn, "postings")
    findings_before = count(conn, "search_findings")
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert "employer" in output.lower()
    assert count(conn, "postings") == postings_before
    assert count(conn, "postings", url) == 0
    assert count(conn, "search_findings") == findings_before


def test_aggregator_url_is_refused_even_with_every_value_supplied(
    db_url, conn, web, monkeypatch, capsys
):
    url = "https://www.linkedin.com/jobs/view/3999000222"
    before = count(conn, "postings")
    code, _ = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Acme",
        "--title",
        "Engineer",
        "--no-input",
    )
    assert code != 0
    assert count(conn, "postings") == before


# --- new row from a supported ATS --------------------------------------------


def test_new_supported_ats_url_is_inserted_decided_apply_with_its_capture(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    findings_before = count(conn, "search_findings")
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row is not None
    assert row["search_agent"] == "manual"
    assert row["decision"] == "Apply"
    assert row["decided_at"] is not None
    decided = datetime.fromisoformat(row["decided_at"])
    assert abs(datetime.now(UTC) - decided).total_seconds() < 600
    assert "## About the role" in row["jd_markdown"]
    assert "Build the platform." in row["jd_markdown"]
    assert row["location"] == "Remote, US"
    assert row["company"] == "Acme Widgets"
    assert row["title"] == "Staff Platform Engineer"
    assert row["normalized_company"] == normalize_company("Acme Widgets")
    assert row["title_slug"] == title_slug("Staff Platform Engineer")
    assert count(conn, "postings", url) == 1
    assert count(conn, "search_findings") == findings_before


def test_every_request_the_add_makes_carries_the_user_agent(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert web.requests
    assert all(r.headers["user-agent"] == USER_AGENT for r in web.requests)


def test_supplied_company_and_title_are_kept_over_the_captured_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Globex Corporation",
        "--title",
        "My Own Title",
        "--no-input",
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Globex Corporation"
    assert row["title"] == "My Own Title"
    assert row["title_slug"] == title_slug("My Own Title")
    assert row["normalized_company"] == normalize_company("Globex Corporation")
    assert "Build the platform." in row["jd_markdown"]


def test_date_posted_is_stored(db_url, conn, web, monkeypatch, capsys):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, "--date-posted", "2026-02-03", "--no-input"
    )
    assert code == 0, output
    assert posting(conn, url)["date_posted"] == "2026-02-03"


def test_a_malformed_date_posted_is_rejected_and_nothing_is_written(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, _ = jsa_add(
        monkeypatch, capsys, url, "--date-posted", "03/02/2026", "--no-input"
    )
    assert code != 0
    assert posting(conn, url) is None


def test_added_url_is_stored_by_its_canonical_form(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url(gh_src="abc123", utm_source="newsletter")
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    stored = conn.execute(
        "SELECT canonical_url FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()
    assert stored is not None
    assert "gh_src" not in stored[0] and "utm_source" not in stored[0]


# --- derivation failures under --no-input ------------------------------------


def test_no_input_off_ats_without_company_or_title_fails_and_writes_nothing(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://careers.example.com/openings/{random.randint(10**9, 10**10)}"
    before = count(conn, "postings")
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert count(conn, "postings") == before
    assert posting(conn, url) is None


def test_no_input_off_ats_with_only_a_company_still_fails(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://careers.example.com/openings/{random.randint(10**9, 10**10)}"
    code, _ = jsa_add(monkeypatch, capsys, url, "--company", "Acme", "--no-input")
    assert code != 0
    assert posting(conn, url) is None


def test_no_input_with_a_failed_capture_and_no_title_fails_and_writes_nothing(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()  # the platform answers 404: no title can be derived
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert posting(conn, url) is None


# --- capture failure and unsupported hosts -----------------------------------


def test_failed_capture_still_inserts_with_null_jd_and_says_the_packet_has_no_posting(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()  # 404 from the platform
    code, output = jsa_add(
        monkeypatch, capsys, url, "--title", "Staff Engineer", "--no-input"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["jd_markdown"] is None
    assert row["decision"] == "Apply"
    assert row["search_agent"] == "manual"
    assert row["title"] == "Staff Engineer"
    assert "job_posting.md" in output


def test_unsupported_host_inserts_with_null_jd_and_says_the_packet_has_no_posting(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://careers.example.com/openings/{random.randint(10**9, 10**10)}"
    code, output = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Example Co",
        "--title",
        "Engineer",
        "--no-input",
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["jd_markdown"] is None
    assert row["decision"] == "Apply"
    assert row["company"] == "Example Co"
    assert "job_posting.md" in output
    assert count(conn, "search_findings", url) == 0


def test_a_successful_capture_does_not_mention_a_missing_packet_posting(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    _, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert "job_posting.md" not in output


def test_a_rippling_detail_404_inserts_without_a_jd_and_never_lists_the_board(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://ats.rippling.com/acme-widgets/jobs/{uuid.uuid4()}"
    code, output = jsa_add(
        monkeypatch, capsys, url, "--title", "Support Lead", "--no-input"
    )
    assert code == 0, output
    assert posting(conn, url)["jd_markdown"] is None
    list_paths = [r.url.path for r in web.requests if r.url.path.endswith("/jobs")]
    assert list_paths == []
    assert all(r.method == "GET" for r in web.requests)


# --- re-adding an existing row -----------------------------------------------


def test_re_adding_a_skipped_row_promotes_it_keeping_feedback_and_source(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    posting_id = seed_decided(conn, url, "Skip", feedback="too junior", agent="gemini")
    seed_finding(conn, url, "Skip")
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["id"] == posting_id
    assert row["decision"] == "Apply"
    assert row["fit_feedback"] == "too junior"
    assert row["search_agent"] == "gemini"
    assert row["decided_at"] > LONG_AGO
    assert finding_decisions(conn, url) == ["Apply"]
    assert str(posting_id) in output
    assert "Skip → Apply" in output
    assert count(conn, "postings", url) == 1


def test_re_adding_a_row_with_a_tracking_variant_url_hits_the_same_row(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    posting_id = seed_decided(conn, url, "Skip", feedback="no")
    variant = f"{url}?gh_src=abc123&utm_source=newsletter&utm_medium=email"
    postings_before = count(conn, "postings")
    code, output = jsa_add(monkeypatch, capsys, variant, "--no-input")
    assert code == 0, output
    assert count(conn, "postings") == postings_before
    assert posting(conn, url)["id"] == posting_id
    assert posting(conn, url)["decision"] == "Apply"
    assert str(posting_id) in output


def test_re_adding_an_apply_row_changes_nothing(db_url, conn, web, monkeypatch, capsys):
    url = gh_url()
    web.routes.update(gh_routes(url))
    posting_id = seed_decided(conn, url, "Apply", feedback="great fit")
    seed_finding(conn, url, "Apply")
    before = posting(conn, url)
    postings_before = count(conn, "postings")
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert "already Apply; no change." in output
    assert str(posting_id) in output
    assert posting(conn, url) == before
    assert count(conn, "postings") == postings_before
    assert finding_decisions(conn, url) == ["Apply"]


def test_re_adding_with_a_new_title_does_not_rewrite_an_existing_row(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    seed_decided(conn, url, "Apply")
    before = posting(conn, url)
    code, _ = jsa_add(
        monkeypatch, capsys, url, "--title", "Something Else", "--no-input"
    )
    assert code == 0
    assert posting(conn, url) == before


def test_re_adding_never_writes_a_search_finding(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    seed_decided(conn, url, "Skip")
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0
    assert count(conn, "search_findings", url) == 0


# --- set_decision / find_posting ---------------------------------------------


def test_set_decision_refreshes_decided_at_and_syncs_findings_on_canonical_url(
    db_url, conn
):
    url = gh_url()
    seed_decided(conn, url, "Apply", feedback="note")
    seed_finding(conn, url, "Apply")
    other = gh_url()
    seed_finding(conn, other, "Apply")
    db.set_decision(conn, f"{url}?gh_src=xyz", "Skip")
    row = posting(conn, url)
    assert row["decision"] == "Skip"
    assert row["fit_feedback"] == "note"
    assert row["decided_at"] > LONG_AGO
    assert finding_decisions(conn, url) == ["Skip"]
    assert finding_decisions(conn, other) == ["Apply"]


def test_find_posting_reports_id_and_decision_for_any_variant_of_the_url(db_url, conn):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Skip")
    assert db.find_posting(conn, url) == (posting_id, "Skip")
    assert db.find_posting(conn, f"{url}?utm_campaign=x") == (posting_id, "Skip")
    assert db.find_posting(conn, gh_url()) is None


# --- prompts inside the add flow ---------------------------------------------


def test_without_a_tty_empty_lines_accept_the_derived_company_and_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Acme Widgets"
    assert row["title"] == "Staff Platform Engineer"
    assert row["decision"] == "Apply"


def test_typed_answers_replace_the_derived_company_and_title_and_survive_capture(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, stdin="Globex Corporation\nMy Corrected Title\n"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Globex Corporation"
    assert row["title"] == "My Corrected Title"
    assert row["title_slug"] == title_slug("My Corrected Title")
    assert "Build the platform." in row["jd_markdown"]


@pytest.mark.parametrize("stdin", ["", "Globex Corporation\n"])
def test_end_of_input_at_a_prompt_writes_nothing(
    db_url, conn, web, monkeypatch, capsys, stdin
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    postings_before = count(conn, "postings")
    code, _ = jsa_add(monkeypatch, capsys, url, stdin=stdin)
    assert code != 0
    assert posting(conn, url) is None
    assert count(conn, "postings") == postings_before
    assert count(conn, "search_findings", url) == 0


def test_ctrl_c_at_a_prompt_writes_nothing(db_url, conn, web, monkeypatch, capsys):
    url = gh_url()
    web.routes.update(gh_routes(url))

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    code, _ = jsa_add(monkeypatch, capsys, url, stdin="")
    assert code != 0
    assert posting(conn, url) is None


# --- capture from the page's schema.org JobPosting data ----------------------


def jobposting_page(**overrides):
    node = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Staff Platform Engineer",
        "description": "<h2>About the role</h2><p>Build the platform.</p>",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": "Austin"},
        },
        **overrides,
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(node)}</script></head><body></body></html>"
    )


def html_response(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/html"})


def off_four_url():
    return f"https://careers.example.com/openings/{random.randint(10**9, 10**10)}"


def page_key(url):
    parsed = httpx.URL(url)
    return (parsed.host, parsed.path)


def test_no_input_off_four_with_jobposting_data_derives_everything_from_it(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(jobposting_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Example Corp"
    assert row["title"] == "Staff Platform Engineer"
    assert "## About the role" in row["jd_markdown"]
    assert "Build the platform." in row["jd_markdown"]
    assert "Austin" in row["location"]
    assert row["decision"] == "Apply"
    assert row["search_agent"] == "manual"
    assert "job_posting.md" not in output


def test_off_four_stored_capture_never_overwrites_the_confirmed_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(jobposting_page())
    code, output = jsa_add(
        monkeypatch, capsys, url, "--title", "My Own Title", "--no-input"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["title"] == "My Own Title"
    assert row["company"] == "Example Corp"
    assert row["jd_markdown"] is not None


def test_off_four_supplied_company_is_kept_over_the_hiring_organization(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(jobposting_page())
    code, output = jsa_add(
        monkeypatch, capsys, url, "--company", "Chosen Co", "--no-input"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Chosen Co"
    assert row["title"] == "Staff Platform Engineer"


def test_off_four_page_without_jobposting_data_and_no_values_fails_and_writes_nothing(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response("<html><body>Hello</body></html>")
    before = count(conn, "postings")
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert count(conn, "postings") == before
    assert posting(conn, url) is None


def test_off_four_page_without_jobposting_data_inserts_null_jd_with_supplied_values(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response("<html><body>Hello</body></html>")
    code, output = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Example Co",
        "--title",
        "Engineer",
        "--no-input",
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["jd_markdown"] is None
    assert (row["company"], row["title"]) == ("Example Co", "Engineer")
    assert "job_posting.md" in output


def descriptionless_page(**overrides):
    node = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Staff Platform Engineer",
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
        "jobLocation": {
            "@type": "Place",
            "address": {"@type": "PostalAddress", "addressLocality": "Austin"},
        },
        **overrides,
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(node)}</script></head><body></body></html>"
    )


def test_no_input_off_four_jobposting_without_description_derives_company_and_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Example Corp"
    assert row["title"] == "Staff Platform Engineer"
    assert row["decision"] == "Apply"
    assert row["search_agent"] == "manual"


def test_off_four_jobposting_without_description_stores_null_jd_and_says_so(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert posting(conn, url)["jd_markdown"] is None
    assert "job_posting.md" in output


def test_off_four_jobposting_without_description_keeps_its_location(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert "Austin" in posting(conn, url)["location"]


@pytest.mark.parametrize("description", ["", "   "])
def test_off_four_jobposting_with_a_blank_description_is_treated_as_having_none(
    db_url, conn, web, monkeypatch, capsys, description
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(
        descriptionless_page(description=description)
    )
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Example Corp", "Staff Platform Engineer")
    assert not row["jd_markdown"]
    assert "job_posting.md" in output


def test_off_four_jobposting_without_description_keeps_supplied_values(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Chosen Co",
        "--title",
        "My Own Title",
        "--no-input",
    )
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Chosen Co", "My Own Title")
    assert row["jd_markdown"] is None
    assert "Austin" in row["location"]


def test_off_four_jobposting_without_description_title_only_still_needs_a_company(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(
        descriptionless_page(hiringOrganization=None)
    )
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert posting(conn, url) is None


def test_without_a_tty_empty_lines_accept_the_derived_values_of_a_descriptionless_jobposting(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Example Corp", "Staff Platform Engineer")
    assert row["jd_markdown"] is None
    assert "job_posting.md" in output


def test_ashby_with_a_failed_ats_fetch_and_a_descriptionless_page_derives_from_it(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://jobs.ashbyhq.com/acme-widgets/{uuid.uuid4()}"
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["title"] == "Staff Platform Engineer"
    assert row["jd_markdown"] is None
    assert "job_posting.md" in output


ATS_PAGE_URLS = {
    "ashby": lambda: f"https://jobs.ashbyhq.com/acme-widgets/{uuid.uuid4()}",
    "rippling": lambda: f"https://ats.rippling.com/acme-widgets/jobs/{uuid.uuid4()}",
}


@pytest.mark.parametrize("platform", ATS_PAGE_URLS)
def test_a_failed_ats_fetch_and_a_descriptionless_page_takes_company_from_the_board_slug(
    platform, db_url, conn, web, monkeypatch, capsys
):
    url = ATS_PAGE_URLS[platform]()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Acme Widgets", "Staff Platform Engineer")
    assert row["jd_markdown"] is None
    assert "job_posting.md" in output


@pytest.mark.parametrize("platform", ATS_PAGE_URLS)
def test_a_failed_ats_fetch_and_a_descriptionless_page_keeps_supplied_values(
    platform, db_url, conn, web, monkeypatch, capsys
):
    url = ATS_PAGE_URLS[platform]()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(
        monkeypatch,
        capsys,
        url,
        "--company",
        "Chosen Co",
        "--title",
        "My Own Title",
        "--no-input",
    )
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Chosen Co", "My Own Title")
    assert row["jd_markdown"] is None


def test_without_a_tty_empty_lines_accept_what_a_descriptionless_page_offers_after_a_failed_ats_fetch(
    db_url, conn, web, monkeypatch, capsys
):
    url = ATS_PAGE_URLS["ashby"]()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Acme Widgets", "Staff Platform Engineer")
    assert row["jd_markdown"] is None


def test_a_failed_ats_fetch_and_a_descriptionless_page_without_a_title_cannot_derive_one(
    db_url, conn, web, monkeypatch, capsys
):
    url = ATS_PAGE_URLS["ashby"]()
    web.routes[page_key(url)] = html_response(descriptionless_page(title=None))
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert posting(conn, url) is None


def test_a_failed_ats_fetch_and_a_blank_description_page_is_treated_as_having_none(
    db_url, conn, web, monkeypatch, capsys
):
    url = ATS_PAGE_URLS["ashby"]()
    web.routes[page_key(url)] = html_response(descriptionless_page(description="  "))
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["title"] == "Staff Platform Engineer"
    assert row["jd_markdown"] is None


def test_greenhouse_with_a_failed_ats_fetch_ignores_a_descriptionless_page_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes[page_key(url)] = html_response(descriptionless_page())
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert posting(conn, url) is None


def test_off_four_malformed_jsonld_is_treated_as_no_data(
    db_url, conn, web, monkeypatch, capsys
):
    url = off_four_url()
    web.routes[page_key(url)] = html_response(
        '<html><script type="application/ld+json">{oops</script></html>'
    )
    code, _ = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code != 0
    assert posting(conn, url) is None


def test_ashby_with_a_failed_ats_fetch_stores_the_page_jobposting_capture(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://jobs.ashbyhq.com/acme-widgets/{uuid.uuid4()}"
    web.routes[page_key(url)] = html_response(jobposting_page())
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["title"] == "Staff Platform Engineer"
    assert "Build the platform." in row["jd_markdown"]
    assert "job_posting.md" not in output


def test_ashby_with_a_failed_ats_fetch_and_no_page_data_stores_null(
    db_url, conn, web, monkeypatch, capsys
):
    url = f"https://jobs.ashbyhq.com/acme-widgets/{uuid.uuid4()}"
    web.routes[page_key(url)] = html_response("<html><body>Hello</body></html>")
    code, output = jsa_add(
        monkeypatch, capsys, url, "--title", "Designer", "--no-input"
    )
    assert code == 0, output
    assert posting(conn, url)["jd_markdown"] is None
    assert "job_posting.md" in output


def test_greenhouse_with_a_failed_ats_fetch_stores_null_even_if_the_page_has_data(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes[page_key(url)] = html_response(jobposting_page())
    code, output = jsa_add(
        monkeypatch, capsys, url, "--title", "Staff Engineer", "--no-input"
    )
    assert code == 0, output
    assert posting(conn, url)["jd_markdown"] is None
    assert [r for r in web.requests if r.url.host == "job-boards.greenhouse.io"] == []


def test_a_supported_fetcher_capture_is_not_replaced_by_page_data(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    web.routes[page_key(url)] = html_response(
        jobposting_page(title="Page Title", description="<p>Page body</p>")
    )
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    row = posting(conn, url)
    assert row["title"] == "Staff Platform Engineer"
    assert "Build the platform." in row["jd_markdown"]
    assert "Page body" not in row["jd_markdown"]
