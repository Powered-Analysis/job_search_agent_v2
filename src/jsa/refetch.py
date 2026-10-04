"""`jsa refetch` (PRD 04 "Reconciliation"): carry an employer's edits into the database, the tracker, and the packet."""

from contextlib import closing
from pathlib import Path

import httpx

from jsa import db, tracker
from jsa.ats import resolve_ats
from jsa.capture import Capture, CaptureError, capture_posting
from jsa.errors import JsaError, one_line
from jsa.generate import build_packet
from jsa.packet import JOB_POSTING, packet_paths
from jsa.profile import (
    AgentSettings,
    Config,
    base_resume,
    checklist_settings,
    load_config,
    tracker_spreadsheet_id,
)


def _read_sheet(
    spreadsheet_id: str, *, required: bool
) -> dict[int, tracker.SheetRow] | None:
    try:
        return tracker.sheet_index(spreadsheet_id)
    except JsaError as error:
        # The default scope is defined by the Sheet, so guessing at it would defeat it (PRD 04).
        if required:
            raise
        print(
            f"warning: could not read the tracker, so no Sheet title will be updated: {one_line(error)}"
        )
        return None


def _unapplied(index: dict[int, tracker.SheetRow], posting_id: int) -> bool:
    row = index.get(posting_id)
    return row is None or not row.date_applied


def _taken(name: Path, source: Path) -> bool:
    # A case-insensitive disk (macOS) finds the source itself under a case-only new name; that is no clash.
    return name.exists() and not (source.exists() and name.samefile(source))


def _rename_packet(
    old_directory: Path, old_copy: Path, new_directory: Path, new_copy: Path
) -> str | None:
    """Move the packet to its new names; returns why nothing was renamed when a name is taken."""
    if _taken(new_directory, old_directory):
        return f"{new_directory} already exists"
    renamed_copy = old_directory / new_copy.name
    copy_moves = new_copy.name != old_copy.name
    if copy_moves and _taken(renamed_copy, old_copy):
        return f"{renamed_copy} already exists"
    # The copy moves first: if the folder rename then fails, the user's edits still sit under the name
    # the regenerated packet would look for, and nothing is ever copied over them.
    if copy_moves and old_copy.exists():
        old_copy.rename(renamed_copy)
    if new_directory != old_directory:
        old_directory.rename(new_directory)
    return None


def _neighbour_moves(
    before: list[db.PacketJob],
    after: list[db.PacketJob],
    config: Config,
    posting_id: int,
) -> list[tuple[int, Path, Path]]:
    """The other postings' packet folders whose names a retitle changed, as (id, old, new)."""
    old_names = {job.id: packet_paths(config, job)[0] for job in before}
    moves = []
    for job in after:
        new_directory = packet_paths(config, job)[0]
        old_directory = old_names[job.id]
        if (
            job.id != posting_id
            and old_directory != new_directory
            and old_directory.is_dir()
        ):
            moves.append((job.id, old_directory, new_directory))
    return moves


def _move_neighbours(
    moves: list[tuple[int, Path, Path]], *, last_pass: bool
) -> tuple[list[tuple[int, Path, Path]], list[str]]:
    """Rename the folders whose new name is free; returns those still waiting and the hand fixes."""
    waiting, fixes = [], []
    for move in moves:
        posting_id, old_directory, new_directory = move
        if _taken(new_directory, old_directory):
            if last_pass:
                fixes.append(
                    f"posting {posting_id}'s packet {old_directory} was not renamed: "
                    f"{new_directory} already exists; rename it by hand"
                )
            else:
                waiting.append(move)
            continue
        try:
            old_directory.rename(new_directory)
        except OSError as error:
            fixes.append(
                f"posting {posting_id}'s packet {old_directory} was not renamed to "
                f"{new_directory} ({one_line(error)}); rename it by hand"
            )
    return waiting, fixes


def _refresh_packet(
    fresh: db.PacketJob,
    old_directory: Path,
    old_copy: Path,
    config: Config,
    resume: Path,
    settings: AgentSettings,
) -> str | None:
    """Rename and regenerate the packet in place; returns what needs a hand fix, if anything."""
    new_directory, new_copy = packet_paths(config, fresh)
    try:
        taken = _rename_packet(old_directory, old_copy, new_directory, new_copy)
        if taken:
            return (
                f"{taken}, so nothing was renamed or regenerated; rename the packet by hand "
                f"to match the new title, then run `jsa generate --id {fresh.id}`"
            )
        # PRD 04: exactly `jsa generate --id`, which keeps the resume copy and rewrites the rest.
        if not build_packet(fresh, config, resume, settings, rewrite=True):
            return (
                f"no job description to assess; fill in {new_directory / JOB_POSTING}, "
                f"then run `jsa generate --id {fresh.id}`"
            )
    except (JsaError, OSError) as error:
        # Never rolled back: the database already holds the employer's edit.
        return (
            f"the packet was not refreshed ({one_line(error)}); "
            f"re-run `jsa generate --id {fresh.id}`"
        )
    return None


def _describe(
    job: db.PacketJob, location: str | None, captured: Capture, title: str | None
) -> list[str]:
    changes = []
    if title is not None and title != job.title:
        changes.append(f"title {job.title!r} -> {title!r}")
    if captured.jd_markdown != job.jd_markdown:
        changes.append("description")
    if captured.location != location:
        changes.append("location")
    return changes


def _reconcile(
    client: httpx.Client,
    target: db.RefetchTarget,
    index: dict[int, tracker.SheetRow] | None,
    spreadsheet_id: str,
    config: Config,
    resume: Path,
    settings: AgentSettings,
    *,
    dry_run: bool,
) -> list[str]:
    """Reconcile one posting; returns the hand fixes it left."""
    job = target.job
    try:
        captured = capture_posting(client, job.url, resolve_ats(job.url))
    except CaptureError as error:
        # XC-6: a failed fetch leaves the row completely untouched.
        print(f"unchanged: posting {job.id}: fetch failed ({one_line(error)})")
        return []
    # PRD 03: the title a user confirmed at add is never replaced by the employer's.
    title = None if target.search_agent == "manual" else captured.title
    changes = _describe(job, target.location, captured, title)
    if not changes:
        print(f"unchanged: posting {job.id}")
        return []
    title_changed = title is not None and title != job.title
    drift = title_changed or captured.jd_markdown != job.jd_markdown
    old_directory, old_copy = packet_paths(config, job)
    has_packet = old_directory.is_dir()
    if dry_run:
        print(f"would update: posting {job.id}: {', '.join(changes)}")
        if has_packet and drift:
            print(f"would refresh the packet: {old_directory}")
        return []
    # A fresh connection: the server drops one left idle through the previous row's checklist run.
    with closing(db.connect()) as conn:
        before = db.company_packet_jobs(conn, job.normalized_company)
        db.capture_jd(
            conn,
            job.id,
            jd_markdown=captured.jd_markdown,
            location=captured.location,
            title=title,
        )
        fresh = db.refetch_targets(conn, posting_id=job.id)[0].job
        after = db.company_packet_jobs(conn, job.normalized_company)
    print(f"updated: posting {job.id}: {', '.join(changes)}")
    fixes = []
    sheet_row = index.get(job.id) if index is not None else None
    if title_changed and sheet_row and not sheet_row.date_applied:
        try:
            tracker.set_title(spreadsheet_id, sheet_row.number, fresh.title)
        except JsaError as error:
            fixes.append(
                f"the Sheet's Title cell C{sheet_row.number} was not updated "
                f"({one_line(error)}); fix it by hand"
            )
    # A retitle can add or drop the "(id)" suffix of another posting's folder. A folder that gains it
    # moves first, freeing the plain name for this posting; one that loses it waits for this posting to move.
    moves = _neighbour_moves(before, after, config, job.id) if title_changed else []
    waiting, neighbour_fixes = _move_neighbours(moves, last_pass=False)
    fixes.extend(neighbour_fixes)
    if (
        has_packet
        and drift
        and (
            problem := _refresh_packet(
                fresh, old_directory, old_copy, config, resume, settings
            )
        )
    ):
        fixes.append(problem)
    fixes.extend(_move_neighbours(waiting, last_pass=True)[1])
    return fixes


def refetch(
    client: httpx.Client,
    posting_id: int | None,
    *,
    every_row: bool,
    dry_run: bool,
) -> None:
    resume = base_resume()
    spreadsheet_id = tracker_spreadsheet_id()
    config = load_config()
    settings = checklist_settings(config)
    default_scope = posting_id is None and not every_row
    index = _read_sheet(spreadsheet_id, required=default_scope)
    with closing(db.connect()) as conn:
        targets = db.refetch_targets(conn, posting_id=posting_id, every_row=every_row)
    if posting_id is not None and not targets:
        raise JsaError(f"posting {posting_id} does not exist")
    if default_scope:
        targets = [t for t in targets if _unapplied(index, t.job.id)]
    if not targets:
        print("No postings to refetch.")
        return
    flagged = []
    for target in targets:
        fixes = _reconcile(
            client,
            target,
            index,
            spreadsheet_id,
            config,
            resume,
            settings,
            dry_run=dry_run,
        )
        for fix in fixes:
            print(f"flagged: posting {target.job.id}: {fix}")
        if fixes:
            flagged.append(target.job.id)
    if flagged:
        raise JsaError(
            f"{len(flagged)} of {len(targets)} postings need a hand fix: "
            + ", ".join(map(str, flagged))
        )
