"""Application packet folders (PRD 04): the job description and the user's own resume and cover letter copies."""

import shutil
from pathlib import Path

from jsa import db
from jsa.naming import (
    cover_letter_file_stem,
    packet_dir_name,
    redline_file_name,
    resume_file_stem,
)
from jsa.profile import (
    COVER_LETTER_PREFIX,
    Config,
    PacketSources,
    load_config,
    packet_sources,
)

JOB_POSTING = "job_posting.md"
CHECKLIST = "resume_checklist.md"
CHECKLIST_PDF = "resume_checklist.pdf"
REDLINE_EDITS = "redline_edits.json"


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


def cover_letter_stem(config: Config, job: db.PacketJob) -> str:
    return cover_letter_file_stem(
        config.candidate_name, job.title_slug, job.normalized_company
    )


def packet_copies(
    config: Config, job: db.PacketJob, sources: PacketSources
) -> dict[Path, Path]:
    """Each file the packet holds a user-owned copy of, as copy -> profile source."""
    directory, resume_copy = packet_paths(config, job)
    copies = {resume_copy: sources.resume}
    if sources.cover_letter is not None:
        # The copy keeps the source's extension, whatever it is (`.docx`, `.pdf`, ...).
        extension = sources.cover_letter.name.removeprefix(COVER_LETTER_PREFIX)
        copies[directory / f"{cover_letter_stem(config, job)}{extension}"] = (
            sources.cover_letter
        )
    return copies


def redline_path(copy: Path) -> Path:
    """The ATS redline beside the resume copy it is made from."""
    return copy.with_name(redline_file_name(copy.stem))


def ensure_head(
    directory: Path,
    job: db.PacketJob,
    copies: dict[Path, Path],
    *,
    refresh_posting: bool = False,
) -> None:
    """Complete the folder, the job description, and the resume and cover letter copies.

    The copies and a hand-filled job description are never replaced. The row's description
    replaces an existing `job_posting.md` only when `refresh_posting` is set.
    """
    directory.mkdir(parents=True, exist_ok=True)
    posting = directory / JOB_POSTING
    if job.jd_markdown is not None and (refresh_posting or not posting.exists()):
        posting.write_text(job.jd_markdown, encoding="utf-8")
    # A copy is the user's working file, so an existing one is never replaced.
    for copy, source in copies.items():
        if not copy.exists():
            shutil.copyfile(source, copy)


def build_packets(posting_id: int | None, *, dry_run: bool) -> None:
    sources = packet_sources()
    config = load_config()
    conn = db.connect()
    queue = db.packet_queue(conn, posting_id)
    if not queue:
        print("No postings awaiting a packet.")
        return
    for job in queue:
        directory = packet_paths(config, job)[0]
        if directory.exists():
            print(f"skipped (folder exists): {directory}")
            continue
        print(f"{'would create' if dry_run else 'created'}: {directory}")
        if not dry_run:
            ensure_head(directory, job, packet_copies(config, job, sources))
