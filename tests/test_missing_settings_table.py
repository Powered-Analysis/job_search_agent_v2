"""A missing settings table reads the same everywhere (XC-14; issue #148).

Each command that needs a settings table raises a `JsaError` naming the file, the table, and the
example profile to copy from, before any model call or network call.
"""

import pytest
from profile_helpers import (
    CONFIG_TOML,
    schedule_toml,
    write_config_toml,
    write_search_toml,
)

from jsa.errors import JsaError
from jsa.profile import (
    claude_settings,
    fly_settings,
    inbox_settings,
    load_config,
    load_search_config,
    redline_settings,
    refine_settings,
)


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def without_block(text, header):
    """The TOML text with the table starting at `header` removed."""
    blocks = text.split("\n\n")
    return "\n\n".join(b for b in blocks if not b.startswith(header))


CONFIG_LOOKUPS = [
    ("fly", fly_settings),
    ("inbox", inbox_settings),
    ("agents.redline", redline_settings),
    ("agents.refine", refine_settings),
]


@pytest.mark.parametrize(("table", "lookup"), CONFIG_LOOKUPS)
def test_missing_config_table_names_file_table_and_example(profile, table, lookup):
    write_config_toml(
        profile,
        without_block(CONFIG_TOML, f"[{table}]"),
    )
    config = load_config()
    with pytest.raises(JsaError) as raised:
        lookup(config)
    message = str(raised.value)
    assert str(profile / "config.toml") in message
    assert f"has no [{table}] table" in message
    assert "profile.example/config.toml" in message


def test_missing_claude_runner_table_names_file_table_and_example(profile):
    # No claude search is scheduled, so the file loads and only the lookup finds the table missing.
    schedule = {"monday": [("perplexity", 72)]}
    text = schedule_toml("America/New_York", "07:00", schedule)
    write_search_toml(profile, without_block(text, "[runners.claude]"))
    config = load_search_config()
    with pytest.raises(JsaError) as raised:
        claude_settings(config)
    message = str(raised.value)
    assert str(profile / "search" / "search.toml") in message
    assert "has no [runners.claude] table" in message
    assert "profile.example/search/search.toml" in message


def test_present_tables_are_returned_not_raised(profile):
    write_config_toml(
        profile,
        CONFIG_TOML + '\n[inbox]\nsenders = ["jobs@example.com"]\n',
    )
    write_search_toml(profile)
    config = load_config()
    assert fly_settings(config).app == "jsa-example"
    assert inbox_settings(config).senders == ["jobs@example.com"]
    assert redline_settings(config).model == "claude-fable-5-1"
    assert refine_settings(config).effort == "high"
    assert claude_settings(load_search_config()).model == "claude-opus-5-5"


def test_every_missing_table_message_has_the_same_shape(profile):
    write_config_toml(profile, 'candidate_name = "Pat"\ntracker_spreadsheet_id = "x"\n')
    config = load_config()
    messages = []
    for _, lookup in CONFIG_LOOKUPS:
        with pytest.raises(JsaError) as raised:
            lookup(config)
        messages.append(str(raised.value))
    for message in messages:
        prefix, _, rest = message.partition(" has no [")
        assert prefix == str(profile / "config.toml")
        assert rest.endswith("table. Copy the shape from profile.example/config.toml.")
