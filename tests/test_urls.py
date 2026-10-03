import socket
from urllib.parse import parse_qsl, urlsplit

import pytest

from jsa.urls import canonicalize_url

GH = "https://job-boards.greenhouse.io/acme/jobs/123"


def test_spec_example_greenhouse_forms_converge():
    assert (
        canonicalize_url(
            "HTTPS://Job-Boards.Greenhouse.io/acme/jobs/123?utm_source=x#apply"
        )
        == GH
    )
    assert canonicalize_url("https://boards.greenhouse.io/acme/jobs/123") == GH


def test_greenhouse_both_hosts_converge_with_gh_jid():
    a = canonicalize_url("https://boards.greenhouse.io/acme/jobs?gh_jid=77")
    b = canonicalize_url("https://job-boards.greenhouse.io/acme/jobs?gh_jid=77")
    assert a == b
    assert urlsplit(a).netloc == "job-boards.greenhouse.io"


def test_lookalike_hosts_are_not_rewritten():
    for url in (
        "https://notboards.greenhouse.io/acme/jobs/1",
        "https://boards.greenhouse.io.example.com/acme/jobs/1",
        "https://jobs.lever.co/acme/abc",
    ):
        assert urlsplit(canonicalize_url(url)).netloc == urlsplit(url).netloc


def test_scheme_and_host_lowercased():
    assert (
        canonicalize_url("HTTPS://Jobs.Lever.CO/acme/abc")
        == "https://jobs.lever.co/acme/abc"
    )


def test_path_case_preserved():
    url = "https://jobs.lever.co/Acme/AbC-123-DeF"
    assert urlsplit(canonicalize_url(url)).path == "/Acme/AbC-123-DeF"


@pytest.mark.parametrize(
    "param",
    [
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_anything_else",
    ],
)
def test_utm_parameters_removed(param):
    assert (
        canonicalize_url(f"https://jobs.lever.co/acme/abc?{param}=x")
        == "https://jobs.lever.co/acme/abc"
    )


@pytest.mark.parametrize("param", ["gclid", "fbclid"])
def test_well_known_tracking_parameters_removed(param):
    assert (
        canonicalize_url(f"https://jobs.lever.co/acme/abc?{param}=zzz")
        == "https://jobs.lever.co/acme/abc"
    )


def test_gh_jid_kept_while_tracking_removed():
    out = canonicalize_url(
        "https://boards.greenhouse.io/acme/jobs?utm_source=x&gh_jid=123&gclid=abc&utm_medium=y"
    )
    assert parse_qsl(urlsplit(out).query) == [("gh_jid", "123")]


def test_gh_jid_distinguishes_jobs():
    assert canonicalize_url("https://acme.com/careers?gh_jid=1") != canonicalize_url(
        "https://acme.com/careers?gh_jid=2"
    )


def test_no_dangling_question_mark_when_all_params_stripped():
    out = canonicalize_url("https://jobs.lever.co/acme/abc?utm_source=x&utm_medium=y")
    assert "?" not in out


def test_fragment_dropped():
    assert "#" not in canonicalize_url("https://jobs.lever.co/acme/abc#apply")
    assert canonicalize_url("https://jobs.lever.co/acme/abc#apply") == canonicalize_url(
        "https://jobs.lever.co/acme/abc"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://job-boards.greenhouse.io/acme/jobs/123",
        "https://jobs.lever.co/acme/abc",
        "https://jobs.ashbyhq.com/acme/0f1e2d3c",
    ],
)
def test_trailing_slash_normalized(url):
    assert canonicalize_url(url + "/") == canonicalize_url(url)
    assert canonicalize_url(url) == canonicalize_url(url + "/")


def test_trailing_slash_with_query():
    assert canonicalize_url("https://acme.com/careers/?gh_jid=5") == canonicalize_url(
        "https://acme.com/careers?gh_jid=5"
    )


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://Job-Boards.Greenhouse.io/acme/jobs/123?utm_source=x#apply",
        "https://boards.greenhouse.io/acme/jobs/123/",
        "https://jobs.lever.co/Acme/AbC?lever-source=x&utm_campaign=y",
        "https://acme.com/careers/?gh_jid=5&gclid=1#top",
        "https://acme.com",
        "https://acme.com/",
    ],
)
def test_idempotent(url):
    once = canonicalize_url(url)
    assert canonicalize_url(once) == once


def test_pure_no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("canonicalization touched the network")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert (
        canonicalize_url("https://boards.greenhouse.io/acme/jobs/123?utm_source=x")
        == GH
    )
