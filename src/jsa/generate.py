"""`jsa generate` (PRD 04 "Resume checklist"): re-check, build the packet, write the checklist, track the row."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import date, datetime
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
from jsa.packet import CHECKLIST, CHECKLIST_PDF, JOB_POSTING, ensure_head, packet_paths
from jsa.profile import (
    AgentSettings,
    Config,
    base_resume,
    checklist_settings,
    load_config,
    load_search_config,
    tracker_spreadsheet_id,
)
from jsa.resume import render_resume
from jsa.tracker import append_tracked
from jsa.verify import CLOSED_OUTCOMES, Verifier, recheck


def _job_description(job: db.PacketJob, directory: Path) -> str | None:
    """The row's JD, else a hand-filled `job_posting.md`; None when there is neither."""
    if job.jd_markdown is not None:
        return job.jd_markdown
    try:
        return (directory / JOB_POSTING).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def build_packet(
    job: db.PacketJob,
    config: Config,
    resume: Path,
    settings: AgentSettings,
    *,
    rewrite: bool,
) -> bool:
    """Complete the packet and its checklist; False when it must stay untracked for lack of a JD."""
    directory, copy = packet_paths(config, job)
    # PRD 04: `--id` (rewrite) refreshes `job_posting.md` from the row, as refetch relies on.
    ensure_head(directory, copy, job, resume, refresh_posting=rewrite)
    checklist = directory / CHECKLIST
    pdf = directory / CHECKLIST_PDF
    # XC-10: an interrupted run resumes after a checklist it already wrote, even one whose PDF failed.
    if checklist.exists() and not rewrite:
        if not pdf.exists():
            render_checklist_pdf(checklist, pdf)
        return True
    job_description = _job_description(job, directory)
    if not (job_description or "").strip():
        return False
    prompt = assemble_checklist_prompt(
        job.title, job.company, job_description, render_resume(copy)
    )
    text = run_checklist(prompt, settings)
    # A failed render must not leave the old checklist's PDF beside the new checklist.
    pdf.unlink(missing_ok=True)
    checklist.write_text(text + "\n", encoding="utf-8")
    render_checklist_pdf(checklist, pdf)
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


def _track(spreadsheet_id: str, today: date, posting_id: int) -> None:
    # A fresh connection: the server drops one left idle through the checklist runs.
    with closing(db.connect()) as conn:
        for entry in db.tracker_queue(conn, posting_id):
            append_tracked(conn, spreadsheet_id, today, entry)


def generate(client: httpx.Client, posting_id: int | None, *, dry_run: bool) -> None:
    resume = base_resume()
    spreadsheet_id = tracker_spreadsheet_id()
    config = load_config()
    settings = checklist_settings(config)
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
    today = datetime.now(load_search_config().tz).date()
    failures = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                build_packet,
                job,
                config,
                resume,
                settings,
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
                _track(spreadsheet_id, today, job.id)
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
