"""Placeholder filling (PRD 04 "Placeholders in the resume and cover letter"): `[COMPANY]`, `[TITLE]`, `[DATE]`.

Pure over the loaded document and the three values (XC-9).
"""

import re
from bisect import bisect_right
from collections.abc import Iterator
from datetime import date
from itertools import accumulate

from docx.document import Document
from docx.oxml.xmlchemy import BaseOxmlElement

from jsa.redline_docx import (
    TEXT_XPATH,
    content,
    paragraph_text,
    runs,
    set_text,
    text_length,
)

COMPANY = "[COMPANY]"
TITLE = "[TITLE]"
DATE = "[DATE]"
_TOKEN = re.compile("|".join(map(re.escape, (COMPANY, TITLE, DATE))))


def _paragraphs(document: Document) -> Iterator[BaseOxmlElement]:
    """Every paragraph of the body (tables included), headers, and footers; text boxes are out of scope."""
    roots = [document.element.body]
    for section in document.sections:
        for part in (
            section.header,
            section.first_page_header,
            section.even_page_header,
            section.footer,
            section.first_page_footer,
            section.even_page_footer,
        ):
            # A linked header or footer is the previous section's, which is visited on its own.
            if not part.is_linked_to_previous:
                roots.append(part._element)
    for root in roots:
        yield from root.xpath(".//w:p[not(ancestor::w:txbxContent)]")


def _text_elements(paragraph: BaseOxmlElement) -> list[BaseOxmlElement]:
    return [element for run in runs(paragraph) for element in run.xpath(TEXT_XPATH)]


def holds_placeholder(document: Document, token: str) -> bool:
    return any(token in paragraph_text(p) for p in _paragraphs(document))


def _fill_paragraph(paragraph: BaseOxmlElement, values: dict[str, str]) -> bool:
    elements = _text_elements(paragraph)
    matches = list(_TOKEN.finditer("".join(map(str, elements))))
    if not matches:
        return False
    starts = list(accumulate(map(text_length, elements), initial=0))
    touched = set()
    # Last token first, so the offsets of the tokens still to fill stay valid.
    for match in reversed(matches):
        first = bisect_right(starts, match.start()) - 1
        last = bisect_right(starts, match.end() - 1) - 1
        for index in range(first, last + 1):
            element, origin = elements[index], starts[index]
            text = str(element)
            # The value lands in the token's first character's run, so it takes that formatting.
            head = (
                text[: match.start() - origin] + values[match[0]]
                if index == first
                else ""
            )
            set_text(element, head + text[match.end() - origin :])
            touched.add(element)
    # A run the token emptied (a token Word split across runs) goes; every other run stays as it was.
    for element in touched:
        if not element.text:
            run = element.getparent()
            run.remove(element)
            if not content(run):
                run.getparent().remove(run)
    return True


def fill_placeholders(
    document: Document, company: str, title: str, today: date
) -> bool:
    """Fill the three tokens in place; whether the document held any."""
    values = {
        COMPANY: company,
        TITLE: title,
        DATE: f"{today:%B} {today.day}, {today.year}",
    }
    filled = False
    for paragraph in _paragraphs(document):
        filled |= _fill_paragraph(paragraph, values)
    return filled
