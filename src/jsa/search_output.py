"""The search-output wire contract and its tolerant parser (PRD 01, XC-9)."""

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, ValidationError, field_validator

from jsa.errors import JsaError
from jsa.profile import NonEmptyStr

log = logging.getLogger(__name__)

_OPENER = re.compile(r"[{\[]")


class SearchOutputError(JsaError):
    """The runner's final text held no `postings` array."""


class Posting(BaseModel):
    """One posting of the wire contract; the only definition of its shape."""

    company: NonEmptyStr
    title: NonEmptyStr
    url: NonEmptyStr
    date_posted: str | None = None

    @field_validator("date_posted", mode="before")
    @classmethod
    def _discard_invalid_date(cls, value: object) -> str | None:
        # A bad optional date costs the date, never the posting.
        if not isinstance(value, str):
            return None
        try:
            datetime.fromisoformat(value)
        except ValueError:
            return None
        return value


class SearchOutput(BaseModel):
    """The whole wire object, which a runner can have its API enforce."""

    postings: list[Posting]


def output_json_schema() -> dict:
    return SearchOutput.model_json_schema()


@dataclass(frozen=True)
class ParsedSearch:
    postings: list[Posting]
    malformed: int


def _json_values(text: str) -> Iterator[object]:
    """The outermost JSON values in `text`, skipping fences and prose around them."""
    decoder = json.JSONDecoder()
    position = 0
    while opener := _OPENER.search(text, position):
        try:
            value, position = decoder.raw_decode(text, opener.start())
        except json.JSONDecodeError:
            position = opener.end()
        else:
            yield value


def _postings_array(text: str) -> list:
    values = list(_json_values(text))
    for value in values:
        if isinstance(value, dict) and isinstance(value.get("postings"), list):
            return value["postings"]
    # A bare array is the postings themselves.
    for value in values:
        if isinstance(value, list):
            return value
    log.error("Unparseable search output: %s", text)
    raise SearchOutputError(
        "The search output holds no `postings` array; the raw text is in the log."
    )


def parse_search_output(text: str) -> ParsedSearch:
    postings = []
    malformed = 0
    for entry in _postings_array(text):
        try:
            postings.append(Posting.model_validate(entry))
        except ValidationError:
            log.warning("Dropped malformed posting: %r", entry)
            malformed += 1
    return ParsedSearch(postings, malformed)
