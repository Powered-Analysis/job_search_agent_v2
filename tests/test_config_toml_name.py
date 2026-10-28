"""The main settings file is named `config.toml` everywhere the app mentions or reads it (issue #178).

Every error message and file lookup that names the file reads the same as before the name had a
single definition.
"""

import pytest
from profile_helpers import ROLE_FAMILIES, write_config_toml, write_role_families

from jsa.errors import JsaError
from jsa.profile import (
    CONFIG_TOML,
    SEARCH_TOML,
    inbox_app,
    inbox_drive_folder,
    load_config,
    tracker_spreadsheet_id,
)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def test_the_constant_is_the_file_name_and_search_keeps_its_own():
    assert CONFIG_TOML == "config.toml"
    assert SEARCH_TOML == "search/search.toml"


def test_load_config_reads_the_file_by_that_name(profile):
    write_config_toml(
        profile, 'candidate_name = "Pat"\ntracker_spreadsheet_id = "abc"\n'
    )
    assert (profile / CONFIG_TOML).is_file()
    assert load_config().candidate_name == "Pat"
    assert tracker_spreadsheet_id() == "abc"


def test_missing_tracker_spreadsheet_id_names_the_file_and_example(profile):
    write_config_toml(profile, 'candidate_name = "Pat"\n')
    with pytest.raises(JsaError) as raised:
        tracker_spreadsheet_id()
    message = str(raised.value)
    assert message.startswith(
        f"tracker_spreadsheet_id is missing or empty in {profile / 'config.toml'}. "
    )
    assert message.endswith("Copy the shape from profile.example/config.toml.")


def test_empty_tracker_spreadsheet_id_names_the_file_and_example(profile):
    write_config_toml(profile, 'tracker_spreadsheet_id = ""\n')
    with pytest.raises(JsaError) as raised:
        tracker_spreadsheet_id()
    message = str(raised.value)
    assert str(profile / "config.toml") in message
    assert "tracker_spreadsheet_id" in message
    assert message.endswith("Copy the shape from profile.example/config.toml.")


@pytest.mark.parametrize(
    ("lookup", "key"), [(inbox_app, "app"), (inbox_drive_folder, "drive_folder_id")]
)
def test_missing_inbox_key_names_the_key_file_and_example(profile, lookup, key):
    write_config_toml(profile, '[inbox]\nsenders = ["jobs@example.com"]\n')
    with pytest.raises(JsaError) as raised:
        lookup(load_config())
    assert str(raised.value) == (
        f"[inbox] {key} is missing in {profile / 'config.toml'}. "
        "Copy the shape from profile.example/config.toml."
    )


def test_a_present_inbox_key_is_returned(profile):
    write_config_toml(
        profile,
        '[inbox]\napp = "jsa-inbox"\ndrive_folder_id = "folder"\nsenders = ["a@b.c"]\n',
    )
    config = load_config()
    assert inbox_app(config) == "jsa-inbox"
    assert inbox_drive_folder(config) == "folder"


def test_the_inbox_machine_ships_config_toml_by_its_file_name(profile, monkeypatch):
    from jsa.deploy import MACHINE_PROFILE_DIR, inbox_machine_files

    write_config_toml(profile)
    write_role_families(profile)
    files = inbox_machine_files()
    assert files[f"{MACHINE_PROFILE_DIR}/config.toml"] == profile / "config.toml"
    for family in ROLE_FAMILIES:
        path = f"role_families/{family}/resume.docx"
        assert files[f"{MACHINE_PROFILE_DIR}/{path}"] == profile / path
