"""A stub `pandoc` for the packet tests (PRD 06 override rule: `JSA_PANDOC_BIN`, as `JSA_GWS_BIN` stands in for `gws`)."""

import json
import sys
from pathlib import Path

# Logs each invocation's argv to $PANDOC_LOG, writes the file named after `-o` (holding the input's text),
# and exits 1 when $PANDOC_FAIL is set.
PANDOC_STUB = """\
#!{python}
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["PANDOC_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
if os.environ.get("PANDOC_FAIL"):
    print("pandoc: stub failure", file=sys.stderr)
    sys.exit(1)
with open(argv[0], encoding="utf-8") as source:
    text = source.read()
with open(argv[argv.index("-o") + 1], "w", encoding="utf-8") as out:
    out.write("PDF OF: " + text)
"""


def install_pandoc(tmp_path: Path, monkeypatch) -> Path:
    """Point `JSA_PANDOC_BIN` at the stub; returns the path of its invocation log."""
    stub = tmp_path / "stub-pandoc"
    stub.write_text(PANDOC_STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(0o755)
    log = tmp_path / "pandoc.log"
    monkeypatch.setenv("JSA_PANDOC_BIN", str(stub))
    monkeypatch.setenv("PANDOC_LOG", str(log))
    monkeypatch.delenv("PANDOC_FAIL", raising=False)
    return log


def pandoc_calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]
