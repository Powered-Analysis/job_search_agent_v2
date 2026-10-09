"""Delivering a packet folder to the Drive packets folder (PRD 04 "Packets built on the inbox machine") through `gws`."""

from collections.abc import Collection
from pathlib import Path

from jsa.errors import JsaError
from jsa.gws import run_owner_gws

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
_FILES = ("drive", "files")


def plan_uploads(local: Collection[str], uploaded: Collection[str]) -> list[str]:
    """The local file names Drive doesn't hold yet, in name order. Pure (XC-9)."""
    return sorted(set(local) - set(uploaded))


def _quoted(name: str) -> str:
    # Drive's query language quotes names with single quotes.
    return "'" + name.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _files(query: str) -> list[dict]:
    """Every non-trashed file matching `query`, oldest first."""
    found: list[dict] = []
    params: dict = {
        "q": f"{query} and trashed = false",
        "fields": "nextPageToken, files(id, name)",
        "orderBy": "createdTime",
        "pageSize": 1000,
    }
    while True:
        response = run_owner_gws((*_FILES, "list"), params)
        if not isinstance(response, dict) or not isinstance(
            response.get("files", []), list
        ):
            raise JsaError("gws output was not a Drive file list")
        found += response.get("files", [])
        if not (token := response.get("nextPageToken")):
            return found
        params = {**params, "pageToken": token}


def _create(
    metadata: dict, *, upload: str | None = None, cwd: Path | None = None
) -> str:
    """Create one Drive file or folder and return its id; raises unless Drive confirms it."""
    response = run_owner_gws(
        (*_FILES, "create"),
        {"fields": "id"},
        metadata,
        upload=upload,
        cwd=cwd,
    )
    file_id = response.get("id") if isinstance(response, dict) else None
    if not isinstance(file_id, str) or not file_id:
        raise JsaError("gws output was not a Drive create response")
    return file_id


def deliver_packet(parent_id: str, directory: Path) -> None:
    """Upload `directory`'s files into the folder of the same name under `parent_id`.

    A retry reuses the folder an earlier attempt made and uploads only the missing files;
    a file already in Drive is never replaced or deleted, because the user may have opened it.
    Files go up as they are, never converted to Google formats.
    """
    folders = _files(
        f"{_quoted(parent_id)} in parents and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and name = {_quoted(directory.name)}"
    )
    folder_id = (
        folders[0]["id"]
        if folders
        else _create(
            {
                "name": directory.name,
                "mimeType": FOLDER_MIME_TYPE,
                "parents": [parent_id],
            }
        )
    )
    uploaded = {file["name"] for file in _files(f"{_quoted(folder_id)} in parents")}
    local = [path.name for path in directory.iterdir() if path.is_file()]
    for name in plan_uploads(local, uploaded):
        # gws reads an upload only from beneath its working directory.
        _create({"name": name, "parents": [folder_id]}, upload=name, cwd=directory)
