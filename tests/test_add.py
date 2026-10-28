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
from conftest import REJECTED, drop_all_tables, raw_connect
from profile_helpers import ROLE_FAMILIES, use_role_families, write_role_families

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


@pytest.fixture(autouse=True)
def profile(tmp_path, monkeypatch):
    """One role family, so no role-family prompt unless a test adds a second (PRD 03)."""
    return use_role_families(tmp_path, monkeypatch, ROLE_FAMILIES[:1])


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


def in_refinement(conn, url):
    return conn.execute(
        "SELECT in_refinement FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()[0]


def stored_role_family(conn, url):
    return conn.execute(
        "SELECT role_family FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()[0]


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


def seed_decided(conn, url, decision, feedback=None, agent="claude", role_family=None):
    """An Apply row may carry a role family (PRD 03); a Skip row never does."""
    posting_id = db.insert_posting(
        conn, company="Acme Widgets", title="Old Title", url=url, search_agent=agent
    )
    conn.execute(
        "UPDATE postings SET decision = ?, fit_feedback = ?, decided_at = ? WHERE id = ?",
        (decision, feedback, LONG_AGO, posting_id),
    )
    if role_family:
        conn.execute(
            "UPDATE postings SET role_family = ? WHERE id = ?",
            (role_family, posting_id),
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
    posting_id = seed_decided(
        conn, url, "Apply", role_family=ROLE_FAMILIES[0], feedback="great fit"
    )
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
    assert db.find_posting(conn, url) == (posting_id, "Skip", 1, None)
    assert db.find_posting(conn, f"{url}?utm_campaign=x") == (
        posting_id,
        "Skip",
        1,
        None,
    )
    assert db.find_posting(conn, gh_url()) is None


def test_find_posting_reports_the_role_family_of_an_apply_row(db_url, conn):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[1])
    assert db.find_posting(conn, url) == (posting_id, "Apply", 1, ROLE_FAMILIES[1])


# --- prompts inside the add flow ---------------------------------------------


def test_without_a_tty_empty_lines_accept_the_derived_company_and_title(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Acme Widgets"
    assert row["title"] == "Staff Platform Engineer"
    assert row["decision"] == "Apply"
    assert in_refinement(conn, url) == 1


def test_typed_answers_replace_the_derived_company_and_title_and_survive_capture(
    db_url, conn, web, monkeypatch, capsys
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, stdin="Globex Corporation\nMy Corrected Title\n\n"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert row["company"] == "Globex Corporation"
    assert row["title"] == "My Corrected Title"
    assert row["title_slug"] == title_slug("My Corrected Title")
    assert in_refinement(conn, url) == 1
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
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Example Corp", "Staff Platform Engineer")
    assert row["jd_markdown"] is None
    assert in_refinement(conn, url) == 1
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
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n\n")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("Acme Widgets", "Staff Platform Engineer")
    assert row["jd_markdown"] is None
    assert in_refinement(conn, url) == 1


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


# --- the refinement setting (issue #180) ------------------------------

QUESTION = "Use this posting to refine the search?"


def exclude(conn, url):
    conn.execute(
        "UPDATE postings SET in_refinement = 0 WHERE canonical_url = ?",
        (canonicalize_url(url),),
    )


def refinement_lines(output):
    return [line for line in output.splitlines() if "exclud" in line.lower()]


# --- the column and its migration (criterion 1; PRD 02 "Schema creation and change") -----


def table_shape(url):
    conn = raw_connect(url)
    try:
        rows = conn.execute(
            "SELECT name, type, \"notnull\", dflt_value FROM pragma_table_info('postings')"
        ).fetchall()
    finally:
        conn.close()
    return {row[0]: tuple(row[1:]) for row in rows}


def downgrade(url):
    """Leave the database as a version without the column left it, holding three decided rows."""
    drop_all_tables(url)
    db.connect().close()
    conn = raw_connect(url)
    try:
        conn.execute("ALTER TABLE postings DROP COLUMN in_refinement")
        for n, decision in enumerate(("Apply", "Skip", None)):
            conn.execute(
                "INSERT INTO postings (company, title, url, canonical_url, search_agent, decision, decided_at) "
                "VALUES ('Acme', 'Engineer', ?, ?, 'claude', ?, ?)",
                (
                    f"https://x.example/{n}",
                    f"https://x.example/{n}",
                    decision,
                    LONG_AGO,
                ),
            )
    finally:
        conn.close()


def test_a_fresh_postings_table_holds_in_refinement_not_null_defaulting_to_one(
    db_url, conn
):
    drop_all_tables(db_url)
    fresh = db.connect()
    fresh.execute(
        "INSERT INTO postings (company, title, url, canonical_url, search_agent) "
        "VALUES ('Acme', 'Engineer', 'https://x.example/a', 'https://x.example/a', 'claude')"
    )
    assert fresh.execute("SELECT in_refinement FROM postings").fetchall() == [(1,)]
    with pytest.raises(REJECTED):
        fresh.execute(
            "INSERT INTO postings (company, title, url, canonical_url, search_agent, in_refinement) "
            "VALUES ('Acme', 'Engineer', 'https://x.example/b', 'https://x.example/b', 'claude', NULL)"
        )
    fresh.close()


def test_a_database_made_before_the_column_gains_it_with_every_row_in_refinement(
    db_url,
):
    downgrade(db_url)
    assert "in_refinement" not in table_shape(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    rows = conn.execute("SELECT in_refinement FROM postings ORDER BY id").fetchall()
    conn.close()
    assert rows == [(1,), (1,), (1,)]


def test_the_migration_is_a_no_op_once_applied(db_url):
    downgrade(db_url)
    db.connect().close()
    conn = raw_connect(db_url)
    conn.execute("UPDATE postings SET in_refinement = 0 WHERE decision = 'Skip'")
    before = conn.execute(
        "SELECT name, sql FROM sqlite_master ORDER BY name"
    ).fetchall()
    rows_before = conn.execute(
        "SELECT id, in_refinement FROM postings ORDER BY id"
    ).fetchall()
    conn.close()
    for _ in range(2):
        db.connect().close()
    conn = raw_connect(db_url)
    after = conn.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
    rows_after = conn.execute(
        "SELECT id, in_refinement FROM postings ORDER BY id"
    ).fetchall()
    conn.close()
    assert after == before
    assert rows_after == rows_before


def test_a_migrated_table_matches_a_fresh_one(db_url):
    drop_all_tables(db_url)
    db.connect().close()
    fresh = table_shape(db_url)
    downgrade(db_url)
    db.connect().close()
    assert table_shape(db_url) == fresh


def test_the_migration_keeps_existing_rows_otherwise_unchanged(db_url):
    downgrade(db_url)
    conn = raw_connect(db_url)
    before = conn.execute(
        "SELECT id, company, title, canonical_url, decision, decided_at FROM postings ORDER BY id"
    ).fetchall()
    conn.close()
    db.connect().close()
    conn = raw_connect(db_url)
    after = conn.execute(
        "SELECT id, company, title, canonical_url, decision, decided_at FROM postings ORDER BY id"
    ).fetchall()
    conn.close()
    assert after == before


# --- the setting write (PRD 02 "Query surface") -------------------------------------------


def test_a_searched_row_is_inserted_in_refinement(db_url, conn):
    url = gh_url()
    db.insert_posting(
        conn, company="Acme", title="Engineer", url=url, search_agent="claude"
    )
    assert in_refinement(conn, url) == 1


def test_writing_the_setting_a_row_already_has_changes_nothing(db_url, conn):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Apply")
    db.set_in_refinement(conn, posting_id, True)
    assert posting(conn, url)["decided_at"] == LONG_AGO
    assert in_refinement(conn, url) == 1


def test_changing_the_setting_stores_it_and_refreshes_decided_at(db_url, conn):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Apply")
    db.set_in_refinement(conn, posting_id, False)
    assert in_refinement(conn, url) == 0
    refreshed = posting(conn, url)["decided_at"]
    assert refreshed > LONG_AGO
    db.set_in_refinement(conn, posting_id, True)
    assert in_refinement(conn, url) == 1
    assert posting(conn, url)["decided_at"] >= refreshed
    assert posting(conn, url)["decided_at"] > LONG_AGO


def test_the_setting_is_committed_immediately(http_url, web):
    url = gh_url()
    conn = db.connect()
    posting_id = seed_decided(conn, url, "Apply")
    db.set_in_refinement(conn, posting_id, False)
    raw = raw_connect(http_url)
    rows = raw.execute(
        "SELECT in_refinement FROM postings WHERE id = ?", (posting_id,)
    ).fetchall()
    raw.close()
    conn.close()
    assert rows == [(0,)]


# --- a new row (criteria 2 and 3) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "stored"),
    [("", 1), ("y", 1), ("Y", 1), ("n", 0), ("N", 0)],
    ids=["enter", "y", "upper-y", "n", "upper-n"],
)
def test_a_new_row_stores_the_answer_to_the_refinement_question(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    answer,
    stored,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, stdin=f"Globex Corporation\nMy Title\n{answer}\n"
    )
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"], row["decision"]) == (
        "Globex Corporation",
        "My Title",
        "Apply",
    )
    assert in_refinement(conn, url) == stored
    assert QUESTION in output
    assert "[Y/n]" in output


def test_the_refinement_question_follows_the_company_and_title_prompts(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    # An answer to the refinement question can't be taken for a company or a title.
    code, output = jsa_add(monkeypatch, capsys, url, stdin="n\nn\nn\n")
    assert code == 0, output
    row = posting(conn, url)
    assert (row["company"], row["title"]) == ("n", "n")
    assert in_refinement(conn, url) == 0


def test_with_two_role_families_the_refinement_question_follows_the_role_family_prompt(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    profile,
):
    write_role_families(profile)
    url = gh_url()
    web.routes.update(gh_routes(url))
    # Company, title, then role family 2; the last line can only be the refinement answer.
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\n2\nn\n")
    assert code == 0, output
    assert in_refinement(conn, url) == 0
    assert stored_role_family(conn, url) == ROLE_FAMILIES[1]


def test_with_two_role_families_an_existing_row_is_asked_its_role_family_straight_after_the_lookup(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    profile,
):
    write_role_families(profile)
    url = gh_url()
    seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[0])
    code, output = jsa_add(monkeypatch, capsys, url, stdin="2\n\n")
    assert code == 0, output
    assert stored_role_family(conn, url) == ROLE_FAMILIES[1]
    assert posting(conn, url)["decided_at"] == LONG_AGO


def test_a_bad_answer_to_the_refinement_question_is_asked_again(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n\nmaybe\nn\n")
    assert code == 0, output
    assert in_refinement(conn, url) == 0


def test_the_exclude_flag_stores_zero_without_asking(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, "--exclude-from-refine", stdin="\n\n"
    )
    assert code == 0, output
    assert in_refinement(conn, url) == 0
    assert QUESTION not in output
    assert posting(conn, url)["decision"] == "Apply"


def test_no_input_alone_stores_one(db_url, conn, web, monkeypatch, capsys):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert in_refinement(conn, url) == 1
    assert QUESTION not in output


def test_no_input_with_the_exclude_flag_stores_zero(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    code, output = jsa_add(
        monkeypatch, capsys, url, "--no-input", "--exclude-from-refine"
    )
    assert code == 0, output
    assert in_refinement(conn, url) == 0


def test_the_closing_line_says_when_a_new_posting_is_excluded_and_only_then(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    excluded, included = gh_url(), gh_url()
    web.routes.update(gh_routes(excluded))
    web.routes.update(gh_routes(included))
    _, out_excluded = jsa_add(
        monkeypatch, capsys, excluded, "--no-input", "--exclude-from-refine"
    )
    _, out_included = jsa_add(monkeypatch, capsys, included, "--no-input")
    assert refinement_lines(out_excluded)
    assert refinement_lines(out_included) == []


# --- an existing row (criteria 4, 5, 6) ------------------------------------------------------


def test_an_existing_row_is_asked_straight_after_the_lookup(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Apply")
    # No company or title prompt precedes it: the first line is the answer.
    code, output = jsa_add(monkeypatch, capsys, url, stdin="n\n")
    assert code == 0, output
    assert QUESTION in output
    assert in_refinement(conn, url) == 0


def test_enter_keeps_an_included_row_included_and_writes_nothing(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[0])
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n")
    assert code == 0, output
    assert in_refinement(conn, url) == 1
    assert posting(conn, url)["decided_at"] == LONG_AGO
    assert "already Apply; no change." in output


def test_enter_keeps_an_excluded_row_excluded_and_writes_nothing(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[0])
    exclude(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, stdin="\n")
    assert code == 0, output
    assert in_refinement(conn, url) == 0
    assert posting(conn, url)["decided_at"] == LONG_AGO
    assert "already Apply; no change." in output


def test_an_excluded_row_is_brought_back_by_a_yes(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Apply")
    exclude(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, stdin="y\n")
    assert code == 0, output
    assert in_refinement(conn, url) == 1
    assert posting(conn, url)["decided_at"] > LONG_AGO
    assert str(posting_id) in output
    assert "no change" not in output
    assert refinement_lines(output) == []


def test_a_no_excludes_an_included_row_and_refreshes_decided_at(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    posting_id = seed_decided(conn, url, "Apply", feedback="great fit")
    before = posting(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, stdin="n\n")
    assert code == 0, output
    after = posting(conn, url)
    assert in_refinement(conn, url) == 0
    assert after["decided_at"] > LONG_AGO
    for field in ("id", "company", "title", "decision", "fit_feedback", "search_agent"):
        assert after[field] == before[field]
    assert str(posting_id) in output
    assert "no change" not in output
    assert refinement_lines(output)


def test_the_exclude_flag_changes_an_existing_row_without_asking(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Apply")
    code, output = jsa_add(monkeypatch, capsys, url, "--exclude-from-refine", stdin="")
    assert code == 0, output
    assert in_refinement(conn, url) == 0
    assert posting(conn, url)["decided_at"] > LONG_AGO
    assert QUESTION not in output
    assert "no change" not in output


def test_the_exclude_flag_on_an_already_excluded_row_is_no_change(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[0])
    exclude(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, "--exclude-from-refine")
    assert code == 0, output
    assert in_refinement(conn, url) == 0
    assert posting(conn, url)["decided_at"] == LONG_AGO
    assert "already Apply; no change." in output


@pytest.mark.parametrize("excluded", [False, True], ids=["included", "excluded"])
def test_no_input_without_the_flag_leaves_an_existing_rows_setting_as_it_is(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    excluded,
):
    url = gh_url()
    seed_decided(conn, url, "Apply", role_family=ROLE_FAMILIES[0])
    if excluded:
        exclude(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert in_refinement(conn, url) == (0 if excluded else 1)
    assert posting(conn, url)["decided_at"] == LONG_AGO
    assert "already Apply; no change." in output


def test_re_adding_a_skipped_row_with_the_flag_promotes_and_excludes_it(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Skip", feedback="too junior", agent="gemini")
    code, output = jsa_add(monkeypatch, capsys, url, "--exclude-from-refine")
    assert code == 0, output
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["fit_feedback"] == "too junior"
    assert row["search_agent"] == "gemini"
    assert in_refinement(conn, url) == 0
    assert "Skip → Apply" in output
    assert refinement_lines(output)


def test_re_adding_a_searched_row_never_changes_a_setting_it_keeps(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Skip")
    exclude(conn, url)
    code, output = jsa_add(monkeypatch, capsys, url, "--no-input")
    assert code == 0, output
    assert posting(conn, url)["decision"] == "Apply"
    assert in_refinement(conn, url) == 0


# --- quitting writes nothing (criterion 6) ----------------------------------------------------


@pytest.mark.parametrize(
    "stdin", ["\n\n", "Globex Corporation\nMy Title\n"], ids=["empty-answers", "typed"]
)
def test_end_of_input_at_the_refinement_prompt_writes_nothing_for_a_new_row(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    stdin,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    before = count(conn, "postings")
    code, _ = jsa_add(monkeypatch, capsys, url, stdin=stdin)
    assert code != 0
    assert posting(conn, url) is None
    assert count(conn, "postings") == before
    assert count(conn, "search_findings", url) == 0


def test_ctrl_c_at_the_refinement_prompt_writes_nothing_for_a_new_row(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    web.routes.update(gh_routes(url))
    answers = iter(["", ""])

    def input_then_interrupt(*_args, **_kwargs):
        try:
            return next(answers)
        except StopIteration:
            raise KeyboardInterrupt from None

    monkeypatch.setattr(builtins, "input", input_then_interrupt)
    code, _ = jsa_add(monkeypatch, capsys, url, stdin="")
    assert code != 0
    assert posting(conn, url) is None


@pytest.mark.parametrize("decision", ["Apply", "Skip"])
def test_end_of_input_at_the_refinement_prompt_writes_nothing_for_an_existing_row(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
    decision,
):
    url = gh_url()
    seed_decided(conn, url, decision, feedback="note")
    before = posting(conn, url)
    code, _ = jsa_add(monkeypatch, capsys, url, stdin="")
    assert code != 0
    assert posting(conn, url) == before
    assert in_refinement(conn, url) == 1


def test_ctrl_c_at_the_refinement_prompt_writes_nothing_for_an_existing_row(
    db_url,
    conn,
    web,
    monkeypatch,
    capsys,
):
    url = gh_url()
    seed_decided(conn, url, "Skip")
    before = posting(conn, url)

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    code, _ = jsa_add(monkeypatch, capsys, url, stdin="")
    assert code != 0
    assert posting(conn, url) == before
    assert in_refinement(conn, url) == 1
