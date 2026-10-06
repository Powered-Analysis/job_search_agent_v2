"""`jsa search` with the Perplexity runner (issue #7; PRD 01 "Runners", "Idempotent capture pipeline", "Run telemetry"; PRD 02 `search_findings`, `search_runs`)."""

import itertools
import json
import logging
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import httpx
import pytest
from conftest import drop_all_tables, raw_connect
from profile_helpers import copy_example
from search_helpers import Jump

from jsa import cli, db, runners, search
from jsa.errors import JsaError
from jsa.http import make_client
from jsa.perplexity import (
    PerplexityRunner,
    StreamState,
    fold,
    fold_events,
    request_body,
)
from jsa.runners import Deadline, WallClockExceeded
from jsa.search_output import output_json_schema
from jsa.urls import canonicalize_url

PERPLEXITY = ("api.perplexity.ai", "/v1/agent")
MODEL = "anthropic/claude-opus-5-5"
COST = 1.25
LOCAL_DATABASE_HOSTS = {"127.0.0.1", "localhost"}
SUMMARY_KEYS = [
    "agent",
    "model",
    "effort",
    "mode",
    "cost",
    "found",
    "verified",
    "verified_no_date",
    "reachable",
    "reachable_no_date",
    "aggregator",
    "unsupported",
    "unverifiable",
    "not_on_index",
    "page_closed",
    "out_of_window",
    "malformed",
    "inserted",
    "already_present",
    "jd_captured",
    "fetch_failed",
    "errors",
]
LINKEDIN_URL = "https://www.linkedin.com/jobs/view/3999"
JD_HTML = "<h2>About the role</h2><p>Build the platform.</p>"
_ids = itertools.count(7_000_000)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def hours_ago(hours):
    return iso(datetime.now(UTC) - timedelta(hours=hours))


def gh_url(token="acme", job_id=None):
    return f"https://job-boards.greenhouse.io/{token}/jobs/{job_id or next(_ids)}"


def job_id_of(url):
    return url.rsplit("/", 1)[1]


def entry(url, **fields):
    return {"company": "Acme", "title": "Staff Engineer", "url": url, **fields}


def sse(events):
    return "".join(
        f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events
    )


STEP_EVENT = "response.sandbox.results"


def stream_for(text, *, model=MODEL, cost=COST, steps=2, deltas=()):
    events = [("response.created", {"response": {"model": "xhigh"}})]
    events += [(STEP_EVENT, {}) for _ in range(steps)]
    events += [("response.output_text.delta", {"delta": piece}) for piece in deltas]
    events += [
        ("response.output_text.done", {"text": text}),
        (
            "response.completed",
            {
                "response": {
                    "model": model,
                    "usage": {"cost": {"total_cost": cost}},
                }
            },
        ),
    ]
    return events


def answer_with(postings):
    return json.dumps({"postings": postings})


def event_stream(events):
    return httpx.Response(
        200,
        content=sse(events).encode(),
        headers={"content-type": "text/event-stream"},
    )


class Web:
    """The outside world: Perplexity, Greenhouse boards, and any other route a test adds.

    `routes` maps (host, path) to JSON, an `httpx.Response`, or a callable taking the
    request. `boards` maps a Greenhouse board token to {job id: updated_at}, with
    "none" meaning the index carries no date; its index and detail records are
    answered from it. Anything unrouted is a 404.
    """

    def __init__(self):
        self.routes = {}
        self.boards = {}
        self.broken_details = set()
        self.requests = []

    def perplexity(self, text, **kwargs):
        self.routes[PERPLEXITY] = event_stream(stream_for(text, **kwargs))

    def posted(self, *postings, **kwargs):
        self.perplexity(answer_with(list(postings)), **kwargs)

    def job(self, token="acme", updated_at=None):
        """A live Greenhouse job; returns its emitted URL."""
        job_id = next(_ids)
        self.boards.setdefault(token, {})[str(job_id)] = (
            updated_at if updated_at is not None else hours_ago(1)
        )
        return gh_url(token, job_id)

    def count(self, host, path=None):
        return sum(
            1
            for request in self.requests
            if request.url.host == host and path in (None, request.url.path)
        )

    def handle(self, request):
        if request.url.host in LOCAL_DATABASE_HOSTS:
            raise AssertionError(f"unexpected local request {request.url}")
        self.requests.append(request)
        answer = self.routes.get((request.url.host, request.url.path))
        if answer is None and request.url.host == "boards-api.greenhouse.io":
            answer = self._greenhouse(request.url.path)
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if callable(answer):
            return answer(request)
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    def _greenhouse(self, path):
        parts = path.strip("/").split("/")
        jobs = self.boards.get(parts[2])
        if jobs is None:
            return None
        if len(parts) == 4:
            return {
                "jobs": [
                    {"id": int(job_id), **({} if at == "none" else {"updated_at": at})}
                    for job_id, at in jobs.items()
                ]
            }
        job_id = parts[4]
        if job_id in self.broken_details:
            return httpx.Response(500)
        if job_id not in jobs:
            return None
        return {
            "title": f"ATS Title {job_id}",
            "content": "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;Build the platform.&lt;/p&gt;",
            "location": {"name": "Remote, US"},
        }


@pytest.fixture
def web(monkeypatch):
    stand_in = Web()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda _self, r: stand_in.handle(r)
    )
    return stand_in


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    monkeypatch.setenv("PERPLEXITY_API_KEY", "test-perplexity-key")
    # No stray `.env` in the working directory may supply a key or a URL.
    monkeypatch.chdir(tmp_path)
    return path


@pytest.fixture
def world(db_url, web, profile):
    drop_all_tables(db_url)
    # Every command ensures the schema on connect (PRD 02); do the same so empty tables can be counted.
    db.connect().close()
    return SimpleNamespace(url=db_url, web=web, profile=profile)


def best_effort(profile):
    toml = profile / "search" / "search.toml"
    toml.write_text(toml.read_text().replace('mode = "strict"', 'mode = "best_effort"'))


def jsa_search(monkeypatch, capsys, *args):
    """Run `jsa search ...` in-process; returns (exit code, stdout, stderr)."""
    monkeypatch.setattr(sys, "argv", ["jsa", "search", *args])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run(monkeypatch, capsys, window="24"):
    return jsa_search(
        monkeypatch, capsys, "--agent", "perplexity", "--window-hours", window
    )


def summary_of(stdout):
    pairs = {}
    for line in stdout.splitlines():
        key, sep, value = line.partition(": ")
        if sep and key in SUMMARY_KEYS:
            pairs[key] = value
    return pairs


def query(url, sql, params=()):
    conn = raw_connect(url)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def table(url, table_name, columns, order="rowid"):
    rows = query(url, f"SELECT {columns} FROM {table_name} ORDER BY {order}")
    return [dict(zip(columns.split(", "), row, strict=True)) for row in rows]


def findings(url):
    return table(
        url,
        "search_findings",
        "canonical_url, agent, run_date, window_hours, rank, found_at, decision, "
        "verification, ats_date, ats_date_kind, model, effort",
        order="rank",
    )


def finding_of(url, posting_url):
    (found,) = [
        f for f in findings(url) if f["canonical_url"] == canonicalize_url(posting_url)
    ]
    return found


def search_runs(url):
    return table(
        url,
        "search_runs",
        "trigger, run_date, agent, window_hours, mode, model, effort, started_at, "
        "finished_at, outcome, error, summary, warnings",
        order="id",
    )


def postings(url):
    return table(
        url,
        "postings",
        "canonical_url, title, jd_markdown, location, search_agent",
        order="id",
    )


def posting_of(url, posting_url):
    (found,) = [
        p for p in postings(url) if p["canonical_url"] == canonicalize_url(posting_url)
    ]
    return found


def count_rows(url, table_name):
    return query(url, f"SELECT COUNT(*) FROM {table_name}")[0][0]


def run_date_now():
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def job_posting_page(**overrides):
    node = {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": "Staff Engineer",
        "description": JD_HTML,
        "hiringOrganization": {"@type": "Organization", "name": "Example Corp"},
        **overrides,
    }
    return (
        '<html><head><script type="application/ld+json">'
        f"{json.dumps(node)}</script></head><body></body></html>"
    )


def html(text):
    return httpx.Response(200, text=text, headers={"content-type": "text/html"})


def off_four_url():
    return f"https://careers.example.com/openings/{next(_ids)}"


def route_page(web, url, response):
    parsed = httpx.URL(url)
    web.routes[(parsed.host, parsed.path)] = response


class Tick:
    """A clock that moves `step` seconds each time it is read."""

    def __init__(self, step):
        self.step = step
        self.now = -step

    def __call__(self):
        self.now += self.step
        return float(self.now)


def deref(schema, node):
    while "$ref" in node:
        target = schema
        for part in node["$ref"].split("/")[1:]:
            target = target[part]
        node = target
    return node


# --- the command line ------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [[], ["--agent", "perplexity"], ["--window-hours", "24"]],
    ids=["neither", "no-window", "no-agent"],
)
def test_search_without_agent_or_window_is_a_usage_error(
    world, monkeypatch, capsys, args
):
    code, _out, err = jsa_search(monkeypatch, capsys, *args)
    assert code != 0
    assert "usage" in err.lower()
    assert world.web.requests == []
    assert count_rows(world.url, "search_runs") == 0


@pytest.mark.parametrize("window", ["abc", "0", "-5"])
def test_a_window_that_is_not_a_positive_number_of_hours_is_refused(
    world, monkeypatch, capsys, window
):
    world.web.posted(entry(world.web.job()))
    code, _out, _err = run(monkeypatch, capsys, window=window)
    assert code != 0
    assert world.web.requests == []


def test_an_unknown_agent_is_refused(world, monkeypatch, capsys):
    code, _out, _err = jsa_search(
        monkeypatch, capsys, "--agent", "nonesuch", "--window-hours", "24"
    )
    assert code != 0
    assert world.web.requests == []


# --- the Perplexity request ------------------------------------------------------


def test_request_body_pins_the_xhigh_preset_with_no_overrides():
    body = request_body("the assembled prompt")
    assert body["preset"] == "xhigh"
    assert body["input"] == "the assembled prompt"
    assert body["stream"] is True
    for forbidden in ("model", "max_steps", "tools", "service_tier"):
        assert forbidden not in body


def test_request_body_carries_nothing_beyond_the_preset_and_io():
    assert set(request_body("p")) == {
        "preset",
        "input",
        "stream",
        "response_format",
    }


def test_request_body_enforces_the_output_contract_as_a_json_schema():
    response_format = request_body("p")["response_format"]
    assert response_format["type"] == "json_schema"
    schema = response_format["json_schema"]["schema"]
    # Derived from the contract's own model (convention 1), not restated.
    assert schema == output_json_schema()
    assert schema["required"] == ["postings"]
    postings_node = deref(schema, schema["properties"]["postings"])
    assert postings_node["type"] == "array"
    item = deref(schema, postings_node["items"])
    assert set(item["properties"]) == {"company", "title", "url", "date_posted"}
    assert set(item["required"]) == {"company", "title", "url"}


def perplexity_requests(web):
    return [r for r in web.requests if r.url.host == PERPLEXITY[0]]


def test_the_request_on_the_wire_is_an_authorised_streaming_post_to_the_agent_api(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    (request,) = perplexity_requests(world.web)
    assert request.method == "POST"
    assert str(request.url) == "https://api.perplexity.ai/v1/agent"
    assert request.headers["authorization"] == "Bearer test-perplexity-key"
    body = json.loads(request.content)
    assert body["preset"] == "xhigh"
    assert body["stream"] is True
    assert body["response_format"]["type"] == "json_schema"
    assert body["input"].strip()
    for forbidden in ("model", "max_steps", "tools", "service_tier"):
        assert forbidden not in body


def test_the_request_allows_the_stream_an_1800_second_read_timeout(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    run(monkeypatch, capsys)
    (request,) = perplexity_requests(world.web)
    assert request.extensions["timeout"]["read"] == 1800


def test_the_assembled_prompt_carries_the_window(world, monkeypatch, capsys):
    world.web.posted(entry(world.web.job()))
    run(monkeypatch, capsys, window="72")
    (request,) = perplexity_requests(world.web)
    assert "72" in json.loads(request.content)["input"]


# --- folding the stream ----------------------------------------------------------


def test_fold_prefers_the_done_text_over_the_deltas():
    state = fold_events(
        [
            ("response.output_text.delta", {"delta": "par"}),
            ("response.output_text.delta", {"delta": "tial"}),
            ("response.output_text.done", {"text": "the whole answer"}),
        ]
    )
    assert state.text == "the whole answer"


def test_a_done_event_is_not_overridden_by_deltas_that_arrive_after_it():
    state = fold_events(
        [
            ("response.output_text.done", {"text": "final"}),
            ("response.output_text.delta", {"delta": "stray"}),
        ]
    )
    assert state.text == "final"


def test_fold_joins_the_deltas_when_there_is_no_done_event():
    state = fold_events(
        [
            ("response.output_text.delta", {"delta": '{"post'}),
            ("response.output_text.delta", {"delta": 'ings": []}'}),
        ]
    )
    assert state.text == '{"postings": []}'


def test_fold_reads_cost_and_model_from_the_completed_event():
    state = fold_events(stream_for("x", model="some/model", cost=3.5))
    assert state.model == "some/model"
    assert state.cost == 3.5


def changed_values(before, after):
    """The values a fold changed, so a test needn't name the state's fields."""
    old, new = asdict(before), asdict(after)
    return [new[name] for name in new if new[name] != old[name]]


@pytest.mark.parametrize("steps", [1, 2, 5])
def test_fold_counts_one_sandbox_step_per_sandbox_results_event(steps):
    events = [(STEP_EVENT, {}) for _ in range(steps)]
    assert changed_values(StreamState(), fold_events(events)) == [steps]


@pytest.mark.parametrize(
    "event",
    [
        "response.reasoning.search_results",
        "response.reasoning.fetch_url_results",
        "response.reasoning.started",
        "response.reasoning.search_queries",
        "response.fetch_url.started",
        "response.sandbox.started",
        "response.created",
    ],
)
def test_events_other_than_sandbox_results_are_not_steps(event):
    assert changed_values(StreamState(), fold_events([(event, {})])) == []


def test_the_completed_event_carries_no_service_tier_into_the_state():
    completed = (
        "response.completed",
        {"response": {"model": MODEL, "service_tier": "default"}},
    )
    assert "default" not in asdict(fold_events([completed])).values()


def test_a_completed_event_without_a_service_tier_still_closes_the_run(
    web, profile, caplog
):
    web.routes[PERPLEXITY] = event_stream(
        [
            ("response.output_text.done", {"text": answer_with([])}),
            ("response.completed", {"response": {"model": MODEL}}),
        ]
    )
    caplog.set_level(logging.INFO, logger="jsa")
    with make_client() as client:
        result = PerplexityRunner(client).run("a prompt")
    assert json.loads(result.text) == {"postings": []}
    assert result.model == MODEL


def test_the_closing_line_counts_every_sandbox_step(web, profile, caplog):
    web.perplexity(answer_with([]), steps=41)
    caplog.set_level(logging.INFO, logger="jsa")
    with make_client() as client:
        PerplexityRunner(client).run("a prompt")
    closing = [r.getMessage() for r in caplog.records if r.name.startswith("jsa")][-1]
    assert "41" in closing


def test_the_closing_line_does_not_count_the_old_search_and_fetch_events(
    web, profile, caplog
):
    web.routes[PERPLEXITY] = event_stream(
        [
            *[("response.reasoning.search_results", {})] * 7,
            *[("response.reasoning.fetch_url_results", {})] * 7,
            ("response.output_text.done", {"text": answer_with([])}),
            ("response.completed", {"response": {"model": MODEL}}),
        ]
    )
    caplog.set_level(logging.INFO, logger="jsa")
    with make_client() as client:
        PerplexityRunner(client).run("a prompt")
    closing = [r.getMessage() for r in caplog.records if r.name.startswith("jsa")][-1]
    assert "7" not in closing
    assert "14" not in closing


def test_fold_ignores_events_it_does_not_know():
    state = fold_events(
        [("response.something.new", {"a": 1}), ("response.created", {})]
    )
    assert state == StreamState()


def test_a_completed_event_without_usage_leaves_cost_unknown():
    state = fold_events([("response.completed", {"response": {"model": "m"}})])
    assert state.cost is None
    assert state.model == "m"


def test_fold_is_pure():
    events = stream_for("answer", deltas=("a", "b"))
    assert fold_events(events) == fold_events(events)
    start = StreamState()
    fold(start, "response.output_text.done", {"text": "changed"})
    assert start == StreamState()


def test_the_runner_reports_the_perplexity_model_and_no_effort(web, profile):
    web.perplexity(answer_with([]))
    with make_client() as client:
        result = PerplexityRunner(client).run("a prompt")
    assert result.model == MODEL
    assert result.effort is None
    assert result.cost == COST
    assert json.loads(result.text) == {"postings": []}


def test_the_runner_answers_with_the_done_text_when_the_stream_also_has_deltas(
    web, profile
):
    web.perplexity(answer_with([]), deltas=("{", "junk"))
    with make_client() as client:
        result = PerplexityRunner(client).run("a prompt")
    assert json.loads(result.text) == {"postings": []}


def test_the_runner_emits_a_heartbeat_at_most_every_five_seconds(
    web, profile, monkeypatch, caplog
):
    clock = Tick(1)
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, clock))
    web.perplexity(answer_with([]), deltas=["x"] * 60)
    caplog.set_level(logging.INFO, logger="jsa")
    with make_client() as client:
        PerplexityRunner(client).run("a prompt")
    traced = [r for r in caplog.records if r.name.startswith("jsa")]
    assert 1 <= len(traced) <= clock.now / 5 + 2


# --- ending the run ends Perplexity's work (PRD 01 "Timeouts/limits") ------------------


class Watched(httpx.SyncByteStream):
    """A response body that serves one event per chunk and notes how much was read and whether it was closed."""

    def __init__(self, events, on_chunk=lambda _n: None):
        self.chunks = [sse([event]).encode() for event in events]
        self.on_chunk = on_chunk
        self.read = 0
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            self.read += 1
            self.on_chunk(self.read)
            yield chunk

    def close(self):
        self.closed = True


def watched_stream(web, events, on_chunk=lambda _n: None):
    body = Watched(events, on_chunk)
    web.routes[PERPLEXITY] = httpx.Response(
        200, stream=body, headers={"content-type": "text/event-stream"}
    )
    return body


def test_a_run_that_finishes_leaves_no_stream_open(web, profile):
    body = watched_stream(web, stream_for(answer_with([])))
    with make_client() as client:
        PerplexityRunner(client).run("a prompt")
    assert body.closed


@pytest.mark.parametrize("failure", ["response.failed", "error"])
def test_a_failure_event_closes_the_stream_without_reading_on(web, profile, failure):
    body = watched_stream(
        web,
        [
            (STEP_EVENT, {}),
            (failure, {"error": {"message": "the model fell over"}}),
            *[(STEP_EVENT, {})] * 5,
            ("response.output_text.done", {"text": answer_with([])}),
        ],
    )
    with make_client() as client, pytest.raises(JsaError):
        PerplexityRunner(client).run("a prompt")
    assert body.closed
    assert body.read < len(body.chunks)


def test_a_run_past_the_ceiling_closes_the_stream_without_reading_on(
    web, profile, monkeypatch
):
    class Late:
        """In time until the stream's third event, past the ceiling after it."""

        now = 0.0

        def __call__(self):
            return self.now

    clock = Late()
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, clock))

    def pass_the_ceiling(chunk):
        if chunk == 3:
            clock.now = 3601.0

    body = watched_stream(
        web,
        [(STEP_EVENT, {})] * 10 + stream_for(answer_with([])),
        on_chunk=pass_the_ceiling,
    )
    with make_client() as client, pytest.raises(WallClockExceeded):
        PerplexityRunner(client).run("a prompt")
    assert body.closed
    assert body.read < len(body.chunks)


def test_a_stream_that_ends_without_an_answer_is_an_error_and_closed(web, profile):
    body = watched_stream(web, [(STEP_EVENT, {}), (STEP_EVENT, {})])
    with make_client() as client, pytest.raises(JsaError):
        PerplexityRunner(client).run("a prompt")
    assert body.closed


def test_an_interrupt_while_reading_closes_the_stream(web, profile):
    def interrupt(chunk):
        if chunk == 2:
            raise KeyboardInterrupt

    body = watched_stream(
        web, [(STEP_EVENT, {})] * 5 + stream_for("x"), on_chunk=interrupt
    )
    with make_client() as client, pytest.raises(KeyboardInterrupt):
        PerplexityRunner(client).run("a prompt")
    assert body.closed


def test_a_cli_search_that_fails_mid_stream_closes_the_stream(
    world, monkeypatch, capsys
):
    body = watched_stream(
        world.web,
        [(STEP_EVENT, {}), ("response.failed", {}), (STEP_EVENT, {})],
    )
    code, _out, _err = run(monkeypatch, capsys)
    assert code != 0
    assert body.closed
    assert [r["outcome"] for r in search_runs(world.url)] == ["failed"]


# --- the API key -----------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_a_missing_key_raises_a_runtime_error_naming_it_before_any_request(
    world, monkeypatch, value
):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("PERPLEXITY_API_KEY", value)
    with (
        make_client() as client,
        pytest.raises(RuntimeError, match="PERPLEXITY_API_KEY"),
    ):
        search.search(client, "perplexity", 24)
    assert world.web.requests == []


def test_the_cli_reports_a_missing_key_cleanly_and_makes_no_request(
    world, monkeypatch, capsys
):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    world.web.posted(entry(world.web.job()))
    code, _out, err = run(monkeypatch, capsys)
    assert code != 0
    assert "PERPLEXITY_API_KEY" in err
    assert "Traceback" not in err
    assert world.web.requests == []
    assert count_rows(world.url, "postings") == 0


def test_commands_that_do_not_use_the_key_run_without_it(world, monkeypatch):
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["jsa", "init-db"])
    cli.main()


# --- the wall-clock ceiling ------------------------------------------------------


def test_a_request_may_wait_the_read_timeout_or_the_time_left_whichever_is_less():
    clock = Tick(0)
    deadline = Deadline(clock=clock)
    for elapsed, allowed in [
        (0, 1800),
        (1800, 1800),
        (1801, 1799),
        (3000, 600),
        (3599, 1),
    ]:
        clock.now = elapsed
        assert deadline.request_timeout() == pytest.approx(allowed)
    clock.now = 3601
    with pytest.raises(WallClockExceeded):
        deadline.request_timeout()


def test_the_ceiling_is_3600_seconds_and_a_run_past_it_raises():
    assert runners.WALL_CLOCK_CEILING_SECONDS == 3600
    clock = Tick(0)
    deadline = Deadline(clock=clock)
    clock.now = 3600
    deadline.check()
    clock.now = 3601
    with pytest.raises(WallClockExceeded):
        deadline.check()


def test_a_request_late_in_the_run_gets_only_the_time_left_as_its_read_timeout(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Jump(3000)))
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    (request,) = perplexity_requests(world.web)
    assert request.extensions["timeout"]["read"] == 600


def test_a_request_with_one_second_left_gets_a_one_second_read_timeout(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Jump(3599)))
    run(monkeypatch, capsys)
    (request,) = perplexity_requests(world.web)
    assert request.extensions["timeout"]["read"] == pytest.approx(1)


def test_no_request_is_sent_once_the_ceiling_has_passed(world, monkeypatch, capsys):
    world.web.posted(entry(world.web.job()))
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Jump(3601)))
    code, _out, _err = run(monkeypatch, capsys)
    assert code != 0
    assert perplexity_requests(world.web) == []
    assert count_rows(world.url, "postings") == 0


def test_a_search_that_runs_past_the_ceiling_raises_and_inserts_nothing(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()), deltas=["x"] * 10)
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Tick(1000)))
    code, _out, _err = run(monkeypatch, capsys)
    assert code != 0
    assert count_rows(world.url, "postings") == 0
    assert count_rows(world.url, "search_findings") == 0
    (row,) = search_runs(world.url)
    assert row["outcome"] == "failed"


def test_the_same_stream_completes_when_the_clock_stays_under_the_ceiling(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()), deltas=["x"] * 10)
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Tick(1)))
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert count_rows(world.url, "postings") == 1


# --- findings and the verification gate ------------------------------------------


def test_every_emitted_posting_gets_one_finding_and_only_the_verified_one_is_inserted(
    world, monkeypatch, capsys
):
    web = world.web
    verified = web.job()
    out_of_window = web.job(updated_at=hours_ago(24 * 10))
    absent = gh_url()
    unsupported = off_four_url()
    web.posted(
        entry(verified),
        entry(out_of_window),
        {"company": "No URL", "title": "Broken"},
        entry(absent),
        entry(LINKEDIN_URL),
        entry(unsupported),
    )
    code, out, _err = run(monkeypatch, capsys, window="24")
    assert code == 0
    rows = {f["canonical_url"]: f for f in findings(world.url)}
    assert len(rows) == 5
    expected = {
        verified: ("verified", 1),
        out_of_window: ("out_of_window", 2),
        absent: ("not_on_index", 3),
        LINKEDIN_URL: ("aggregator", 4),
        unsupported: ("unsupported", 5),
    }
    for url, (outcome, rank) in expected.items():
        found = rows[canonicalize_url(url)]
        assert found["verification"] == outcome, url
        assert found["rank"] == rank, url
        assert found["window_hours"] == 24
        assert found["agent"] == "perplexity"
        assert found["model"] == MODEL
        assert found["effort"] is None
        assert found["found_at"]
        assert found["run_date"] == run_date_now()
    assert rows[canonicalize_url(verified)]["ats_date_kind"] == "updated"
    assert rows[canonicalize_url(verified)]["ats_date"]
    assert rows[canonicalize_url(out_of_window)]["ats_date"]
    assert [p["canonical_url"] for p in postings(world.url)] == [
        canonicalize_url(verified)
    ]
    summary = summary_of(out)
    for key, value in {
        "found": "5",
        "verified": "1",
        "out_of_window": "1",
        "not_on_index": "1",
        "aggregator": "1",
        "unsupported": "1",
        "malformed": "1",
        "inserted": "1",
        "jd_captured": "1",
        "already_present": "0",
        "fetch_failed": "0",
    }.items():
        assert summary[key] == value, key


def test_the_verified_posting_is_stored_with_its_agent_jd_and_ats_title(
    world, monkeypatch, capsys
):
    url = world.web.job()
    world.web.posted(entry(url, title="Agent's transcription"))
    run(monkeypatch, capsys)
    stored = posting_of(world.url, url)
    assert stored["search_agent"] == "perplexity"
    assert "Build the platform." in stored["jd_markdown"]
    assert stored["location"] == "Remote, US"
    assert stored["title"] == f"ATS Title {job_id_of(url)}"


def test_a_verified_no_date_posting_is_inserted(world, monkeypatch, capsys):
    url = world.web.job(updated_at="none")
    world.web.posted(entry(url))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert finding_of(world.url, url)["verification"] == "verified_no_date"
    assert finding_of(world.url, url)["ats_date"] is None
    assert len(postings(world.url)) == 1
    assert summary_of(out)["verified_no_date"] == "1"


def test_under_best_effort_reachable_postings_are_inserted_and_closed_pages_are_not(
    world, monkeypatch, capsys
):
    best_effort(world.profile)
    dated, undated, closed = off_four_url(), off_four_url(), off_four_url()
    route_page(world.web, dated, html(job_posting_page(datePosted=hours_ago(2))))
    route_page(world.web, undated, html("<html><body>Apply</body></html>"))
    route_page(world.web, closed, httpx.Response(404))
    world.web.posted(entry(dated), entry(undated), entry(closed))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    outcomes = {f["canonical_url"]: f for f in findings(world.url)}
    assert outcomes[canonicalize_url(dated)]["verification"] == "reachable"
    assert outcomes[canonicalize_url(dated)]["ats_date_kind"] == "published"
    assert outcomes[canonicalize_url(undated)]["verification"] == "reachable_no_date"
    assert outcomes[canonicalize_url(closed)]["verification"] == "page_closed"
    stored = {p["canonical_url"] for p in postings(world.url)}
    assert stored == {canonicalize_url(dated), canonicalize_url(undated)}
    summary = summary_of(out)
    assert (summary["reachable"], summary["reachable_no_date"]) == ("1", "1")
    assert summary["page_closed"] == "1"
    assert summary["mode"] == "best_effort"


def test_an_off_four_posting_is_captured_from_the_page_already_fetched(
    world, monkeypatch, capsys
):
    best_effort(world.profile)
    url = off_four_url()
    route_page(world.web, url, html(job_posting_page(datePosted=hours_ago(2))))
    world.web.posted(entry(url))
    run(monkeypatch, capsys)
    assert "Build the platform." in posting_of(world.url, url)["jd_markdown"]
    assert world.web.count(httpx.URL(url).host, httpx.URL(url).path) == 1


def test_a_board_index_is_fetched_once_however_many_postings_it_holds(
    world, monkeypatch, capsys
):
    urls = [world.web.job() for _ in range(3)]
    world.web.posted(*[entry(u) for u in urls])
    run(monkeypatch, capsys)
    assert world.web.count("boards-api.greenhouse.io", "/v1/boards/acme/jobs") == 1
    assert len(postings(world.url)) == 3


def test_an_ashby_board_is_fetched_once_for_verification_and_capture(
    world, monkeypatch, capsys
):
    ids = [
        "8d7c6b5a-4f3e-4d2c-b1a0-9e8f7a6b5c01",
        "8d7c6b5a-4f3e-4d2c-b1a0-9e8f7a6b5c02",
    ]
    world.web.routes[("api.ashbyhq.com", "/posting-api/job-board/acme")] = {
        "jobs": [
            {
                "id": job_id,
                "title": "Ashby Title",
                "descriptionHtml": JD_HTML,
                "location": "Remote",
                "publishedAt": hours_ago(1),
            }
            for job_id in ids
        ]
    }
    urls = [f"https://jobs.ashbyhq.com/acme/{job_id}" for job_id in ids]
    world.web.posted(*[entry(u) for u in urls])
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert world.web.count("api.ashbyhq.com") == 1
    assert summary_of(out)["jd_captured"] == "2"
    for url in urls:
        assert "Build the platform." in posting_of(world.url, url)["jd_markdown"]


def test_a_rippling_detail_is_fetched_once_for_verification_and_capture(
    world, monkeypatch, capsys
):
    job_id = "c4b5a697-8f0e-4a1b-8c2d-3e4f5a6b7c8d"
    world.web.routes[("ats.rippling.com", "/api/v2/board/acme/jobs")] = {
        "totalPages": 1,
        "items": [{"id": job_id}],
    }
    detail_path = f"/api/v2/board/acme/jobs/{job_id}"
    world.web.routes[("ats.rippling.com", detail_path)] = {
        "createdOn": hours_ago(1),
        "name": "Rippling Title",
        "description": {"role": "<p>Role text.</p>", "company": "<p>Company text.</p>"},
        "workLocations": ["Remote"],
    }
    url = f"https://ats.rippling.com/acme/jobs/{job_id}"
    world.web.posted(entry(url))
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert finding_of(world.url, url)["verification"] == "verified"
    assert world.web.count("ats.rippling.com", detail_path) == 1
    assert "Role text." in posting_of(world.url, url)["jd_markdown"]


def test_one_unverifiable_board_does_not_stop_the_others(world, monkeypatch, capsys):
    broken = world.web.job("down")
    healthy = world.web.job("up")
    world.web.routes[("boards-api.greenhouse.io", "/v1/boards/down/jobs")] = (
        httpx.Response(500)
    )
    world.web.posted(entry(broken), entry(healthy))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert finding_of(world.url, broken)["verification"] == "unverifiable"
    assert finding_of(world.url, healthy)["verification"] == "verified"
    assert [p["canonical_url"] for p in postings(world.url)] == [
        canonicalize_url(healthy)
    ]
    assert summary_of(out)["unverifiable"] == "1"


def test_a_finding_is_written_for_a_dropped_posting_even_when_nothing_is_inserted(
    world, monkeypatch, capsys
):
    gone = gh_url()
    world.web.boards["acme"] = {}
    world.web.posted(entry(gone))
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert finding_of(world.url, gone)["verification"] == "not_on_index"
    assert count_rows(world.url, "postings") == 0


# --- already present, failed capture, repeated URLs ------------------------------


def test_an_already_present_posting_is_counted_recorded_and_not_captured_again(
    world, monkeypatch, capsys
):
    url = world.web.job()
    conn = db.connect()
    db.insert_posting(
        conn, company="Original Co", title="Original", url=url, search_agent="claude"
    )
    conn.execute(
        "UPDATE postings SET decision = 'Apply' WHERE canonical_url = ?",
        (canonicalize_url(url),),
    )
    conn.close()
    world.web.posted(entry(url, company="Perplexity Co"))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    summary = summary_of(out)
    assert summary["already_present"] == "1"
    assert summary["inserted"] == "0"
    assert summary["jd_captured"] == "0"
    detail_path = f"/v1/boards/acme/jobs/{job_id_of(url)}"
    assert world.web.count("boards-api.greenhouse.io", detail_path) == 0
    stored = posting_of(world.url, url)
    assert stored["jd_markdown"] is None
    assert stored["search_agent"] == "claude"
    found = finding_of(world.url, url)
    assert found["verification"] == "verified"
    assert found["agent"] == "perplexity"
    # The denormalized decision is current from the finding's first write.
    assert found["decision"] == "Apply"
    assert count_rows(world.url, "postings") == 1


def test_a_failed_capture_keeps_the_row_with_no_jd_and_counts_fetch_failed(
    world, monkeypatch, capsys
):
    failing, fine = world.web.job(), world.web.job()
    world.web.broken_details.add(job_id_of(failing))
    world.web.posted(entry(failing), entry(fine))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert posting_of(world.url, failing)["jd_markdown"] is None
    assert "Build the platform." in posting_of(world.url, fine)["jd_markdown"]
    summary = summary_of(out)
    assert summary["inserted"] == "2"
    assert summary["fetch_failed"] == "1"
    assert summary["jd_captured"] == "1"
    (row,) = search_runs(world.url)
    assert row["outcome"] == "ok"


def test_a_url_emitted_twice_produces_one_finding_at_its_first_rank(
    world, monkeypatch, capsys
):
    first, second = world.web.job(), world.web.job()
    world.web.posted(
        entry(first),
        entry(second),
        entry(first + "/"),
        entry(first + "?utm_source=newsletter"),
    )
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert len(findings(world.url)) == 2
    assert finding_of(world.url, first)["rank"] == 1
    assert finding_of(world.url, second)["rank"] == 2
    assert summary_of(out)["found"] == "2"
    assert summary_of(out)["inserted"] == "2"


def test_rank_counts_valid_postings_and_a_repeated_url_keeps_its_slot(
    world, monkeypatch, capsys
):
    first, second, third = world.web.job(), world.web.job(), world.web.job()
    world.web.posted(
        {"company": "Broken"},
        entry(first),
        entry(first),
        entry(second),
        {"title": "Also broken", "url": third},
        entry(third),
    )
    code, _out, _err = run(monkeypatch, capsys)
    assert code == 0
    # Valid postings in order: first (1), first again (2), second (3), third (4).
    assert finding_of(world.url, first)["rank"] == 1
    assert finding_of(world.url, second)["rank"] == 3
    assert finding_of(world.url, third)["rank"] == 4
    assert len(findings(world.url)) == 3


def test_a_second_search_the_same_day_leaves_the_first_finding_unchanged(
    world, monkeypatch, capsys
):
    kept, other = world.web.job(), world.web.job()
    world.web.posted(entry(kept), entry(other))
    run(monkeypatch, capsys, window="24")
    before = finding_of(world.url, kept)
    # The second run finds it at another rank and window, and now it is gone from the board.
    world.web.boards["acme"].pop(job_id_of(kept))
    world.web.posted(entry(other), entry(kept), model="another/model")
    code, out, _err = run(monkeypatch, capsys, window="72")
    assert code == 0
    assert finding_of(world.url, kept) == before
    matching = [
        f for f in findings(world.url) if f["canonical_url"] == before["canonical_url"]
    ]
    assert len(matching) == 1
    assert count_rows(world.url, "search_runs") == 2
    assert summary_of(out)["not_on_index"] == "1"


def test_another_agents_finding_of_the_same_url_is_a_separate_row(
    world, monkeypatch, capsys
):
    url = world.web.job()
    conn = db.connect()
    db.record_finding(
        conn,
        run_date=run_date_now(),
        agent="gemini",
        canonical_url=canonicalize_url(url),
        window_hours=24,
        rank=1,
        verification="verified",
        ats_date=None,
        ats_date_kind=None,
        model=None,
        effort=None,
    )
    conn.close()
    world.web.posted(entry(url))
    run(monkeypatch, capsys)
    assert sorted(f["agent"] for f in findings(world.url)) == ["gemini", "perplexity"]


# --- output that cannot be used --------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["I could not find any postings today, sorry.", '{"results": []}'],
    ids=["prose", "no-postings-array"],
)
def test_unparseable_output_raises_after_logging_it_and_closes_the_run_failed(
    world, monkeypatch, capsys, caplog, text
):
    world.web.perplexity(text)
    caplog.set_level(logging.INFO)
    code, _out, _err = run(monkeypatch, capsys)
    assert code != 0
    assert text in caplog.text
    assert count_rows(world.url, "postings") == 0
    assert count_rows(world.url, "search_findings") == 0
    (row,) = search_runs(world.url)
    assert row["outcome"] == "failed"
    assert row["error"]
    assert "\n" not in row["error"]
    assert row["finished_at"]


def test_an_empty_postings_array_closes_the_run_ok_with_no_inserts(
    world, monkeypatch, capsys
):
    world.web.posted()
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    (row,) = search_runs(world.url)
    assert row["outcome"] == "ok"
    assert row["error"] is None
    assert json.loads(row["summary"])["found"] == 0
    assert count_rows(world.url, "postings") == 0
    assert count_rows(world.url, "search_findings") == 0
    assert summary_of(out)["found"] == "0"


def test_an_api_failure_closes_the_run_failed_and_inserts_nothing(
    world, monkeypatch, capsys
):
    world.web.routes[PERPLEXITY] = httpx.Response(500, json={"error": "boom"})
    # The error may surface as an exit code or as an exception; either is a failed command.
    try:
        code, _out, _err = run(monkeypatch, capsys)
    except httpx.HTTPError, JsaError:
        code = 1
    assert code != 0
    assert count_rows(world.url, "postings") == 0
    (row,) = search_runs(world.url)
    assert row["outcome"] == "failed"
    assert row["error"]


# --- telemetry -------------------------------------------------------------------


def test_the_cli_prints_the_summary_with_every_key(world, monkeypatch, capsys):
    world.web.posted(entry(world.web.job()))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    summary = summary_of(out)
    assert sorted(summary) == sorted(SUMMARY_KEYS)
    assert summary["agent"] == "perplexity"
    assert summary["model"] == MODEL
    assert summary["mode"] == "strict"
    assert float(summary["cost"]) == COST


def test_a_clean_run_prints_no_warnings_and_stores_none(world, monkeypatch, capsys):
    world.web.posted(entry(world.web.job()))
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    assert "warning" not in out.lower()
    (row,) = search_runs(world.url)
    assert row["warnings"] is None


def test_the_closing_warnings_are_printed_and_stored(world, monkeypatch, capsys):
    best_effort(world.profile)
    no_date_gh = world.web.job(updated_at="none")
    undated_pages = [off_four_url(), off_four_url()]
    for url in undated_pages:
        route_page(world.web, url, html("<html><body>Apply</body></html>"))
    world.web.posted(
        entry(no_date_gh),
        *[entry(u) for u in undated_pages],
        {"company": "x"},
        {"title": "y"},
        {"url": "z"},
    )
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    (row,) = search_runs(world.url)
    lines = row["warnings"].splitlines()
    assert any("greenhouse" in line.lower() for line in lines)
    assert any("reachable" in line.lower() and "2" in line for line in lines)
    assert any("malformed" in line.lower() and "3" in line for line in lines)
    assert len(lines) == 3
    for line in lines:
        assert line in out


def test_a_run_with_only_malformed_postings_warns_with_the_count(
    world, monkeypatch, capsys
):
    world.web.posted({"company": "x"}, {"title": "y", "url": 5})
    code, out, _err = run(monkeypatch, capsys)
    assert code == 0
    (row,) = search_runs(world.url)
    assert "malformed" in row["warnings"].lower()
    assert "2" in row["warnings"]
    assert summary_of(out)["malformed"] == "2"


def test_a_successful_search_leaves_a_closed_hand_run_row_with_a_json_summary(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()), entry(gh_url()))
    code, out, _err = run(monkeypatch, capsys, window="36")
    assert code == 0
    (row,) = search_runs(world.url)
    assert row["trigger"] == "hand"
    assert row["outcome"] == "ok"
    assert row["error"] is None
    assert row["agent"] == "perplexity"
    assert row["window_hours"] == 36
    assert row["mode"] == "strict"
    assert row["run_date"] == run_date_now()
    assert row["started_at"]
    assert row["finished_at"]
    assert row["model"] == MODEL
    stored = json.loads(row["summary"])
    assert set(SUMMARY_KEYS) - {"errors"} <= set(stored)
    assert stored["found"] == 2
    assert stored["verified"] == 1
    assert stored["inserted"] == 1
    assert stored["cost"] == COST
    assert summary_of(out)["found"] == "2"


def test_the_run_row_is_open_before_the_runner_starts(world, monkeypatch, capsys):
    seen = []

    def runner(_request):
        seen.extend(search_runs(world.url))
        return event_stream(stream_for(answer_with([])))

    world.web.routes[PERPLEXITY] = runner
    code, _out, _err = run(monkeypatch, capsys, window="48")
    assert code == 0
    (opened,) = seen
    assert opened["trigger"] == "hand"
    assert opened["agent"] == "perplexity"
    assert opened["window_hours"] == 48
    assert opened["mode"] == "strict"
    assert opened["run_date"] == run_date_now()
    assert opened["started_at"]
    assert opened["outcome"] is None
    assert opened["finished_at"] is None


def test_a_hand_search_neither_checks_nor_takes_the_cron_claim(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    for _ in range(2):
        code, _out, _err = run(monkeypatch, capsys)
        assert code == 0
        assert count_rows(world.url, "cron_runs") == 0


# --- the test seam ---------------------------------------------------------------


def test_a_search_reaches_only_perplexity_and_the_ats_through_the_shared_client(
    world, monkeypatch, capsys
):
    world.web.posted(entry(world.web.job()))
    run(monkeypatch, capsys)
    hosts = {request.url.host for request in world.web.requests}
    assert hosts == {"api.perplexity.ai", "boards-api.greenhouse.io"}
