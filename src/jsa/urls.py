"""URL canonicalization: the single idempotency key for postings (XC-3)."""

from urllib.parse import urlsplit, urlunsplit

# Tracking and session parameters only. A parameter that identifies the job
# (such as Greenhouse's gh_jid) must never be listed here.
_TRACKING_PARAMS = frozenset(
    {
        "_ga",
        "_gl",
        "_hsenc",
        "_hsmi",
        "dclid",
        "fbclid",
        "gclid",
        "gh_src",
        "igshid",
        "jsessionid",
        "lever-origin",
        "lever-source",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "phpsessid",
        "ref",
        "referrer",
        "session_id",
        "sessionid",
        "source",
        "src",
        "trk",
        "yclid",
    }
)

# Discovery sites, never the employer's own record (XC-3, PRD 01). The list is
# necessarily incomplete.
_AGGREGATOR_DOMAINS = (
    "linkedin.com",
    "indeed.com",
    "glassdoor.com",
    "ziprecruiter.com",
    "monster.com",
    "simplyhired.com",
    "careerbuilder.com",
    "dice.com",
    "builtin.com",
    "wellfound.com",
)

_GREENHOUSE_BOARD_HOST = "boards.greenhouse.io"
_GREENHOUSE_CANONICAL_HOST = "job-boards.greenhouse.io"


def _is_tracking(param: str) -> bool:
    name = param.partition("=")[0].lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if host == _GREENHOUSE_BOARD_HOST:
        host = _GREENHOUSE_CANONICAL_HOST
    if parts.port:
        host = f"{host}:{parts.port}"
    # Sorted so the same parameters in a different order are the same URL.
    query = "&".join(
        sorted(p for p in parts.query.split("&") if p and not _is_tracking(p))
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))


def is_aggregator(url: str) -> bool:
    host = (urlsplit(url.strip()).hostname or "").lower()
    return any(
        host == domain or host.endswith(f".{domain}") for domain in _AGGREGATOR_DOMAINS
    )
