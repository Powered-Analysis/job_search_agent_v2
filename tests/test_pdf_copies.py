"""The packet's PDF copies, rendered by LibreOffice (issue #131; PRD 04 "PDF copies", PRD 06).

`render_pdf` and `ensure_pdf` are driven directly against the stub `soffice` (the `JSA_SOFFICE_BIN` seam,
`XC-9`). The image, README and `.env.example` are checked as text.
"""

import re
import stat
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from soffice_helpers import SOFFICE_STUB, soffice_calls

from jsa.docx_pdf import ensure_pdf, render_pdf
from jsa.errors import JsaError


def make_docx(directory: Path, name: str = "Resume.docx") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"A DOCX")
    return path


# --- the converter seam -------------------------------------------------------


def test_render_pdf_runs_the_binary_named_by_jsa_soffice_bin_headless_to_pdf(
    tmp_path, soffice
):
    source = make_docx(tmp_path / "packet")
    render_pdf(source)
    (call,) = soffice_calls(soffice)
    assert "--headless" in call
    assert call[call.index("--convert-to") + 1] == "pdf"
    assert call[-1] == str(source)


def test_the_pdf_lands_beside_the_docx_under_the_same_name_and_nothing_else_is_left(
    tmp_path,
):
    source = make_docx(tmp_path / "packet", "PatExample_Resume_Staff_Acme.docx")
    render_pdf(source)
    assert sorted(p.name for p in source.parent.iterdir()) == [
        "PatExample_Resume_Staff_Acme.docx",
        "PatExample_Resume_Staff_Acme.pdf",
    ]
    assert source.read_bytes() == b"A DOCX"


def test_soffice_is_found_on_the_path_when_jsa_soffice_bin_is_unset(
    tmp_path, monkeypatch, soffice
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "soffice"
    stub.write_text(SOFFICE_STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    monkeypatch.delenv("JSA_SOFFICE_BIN")
    monkeypatch.setenv("PATH", str(bin_dir))
    source = make_docx(tmp_path / "packet")
    render_pdf(source)
    assert source.with_suffix(".pdf").is_file()
    assert len(soffice_calls(soffice)) == 1


def test_a_missing_soffice_binary_raises_and_leaves_no_pdf(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_SOFFICE_BIN", str(tmp_path / "no-such-soffice"))
    source = make_docx(tmp_path / "packet")
    with pytest.raises(JsaError):
        render_pdf(source)
    assert sorted(p.name for p in source.parent.iterdir()) == ["Resume.docx"]


@pytest.mark.parametrize("failure", ["SOFFICE_FAIL", "SOFFICE_NO_PDF"])
def test_a_failed_render_or_one_with_no_pdf_raises_and_leaves_nothing_behind(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setenv(failure, "1")
    source = make_docx(tmp_path / "packet")
    with pytest.raises(JsaError):
        render_pdf(source)
    assert sorted(p.name for p in source.parent.iterdir()) == ["Resume.docx"]


def test_every_conversion_gets_its_own_libreoffice_profile(tmp_path, soffice):
    for number in (1, 2):
        render_pdf(make_docx(tmp_path / f"packet{number}"))
    profiles = [
        arg
        for call in soffice_calls(soffice)
        for arg in call
        if arg.startswith("-env:UserInstallation=")
    ]
    assert len(profiles) == 2
    assert profiles[0] != profiles[1]


def test_conversions_running_at_once_never_wait_on_or_fail_because_of_one_another(
    tmp_path, monkeypatch
):
    sync = tmp_path / "sync"
    sync.mkdir()
    stub = tmp_path / "overlapping-soffice"
    # Each run announces itself, then waits for the other's announcement: serialized runs time out and fail.
    stub.write_text(
        f"""#!{sys.executable}
import os, sys, time
from pathlib import Path

sync = Path({str(sync)!r})
(sync / str(os.getpid())).touch()
deadline = time.monotonic() + 20
while len(list(sync.iterdir())) < 2:
    if time.monotonic() > deadline:
        sys.exit(1)
    time.sleep(0.05)
argv = sys.argv[1:]
source = Path(argv[-1])
outdir = Path(argv[argv.index("--outdir") + 1])
(outdir / (source.stem + ".pdf")).write_text("PDF")
""",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setenv("JSA_SOFFICE_BIN", str(stub))
    sources = [make_docx(tmp_path / f"packet{n}") for n in (1, 2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(render_pdf, sources))
    assert all(source.with_suffix(".pdf").is_file() for source in sources)


def test_conversions_in_one_directory_do_not_disturb_each_other(tmp_path):
    packet = tmp_path / "packet"
    sources = [make_docx(packet, f"File{n}.docx") for n in range(4)]
    barrier = threading.Barrier(len(sources))

    def render(source: Path) -> None:
        barrier.wait(timeout=10)
        render_pdf(source)

    with ThreadPoolExecutor(max_workers=len(sources)) as pool:
        list(pool.map(render, sources))
    assert sorted(p.name for p in packet.iterdir()) == sorted(
        [s.name for s in sources] + [s.with_suffix(".pdf").name for s in sources]
    )


# --- an existing PDF is never replaced or refreshed ----------------------------


def test_ensure_pdf_renders_when_there_is_none(tmp_path, soffice):
    source = make_docx(tmp_path / "packet")
    ensure_pdf(source)
    assert source.with_suffix(".pdf").is_file()
    assert len(soffice_calls(soffice)) == 1


def test_ensure_pdf_leaves_an_existing_pdf_alone_even_after_the_docx_changes(
    tmp_path, soffice
):
    source = make_docx(tmp_path / "packet")
    source.with_suffix(".pdf").write_bytes(b"MY OWN EXPORT")
    source.write_bytes(b"THE USER EDITED THIS")
    ensure_pdf(source)
    assert source.with_suffix(".pdf").read_bytes() == b"MY OWN EXPORT"
    assert soffice_calls(soffice) == []


def test_ensure_pdf_lets_a_failed_render_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("SOFFICE_FAIL", "1")
    source = make_docx(tmp_path / "packet")
    with pytest.raises(JsaError):
        ensure_pdf(source)
    assert not source.with_suffix(".pdf").exists()


# --- the image, README and .env.example (PRD 06) -------------------------------


def dockerfile_lines() -> list[str]:
    joined = re.sub(r"\\\n", " ", (REPO_ROOT / "Dockerfile").read_text())
    return [
        line.strip()
        for line in joined.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def test_the_image_installs_libreoffice_writer_before_it_drops_to_the_jsa_user():
    lines = dockerfile_lines()
    installs = [
        i for i, line in enumerate(lines) if "libreoffice-writer" in line.lower()
    ]
    assert len(installs) == 1
    assert lines[installs[0]].startswith("RUN ")
    user = next(i for i, line in enumerate(lines) if line.startswith("USER jsa"))
    assert installs[0] < user


def test_the_image_still_carries_the_other_inbox_tools_and_its_entrypoint():
    text = (REPO_ROOT / "Dockerfile").read_text()
    for tool in ("gws", "tzdata"):
        assert tool in text
    for removed in ("pandoc", "typst"):
        assert removed not in text.lower()
    assert any(line.startswith("ENTRYPOINT") for line in dockerfile_lines())


def test_env_example_lists_jsa_soffice_bin_with_a_comment():
    lines = (REPO_ROOT / ".env.example").read_text().splitlines()
    index = next(
        i for i, line in enumerate(lines) if re.match(r"#?\s*JSA_SOFFICE_BIN=", line)
    )
    assert index > 0 and lines[index - 1].startswith("#")


def test_the_readme_describes_libreoffice_and_jsa_soffice_bin_and_no_longer_a_separate_redline_file():
    text = (REPO_ROOT / "README.md").read_text()
    assert "LibreOffice" in text
    assert "JSA_SOFFICE_BIN" in text
    assert "_redline.docx" not in text


def test_no_source_or_doc_file_still_names_a_separate_redline_docx():
    for path in [*(REPO_ROOT / "src").rglob("*.py"), REPO_ROOT / ".env.example"]:
        assert "_redline.docx" not in path.read_text(), path
