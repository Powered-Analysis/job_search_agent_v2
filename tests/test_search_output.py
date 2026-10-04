"""PRD 01 "Output contract & parsing" (XC-9): the wire contract and its tolerant parser."""

import json
import logging

import pytest

from jsa.errors import JsaError
from jsa.search_output import parse_search_output

ONE = {
    "company": "Acme",
    "title": "Data Engineer",
    "url": "https://job-boards.greenhouse.io/acme/jobs/1",
}
TWO = {
    "company": "Globex",
    "title": "Analytics Engineer",
    "url": "https://jobs.lever.co/globex/abc",
    "date_posted": "2026-03-01",
}


def urls(parsed):
    return [posting.url for posting in parsed.postings]


# --- accepted shapes --------------------------------------------------------


def test_the_full_object_is_parsed():
    parsed = parse_search_output(json.dumps({"postings": [ONE, TWO]}))
    assert urls(parsed) == [ONE["url"], TWO["url"]]
    assert parsed.malformed == 0


def test_postings_keep_their_fields_and_order():
    parsed = parse_search_output(json.dumps({"postings": [ONE, TWO]}))
    first, second = parsed.postings
    assert (first.company, first.title, first.url) == (
        "Acme",
        "Data Engineer",
        ONE["url"],
    )
    assert first.date_posted is None
    assert (second.company, second.title) == ("Globex", "Analytics Engineer")
    assert second.date_posted is not None
    assert second.date_posted.startswith("2026-03-01")


def test_a_bare_array_is_wrapped():
    parsed = parse_search_output(json.dumps([ONE, TWO]))
    assert urls(parsed) == [ONE["url"], TWO["url"]]
    assert parsed.malformed == 0


def test_json_inside_a_markdown_fence_is_parsed():
    text = "```json\n" + json.dumps({"postings": [ONE]}) + "\n```"
    assert urls(parse_search_output(text)) == [ONE["url"]]


def test_a_bare_fence_without_a_language_is_parsed():
    text = "```\n" + json.dumps({"postings": [ONE]}) + "\n```"
    assert urls(parse_search_output(text)) == [ONE["url"]]


def test_a_fenced_bare_array_is_parsed():
    text = "```json\n" + json.dumps([ONE, TWO]) + "\n```"
    assert urls(parse_search_output(text)) == [ONE["url"], TWO["url"]]


def test_json_surrounded_by_prose_is_parsed():
    text = (
        "I found these postings for you:\n"
        + json.dumps({"postings": [ONE]})
        + "\nHope that helps!"
    )
    assert urls(parse_search_output(text)) == [ONE["url"]]


def test_a_fence_surrounded_by_prose_is_parsed():
    text = (
        "Here you go.\n```json\n"
        + json.dumps({"postings": [ONE, TWO]})
        + "\n```\nLet me know."
    )
    assert urls(parse_search_output(text)) == [ONE["url"], TWO["url"]]


def test_a_bare_array_surrounded_by_prose_is_parsed():
    text = "Results:\n" + json.dumps([ONE]) + "\nDone."
    assert urls(parse_search_output(text)) == [ONE["url"]]


def test_brackets_in_the_prose_before_the_json_do_not_hide_it():
    text = "Found [3] candidates {see below}:\n" + json.dumps({"postings": [ONE]})
    assert urls(parse_search_output(text)) == [ONE["url"]]


def test_braces_and_brackets_inside_strings_do_not_break_extraction():
    tricky = {
        "company": "Acme {Labs}",
        "title": "Engineer [Data] }{",
        "url": "https://x.example/1",
    }
    parsed = parse_search_output(
        "prose " + json.dumps({"postings": [tricky]}) + " more prose"
    )
    assert parsed.postings[0].title == "Engineer [Data] }{"
    assert parsed.postings[0].company == "Acme {Labs}"


def test_unicode_survives_parsing():
    entry = {
        "company": "Café Münch",
        "title": "Ingénieur données",
        "url": "https://x.example/é",
    }
    assert (
        parse_search_output(json.dumps({"postings": [entry]})).postings[0].company
        == "Café Münch"
    )


def test_an_empty_postings_array_parses_to_zero_postings():
    parsed = parse_search_output('{"postings": []}')
    assert parsed.postings == []
    assert parsed.malformed == 0


def test_an_empty_bare_array_parses_to_zero_postings():
    parsed = parse_search_output("[]")
    assert parsed.postings == []
    assert parsed.malformed == 0


def test_an_empty_array_in_a_fence_parses_to_zero_postings():
    parsed = parse_search_output('```json\n{"postings": []}\n```')
    assert parsed.postings == []
    assert parsed.malformed == 0


def test_extra_keys_on_a_posting_do_not_invalidate_it():
    parsed = parse_search_output(
        json.dumps({"postings": [{**ONE, "salary": "n/a", "rank": 1}]})
    )
    assert urls(parsed) == [ONE["url"]]
    assert parsed.malformed == 0


# --- unparseable output -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "I could not find any postings today, sorry.",
        "",
        "   \n",
        "```\nnot json at all\n```",
        '{"postings": [',
    ],
    ids=["prose", "empty", "blank", "fence-without-json", "truncated"],
)
def test_text_with_no_json_value_raises(text):
    with pytest.raises(JsaError):
        parse_search_output(text)


@pytest.mark.parametrize(
    "payload",
    [
        {"results": [ONE]},
        {},
        {"postings": "none"},
        {"postings": None},
        {"postings": {"a": ONE}},
    ],
    ids=["other-key", "empty-object", "string", "null", "object"],
)
def test_a_json_object_without_a_postings_array_raises(payload):
    with pytest.raises(JsaError):
        parse_search_output(json.dumps(payload))


def test_raw_text_is_logged_when_there_is_no_json(caplog):
    text = "No JSON here, only distinctive-marker-4821 prose."
    with caplog.at_level(logging.DEBUG), pytest.raises(JsaError):
        parse_search_output(text)
    assert "distinctive-marker-4821" in caplog.text


def test_raw_text_is_logged_when_the_object_has_no_postings(caplog):
    text = '{"results": [], "note": "distinctive-marker-9917"}'
    with caplog.at_level(logging.DEBUG), pytest.raises(JsaError):
        parse_search_output(text)
    assert "distinctive-marker-9917" in caplog.text


# --- per-posting validation -------------------------------------------------


def test_a_posting_missing_its_url_is_dropped_and_counted_and_the_others_kept():
    no_url = {"company": "Initech", "title": "Engineer"}
    parsed = parse_search_output(json.dumps({"postings": [ONE, no_url, TWO]}))
    assert urls(parsed) == [ONE["url"], TWO["url"]]
    assert parsed.malformed == 1


def test_a_posting_with_a_non_string_company_is_dropped_and_counted_and_the_others_kept():
    bad = {**ONE, "company": 12345}
    parsed = parse_search_output(json.dumps({"postings": [bad, TWO]}))
    assert urls(parsed) == [TWO["url"]]
    assert parsed.malformed == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"title": "Engineer", "url": "https://x.example/1"},
        {"company": "Acme", "url": "https://x.example/1"},
        {"company": "Acme", "title": "Engineer"},
        {"company": None, "title": "Engineer", "url": "https://x.example/1"},
        {"company": "Acme", "title": ["Engineer"], "url": "https://x.example/1"},
        {"company": "Acme", "title": "Engineer", "url": 42},
        {"company": "Acme", "title": "Engineer", "url": None},
        {"company": ["Acme"], "title": "Engineer", "url": "https://x.example/1"},
        {},
    ],
    ids=[
        "no-company",
        "no-title",
        "no-url",
        "null-company",
        "list-title",
        "int-url",
        "null-url",
        "list-company",
        "empty-object",
    ],
)
def test_a_posting_with_a_missing_or_non_string_required_field_is_malformed(bad):
    parsed = parse_search_output(json.dumps({"postings": [ONE, bad, TWO]}))
    assert urls(parsed) == [ONE["url"], TWO["url"]]
    assert parsed.malformed == 1


@pytest.mark.parametrize(
    "entry", ["a string", 7, None, [1, 2]], ids=["string", "number", "null", "array"]
)
def test_an_entry_that_is_not_an_object_is_malformed(entry):
    parsed = parse_search_output(json.dumps({"postings": [ONE, entry]}))
    assert urls(parsed) == [ONE["url"]]
    assert parsed.malformed == 1


def test_every_invalid_posting_is_counted():
    bad = {"company": "Initech"}
    parsed = parse_search_output(json.dumps({"postings": [bad, ONE, bad, bad]}))
    assert urls(parsed) == [ONE["url"]]
    assert parsed.malformed == 3


def test_an_all_malformed_response_returns_no_postings_without_raising():
    parsed = parse_search_output(
        json.dumps({"postings": [{"company": "x"}, {"url": "y"}]})
    )
    assert parsed.postings == []
    assert parsed.malformed == 2


def test_a_malformed_posting_is_logged_with_its_raw_entry(caplog):
    bad = {"company": "Initech", "note": "distinctive-marker-3305"}
    with caplog.at_level(logging.DEBUG):
        parse_search_output(json.dumps({"postings": [bad, ONE]}))
    assert "distinctive-marker-3305" in caplog.text


@pytest.mark.parametrize(
    "date",
    [
        "not a date",
        "yesterday",
        "3 days ago",
        "2026-13-45",
        "",
        20260301,
        1.5,
        ["2026-03-01"],
        {"d": 1},
    ],
    ids=[
        "words",
        "relative",
        "relative-n",
        "impossible",
        "empty",
        "int",
        "float",
        "list",
        "object",
    ],
)
def test_an_invalid_date_posted_is_discarded_and_the_posting_kept(date):
    parsed = parse_search_output(
        json.dumps({"postings": [{**ONE, "date_posted": date}]})
    )
    assert urls(parsed) == [ONE["url"]]
    assert parsed.postings[0].date_posted is None
    assert parsed.malformed == 0


@pytest.mark.parametrize(
    "date",
    [
        "2026-03-01",
        "2026-03-01T09:30:00Z",
        "2026-03-01T09:30:00+00:00",
        "2026-03-01T09:30:00",
    ],
)
def test_a_valid_iso_date_posted_is_kept(date):
    parsed = parse_search_output(
        json.dumps({"postings": [{**ONE, "date_posted": date}]})
    )
    assert parsed.postings[0].date_posted is not None
    assert parsed.postings[0].date_posted.startswith("2026-03-01")


def test_a_null_date_posted_is_no_date_and_not_malformed():
    parsed = parse_search_output(
        json.dumps({"postings": [{**ONE, "date_posted": None}]})
    )
    assert parsed.postings[0].date_posted is None
    assert parsed.malformed == 0
