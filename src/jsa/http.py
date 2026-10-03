"""The one HTTP client factory and JSON GETs (convention 3, XC-9): every outside HTTP call goes through here."""

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


def get_json(client: httpx.Client, url: str) -> object:
    response = client.get(url)
    response.raise_for_status()
    return response.json()


def get_lever_json(client: httpx.Client, path: str) -> object:
    try:
        return get_json(client, f"https://api.lever.co{path}")
    except httpx.HTTPError:
        # EU-hosted boards answer only on their own host.
        return get_json(client, f"https://api.eu.lever.co{path}")
