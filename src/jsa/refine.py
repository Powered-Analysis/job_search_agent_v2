"""`jsa refine` (PRD 05): decisions since the last run become a conflict-marked proposal."""

import difflib
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import closing
from pathlib import Path

from jsa import agent_loop, db
from jsa.assemble import Slot, app_template, assemble
from jsa.errors import JsaError
from jsa.profile import (
    SearchConfig,
    load_config,
    load_search_config,
    profile_dir,
    refine_settings,
)
from jsa.search_prompt import (
    FRAGMENTS,
    assemble_search_prompt_for,
    fragment_path,
    read_fragment,
)

REFINE_DIR = "refine"
RATIONALE = "rationale.md"
# The hash of each live fragment the proposal was built from, for `--accept` to compare (PRD 05).
BUILT_FROM = ".built_from.json"
# Facts about the person are not learnable from decisions, so the candidate fragment is out.
REFINABLE = [name for slot, (name, _) in FRAGMENTS.items() if slot != "CANDIDATE"]
# Neither tool reaches outside the scratch directory, and the ground truth is untrusted text.
TOOLS = ["Read", "Edit"]
MAX_TURNS = 80
WINDOW_NOTE = (
    "a window that varies with each search; it is filled in when the search runs "
    "and is not shown here"
)
CONFLICT_CURRENT = "<<<<<<< current"
CONFLICT_SEPARATOR = "======="
CONFLICT_PROPOSED = ">>>>>>> proposed"
# Any git-style marker the user left behind, even one they edited a little.
_MARKER_LINE = re.compile(r"(<{7}|>{7})( .*)?|={7}")


def _field(value: str | None, empty: str = "(not recorded)") -> str:
    return value if value else empty


def history_line(row: db.DecidedPosting) -> str:
    return f"{row.id} | {row.decision} | {_field(row.company)} | {_field(row.title)}"


def render_history(rows: Sequence[db.DecidedPosting]) -> str:
    """One line per decided posting, with no description and no feedback, under the counts."""
    applied = sum(row.decision == "Apply" for row in rows)
    counts = f"Apply: {applied}, Skip: {len(rows) - applied}"
    lines = ["id | decision | company | title", *map(history_line, rows)]
    return "\n".join([counts, "", *lines])


def render_ground_truth(rows: Sequence[db.DecidedPosting]) -> str:
    """Every row in full. The description is fenced because it is untrusted employer text."""
    blocks = []
    for row in rows:
        blocks.append(
            "\n".join(
                [
                    f"## Posting {row.id}: {_field(row.company)} — {_field(row.title)}",
                    f"- Decision: {row.decision}",
                    f"- Decided at: {row.decided_at}",
                    f"- Fit feedback: {_field(row.fit_feedback, '(none)')}",
                    f"- Search agent: {_field(row.search_agent)}",
                    f"- URL: {_field(row.url)}",
                    f"- Location: {_field(row.location)}",
                    f"- Date posted: {_field(row.date_posted)}",
                    "",
                    "<job_description>",
                    _field(row.jd_markdown, "(no job description captured)"),
                    "</job_description>",
                ]
            )
        )
    return "\n\n".join(blocks)


def assemble_refine_prompt(
    ground_truth: str, history: str, search_config: SearchConfig
) -> str:
    slots = {
        "GROUND_TRUTH": Slot(ground_truth, "the decisions in scope"),
        "HISTORY": Slot(history, "the decided history"),
        "SEARCH_PROMPT": Slot(
            assemble_search_prompt_for(search_config, WINDOW_NOTE),
            "the assembled search prompt",
        ),
    }
    return assemble(app_template("refine.md"), slots)


def conflict_view(live: str, proposed: str) -> str | None:
    """The live text with each changed region as a conflict block; None when nothing changed."""
    current, wanted = live.splitlines(), proposed.splitlines()
    if current == wanted:
        return None
    matcher = difflib.SequenceMatcher(None, current, wanted, autojunk=False)
    lines: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            lines.extend(current[i1:i2])
            continue
        lines.extend(
            [
                CONFLICT_CURRENT,
                *current[i1:i2],
                CONFLICT_SEPARATOR,
                *wanted[j1:j2],
                CONFLICT_PROPOSED,
            ]
        )
    return "\n".join(lines) + "\n"


def _hash(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def refine_dir() -> Path:
    return profile_dir() / REFINE_DIR


def proposal_pending(directory: Path) -> bool:
    return directory.is_dir() and any(directory.iterdir())


def _write_proposal(
    directory: Path, rationale: str, views: dict[str, str], live: dict[str, str | None]
) -> None:
    """Built beside the target and renamed into place, so a crash never leaves half a proposal."""
    with tempfile.TemporaryDirectory(dir=directory.parent) as staging:
        staged = Path(staging)
        (staged / RATIONALE).write_text(rationale + "\n", encoding="utf-8")
        for name, view in views.items():
            (staged / name).write_text(view, encoding="utf-8")
        (staged / BUILT_FROM).write_text(
            json.dumps({name: _hash(text) for name, text in live.items()}, indent=2)
            + "\n",
            encoding="utf-8",
        )
        staged.rename(directory)


def _run_in_scratch(
    prompt: str, live: dict[str, str | None]
) -> tuple[str, dict[str, str]]:
    """The agent's rationale and its copy of each fragment; the scratch directory is discarded."""
    settings = refine_settings(load_config())
    with tempfile.TemporaryDirectory(prefix="jsa-refine-") as scratch:
        scratch_dir = Path(scratch)
        for name, text in live.items():
            (scratch_dir / name).write_text(text or "", encoding="utf-8")
        result = agent_loop.run_agent(
            prompt,
            settings,
            tools=TOOLS,
            max_turns=MAX_TURNS,
            # Edits inside the working directory are accepted; anything outside it is not.
            permission_mode="acceptEdits",
            cwd=scratch_dir,
        )
        proposed = {
            name: (scratch_dir / name).read_text(encoding="utf-8") for name in live
        }
    return result.text, proposed


def refine(*, dry_run: bool) -> None:
    directory = refine_dir()
    if not dry_run and proposal_pending(directory):
        raise JsaError(
            f"{directory} holds a pending proposal. Accept or reject it before refining again."
        )
    with closing(db.connect()) as conn:
        cutoff = db.refinement_cutoff(conn)
        decided = db.decided_postings(conn)
    # Stored timestamps share one format, so comparing them as text orders them (PRD 02).
    scope = [row for row in decided if cutoff is None or row.decided_at > cutoff]
    if not scope:
        print("Nothing new to refine.")
        return
    if dry_run:
        since = (
            "since the first decision" if cutoff is None else f"decided after {cutoff}"
        )
        print(f"{len(scope)} decided postings in scope ({since}):")
        for row in scope:
            print(history_line(row))
        return
    prompt = assemble_refine_prompt(
        render_ground_truth(scope), render_history(decided), load_search_config()
    )
    live = {name: read_fragment(name) for name in REFINABLE}
    print(f"Refining from {len(scope)} decided postings.")
    rationale, proposed = _run_in_scratch(prompt, live)
    views = {
        name: view
        for name, text in proposed.items()
        if (view := conflict_view(live[name] or "", text)) is not None
    }
    if views:
        _write_proposal(directory, rationale, views, live)
    # PRD 05: the cutoff is what the run saw; a fresh connection, as the server drops idle ones.
    with closing(db.connect()) as conn:
        db.record_refinement_run(
            conn,
            cutoff=max(row.decided_at for row in scope),
            considered=len(scope),
            changed=len(views),
        )
    if views:
        print(f"Proposal written to {directory}")
    else:
        print("The refiner proposed no changes.")


def _resolved(directory: Path) -> dict[str, str]:
    """The user's text for each fragment the proposal changed; stray files are not fragments."""
    return {
        name: (directory / name).read_text(encoding="utf-8")
        for name in REFINABLE
        if (directory / name).is_file()
    }


def _check_resolved(resolved: Mapping[str, str], directory: Path) -> None:
    left = [
        f"{name}, line {number}"
        for name, text in resolved.items()
        for number, line in enumerate(text.splitlines(), start=1)
        if _MARKER_LINE.fullmatch(line.rstrip())
    ]
    if left:
        raise JsaError(
            "The proposal still has conflict markers to resolve: " + "; ".join(left)
        )
    try:
        assemble_search_prompt_for(
            load_search_config(),
            WINDOW_NOTE,
            resolved,
            directory,
        )
    except JsaError as error:
        raise JsaError(
            f"The resolved proposal would break the search prompt. {error}"
        ) from error


def _check_unedited(directory: Path) -> None:
    try:
        built_from = json.loads((directory / BUILT_FROM).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JsaError(
            f"{directory / BUILT_FROM} is missing, so the proposal can't be checked "
            "against your live fragments. Reject it and run `jsa refine` again."
        ) from None
    edited = [
        name
        for name, hashed in built_from.items()
        if _hash(read_fragment(name)) != hashed
    ]
    if edited:
        raise JsaError(
            f"{', '.join(edited)} changed since this proposal was built, "
            "so accepting would overwrite your edit. "
            "Reject the proposal and run `jsa refine` again."
        )


def accept() -> None:
    directory = refine_dir()
    if not proposal_pending(directory):
        print("No proposal is pending.")
        return
    resolved = _resolved(directory)
    _check_resolved(resolved, directory)
    _check_unedited(directory)
    for name, text in resolved.items():
        live = read_fragment(name) or ""
        diff = list(
            difflib.unified_diff(
                live.splitlines(),
                text.splitlines(),
                fromfile=f"{name} (live)",
                tofile=f"{name} (accepted)",
                lineterm="",
            )
        )
        print("\n".join(diff) if diff else f"{name}: no change")
        fragment_path(name).write_text(text, encoding="utf-8")
    shutil.rmtree(directory)
    print("Proposal accepted. Run `jsa deploy` to ship the change to the cloud.")


def reject() -> None:
    directory = refine_dir()
    if not proposal_pending(directory):
        print("No proposal is pending.")
        return
    shutil.rmtree(directory)
    print("Proposal rejected. Your search fragments are unchanged.")
