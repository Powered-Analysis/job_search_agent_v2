"""The one search prompt every runner sends (PRD 01, XC-13)."""

from collections.abc import Collection
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsa.assemble import Slot, app_template, assemble
from jsa.profile import (
    RESUME_SKILLS,
    SEARCH_DIR,
    SHARED_FAMILY,
    SearchConfig,
    profile_dir,
    require_search_family,
    resume_skills,
)

# Slot name -> (fragment file, required); the one definition of the fragments' homes.
FRAGMENTS = {
    "CANDIDATE": ("candidate.md", True),
    "TARGET_ROLES": ("target_roles.md", True),
    "FILTERS": ("filters.md", True),
    "POSITIVE_SIGNALS": ("positive_signals.md", False),
    "NEGATIVE_SIGNALS": ("negative_signals.md", False),
    "HARD_EXCLUSIONS": ("hard_exclusions.md", False),
}


def search_window(hours: int, now: datetime, config: SearchConfig) -> str:
    """The window as concrete dates in the profile's timezone, so recency is judged against them."""
    if now.tzinfo is None:
        raise ValueError("`now` must be timezone-aware")

    def render(moment: datetime) -> str:
        return f"{moment.astimezone(config.tz):%a %Y-%m-%d %H:%M %Z}"

    start = now.astimezone(UTC) - timedelta(hours=hours)
    return f"the last {hours} hours (from {render(start)} through {render(now)})"


def fragment_path(folder: str, filename: str) -> Path:
    return profile_dir() / SEARCH_DIR / folder / filename


def fragment_source(
    family: str,
    filename: str,
    replacement: Path | None = None,
    shared_replacement: Path | None = None,
) -> Path | None:
    """The file a family's fragment comes from: its own, even empty, else `shared/`'s, else None (PRD 01).

    A replacement stands in front of the live file of its level, so a proposed change can be tried
    before it is accepted. Every reader of a family's fragments goes through this (convention 3)."""
    candidates = (
        replacement,
        fragment_path(family, filename),
        shared_replacement,
        fragment_path(SHARED_FAMILY, filename),
    )
    return next((path for path in candidates if path and path.is_file()), None)


def _fragment_slot(
    family: str,
    name: str,
    filename: str,
    replacement: Path | None,
    shared_replacement: Path | None,
) -> Slot:
    required = FRAGMENTS[name][1]
    source = fragment_source(family, filename, replacement, shared_replacement)
    if source is not None and source in (replacement, shared_replacement):
        return Slot(source.read_text(encoding="utf-8"), str(source), required)
    return Slot(
        source.read_text(encoding="utf-8") if source else None,
        f"{fragment_path(family, filename)} (or {fragment_path(SHARED_FAMILY, filename)} "
        f"when the family has no file of its own; copy the shape from profile.example/{SEARCH_DIR}/)",
        required,
    )


def assemble_search_prompt(
    config: SearchConfig, family: str, hours: int, now: datetime
) -> str:
    return assemble_search_prompt_for(config, family, search_window(hours, now, config))


def assemble_search_prompt_for(
    config: SearchConfig,
    family: str,
    window: str,
    replacements_dir: Path | None = None,
    replaceable: Collection[str] | None = None,
    shared_replacements_dir: Path | None = None,
) -> str:
    """The search family's prompt with `window` in the window slot, for a caller with no run of its own.

    A fragment file in `replacements_dir` is used in place of the family's own live file, and one in
    `shared_replacements_dir` in place of `shared/`'s, so a proposed change can be checked before it
    is accepted; an error about it names the file the user must fix.
    Only the fragments in `replaceable` (default: all) are looked for in either."""
    require_search_family(family)
    slots = {
        "SEARCH_WINDOW": Slot(window, "the search window"),
        "RESULTS_PER_RUN": Slot(
            str(config.results_per_run), "results_per_run in search.toml"
        ),
        "LIVENESS_RULES": Slot(
            app_template(f"liveness_{config.verification.mode}.md"),
            "the app's liveness rules",
        ),
    }
    for name, (filename, _) in FRAGMENTS.items():
        replacements = [
            directory / filename
            if directory is not None
            and (replaceable is None or filename in replaceable)
            else None
            for directory in (replacements_dir, shared_replacements_dir)
        ]
        slots[name] = _fragment_slot(family, name, filename, *replacements)
    skills = resume_skills()
    slots["RESUME_SKILLS"] = Slot(
        skills.read_text(encoding="utf-8") if skills else None,
        str(profile_dir() / RESUME_SKILLS),
        required=False,
    )
    return assemble(app_template("search.md"), slots)
