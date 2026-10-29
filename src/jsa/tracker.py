"""The Google Sheet tracker (PRD 04 "Tracker write"): build a row, append it through `gws`."""

from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from datetime import date, datetime
from typing import NamedTuple, Protocol

from jsa import db
from jsa.errors import JsaError
from jsa.gws import run_owner_gws
from jsa.profile import (
    load_search_config,
    role_family_names,
    tracker_spreadsheet_id,
    unassigned_reason,
)

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
    return run_owner_gws(("sheets", "spreadsheets", "values", method), params, body)


def _range(tab: str, cells: str) -> str:
    """An A1 range on one tab; the quotes keep a name with spaces whole, and a quote in it is doubled."""
    quoted = tab.replace("'", "''")
    return f"'{quoted}'!{cells}"


@contextmanager
def _on_tab(tab: str) -> Iterator[None]:
    """A failure inside names the tab, since a role family's tab may not exist yet (PRD 04)."""
    try:
        yield
    except JsaError as error:
        raise JsaError(f"the {tab!r} tab: {error}") from None


def append_row(spreadsheet_id: str, tab: str, row: list[str | int]) -> None:
    """Append one row to a role family's tab, raising on any ambiguity: a posting is marked tracked only after this returns."""
    # OVERWRITE fills the sheet's pre-formatted blank rows; INSERT_ROWS would lose the Status dropdown (PRD 04).
    params = {
        "spreadsheetId": spreadsheet_id,
        "range": _range(tab, "A:H"),
        "valueInputOption": "USER_ENTERED",
        "insertDataOption": "OVERWRITE",
    }
    with _on_tab(tab):
        response = _gws("append", params, {"values": [row]})
        try:
            updated_rows = response["updates"]["updatedRows"]
        except KeyError, TypeError:
            raise JsaError("gws output was not an append response") from None
        if not isinstance(updated_rows, int) or updated_rows < 1:
            raise JsaError("gws reported no updated row")


class SheetRow(NamedTuple):
    tab: str
    number: int
    date_applied: str


def sheet_index(spreadsheet_id: str, tabs: Iterable[str]) -> dict[int, SheetRow]:
    """`postings.id` -> its tab, row number and Date Applied, across the role families' tabs.

    Rows whose ID cell isn't an integer are skipped; a posting on two tabs is found on the first listed.
    """
    index: dict[int, SheetRow] = {}
    for tab in tabs:
        with _on_tab(tab):
            # The range starts at row 1, so a value's position is its row number; blank rows come back empty.
            response = _gws(
                "get", {"spreadsheetId": spreadsheet_id, "range": _range(tab, "A:G")}
            )
            rows = response.get("values", []) if isinstance(response, dict) else None
            if not isinstance(rows, list):
                raise JsaError("gws output was not a get response")
        for number, row in enumerate(rows, start=1):
            cells = [str(cell).strip() for cell in row] if isinstance(row, list) else []
            if cells and cells[0].isdecimal():
                index.setdefault(
                    int(cells[0]),
                    SheetRow(tab, number, cells[6] if len(cells) > 6 else ""),
                )
    return index


def set_title(spreadsheet_id: str, tab: str, row_number: int, title: str) -> None:
    """Rewrite one row's Title cell (column C) on its tab; raises on any ambiguity, like the append."""
    params = {
        "spreadsheetId": spreadsheet_id,
        "range": _range(tab, f"C{row_number}"),
        "valueInputOption": "USER_ENTERED",
    }
    with _on_tab(tab):
        response = _gws("update", params, {"values": [[_literal(title)]]})
        try:
            updated_cells = response["updatedCells"]
        except KeyError, TypeError:
            raise JsaError("gws output was not an update response") from None
        if not isinstance(updated_cells, int) or updated_cells < 1:
            raise JsaError("gws reported no updated cell")


def entry_row(entry: db.TrackerEntry, today: date) -> list[str | int]:
    return tracker_row(
        entry.id,
        entry.normalized_company,
        entry.title,
        entry.url,
        entry.date_posted,
        today,
    )


def append_tracked(
    conn: db.Connection, spreadsheet_id: str, today: date, entry: db.TrackerEntry
) -> None:
    """Append one tracker-queue row to its role family's tab, then mark it tracked; raises JsaError if the append fails."""
    append_row(spreadsheet_id, entry.role_family, entry_row(entry, today))
    db.mark_tracked(conn, entry.id)


def today() -> date:
    """Today's date in the profile's `timezone`: a row's Date Added."""
    return datetime.now(load_search_config().tz).date()


class HasRoleFamily(Protocol):
    id: int
    role_family: str | None


def flag_unassigned[Row: HasRoleFamily](
    rows: list[Row], known: Collection[str]
) -> list[Row]:
    """The rows whose role family the profile has; each other row is flagged and dropped, so it stays queued (PRD 04)."""
    kept = []
    for row in rows:
        if reason := unassigned_reason(row.id, row.role_family, known):
            print(f"flagged: posting {row.id}: {reason}")
        else:
            kept.append(row)
    return kept


def track(posting_id: int | None, *, dry_run: bool) -> None:
    names = role_family_names()
    spreadsheet_id = tracker_spreadsheet_id()
    added = today()
    conn = db.connect()
    queue = db.tracker_queue(conn, posting_id)
    if not queue:
        print("No postings awaiting the tracker.")
        return
    ready = flag_unassigned(queue, names)
    failures = []
    for entry in ready:
        pid = entry.id
        if dry_run:
            print(f"would append to {entry.role_family}: {entry_row(entry, added)}")
            continue
        try:
            append_tracked(conn, spreadsheet_id, added, entry)
        except JsaError as error:
            print(f"failed: posting {pid}: {error}")
            failures.append(pid)
            continue
        print(f"appended: posting {pid}")
    if failures:
        raise JsaError(
            f"{len(failures)} of {len(ready)} rows failed to append: "
            + ", ".join(map(str, failures))
        )
