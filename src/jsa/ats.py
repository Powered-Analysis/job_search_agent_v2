"""ATS resolution: map a posting URL to its platform, board, and job id (PRD 01). Pure, I/O-free."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class AtsRef:
    platform: str
    board: str
    job_id: str


# (platform, hosts, path pattern). Greenhouse on an employer's own domain
# (`?gh_jid=`) names no board, so it matches nothing here.
_PLATFORMS = (
    (
        "greenhouse",
        {"boards.greenhouse.io", "job-boards.greenhouse.io"},
        re.compile(r"/(?P<board>[^/]+)/jobs/(?P<id>\d+)"),
    ),
    (
        "lever",
        {"jobs.lever.co", "jobs.eu.lever.co"},
        re.compile(r"/(?P<board>[^/]+)/(?P<id>[^/]+)"),
    ),
    (
        "ashby",
        {"jobs.ashbyhq.com"},
        re.compile(r"/(?P<board>[^/]+)/(?P<id>[^/]+)"),
    ),
    (
        "rippling",
        {"ats.rippling.com"},
        re.compile(r"/(?P<board>[^/]+)/jobs/(?P<id>[^/]+)"),
    ),
)


def resolve_ats(url: str) -> AtsRef | None:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    path = parts.path.rstrip("/")
    for platform, hosts, pattern in _PLATFORMS:
        if host in hosts and (match := pattern.fullmatch(path)):
            return AtsRef(platform, match["board"], match["id"])
    return None


# One definition per endpoint fetched both to verify a posting and to capture it (convention 1).
def ashby_board_url(board: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{board}"


def rippling_detail_url(board: str, job_id: str) -> str:
    return f"https://ats.rippling.com/api/v2/board/{board}/jobs/{job_id}"
