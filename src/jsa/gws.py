"""The one way to call `gws` (convention 3): Sheets and Gmail both go through here."""

import json
import os
import tempfile
from collections.abc import Sequence

from jsa.config import gws_bin
from jsa.errors import JsaError
from jsa.tools import run_tool

CREDENTIALS_FILE_ENV = "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"


def _run(command: list[str], credential: str | None):
    if credential is None:
        return run_tool(command)
    # XC-1: gws reads an exported credential only from a file, so the secret's text goes to a private one for this call.
    with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
        file.write(credential)
        file.flush()
        return run_tool(command, env={**os.environ, CREDENTIALS_FILE_ENV: file.name})


def run_gws(
    method: Sequence[str],
    params: dict,
    body: dict | None = None,
    *,
    credential: str | None = None,
) -> object:
    """Run one `gws <method...>` call and return its parsed JSON, raising on any failure.

    Without `credential`, gws uses its own login (XC-1); with one, the exported credential's text.
    """
    command = [gws_bin(), *method, "--params", json.dumps(params)]
    if body is not None:
        command += ["--json", json.dumps(body)]
    result = _run(command, credential)
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).partition("\n")[0]
        hint = (
            " (re-run `gws auth login`)"
            if result.returncode == 2 and credential is None
            else ""
        )
        raise JsaError(f"gws exited {result.returncode}{hint}: {detail}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise JsaError("gws output was not JSON") from None
