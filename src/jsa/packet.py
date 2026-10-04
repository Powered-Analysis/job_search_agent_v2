"""Application packet folders (PRD 04): the job description and the user's own resume copy."""

import shutil
from pathlib import Path

from jsa import db
from jsa.naming import packet_dir_name, resume_file_stem
from jsa.profile import Config, base_resume, load_config

JOB_POSTING = "job_posting.md"
CHECKLIST = "resume_checklist.md"


def packet_paths(config: Config, job: db.PacketJob) -> tuple[Path, Path]:
    """The packet's folder and the resume copy inside it."""
    directory = config.packets_dir / packet_dir_name(
        job.normalized_company, job.title_slug, job.id, shares_name=job.shares_name
    )
    copy = (
        directory
        / f"{resume_file_stem(config.candidate_name, job.title_slug, job.normalized_company)}.docx"
    )
    return directory, copy


def ensure_head(
    directory: Path,
    copy: Path,
    job: db.PacketJob,
    resume: Path,
    *,
    refresh_posting: bool = False,
) -> None:
    """Complete the folder, the job description, and the resume copy.

    The resume copy and a hand-filled job description are never replaced. The row's description
    replaces an existing `job_posting.md` only when `refresh_posting` is set.
    """
    directory.mkdir(parents=True, exist_ok=True)
    posting = directory / JOB_POSTING
    if job.jd_markdown is not None and (refresh_posting or not posting.exists()):
        posting.write_text(job.jd_markdown, encoding="utf-8")
    # The copy is the user's working file, so an existing one is never replaced.
    if not copy.exists():
        shutil.copyfile(resume, copy)


def build_packets(posting_id: int | None, *, dry_run: bool) -> None:
    resume = base_resume()
    config = load_config()
    conn = db.connect()
    queue = db.packet_queue(conn, posting_id)
    if not queue:
        print("No postings awaiting a packet.")
        return
    for job in queue:
        directory, copy = packet_paths(config, job)
        if directory.exists():
            print(f"skipped (folder exists): {directory}")
            continue
        print(f"{'would create' if dry_run else 'created'}: {directory}")
        if not dry_run:
            ensure_head(directory, copy, job, resume)
