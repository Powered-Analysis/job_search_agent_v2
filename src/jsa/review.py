"""The review loop (PRD 03): no model call, one decision at a time, each committed on submit."""

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

import httpx

from jsa import db, prompts
from jsa.errors import JsaError
from jsa.health import search_health
from jsa.profile import role_family_names
from jsa.verify import CLOSED_OUTCOMES, Verifier, recheck

FEEDBACK_LABEL = "Feedback (Enter to skip, :a/:s to change the decision)"
DECISION_KEYS = {"a": "Apply", "s": "Skip"}
COMMANDS = {
    ":a": "Apply",
    ":apply": "Apply",
    ":s": "Skip",
    ":skip": "Skip",
    ":b": "back",
    ":back": "back",
}


@dataclass
class Entry:
    id: int
    company: str
    title: str
    location: str | None
    url: str
    decision: str | None = None
    feedback: str | None = None
    role_family: str | None = None
    rechecked: bool = True


@dataclass(frozen=True)
class Feedback:
    action: Literal["save", "back"]
    decision: str
    feedback: str | None


def parse_feedback(decision: str, text: str) -> Feedback:
    """Read a feedback-prompt line: an inline `:a`/`:s` flips the decision, `:b` discards."""
    command, _, rest = text.strip().partition(" ")
    target = COMMANDS.get(command.lower())
    if target == "back":
        return Feedback("back", decision, None)
    if target:
        decision, text = target, rest
    return Feedback("save", decision, text.strip() or None)


class FamilyChoice(NamedTuple):
    """The answer to the role-family prompt: `b` went back, otherwise `name` (None when none fits)."""

    back: bool
    name: str | None


def parse_role_family(
    key: str, names: Sequence[str], current: str | None
) -> str | None:
    """The role family a valid non-back key names: a number, `n` for none, or Enter for `current`."""
    if key == "":
        return current
    if key == "n":
        return None
    return names[int(key) - 1]


def ask_role_family(
    names: Sequence[str], current: str | None, *, allow_back: bool = True
) -> FamilyChoice:
    """Pick a role family from the numbered list (PRD 03); with only one in the profile nobody is asked."""
    if len(names) == 1:
        return FamilyChoice(False, names[0])
    print("Role families:")
    for number, name in enumerate(names, 1):
        print(f"  {number}. {name}")
    print("  n. none fits")
    keys = [*(str(number) for number in range(1, len(names) + 1)), "n"]
    if allow_back:
        keys.append("b")
    label = "Role family" + (f" (Enter keeps {current})" if current else "")
    key = prompts.choose(label, keys, allow_enter=current is not None)
    if key == "b":
        return FamilyChoice(True, None)
    return FamilyChoice(False, parse_role_family(key, names, current))


def _open_in_chrome(url: str) -> None:
    try:
        subprocess.run(
            ["open", "-a", "Google Chrome", url],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        # The URL is printed too, so a missing browser must not stop review.
        pass


def _show(entry: Entry) -> None:
    amending = " (amending)" if entry.decision else ""
    unchecked = "" if entry.rechecked else " (not re-checked)"
    print(f"\n{entry.company} — {entry.title}{amending}{unchecked}")
    print(f"Location: {entry.location or 'unknown'}")
    print(entry.url)


def _decide(entry: Entry) -> str:
    """Prompt for a decision; returns 'Apply', 'Skip', 'back' or 'quit'."""
    keys = [*DECISION_KEYS, "b", "q"]
    if entry.decision:
        key = prompts.choose(
            f"Decision (Enter keeps {entry.decision})", keys, allow_enter=True
        )
    else:
        key = prompts.choose("Decision", keys)
    if key == "":
        return entry.decision
    return DECISION_KEYS.get(key) or {"b": "back", "q": "quit"}[key]


def _record(
    conn: db.Connection, entry: Entry, decision: str, families: Sequence[str]
) -> bool:
    """Ask the role family and feedback, then commit all three together; False when the user backed out to the decision prompt."""
    choice = FamilyChoice(False, None)
    if decision == "Apply":
        choice = ask_role_family(families, entry.role_family)
    if choice.back:
        return False
    answer = prompts.ask(FEEDBACK_LABEL, entry.feedback or "")
    parsed = parse_feedback(decision, answer)
    if parsed.action == "back":
        return False
    if parsed.decision == "Apply" and decision != "Apply":
        # A `:a` flip: the role family is asked before anything is written.
        choice = ask_role_family(families, entry.role_family)
        if choice.back:
            return False
    family = choice.name if parsed.decision == "Apply" else None
    db.record_decision(conn, entry.url, parsed.decision, parsed.feedback, family)
    entry.decision, entry.feedback, entry.role_family = (
        parsed.decision,
        parsed.feedback,
        family,
    )
    return True


def _review_entry(
    conn: db.Connection, entry: Entry, families: Sequence[str], *, first: bool
) -> str:
    """Work one posting to a commit; returns 'next', 'back' or 'quit'."""
    while True:
        decision = _decide(entry)
        if decision == "back" and first:
            print("This is the first posting.")
        elif decision in ("back", "quit"):
            return decision
        elif _record(conn, entry, decision, families):
            return "next"


def _work(conn: db.Connection, entries: list[Entry], families: Sequence[str]) -> None:
    index = 0
    while index < len(entries):
        entry = entries[index]
        _show(entry)
        _open_in_chrome(entry.url)
        outcome = _review_entry(conn, entry, families, first=index == 0)
        if outcome == "quit":
            return
        if outcome == "back":
            index -= 1
            continue
        index += 1


def _recheck_backlog(
    conn: db.Connection, client: httpx.Client, backlog: list[tuple]
) -> list[Entry]:
    """Mark closed postings and drop them from the backlog; the rest are shown (PRD 03)."""
    outcomes = recheck(conn, Verifier(client), [(row[0], row[4]) for row in backlog])
    open_rows = [row for row in backlog if outcomes[row[0]] not in CLOSED_OUTCOMES]
    closed = len(backlog) - len(open_rows)
    print(f"{closed} {'posting' if closed == 1 else 'postings'} closed while waiting.")
    return [
        Entry(*row, rechecked=outcomes[row[0]] != "unverifiable") for row in open_rows
    ]


def _abortable(work: Callable[[], None]) -> None:
    try:
        work()
    except prompts.PromptAborted:
        # Every committed decision already survives; the interrupted one is untouched.
        print("\nQuit.")


def review(client: httpx.Client) -> None:
    families = role_family_names()
    conn = db.connect()
    for line in search_health(conn):
        print(line)
    # Captured once so stepping back is stable (PRD 03).
    backlog = db.review_backlog(conn)
    entries = _recheck_backlog(conn, client, backlog) if backlog else []
    if not entries:
        print("No postings awaiting review. 🎉")
        return
    _abortable(lambda: _work(conn, entries, families))


def revise(*, posting_id: int | None = None, last: bool = False) -> None:
    """Reopen one decided posting outside a review session (PRD 03)."""
    families = role_family_names()
    conn = db.connect()
    row = db.last_decided_posting(conn) if last else db.posting_by_id(conn, posting_id)
    if row is None:
        raise JsaError(
            "no decided postings to revise."
            if last
            else f"no posting with id {posting_id}."
        )
    entry = Entry(*row)
    if not entry.decision:
        raise JsaError(
            f"posting {entry.id} has no decision to revise; decide it with `jsa review`."
        )
    _show(entry)
    _open_in_chrome(entry.url)
    # first=True: a step back has nowhere to go, so it re-asks, and `q` leaves the row as it was.
    _abortable(lambda: _review_entry(conn, entry, families, first=True))
