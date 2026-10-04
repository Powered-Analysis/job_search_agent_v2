"""The Google Sheet tracker (PRD 04 "Tracker write"): build a row, append it through `gws`."""

import json
import subprocess
from datetime import date, datetime

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


def append_row(spreadsheet_id: str, row: list[str | int]) -> None:
    """Append one row, raising on any ambiguity: a posting is marked tracked only after this returns."""
    # OVERWRITE fills the sheet's pre-formatted blank rows; INSERT_ROWS would lose the Status dropdown (PRD 04).
    params = {
        "spreadsheetId": spreadsheet_id,
        "range": f"{TAB}!A:H",
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "OVERWRITE",
    }
    command = [
        gws_bin(),
        "sheets",
        "spreadsheets",
        "values",
        "append",
        "--params",
        json.dumps(params),
        "--json",
        json.dumps({"values": [row]}),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as error:
        raise JsaError(f"could not run {command[0]}: {error}") from error
    if result.returncode != 0:
        detail = (result.stderr.strip() or result.stdout.strip()).partition("\n")[0]
        hint = " (re-run `gws auth login`)" if result.returncode == 2 else ""
        raise JsaError(f"gws exited {result.returncode}{hint}: {detail}")
    try:
        updated_rows = json.loads(result.stdout)["updates"]["updatedRows"]
    except ValueError, KeyError, TypeError:
        raise JsaError("gws output was not an append response") from None
    if not isinstance(updated_rows, int) or updated_rows < 1:
        raise JsaError("gws reported no updated row")


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
