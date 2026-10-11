"""Resume rendering (PRD 04 "Base resume"): a .docx as text the checklist agent can read."""

from itertools import groupby
from pathlib import Path
from zipfile import BadZipFile

import docx
from docx.document import Document
from docx.opc.exceptions import PackageNotFoundError
from docx.text.paragraph import Paragraph
from docx.text.run import Run

from jsa.errors import JsaError


def _accepted_runs(paragraph: Paragraph) -> list[Run]:
    """The runs as they read once every pending tracked change is accepted (PRD 04): inserted text stays, deleted text goes."""
    return [
        Run(run, paragraph)
        for run in paragraph._p.xpath("./w:r | ./w:ins/w:r | ./w:moveTo/w:r")
    ]


def _render_paragraph(paragraph: Paragraph) -> str:
    # Adjacent bold runs merge, so one bold phrase is marked once however Word split it.
    pieces = []
    for bold, runs in groupby(
        _accepted_runs(paragraph), key=lambda run: run.bold is True
    ):
        text = "".join(run.text for run in runs)
        core = text.strip()
        if bold and core:
            # Markers hug the words: `** word **` would not read as bold.
            start = text.index(core)
            text = f"{text[:start]}**{core}**{text[start + len(core) :]}"
        pieces.append(text)
    return "".join(pieces)


def render_document(document: Document) -> str:
    """Paragraphs in order, bold as `**…**`. Pure over the loaded document (XC-9)."""
    return "\n".join(_render_paragraph(p) for p in document.paragraphs)


def load_resume(path: Path) -> Document:
    try:
        return docx.Document(str(path))
    except (PackageNotFoundError, BadZipFile) as error:
        raise JsaError(f"{path} is not a readable .docx file: {error}") from error


def render_resume(path: Path) -> str:
    return render_document(load_resume(path))
