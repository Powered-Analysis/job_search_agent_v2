"""The one HTTP client factory (convention 3, XC-9): every outside HTTP call uses a client from here."""

from importlib.metadata import version

import httpx


def make_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    # Tests pass a transport so no request reaches the network (XC-9).
    return httpx.Client(
        headers={"User-Agent": f"job-search-agent/{version('jsa')}"},
        follow_redirects=True,
        timeout=30,
        transport=transport,
    )
