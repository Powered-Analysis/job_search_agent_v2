"""Prompt assembly (XC-13): a pure fill of an app template's `{{SLOT}}` markers."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.resources import files

from jsa.errors import JsaError

OPTIONAL_PLACEHOLDER = "(none provided)"
_MARKER = re.compile(r"\{\{[A-Z][A-Z0-9_]*\}\}")


@dataclass(frozen=True)
class Slot:
    """What fills one marker. `source` says where a missing value should have come from."""

    text: str | None
    source: str
    required: bool = True


def app_template(name: str) -> str:
    return (files("jsa") / "templates" / name).read_text(encoding="utf-8")


def assemble(template: str, slots: Mapping[str, Slot]) -> str:
    def fill(marker: re.Match[str]) -> str:
        name = marker[0][2:-2]
        if name not in slots:
            # Left in place so the check below names it.
            return marker[0]
        slot = slots[name]
        text = (slot.text or "").strip()
        if text:
            return text
        if slot.required:
            raise JsaError(f"{name} has no content: {slot.source} is missing or empty.")
        return OPTIONAL_PLACEHOLDER

    # A single pass, so text a slot brings in is never itself expanded.
    assembled = _MARKER.sub(fill, template)
    if unfilled := _MARKER.search(assembled):
        raise JsaError(f"The prompt template has no source for {unfilled[0]}.")
    return assembled
