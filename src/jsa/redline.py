"""The ATS redline (PRD 04): wording edits traced to the posting, validated in code, written as tracked changes."""

import json
import re
from collections import Counter
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

from jsa import agent_loop
from jsa.assemble import Slot, app_template, assemble
from jsa.errors import JsaError
from jsa.profile import AgentSettings
from jsa.redline_docx import (
    TrackedChange,
    TrackedEdit,
    apply_edits,
    body_paragraph_texts,
    has_unresolved_changes,
)
from jsa.resume import load_resume

MAX_FIND_WORDS = 6
_PUNCTUATION = ".,;:!?()[]\"'"
_FIELDS = {
    "paragraph": int,
    "find": str,
    "replace": str,
    "jd_quote": str,
    "why_same_meaning": str,
}


@dataclass(frozen=True)
class Edit:
    paragraph: int
    find: str
    replace: str
    jd_quote: str
    why_same_meaning: str


@dataclass(frozen=True)
class Proposal:
    """What the redline agent returned: `explanation` is set exactly when `edits` is empty."""

    edits: list[Edit]
    explanation: str | None


@dataclass(frozen=True)
class Change:
    """Replace `[start, end)` of an edit's `find` with `text`; `start == end` inserts."""

    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RedlineResult:
    applied: int
    dropped: int


def _words(text: str) -> list[str]:
    """PRD 04: whitespace-separated tokens, edge punctuation stripped, compared without case."""
    return [token.strip(_PUNCTUATION).lower() for token in text.split()]


def _tokens(text: str) -> list[re.Match[str]]:
    return list(re.finditer(r"\S+", text))


def plan_changes(find: str, replace: str) -> list[Change]:
    """The word-level diff of `find` against `replace`, as changes to `find`.

    Accepting every change must give `replace` exactly; when the whitespace between kept words
    differs, so that the diff cannot, the whole span is replaced.
    """
    old, new = _tokens(find), _tokens(replace)
    matcher = SequenceMatcher(
        None, [m[0] for m in old], [m[0] for m in new], autojunk=False
    )
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if i1 < i2:
            start, end = old[i1].start(), old[i2 - 1].end()
            text = replace[new[j1].start() : new[j2 - 1].end()] if j1 < j2 else ""
        elif i1 > 0:
            # Inserted between kept words: the gap before the new words comes along with them.
            start = end = old[i1 - 1].end()
            text = replace[new[j1 - 1].end() : new[j2 - 1].end()]
        else:
            start = end = old[0].start()
            text = replace[new[j1].start() : new[j2].start()]
        changes.append(Change(start, end, text))
    result, position = [], 0
    for change in changes:
        result.append(find[position : change.start])
        result.append(change.text)
        position = change.end
    result.append(find[position:])
    if "".join(result) != replace:
        return [Change(0, len(find), replace)]
    return changes


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _occurrences(text: str, find: str) -> list[int]:
    return [m.start() for m in re.finditer(f"(?={re.escape(find)})", text)]


def _digit_words(text: str) -> Counter[str]:
    return Counter(word for word in _words(text) if any(c.isdigit() for c in word))


def _problem(
    edit: Edit,
    paragraphs: list[str],
    job_description: str,
    taken: list[tuple[int, int]],
) -> tuple[str | None, tuple[int, int] | None]:
    """The first rule the edit breaks, else None; with the span of `find` it would occupy."""
    if not 0 <= edit.paragraph < len(paragraphs):
        return f"paragraph {edit.paragraph} is not a body paragraph", None
    text = paragraphs[edit.paragraph]
    if not _words(edit.find):
        return "find has no words", None
    found = _occurrences(text, edit.find)
    if len(found) != 1:
        return f"find occurs {len(found)} times in the paragraph, not once", None
    span = (found[0], found[0] + len(edit.find))
    if any(start < span[1] and span[0] < end for start, end in taken):
        return "find overlaps an earlier edit", None
    if not edit.jd_quote.strip():
        return "jd_quote is empty", None
    if _normalize_whitespace(edit.jd_quote) not in _normalize_whitespace(
        job_description
    ):
        return "jd_quote is not in the job description", None
    if not edit.replace.strip():
        return "replace is empty, which deletes content", None
    if edit.find.split() == edit.replace.split():
        return "replace does not differ from find", None
    changes = plan_changes(edit.find, edit.replace)
    allowed = set(_words(edit.jd_quote)) | set(_words(edit.find))
    inserted = {
        word for change in changes for word in _words(change.text) if word
    } - allowed
    if inserted:
        return (
            "inserts words found in neither jd_quote nor find: "
            + ", ".join(sorted(inserted)),
            None,
        )
    if _digit_words(edit.find) != _digit_words(edit.replace):
        return "find and replace do not have the same numbers", None
    if len(edit.find.split()) > MAX_FIND_WORDS:
        return f"find is longer than {MAX_FIND_WORDS} words", None
    if not edit.why_same_meaning.strip():
        return "why_same_meaning is empty", None
    return None, span


def validate_edits(
    edits: list[Edit], paragraphs: list[str], job_description: str
) -> list[str | None]:
    """Each edit's result in order: None when it passes, else why it is dropped (PRD 04, "Validation")."""
    taken: dict[int, list[tuple[int, int]]] = {}
    results = []
    for edit in edits:
        spans = taken.setdefault(edit.paragraph, [])
        problem, span = _problem(edit, paragraphs, job_description, spans)
        if span is not None:
            spans.append(span)
        results.append(problem)
    return results


def _tracked_edit(edit: Edit, paragraphs: list[str]) -> TrackedEdit:
    offset = paragraphs[edit.paragraph].index(edit.find)
    comment = f'Posting: "{edit.jd_quote}"\nSame meaning: {edit.why_same_meaning}'
    return TrackedEdit(
        [
            TrackedChange(edit.paragraph, offset + c.start, offset + c.end, c.text)
            for c in plan_changes(edit.find, edit.replace)
        ],
        comment,
    )


def _parse_edit(position: int, item: object) -> Edit:
    if not isinstance(item, dict) or any(
        not isinstance(item.get(name), kind) or isinstance(item.get(name), bool)
        for name, kind in _FIELDS.items()
    ):
        raise JsaError(
            f"edit {position} of the redline agent's output is not an object with "
            + ", ".join(_FIELDS)
        )
    return Edit(**{name: item[name] for name in _FIELDS})


def parse_proposal(text: str) -> Proposal:
    """The agent's final text as a proposal; anything but a JSON `{edits, explanation}` object raises."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsaError(f"the redline agent's output is not JSON: {error}") from None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("edits"), list)
        or "explanation" not in raw
        or not isinstance(raw["explanation"], str | None)
    ):
        raise JsaError(
            "the redline agent's output is not a JSON object with an edits array "
            "and an explanation string or null"
        )
    edits = [_parse_edit(position, item) for position, item in enumerate(raw["edits"])]
    explanation = raw["explanation"]
    # `null`, not an empty string, is how a reply with edits carries no explanation.
    valid = explanation is None if edits else bool(explanation and explanation.strip())
    if not valid:
        raise JsaError(
            "the redline agent's explanation must be non-empty exactly when it proposes no edits"
        )
    return Proposal(edits, explanation)


def assemble_redline_prompt(job_description: str | None, paragraphs: list[str]) -> str:
    """The posting and the indexed resume paragraphs are the only inputs (PRD 04, XC-13)."""
    numbered = "\n".join(
        f"{index}: {text}" for index, text in enumerate(paragraphs) if text.strip()
    )
    slots = {
        "JOB_DESCRIPTION": Slot(job_description, "the posting's job description"),
        "RESUME_PARAGRAPHS": Slot(numbered, "the packet's resume copy"),
    }
    return assemble(app_template("redline.md"), slots)


def run_redline(prompt: str, settings: AgentSettings) -> Proposal:
    return parse_proposal(agent_loop.run_single_turn(prompt, settings, agent="redline"))


def redline_resume(
    copy: Path,
    redline: Path,
    edits_file: Path,
    job_description: str,
    settings: AgentSettings,
) -> RedlineResult | None:
    """Propose, validate, and write the redline of a resume copy; None when it has unresolved changes.

    `edits_file` is written last, so it marks the step done (PRD 04, "Re-entry").
    """
    document = load_resume(copy)
    if has_unresolved_changes(document):
        return None
    paragraphs = body_paragraph_texts(document)
    proposal = run_redline(
        assemble_redline_prompt(job_description, paragraphs), settings
    )
    edits = proposal.edits
    results = validate_edits(edits, paragraphs, job_description)
    tracked = [
        _tracked_edit(edit, paragraphs)
        for edit, problem in zip(edits, results, strict=True)
        if problem is None
    ]
    if tracked:
        apply_edits(document, tracked)
        document.save(str(redline))
    record = {
        "explanation": proposal.explanation,
        "edits": [
            {**asdict(edit), "validation": problem}
            for edit, problem in zip(edits, results, strict=True)
        ],
    }
    edits_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return RedlineResult(results.count(None), len(results) - results.count(None))
