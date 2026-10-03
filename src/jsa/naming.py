"""Filesystem-safe names for packet folders and resume files, computed once at insert."""

import re
import string

_PATH_HOSTILE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_CORPORATE_SUFFIX = re.compile(
    r"[,\s]+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|plc|"
    r"ag|lp|llp|pty|pbc)\.?$",
    re.IGNORECASE,
)

TITLE_SLUG_MAX_LENGTH = 80


def _path_safe(text: str) -> str:
    cleaned = _PATH_HOSTILE.sub("", text)
    # Windows refuses names that end in a dot or space.
    return " ".join(cleaned.split()).strip(" .")


def normalize_company(company: str) -> str:
    stripped = company.strip()
    while (shorter := _CORPORATE_SUFFIX.sub("", stripped)) != stripped:
        stripped = shorter
    # A company named only "Inc" keeps its name rather than becoming empty.
    return string.capwords(_path_safe(stripped or company))


def title_slug(title: str) -> str:
    return _path_safe(_path_safe(title)[:TITLE_SLUG_MAX_LENGTH])
