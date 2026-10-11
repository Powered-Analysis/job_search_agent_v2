"""The ATS redline (PRD 04): wording edits traced to the posting, validated in code, written as tracked changes."""

import json
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Collection, Sequence
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
from types import UnionType

from docx.document import Document

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
    skills_section,
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
_SKILLS_FIELDS = {
    "paragraph": int,
    "find": str,
    "replace": str,
    "goal": str,
    "jd_quote": str | None,
    "reason": str,
}
GOALS = ("ats", "narrative")
_LIST_MARKER = re.compile(r"(?:[-*+]|\d+[.)])(?:\s+|$)")
NO_SKILLS_LIST = (
    "There is no skills list, so the skills section is edited under the ordinary rules like "
    "any other paragraph, and `skills_edits` must be []."
)
NO_SKILLS_SECTION = (
    "No paragraph is open to skills edits, so `skills_edits` must be []. Every paragraph "
    "is edited under the ordinary rules."
)


@dataclass(frozen=True)
class Edit:
    paragraph: int
    find: str
    replace: str
    jd_quote: str
    why_same_meaning: str


@dataclass(frozen=True)
class SkillsEdit:
    paragraph: int
    find: str
    replace: str
    goal: str
    jd_quote: str | None
    reason: str


@dataclass(frozen=True)
class Proposal:
    """What the redline agent returned: `explanation` is set exactly when both edit lists are empty."""

    edits: list[Edit]
    skills_edits: list[SkillsEdit]
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
    has_skills_section: bool


def parse_skills(text: str) -> list[str]:
    """PRD 04 "Skills section": items split on line breaks and commas, trimmed, list markers dropped, case-only repeats kept once."""
    items: dict[str, str] = {}
    for item in re.split(r"[\r\n,]", text):
        item = _LIST_MARKER.sub("", item.strip(), count=1).strip()
        if item:
            items.setdefault(item.casefold(), item)
    return list(items.values())


def _words(text: str) -> list[str]:
    """PRD 04: whitespace-separated tokens, edge punctuation stripped, compared without case.

    A token with no letter or digit, such as a `|` between skills, is not a word.
    """
    stripped = (token.strip(_PUNCTUATION).lower() for token in text.split())
    return [word for word in stripped if any(c.isalnum() for c in word)]


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


def _inserted_words(find: str, replace: str) -> set[str]:
    return {
        word for change in plan_changes(find, replace) for word in _words(change.text)
    }


def _quote_problem(jd_quote: str, job_description: str) -> str | None:
    if _normalize_whitespace(jd_quote) not in _normalize_whitespace(job_description):
        return "jd_quote is not in the job description"
    return None


def _locate(
    paragraphs: list[str], paragraph: int, find: str, taken: list[tuple[int, int]]
) -> tuple[str | None, tuple[int, int] | None]:
    """Why `find` can't be placed once in the paragraph, else None; with the span it would occupy."""
    if not _words(find):
        return "find has no words", None
    found = _occurrences(paragraphs[paragraph], find)
    if len(found) != 1:
        return f"find occurs {len(found)} times in the paragraph, not once", None
    span = (found[0], found[0] + len(find))
    if any(start < span[1] and span[0] < end for start, end in taken):
        return "find overlaps an earlier edit", None
    return None, span


def _problem(
    edit: Edit,
    paragraphs: list[str],
    job_description: str,
    taken: list[tuple[int, int]],
    skills_section: Collection[int],
) -> tuple[str | None, tuple[int, int] | None]:
    """The first rule the edit breaks, else None; with the span of `find` it would occupy."""
    if not 0 <= edit.paragraph < len(paragraphs):
        return f"paragraph {edit.paragraph} is not a body paragraph", None
    if edit.paragraph in skills_section:
        return (
            f"paragraph {edit.paragraph} is in the skills section, which takes skills edits",
            None,
        )
    problem, span = _locate(paragraphs, edit.paragraph, edit.find, taken)
    if problem:
        return problem, None
    if not edit.jd_quote.strip():
        return "jd_quote is empty", span
    if problem := _quote_problem(edit.jd_quote, job_description):
        return problem, span
    if not edit.replace.strip():
        return "replace is empty, which deletes content", span
    if edit.find.split() == edit.replace.split():
        return "replace does not differ from find", span
    allowed = set(_words(edit.jd_quote)) | set(_words(edit.find))
    if inserted := _inserted_words(edit.find, edit.replace) - allowed:
        return (
            "inserts words found in neither jd_quote nor find: "
            + ", ".join(sorted(inserted)),
            span,
        )
    if _digit_words(edit.find) != _digit_words(edit.replace):
        return "find and replace do not have the same numbers", span
    if len(edit.find.split()) > MAX_FIND_WORDS:
        return f"find is longer than {MAX_FIND_WORDS} words", span
    if not edit.why_same_meaning.strip():
        return "why_same_meaning is empty", span
    return None, span


def _skills_problem(
    edit: SkillsEdit,
    paragraphs: list[str],
    job_description: str,
    skills: Sequence[str],
    skills_section: Collection[int],
    taken: list[tuple[int, int]],
) -> tuple[str | None, tuple[int, int] | None]:
    """The first rule the skills edit breaks, else None; with the span of `find` it would occupy."""
    if not skills:
        return "there is no skills list", None
    if edit.paragraph not in skills_section:
        return f"paragraph {edit.paragraph} is not in the skills section", None
    problem, span = _locate(paragraphs, edit.paragraph, edit.find, taken)
    if problem:
        return problem, None
    if edit.replace == edit.find:
        return "replace does not differ from find", span
    if edit.goal not in GOALS:
        return f"goal is not one of {', '.join(GOALS)}", span
    quote = (edit.jd_quote or "").strip()
    if edit.goal == "ats" and not quote:
        return "an ats edit needs a jd_quote", span
    if quote and (problem := _quote_problem(quote, job_description)):
        return problem, span
    allowed = (
        {word for skill in skills for word in _words(skill)}
        | set(_words(quote))
        | set(_words(edit.find))
    )
    if inserted := _inserted_words(edit.find, edit.replace) - allowed:
        return (
            "inserts words found in none of the skills list, jd_quote, and find: "
            + ", ".join(sorted(inserted)),
            span,
        )
    if not edit.reason.strip():
        return "reason is empty", span
    return None, span


def _validated[Item](
    edits: list[Item],
    problem_of: Callable[
        [Item, list[tuple[int, int]]], tuple[str | None, tuple[int, int] | None]
    ],
    paragraph_of: Callable[[Item], int],
) -> list[str | None]:
    """Each edit's result in order; a passing edit's span is held against the later edits of its paragraph."""
    taken: dict[int, list[tuple[int, int]]] = {}
    results = []
    for edit in edits:
        spans = taken.setdefault(paragraph_of(edit), [])
        problem, span = problem_of(edit, spans)
        if problem is None and span is not None:
            spans.append(span)
        results.append(problem)
    return results


def validate_edits(
    edits: list[Edit],
    paragraphs: list[str],
    job_description: str,
    skills_section: Collection[int] = (),
) -> list[str | None]:
    """Each edit's result in order: None when it passes, else why it is dropped (PRD 04, "Validation").

    `skills_section` holds the paragraphs that take skills edits instead; it is empty while no skills list is in use.
    """
    return _validated(
        edits,
        lambda edit, taken: _problem(
            edit, paragraphs, job_description, taken, skills_section
        ),
        lambda edit: edit.paragraph,
    )


def validate_skills_edits(
    edits: list[SkillsEdit],
    paragraphs: list[str],
    job_description: str,
    skills: Sequence[str],
    skills_section: Collection[int],
) -> list[str | None]:
    """Each skills edit's result in order: None when it passes, else why it is dropped (PRD 04, "Skills section")."""
    return _validated(
        edits,
        lambda edit, taken: _skills_problem(
            edit, paragraphs, job_description, skills, skills_section, taken
        ),
        lambda edit: edit.paragraph,
    )


def _skills_comment(edit: SkillsEdit) -> str:
    quote = (edit.jd_quote or "").strip()
    lines = [f"Goal: {edit.goal}"]
    if quote:
        lines.append(f'Posting: "{edit.jd_quote}"')
    lines.append(f"Reason: {edit.reason}")
    return "\n".join(lines)


def _tracked_edit(
    paragraphs: list[str], paragraph: int, find: str, replace: str, comment: str
) -> TrackedEdit:
    offset = paragraphs[paragraph].index(find)
    return TrackedEdit(
        [
            TrackedChange(paragraph, offset + c.start, offset + c.end, c.text)
            for c in plan_changes(find, replace)
        ],
        comment,
    )


def _object_items[Item](
    raw: dict, key: str, fields: dict[str, type | UnionType], build: type[Item]
) -> list[Item]:
    items = []
    for position, item in enumerate(raw[key]):
        if not isinstance(item, dict) or any(
            not isinstance(item.get(name), kind) or isinstance(item.get(name), bool)
            for name, kind in fields.items()
        ):
            raise JsaError(
                f"{key} item {position} of the redline agent's output is not an object with "
                + ", ".join(fields)
            )
        items.append(build(**{name: item[name] for name in fields}))
    return items


def parse_proposal(text: str) -> Proposal:
    """The agent's final text as a proposal; anything but a JSON `{edits, skills_edits, explanation}` object raises."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsaError(f"the redline agent's output is not JSON: {error}") from None
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("edits"), list)
        or not isinstance(raw.get("skills_edits"), list)
        or "explanation" not in raw
        or not isinstance(raw["explanation"], str | None)
    ):
        raise JsaError(
            "the redline agent's output is not a JSON object with an edits array, "
            "a skills_edits array, and an explanation string or null"
        )
    edits = _object_items(raw, "edits", _FIELDS, Edit)
    skills_edits = _object_items(raw, "skills_edits", _SKILLS_FIELDS, SkillsEdit)
    explanation = raw["explanation"]
    # `null`, not an empty string, is how a reply with edits carries no explanation.
    valid = (
        explanation is None
        if edits or skills_edits
        else bool(explanation and explanation.strip())
    )
    if not valid:
        raise JsaError(
            "the redline agent's explanation must be non-empty exactly when it proposes no edits"
        )
    return Proposal(edits, skills_edits, explanation)


def assemble_redline_prompt(
    job_description: str | None,
    paragraphs: list[str],
    skills: Sequence[str] = (),
    skills_section: Collection[int] = (),
) -> str:
    """The posting, the indexed resume paragraphs, and the skills list are the only inputs (PRD 04, XC-13)."""
    numbered = "\n".join(
        f"{index}: {text}" for index, text in enumerate(paragraphs) if text.strip()
    )
    slots = {
        "JOB_DESCRIPTION": Slot(job_description, "the posting's job description"),
        "RESUME_PARAGRAPHS": Slot(numbered, "the packet's resume copy"),
        "SKILLS": Slot(
            "\n".join(skills) or NO_SKILLS_LIST,
            "profile/resume_skills.md",
            required=False,
        ),
        "SKILLS_SECTION": Slot(
            f"Paragraph{'s' if len(skills_section) > 1 else ''} "
            + ", ".join(map(str, sorted(skills_section)))
            if skills_section
            else NO_SKILLS_SECTION,
            "the resume's skills section",
            required=False,
        ),
    }
    return assemble(app_template("redline.md"), slots)


def run_redline(prompt: str, settings: AgentSettings) -> Proposal:
    return parse_proposal(agent_loop.run_single_turn(prompt, settings, agent="redline"))


def _replace_copy(document: Document, copy: Path) -> None:
    """Save `document` over `copy` whole or not at all, so a failed write leaves the old copy intact."""
    with tempfile.NamedTemporaryFile(
        dir=copy.parent, prefix=".redline-", suffix=".docx", delete=False
    ) as handle:
        staged = Path(handle.name)
    try:
        document.save(str(staged))
        shutil.copymode(copy, staged)
        staged.replace(copy)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def edits_applied(edits_file: Path) -> bool:
    """Whether the recorded redline wrote any edit, of either kind, into the resume copy."""
    try:
        record = json.loads(edits_file.read_text(encoding="utf-8"))
        return any(
            edit["validation"] is None
            for edit in record["edits"] + record.get("skills_edits", [])
        )
    except (ValueError, KeyError, TypeError) as error:
        raise JsaError(f"{edits_file} is not a redline record: {error}") from error


def _record(edits: Sequence, results: list[str | None]) -> list[dict]:
    return [
        {**asdict(edit), "validation": problem}
        for edit, problem in zip(edits, results, strict=True)
    ]


def redline_resume(
    copy: Path,
    edits_file: Path,
    job_description: str,
    settings: AgentSettings,
    skills: Sequence[str] = (),
) -> RedlineResult | None:
    """Propose, validate, and write the redline of a resume copy; None when it has unresolved changes.

    `skills` is the parsed skills list. The redlined document replaces `copy`. `edits_file` is
    written last, after the replacement, so it marks the step done (PRD 04, "Re-entry").
    """
    document = load_resume(copy)
    if has_unresolved_changes(document):
        return None
    paragraphs = body_paragraph_texts(document)
    located = skills_section(document)
    # The skills section gets its latitude only while the skills list is in use.
    section = frozenset(located or ()) if skills else frozenset()
    proposal = run_redline(
        assemble_redline_prompt(job_description, paragraphs, skills, section),
        settings,
    )
    edit_results = validate_edits(proposal.edits, paragraphs, job_description, section)
    skills_results = validate_skills_edits(
        proposal.skills_edits, paragraphs, job_description, skills, section
    )
    tracked = [
        _tracked_edit(
            paragraphs,
            edit.paragraph,
            edit.find,
            edit.replace,
            f'Posting: "{edit.jd_quote}"\nSame meaning: {edit.why_same_meaning}',
        )
        for edit, problem in zip(proposal.edits, edit_results, strict=True)
        if problem is None
    ] + [
        _tracked_edit(
            paragraphs,
            edit.paragraph,
            edit.find,
            edit.replace,
            _skills_comment(edit),
        )
        for edit, problem in zip(proposal.skills_edits, skills_results, strict=True)
        if problem is None
    ]
    if tracked:
        apply_edits(document, tracked)
        _replace_copy(document, copy)
    record = {
        "explanation": proposal.explanation,
        "edits": _record(proposal.edits, edit_results),
        "skills_edits": _record(proposal.skills_edits, skills_results),
    }
    edits_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    results = edit_results + skills_results
    return RedlineResult(
        results.count(None), len(results) - results.count(None), located is not None
    )
