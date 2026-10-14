"""The redline document (PRD 04 "Redline"): Word tracked changes and comments written into a resume copy.

Pure over the loaded document (XC-9). Text offsets count the characters of a paragraph's runs, which
is also the text the redline agent is shown, so an edit's position means the same thing throughout.
"""

import copy
import itertools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from docx.document import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.text.paragraph import Paragraph

AUTHOR = "Claude (Redline)"
_INITIALS = "CR"
_TEXT_TAGS = ("w:br", "w:cr", "w:noBreakHyphen", "w:ptab", "w:t", "w:tab")
TEXT_XPATH = " | ".join(_TEXT_TAGS)
_TEXT_QNAMES = {qn(tag) for tag in _TEXT_TAGS}
_UNRESOLVED_XPATH = ".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_BULLET_START = re.compile(r"\s*(?:[•●○◦▪▫■□‣⁃∙·]|[-*–—]\s)")
# The outermost run-level elements of a paragraph; a text box's runs sit inside a run.
_TOP_LEVEL_XPATH = (
    "./descendant::*[self::w:r or self::w:ins or self::w:del]"
    "[not(ancestor::w:r or ancestor::w:ins or ancestor::w:del)]"
)


@dataclass(frozen=True)
class TrackedChange:
    """Replace `[start, end)` of a body paragraph's text with `text`; `start == end` inserts."""

    paragraph: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class TrackedEdit:
    """The tracked changes of one edit, which share a single comment."""

    changes: list[TrackedChange]
    comment: str


@dataclass(frozen=True)
class ParagraphComment:
    """A comment on a whole body paragraph that changes none of its text."""

    paragraph: int
    comment: str


@dataclass(frozen=True)
class TrackedRemoval:
    """A body paragraph deleted whole, with its comment."""

    paragraph: int
    comment: str


@dataclass(frozen=True)
class TrackedReorder:
    """Body paragraphs moved into the order listed, with one comment."""

    paragraphs: list[int]
    comment: str


def runs(paragraph: BaseOxmlElement) -> list[BaseOxmlElement]:
    # Runs inside a text box belong to the box's own paragraphs, not to this one (PRD 04, "Text in scope").
    return paragraph.xpath("./descendant::w:r[not(ancestor::w:txbxContent)]")


def _run_text(run: BaseOxmlElement) -> str:
    return "".join(str(child) for child in run.xpath(TEXT_XPATH))


def paragraph_text(paragraph: BaseOxmlElement) -> str:
    return "".join(_run_text(run) for run in runs(paragraph))


def body_paragraph_texts(document: Document) -> list[str]:
    """The plain text of every body paragraph, indexed as the redline's edits address them."""
    return [paragraph_text(p._p) for p in document.paragraphs]


def _wholly_bold(paragraph: Paragraph) -> bool:
    texts = [(run.bold, run.text) for run in paragraph.runs if run.text.strip()]
    return bool(texts) and all(bold is True for bold, _ in texts)


def bold_paragraphs(document: Document) -> set[int]:
    """The indices of the wholly bold body paragraphs, which the redline's rendering tags `bold`."""
    return {
        index
        for index, paragraph in enumerate(document.paragraphs)
        if _wholly_bold(paragraph)
    }


def _in_word_list(paragraph: Paragraph) -> bool:
    """Whether the paragraph's own list setting, else its style's or its base styles', puts it in a list."""
    style = paragraph.style
    sources = [paragraph._p]
    while style is not None:
        sources.append(style.element)
        style = style.base_style
    for source in sources:
        if ids := source.xpath("./w:pPr/w:numPr/w:numId/@w:val"):
            return ids[0] != "0"
    return False


def is_bullet(paragraph: Paragraph) -> bool:
    """PRD 04 "Redline", Bullets: a paragraph in a Word list, or whose text begins with a bullet character."""
    return _in_word_list(paragraph) or bool(
        _BULLET_START.match(paragraph_text(paragraph._p))
    )


def bullet_paragraphs(document: Document) -> set[int]:
    """The indices of the bullet paragraphs, which the redline's rendering tags `bullet`."""
    return {
        index
        for index, paragraph in enumerate(document.paragraphs)
        if is_bullet(paragraph)
    }


def _ends_skills_section(paragraph: Paragraph) -> bool:
    return (paragraph.style.name or "").startswith("Heading") or _wholly_bold(paragraph)


def skills_section(document: Document) -> range | None:
    """The indices of the skills section's paragraphs (PRD 04 "Skills section"); None unless one paragraph is headed `Skills`."""
    paragraphs = document.paragraphs
    headings = [
        index
        for index, paragraph in enumerate(paragraphs)
        if paragraph_text(paragraph._p).strip().rstrip(":").casefold() == "skills"
    ]
    if len(headings) != 1:
        return None
    start = headings[0] + 1
    end = next(
        (
            index
            for index in range(start, len(paragraphs))
            if _ends_skills_section(paragraphs[index])
        ),
        len(paragraphs),
    )
    return range(start, end)


def has_unresolved_changes(document: Document) -> bool:
    return bool(document.element.body.xpath(_UNRESOLVED_XPATH))


def _placed_runs(paragraph: BaseOxmlElement) -> list[tuple[BaseOxmlElement, int, int]]:
    """Each run that carries text, with its start and end offsets in the paragraph."""
    placed, offset = [], 0
    for run in runs(paragraph):
        length = len(_run_text(run))
        if length:
            placed.append((run, offset, offset + length))
        offset += length
    return placed


def content(run: BaseOxmlElement) -> list[BaseOxmlElement]:
    return [child for child in run if child.tag != qn("w:rPr")]


def text_length(element: BaseOxmlElement) -> int:
    return len(str(element)) if element.tag in _TEXT_QNAMES else 0


def set_text(element: BaseOxmlElement, text: str) -> None:
    element.text = text
    element.set(_XML_SPACE, "preserve")


def _split_run(run: BaseOxmlElement, offset: int) -> None:
    """Split a run in two at a text offset inside it; both halves keep its formatting."""
    tail = copy.deepcopy(run)
    run.addnext(tail)
    position = 0
    for head_child, tail_child in zip(content(run), content(tail), strict=True):
        length = text_length(head_child)
        end = position + length
        if length and position < offset < end:
            text = head_child.text
            set_text(head_child, text[: offset - position])
            set_text(tail_child, text[offset - position :])
        elif end <= offset and (length or position < offset):
            tail.remove(tail_child)
        else:
            run.remove(head_child)
        position = end


def _cut(paragraph: BaseOxmlElement, offset: int) -> None:
    """Make `offset` fall on a run boundary."""
    for run, start, end in _placed_runs(paragraph):
        if start < offset < end:
            _split_run(run, offset - start)
            return


class _Revisions:
    """Builds tracked-change elements with ids no other element uses, and one timestamp."""

    def __init__(self, document: Document, comment_count: int) -> None:
        used = {int(value) for value in document.element.xpath("//@w:id")}
        used.update(comment.comment_id for comment in document.comments)
        # The comments added later take the ids just above the existing ones; these start past them.
        self._ids = itertools.count(max(used, default=-1) + 1 + comment_count)
        self._now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def new_id(self) -> int:
        return next(self._ids)

    def element(self, tag: str) -> BaseOxmlElement:
        element = OxmlElement(tag)
        element.set(qn("w:id"), str(self.new_id()))
        element.set(qn("w:author"), AUTHOR)
        element.set(qn("w:date"), self._now)
        return element


def _wrap(
    elements: list[BaseOxmlElement], tag: str, revisions: _Revisions
) -> list[BaseOxmlElement]:
    """Wrap each stretch of adjacent elements in one `tag` revision; returns the wrappers."""
    wrappers: list[BaseOxmlElement] = []
    for element in elements:
        if not wrappers or element.getprevious() is not wrappers[-1]:
            wrapper = revisions.element(tag)
            element.addprevious(wrapper)
            wrappers.append(wrapper)
        wrappers[-1].append(element)
    return wrappers


def _delete(
    runs: list[BaseOxmlElement], revisions: _Revisions, tag: str = "w:del"
) -> list[BaseOxmlElement]:
    """Mark the runs removed, as a deletion or, with `w:moveFrom`, as the old place of a move."""
    for run in runs:
        for text in run.xpath("w:t"):
            text.tag = qn("w:delText")
            text.set(_XML_SPACE, "preserve")
    return _wrap(runs, tag, revisions)


def _insertion(
    text: str, format_source: BaseOxmlElement, revisions: _Revisions
) -> BaseOxmlElement:
    insertion = revisions.element("w:ins")
    run = OxmlElement("w:r")
    properties = format_source.find(qn("w:rPr"))
    if properties is not None:
        run.append(copy.deepcopy(properties))
    content = OxmlElement("w:t")
    set_text(content, text)
    run.append(content)
    insertion.append(run)
    return insertion


def _apply(
    paragraph: BaseOxmlElement, change: TrackedChange, revisions: _Revisions
) -> list[BaseOxmlElement]:
    """Write one change into the paragraph; returns its tracked-change elements in order."""
    if change.start < change.end:
        _cut(paragraph, change.start)
        _cut(paragraph, change.end)
        replaced = [
            run
            for run, start, end in _placed_runs(paragraph)
            if change.start <= start and end <= change.end
        ]
        elements = _delete(replaced, revisions)
        if change.text:
            insertion = _insertion(change.text, replaced[0], revisions)
            elements[-1].addnext(insertion)
            elements.append(insertion)
        return elements
    _cut(paragraph, change.start)
    placed = _placed_runs(paragraph)
    before = [run for run, _, end in placed if end == change.start]
    if before:
        insertion = _insertion(change.text, before[-1], revisions)
        before[-1].addnext(insertion)
    else:
        after = next(run for run, start, _ in placed if start == change.start)
        insertion = _insertion(change.text, after, revisions)
        after.addprevious(insertion)
    return [insertion]


def _anchor_comment(
    document: Document,
    first: BaseOxmlElement,
    last: BaseOxmlElement,
    text: str,
) -> None:
    comment = document.comments.add_comment(
        text=text, author=AUTHOR, initials=_INITIALS
    )

    def mark(tag: str) -> BaseOxmlElement:
        element = OxmlElement(tag)
        element.set(qn("w:id"), str(comment.comment_id))
        return element

    start, end = mark("w:commentRangeStart"), mark("w:commentRangeEnd")
    reference = OxmlElement("w:r")
    reference.append(mark("w:commentReference"))
    # The range wraps the tracked elements, so rejecting a change leaves the comment on the original text.
    first.addprevious(start)
    last.addnext(end)
    end.addnext(reference)


def _comment_paragraph(
    document: Document, paragraph: BaseOxmlElement, text: str
) -> None:
    """Anchor a comment on everything in the paragraph, which is left as it is."""
    if not paragraph.xpath("./*[not(self::w:pPr)]"):
        # A range needs something to wrap; an empty run changes no text.
        paragraph.append(OxmlElement("w:r"))
    children = paragraph.xpath("./*[not(self::w:pPr)]")
    _anchor_comment(document, children[0], children[-1], text)


def _mark_paragraph(
    paragraph: BaseOxmlElement, tag: str, revisions: _Revisions
) -> None:
    """Track the paragraph's own mark, so accepting a deletion or move leaves no empty line."""
    properties = paragraph.get_or_add_pPr()
    mark_properties = properties.find(qn("w:rPr"))
    if mark_properties is None:
        mark_properties = OxmlElement("w:rPr")
        # Schema order: the mark's properties precede a section break and a recorded paragraph change.
        following = properties.find(qn("w:sectPr"))
        if following is None:
            following = properties.find(qn("w:pPrChange"))
        if following is None:
            properties.append(mark_properties)
        else:
            following.addprevious(mark_properties)
    mark_properties.insert(0, revisions.element(tag))


def _remove(
    document: Document,
    paragraph: BaseOxmlElement,
    comment: str,
    revisions: _Revisions,
) -> None:
    _mark_paragraph(paragraph, "w:del", revisions)
    deletions = _delete(runs(paragraph), revisions)
    if deletions:
        _anchor_comment(document, deletions[0], deletions[-1], comment)
    else:
        _comment_paragraph(document, paragraph, comment)


# Markers that name or anchor something once; the old place of a move must not repeat them.
_UNIQUE_MARKERS_XPATH = (
    ".//w:bookmarkStart | .//w:bookmarkEnd | .//w:commentRangeStart"
    " | .//w:commentRangeEnd | .//w:r[w:commentReference]"
)


def _old_place(paragraph: BaseOxmlElement, revisions: _Revisions) -> None:
    """Turn a copy of a paragraph into the place a move leaves."""
    for marker in paragraph.xpath(_UNIQUE_MARKERS_XPATH):
        marker.getparent().remove(marker)
    _mark_paragraph(paragraph, "w:moveFrom", revisions)
    _delete(runs(paragraph), revisions, "w:moveFrom")


def _bracket_move(
    paragraph: BaseOxmlElement, direction: str, name: str, revisions: _Revisions
) -> tuple[BaseOxmlElement, BaseOxmlElement]:
    """Name the paragraph's end of a move, so Word pairs it with the other end; returns the two markers."""
    start = revisions.element(f"w:move{direction}RangeStart")
    start.set(qn("w:name"), name)
    end = OxmlElement(f"w:move{direction}RangeEnd")
    end.set(qn("w:id"), start.get(qn("w:id")))
    properties = paragraph.pPr
    if properties is None:
        paragraph.insert(0, start)
    else:
        properties.addnext(start)
    paragraph.append(end)
    return start, end


def _move(
    document: Document,
    paragraphs: list[Paragraph],
    reorder: TrackedReorder,
    copies: dict[int, BaseOxmlElement],
    revisions: _Revisions,
) -> None:
    """Leave each listed paragraph's old text where it was, and put the paragraphs themselves, in the listed order, after the last."""
    ends = {}
    for index in reorder.paragraphs:
        moved, old = paragraphs[index]._p, copies[index]
        moved.addprevious(old)
        _old_place(old, revisions)
        name = f"move{revisions.new_id()}"
        ends[index] = _bracket_move(old, "From", name, revisions)
        _mark_paragraph(moved, "w:moveTo", revisions)
        _wrap(moved.xpath(_TOP_LEVEL_XPATH), "w:moveTo", revisions)
        _bracket_move(moved, "To", name, revisions)
    # The comment spans the old places, so rejecting the move leaves it on the original text.
    first, last = min(reorder.paragraphs), max(reorder.paragraphs)
    _anchor_comment(document, ends[first][0], ends[last][1], reorder.comment)
    target = copies[last]
    for index in reorder.paragraphs:
        target.addnext(paragraphs[index]._p)
        target = paragraphs[index]._p


def apply_edits(
    document: Document,
    edits: list[TrackedEdit],
    comments: Sequence[ParagraphComment] = (),
    removals: Sequence[TrackedRemoval] = (),
    reorders: Sequence[TrackedReorder] = (),
) -> None:
    """Write the proposals into the document as tracked changes, one comment around each.

    Later text is changed first, so the offsets of the changes still to apply stay valid. Each
    paragraph comment is anchored afterwards, around the paragraph's final content. A removal is
    written after the edits and a move after the removals, so a moved paragraph carries its own
    edits and a removed one stays where it is, inside the stretch a move reorders.
    """
    revisions = _Revisions(
        document, len(edits) + len(comments) + len(removals) + len(reorders)
    )
    paragraphs = document.paragraphs
    # Taken before any change: rejecting a move must give back the paragraph exactly as it was.
    copies = {
        index: copy.deepcopy(paragraphs[index]._p)
        for reorder in reorders
        for index in reorder.paragraphs
    }
    for edit in sorted(
        edits, key=lambda e: (e.changes[0].paragraph, e.changes[0].start), reverse=True
    ):
        # Changes apply last to first, so the earliest change's elements come last.
        applied = [
            _apply(paragraphs[change.paragraph]._p, change, revisions)
            for change in sorted(
                edit.changes, key=lambda c: (c.start, c.end), reverse=True
            )
        ]
        _anchor_comment(document, applied[-1][0], applied[0][-1], edit.comment)
    for comment in comments:
        _comment_paragraph(document, paragraphs[comment.paragraph]._p, comment.comment)
    for removal in removals:
        _remove(document, paragraphs[removal.paragraph]._p, removal.comment, revisions)
    for reorder in reorders:
        _move(document, paragraphs, reorder, copies, revisions)
