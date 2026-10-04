"""The Google Sheet tracker (PRD 04 "Tracker write"): build a row, append it through `gws`."""

import json
import subprocess
from datetime import date, datetime
from typing import NamedTuple

from jsa import db
from jsa.config import gws_bin
from jsa.errors import JsaError
from jsa.profile import load_search_config, tracker_spreadsheet_id

TAB = "Applications"
_FORMULA_STARTS = ("=", "+", "-", "@")


def _literal(text: str) -> str:
    # USER_ENTERED runs posting text that looks like a formula; a leading apostrophe stores it as text.
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def tracker_row(
    posting_id: int,
    company: str,
    title: str,
    url: str,
    date_posted: str | None,
    today: date,
) -> list[str | int]:
    """Columns A:H; Date Applied and Status stay blank because the user owns them (XC-4). Pure (XC-9)."""
    return [
        posting_id,
        _literal(company),
        _literal(title),
        _literal(url),
        date_posted or "",
        today.isoformat(),
        "",
        "",
    ]


def _gws(method: str, params: dict, body: dict | None = None) -> object:
    """Run one `gws sheets spreadsheets values` call and return its parsed JSON, raising on any failure."""
    command = [
        gws_bin(),
        "sheets",
        "spreadsheets",
        "values",
        method,
        "--params",
        json.dumps(params),
    ]
    if body is not None:
        command += ["--json", json.dumps(body)]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise JsaError(f"could not run {command[0]}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).partition("\n")[0]
        hint = " (re-run `gws auth login`)" if result.returncode == 2 else ""
        raise JsaError(f"gws exited {result.returncode}{hint}: {detail}")
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise JsaError("gws output was not JSON") from None


def append_row(spreadsheet_id: str, row: list[str | int]) -> None:
    """Append one row, raising on any ambiguity: a posting is marked tracked only after this returns."""
    # OVERWRITE fills the sheet's pre-formatted blank rows; INSERT_ROWS would lose the Status dropdown (PRD 04).
    params = {
        "spreadsheetId": spreadsheet_id,
        "range": f"{TAB}!A:H",
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "OVERWRITE",
    }
    response = _gws("append", params, {"values": [row]})
    try:
        updated_rows = response["updates"]["updatedRows"]
    except KeyError, TypeError:
        raise JsaError("gws output was not an append response") from None
    if not isinstance(updated_rows, int) or updated_rows < 1:
        raise JsaError("gws reported no updated row")


class SheetRow(NamedTuple):
    number: int
    date_applied: str


def sheet_index(spreadsheet_id: str) -> dict[int, SheetRow]:
    """`postings.id` -> its Sheet row number and Date Applied; rows whose ID cell isn't an integer are skipped."""
    # The range starts at row 1, so a value's position is its row number; blank rows come back empty.
    response = _gws("get", {"spreadsheetId": spreadsheet_id, "range": f"{TAB}!A:G"})
    rows = response.get("values", []) if isinstance(response, dict) else None
    if not isinstance(rows, list):
        raise JsaError("gws output was not a get response")
    index: dict[int, SheetRow] = {}
    for number, row in enumerate(rows, start=1):
        cells = [str(cell).strip() for cell in row] if isinstance(row, list) else []
        if cells and cells[0].isdecimal():
            index.setdefault(
                int(cells[0]), SheetRow(number, cells[6] if len(cells) > 6 else "")
            )
    return index


def set_title(spreadsheet_id: str, row_number: int, title: str) -> None:
    """Rewrite one row's Title cell (column C); raises on any ambiguity, like the append."""
    params = {
        "spreadsheetId": spreadsheet_id,
        "range": f"{TAB}!C{row_number}",
        "valueInputOption": "USER_ENTERED",
    }
    response = _gws("update", params, {"values": [[_literal(title)]]})
    try:
        updated_cells = response["updatedCells"]
    except KeyError, TypeError:
        raise JsaError("gws output was not an update response") from None
    if not isinstance(updated_cells, int) or updated_cells < 1:
        raise JsaError("gws reported no updated cell")


def append_tracked(
    conn: db.Connection, spreadsheet_id: str, today: date, entry: tuple
) -> None:
    """Append one tracker-queue row, then mark it tracked; raises JsaError if the append fails."""
    posting_id, company, title, url, date_posted = entry
    append_row(
        spreadsheet_id, tracker_row(posting_id, company, title, url, date_posted, today)
    )
    db.mark_tracked(conn, posting_id)


def track(posting_id: int | None, *, dry_run: bool) -> None:
    spreadsheet_id = tracker_spreadsheet_id()
    today = datetime.now(load_search_config().tz).date()
    conn = db.connect()
    queue = db.tracker_queue(conn, posting_id)
    if not queue:
        print("No postings awaiting the tracker.")
        return
    failures = []
    for entry in queue:
        pid = entry[0]
        if dry_run:
            print(f"would append: {tracker_row(*entry, today)}")
            continue
        try:
            append_tracked(conn, spreadsheet_id, today, entry)
        except JsaError as error:
            print(f"failed: posting {pid}: {error}")
            failures.append(pid)
            continue
        print(f"appended: posting {pid}")
    if failures:
        raise JsaError(
            f"{len(failures)} of {len(queue)} rows failed to append: "
            + ", ".join(map(str, failures))
        )
