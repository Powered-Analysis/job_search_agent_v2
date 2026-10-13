"""Loading the base resume (PRD 04 "Base resume")."""

from pathlib import Path
from zipfile import BadZipFile

import docx
from docx.document import Document
from docx.opc.exceptions import PackageNotFoundError

from jsa.errors import JsaError


def load_resume(path: Path) -> Document:
    try:
        return docx.Document(str(path))
    except (PackageNotFoundError, BadZipFile) as error:
        raise JsaError(f"{path} is not a readable .docx file: {error}") from error
