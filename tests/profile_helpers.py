"""Shared builders for profile tests: a minimal valid profile written to a temp directory."""

import shutil
from pathlib import Path

from conftest import REPO_ROOT

EXAMPLE_DIR = REPO_ROOT / "profile.example"
FRAGMENTS = (
    "candidate",
    "target_roles",
    "filters",
    "positive_signals",
    "negative_signals",
    "hard_exclusions",
)

SEARCH_TOML = """\
timezone = "America/New_York"
run_at = "07:00"

[schedule]
monday = [{ agent = "perplexity", window_hours = 72 }]
tuesday = [
  { agent = "claude", window_hours = 24 },
  { agent = "gemini", window_hours = 48 },
]

[runners.claude]
model = "claude-opus-5-5"
effort = "high"

[runners.gemini]
agent = "deep-research-preview-04-2026"

[verification]
mode = "strict"
"""

CONFIG_TOML = """\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-id"

[fly]
app = "jsa-example"
region = "iad"

[agents.checklist]
model = "claude-fable-5-1"
effort = "medium"

[agents.refine]
model = "claude-opus-5-5"
effort = "high"
"""


def write_search_toml(profile: Path, text: str = SEARCH_TOML) -> None:
    (profile / "search").mkdir(parents=True, exist_ok=True)
    (profile / "search" / "search.toml").write_text(text, encoding="utf-8")


def write_config_toml(profile: Path, text: str = CONFIG_TOML) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.toml").write_text(text, encoding="utf-8")


def write_fragment(profile: Path, name: str, text: str) -> None:
    (profile / "search").mkdir(parents=True, exist_ok=True)
    (profile / "search" / f"{name}.md").write_text(text, encoding="utf-8")


def copy_example(destination: Path) -> Path:
    shutil.copytree(EXAMPLE_DIR, destination)
    return destination
