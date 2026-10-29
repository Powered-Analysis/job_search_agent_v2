"""PRD 04 "Role families" (discovery and validation), PRD 03 "The address names the role family" (the tag rule),
PRD 06 "Configuration surface": the profile's `role_families/` folders, and the commands that need valid ones."""

import sys

import httpx
import pytest
from conftest import drop_all_tables
from profile_helpers import (
    EXAMPLE_DIR,
    ROLE_FAMILIES,
    copy_example,
    write_role_families,
)

from jsa import cli, db
from jsa.errors import JsaError
from jsa.profile import RoleFamily, role_families, role_family_tag, unassigned_reason

FOLDERS = "role_families"


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = copy_example(tmp_path / "profile")
    write_role_families(path)
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def family_folder(profile, name):
    return profile / FOLDERS / name


def add_family(profile, name, resume=b"base resume"):
    folder = family_folder(profile, name)
    folder.mkdir(parents=True)
    (folder / "resume.docx").write_bytes(resume)
    return folder


# --- discovery -----------------------------------------------------------------------


def test_every_folder_is_a_role_family_named_by_the_folder_in_name_order(profile):
    write_role_families(profile, ("Zebra Wrangling", "Analytics", "Mid Tier"))
    families = role_families()
    assert [family.name for family in families] == [
        "Analytics",
        "Mid Tier",
        "Zebra Wrangling",
    ]
    assert all(isinstance(family, RoleFamily) for family in families)


def test_a_role_family_points_to_its_resume_and_has_no_cover_letter_by_default(
    profile,
):
    write_role_families(profile, ("Only One",))
    (family,) = role_families()
    assert family.resume == family_folder(profile, "Only One") / "resume.docx"
    assert family.cover_letter is None


@pytest.mark.parametrize("file_name", ["cover_letter.docx", "cover_letter.pdf"])
def test_a_role_family_has_the_cover_letter_in_its_folder(profile, file_name):
    letter = family_folder(profile, ROLE_FAMILIES[0]) / file_name
    letter.write_bytes(b"letter")
    by_name = {family.name: family for family in role_families()}
    assert by_name[ROLE_FAMILIES[0]].cover_letter == letter
    assert by_name[ROLE_FAMILIES[1]].cover_letter is None


def test_one_cover_letter_per_family_is_not_shared_between_families(profile):
    for name in ROLE_FAMILIES:
        (family_folder(profile, name) / "cover_letter.docx").write_bytes(name.encode())
    for family in role_families():
        assert family.cover_letter == family_folder(profile, family.name) / (
            "cover_letter.docx"
        )


def test_the_example_profile_has_two_role_families_with_non_empty_resumes():
    names = sorted(path.name for path in (EXAMPLE_DIR / FOLDERS).iterdir())
    assert names == ["Analytics Engineering", "Data Analysis"]
    for name in names:
        assert (EXAMPLE_DIR / FOLDERS / name / "resume.docx").stat().st_size > 0


def test_the_example_profile_loads_as_two_role_families(tmp_path, monkeypatch):
    monkeypatch.setenv("JSA_PROFILE_DIR", str(copy_example(tmp_path / "profile")))
    assert [family.name for family in role_families()] == [
        "Analytics Engineering",
        "Data Analysis",
    ]


# --- validation ----------------------------------------------------------------------


def test_a_profile_with_no_role_families_directory_raises(profile):
    (profile / FOLDERS).rename(profile / "elsewhere")
    with pytest.raises(JsaError, match=r"profile\.example/"):
        role_families()


def test_a_profile_with_an_empty_role_families_directory_raises(profile):
    write_role_families(profile, ())
    (profile / FOLDERS).mkdir(exist_ok=True)
    with pytest.raises(JsaError, match=r"profile\.example/"):
        role_families()


def test_a_missing_resume_raises_naming_the_folder(profile):
    (family_folder(profile, "Data Analysis") / "resume.docx").unlink()
    with pytest.raises(JsaError) as raised:
        role_families()
    assert "Data Analysis" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_an_empty_resume_raises_naming_the_folder(profile):
    (family_folder(profile, "Data Analysis") / "resume.docx").write_bytes(b"")
    with pytest.raises(JsaError) as raised:
        role_families()
    assert "Data Analysis" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_two_cover_letters_raise_naming_the_folder(profile):
    folder = family_folder(profile, "Data Analysis")
    (folder / "cover_letter.docx").write_bytes(b"one")
    (folder / "cover_letter.pdf").write_bytes(b"two")
    with pytest.raises(JsaError) as raised:
        role_families()
    assert "Data Analysis" in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_files_that_only_resemble_a_cover_letter_do_not_count_as_one(profile):
    folder = family_folder(profile, "Data Analysis")
    (folder / "cover_letter.docx").write_bytes(b"letter")
    (folder / "my_cover_letter.docx").write_bytes(b"not it")
    (folder / "cover_letters.txt").write_bytes(b"not it")
    (folder / "notes.md").write_bytes(b"not it")
    by_name = {family.name: family for family in role_families()}
    assert by_name["Data Analysis"].cover_letter == folder / "cover_letter.docx"


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("Customer Enablement", "customer-enablement"),
        ("Data Analysis", "data  analysis"),
        ("Data Analysis", "DATA_ANALYSIS"),
        ("Data Analysis", "-Data Analysis-"),
    ],
)
def test_two_role_families_sharing_an_email_tag_raise_naming_both_folders(
    profile, first, second
):
    write_role_families(profile, (first, second))
    with pytest.raises(JsaError) as raised:
        role_families()
    assert first in str(raised.value)
    assert second in str(raised.value)
    assert "profile.example/" in str(raised.value)


def test_role_families_with_different_tags_are_valid(profile):
    write_role_families(profile, ("Data Analysis", "Data Analytics", "Data Science"))
    assert len(role_families()) == 3


# --- the tag rule --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tag"),
    [
        ("Customer Enablement", "customer-enablement"),
        ("Data Analysis", "data-analysis"),
        ("Analytics Engineering", "analytics-engineering"),
        ("Data   Science", "data-science"),
        ("R&D -- Ops", "r-d-ops"),
        ("  Padded Name  ", "padded-name"),
        ("---Dashed---", "dashed"),
        ("Back/End (Platform)", "back-end-platform"),
        ("ML2 Ops", "ml2-ops"),
        ("UPPER", "upper"),
        ("single", "single"),
    ],
)
def test_the_tag_is_lowercase_with_each_run_of_other_characters_one_dash_and_none_at_either_end(
    name, tag
):
    assert role_family_tag(name) == tag


# --- the commands that need valid role families --------------------------------------

BROKEN = {
    "no role family": lambda profile: write_role_families(profile, ()),
    "missing resume": lambda profile: (
        family_folder(profile, "Data Analysis") / "resume.docx"
    ).unlink(),
    "empty resume": lambda profile: (
        family_folder(profile, "Data Analysis") / "resume.docx"
    ).write_bytes(b""),
    "two cover letters": lambda profile: [
        (family_folder(profile, "Data Analysis") / name).write_bytes(b"letter")
        for name in ("cover_letter.docx", "cover_letter.pdf")
    ],
    "shared tag": lambda profile: add_family(profile, "data-analysis"),
}


class Quiet:
    """A stdin that fails the test if the command asks anything."""

    asked = 0

    def readline(self, *_):
        Quiet.asked += 1
        return ""

    def isatty(self):
        return False

    def fileno(self):
        raise OSError


@pytest.fixture
def untouched_world(http_url, monkeypatch):
    """Nothing reaches the network, and the command's prompts are counted."""
    drop_all_tables(http_url)
    conn = db.connect()
    url = "https://job-boards.greenhouse.io/acme/jobs/1234567890"
    db.insert_posting(
        conn, company="Acme", title="Analyst", url=url, search_agent="claude"
    )
    posting_id = db.find_posting(conn, url)[0]
    db.record_decision(conn, url, "Apply", None, "Data Analysis")
    conn.close()

    def refuse(self, request):
        raise AssertionError(f"unexpected request {request.url}")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    Quiet.asked = 0
    monkeypatch.setattr(sys, "stdin", Quiet())
    return url, posting_id


def run(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["jsa", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


@pytest.mark.parametrize("broken", BROKEN)
@pytest.mark.parametrize("command", ["review", "revise", "add"])
def test_review_revise_and_add_raise_before_any_prompt_when_the_profile_is_unusable(
    profile, untouched_world, monkeypatch, capsys, command, broken
):
    url, posting_id = untouched_world
    BROKEN[broken](profile)
    args = {
        "review": ["review"],
        "revise": ["revise", "--id", str(posting_id)],
        "add": ["add", "https://job-boards.greenhouse.io/acme/jobs/987654321"],
    }[command]
    code, output = run(monkeypatch, capsys, *args)
    assert code != 0
    assert "profile.example/" in output
    assert "Traceback" not in output
    assert Quiet.asked == 0
    conn = db.connect()
    try:
        assert db.find_posting(conn, url)[1:] == ("Apply", True, "Data Analysis")
        count = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
    finally:
        conn.close()
    assert count == 1


@pytest.mark.parametrize(
    ("broken", "folder"),
    [
        ("missing resume", "Data Analysis"),
        ("empty resume", "Data Analysis"),
        ("two cover letters", "Data Analysis"),
    ],
)
def test_the_command_failure_names_the_offending_folder(
    profile, untouched_world, monkeypatch, capsys, broken, folder
):
    BROKEN[broken](profile)
    code, output = run(monkeypatch, capsys, "review")
    assert code != 0
    assert folder in output


@pytest.mark.parametrize(
    "role_family", [None, "gone"], ids=["no-role-family", "unknown-role-family"]
)
def test_the_profiles_unassigned_check_names_the_row_and_prints_nothing(
    role_family, capsys
):
    reason = unassigned_reason(41, role_family, ["data", "product"])
    assert reason is not None and "jsa revise --id 41" in reason
    assert capsys.readouterr().out == ""


def test_the_profiles_unassigned_check_passes_a_known_role_family(capsys):
    assert unassigned_reason(41, "data", ["data", "product"]) is None
    assert capsys.readouterr().out == ""
