"""The email side door (issue #87; PRD 03 "Email side door" and "Manual-add side door"; XC-1, XC-3, XC-5, XC-9).

`gws` is replaced by a stateful fake Gmail behind `JSA_GWS_BIN`; outside HTTP is replaced beneath the
app's shared client; the database is the libSQL container; the profile is a
temporary directory.
"""

import base64
import json
import logging
import shutil
import sys
from email.message import EmailMessage
from pathlib import Path

import httpx
import pytest
from conftest import REPO_ROOT
from profile_helpers import ROLE_FAMILIES, copy_example
from test_add import (
    LONG_AGO,
    count,
    descriptionless_page,
    gh_routes,
    gh_url,
    html_response,
    off_four_url,
    page_key,
    posting,
    seed_decided,
    stored_role_family,
)
from test_generate import Agent

from jsa import agent_loop, cli, db
from jsa.inbox import addressed_role_family, parse_posting, sender_allowed
from jsa.urls import canonicalize_url

LOCAL_DATABASE_HOSTS = {"127.0.0.1", "localhost"}
SENDER = "jordan.example@example.com"
MAILBOX_CREDENTIAL = "MAILBOX-CREDENTIAL-111"
OWNER_CREDENTIAL = "OWNER-CREDENTIAL-222"
BODY_MARKER = "PRIVATE-BODY-MARKER-9f3"
PLAIN_ADDRESS = "jobs.mailbox@example.net"
# PRD 03: `+tag` is the role family's name lowercased, so this names the example profile's `Data Analysis`.
DATA_ANALYSIS_ADDRESS = "jobs.mailbox+data-analysis@example.net"

GOOD_RESULTS = (
    "mx.google.com; dkim=pass header.i=@example.com header.s=sel; "
    "spf=pass smtp.mailfrom=jordan.example@example.com; "
    "dmarc=pass (p=NONE sp=NONE dis=NONE) header.from=example.com"
)
FAILING_RESULTS = (
    "mx.google.com; dkim=fail header.i=@example.com; "
    "dmarc=fail (p=REJECT sp=REJECT dis=NONE) header.from=example.com"
)
FORGED_RESULTS = "mx.google.com; dmarc=pass header.from=example.com"

# 500+ characters, one line, no leading or trailing whitespace.
DESCRIPTION = " ".join(f"responsibility{i}" for i in range(60))
assert len(DESCRIPTION) >= 500

# What the fake `gws` does: a Gmail mailbox kept in $FAKE_MAILBOX, every call logged into it.
FAKE_GWS = """\
#!{python}
import base64, email, json, os, sys

argv = sys.argv[1:]
state_path = os.environ["FAKE_MAILBOX"]
state = json.load(open(state_path))
tokens = argv[: argv.index("--params")] if "--params" in argv else argv
params = json.loads(argv[argv.index("--params") + 1]) if "--params" in argv else {{}}
body = json.loads(argv[argv.index("--json") + 1]) if "--json" in argv else None

seen = []
candidates = [v for k, v in os.environ.items() if not k.startswith(("JSA_", "FAKE_"))] + argv
for value in list(candidates):
    try:
        if os.path.isfile(value):
            candidates.append(open(value).read())
    except (OSError, ValueError):
        pass
for name, secret in state["credentials"].items():
    if any(secret in value for value in candidates):
        seen.append(name)
state["calls"].append({{"tokens": tokens, "params": params, "body": body, "credentials": seen}})


def finish(code, out=None, err=None):
    json.dump(state, open(state_path, "w"))
    if err:
        print(err, file=sys.stderr)
    if out is not None:
        print(json.dumps(out))
    sys.exit(code)


labels = state["labels"]
messages = state["messages"]
method = next(t for t in reversed(tokens) if t in ("list", "get", "modify", "create"))
if "labels" in tokens:
    if method == "list":
        finish(0, {{"labels": [{{"id": i, "name": n}} for n, i in labels.items()]}})
    if body["name"] in labels:
        finish(1, err="409 label exists")
    labels[body["name"]] = "Label_%d" % (len(labels) + 1)
    finish(0, {{"id": labels[body["name"]], "name": body["name"]}})

if method == "list":
    wanted = params.get("labelIds") or ["INBOX"]
    found = [i for i, m in messages.items() if all(w in m["labelIds"] for w in wanted)]
    found.sort(key=lambda i: -int(messages[i]["internalDate"]))
    finish(0, {{"messages": [{{"id": i, "threadId": i}} for i in found]}} if found else {{"resultSizeEstimate": 0}})

message = messages[params["id"]]
if method == "get":
    if params.get("format") == "metadata":
        names = [n.lower() for n in params.get("metadataHeaders", [])]
        headers = [
            {{"name": n, "value": v}} for n, v in message["headers"] if not names or n.lower() in names
        ]
        finish(0, {{"id": params["id"], "internalDate": message["internalDate"],
                   "labelIds": message["labelIds"], "payload": {{"headers": headers}}}})
    if params.get("format") == "raw":
        if params["id"] in state["fail_raw"]:
            finish(1, err="500 backend error")
        finish(0, {{"id": params["id"], "internalDate": message["internalDate"], "raw": message["raw"]}})
    finish(1, err="unsupported format")

if params["id"] in state["fail_modify"]:
    finish(1, err="500 backend error")
message["labelIds"] = [
    i for i in message["labelIds"] if i not in body.get("removeLabelIds", [])
]
message["labelIds"] += [i for i in body.get("addLabelIds", []) if i not in message["labelIds"]]
finish(0, {{"id": params["id"], "labelIds": message["labelIds"]}})
"""


class FakeGmail:
    """The jobs mailbox as the fake `gws` serves it."""

    def __init__(self, path: Path):
        self.path = path
        self._write(
            {
                "labels": {"INBOX": "INBOX"},
                "messages": {},
                "calls": [],
                "fail_raw": [],
                "fail_modify": [],
                "credentials": {
                    "mailbox": MAILBOX_CREDENTIAL,
                    "owner": OWNER_CREDENTIAL,
                },
            }
        )

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def _write(self, state: dict) -> None:
        self.path.write_text(json.dumps(state))

    def add(
        self,
        message_id: str,
        date: int,
        text: str | None = None,
        *,
        html: str | None = None,
        sender: str = f"Jordan Example <{SENDER}>",
        to: str = PLAIN_ADDRESS,
        cc: str | None = None,
        results: list[str] | str | None = GOOD_RESULTS,
    ) -> None:
        mime = EmailMessage()
        mime["From"] = sender
        mime["To"] = to
        if cc is not None:
            mime["Cc"] = cc
        mime["Subject"] = "a job"
        if text is not None:
            mime.set_content(text)
            if html is not None:
                mime.add_alternative(html, subtype="html")
        else:
            mime.set_content(html, subtype="html")
        headers = [["From", sender]]
        for value in [results] if isinstance(results, str) else results or []:
            headers.append(["Authentication-Results", value])
        state = self._read()
        state["messages"][message_id] = {
            "internalDate": str(date),
            "labelIds": ["INBOX"],
            "headers": headers,
            "raw": base64.urlsafe_b64encode(mime.as_bytes()).decode().rstrip("="),
        }
        self._write(state)

    def requeue(self, message_id: str) -> None:
        state = self._read()
        state["messages"][message_id]["labelIds"].append("INBOX")
        self._write(state)

    def archive(self, message_id: str) -> None:
        state = self._read()
        state["messages"][message_id]["labelIds"] = []
        self._write(state)

    def fail(self, operation: str, message_id: str) -> None:
        state = self._read()
        state[f"fail_{operation}"].append(message_id)
        self._write(state)

    def recover(self, operation: str) -> None:
        state = self._read()
        state[f"fail_{operation}"] = []
        self._write(state)

    def create_label(self, name: str) -> None:
        state = self._read()
        state["labels"][name] = f"Existing_{len(state['labels'])}"
        self._write(state)

    def in_inbox(self, message_id: str) -> bool:
        return "INBOX" in self._read()["messages"][message_id]["labelIds"]

    def label_ids(self) -> dict[str, str]:
        return self._read()["labels"]

    def outcome_labels(self, message_id: str) -> list[str]:
        state = self._read()
        names = {label_id: name for name, label_id in state["labels"].items()}
        ids = state["messages"][message_id]["labelIds"]
        return sorted(names[i] for i in ids if names[i].startswith("jsa/"))

    def calls(self) -> list[dict]:
        return self._read()["calls"]

    def calls_for(self, message_id: str, method: str) -> list[dict]:
        return [
            call
            for call in self.calls()
            if call["params"].get("id") == message_id and call["tokens"][-1] == method
        ]

    def raw_fetches(self, message_id: str) -> list[dict]:
        return [
            call
            for call in self.calls_for(message_id, "get")
            if call["params"].get("format") != "metadata"
        ]


@pytest.fixture(autouse=True)
def agent(monkeypatch):
    """Claude is replaced at the shared agent loop (XC-12) wherever a packet is built."""
    stand_in = Agent()
    monkeypatch.setattr(agent_loop, "query", stand_in.query)
    return stand_in


@pytest.fixture
def web(monkeypatch):
    """Route every outside HTTP call to `web[(host, path)]`; anything unrouted is a 404."""
    routes = {}

    def handle(self, request):
        if request.url.host in LOCAL_DATABASE_HOSTS:
            raise AssertionError(f"unexpected local request {request.url}")
        answer = routes.get((request.url.host, request.url.path))
        if answer is None:
            return httpx.Response(404, json={"error": "not found"})
        if isinstance(answer, httpx.Response):
            return answer
        return httpx.Response(200, json=answer)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    return routes


@pytest.fixture
def profile(tmp_path, monkeypatch):
    directory = copy_example(tmp_path / "profile")
    with (directory / "config.toml").open("a", encoding="utf-8") as file:
        file.write(
            f'\n[inbox]\napp = "jsa-inbox-example"\nsenders = ["{SENDER}"]\n'
            'drive_folder_id = "folder-123"\n'
        )
    monkeypatch.setenv("JSA_PROFILE_DIR", str(directory))
    return directory


@pytest.fixture
def gmail(tmp_path, profile, monkeypatch, caplog) -> FakeGmail:
    stub = tmp_path / "fake-gws"
    stub.write_text(FAKE_GWS.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("JSA_GWS_BIN", str(stub))
    monkeypatch.setenv("FAKE_MAILBOX", str(tmp_path / "mailbox.json"))
    monkeypatch.setenv("JSA_INBOX_GWS_CREDENTIALS", MAILBOX_CREDENTIAL)
    monkeypatch.setenv("JSA_GWS_CREDENTIALS", OWNER_CREDENTIAL)
    caplog.set_level(logging.DEBUG)
    return FakeGmail(tmp_path / "mailbox.json")


def run_inbox(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["jsa", "inbox"])
    try:
        cli.main()
    except SystemExit as exit_:
        return exit_.code if isinstance(exit_.code, int) else 1
    return 0


def ats_posting(web):
    url = gh_url()
    web.update(gh_routes(url))
    return url


def bare_posting(web):
    """An off-ATS posting whose page names company and title but carries no description."""
    url = off_four_url()
    web[page_key(url)] = html_response(descriptionless_page())
    return url


# --- the sender check and the email parse are pure (XC-9) ----------------------


def headers_of(*results, sender=f"Jordan Example <{SENDER}>"):
    return [("From", sender), *[("Authentication-Results", r) for r in results]]


@pytest.mark.parametrize(
    ("headers", "senders", "allowed"),
    [
        (headers_of(GOOD_RESULTS), [SENDER], True),
        (headers_of(GOOD_RESULTS, sender=SENDER), [SENDER], True),
        (headers_of(GOOD_RESULTS), ["other@example.com", SENDER], True),
        (headers_of(GOOD_RESULTS), ["other@example.com"], False),
        (headers_of(GOOD_RESULTS), [], False),
        (headers_of(FAILING_RESULTS), [SENDER], False),
        (headers_of(), [SENDER], False),
        (
            headers_of("mx.google.com; dmarc=pass header.from=other.example"),
            [SENDER],
            False,
        ),
        (
            headers_of("mx.google.com; dkim=pass header.i=@example.com; spf=pass"),
            [SENDER],
            False,
        ),
        (
            headers_of("mx.google.com; dmarc=none header.from=example.com"),
            [SENDER],
            False,
        ),
        (
            headers_of("mx.google.com; dmarc=temperror header.from=example.com"),
            [SENDER],
            False,
        ),
        # A sender can write an Authentication-Results header; only the topmost (Gmail's) counts.
        (headers_of(FAILING_RESULTS, FORGED_RESULTS), [SENDER], False),
        (headers_of(GOOD_RESULTS, FORGED_RESULTS), [SENDER], True),
        (
            [("Authentication-Results", GOOD_RESULTS), ("From", SENDER)],
            [SENDER],
            True,
        ),
    ],
    ids=[
        "display-name-form",
        "bare-address",
        "one-of-several-senders",
        "sender-not-listed",
        "no-senders-configured",
        "dmarc-fail",
        "no-authentication-results",
        "dmarc-pass-for-another-domain",
        "no-dmarc-result",
        "dmarc-none",
        "dmarc-temperror",
        "forged-pass-below-gmails-fail",
        "forged-header-below-gmails-pass",
        "header-order-in-message",
    ],
)
def test_sender_check(headers, senders, allowed):
    assert sender_allowed(headers, senders) is allowed


def mime_bytes(text=None, html=None) -> bytes:
    mime = EmailMessage()
    mime["From"] = SENDER
    mime["Subject"] = "a job"
    if text is not None:
        mime.set_content(text)
        if html is not None:
            mime.add_alternative(html, subtype="html")
    else:
        mime.set_content(html, subtype="html")
    return mime.as_bytes()


URL = "https://job-boards.greenhouse.io/acme-widgets/jobs/123456"


def test_the_url_is_the_first_http_url_of_the_plain_text_part():
    text = f"Look at this one {URL} and also https://example.org/other\n"
    parsed = parse_posting(mime_bytes(text))
    assert parsed.url == URL
    assert parsed.description is None


def test_an_http_url_is_found_as_well_as_an_https_one():
    parsed = parse_posting(mime_bytes("http://careers.example.com/jobs/7\n"))
    assert parsed.url == "http://careers.example.com/jobs/7"


def test_without_a_plain_text_part_the_html_part_is_read_as_text():
    html = f'<html><body><p>This one:</p><p><a href="{URL}">{URL}</a></p></body></html>'
    parsed = parse_posting(mime_bytes(html=html))
    assert parsed.url == URL
    assert parsed.description is None


def test_the_plain_text_part_wins_over_the_html_part():
    other = "https://careers.example.com/jobs/html-only"
    parsed = parse_posting(mime_bytes(f"{URL}\n", html=f"<p>{other}</p>"))
    assert parsed.url == URL


def test_a_url_only_in_the_html_part_is_not_used_when_there_is_a_plain_text_part():
    parsed = parse_posting(mime_bytes("No link here, sorry.\n", html=f"<p>{URL}</p>"))
    assert parsed.url is None


def test_a_message_with_no_url_has_none():
    assert parse_posting(mime_bytes("Just a thought, no link.\n")).url is None


@pytest.mark.parametrize(
    ("length", "supplied"),
    [(0, False), (40, False), (499, False), (500, True), (501, True), (2000, True)],
    ids=["none", "signature", "499", "500", "501", "2000"],
)
def test_text_beside_the_url_is_a_description_only_from_500_characters(
    length, supplied
):
    text = "d" * length
    parsed = parse_posting(mime_bytes(f"{URL}\n\n{text}\n"))
    assert parsed.url == URL
    assert parsed.description == (text if supplied else None)


def test_the_url_is_removed_from_the_description_and_the_whitespace_trimmed():
    parsed = parse_posting(mime_bytes(f"\n\n  {DESCRIPTION}  {URL}  \n\n\n"))
    assert parsed.url == URL
    assert URL not in parsed.description
    assert parsed.description == parsed.description.strip()
    assert DESCRIPTION in parsed.description


def test_a_description_in_the_html_part_counts_too():
    html = f"<html><body><p>{URL}</p><p>{DESCRIPTION}</p></body></html>"
    parsed = parse_posting(mime_bytes(html=html))
    assert parsed.url == URL
    assert DESCRIPTION in parsed.description


# --- an empty Inbox ----------------------------------------------------------


def test_an_empty_inbox_does_nothing(db_url, conn, gmail, web, monkeypatch):
    before = count(conn, "postings")
    assert run_inbox(monkeypatch) == 0
    assert [call["tokens"][-1] for call in gmail.calls()] == ["list"]
    assert count(conn, "postings") == before


def test_only_messages_in_the_inbox_are_processed(
    db_url, conn, gmail, web, monkeypatch
):
    gmail.add("archived", 1000, "nothing to see\n")
    gmail.archive("archived")
    gmail.add("waiting", 2000, "nothing to see\n")
    run_inbox(monkeypatch)
    assert gmail.calls_for("archived", "get") == []
    assert gmail.outcome_labels("waiting") == ["jsa/refused"]


# --- order (criterion 1) -----------------------------------------------------


def test_messages_are_processed_oldest_first_by_arrival_time(
    db_url, gmail, web, monkeypatch
):
    urls = {name: ats_posting(web) for name in ("newest", "oldest", "middle")}
    # Ids that sort differently from arrival time.
    gmail.add("a-newest", 3000, urls["newest"] + "\n")
    gmail.add("c-oldest", 1000, urls["oldest"] + "\n")
    gmail.add("b-middle", 2000, urls["middle"] + "\n")
    assert run_inbox(monkeypatch) == 0
    # Opened after the run: a connection idle through it could have its stream expire.
    conn = db.connect()
    ids = {name: posting(conn, url)["id"] for name, url in urls.items()}
    conn.close()
    assert ids["oldest"] < ids["middle"] < ids["newest"]


def test_a_message_that_arrived_earlier_is_labeled_earlier(
    db_url, conn, gmail, web, monkeypatch
):
    gmail.add("z-first", 1000, "no link\n")
    gmail.add("a-last", 9000, "no link\n")
    gmail.add("m-second", 5000, "no link\n")
    run_inbox(monkeypatch)
    labeled = [
        call["params"]["id"] for call in gmail.calls() if call["tokens"][-1] == "modify"
    ]
    assert labeled == ["z-first", "m-second", "a-last"]


# --- outcome labels (PRD 03's table) -----------------------------------------


def test_a_message_with_no_url_is_refused_and_archived(
    db_url, conn, gmail, web, monkeypatch
):
    before = count(conn, "postings")
    gmail.add("m1", 1000, "Remember to look at jobs tonight.\n")
    assert run_inbox(monkeypatch) == 0
    assert gmail.outcome_labels("m1") == ["jsa/refused"]
    assert not gmail.in_inbox("m1")
    assert count(conn, "postings") == before


@pytest.mark.parametrize(
    "url",
    [
        "https://www.linkedin.com/jobs/view/3999000111",
        "https://www.indeed.com/viewjob?jk=0123456789abcdef",
    ],
)
def test_an_aggregator_url_is_refused_and_nothing_is_written(
    db_url, conn, gmail, web, monkeypatch, url
):
    before = count(conn, "postings")
    gmail.add("m1", 1000, f"{url}\n\n{DESCRIPTION}\n")
    assert run_inbox(monkeypatch) == 0
    assert gmail.outcome_labels("m1") == ["jsa/refused"]
    assert not gmail.in_inbox("m1")
    assert count(conn, "postings") == before
    assert count(conn, "postings", url) == 0


def test_a_posting_whose_company_or_title_cannot_be_derived_writes_nothing(
    db_url, conn, gmail, web, monkeypatch
):
    url = off_four_url()  # the page is unrouted: no JobPosting data, no board slug
    before = count(conn, "postings")
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert gmail.outcome_labels("m1") == ["jsa/needs-fields"]
    assert not gmail.in_inbox("m1")
    assert count(conn, "postings") == before
    assert posting(conn, url) is None


def test_needs_fields_even_when_a_description_was_supplied(
    db_url, conn, gmail, web, monkeypatch
):
    url = off_four_url()
    before = count(conn, "postings")
    gmail.add("m1", 1000, f"{url}\n\n{DESCRIPTION}\n")
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("m1") == ["jsa/needs-fields"]
    assert count(conn, "postings") == before


def test_a_posting_without_any_description_is_added_apply_and_labeled_needs_jd(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n\nSent from my iPhone\n", to=DATA_ANALYSIS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["decided_at"] is not None
    assert row["search_agent"] == "manual"
    assert row["jd_markdown"] is None
    assert (row["company"], row["title"]) == ("Example Corp", "Staff Platform Engineer")
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]
    assert not gmail.in_inbox("m1")


def test_a_supported_ats_posting_is_added_apply_with_its_captured_description(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["search_agent"] == "manual"
    assert "Build the platform" in row["jd_markdown"]


def test_an_emailed_posting_writes_no_search_finding(
    db_url, conn, gmail, web, monkeypatch
):
    before = count(conn, "search_findings")
    gmail.add("m1", 1000, ats_posting(web) + "\n")
    run_inbox(monkeypatch)
    assert count(conn, "search_findings") == before


# --- the sender check, end to end (criterion 2) -------------------------------


@pytest.mark.parametrize(
    ("sender", "results"),
    [
        ("Mallory <mallory@example.org>", GOOD_RESULTS),
        (f"Jordan Example <{SENDER}>", FAILING_RESULTS),
        (f"Jordan Example <{SENDER}>", None),
        (f"Jordan Example <{SENDER}>", [FAILING_RESULTS, FORGED_RESULTS]),
    ],
    ids=[
        "sender-not-listed",
        "dmarc-fail",
        "no-results-header",
        "forged-results-header",
    ],
)
def test_an_unwelcome_message_is_ignored_unread_and_writes_nothing(
    db_url, conn, gmail, web, monkeypatch, sender, results
):
    url = ats_posting(web)
    before = count(conn, "postings")
    gmail.add("m1", 1000, f"{url}\n\n{DESCRIPTION}\n", sender=sender, results=results)
    assert run_inbox(monkeypatch) == 0
    assert gmail.outcome_labels("m1") == ["jsa/ignored"]
    assert not gmail.in_inbox("m1")
    assert gmail.raw_fetches("m1") == []
    assert all(
        call["params"]["format"] == "metadata" for call in gmail.calls_for("m1", "get")
    )
    assert count(conn, "postings") == before
    assert posting(conn, url) is None


def test_an_allowed_authenticated_sender_is_processed(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{url}\n", results=[GOOD_RESULTS, FORGED_RESULTS])
    run_inbox(monkeypatch)
    assert posting(conn, url)["decision"] == "Apply"


# --- the description (criteria 3, 4, 6) ---------------------------------------


def test_the_url_is_taken_from_the_html_part_when_there_is_no_plain_part(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add(
        "m1", 1000, html=f"<html><body><p>Apply here:</p><p>{url}</p></body></html>"
    )
    run_inbox(monkeypatch)
    assert posting(conn, url)["decision"] == "Apply"


def test_the_first_url_of_several_is_the_posting(db_url, conn, gmail, web, monkeypatch):
    first, second = ats_posting(web), ats_posting(web)
    gmail.add("m1", 1000, f"Forwarded:\n{first}\nand also\n{second}\n")
    run_inbox(monkeypatch)
    assert posting(conn, first) is not None
    assert posting(conn, second) is None


def test_a_supplied_description_is_stored_when_capture_gave_none_and_the_message_is_archived(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n\n{DESCRIPTION}\n", to=DATA_ANALYSIS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["jd_markdown"] == DESCRIPTION
    # The build has no gws or Claude here, so it fails (#88 criterion 6) and the posting stays untracked.
    assert not gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == ["jsa/failed"]


def test_text_shorter_than_500_characters_is_not_a_description(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n\n{'d' * 499}\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert posting(conn, url)["jd_markdown"] is None
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]


def test_a_captured_description_wins_over_a_supplied_one(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{url}\n\n{DESCRIPTION}\n")
    run_inbox(monkeypatch)
    jd = posting(conn, url)["jd_markdown"]
    assert "Build the platform" in jd
    assert DESCRIPTION not in jd


def test_a_supplied_description_fills_in_a_stored_posting_that_has_none(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("first", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("first") == ["jsa/needs-jd"]
    assert posting(conn, url)["jd_markdown"] is None
    gmail.add("second", 2000, f"{url}\n\n{DESCRIPTION}\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert count(conn, "postings", url) == 1
    assert posting(conn, url)["jd_markdown"] == DESCRIPTION
    assert not gmail.in_inbox("second")
    assert gmail.outcome_labels("second") == ["jsa/failed"]


def test_a_stored_description_is_never_replaced_by_a_supplied_one(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("first", 1000, f"{url}\n")
    run_inbox(monkeypatch)
    stored = posting(conn, url)["jd_markdown"]
    assert stored
    # The posting's page is gone by the second email, so only the stored copy and the email remain.
    web.clear()
    gmail.add("second", 2000, f"{url}\n\n{DESCRIPTION}\n")
    run_inbox(monkeypatch)
    assert posting(conn, url)["jd_markdown"] == stored


# --- manual add, unchanged (PRD 03) --------------------------------------------


def test_a_skipped_posting_emailed_is_promoted_keeping_its_feedback_and_source(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    seed_decided(conn, url, "Skip", feedback="too junior", agent="gemini")
    gmail.add("m1", 1000, f"{url}\n")
    run_inbox(monkeypatch)
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["fit_feedback"] == "too junior"
    assert row["search_agent"] == "gemini"
    assert count(conn, "postings", url) == 1


def test_the_same_posting_emailed_twice_is_one_row(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    gmail.add("m2", 2000, f"{url}?utm_source=phone\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert count(conn, "postings", url) == 1
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]
    assert gmail.outcome_labels("m2") == ["jsa/needs-jd"]


# --- crash safety (criteria 1, 8) ----------------------------------------------


def test_a_message_moved_back_to_the_inbox_is_processed_again(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert not gmail.in_inbox("m1")
    assert len(gmail.raw_fetches("m1")) == 1
    gmail.requeue("m1")
    run_inbox(monkeypatch)
    assert len(gmail.raw_fetches("m1")) == 2
    assert not gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]
    assert count(conn, "postings", url) == 1


def test_a_message_whose_labeling_failed_is_processed_again_and_finds_the_posting(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    gmail.fail("modify", "m1")
    run_inbox(monkeypatch)
    assert gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == []
    assert count(conn, "postings", url) == 1
    gmail.recover("modify")
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]
    assert not gmail.in_inbox("m1")
    assert count(conn, "postings", url) == 1


def test_a_message_requeued_after_a_failed_build_is_reprocessed_safely(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    run_inbox(monkeypatch)
    assert not gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == ["jsa/failed"]
    gmail.requeue("m1")
    run_inbox(monkeypatch)
    assert not gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == ["jsa/failed"]
    assert count(conn, "postings", url) == 1
    assert posting(conn, url)["decision"] == "Apply"


def test_a_message_is_never_archived_without_its_label(
    db_url, conn, gmail, web, monkeypatch
):
    gmail.add("m1", 1000, bare_posting(web) + "\n")
    gmail.add("m2", 2000, "no link\n")
    gmail.add("m3", 3000, "x\n", sender="Mallory <mallory@example.org>")
    run_inbox(monkeypatch)
    outcome_ids = {i for n, i in gmail.label_ids().items() if n.startswith("jsa/")}
    for message_id in ("m1", "m2", "m3"):
        modifies = gmail.calls_for(message_id, "modify")
        assert modifies
        for call in modifies:
            if "INBOX" in call["body"].get("removeLabelIds", []):
                assert set(call["body"].get("addLabelIds", [])) & outcome_ids


def test_outcome_labels_that_already_exist_in_the_mailbox_are_reused(
    db_url, conn, gmail, web, monkeypatch
):
    gmail.create_label("jsa/refused")
    gmail.add("m1", 1000, "no link\n")
    gmail.add("m2", 2000, "no link either\n")
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("m1") == ["jsa/refused"]
    assert gmail.outcome_labels("m2") == ["jsa/refused"]


# --- one failure never stops the rest (criterion 9) ----------------------------


def test_a_failing_message_is_labeled_failed_and_the_rest_are_processed(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{ats_posting(web)}\n")
    gmail.add("m2", 2000, "no link\n")
    gmail.add("m3", 3000, f"{url}\n")
    gmail.fail("raw", "m1")
    gmail.fail("raw", "m2")
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("m1") == ["jsa/failed"]
    assert gmail.outcome_labels("m2") == ["jsa/failed"]
    assert not gmail.in_inbox("m1")
    assert not gmail.in_inbox("m2")
    assert posting(conn, url)["decision"] == "Apply"


# --- credentials and the logs (criterion 10; XC-1) ----------------------------


def test_every_gmail_call_uses_the_jobs_mailbox_credential_and_never_the_owners(
    db_url, conn, gmail, web, monkeypatch
):
    gmail.add("m1", 1000, bare_posting(web) + "\n")
    gmail.add("m2", 2000, "x\n", sender="Mallory <mallory@example.org>")
    run_inbox(monkeypatch)
    calls = gmail.calls()
    assert len(calls) > 4
    assert all(call["tokens"][0] == "gmail" for call in calls)
    assert all(call["credentials"] == ["mailbox"] for call in calls)


def test_no_log_line_carries_a_message_body_or_a_credential(
    db_url, conn, gmail, web, monkeypatch, caplog, capsys
):
    bare = bare_posting(web)
    gmail.add("ok", 1000, f"{bare}\n\n{BODY_MARKER} {DESCRIPTION}\n")
    gmail.add("nolink", 2000, f"{BODY_MARKER} just words\n")
    gmail.add(
        "nobody", 3000, f"{BODY_MARKER}\n", sender="Mallory <mallory@example.org>"
    )
    gmail.add("broken", 4000, f"{BODY_MARKER}\n")
    gmail.fail("raw", "broken")
    run_inbox(monkeypatch)
    output = capsys.readouterr()
    text = f"{caplog.text}\n{output.out}\n{output.err}"
    assert text.strip()  # the run does log something
    for secret in (BODY_MARKER, DESCRIPTION[:60], MAILBOX_CREDENTIAL, OWNER_CREDENTIAL):
        assert secret not in text


# --- configuration -------------------------------------------------------------


def test_inbox_without_the_inbox_table_fails_naming_it_and_calls_nothing(
    db_url, conn, gmail, web, profile, monkeypatch, capsys
):
    config = profile / "config.toml"
    config.write_text(config.read_text().split("\n[inbox]")[0] + "\n")
    gmail.add("m1", 1000, "no link\n")
    assert run_inbox(monkeypatch) != 0
    output = capsys.readouterr()
    assert "inbox" in (output.out + output.err).lower()
    assert gmail.calls() == []
    assert gmail.in_inbox("m1")


def test_inbox_without_the_mailbox_credential_fails_naming_it_and_calls_nothing(
    db_url, conn, gmail, web, monkeypatch, capsys
):
    monkeypatch.delenv("JSA_INBOX_GWS_CREDENTIALS")
    gmail.add("m1", 1000, "no link\n")
    assert run_inbox(monkeypatch) != 0
    output = capsys.readouterr()
    assert "JSA_INBOX_GWS_CREDENTIALS" in output.out + output.err
    assert gmail.calls() == []
    assert gmail.in_inbox("m1")


def test_an_unreadable_mailbox_leaves_every_message_unlabeled(
    db_url, conn, gmail, web, monkeypatch, tmp_path
):
    gmail.add("m1", 1000, "no link\n")
    broken = tmp_path / "broken-gws"
    broken.write_text("#!/bin/sh\necho 'invalid_grant' >&2\nexit 1\n")
    broken.chmod(0o755)
    monkeypatch.setenv("JSA_GWS_BIN", str(broken))
    run_inbox(monkeypatch)
    assert gmail.in_inbox("m1")
    assert gmail.outcome_labels("m1") == []


def test_the_example_profile_shows_a_commented_inbox_table_with_a_fictional_sender():
    config = (REPO_ROOT / "profile.example" / "config.toml").read_text()
    assert "# [inbox]" in config
    assert "senders" in config
    assert "@example.com" in config


def test_the_env_example_lists_the_mailbox_credential():
    assert "JSA_INBOX_GWS_CREDENTIALS" in (REPO_ROOT / ".env.example").read_text()


# --- the refinement setting (issue #180; PRD 03 "Email side door") ---------------------------


def setting(conn, url):
    return conn.execute(
        "SELECT in_refinement FROM postings WHERE canonical_url = ?",
        (canonicalize_url(url),),
    ).fetchone()[0]


def test_an_emailed_new_posting_is_stored_in_refinement(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert setting(conn, url) == 1


def test_an_emailed_description_less_posting_is_stored_in_refinement(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert setting(conn, url) == 1


@pytest.mark.parametrize("decision", ["Apply", "Skip"])
def test_an_emailed_url_matching_an_excluded_row_leaves_its_setting_alone(
    db_url, conn, gmail, web, monkeypatch, decision
):
    url = ats_posting(web)
    seed_decided(conn, url, decision)
    conn.execute(
        "UPDATE postings SET in_refinement = 0 WHERE canonical_url = ?",
        (canonicalize_url(url),),
    )
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert setting(conn, url) == 0
    assert posting(conn, url)["decision"] == "Apply"


def test_an_emailed_url_matching_an_included_row_leaves_its_setting_alone(
    db_url, conn, gmail, web, monkeypatch
):
    url = ats_posting(web)
    seed_decided(conn, url, "Apply")
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert setting(conn, url) == 1
    assert posting(conn, url)["decided_at"] == LONG_AGO


# --- the address names the role family (issue #196; PRD 03 "The address names the role family") ---

ANALYTICS_ADDRESS = "jobs.mailbox+analytics-engineering@example.net"
ANALYTICS, DATA_ANALYSIS = ROLE_FAMILIES


@pytest.mark.parametrize(
    ("addresses", "expected"),
    [
        (["jobs+data-analysis@example.net"], DATA_ANALYSIS),
        (["jobs+DATA-Analysis@example.net"], DATA_ANALYSIS),
        (["jobs@example.net"], None),
        (["jobs+nonsense@example.net"], None),
        (["jobs+data@example.net"], None),
        (["data-analysis@example.net"], None),
        ([], None),
        (
            [
                "jobs+analytics-engineering@example.net",
                "jobs+data-analysis@example.net",
            ],
            ANALYTICS,
        ),
        (
            ["jobs+nonsense@example.net", "jobs+data-analysis@example.net"],
            DATA_ANALYSIS,
        ),
        (["jobs@example.net", "jobs+data-analysis@example.net"], DATA_ANALYSIS),
    ],
    ids=[
        "tag",
        "ignoring-case",
        "plain-address",
        "unknown-tag",
        "prefix-of-a-tag",
        "tag-without-plus",
        "no-addresses",
        "first-match-wins",
        "unmatched-address-skipped",
        "plain-address-skipped",
    ],
)
def test_the_addressed_role_family_is_the_first_address_with_a_matching_tag(
    addresses, expected
):
    assert addressed_role_family(addresses, ROLE_FAMILIES) == expected


def test_the_tag_rule_is_the_one_prd_03_defines():
    names = ["Customer Enablement", "Data Science"]
    assert (
        addressed_role_family(["jobs+customer-enablement@example.net"], names)
        == "Customer Enablement"
    )
    assert addressed_role_family(["jobs+data_science@example.net"], names) is None


def keep_only(profile, name):
    for family in ROLE_FAMILIES:
        if family != name:
            shutil.rmtree(profile / "role_families" / family)


def role_family_of(conn, url):
    return stored_role_family(conn, url)


@pytest.mark.parametrize(
    "text", [None, DESCRIPTION], ids=["no-description", "description"]
)
@pytest.mark.parametrize(
    "address",
    [PLAIN_ADDRESS, "jobs.mailbox+no-such-family@example.net"],
    ids=["plain-address", "unmatched-tag"],
)
def test_without_a_matching_tag_and_with_two_role_families_the_posting_is_added_with_none_and_labeled_needs_family(
    db_url, conn, gmail, web, agent, monkeypatch, address, text
):
    url = bare_posting(web)
    body = f"{url}\n" if text is None else f"{url}\n\n{text}\n"
    gmail.add("m1", 1000, body, to=address)
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["search_agent"] == "manual"
    assert role_family_of(conn, url) is None
    assert row["jd_markdown"] == text
    assert gmail.outcome_labels("m1") == ["jsa/needs-family"]
    assert "jsa/needs-family" in gmail.label_ids()
    assert not gmail.in_inbox("m1")
    assert agent.calls == []


@pytest.mark.parametrize(
    ("to", "cc", "expected"),
    [
        (DATA_ANALYSIS_ADDRESS, None, DATA_ANALYSIS),
        ("jobs.mailbox+Data-Analysis@example.net", None, DATA_ANALYSIS),
        (PLAIN_ADDRESS, DATA_ANALYSIS_ADDRESS, DATA_ANALYSIS),
        (f"other@example.org, {ANALYTICS_ADDRESS}", None, ANALYTICS),
        (ANALYTICS_ADDRESS, DATA_ANALYSIS_ADDRESS, ANALYTICS),
        (PLAIN_ADDRESS, f"x@example.org, {DATA_ANALYSIS_ADDRESS}", DATA_ANALYSIS),
    ],
    ids=["to", "to-ignoring-case", "cc", "second-to", "to-before-cc", "second-cc"],
)
def test_a_matching_tag_in_to_or_cc_sets_the_role_family_of_a_new_row(
    db_url, conn, gmail, web, monkeypatch, to, cc, expected
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=to, cc=cc)
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == expected
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]


def test_a_tagged_message_with_no_description_is_needs_jd_not_needs_family(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]
    assert posting(conn, url)["jd_markdown"] is None


@pytest.mark.parametrize("name", ROLE_FAMILIES)
@pytest.mark.parametrize(
    "address",
    [PLAIN_ADDRESS, "jobs.mailbox+no-such-family@example.net"],
    ids=["plain-address", "unmatched-tag"],
)
def test_with_exactly_one_role_family_a_message_without_a_matching_tag_uses_it(
    db_url, conn, gmail, web, profile, monkeypatch, name, address
):
    keep_only(profile, name)
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=address)
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == name
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]


def test_a_matching_tag_is_used_even_when_it_is_not_the_only_role_family(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n", to=ANALYTICS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == ANALYTICS


@pytest.mark.parametrize(
    "address",
    [DATA_ANALYSIS_ADDRESS, ANALYTICS_ADDRESS, PLAIN_ADDRESS],
    ids=["other-tag", "same-tag", "plain-address"],
)
def test_email_never_changes_a_role_family_an_existing_row_has(
    db_url, conn, gmail, web, monkeypatch, address
):
    url = bare_posting(web)
    seed_decided(conn, url, "Apply", role_family=ANALYTICS)
    gmail.add("m1", 1000, f"{url}\n", to=address)
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == ANALYTICS
    assert gmail.outcome_labels("m1") != ["jsa/needs-family"]


def test_an_existing_apply_row_with_no_role_family_takes_the_tags(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    seed_decided(conn, url, "Apply")
    gmail.add("m1", 1000, f"{url}\n", to=DATA_ANALYSIS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == DATA_ANALYSIS
    assert count(conn, "postings", url) == 1
    assert gmail.outcome_labels("m1") == ["jsa/needs-jd"]


def test_an_existing_apply_row_with_no_role_family_stays_without_one_on_the_plain_address(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    seed_decided(conn, url, "Apply")
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) is None
    assert gmail.outcome_labels("m1") == ["jsa/needs-family"]


def test_a_skipped_posting_emailed_to_a_tag_is_promoted_with_the_tags_role_family(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    seed_decided(conn, url, "Skip", feedback="too junior")
    gmail.add("m1", 1000, f"{url}\n", to=ANALYTICS_ADDRESS)
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert row["fit_feedback"] == "too junior"
    assert role_family_of(conn, url) == ANALYTICS


def test_a_skipped_posting_emailed_to_the_plain_address_is_promoted_with_no_role_family(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    seed_decided(conn, url, "Skip")
    gmail.add("m1", 1000, f"{url}\n")
    assert run_inbox(monkeypatch) == 0
    row = posting(conn, url)
    assert row["decision"] == "Apply"
    assert role_family_of(conn, url) is None
    assert gmail.outcome_labels("m1") == ["jsa/needs-family"]


def test_a_needs_family_message_requeued_after_the_row_gets_a_role_family_keeps_it(
    db_url, conn, gmail, web, monkeypatch
):
    url = bare_posting(web)
    gmail.add("m1", 1000, f"{url}\n")
    run_inbox(monkeypatch)
    assert gmail.outcome_labels("m1") == ["jsa/needs-family"]
    conn.execute(
        "UPDATE postings SET role_family = ? WHERE canonical_url = ?",
        (ANALYTICS, canonicalize_url(url)),
    )
    gmail.requeue("m1")
    assert run_inbox(monkeypatch) == 0
    assert role_family_of(conn, url) == ANALYTICS
    assert count(conn, "postings", url) == 1
