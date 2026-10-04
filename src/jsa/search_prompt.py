"""The one search prompt every runner sends (PRD 01, XC-13)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from jsa.assemble import Slot, app_template, assemble
from jsa.profile import SearchConfig, profile_dir

SEARCH_DIR = "search"
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


def fragment_path(filename: str) -> Path:
    return profile_dir() / SEARCH_DIR / filename


def read_fragment(filename: str) -> str | None:
    """The fragment's text; None when the user has no such file."""
    try:
        return fragment_path(filename).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _read_fragment(name: str, filename: str, replacements_dir: Path | None) -> Slot:
    required = FRAGMENTS[name][1]
    if replacements_dir is not None and (replacements_dir / filename).is_file():
        replacement = replacements_dir / filename
        return Slot(replacement.read_text(encoding="utf-8"), str(replacement), required)
    return Slot(
        read_fragment(filename),
        f"{fragment_path(filename)} (copy the shape from profile.example/{SEARCH_DIR}/{filename})",
        required,
    )


def assemble_search_prompt(config: SearchConfig, hours: int, now: datetime) -> str:
    return assemble_search_prompt_for(config, search_window(hours, now, config))


def assemble_search_prompt_for(
    config: SearchConfig,
    window: str,
    replacements_dir: Path | None = None,
) -> str:
    """The search prompt with `window` in the window slot, for a caller with no run of its own.

    A fragment file in `replacements_dir` is used in place of the live one, so a proposed change
    can be checked before it is accepted; an error about it names the file the user must fix."""
    slots = {
        "SEARCH_WINDOW": Slot(window, "the search window"),
        "LIVENESS_RULES": Slot(
            app_template(f"liveness_{config.verification.mode}.md"),
            "the app's liveness rules",
        ),
    }
    for name, (filename, _) in FRAGMENTS.items():
        slots[name] = _read_fragment(name, filename, replacements_dir)
    return assemble(app_template("search.md"), slots)
