"""The email side door (PRD 03): add each posting emailed to the jobs mailbox, then label and archive the message."""

import base64
import email
import email.policy
import email.utils
import logging
import re
import tempfile
from collections.abc import Callable, Collection, Sequence
from contextlib import closing
from pathlib import Path
from typing import NamedTuple

import httpx
from bs4 import BeautifulSoup

from jsa import db
from jsa.add import AggregatorUrlError, MissingFieldsError, add_posting
from jsa.config import inbox_gws_credentials
from jsa.drive import deliver_packet
from jsa.errors import JsaError
from jsa.generate import build_packet, track_posting
from jsa.gws import run_gws
from jsa.packet import packet_paths
from jsa.profile import (
    base_resume,
    inbox_drive_folder,
    inbox_settings,
    load_config,
    packet_agents,
    tracker_spreadsheet_id,
)
from jsa.tracker import today

log = logging.getLogger(__name__)

TRACKED = "jsa/tracked"
NEEDS_JD = "jsa/needs-jd"
NEEDS_FIELDS = "jsa/needs-fields"
REFUSED = "jsa/refused"
IGNORED = "jsa/ignored"
FAILED = "jsa/failed"

# PRD 03: shorter text is a signature, not a description.
MIN_DESCRIPTION_CHARS = 500
_URL = re.compile(r"https?://[^\s<>\"]+")
_URL_TRAILING = ".,;:!?)]}'"
_COMMENT = re.compile(r"\([^)]*\)")
_HEADER_FROM = re.compile(r"header\.from=([^\s;]+)", re.IGNORECASE)
_DMARC_PASS = re.compile(r"dmarc=pass\b", re.IGNORECASE)
# The authserv-id Gmail stamps on mail it received; a sender can write a header but not own this one.
_GMAIL_AUTHSERV_ID = "mx.google.com"

Headers = Sequence[tuple[str, str]]


class Posting(NamedTuple):
    url: str | None
    description: str | None


def sender_allowed(headers: Headers, senders: Collection[str]) -> bool:
    """From is an allowed address and Gmail's own (topmost) Authentication-Results shows DMARC passing for its domain. Pure (XC-9)."""
    froms = [value for name, value in headers if name.lower() == "from"]
    if len(froms) != 1:
        return False
    address = email.utils.parseaddr(froms[0])[1].lower()
    if address not in {sender.lower() for sender in senders} or "@" not in address:
        return False
    domain = address.rpartition("@")[2]
    results = next(
        (value for name, value in headers if name.lower() == "authentication-results"),
        None,
    )
    if results is None:
        return False
    authserv_id, *checks = _COMMENT.sub("", results).split(";")
    if authserv_id.strip().lower() != _GMAIL_AUTHSERV_ID:
        return False
    return any(
        _DMARC_PASS.match(check.strip())
        and (match := _HEADER_FROM.search(check))
        and match[1].lower() == domain
        for check in checks
    )


def parse_posting(raw: bytes) -> Posting:
    """The posting URL and supplied description in a message's text (plain part, else HTML part). Pure (XC-9)."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return Posting(None, None)
    text = part.get_content()
    if part.get_content_type() == "text/html":
        soup = BeautifulSoup(text, "html.parser")
        for tag in soup(["head", "script", "style"]):
            tag.decompose()
        text = soup.get_text("\n")
    found = _URL.search(text)
    if found is None:
        return Posting(None, None)
    url = found[0].rstrip(_URL_TRAILING)
    rest = (text[: found.start()] + text[found.start() + len(url) :]).strip()
    return Posting(url, rest if len(rest) >= MIN_DESCRIPTION_CHARS else None)


class Mailbox:
    """The jobs mailbox, read and written only through `gws` with its own credential (XC-1)."""

    def __init__(self, credential: str):
        self._credential = credential
        self._label_ids: dict[str, str] | None = None

    def _call(
        self, resource: str, method: str, params: dict, body: dict | None = None
    ) -> dict:
        response = run_gws(
            ("gmail", "users", resource, method),
            {"userId": "me", **params},
            body,
            credential=self._credential,
        )
        if not isinstance(response, dict):
            raise JsaError("gws output was not a Gmail response")
        return response

    def inbox_ids(self) -> list[str]:
        ids: list[str] = []
        params: dict = {"labelIds": ["INBOX"], "maxResults": 500}
        while True:
            response = self._call("messages", "list", params)
            ids += [message["id"] for message in response.get("messages", [])]
            if not (token := response.get("nextPageToken")):
                return ids
            params = {**params, "pageToken": token}

    def headers(self, message_id: str) -> tuple[int, Headers]:
        """The message's arrival time and its sender-check headers, in message order; the body is not fetched."""
        response = self._call(
            "messages",
            "get",
            {
                "id": message_id,
                "format": "metadata",
                "metadataHeaders": ["From", "Authentication-Results"],
            },
        )
        listed = response.get("payload", {}).get("headers", [])
        return int(response["internalDate"]), [(h["name"], h["value"]) for h in listed]

    def raw(self, message_id: str) -> bytes:
        response = self._call("messages", "get", {"id": message_id, "format": "raw"})
        return base64.urlsafe_b64decode(
            response["raw"] + "=" * (-len(response["raw"]) % 4)
        )

    def _label_id(self, name: str) -> str:
        if self._label_ids is None:
            listed = self._call("labels", "list", {}).get("labels", [])
            self._label_ids = {label["name"]: label["id"] for label in listed}
        if name not in self._label_ids:
            self._label_ids[name] = self._call("labels", "create", {}, {"name": name})[
                "id"
            ]
        return self._label_ids[name]

    def label_and_archive(self, message_id: str, label: str) -> None:
        """One call, so a message never ends up archived without its outcome label."""
        self._call(
            "messages",
            "modify",
            {"id": message_id},
            {"addLabelIds": [self._label_id(label)], "removeLabelIds": ["INBOX"]},
        )


def _deliver(posting_id: int, *, has_jd: bool) -> str:
    """Build the posting's packet, upload it, then append its tracker row; the outcome label.

    The same build as `jsa generate --id` (no liveness re-check, XC-5), in a temporary directory.
    A posting already tracked is left alone (XC-10).
    """
    with closing(db.connect()) as conn:
        if not db.tracker_queue(conn, posting_id):
            return TRACKED
        if not has_jd:
            return NEEDS_JD
        [job] = db.packet_queue(conn, posting_id)
    config = load_config()
    # Checked before the slow build: the tracker row waits on the upload.
    spreadsheet_id = tracker_spreadsheet_id()
    drive_folder_id = inbox_drive_folder(config)
    agents = packet_agents(config)
    resume = base_resume()
    with tempfile.TemporaryDirectory() as scratch:
        built = config.model_copy(update={"packets_dir": Path(scratch)})
        if not build_packet(job, built, resume, agents, rewrite=True):
            return NEEDS_JD
        deliver_packet(drive_folder_id, packet_paths(built, job)[0])
    track_posting(spreadsheet_id, today(), posting_id)
    return TRACKED


def _process(
    client: httpx.Client,
    mailbox: Mailbox,
    message_id: str,
    headers: Headers | None,
    senders: Collection[str],
) -> str:
    """The outcome label for one message."""
    if headers is None:
        raise JsaError("could not read the message's headers")
    if not sender_allowed(headers, senders):
        return IGNORED
    posting = parse_posting(mailbox.raw(message_id))
    if posting.url is None:
        return REFUSED
    try:
        outcome = add_posting(
            client, posting.url, date_posted=None, description=posting.description
        )
    except AggregatorUrlError:
        return REFUSED
    except MissingFieldsError:
        return NEEDS_FIELDS
    log.info("message %s: posting %s is Apply", message_id, outcome.posting_id)
    return _deliver(outcome.posting_id, has_jd=outcome.has_jd)


def _safe[Result](
    message_id: str, fallback: Result, step: Callable[..., Result], *args: object
) -> Result:
    """Run one step of one message; any failure is logged by type only (its text could quote the message) and yields `fallback`."""
    try:
        return step(*args)
    except Exception as error:  # noqa: BLE001  # one message's failure never stops the rest
        log.error("message %s failed: %s", message_id, type(error).__name__)
        return fallback


def inbox(client: httpx.Client) -> None:
    """Work through the jobs mailbox's Inbox, oldest first; one message's failure never stops the rest."""
    senders = inbox_settings(load_config()).senders
    mailbox = Mailbox(inbox_gws_credentials())
    # An unreadable message sorts first and is labeled failed below.
    queue = [
        (*_safe(message_id, (0, None), mailbox.headers, message_id), message_id)
        for message_id in mailbox.inbox_ids()
    ]
    for _, headers, message_id in sorted(queue, key=lambda entry: (entry[0], entry[2])):
        label = _safe(
            message_id, FAILED, _process, client, mailbox, message_id, headers, senders
        )
        _safe(message_id, None, mailbox.label_and_archive, message_id, label)
        log.info("message %s: %s", message_id, label)
