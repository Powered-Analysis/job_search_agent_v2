"""`jsa generate` (PRD 04 "Resume checklist", "ATS redline"): re-check, build the packet, write the checklist and the redline, track the row."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date
from pathlib import Path

import httpx

from jsa import db
from jsa.checklist import (
    assemble_checklist_prompt,
    render_checklist_pdf,
    run_checklist,
)
from jsa.config import generate_workers
from jsa.errors import JsaError, one_line
from jsa.packet import (
    CHECKLIST,
    CHECKLIST_PDF,
    JOB_POSTING,
    REDLINE_EDITS,
    ensure_head,
    packet_copies,
    packet_paths,
    redline_path,
)
from jsa.profile import (
    AgentSettings,
    Config,
    PacketAgents,
    PacketSources,
    load_config,
    packet_agents,
    packet_sources,
    tracker_spreadsheet_id,
)
from jsa.redline import redline_resume
from jsa.resume import render_resume
from jsa.tracker import append_tracked, today
from jsa.verify import CLOSED_OUTCOMES, Verifier, recheck


def _job_description(job: db.PacketJob, directory: Path) -> str | None:
    """The row's JD, else a hand-filled `job_posting.md`; None when there is neither."""
    if job.jd_markdown is not None:
        return job.jd_markdown
    try:
        return (directory / JOB_POSTING).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _build_checklist(
    job_description: str,
    copy: Path,
    directory: Path,
    job: db.PacketJob,
    settings: AgentSettings,
) -> None:
    prompt = assemble_checklist_prompt(
        job.title, job.company, job_description, render_resume(copy)
    )
    text = run_checklist(prompt, settings)
    pdf = directory / CHECKLIST_PDF
    # A failed render must not leave the old checklist's PDF beside the new checklist.
    pdf.unlink(missing_ok=True)
    (directory / CHECKLIST).write_text(text + "\n", encoding="utf-8")
    render_checklist_pdf(directory / CHECKLIST, pdf)


def _build_redline(
    job_description: str,
    job: db.PacketJob,
    directory: Path,
    copy: Path,
    settings: AgentSettings,
) -> None:
    result = redline_resume(
        copy,
        redline_path(copy),
        directory / REDLINE_EDITS,
        job_description,
        settings,
    )
    if result is None:
        print(
            f"warning: posting {job.id}: the resume copy has unresolved tracked changes, "
            "so it was not redlined; accept or reject them, then run "
            f"`jsa generate --id {job.id}`"
        )
    else:
        print(
            f"redline: posting {job.id}: {result.applied} applied, {result.dropped} dropped"
        )


def build_packet(
    job: db.PacketJob,
    config: Config,
    sources: PacketSources,
    agents: PacketAgents,
    *,
    rewrite: bool,
) -> bool:
    """Complete the packet, its checklist, and its redline; False when it must stay untracked for lack of a JD."""
    directory, copy = packet_paths(config, job)
    # PRD 04: `--id` (rewrite) refreshes `job_posting.md` from the row, as refetch relies on.
    ensure_head(
        directory, job, packet_copies(config, job, sources), refresh_posting=rewrite
    )
    checklist = directory / CHECKLIST
    pdf = directory / CHECKLIST_PDF
    redline = redline_path(copy)
    # XC-10: an interrupted run resumes after the steps it already finished, even a checklist whose PDF failed.
    need_checklist = rewrite or not checklist.exists()
    # PRD 04: a redline the user may be reviewing is never replaced by a refresh.
    need_redline = (
        not redline.exists() if rewrite else not (directory / REDLINE_EDITS).exists()
    )
    if not need_checklist and not need_redline:
        if not pdf.exists():
            render_checklist_pdf(checklist, pdf)
        return True
    job_description = _job_description(job, directory)
    if not (job_description or "").strip():
        return False
    if need_checklist:
        _build_checklist(job_description, copy, directory, job, agents.checklist)
    elif not pdf.exists():
        render_checklist_pdf(checklist, pdf)
    if need_redline:
        _build_redline(job_description, job, directory, copy, agents.redline)
    return True


def _still_open(
    conn: db.Connection, client: httpx.Client, queue: list[db.PacketJob]
) -> list[db.PacketJob]:
    outcomes = recheck(conn, Verifier(client), [(job.id, job.url) for job in queue])
    open_jobs = [job for job in queue if outcomes[job.id] not in CLOSED_OUTCOMES]
    closed = len(queue) - len(open_jobs)
    noun = "posting" if closed == 1 else "postings"
    print(f"{closed} Apply {noun} closed since they were decided.")
    return open_jobs


def track_posting(spreadsheet_id: str, added: date, posting_id: int) -> None:
    # A fresh connection: the server drops one left idle through the checklist runs.
    with closing(db.connect()) as conn:
        for entry in db.tracker_queue(conn, posting_id):
            append_tracked(conn, spreadsheet_id, added, entry)


def generate(client: httpx.Client, posting_id: int | None, *, dry_run: bool) -> None:
    sources = packet_sources()
    spreadsheet_id = tracker_spreadsheet_id()
    config = load_config()
    agents = packet_agents(config)
    workers = generate_workers()
    conn = db.connect()
    queue = db.packet_queue(conn, posting_id)
    if dry_run:
        for job in queue:
            print(f"would generate: {packet_paths(config, job)[0]}")
        if not queue:
            print("No postings awaiting a packet.")
        return
    # PRD 04: --id builds a closed posting anyway, so it skips the re-check.
    if queue and posting_id is None:
        queue = _still_open(conn, client, queue)
    if not queue:
        print("No postings awaiting a packet.")
        return
    added = today()
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                build_packet,
                job,
                config,
                sources,
                agents,
                rewrite=posting_id is not None,
            ): job
            for job in queue
        }
        # Appends happen here, one at a time, as each row finishes (PRD 04, the Step 4→5 seam).
        for future in as_completed(futures):
            job = futures[future]
            try:
                if not future.result():
                    print(
                        f"flagged: posting {job.id} has no job description; "
                        f"fill in {packet_paths(config, job)[0] / JOB_POSTING} and run "
                        f"`jsa generate --id {job.id}`"
                    )
                    continue
                track_posting(spreadsheet_id, added, job.id)
            except (JsaError, OSError) as error:
                print(f"failed: posting {job.id}: {one_line(error)}")
                failures.append(job.id)
                continue
            print(f"generated: {packet_paths(config, job)[0]}")
    if failures:
        raise JsaError(
            f"{len(failures)} of {len(queue)} postings failed: "
            + ", ".join(map(str, sorted(failures)))
        )
