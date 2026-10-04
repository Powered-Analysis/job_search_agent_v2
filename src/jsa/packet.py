"""Application packet folders (PRD 04): the job description and the user's own resume copy."""

import shutil

from jsa import db
from jsa.naming import packet_dir_name, resume_file_stem
from jsa.profile import base_resume, load_config


def build_packets(posting_id: int | None, *, dry_run: bool) -> None:
    resume = base_resume()
    config = load_config()
    conn = db.connect()
    queue = db.packet_queue(conn, posting_id)
    if not queue:
        print("No postings awaiting a packet.")
        return
    for pid, company, slug, jd_markdown, shares_name in queue:
        directory = config.packets_dir / packet_dir_name(
            company, slug, pid, shares_name=shares_name
        )
        if directory.exists():
            print(f"skipped (folder exists): {directory}")
            continue
        print(f"{'would create' if dry_run else 'created'}: {directory}")
        if dry_run:
            continue
        directory.mkdir(parents=True)
        if jd_markdown is not None:
            (directory / "job_posting.md").write_text(jd_markdown, encoding="utf-8")
        copy = (
            directory / f"{resume_file_stem(config.candidate_name, slug, company)}.docx"
        )
        # The copy is the user's working file, so an existing one is never replaced.
        if not copy.exists():
            shutil.copyfile(resume, copy)
