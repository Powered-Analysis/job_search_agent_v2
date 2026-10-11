"""A stub `soffice` for the PDF-copy tests (PRD 06 override rule: `JSA_SOFFICE_BIN`, as `JSA_PANDOC_BIN` stands in for `pandoc`)."""

import json
import sys
from pathlib import Path

# Logs each invocation's argv to $SOFFICE_LOG, writes `<input stem>.pdf` into the `--outdir` directory
# (holding the input's file name), exits 1 when $SOFFICE_FAIL is set, and exits 0 having written nothing
# when $SOFFICE_NO_PDF is set.
SOFFICE_STUB = """\
#!{python}
import json, os, sys
from pathlib import Path

argv = sys.argv[1:]
with open(os.environ["SOFFICE_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
if os.environ.get("SOFFICE_FAIL"):
    print("soffice: stub failure", file=sys.stderr)
    sys.exit(1)
if os.environ.get("SOFFICE_NO_PDF"):
    sys.exit(0)
source = Path(argv[-1])
outdir = Path(argv[argv.index("--outdir") + 1])
(outdir / (source.stem + ".pdf")).write_text("PDF OF: " + source.name, encoding="utf-8")
"""


def install_soffice(tmp_path: Path, monkeypatch) -> Path:
    """Point `JSA_SOFFICE_BIN` at the stub; returns the path of its invocation log."""
    stub = tmp_path / "stub-soffice"
    stub.write_text(SOFFICE_STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "soffice.log"
    monkeypatch.setenv("JSA_SOFFICE_BIN", str(stub))
    monkeypatch.setenv("SOFFICE_LOG", str(log))
    monkeypatch.delenv("SOFFICE_FAIL", raising=False)
    monkeypatch.delenv("SOFFICE_NO_PDF", raising=False)
    return log


def soffice_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def rendered(log: Path) -> list[str]:
    """The file names the stub was asked to render, in order."""
    return [Path(call[-1]).name for call in soffice_calls(log)]
