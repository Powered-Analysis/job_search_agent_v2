"""Shared builders for profile tests: a minimal valid profile written to a temp directory."""

import re
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

FAMILY = "analytics"
OTHER_FAMILY = "charter"
SHARED = "shared"

SEARCH_TOML = """\
timezone = "America/New_York"
run_at = "07:00"
results_per_run = 10

[schedule]
monday = [{ search_family = "analytics", agent = "perplexity", window_hours = 72 }]
tuesday = [
  { search_family = "analytics", agent = "claude", window_hours = 24 },
  { search_family = "charter", agent = "gemini", window_hours = 48 },
]

[runners.claude]
model = "claude-opus-5-5"
effort = "high"

[verification]
mode = "strict"
"""

CONFIG_TOML = """\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-id"

[fly]
app = "jsa-example"
region = "iad"

[agents.redline]
model = "claude-fable-5-1"
effort = "medium"

[agents.refine]
model = "claude-opus-5-5"
effort = "high"
"""


def write_search_toml(
    profile: Path, text: str = SEARCH_TOML, folders: bool = True
) -> None:
    """Write `search.toml`; unless `folders` is false, also make a folder for each family it schedules."""
    (profile / "search").mkdir(parents=True, exist_ok=True)
    (profile / "search" / "search.toml").write_text(text, encoding="utf-8")
    if folders:
        for family in re.findall(r'search_family = "([^"]+)"', text):
            if family != SHARED:
                (profile / "search" / family).mkdir(exist_ok=True)


def write_config_toml(profile: Path, text: str = CONFIG_TOML) -> None:
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "config.toml").write_text(text, encoding="utf-8")


def fragment_file(profile: Path, name: str, family: str = FAMILY) -> Path:
    """Where a search family's fragment lives (PRD 01 "Search families"); `family="shared"` for `shared/`."""
    return profile / "search" / family / f"{name}.md"


def write_fragment(profile: Path, name: str, text: str, family: str = FAMILY) -> None:
    path = fragment_file(profile, name, family)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def remove_fragment(profile: Path, name: str, family: str = FAMILY) -> None:
    """Make a fragment missing for `family`: delete its own file and the `shared/` one."""
    fragment_file(profile, name, family).unlink(missing_ok=True)
    fragment_file(profile, name, SHARED).unlink(missing_ok=True)


def example_fragment(name: str, family: str = FAMILY) -> str:
    """The example profile's text for a family's fragment: its own file, else `shared/`'s."""
    own = fragment_file(EXAMPLE_DIR, name, family)
    path = own if own.is_file() else fragment_file(EXAMPLE_DIR, name, SHARED)
    return path.read_text(encoding="utf-8")


def copy_example(destination: Path) -> Path:
    shutil.copytree(EXAMPLE_DIR, destination)
    return destination


ROLE_FAMILIES = ("Analytics Engineering", "Data Analysis")


def write_role_families(profile: Path, names: tuple[str, ...] = ROLE_FAMILIES) -> None:
    """Make `names` the profile's role families (PRD 04), each with a non-empty `resume.docx`."""
    folder = profile / "role_families"
    shutil.rmtree(folder, ignore_errors=True)
    for name in names:
        (folder / name).mkdir(parents=True)
        (folder / name / "resume.docx").write_bytes(b"base resume")


ROLE_FAMILY = ROLE_FAMILIES[0]


def family_dir(profile: Path, name: str = ROLE_FAMILY) -> Path:
    """A role family's folder in the profile (PRD 04): its `resume.docx` and optional `cover_letter.*` live here."""
    return profile / "role_families" / name


def use_role_families(
    tmp_path: Path, monkeypatch, names: tuple[str, ...] = ROLE_FAMILIES
) -> Path:
    """Point `JSA_PROFILE_DIR` at a copy of the example profile that keeps exactly `names`."""
    profile = copy_example(tmp_path / "profile")
    write_role_families(profile, names)
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    return profile


WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
_RUNNERS_AND_VERIFICATION = (
    "[runners.claude]" + SEARCH_TOML.split("[runners.claude]")[1]
)


def schedule_toml(
    timezone: str,
    run_at: str,
    days: dict[str, list[tuple[str, int] | tuple[str, int, str]]],
):
    """A `search.toml` whose `[schedule]` maps each named weekday to ordered searches.

    A search is `(agent, window_hours)` for the `analytics` family or `(agent, window_hours, family)`."""
    schedule = "\n".join(
        f"{day} = ["
        + ", ".join(
            f'{{ search_family = "{(rest or (FAMILY,))[0]}", agent = "{agent}", window_hours = {window} }}'
            for agent, window, *rest in searches
        )
        + "]"
        for day, searches in days.items()
    )
    return (
        f'timezone = "{timezone}"\nrun_at = "{run_at}"\nresults_per_run = 10\n\n[schedule]\n{schedule}\n\n'
        + _RUNNERS_AND_VERIFICATION
    )
