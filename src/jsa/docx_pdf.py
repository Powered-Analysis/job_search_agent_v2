"""PDF copies of a packet's `.docx` files (PRD 04 "PDF copies"), rendered by LibreOffice."""

import tempfile
from pathlib import Path

from jsa.config import soffice_bin
from jsa.errors import JsaError
from jsa.tools import run_tool


def render_pdf(docx: Path) -> None:
    """Render `docx` to a PDF of the same name beside it; raises, leaving no partial PDF, if it can't."""
    pdf = docx.with_suffix(".pdf")
    # Scratch space beside the packet so the finished PDF is moved in whole (one filesystem, one rename).
    with tempfile.TemporaryDirectory(dir=docx.parent, prefix=".soffice-") as scratch:
        scratch_dir = Path(scratch)
        rendered = scratch_dir / pdf.name
        result = run_tool(
            [
                soffice_bin(),
                # Each conversion gets its own LibreOffice profile: instances sharing one lock it,
                # so a second row's render would fail or wait on the first (PRD 04, worker pool).
                f"-env:UserInstallation={(scratch_dir / 'profile').as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(scratch_dir),
                str(docx),
            ]
        )
        if result.returncode != 0:
            detail = (result.stderr.strip() or result.stdout.strip()).partition("\n")[0]
            raise JsaError(f"soffice exited {result.returncode}: {detail}")
        if not rendered.is_file():
            raise JsaError(f"soffice produced no PDF for {docx.name}")
        rendered.replace(pdf)


def ensure_pdf(docx: Path) -> None:
    """Render `docx`'s PDF unless it has one: an existing PDF is never replaced or refreshed (PRD 04)."""
    if not docx.with_suffix(".pdf").exists():
        render_pdf(docx)
