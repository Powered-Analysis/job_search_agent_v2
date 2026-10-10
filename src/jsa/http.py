"""The one HTTP client factory and JSON GETs (convention 3, XC-9): every outside HTTP call goes through here."""

import json
import logging
import socket
from collections.abc import Iterator
from contextlib import suppress
from importlib.metadata import version
from typing import Self

import httpx

log = logging.getLogger(__name__)


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


class SseEvents:
    """A POST's server-sent events as (event name, JSON data), readable now and abortable from another thread."""

    def __init__(
        self,
        client: httpx.Client,
        url: str,
        body: dict,
        *,
        headers: dict[str, str],
        read_timeout: float,
    ) -> None:
        self._response: httpx.Response | None = None
        self._events = self._read(client, url, body, headers, read_timeout)

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> tuple[str, dict]:
        return next(self._events)

    def close(self) -> None:
        self._events.close()

    def abort(self) -> None:
        """Drop the connection even while another thread is blocked reading it.

        Closing the response alone leaves the socket open until that read returns, so the server
        keeps working. Shutting the socket down first wakes the read and ends the connection.
        """
        response = self._response
        if response is None:
            return
        network_stream = response.extensions.get("network_stream")
        if sock := network_stream and network_stream.get_extra_info("socket"):
            with suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)
        response.close()

    def _read(
        self,
        client: httpx.Client,
        url: str,
        body: dict,
        headers: dict[str, str],
        read_timeout: float,
    ) -> Iterator[tuple[str, dict]]:
        timeout = httpx.Timeout(client.timeout.connect, read=read_timeout)
        with client.stream(
            "POST", url, json=body, headers=headers, timeout=timeout
        ) as response:
            self._response = response
            if response.is_error:
                # The status error alone omits the explanation the service sends.
                response.read()
                log.error(
                    "%s answered %s: %s", url, response.status_code, response.text
                )
                response.raise_for_status()
            name = ""
            data: list[str] = []
            for line in response.iter_lines():
                if line:
                    field, _, value = line.partition(":")
                    if field == "event":
                        name = value.strip()
                    elif field == "data":
                        data.append(value.removeprefix(" "))
                elif data:
                    yield name, json.loads("\n".join(data))
                    name, data = "", []
            if data:
                yield name, json.loads("\n".join(data))
