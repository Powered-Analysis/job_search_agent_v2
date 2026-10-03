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
