import builtins
import io
import sys

import pytest

from jsa.errors import JsaError
from jsa.prompts import PromptAborted, ask, choose


def stdin_lines(monkeypatch, text):
    stream = io.StringIO(text)
    monkeypatch.setattr(sys, "stdin", stream)
    return stream


def test_without_a_tty_ask_reads_a_plain_line(monkeypatch):
    stdin_lines(monkeypatch, "Globex Corporation\n")
    assert ask("Company", "Globex") == "Globex Corporation"


def test_without_a_tty_an_empty_line_accepts_the_prefilled_value(monkeypatch):
    stdin_lines(monkeypatch, "\n")
    assert ask("Company", "Acme Widgets") == "Acme Widgets"


def test_without_a_tty_an_empty_line_with_no_default_is_empty(monkeypatch):
    stdin_lines(monkeypatch, "\n")
    assert ask("Company") == ""


def test_end_of_input_aborts_the_prompt(monkeypatch):
    stdin_lines(monkeypatch, "")
    with pytest.raises(PromptAborted):
        ask("Company", "Acme")


def test_ctrl_c_aborts_the_prompt(monkeypatch):
    stdin_lines(monkeypatch, "")

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    with pytest.raises(PromptAborted):
        ask("Company", "Acme")


def test_an_abort_is_a_reportable_app_error():
    assert issubclass(PromptAborted, JsaError)


def test_choose_returns_a_valid_key(monkeypatch):
    stdin_lines(monkeypatch, "a\n")
    assert choose("Decision", ["a", "s"]) == "a"


def test_choose_reprompts_until_the_input_matches_a_valid_key(monkeypatch):
    stream = stdin_lines(monkeypatch, "x\n\nzz\ns\nleft over\n")
    assert choose("Decision", ["a", "s"]) == "s"
    assert stream.read() == "left over\n"


def test_choose_aborts_instead_of_looping_when_input_ends(monkeypatch):
    stdin_lines(monkeypatch, "nope\n")
    with pytest.raises(PromptAborted):
        choose("Decision", ["a", "s"])
