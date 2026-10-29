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
    SEARCH_DIR,
    SHARED_FAMILY,
    SearchConfig,
    load_config,
    load_search_config,
    profile_dir,
    refine_settings,
    search_families,
)
from jsa.search_prompt import FRAGMENTS, assemble_search_prompt_for, fragment_source

REFINE_DIR = "refine"
RATIONALE = "rationale.md"
# What the proposal was built from, for `--accept` to compare (PRD 05): the search families, the hash
# of each live fragment, and the paths the proposal creates.
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


def found_by_text(found_by: Sequence[str | None]) -> str:
    """The search families that found a posting (PRD 05); "none" for a hand-added one."""
    names = sorted(name for name in found_by if name)
    if names:
        return ", ".join(names)
    return "unrecorded" if found_by else "none"


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
                    f"- Search families that found it: {found_by_text(row.found_by)}",
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


def render_search_prompt(family: str, config: SearchConfig) -> str:
    """A family's assembled search prompt under its name and the path each fragment resolved to."""
    search_dir = profile_dir() / SEARCH_DIR
    resolved = []
    for filename, _ in FRAGMENTS.values():
        source = fragment_source(family, filename)
        where = (
            source.relative_to(search_dir).as_posix()
            if source
            else "no file, so the fragment is off"
        )
        resolved.append(f"- {filename}: {where}")
    return "\n".join(
        [
            f"### Search family: {family}",
            "",
            "Each fragment came from:",
            *resolved,
            "",
            "<search_prompt>",
            assemble_search_prompt_for(config, family, WINDOW_NOTE),
            "</search_prompt>",
        ]
    )


def assemble_refine_prompt(
    ground_truth: str,
    history: str,
    search_config: SearchConfig,
    families: Sequence[str],
) -> str:
    slots = {
        "GROUND_TRUTH": Slot(ground_truth, "the decisions in scope"),
        "HISTORY": Slot(history, "the decided history"),
        "SEARCH_PROMPTS": Slot(
            "\n\n".join(render_search_prompt(f, search_config) for f in families),
            "the assembled search prompts",
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


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def refine_dir() -> Path:
    return profile_dir() / REFINE_DIR


def proposal_pending(directory: Path) -> bool:
    return directory.is_dir() and any(directory.iterdir())


def _live_file(path: str) -> Path:
    return profile_dir() / SEARCH_DIR / path


def _read_live(path: str) -> str | None:
    file = _live_file(path)
    return file.read_text(encoding="utf-8") if file.is_file() else None


def _folders(families: Sequence[str]) -> list[str]:
    return [SHARED_FAMILY, *families]


def refinable_paths(families: Sequence[str]) -> list[str]:
    """The only paths the refiner may change, relative to `profile/search/` (PRD 05, Write set)."""
    return [f"{folder}/{name}" for folder in _folders(families) for name in REFINABLE]


def scratch_layout(
    families: Sequence[str], live: Mapping[str, str | None]
) -> tuple[list[str], dict[str, str]]:
    """The scratch directory's folders, and the text of each refinable fragment that exists."""
    files = {path: text for path, text in live.items() if text is not None}
    return _folders(families), files


def proposal_views(
    live: Mapping[str, str | None], proposed: Mapping[str, str]
) -> dict[str, str]:
    """A conflict view for each fragment the refiner changed or created; a created one has an empty current side."""
    views = {}
    for path, text in proposed.items():
        if (view := conflict_view(live[path] or "", text)) is not None:
            views[path] = view
    return views


def _write_proposal(
    directory: Path,
    rationale: str,
    views: Mapping[str, str],
    live: Mapping[str, str | None],
    families: Sequence[str],
) -> None:
    """Built beside the target and renamed into place, so a crash never leaves half a proposal."""
    with tempfile.TemporaryDirectory(dir=directory.parent) as staging:
        staged = Path(staging)
        (staged / RATIONALE).write_text(rationale + "\n", encoding="utf-8")
        for path, view in views.items():
            (staged / path).parent.mkdir(exist_ok=True)
            (staged / path).write_text(view, encoding="utf-8")
        record = {
            "families": list(families),
            "hashes": {
                path: _hash(text) for path, text in live.items() if text is not None
            },
            "created": [path for path in views if live[path] is None],
        }
        (staged / BUILT_FROM).write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8"
        )
        staged.rename(directory)


def _run_in_scratch(
    prompt: str, families: Sequence[str], live: Mapping[str, str | None]
) -> tuple[str, dict[str, str]]:
    """The agent's rationale and its copy of each fragment; the scratch directory is discarded."""
    settings = refine_settings(load_config())
    folders, files = scratch_layout(families, live)
    with tempfile.TemporaryDirectory(prefix="jsa-refine-") as scratch:
        scratch_dir = Path(scratch)
        for folder in folders:
            (scratch_dir / folder).mkdir()
        for path, text in files.items():
            (scratch_dir / path).write_text(text, encoding="utf-8")
        result = agent_loop.run_agent(
            prompt,
            settings,
            tools=TOOLS,
            max_turns=MAX_TURNS,
            # Edits inside the working directory are accepted; anything outside it is not.
            permission_mode="acceptEdits",
            cwd=scratch_dir,
        )
        # Only the refinable paths are read back, so a file the agent made elsewhere goes nowhere.
        proposed = {
            path: (scratch_dir / path).read_text(encoding="utf-8")
            for path in live
            if (scratch_dir / path).is_file()
        }
    return result.text, proposed


def _excluded_note(count: int) -> str:
    return f"Excluded from refinement: {count} decided after the cutoff."


def refine(*, dry_run: bool) -> None:
    directory = refine_dir()
    if not dry_run and proposal_pending(directory):
        raise JsaError(
            f"{directory} holds a pending proposal. Accept or reject it before refining again."
        )
    with closing(db.connect()) as conn:
        cutoff = db.refinement_cutoff(conn)
        decided = db.decided_postings(conn)
        excluded = db.excluded_decided_count(conn, cutoff)
    # Stored timestamps share one format, so comparing them as text orders them (PRD 02).
    scope = [row for row in decided if cutoff is None or row.decided_at > cutoff]
    if not scope:
        print("Nothing new to refine.")
        if dry_run and excluded:
            print(_excluded_note(excluded))
        return
    if dry_run:
        since = (
            "since the first decision" if cutoff is None else f"decided after {cutoff}"
        )
        print(f"{len(scope)} decided postings in scope ({since}):")
        for row in scope:
            print(history_line(row))
        if excluded:
            print(_excluded_note(excluded))
        return
    search_config = load_search_config()
    families = search_families()
    if not families:
        raise JsaError(f"{profile_dir() / SEARCH_DIR} holds no search family.")
    prompt = assemble_refine_prompt(
        render_ground_truth(scope), render_history(decided), search_config, families
    )
    live = {path: _read_live(path) for path in refinable_paths(families)}
    print(f"Refining from {len(scope)} decided postings.")
    rationale, proposed = _run_in_scratch(prompt, families, live)
    views = proposal_views(live, proposed)
    if views:
        _write_proposal(directory, rationale, views, live, families)
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


def _read_record(directory: Path) -> dict:
    try:
        return json.loads((directory / BUILT_FROM).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JsaError(
            f"{directory / BUILT_FROM} is missing, so the proposal can't be checked "
            "against your live fragments. Reject it and run `jsa refine` again."
        ) from None


def _check_families(built_for: Sequence[str]) -> None:
    now = search_families()
    if now != list(built_for):
        added = [folder for folder in now if folder not in built_for]
        removed = [folder for folder in built_for if folder not in now]
        changes = [
            *(f"{SEARCH_DIR}/{folder} was added" for folder in added),
            *(f"{SEARCH_DIR}/{folder} was removed" for folder in removed),
        ]
        raise JsaError(
            f"The search families changed since this proposal was built: {'; '.join(changes)}. "
            "Reject the proposal and run `jsa refine` again."
        )


def _resolved(directory: Path, families: Sequence[str]) -> dict[str, str]:
    """The user's text for each fragment the proposal holds; stray files are not fragments."""
    return {
        path: (directory / path).read_text(encoding="utf-8")
        for path in refinable_paths(families)
        if (directory / path).is_file()
    }


def _check_no_markers(resolved: Mapping[str, str]) -> None:
    left = [
        f"{path}, line {number}"
        for path, text in resolved.items()
        for number, line in enumerate(text.splitlines(), start=1)
        if _MARKER_LINE.fullmatch(line.rstrip())
    ]
    if left:
        raise JsaError(
            "The proposal still has conflict markers to resolve: " + "; ".join(left)
        )


def _check_unedited(record: Mapping) -> None:
    edited = [
        path
        for path, hashed in record["hashes"].items()
        if (text := _read_live(path)) is None or _hash(text) != hashed
    ]
    if edited:
        raise JsaError(
            f"{', '.join(edited)} changed since this proposal was built, "
            "so accepting would overwrite your edit. "
            "Reject the proposal and run `jsa refine` again."
        )
    exist = [path for path in record["created"] if _live_file(path).exists()]
    if exist:
        raise JsaError(
            f"{', '.join(exist)} now exists, but this proposal creates it, "
            "so accepting would overwrite your file. "
            "Reject the proposal and run `jsa refine` again."
        )


def _check_assembles(directory: Path, families: Sequence[str]) -> None:
    config = load_search_config()
    for family in families:
        try:
            assemble_search_prompt_for(
                config,
                family,
                WINDOW_NOTE,
                directory / family,
                REFINABLE,
                directory / SHARED_FAMILY,
            )
        except JsaError as error:
            raise JsaError(
                f"The resolved proposal would break the search prompt of {family}. {error}"
            ) from error


def accept() -> None:
    directory = refine_dir()
    if not proposal_pending(directory):
        print("No proposal is pending.")
        return
    record = _read_record(directory)
    families = record["families"]
    _check_families(families)
    resolved = _resolved(directory, families)
    _check_no_markers(resolved)
    _check_unedited(record)
    _check_assembles(directory, families)
    for path, text in resolved.items():
        live = _read_live(path)
        diff = list(
            difflib.unified_diff(
                (live or "").splitlines(),
                text.splitlines(),
                fromfile=f"{path} ({'live' if live is not None else 'new'})",
                tofile=f"{path} (accepted)",
                lineterm="",
            )
        )
        unchanged = (
            f"{path}: no change" if live is not None else f"{path}: created empty"
        )
        print("\n".join(diff) if diff else unchanged)
        _live_file(path).parent.mkdir(exist_ok=True)
        _live_file(path).write_text(text, encoding="utf-8")
    shutil.rmtree(directory)
    print("Proposal accepted. Run `jsa deploy` to ship the change to the cloud.")


def reject() -> None:
    directory = refine_dir()
    if not proposal_pending(directory):
        print("No proposal is pending.")
        return
    shutil.rmtree(directory)
    print("Proposal rejected. Your search fragments are unchanged.")
