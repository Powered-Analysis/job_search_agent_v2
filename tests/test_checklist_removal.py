"""The resume checklist, `pandoc` and `typst` are gone (issue #150; PRD 04, PRD 06).

Checked as text and as importable names; behavior is covered by the generate, refetch, deploy and
inbox tests.
"""

import importlib
import inspect
import re

import pytest
from conftest import REPO_ROOT

from jsa import config, generate, resume
from jsa.profile import AgentsConfig

SCANNED = [
    *(REPO_ROOT / "src").rglob("*"),
    *(REPO_ROOT / "profile.example").rglob("*"),
    *(REPO_ROOT / "scripts").rglob("*"),
    REPO_ROOT / "Dockerfile",
    REPO_ROOT / ".env.example",
    REPO_ROOT / "README.md",
]
SCANNED_TEXT = [
    path
    for path in SCANNED
    if path.is_file() and path.suffix not in {".pyc", ".docx", ".pdf"}
]


def texts():
    return [
        (path, path.read_text(encoding="utf-8", errors="ignore"))
        for path in SCANNED_TEXT
    ]


def test_no_shipped_file_mentions_the_checklist():
    offenders = [p for p, text in texts() if "checklist" in text.lower()]
    assert offenders == []


def test_no_shipped_file_mentions_pandoc_typst_or_their_override():
    offenders = [
        p for p, text in texts() if re.search(r"pandoc|typst", text, re.IGNORECASE)
    ]
    assert offenders == []


def test_the_checklist_module_and_template_do_not_exist():
    with pytest.raises(ImportError):
        importlib.import_module("jsa.checklist")
    assert not (REPO_ROOT / "src/jsa/templates/checklist.md").exists()


def test_the_pandoc_helper_is_gone():
    assert not hasattr(config, "pandoc_bin")
    with pytest.raises(ImportError):
        importlib.import_module("jsa.pandoc_helpers")


def test_the_checklist_only_resume_rendering_is_gone_and_load_resume_stays():
    for name in ("render_resume", "render_document"):
        assert not hasattr(resume, name)
    assert callable(resume.load_resume)


def test_there_is_no_packet_agents_wrapper():
    assert not hasattr(generate, "PacketAgents")
    assert not hasattr(generate, "packet_agents")


def test_build_packet_takes_the_redline_settings_directly():
    parameters = inspect.signature(generate.build_packet).parameters
    assert "redline" in parameters
    assert "agents" not in parameters


def test_agents_config_has_no_checklist_field_and_still_has_redline_and_refine():
    assert "checklist" not in AgentsConfig.model_fields
    assert {"redline", "refine"} <= set(AgentsConfig.model_fields)


def test_the_example_config_has_the_redline_table_and_its_wall_clock_example():
    text = (REPO_ROOT / "profile.example/config.toml").read_text(encoding="utf-8")
    assert "[agents.redline]" in text
    assert "wall_clock_seconds" in text
    assert "[agents.checklist]" not in text


def test_the_live_profile_script_sets_model_and_effort_for_redline_and_refine_only():
    text = (REPO_ROOT / "scripts/live_profile.sh").read_text(encoding="utf-8")
    (loop,) = re.findall(r"for agent in ([^;\n]+);", text)
    assert loop.split() == ["redline", "refine"]


def test_the_readme_tells_the_user_to_review_the_resume_copys_tracked_changes():
    text = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "tracked changes" in text
    assert "lone `.docx`" in text.lower() or "lone .docx" in text.lower()
    assert "`.docx` and `.pdf`" in text
