"""The one way to run an external CLI (convention 3): `gws` and `fly` both go through here."""

import subprocess

from jsa.errors import JsaError


def run_tool(
    command: list[str], *, capture: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run `command` to completion; without `capture`, its output goes straight to the terminal."""
    try:
        return subprocess.run(command, capture_output=capture, text=True, check=False)
    except OSError as error:
        raise JsaError(f"could not run {command[0]}: {error}") from error
