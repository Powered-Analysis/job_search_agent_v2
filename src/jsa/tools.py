"""The one way to run an external CLI (convention 3): `gws` and `fly` both go through here."""

import subprocess
from pathlib import Path

from jsa.errors import JsaError


def run_tool(
    command: list[str],
    *,
    capture: bool = True,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `command` to completion; without `capture`, its output goes straight to the terminal.

    `env` replaces the inherited environment when given; `cwd` is the directory it runs in.
    """
    try:
        return subprocess.run(
            command, capture_output=capture, text=True, check=False, env=env, cwd=cwd
        )
    except OSError as error:
        raise JsaError(f"could not run {command[0]}: {error}") from error
