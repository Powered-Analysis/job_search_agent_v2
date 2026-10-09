"""The one way to call `gws` (convention 3): Sheets, Drive, and Gmail all go through here."""

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from jsa.config import gws_bin, owner_gws_credentials
from jsa.errors import JsaError
from jsa.tools import run_tool

CREDENTIALS_FILE_ENV = "GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE"


def _run(command: list[str], credential: str | None, cwd: Path | None):
    if credential is None:
        return run_tool(command, cwd=cwd)
    # XC-1: gws reads an exported credential only from a file, so the secret's text goes to a private one for this call.
    with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
        file.write(credential)
        file.flush()
        return run_tool(
            command, env={**os.environ, CREDENTIALS_FILE_ENV: file.name}, cwd=cwd
        )


def run_gws(
    method: Sequence[str],
    params: dict,
    body: dict | None = None,
    *,
    credential: str | None = None,
    upload: str | None = None,
    cwd: Path | None = None,
) -> object:
    """Run one `gws <method...>` call and return its parsed JSON, raising on any failure.

    Without `credential`, gws uses its own login (XC-1); with one, the exported credential's text.
    `upload` names a file, relative to `cwd`, to send as the call's media.
    """
    command = [gws_bin(), *method, "--params", json.dumps(params)]
    if body is not None:
        command += ["--json", json.dumps(body)]
    if upload is not None:
        command += ["--upload", upload]
    result = _run(command, credential, cwd)
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


def run_owner_gws(
    method: Sequence[str],
    params: dict,
    body: dict | None = None,
    *,
    upload: str | None = None,
    cwd: Path | None = None,
) -> object:
    """`run_gws` as the owner: the tracker and the Drive packets folder (XC-1)."""
    return run_gws(
        method,
        params,
        body,
        credential=owner_gws_credentials(),
        upload=upload,
        cwd=cwd,
    )
