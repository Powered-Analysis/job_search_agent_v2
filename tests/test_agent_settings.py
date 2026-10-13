"""Settings lookup for the redline and refine agents (PRD 04, PRD 05, `XC-14`; issue #136).

Each function hands back its own `[agents.*]` table, the redline carries the optional time limit
and refine does not, and a missing table raises before any model call.
"""

import pytest
from profile_helpers import write_config_toml

from jsa.errors import JsaError
from jsa.profile import (
    AgentSettings,
    load_config,
    redline_settings,
    refine_settings,
)

CONFIG = """\
candidate_name = "Pat Example"
tracker_spreadsheet_id = "sheet-id"

[agents.redline]
model = "model-for-redline"
effort = "medium"
wall_clock_seconds = 22

[agents.refine]
model = "model-for-refine"
effort = "max"
"""

SETTINGS = {
    "redline": redline_settings,
    "refine": refine_settings,
}


@pytest.fixture
def profile(tmp_path, monkeypatch):
    path = tmp_path / "profile"
    path.mkdir()
    monkeypatch.setenv("JSA_PROFILE_DIR", str(path))
    return path


def without_table(name):
    """The config with one `[agents.<name>]` table removed."""
    blocks = CONFIG.split("\n\n")
    return "\n\n".join(b for b in blocks if not b.startswith(f"[agents.{name}]"))


@pytest.mark.parametrize(
    ("name", "model", "effort"),
    [
        ("redline", "model-for-redline", "medium"),
        ("refine", "model-for-refine", "max"),
    ],
)
def test_each_agent_gets_its_own_model_and_effort(profile, name, model, effort):
    write_config_toml(profile, CONFIG)
    settings = SETTINGS[name](load_config())
    assert (settings.model, settings.effort) == (model, effort)


def test_the_redline_settings_carry_their_own_time_limit(profile):
    write_config_toml(profile, CONFIG)
    redline = redline_settings(load_config())
    assert isinstance(redline, AgentSettings)
    assert redline.wall_clock_seconds == 22


def test_the_refine_settings_have_no_time_limit(profile):
    write_config_toml(profile, CONFIG)
    settings = refine_settings(load_config())
    assert not isinstance(settings, AgentSettings)
    assert not hasattr(settings, "wall_clock_seconds")


@pytest.mark.parametrize("name", ["redline", "refine"])
def test_a_missing_table_raises_naming_the_config_file_and_the_table(profile, name):
    write_config_toml(profile, without_table(name))
    config = load_config()
    with pytest.raises(JsaError) as raised:
        SETTINGS[name](config)
    assert f"[agents.{name}]" in str(raised.value)
    assert "config.toml" in str(raised.value)


@pytest.mark.parametrize("missing", ["redline", "refine"])
def test_a_missing_table_does_not_affect_the_other_agents(profile, missing):
    write_config_toml(profile, without_table(missing))
    config = load_config()
    for name, settings in SETTINGS.items():
        if name != missing:
            assert settings(config).model == f"model-for-{name}"
