"""`jsa search --agent gemini` (issue #9; PRD 01 "Gemini runner"; XC-9, XC-14).

The Gemini client is replaced at its one point, `gemini.make_client`, with a scripted fake.
"""

import logging
import sys
from types import SimpleNamespace

import httpx
import pytest
from conftest import drop_all_tables
from profile_helpers import SEARCH_TOML, copy_example, write_search_toml
from search_helpers import Boards, rows

from jsa import cli, db, gemini
from jsa.errors import JsaError
from jsa.gemini import GeminiAgentRunner
from jsa.profile import GeminiRunner as GeminiSettings
from jsa.runners import Deadline, RunnerResult, WallClockExceeded

AGENT = "deep-research-preview-04-2026"
SETTINGS = GeminiSettings(agent=AGENT)
KEY = "test-gemini-key"
INTERACTION = "interaction-1"
ANSWER = '{"postings": []}'
USAGE = {
    "total_input_tokens": 100_000,
    "total_cached_tokens": 20_000,
    "total_output_tokens": 5_000,
    "total_thought_tokens": 2_000,
    "total_tool_use_tokens": 10_000,
}


# --- the scripted client ------------------------------------------------------------------


class Wire:
    """What the SDK hands back: an object that dumps to the event or interaction dict."""

    def __init__(self, data):
        self.data = data

    def model_dump(self, **_ignored):
        return dict(self.data)


def created(event_id="e1"):
    return {
        "event_type": "interaction.created",
        "event_id": event_id,
        "interaction": {"id": INTERACTION, "status": "in_progress"},
    }


def thought(event_id):
    return {
        "event_type": "step.delta",
        "event_id": event_id,
        "index": 0,
        "delta": {"type": "thought_summary", "content": {"text": "thinking"}},
    }


def text_delta(text, event_id):
    return {
        "event_type": "step.delta",
        "event_id": event_id,
        "index": 1,
        "delta": {"type": "text", "text": text},
    }


def status(value, event_id):
    return {
        "event_type": "interaction.status_update",
        "event_id": event_id,
        "interaction_id": INTERACTION,
        "status": value,
    }


def completed(event_id, **interaction):
    return {
        "event_type": "interaction.completed",
        "event_id": event_id,
        "interaction": {"id": INTERACTION, "status": "completed", **interaction},
    }


def finished(text=ANSWER, usage=USAGE):
    fields = {"id": INTERACTION, "status": "completed", "output_text": text}
    if usage is not None:
        fields["usage"] = usage
    return fields


def drops(events):
    """A stream that yields `events`, then the connection fails mid-run."""

    def stream():
        yield from (Wire(event) for event in events)
        raise httpx.ReadError("connection reset")

    return stream()


class Gemini:
    """Stands in for `genai.Client`.

    `created` is the stream `interactions.create` returns; `reconnects` are the streams
    `interactions.get(..., stream=True)` returns, in turn; `reads` are what a plain
    `interactions.get(id)` returns, in turn, the last one repeating.
    """

    def __init__(self):
        self.created = [created("e1"), thought("e2"), completed("e3")]
        self.reconnects = []
        self.reads = [finished()]
        self.create_calls = []
        self.reconnect_calls = []
        self.read_calls = []
        self.interactions = SimpleNamespace(create=self._create, get=self._get)

    def _create(self, **kwargs):
        self.create_calls.append(kwargs)
        return self._stream(self.created)

    def _get(self, interaction_id, **kwargs):
        if kwargs.get("stream"):
            self.reconnect_calls.append({"id": interaction_id, **kwargs})
            assert len(self.reconnect_calls) < 50, "the run never ends"
            return self._stream(self.reconnects.pop(0) if self.reconnects else [])
        self.read_calls.append(interaction_id)
        assert len(self.read_calls) < 50, "the run never ends"
        read = self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]
        return Wire(read)

    @staticmethod
    def _stream(events):
        if hasattr(events, "__next__"):
            return events
        return iter([Wire(event) for event in events])

    @property
    def create_call(self):
        (call,) = self.create_calls
        return call


@pytest.fixture
def fake(monkeypatch):
    stand_in = Gemini()
    monkeypatch.setattr(gemini, "make_client", lambda _key: stand_in)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    return stand_in


def runner():
    return GeminiAgentRunner(SETTINGS, sleep=lambda _seconds: None)


class Tick:
    """A clock that moves `step` seconds each time it is read."""

    def __init__(self, step):
        self.step = step
        self.now = -step

    def __call__(self):
        self.now += self.step
        return float(self.now)


def tool_names(tools):
    return sorted(t["type"] if isinstance(t, dict) else t for t in tools)


# --- the request --------------------------------------------------------------------------


def test_the_create_call_uses_the_configured_agent_in_the_background_and_streamed(fake):
    runner().run("find me roles")
    call = fake.create_call
    assert call["agent"] == AGENT
    assert call["background"] is True
    assert call["stream"] is True
    assert call["input"] == "find me roles"


def test_the_create_call_carries_the_deep_research_agent_config(fake):
    runner().run("p")
    assert fake.create_call["agent_config"] == {
        "type": "deep-research",
        "thinking_summaries": "auto",
        "visualization": "off",
        "collaborative_planning": False,
    }


def test_the_tools_are_exactly_google_search_and_url_context(fake):
    runner().run("p")
    assert tool_names(fake.create_call["tools"]) == ["google_search", "url_context"]


def test_the_runner_makes_exactly_one_create_call_for_a_run_that_completes(fake):
    runner().run("p")
    assert len(fake.create_calls) == 1
    assert fake.reconnect_calls == []


def test_the_client_is_built_from_the_gemini_api_key(monkeypatch):
    keys = []
    stand_in = Gemini()
    monkeypatch.setattr(gemini, "make_client", lambda key: keys.append(key) or stand_in)
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    runner().run("p")
    assert keys == [KEY]


# --- what the runner reports ----------------------------------------------------------------


def test_the_runner_reports_the_agent_id_as_its_model_and_no_effort(fake):
    result = runner().run("p")
    assert isinstance(result, RunnerResult)
    assert (result.model, result.effort) == (AGENT, None)


def test_the_runner_returns_the_completed_interactions_text(fake):
    fake.reads = [finished('{"postings": [1]}')]
    assert runner().run("p").text == '{"postings": [1]}'


def test_the_completed_interactions_text_wins_over_the_joined_deltas(fake):
    fake.created = [
        created("e1"),
        text_delta("partial ", "e2"),
        text_delta("deltas", "e3"),
        completed("e4"),
    ]
    fake.reads = [finished("the whole answer")]
    assert runner().run("p").text == "the whole answer"


# --- folding the stream ---------------------------------------------------------------------


def test_folding_counts_thought_steps_and_records_the_ids():
    state = gemini.fold_events(
        [
            created("e1"),
            thought("e2"),
            thought("e3"),
            text_delta("x", "e4"),
            thought("e5"),
        ]
    )
    assert state.thought_steps == 3
    assert state.interaction_id == INTERACTION
    assert state.last_event_id == "e5"


def test_folding_an_empty_stream_counts_nothing():
    state = gemini.fold_events([])
    assert state.thought_steps == 0
    assert state.interaction_id is None
    assert state.last_event_id is None


def test_folding_prefers_the_completed_interactions_text_over_the_deltas():
    state = gemini.fold_events(
        [
            created("e1"),
            text_delta("partial ", "e2"),
            text_delta("deltas", "e3"),
            completed("e4"),
        ]
    )
    state = gemini.fold_interaction(state, finished("the whole answer"))
    assert state.text == "the whole answer"


def test_folding_joins_the_text_deltas_when_no_completed_text_arrives():
    state = gemini.fold_events(
        [created("e1"), text_delta("one ", "e2"), text_delta("two", "e3")]
    )
    assert state.text == "one two"


def test_a_text_delta_is_not_counted_as_a_thought_step():
    assert gemini.fold_events([text_delta("x", "e1")]).thought_steps == 0


def test_folding_ignores_events_it_does_not_know():
    state = gemini.fold_events(
        [created("e1"), {"event_type": "step.start", "event_id": "e2", "index": 0}]
    )
    assert state.thought_steps == 0
    assert state.interaction_id == INTERACTION


def test_the_fold_is_pure():
    events = [created("e1"), thought("e2"), text_delta("x", "e3")]
    snapshot = [dict(event) for event in events]
    before = gemini.StreamState()
    first = gemini.fold_events(events)
    assert gemini.fold_events(events) == first
    assert events == snapshot
    after_one = gemini.fold(before, events[0])
    assert before == gemini.StreamState()
    assert after_one != before


# --- reconnecting ---------------------------------------------------------------------------


@pytest.mark.parametrize("drop", ["ends", "transport error"])
def test_a_dropped_stream_reconnects_from_the_last_event_id_and_finishes(fake, drop):
    first = [created("e1"), thought("e2"), thought("e3")]
    fake.created = first if drop == "ends" else drops(first)
    fake.reconnects = [[text_delta("the rest", "e4"), completed("e5")]]
    fake.reads = [finished("the full text")]
    result = runner().run("p")
    assert result.text == "the full text"
    (call,) = fake.reconnect_calls
    assert call["id"] == INTERACTION
    assert call["stream"] is True
    assert call["last_event_id"] == "e3"
    assert len(fake.create_calls) == 1


def test_a_second_drop_reconnects_from_the_event_the_first_reconnect_reached(fake):
    fake.created = [created("e1"), thought("e2")]
    fake.reconnects = [[thought("e3"), thought("e4")], [completed("e5")]]
    fake.reads = [finished("done")]
    assert runner().run("p").text == "done"
    assert [c["last_event_id"] for c in fake.reconnect_calls] == ["e2", "e4"]


def test_the_closing_line_counts_the_thought_steps_from_before_and_after_a_drop(
    fake, caplog
):
    fake.created = [created("e1"), thought("e2"), thought("e3")]
    fake.reconnects = [[thought("e4"), completed("e5")]]
    caplog.set_level(logging.INFO, logger="jsa")
    runner().run("p")
    assert "3 thought" in caplog.text


def test_a_stream_that_drops_without_any_progress_still_finishes_by_polling(fake):
    fake.created = [created("e1")]
    fake.reconnects = [[], [], []]
    fake.reads = [
        {"id": INTERACTION, "status": "in_progress"},
        {"id": INTERACTION, "status": "in_progress"},
        finished("polled answer"),
    ]
    assert runner().run("p").text == "polled answer"


def test_the_ceiling_spans_reconnects_and_raises_instead_of_waiting_forever(
    fake, monkeypatch
):
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, Tick(1000)))
    fake.created = [created("e1")]
    fake.reconnects = [[thought(f"e{n}")] for n in range(2, 40)]
    fake.reads = [{"id": INTERACTION, "status": "in_progress"}]
    with pytest.raises(WallClockExceeded):
        runner().run("p")


# --- failures -------------------------------------------------------------------------------


def test_an_error_event_raises(fake):
    fake.created = [
        created("e1"),
        thought("e2"),
        {
            "event_type": "error",
            "event_id": "e3",
            "error": {"code": "internal", "message": "the agent fell over"},
        },
    ]
    with pytest.raises(JsaError):
        runner().run("p")


def test_a_failed_status_event_raises(fake):
    fake.created = [created("e1"), status("failed", "e2")]
    with pytest.raises(JsaError):
        runner().run("p")


def test_a_failed_status_found_by_the_status_check_raises(fake):
    fake.created = [created("e1")]
    fake.reconnects = [[]]
    fake.reads = [{"id": INTERACTION, "status": "failed"}]
    with pytest.raises(JsaError):
        runner().run("p")


def test_a_failure_ends_the_run_instead_of_reconnecting(fake):
    fake.created = [created("e1"), status("failed", "e2")]
    fake.reconnects = [[completed("e3")]]
    with pytest.raises(JsaError):
        runner().run("p")
    assert fake.reconnect_calls == []


def test_a_completed_run_with_no_text_is_an_error_not_an_empty_answer(fake):
    fake.reads = [finished("")]
    with pytest.raises(JsaError):
        runner().run("p")


def test_an_unrelated_exception_is_not_swallowed_as_a_drop(fake):
    def broken():
        yield Wire(created("e1"))
        raise ValueError("not a transport problem")

    fake.created = broken()
    with pytest.raises(ValueError, match="not a transport problem"):
        runner().run("p")


# --- cost -----------------------------------------------------------------------------------


def test_given_usage_the_cost_is_an_estimate_labeled_as_one(fake, caplog):
    caplog.set_level(logging.INFO, logger="jsa")
    result = runner().run("p")
    assert result.cost is not None
    assert result.cost > 0
    assert result.cost_is_estimate is True
    assert "estimate" in caplog.text.lower()
    assert "cost unknown" not in caplog.text


def test_the_estimated_cost_grows_with_the_tokens_used(fake):
    fake.reads = [finished(usage={**USAGE, "total_output_tokens": 5_000})]
    smaller = runner().run("p").cost
    fake.reads = [finished(usage={**USAGE, "total_output_tokens": 500_000})]
    larger = runner().run("p").cost
    assert larger > smaller


def test_without_usage_the_log_says_cost_unknown_and_the_run_still_succeeds(
    fake, caplog
):
    fake.reads = [finished(usage=None)]
    caplog.set_level(logging.INFO, logger="jsa")
    result = runner().run("p")
    assert result.text == ANSWER
    assert result.cost is None
    assert "cost unknown" in caplog.text


def test_the_closing_line_reports_steps_seconds_and_cost(fake, caplog):
    fake.created = [created("e1"), thought("e2"), thought("e3"), completed("e4")]
    caplog.set_level(logging.INFO, logger="jsa")
    runner().run("p")
    lines = [r.getMessage() for r in caplog.records if "2 thought" in r.getMessage()]
    assert lines
    assert any(
        "$" in line and ("s," in line or " s" in line or "second" in line)
        for line in lines
    )


def test_the_runner_emits_a_heartbeat_at_most_every_five_seconds(
    fake, monkeypatch, caplog
):
    clock = Tick(1)
    monkeypatch.setattr(Deadline.__init__, "__defaults__", (3600, clock))
    fake.created = [created("e1")] + [thought(f"e{n}") for n in range(2, 62)]
    fake.created.append(completed("e99"))
    caplog.set_level(logging.INFO, logger="jsa")
    runner().run("p")
    traced = [r for r in caplog.records if r.name.startswith("jsa")]
    assert 1 <= len(traced) <= clock.now / 5 + 3


# --- the API key ----------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_a_missing_key_raises_a_runtime_error_naming_it_before_any_call(
    fake, monkeypatch, value
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        runner().run("p")
    assert fake.create_calls == []
    assert fake.read_calls == []


# --- jsa search --agent gemini ----------------------------------------------------------------


@pytest.fixture
def world(db_url, fake, tmp_path, monkeypatch):
    drop_all_tables(db_url)
    db.connect().close()
    boards = Boards()
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda _self, r: boards.handle(r)
    )
    profile = copy_example(tmp_path / "profile")
    write_search_toml(profile, SEARCH_TOML)
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    # Gemini searches need no Perplexity key, and no stray `.env` may supply anything.
    monkeypatch.delenv("PERPLEXITY_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(url=db_url, boards=boards, fake=fake, profile=profile)


def jsa_search_gemini(monkeypatch, capsys, window="24"):
    monkeypatch.setattr(
        sys, "argv", ["jsa", "search", "--agent", "gemini", "--window-hours", window]
    )
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def posted(url):
    return (
        '{"postings": [{"company": "Acme", "title": "Staff Engineer", '
        f'"url": "{url}"}}]}}'
    )


def test_search_offers_gemini_as_an_agent(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jsa", "search", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    assert "gemini" in capsys.readouterr().out


def test_a_gemini_search_records_findings_with_the_agent_id_as_model_and_null_effort(
    world, monkeypatch, capsys
):
    live = world.boards.job()
    world.fake.reads = [finished(posted(live))]
    code, out, _err = jsa_search_gemini(monkeypatch, capsys, window="48")
    assert code == 0
    assert rows(
        world.url, "SELECT agent, window_hours, model, effort FROM search_findings"
    ) == [("gemini", 48, AGENT, None)]
    assert rows(world.url, "SELECT search_agent FROM postings") == [("gemini",)]
    assert rows(
        world.url, "SELECT agent, window_hours, model, effort, outcome FROM search_runs"
    ) == [("gemini", 48, AGENT, None, "ok")]
    assert "agent: gemini" in out


def test_the_search_prompt_reaches_gemini_as_the_input(world, monkeypatch, capsys):
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys, window="36")
    assert code == 0
    assert "36" in world.fake.create_call["input"]
    assert world.fake.create_call["agent"] == AGENT


def test_the_agent_comes_from_the_profile_not_from_the_app(world, monkeypatch, capsys):
    toml = world.profile / "search" / "search.toml"
    toml.write_text(toml.read_text().replace(AGENT, "some-other-agent-id"))
    live = world.boards.job()
    world.fake.reads = [finished(posted(live))]
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys)
    assert code == 0
    assert world.fake.create_call["agent"] == "some-other-agent-id"
    assert rows(world.url, "SELECT model FROM search_findings") == [
        ("some-other-agent-id",)
    ]


def test_a_gemini_search_with_no_postings_closes_ok_and_inserts_nothing(
    world, monkeypatch, capsys
):
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys)
    assert code == 0
    assert rows(world.url, "SELECT outcome FROM search_runs") == [("ok",)]
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(0,)]


def test_the_run_summary_labels_the_cost_an_estimate(world, monkeypatch, capsys):
    code, out, err = jsa_search_gemini(monkeypatch, capsys)
    assert code == 0
    assert "estimate" in (out + err).lower()


def test_a_search_without_usage_succeeds_and_does_not_claim_an_estimate(
    world, monkeypatch, capsys
):
    world.fake.reads = [finished(usage=None)]
    code, out, err = jsa_search_gemini(monkeypatch, capsys)
    assert code == 0
    assert "estimate" not in (out + err).lower()


@pytest.mark.parametrize("failure", ["error event", "failed status"])
def test_a_failed_gemini_run_closes_the_run_failed_and_inserts_nothing(
    world, monkeypatch, capsys, failure
):
    live = world.boards.job()
    world.fake.reads = [finished(posted(live))]
    if failure == "failed status":
        world.fake.created = [created("e1"), status("failed", "e2")]
    else:
        world.fake.created = [
            created("e1"),
            {
                "event_type": failure.removesuffix(" event"),
                "event_id": "e2",
                "error": {"message": "boom"},
            },
        ]
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys)
    assert code != 0
    assert [r[0] for r in rows(world.url, "SELECT outcome FROM search_runs")] == [
        "failed"
    ]
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(0,)]
    assert rows(world.url, "SELECT COUNT(*) FROM search_findings") == [(0,)]


def test_an_unparseable_gemini_answer_fails_the_run_and_inserts_nothing(
    world, monkeypatch, capsys
):
    world.fake.reads = [finished("I could not find anything, sorry.")]
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys)
    assert code != 0
    assert [r[0] for r in rows(world.url, "SELECT outcome FROM search_runs")] == [
        "failed"
    ]
    assert rows(world.url, "SELECT COUNT(*) FROM search_findings") == [(0,)]


def test_a_gemini_answer_wrapped_in_fences_and_prose_is_parsed(
    world, monkeypatch, capsys
):
    live = world.boards.job()
    world.fake.reads = [
        finished(
            f"Here are the roles I found:\n```json\n{posted(live)}\n```\nGood luck!"
        )
    ]
    code, _out, _err = jsa_search_gemini(monkeypatch, capsys)
    assert code == 0
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(1,)]


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_the_cli_reports_a_missing_key_cleanly_before_any_call(
    world, monkeypatch, capsys, value
):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if value is not None:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    code, _out, err = jsa_search_gemini(monkeypatch, capsys)
    assert code != 0
    assert "GEMINI_API_KEY" in err
    assert "Traceback" not in err
    assert world.fake.create_calls == []
    assert rows(world.url, "SELECT COUNT(*) FROM postings") == [(0,)]


def test_a_profile_without_the_gemini_table_fails_before_any_call_or_run_row(
    world, monkeypatch, capsys
):
    toml = world.profile / "search" / "search.toml"
    text = toml.read_text()
    toml.write_text(
        text.replace("[runners.gemini]", "[runners.other]").replace(AGENT, "x")
    )
    code, _out, err = jsa_search_gemini(monkeypatch, capsys)
    assert code != 0
    assert "search.toml" in err
    assert world.fake.create_calls == []
    assert rows(world.url, "SELECT COUNT(*) FROM search_runs") == [(0,)]
