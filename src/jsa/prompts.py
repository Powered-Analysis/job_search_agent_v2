"""Line-editing prompts (PRD 03): editable pre-filled buffer, `$EDITOR` on Ctrl-X Ctrl-E."""

import readline  # noqa: F401  # PRD 03: gives plain `input()` line editing off a TTY
import sys
from collections.abc import Sequence

from prompt_toolkit import PromptSession

from jsa.errors import JsaError


class PromptAborted(JsaError):
    """The user pressed Ctrl-C or Ctrl-D at a prompt."""


def ask(label: str, default: str = "") -> str:
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return PromptSession().prompt(
                f"{label}: ", default=default, enable_open_in_editor=True
            )
        suffix = f" [{default}]" if default else ""
        return input(f"{label}{suffix}: ") or default
    except (KeyboardInterrupt, EOFError) as error:
        raise PromptAborted("aborted") from error


def choose(label: str, keys: Sequence[str], *, allow_enter: bool = False) -> str:
    """Re-prompt until a valid key; with `allow_enter`, a bare Enter returns ''."""
    valid = {key.lower() for key in keys}
    if allow_enter:
        valid.add("")
    while True:
        answer = ask(f"{label} [{'/'.join(keys)}]").strip().lower()
        if answer in valid:
            return answer
