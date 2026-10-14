"""The redline (PRD 04): edits and flags that tailor a resume to its posting, validated in code, written as tracked changes and comments."""

import json
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Collection, Sequence
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
    ParagraphComment,
    TrackedChange,
    TrackedEdit,
    apply_edits,
    body_paragraph_texts,
    bold_paragraphs,
    has_unresolved_changes,
    skills_section,
)
from jsa.resume import load_resume

_PUNCTUATION = ".,;:!?()[]\"'"
_EDIT_FIELDS = {
    "paragraph": int,
    "find": str,
    "replace": str,
    "goal": str,
    "jd_quote": str | None,
    "reason": str,
}
_FLAG_FIELDS = {
    "paragraph": int,
    "jd_quote": str,
    "suggestion": str,
    "reason": str,
}
_PROPOSAL_KEYS = {"edits", "flags", "explanation"}
GOALS = ("ats", "narrative", "correction")
MAX_FLAGS = 3
MAX_CORRECTION_DISTANCE = 2
_LIST_MARKER = re.compile(r"(?:[-*+]|\d+[.)])(?:\s+|$)")
NO_SKILLS_LIST = (
    "There is no skills list, so the skills section is edited under the ordinary rules like "
    "any other paragraph."
)
NO_SKILLS_SECTION = "No paragraph is in the skills section. Every paragraph is edited under the ordinary rules."


@dataclass(frozen=True)
class Edit:
    paragraph: int
    find: str
    replace: str
    goal: str
    jd_quote: str | None
    reason: str


@dataclass(frozen=True)
class Flag:
    paragraph: int
    jd_quote: str
    suggestion: str
    reason: str


@dataclass(frozen=True)
class Proposal:
    """What the redline agent returned: `explanation` is set exactly when both lists are empty."""

    edits: list[Edit]
    flags: list[Flag]
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
    if not jd_quote.strip():
        return "jd_quote is empty"
    if _normalize_whitespace(jd_quote) not in _normalize_whitespace(job_description):
        return "jd_quote is not in the job description"
    return None


def _edit_distance(a: str, b: str) -> int:
    """Single-character insertions, deletions, and substitutions that turn `a` into `b`."""
    row = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        diagonal, row[0] = row[0], i
        for j, char_b in enumerate(b, 1):
            diagonal, row[j] = (
                row[j],
                min(row[j] + 1, row[j - 1] + 1, diagonal + (char_a != char_b)),
            )
    return row[-1]


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


def _correction_problem(edit: Edit) -> str | None:
    if edit.jd_quote is not None:
        return "a correction takes no jd_quote"
    old, new = _words(edit.find), _words(edit.replace)
    if len(old) != len(new):
        return "find and replace do not have the same number of words"
    if any(
        _edit_distance(before, after) > MAX_CORRECTION_DISTANCE
        for before, after in zip(old, new, strict=True)
    ):
        return f"a word changes by more than {MAX_CORRECTION_DISTANCE} characters"
    return None


def _traced_problem(
    edit: Edit, job_description: str, skills: Sequence[str]
) -> str | None:
    """Why an `ats` or `narrative` edit isn't traced to the posting; `skills` is empty outside the skills section."""
    quote = edit.jd_quote or ""
    if problem := _quote_problem(quote, job_description):
        return problem
    allowed = (
        set(_words(quote))
        | set(_words(edit.find))
        | {word for skill in skills for word in _words(skill)}
    )
    if inserted := _inserted_words(edit.find, edit.replace) - allowed:
        return (
            "inserts words found in none of the jd_quote, find, and skills list: "
            + ", ".join(sorted(inserted))
        )
    return None


def _edit_problem(
    edit: Edit,
    paragraphs: list[str],
    job_description: str,
    skills: Sequence[str],
    skills_section: Collection[int],
    taken: list[tuple[int, int]],
) -> tuple[str | None, tuple[int, int] | None]:
    """The first rule the edit breaks, else None; with the span of `find` it would occupy."""
    problem, span = _locate(paragraphs, edit.paragraph, edit.find, taken)
    if problem:
        return problem, None
    in_section = edit.paragraph in skills_section
    if edit.goal not in GOALS:
        return f"goal is not one of {', '.join(GOALS)}", span
    if not edit.reason.strip():
        return "reason is empty", span
    if edit.find.split() == edit.replace.split():
        return "replace does not differ from find", span
    if not edit.replace.strip() and not in_section:
        return "replace is empty, which deletes content", span
    if edit.goal == "correction":
        problem = _correction_problem(edit)
    else:
        problem = _traced_problem(edit, job_description, skills if in_section else ())
    if problem:
        return problem, span
    if not in_section and _digit_words(edit.find) != _digit_words(edit.replace):
        return "find and replace do not have the same numbers", span
    return None, span


def validate_edits(
    edits: list[Edit],
    paragraphs: list[str],
    job_description: str,
    skills: Sequence[str] = (),
    skills_section: Collection[int] = (),
) -> list[str | None]:
    """Each edit's result in order: None when it passes, else why it is dropped (PRD 04, "Validation").

    `skills_section` holds the paragraphs that get the skills section's latitude; it is empty while
    no skills list is in use. A passing edit's span is held against the later edits of its paragraph.
    """
    taken: dict[int, list[tuple[int, int]]] = {}
    results = []
    for edit in edits:
        if not 0 <= edit.paragraph < len(paragraphs):
            results.append(f"paragraph {edit.paragraph} is not a body paragraph")
            continue
        spans = taken.setdefault(edit.paragraph, [])
        problem, span = _edit_problem(
            edit, paragraphs, job_description, skills, skills_section, spans
        )
        if problem is None and span is not None:
            spans.append(span)
        results.append(problem)
    return results


def _flag_problem(
    flag: Flag, paragraphs: list[str], job_description: str
) -> str | None:
    if not 0 <= flag.paragraph < len(paragraphs):
        return f"paragraph {flag.paragraph} is not a body paragraph"
    if problem := _quote_problem(flag.jd_quote, job_description):
        return problem
    if not flag.suggestion.strip():
        return "suggestion is empty"
    if not flag.reason.strip():
        return "reason is empty"
    return None


def validate_flags(
    flags: list[Flag], paragraphs: list[str], job_description: str
) -> list[str | None]:
    """Each flag's result in order: None when it passes, else why it is dropped (PRD 04, "Validation")."""
    results = []
    passed = 0
    for flag in flags:
        problem = _flag_problem(flag, paragraphs, job_description)
        if problem is None:
            if passed == MAX_FLAGS:
                problem = f"only the first {MAX_FLAGS} flags that pass are kept"
            else:
                passed += 1
        results.append(problem)
    return results


def _edit_comment(edit: Edit) -> str:
    lines = [f"Goal: {edit.goal}"]
    if edit.jd_quote:
        lines.append(f'Posting: "{edit.jd_quote}"')
    lines.append(f"Reason: {edit.reason}")
    return "\n".join(lines)


def _flag_comment(flag: Flag) -> str:
    return "\n".join(
        [
            "For you to decide; this is not a change.",
            f"Suggestion: {flag.suggestion}",
            f'Posting: "{flag.jd_quote}"',
            f"Reason: {flag.reason}",
        ]
    )


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
            name not in item
            or not isinstance(item[name], kind)
            or isinstance(item[name], bool)
            for name, kind in fields.items()
        ):
            raise JsaError(
                f"{key} item {position} of the redline agent's output is not an object with "
                + ", ".join(fields)
            )
        items.append(build(**{name: item[name] for name in fields}))
    return items


def parse_proposal(text: str) -> Proposal:
    """The agent's final text as a proposal; anything but a JSON `{edits, flags, explanation}` object raises."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as error:
        raise JsaError(f"the redline agent's output is not JSON: {error}") from None
    if (
        not isinstance(raw, dict)
        or raw.keys() != _PROPOSAL_KEYS
        or not isinstance(raw["edits"], list)
        or not isinstance(raw["flags"], list)
        or not isinstance(raw["explanation"], str | None)
    ):
        raise JsaError(
            "the redline agent's output is not a JSON object with exactly an edits array, "
            "a flags array, and an explanation string or null"
        )
    edits = _object_items(raw, "edits", _EDIT_FIELDS, Edit)
    flags = _object_items(raw, "flags", _FLAG_FIELDS, Flag)
    explanation = raw["explanation"]
    # `null`, not an empty string, is how a reply with proposals carries no explanation.
    valid = (
        explanation is None
        if edits or flags
        else bool(explanation and explanation.strip())
    )
    if not valid:
        raise JsaError(
            "the redline agent's explanation must be non-empty exactly when it proposes nothing"
        )
    return Proposal(edits, flags, explanation)


def assemble_redline_prompt(
    job_description: str | None,
    paragraphs: list[str],
    skills: Sequence[str] = (),
    skills_section: Collection[int] = (),
    bold: Collection[int] = (),
) -> str:
    """The posting, the indexed resume paragraphs, and the skills list are the only inputs (PRD 04, XC-13)."""
    numbered = "\n".join(
        f"{index}{' [bold]' if index in bold else ''}: {text}"
        for index, text in enumerate(paragraphs)
        if text.strip()
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
    """Whether the recorded redline wrote any proposal, an edit or a flag, into the resume copy."""
    try:
        record = json.loads(edits_file.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.keys() != _PROPOSAL_KEYS:
            raise ValueError("expected exactly explanation, edits, and flags")
        return any(
            proposal["validation"] is None
            for proposal in record["edits"] + record["flags"]
        )
    except (ValueError, KeyError, TypeError) as error:
        raise JsaError(f"{edits_file} is not a redline record: {error}") from error


def _record(proposals: Sequence, results: list[str | None]) -> list[dict]:
    return [
        {**asdict(proposal), "validation": problem}
        for proposal, problem in zip(proposals, results, strict=True)
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
        assemble_redline_prompt(
            job_description,
            paragraphs,
            skills,
            section,
            bold_paragraphs(document),
        ),
        settings,
    )
    edit_results = validate_edits(
        proposal.edits, paragraphs, job_description, skills, section
    )
    flag_results = validate_flags(proposal.flags, paragraphs, job_description)
    tracked = [
        _tracked_edit(
            paragraphs, edit.paragraph, edit.find, edit.replace, _edit_comment(edit)
        )
        for edit, problem in zip(proposal.edits, edit_results, strict=True)
        if problem is None
    ]
    comments = [
        ParagraphComment(flag.paragraph, _flag_comment(flag))
        for flag, problem in zip(proposal.flags, flag_results, strict=True)
        if problem is None
    ]
    if tracked or comments:
        apply_edits(document, tracked, comments)
        _replace_copy(document, copy)
    record = {
        "explanation": proposal.explanation,
        "edits": _record(proposal.edits, edit_results),
        "flags": _record(proposal.flags, flag_results),
    }
    edits_file.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    results = edit_results + flag_results
    return RedlineResult(
        results.count(None), len(results) - results.count(None), located is not None
    )
