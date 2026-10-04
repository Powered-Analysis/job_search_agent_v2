"""Filesystem-safe names for packet folders and resume files, computed once at insert."""

import re
import string

_PATH_HOSTILE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
# The optional leading connector keeps "Acme & Co" from becoming "Acme &".
_CORPORATE_SUFFIX = re.compile(
    r"(?:[,\s]+(?:&|and))?[,\s]+(inc|incorporated|llc|l\.l\.c|ltd|limited|corp|corporation|co|gmbh|plc|"
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


def company_from_board(board: str) -> str:
    """Derive a company name from an ATS board slug, e.g. "acme-corp" -> "Acme Corp"."""
    words = re.split(r"[-_.+]+", board)
    return " ".join(word.capitalize() for word in words if word)


def packet_dir_name(
    normalized_company: str, slug: str, posting_id: int, *, shares_name: bool
) -> str:
    """The packet folder name; `shares_name` is set on every posting but the lowest id of its name (PRD 04)."""
    name = f"{normalized_company} - {slug}"
    return f"{name} ({posting_id})" if shares_name else name


def resume_file_stem(
    candidate_name: str | None, slug: str, normalized_company: str
) -> str:
    parts = [_path_safe(candidate_name or ""), "Resume", slug, normalized_company]
    return "_".join(part for part in parts if part).replace(" ", "")
