"""The review loop (PRD 03): no model call, one decision at a time, each committed on submit."""

import subprocess
from dataclasses import dataclass
from typing import Literal

from jsa import db, prompts

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
    print(f"\n{entry.company} — {entry.title}{amending}")
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


def _record(conn: db.Connection, entry: Entry, decision: str) -> bool:
    """Ask for feedback and commit; False when the user backed out to the decision prompt."""
    answer = prompts.ask(FEEDBACK_LABEL, entry.feedback or "")
    parsed = parse_feedback(decision, answer)
    if parsed.action == "back":
        return False
    db.record_decision(conn, entry.url, parsed.decision, parsed.feedback)
    entry.decision, entry.feedback = parsed.decision, parsed.feedback
    return True


def _review_entry(conn: db.Connection, entry: Entry, *, first: bool) -> str:
    """Work one posting to a commit; returns 'next', 'back' or 'quit'."""
    while True:
        decision = _decide(entry)
        if decision == "back" and first:
            print("This is the first posting.")
        elif decision in ("back", "quit"):
            return decision
        elif _record(conn, entry, decision):
            return "next"


def _work(conn: db.Connection, entries: list[Entry]) -> None:
    index = 0
    final_pass_offered = False
    while index < len(entries):
        entry = entries[index]
        _show(entry)
        _open_in_chrome(entry.url)
        outcome = _review_entry(conn, entry, first=index == 0)
        if outcome == "quit":
            return
        if outcome == "back":
            index -= 1
            final_pass_offered = False
            continue
        index += 1
        if index == len(entries) and not final_pass_offered:
            # One more look at the last entry before the session ends (PRD 03).
            final_pass_offered = True
            index -= 1


def review() -> None:
    conn = db.connect()
    # Captured once so stepping back is stable (PRD 03).
    entries = [Entry(*row) for row in db.review_backlog(conn)]
    if not entries:
        print("No postings awaiting review. 🎉")
        return
    try:
        _work(conn, entries)
    except prompts.PromptAborted:
        # Every committed decision already survives; the interrupted one is untouched.
        print("\nQuit.")
