"""XC-13: the pure `{{SLOT}}` fill."""

import pytest

from jsa.assemble import OPTIONAL_PLACEHOLDER, Slot, assemble
from jsa.errors import JsaError


def test_every_marker_is_filled_from_its_slot():
    result = assemble(
        "Who: {{WHO}}\nWhat: {{WHAT}}",
        {"WHO": Slot("a chef", "who.md"), "WHAT": Slot("cooking", "what.md")},
    )
    assert result == "Who: a chef\nWhat: cooking"


def test_a_marker_used_twice_is_filled_both_times():
    result = assemble("{{X}} and {{X}}", {"X": Slot("value", "x.md")})
    assert result == "value and value"


@pytest.mark.parametrize(
    "text", [None, "", "   \n\t "], ids=["missing", "empty", "blank"]
)
def test_a_missing_or_empty_required_slot_raises_naming_its_source(text):
    with pytest.raises(JsaError) as raised:
        assemble(
            "{{CANDIDATE}}", {"CANDIDATE": Slot(text, "profile/search/candidate.md")}
        )
    assert "profile/search/candidate.md" in str(raised.value)


@pytest.mark.parametrize("text", [None, "", "  \n "], ids=["missing", "empty", "blank"])
def test_a_missing_or_empty_optional_slot_degrades_to_the_placeholder(text):
    result = assemble(
        "Signals: {{SIGNALS}}", {"SIGNALS": Slot(text, "s.md", required=False)}
    )
    assert result == f"Signals: {OPTIONAL_PLACEHOLDER}"
    assert OPTIONAL_PLACEHOLDER.strip()


def test_every_optional_slot_gets_the_same_placeholder():
    result = assemble(
        "{{A}}|{{B}}",
        {
            "A": Slot(None, "a.md", required=False),
            "B": Slot("", "b.md", required=False),
        },
    )
    first, second = result.split("|")
    assert first == second == OPTIONAL_PLACEHOLDER


def test_a_present_optional_slot_is_used_not_the_placeholder():
    result = assemble("{{A}}", {"A": Slot("real content", "a.md", required=False)})
    assert result == "real content"


def test_a_marker_with_no_source_raises_after_assembly_naming_it():
    with pytest.raises(JsaError, match="ORPHAN"):
        assemble("{{KNOWN}} {{ORPHAN}}", {"KNOWN": Slot("ok", "k.md")})


def test_a_marker_with_no_source_raises_even_when_every_other_slot_is_filled():
    with pytest.raises(JsaError):
        assemble("{{A}}{{B}}{{C}}", {"A": Slot("1", "a"), "B": Slot("2", "b")})


def test_a_template_with_no_markers_comes_back_unchanged():
    assert assemble("nothing to fill", {}) == "nothing to fill"


@pytest.mark.parametrize(
    "text",
    [r"C:\path\1 and \g<0>", "a $1 b ${X}", "100% {not a marker} and {single}"],
    ids=["backslashes", "dollars", "braces"],
)
def test_slot_text_is_inserted_verbatim(text):
    assert assemble("<{{X}}>", {"X": Slot(text, "x.md")}) == f"<{text}>"


def test_the_template_text_around_markers_is_preserved():
    template = "# Title\n\nBefore {{X}} after.\n\n- list\n"
    assert (
        assemble(template, {"X": Slot("mid", "x.md")})
        == "# Title\n\nBefore mid after.\n\n- list\n"
    )
