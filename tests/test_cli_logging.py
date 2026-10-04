"""What the CLI's log carries (issue #29; PRD 01 "Run telemetry": the live trace and the summary).

httpx's own per-request INFO line is not part of that trace and must never reach the log.
"""

import logging
import sys
from types import SimpleNamespace

import httpx
import pytest
from conftest import drop_all_tables
from profile_helpers import copy_example
from test_review import Script, name, seed
from test_review import Web as ReviewWeb
from test_search import Web as SearchWeb
from test_search import entry, summary_of

from jsa import cli, db

HTTPX_LINE = "HTTP Request:"


@pytest.fixture(autouse=True)
def fresh_httpx_logger():
    """`cli.main` configures process-wide loggers; give every test the untouched one."""
    logger = logging.getLogger("httpx")
    before = logger.level
    logger.setLevel(logging.NOTSET)
    yield
    logger.setLevel(before)


@pytest.fixture
def open_stub(tmp_path, monkeypatch):
    """`jsa review` opens each posting with macOS `open`; a no-op stand-in goes first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "open"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))


def route_to(stand_in, monkeypatch):
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda _self, r: stand_in.handle(r)
    )


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["jsa", *argv])
    try:
        cli.main()
    except SystemExit as exit_:
        return exit_.code or 0
    return 0


def logged_by_httpx(caplog, capsys):
    captured = capsys.readouterr()
    return HTTPX_LINE in caplog.text or HTTPX_LINE in captured.out + captured.err


def test_the_harness_sees_httpx_request_lines_when_nothing_silences_them(
    monkeypatch, caplog
):
    stand_in = ReviewWeb()
    route_to(stand_in, monkeypatch)
    caplog.set_level(logging.INFO)
    with httpx.Client() as client:
        client.get("https://example.com/")
    assert HTTPX_LINE in caplog.text


def test_a_review_run_prints_no_httpx_request_lines(
    db_url, open_stub, monkeypatch, capsys, caplog
):
    drop_all_tables(db_url)
    conn = db.connect()
    seed(conn, name(), order=1)
    seed(conn, name(), order=2)
    conn.close()
    stand_in = ReviewWeb()
    route_to(stand_in, monkeypatch)
    monkeypatch.setattr(sys, "stdin", Script(["s", "", "s", "", "q"]))
    caplog.set_level(logging.INFO)
    assert run_main(monkeypatch, "review") == 0
    assert stand_in.requests, "review's re-check should have made a request"
    assert not logged_by_httpx(caplog, capsys)


def test_an_add_run_prints_no_httpx_request_lines(
    db_url, tmp_path, monkeypatch, capsys, caplog
):
    drop_all_tables(db_url)
    db.connect().close()
    monkeypatch.chdir(tmp_path)
    stand_in = SearchWeb()
    route_to(stand_in, monkeypatch)
    caplog.set_level(logging.INFO)
    run_main(
        monkeypatch,
        "add",
        "https://job-boards.greenhouse.io/acme/jobs/4242",
        "--company",
        "Acme",
        "--title",
        "Staff Engineer",
        "--no-input",
    )
    assert stand_in.requests, "add should have fetched the posting"
    assert not logged_by_httpx(caplog, capsys)


@pytest.fixture
def search_world(db_url, tmp_path, monkeypatch):
    drop_all_tables(db_url)
    db.connect().close()
    path = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    monkeypatch.chdir(tmp_path)
    stand_in = SearchWeb()
    route_to(stand_in, monkeypatch)
    return SimpleNamespace(web=stand_in)


def test_a_search_run_keeps_its_trace_and_summary_but_not_httpx_lines(
    search_world, monkeypatch, capsys, caplog
):
    web = search_world.web
    web.posted(entry(web.job()))
    caplog.set_level(logging.INFO)
    code = run_main(
        monkeypatch, "search", "--agent", "perplexity", "--window-hours", "24"
    )
    out = capsys.readouterr()
    assert code == 0
    assert web.count("api.perplexity.ai") == 1
    assert [r for r in caplog.records if r.name.startswith("jsa")]
    assert not [r for r in caplog.records if r.name.startswith("httpx")]
    assert HTTPX_LINE not in caplog.text + out.out + out.err
    assert summary_of(out.out)["agent"] == "perplexity"
