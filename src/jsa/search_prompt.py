"""The one search prompt every runner sends (PRD 01, XC-13)."""

from datetime import datetime, timedelta

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

    start = now - timedelta(hours=hours)
    return f"the last {hours} hours (from {render(start)} through {render(now)})"


def _read_fragment(name: str, filename: str) -> Slot:
    path = profile_dir() / SEARCH_DIR / filename
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = None
    return Slot(
        text,
        f"{path} (copy the shape from profile.example/{SEARCH_DIR}/{filename})",
        required=FRAGMENTS[name][1],
    )


def assemble_search_prompt(config: SearchConfig, hours: int, now: datetime) -> str:
    slots = {
        "SEARCH_WINDOW": Slot(search_window(hours, now, config), "the search window"),
        "LIVENESS_RULES": Slot(
            app_template(f"liveness_{config.verification.mode}.md"),
            "the app's liveness rules",
        ),
    }
    for name, (filename, _) in FRAGMENTS.items():
        slots[name] = _read_fragment(name, filename)
    return assemble(app_template("search.md"), slots)
