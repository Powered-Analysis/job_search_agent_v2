"""`jsa deploy` and the deployment artifacts (issue #17; PRD 06 "Deployment image", "Fly configuration", "`jsa deploy`",
"Entry Point & First-Time Experience", "Configuration surface"; XC-1, XC-11, XC-13, XC-14).

`fly` is replaced by a stub script reached through `JSA_FLY_BIN`, which logs each invocation's argv and
answers `machine list --json` from $STUB_MACHINES; the profile is a temporary copy of `profile.example/`.
No test reaches the network, and none needs the database.
"""

import json
import os
import re
import shutil
import stat
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import REPO_ROOT
from profile_helpers import (
    FRAGMENTS,
    SEARCH_TOML,
    WEEKDAYS,
    copy_example,
    schedule_toml,
    write_config_toml,
    write_search_toml,
)

from jsa import cli
from jsa.deploy import BUILD_INPUTS

# Logs argv to $STUB_LOG, answers a machine listing from $STUB_MACHINES, and fails any call whose
# subcommand is named in $STUB_FAIL (exit 1). `secrets list` answers $STUB_SECRETS for the search app and
# $STUB_INBOX_SECRETS for an app whose name ends in `-inbox`, and a machine
# listing for an app whose name ends in `-inbox` answers $STUB_INBOX_MACHINES when that is set. `machine update` fails its first $STUB_UPDATE_FAILS
# attempts, and `machine run` its first $STUB_RUN_FAILS, as a registry that hasn't caught up with the push would. When $STUB_SNAPSHOT is set, a
# `deploy` call copies its build context (the directory named in its arguments, else its working
# directory) there, since the app may delete the context once the build returns. When $STUB_RUN_CREATES_ON is N, the Nth `machine run`
# fails after creating an `hourly` machine, as a launch that times out waiting for the machine to start would; later listings of that app show it.
STUB = """\
#!{python}
import json, os, shutil, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
words = [a for a in argv if not a.startswith("-")]
if "STUB_SNAPSHOT" in os.environ and words[:1] == ["deploy"]:
    context = ([a for a in argv if os.path.isdir(a)] or [os.getcwd()])[-1]
    shutil.copytree(context, os.environ["STUB_SNAPSHOT"], dirs_exist_ok=True)
if words[:1] and words[0] in os.environ.get("STUB_FAIL", "").split(","):
    print("boom", file=sys.stderr)
    sys.exit(1)
app = next((argv[i + 1] for i in range(len(argv) - 1) if argv[i] == "-a"), "")
if words[:2] == ["secrets", "list"]:
    held = "STUB_INBOX_SECRETS" if app.endswith("-inbox") else "STUB_SECRETS"
    print(os.environ.get(held, "[]"))
    sys.exit(0)
created = os.environ["STUB_LOG"] + ".created"
if ("list" in words or "ls" in words) and os.path.exists(created) and open(created).read() == app:
    print(json.dumps([{{"id": "created", "state": "started", "config": {{"schedule": "hourly"}}}}]))
    sys.exit(0)
if "list" in words or "ls" in words:
    inbox_machines = os.environ.get("STUB_INBOX_MACHINES")
    if app.endswith("-inbox") and inbox_machines is not None:
        print(inbox_machines)
    else:
        print(os.environ.get("STUB_MACHINES", "[]"))
    sys.exit(0)
if "update" in words:
    counter = os.environ["STUB_LOG"] + ".updates"
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(seen + 1))
    if seen < int(os.environ.get("STUB_UPDATE_FAILS", "0")):
        print("Error: image not found", file=sys.stderr)
        sys.exit(1)
if "run" in words:
    counter = os.environ["STUB_LOG"] + ".runs"
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(seen + 1))
    if seen + 1 == int(os.environ.get("STUB_RUN_CREATES_ON", "0")):
        open(created, "w").write(app)
        print("Error: timed out waiting for the machine to start", file=sys.stderr)
        sys.exit(1)
    if seen < int(os.environ.get("STUB_RUN_FAILS", "0")):
        print("Error: failed to get manifest: manifest unknown", file=sys.stderr)
        sys.exit(1)
"""

# What the search app holds so a deploy is not stopped for a missing secret (PRD 06, `jsa deploy` step 1):
# the database's two and the key of every search agent a profile can schedule.
SEARCH_APP_SECRETS = (
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "PERPLEXITY_API_KEY",
    "JSA_SEARCH_ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
)

FULL_WEEK = {day: [("perplexity", 24)] for day in WEEKDAYS}
SAFE_RUN_AT = "07:00"
REQUIRED = ("candidate", "target_roles", "filters")
OPTIONAL = ("positive_signals", "negative_signals", "hard_exclusions")


@pytest.fixture
def fly(tmp_path, monkeypatch):
    """A copy of the example profile, a stub `fly`, and the stub's argv log."""
    profile = copy_example(tmp_path / "profile")
    monkeypatch.setenv("JSA_PROFILE_DIR", str(profile))
    stub = tmp_path / "stub-fly"
    stub.write_text(STUB.format(python=sys.executable), encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "fly.log"
    monkeypatch.setenv("JSA_FLY_BIN", str(stub))
    monkeypatch.setenv("STUB_LOG", str(log))
    for name in (
        "STUB_FAIL",
        "STUB_MACHINES",
        "STUB_INBOX_MACHINES",
        "STUB_INBOX_SECRETS",
        "STUB_UPDATE_FAILS",
        "STUB_RUN_FAILS",
        "STUB_RUN_CREATES_ON",
        "STUB_SNAPSHOT",
    ):
        monkeypatch.delenv(name, raising=False)
    # A retry for registry lag must not slow the suite, whichever way the app sleeps.
    monkeypatch.setenv("STUB_SECRETS", secrets_json(*SEARCH_APP_SECRETS))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)
    monkeypatch.setattr("jsa.deploy.sleep", lambda _seconds: None, raising=False)
    monkeypatch.setattr("jsa.deploy.UPDATE_RETRY_SECONDS", 0, raising=False)
    return profile, log


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines()]


def jsa_deploy(monkeypatch, capsys, *args) -> tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["jsa", "deploy", *args])
    code = 0
    try:
        cli.main()
    except SystemExit as exit_:
        code = exit_.code if isinstance(exit_.code, int) else 1
    captured = capsys.readouterr()
    return code, captured.out + captured.err


def machines(*schedules: str | None) -> str:
    """`fly machine list --json` output for machines `m0`, `m1`, ... carrying these schedules."""
    return json.dumps(
        [
            {
                "id": f"m{index}",
                "state": "stopped",
                "config": {"schedule": schedule} if schedule else {},
            }
            for index, schedule in enumerate(schedules)
        ]
    )


def has_flag(argv: list[str], flag: str, value: str) -> bool:
    return (
        any(argv[i] == flag and argv[i + 1] == value for i in range(len(argv) - 1))
        or f"{flag}={value}" in argv
    )


def pick(all_calls: list[list[str]], *words: str) -> list[list[str]]:
    """The calls whose leading non-flag words are exactly `words`."""
    picked = []
    for argv in all_calls:
        leading = [a for a in argv if not a.startswith("-")][: len(words)]
        if leading == list(words):
            picked.append(argv)
    return picked


def secret_calls(log: Path) -> list[list[str]]:
    return [argv for argv in calls(log) if any(a.startswith("secret") for a in argv)]


def pick_machine(all_calls: list[list[str]], verb: str) -> list[list[str]]:
    return [
        argv
        for argv in all_calls
        if [a for a in argv if not a.startswith("-")][:1] in (["machine"], ["machines"])
        and verb in argv
    ]


def set_search(profile: Path, toml: str) -> None:
    write_search_toml(profile, toml)


def full_week(run_at: str = SAFE_RUN_AT) -> str:
    return schedule_toml("America/New_York", run_at, FULL_WEEK)


def warned(output: str) -> bool:
    return "warn" in output.lower()


# --- validation before any build ------------------------------------------------------


@pytest.mark.parametrize("fragment", REQUIRED)
def test_missing_fragment_aborts_before_fly(fly, monkeypatch, capsys, fragment):
    profile, log = fly
    (profile / "search" / f"{fragment}.md").unlink()
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert fragment in output
    assert calls(log) == []


@pytest.mark.parametrize("fragment", REQUIRED)
def test_empty_fragment_aborts_before_fly(fly, monkeypatch, capsys, fragment):
    profile, log = fly
    (profile / "search" / f"{fragment}.md").write_text("  \n", encoding="utf-8")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert calls(log) == []


@pytest.mark.parametrize("fragment", OPTIONAL)
def test_missing_optional_fragment_does_not_block_the_deploy(
    fly, monkeypatch, capsys, fragment
):
    profile, log = fly
    (profile / "search" / f"{fragment}.md").unlink()
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert pick(calls(log), "deploy")


def test_missing_search_toml_aborts_before_fly(fly, monkeypatch, capsys):
    profile, log = fly
    (profile / "search" / "search.toml").unlink()
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "search.toml" in output
    assert calls(log) == []


def broken(old: str, new: str) -> str:
    assert old in SEARCH_TOML
    return SEARCH_TOML.replace(old, new)


INVALID_SEARCH_TOML = {
    "not-toml": "this is [not toml",
    "bad-timezone": broken('"America/New_York"', '"Mars/Olympus_Mons"'),
    "bad-run-at": broken('run_at = "07:00"', 'run_at = "25:00"'),
    "unknown-key": broken('run_at = "07:00"', 'run_at = "07:00"\ncolour = "blue"'),
    "unknown-agent": broken('agent = "perplexity"', 'agent = "chatgpt"'),
    "zero-window": broken("window_hours = 72", "window_hours = 0"),
    "no-verification-mode": SEARCH_TOML.split("[verification]")[0],
    "bad-verification-mode": broken('mode = "strict"', 'mode = "sloppy"'),
    "scheduled-runner-has-no-settings": broken(
        '[runners.claude]\nmodel = "claude-opus-5-5"\neffort = "high"\n', ""
    ),
    "gemini-has-no-settings": broken(
        "[runners.claude]",
        '[runners.gemini]\nagent = "deep-research-preview-04-2026"\n\n[runners.claude]',
    ),
    "malformed-runner-setting": broken('effort = "high"', "effort = 7"),
    "unknown-runner-key": broken('effort = "high"', 'effort = "high"\nturbo = true'),
}


@pytest.mark.parametrize("name", list(INVALID_SEARCH_TOML))
@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["deploy", "dry-run"])
def test_invalid_search_toml_aborts_before_fly(fly, monkeypatch, capsys, name, flags):
    profile, log = fly
    set_search(profile, INVALID_SEARCH_TOML[name])
    code, output = jsa_deploy(monkeypatch, capsys, *flags)
    assert code != 0
    assert "search.toml" in output
    assert calls(log) == []


def test_model_ids_are_not_checked_against_a_list(fly, monkeypatch, capsys):
    profile, _ = fly
    set_search(
        profile,
        broken('"claude-opus-5-5"', '"claude-future-model-9"'),
    )
    code, _ = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0


@pytest.mark.parametrize("missing", ["app", "region"])
def test_missing_fly_setting_names_config_and_points_to_example(
    fly, monkeypatch, capsys, missing
):
    profile, log = fly
    text = "\n".join(
        line
        for line in (REPO_ROOT / "profile.example" / "config.toml")
        .read_text()
        .splitlines()
        if not re.match(rf"{missing}\s*=", line)
    )
    write_config_toml(profile, text)
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "config.toml" in output
    assert "profile.example" in output
    assert calls(log) == []


def test_missing_fly_table_names_config_and_points_to_example(fly, monkeypatch, capsys):
    profile, log = fly
    write_config_toml(profile, 'candidate_name = "Pat"\n')
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "config.toml" in output
    assert "profile.example" in output
    assert calls(log) == []


# --- warnings: reported, never blocking -----------------------------------------------


def test_a_clean_profile_deploys_without_warnings(fly, monkeypatch, capsys):
    profile, log = fly
    set_search(profile, full_week())
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert not warned(output)
    assert calls(log)


def test_windows_covering_the_whole_week_produce_no_coverage_warning(
    fly, monkeypatch, capsys
):
    profile, _ = fly
    set_search(profile, full_week())
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert not warned(output)


def test_one_window_as_long_as_the_week_covers_it(fly, monkeypatch, capsys):
    profile, _ = fly
    set_search(
        profile,
        schedule_toml("America/New_York", SAFE_RUN_AT, {"monday": [("claude", 168)]}),
    )
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert not warned(output)


def test_overlapping_windows_covering_the_week_produce_no_warning(
    fly, monkeypatch, capsys
):
    profile, _ = fly
    days = {"monday": [("claude", 96)], "thursday": [("gemini", 96)]}
    set_search(profile, schedule_toml("America/New_York", SAFE_RUN_AT, days))
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert not warned(output)


@pytest.mark.parametrize(
    "days",
    [
        {"monday": [("claude", 24)]},
        {day: [("perplexity", 23)] for day in WEEKDAYS},
        {"monday": [("claude", 72)], "thursday": [("claude", 72)]},
        {},
    ],
    ids=["one-day", "an-hour-short-each-day", "a-day-between-two", "no-searches"],
)
def test_unsearched_hours_warn_and_the_deploy_continues(fly, monkeypatch, capsys, days):
    profile, log = fly
    set_search(profile, schedule_toml("America/New_York", SAFE_RUN_AT, days))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert warned(output)
    assert pick(calls(log), "deploy")


def test_closing_the_last_gap_removes_the_warning(fly, monkeypatch, capsys):
    profile, _ = fly
    days = {
        "monday": [("claude", 72)],
        "thursday": [("claude", 72)],
        "friday": [("claude", 24)],
    }
    set_search(profile, schedule_toml("America/New_York", SAFE_RUN_AT, days))
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert not warned(output)


def test_run_at_after_2259_warns_and_the_deploy_continues(fly, monkeypatch, capsys):
    profile, log = fly
    set_search(profile, full_week("23:00"))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert warned(output)
    assert pick(calls(log), "deploy")


def test_run_at_2259_is_not_late(fly, monkeypatch, capsys):
    profile, _ = fly
    set_search(profile, full_week("22:59"))
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert not warned(output)


def test_pending_refine_proposal_warns_and_the_deploy_continues(
    fly, monkeypatch, capsys
):
    profile, log = fly
    set_search(profile, full_week())
    (profile / "refine").mkdir()
    (profile / "refine" / "rationale.md").write_text("1. Tighten filters.\n")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert warned(output)
    assert "refine" in output.lower()
    assert pick(calls(log), "deploy")


def test_pending_proposal_does_not_ship(fly, monkeypatch, capsys):
    profile, _ = fly
    set_search(profile, full_week())
    (profile / "refine").mkdir()
    (profile / "refine" / "rationale.md").write_text("1. Tighten filters.\n")
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "rationale.md" not in output


def test_warnings_stack(fly, monkeypatch, capsys):
    profile, _ = fly
    set_search(
        profile,
        schedule_toml("America/New_York", "23:30", {"monday": [("claude", 24)]}),
    )
    (profile / "refine").mkdir()
    (profile / "refine" / "rationale.md").write_text("1. Tighten filters.\n")
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert len([l for l in output.splitlines() if warned(l)]) >= 3


# --- --dry-run ------------------------------------------------------------------------


def test_dry_run_lists_the_search_files_and_runs_no_fly_command_but_the_secret_listing(
    fly, monkeypatch, capsys
):
    _, log = fly
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    for name in (*(f"{f}.md" for f in FRAGMENTS), "search.toml"):
        assert name in output
    assert all(pick([argv], "secrets", "list") for argv in calls(log))


def test_dry_run_lists_nothing_outside_profile_search(fly, monkeypatch, capsys):
    profile, _ = fly
    (profile / "notes.txt").write_text("private\n")
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "config.toml" not in output
    assert "resume.docx" not in output
    assert "notes.txt" not in output


def test_dry_run_lists_files_in_nested_search_directories(fly, monkeypatch, capsys):
    profile, _ = fly
    (profile / "search" / "extra").mkdir()
    (profile / "search" / "extra" / "more.md").write_text("x\n")
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "more.md" in output


def test_dry_run_with_no_fly_binary_installed_ends_cleanly_without_building(
    fly, monkeypatch, capsys, tmp_path
):
    _, log = fly
    monkeypatch.setenv("JSA_FLY_BIN", str(tmp_path / "no-such-fly"))
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code in (0, 1)
    assert "Traceback" not in output
    assert pick(calls(log), "deploy") == []


def test_dry_run_and_smoke_are_mutually_exclusive(fly, monkeypatch, capsys):
    _, log = fly
    code, _ = jsa_deploy(monkeypatch, capsys, "--dry-run", "--smoke")
    assert code != 0
    assert calls(log) == []


# --- build, push, and the swap --------------------------------------------------------


def utc_digits() -> str:
    return datetime.now(UTC).strftime("%Y%m%d")


def label_of(build: list[str]) -> str:
    value = next(
        build[i + 1] for i in range(len(build) - 1) if build[i] == "--image-label"
    )
    return value


def test_build_pushes_with_a_utc_image_label_for_the_configured_app(
    fly, monkeypatch, capsys
):
    _, log = fly
    before = utc_digits()
    code, _ = jsa_deploy(monkeypatch, capsys)
    after = utc_digits()
    assert code == 0
    (build,) = pick(calls(log), "deploy")
    assert "--build-only" in build
    assert "--push" in build
    assert has_flag(build, "-a", "jsa-example") or "--app=jsa-example" in build
    digits = re.sub(r"\D", "", label_of(build))
    assert digits[:8] in {before, after}


def test_no_hourly_machine_creates_one(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    (build,) = pick(all_calls, "deploy")
    (run,) = pick_machine(all_calls, "run")
    assert has_flag(run, "--schedule", "hourly")
    assert has_flag(run, "--vm-memory", "1024")
    assert has_flag(run, "--region", "iad")
    assert "--rm" not in run
    assert any(label_of(build) in arg for arg in run)
    assert all_calls.index(build) < all_calls.index(run)
    assert pick_machine(all_calls, "update") == []


def test_machines_without_the_hourly_schedule_do_not_count(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines(None, "daily"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert len(pick_machine(calls(log), "run")) == 1
    assert pick_machine(calls(log), "update") == []


def test_one_hourly_machine_is_updated_in_place(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines(None, "hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    (build,) = pick(all_calls, "deploy")
    (update,) = pick_machine(all_calls, "update")
    assert "m1" in update
    assert "m0" not in update
    assert has_flag(update, "--schedule", "hourly")
    assert has_flag(update, "--vm-memory", "1024")
    image = next(
        update[i + 1] for i in range(len(update) - 1) if update[i] == "--image"
    )
    assert label_of(build) in image
    assert has_flag(update, "-a", "jsa-example") or "--app=jsa-example" in update
    assert pick_machine(all_calls, "run") == []
    assert all_calls.index(build) < all_calls.index(update)


def test_update_retries_while_the_registry_catches_up(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_UPDATE_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    updates = pick_machine(calls(log), "update")
    assert len(updates) == 3
    for update in updates:
        assert has_flag(update, "--schedule", "hourly")


def test_update_that_never_succeeds_exits_nonzero(fly, monkeypatch, capsys):
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_UPDATE_FAILS", "1000")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "image not found" in output or "update" in output


def test_more_than_one_hourly_machine_is_an_error_and_updates_none(
    fly, monkeypatch, capsys
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly", "hourly"))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "m0" in output and "m1" in output
    assert pick_machine(calls(log), "update") == []
    assert pick_machine(calls(log), "run") == []


def test_a_failed_build_stops_before_touching_machines(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_FAIL", "deploy")
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert pick_machine(calls(log), "update") == []
    assert pick_machine(calls(log), "run") == []


def test_machine_listing_that_is_not_a_list_exits_nonzero_without_changes(
    fly, monkeypatch, capsys
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", "this is not json")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert pick_machine(calls(log), "update") == []
    assert pick_machine(calls(log), "run") == []


def test_every_fly_call_uses_the_configured_app(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    for argv in calls(log):
        assert has_flag(argv, "-a", "jsa-example") or "--app=jsa-example" in argv


def test_app_and_region_come_from_the_profile(fly, monkeypatch, capsys):
    profile, log = fly
    text = (REPO_ROOT / "profile.example" / "config.toml").read_text()
    text = text.replace("jsa-example", "my-own-app").replace('"iad"', '"ams"')
    write_config_toml(profile, text)
    monkeypatch.setenv("STUB_MACHINES", machines())
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    for argv in calls(log):
        assert has_flag(argv, "-a", "my-own-app") or "--app=my-own-app" in argv
    (run,) = pick_machine(calls(log), "run")
    assert has_flag(run, "--region", "ams")


def test_deploy_never_runs_fly_secrets(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    for flags in ((), ("--smoke",), ("--dry-run",)):
        assert jsa_deploy(monkeypatch, capsys, *flags)[0] == 0
    for argv in secret_calls(log):
        assert "list" in argv
        assert not {"set", "import", "unset"} & set(argv)


def test_the_label_changes_between_deploys(fly, monkeypatch, capsys):
    _, log = fly
    labels = set()
    for _ in range(2):
        assert jsa_deploy(monkeypatch, capsys)[0] == 0
    for build in pick(calls(log), "deploy"):
        labels.add(label_of(build))
    assert len(pick(calls(log), "deploy")) == 2
    assert all(re.fullmatch(r"[A-Za-z0-9._-]+", label) for label in labels)


# --- --smoke --------------------------------------------------------------------------


def test_smoke_builds_then_runs_one_ungated_cron_on_a_throwaway_machine(
    fly, monkeypatch, capsys
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    all_calls = calls(log)
    (build,) = pick(all_calls, "deploy")
    (run,) = pick_machine(all_calls, "run")
    assert "--rm" in run
    assert "--ungated" in " ".join(run)
    assert "cron" in " ".join(run)
    assert any(label_of(build) in arg for arg in run)
    assert all_calls.index(build) < all_calls.index(run)


def test_smoke_leaves_the_scheduled_machine_alone(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    all_calls = calls(log)
    assert pick_machine(all_calls, "update") == []
    assert not any(has_flag(argv, "--schedule", "hourly") for argv in all_calls)
    assert not any("m0" in argv for argv in all_calls)


def test_smoke_with_no_scheduled_machine_creates_none(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    (run,) = pick_machine(calls(log), "run")
    assert "--rm" in run
    assert not any("--schedule" in arg for arg in run)


def test_smoke_validates_before_building(fly, monkeypatch, capsys):
    profile, log = fly
    (profile / "search" / "filters.md").unlink()
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code != 0
    assert calls(log) == []


def test_smoke_fails_when_the_machine_run_fails(fly, monkeypatch, capsys):
    monkeypatch.setenv("STUB_FAIL", "machine")
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code != 0


# --- registry lag on every launch of the pushed image -----------------------------------


@pytest.fixture
def naps(monkeypatch):
    """The seconds the app sleeps between launch attempts, however it sleeps."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.setattr("jsa.deploy.sleep", slept.append, raising=False)
    return slept


def attempts_until_giving_up(monkeypatch, capsys, verb: str, *args) -> int:
    """How many `machine <verb>` calls a launch that never succeeds makes before the deploy fails."""
    monkeypatch.setenv("STUB_UPDATE_FAILS", "1000")
    monkeypatch.setenv("STUB_RUN_FAILS", "1000")
    code, _ = jsa_deploy(monkeypatch, capsys, *args)
    assert code != 0
    return len(pick_machine(calls(Path(os.environ["STUB_LOG"])), verb))


def test_create_retries_while_the_registry_catches_up(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    runs = pick_machine(all_calls, "run")
    assert len(runs) == 3
    for run in runs:
        assert has_flag(run, "--schedule", "hourly")
        assert "--rm" not in run
    assert len(pick(all_calls, "deploy")) == 1
    assert pick_machine(all_calls, "update") == []


@pytest.mark.parametrize("creating_attempt", [1, 2], ids=["first", "second"])
def test_a_create_that_left_a_machine_behind_is_not_retried(
    fly, monkeypatch, capsys, creating_attempt
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "1000")
    monkeypatch.setenv("STUB_RUN_CREATES_ON", str(creating_attempt))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "Traceback" not in output
    all_calls = calls(log)
    assert len(pick_machine(all_calls, "run")) == creating_attempt
    assert pick_machine(all_calls, "update") == []


def test_a_create_that_left_a_machine_behind_exits_with_the_failed_command(
    fly, monkeypatch, capsys
):
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_CREATES_ON", "1")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "machine run" in output
    assert "Traceback" not in output


def test_the_next_deploy_after_a_create_that_left_a_machine_behind_updates_it(
    fly, monkeypatch, capsys
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_CREATES_ON", "1")
    assert jsa_deploy(monkeypatch, capsys)[0] != 0
    log.unlink()
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.delenv("STUB_RUN_CREATES_ON")
    Path(str(log) + ".created").unlink()
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert len(pick_machine(calls(log), "update")) == 1
    assert pick_machine(calls(log), "run") == []


def test_create_retries_past_machines_that_are_not_hourly(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines(None, "daily"))
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert len(pick_machine(calls(log), "run")) == 3


def test_smoke_retries_even_when_the_failed_attempt_left_a_machine_behind(
    fly, monkeypatch, capsys
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_CREATES_ON", "1")
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    runs = pick_machine(calls(log), "run")
    assert len(runs) == 2
    assert all("--rm" in run for run in runs)


def test_smoke_retries_while_the_registry_catches_up(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    all_calls = calls(log)
    runs = pick_machine(all_calls, "run")
    assert len(runs) == 3
    for run in runs:
        assert "--rm" in run
        assert "--ungated" in " ".join(run)
    assert len(pick(all_calls, "deploy")) == 1
    assert pick_machine(all_calls, "update") == []


def test_smoke_retry_leaves_a_scheduled_machine_alone(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    all_calls = calls(log)
    assert not any(has_flag(argv, "--schedule", "hourly") for argv in all_calls)
    assert not any("m0" in argv for argv in all_calls)


@pytest.mark.parametrize("smoke", [(), ("--smoke",)], ids=["create", "smoke"])
def test_every_launch_retries_the_image_it_just_pushed(fly, monkeypatch, capsys, smoke):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys, *smoke)
    assert code == 0
    all_calls = calls(log)
    (build,) = pick(all_calls, "deploy")
    runs = pick_machine(all_calls, "run")
    assert all(label_of(build) in " ".join(run) for run in runs)
    assert all(run == runs[0] for run in runs)


def test_create_gives_up_after_as_many_attempts_as_update(fly, monkeypatch, capsys):
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    update_attempts = attempts_until_giving_up(monkeypatch, capsys, "update")
    assert update_attempts > 1
    Path(os.environ["STUB_LOG"]).unlink()
    for suffix in (".updates", ".runs"):
        Path(os.environ["STUB_LOG"] + suffix).unlink(missing_ok=True)
    monkeypatch.setenv("STUB_MACHINES", machines())
    assert attempts_until_giving_up(monkeypatch, capsys, "run") == update_attempts


def test_smoke_gives_up_after_as_many_attempts_as_update(fly, monkeypatch, capsys):
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    update_attempts = attempts_until_giving_up(monkeypatch, capsys, "update")
    Path(os.environ["STUB_LOG"]).unlink()
    for suffix in (".updates", ".runs"):
        Path(os.environ["STUB_LOG"] + suffix).unlink(missing_ok=True)
    assert (
        attempts_until_giving_up(monkeypatch, capsys, "run", "--smoke")
        == update_attempts
    )


@pytest.mark.parametrize("smoke", [(), ("--smoke",)], ids=["create", "smoke"])
def test_a_launch_that_succeeds_on_the_last_attempt_deploys(
    fly, monkeypatch, capsys, smoke
):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines())
    attempts = attempts_until_giving_up(monkeypatch, capsys, "run", *smoke)
    log.unlink()
    Path(str(log) + ".runs").unlink()
    monkeypatch.setenv("STUB_RUN_FAILS", str(attempts - 1))
    code, _ = jsa_deploy(monkeypatch, capsys, *smoke)
    assert code == 0
    assert len(pick_machine(calls(log), "run")) == attempts


@pytest.mark.parametrize("smoke", [(), ("--smoke",)], ids=["create", "smoke"])
def test_a_launch_that_never_succeeds_exits_nonzero_with_the_fly_error(
    fly, monkeypatch, capsys, smoke
):
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "1000")
    code, output = jsa_deploy(monkeypatch, capsys, *smoke)
    assert code != 0
    assert "machine run" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("smoke", [(), ("--smoke",)], ids=["create", "smoke"])
def test_launch_retries_wait_the_same_as_update_retries(
    fly, monkeypatch, capsys, naps, smoke
):
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_UPDATE_FAILS", "2")
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    update_naps = list(naps)
    assert update_naps
    naps.clear()
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    assert jsa_deploy(monkeypatch, capsys, *smoke)[0] == 0
    assert naps == update_naps


def test_a_first_try_launch_does_not_wait(fly, monkeypatch, capsys, naps):
    monkeypatch.setenv("STUB_MACHINES", machines())
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    assert naps == []


def test_a_missing_fly_binary_is_a_clean_error(fly, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("JSA_FLY_BIN", str(tmp_path / "no-such-fly"))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "Traceback" not in output


def test_deploy_is_a_jsa_command_with_dry_run_and_smoke(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["jsa", "deploy", "--help"])
    with pytest.raises(SystemExit) as exit_:
        cli.main()
    assert exit_.value.code == 0
    help_text = capsys.readouterr().out
    assert "--dry-run" in help_text
    assert "--smoke" in help_text


# --- the image ------------------------------------------------------------------------


def dockerfile_instructions() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with continuation lines joined and comments dropped."""
    joined = re.sub(r"\\\n", " ", (REPO_ROOT / "Dockerfile").read_text())
    instructions = []
    for line in joined.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            keyword, _, rest = line.partition(" ")
            instructions.append((keyword.upper(), rest.strip()))
    return instructions


def test_image_entrypoint_is_jsa_cron_and_nothing_else():
    entrypoints = [a for k, a in dockerfile_instructions() if k == "ENTRYPOINT"]
    assert len(entrypoints) == 1
    assert json.loads(entrypoints[0]) == ["jsa", "cron"]
    assert not [a for k, a in dockerfile_instructions() if k == "CMD"]


def test_image_runs_as_a_non_root_user_with_a_home_directory():
    instructions = dockerfile_instructions()
    users = [a for k, a in instructions if k == "USER"]
    assert users
    assert users[-1].split(":")[0] not in ("root", "0")
    creation = [a for k, a in instructions if k == "RUN" and "useradd" in a]
    assert creation
    assert "--create-home" in creation[0] or "-m" in creation[0].split()


def test_image_is_python_314_with_timezone_data():
    instructions = dockerfile_instructions()
    bases = [a for k, a in instructions if k == "FROM"]
    assert bases[-1].startswith(("python:3.14", "docker.io/library/python:3.14"))
    assert any(k == "RUN" and "tzdata" in a for k, a in instructions)


def test_image_installs_no_dev_dependencies():
    installs = [a for k, a in dockerfile_instructions() if k == "RUN" and "uv " in a]
    assert installs
    assert all("--no-dev" in a for a in installs if "sync" in a)
    assert not any("--dev" in a.split() or "--all-groups" in a for a in installs)


def test_image_sets_no_tz():
    for keyword, args in dockerfile_instructions():
        if keyword in ("ENV", "ARG"):
            assert not re.match(r"TZ\b", args)


def test_image_does_not_copy_the_whole_build_context():
    for keyword, args in dockerfile_instructions():
        if keyword in ("COPY", "ADD") and "--from" not in args:
            sources = [w for w in args.split() if not w.startswith("--")][:-1]
            assert not {"."} & set(sources)


def dockerignore_rules() -> list[tuple[bool, re.Pattern]]:
    rules = []
    for line in (REPO_ROOT / ".dockerignore").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        pattern = line.lstrip("!").strip("/")
        regex = ""
        i = 0
        while i < len(pattern):
            if pattern.startswith("**", i):
                regex += ".*"
                i += 2
                continue
            char = pattern[i]
            regex += {"*": "[^/]*", "?": "[^/]"}.get(char, re.escape(char))
            i += 1
        rules.append((negated, re.compile(f"{regex}$")))
    return rules


def in_build_context(path: str) -> bool:
    """Docker's `.dockerignore` semantics: the last rule matching the path or any parent directory wins."""
    parts = path.split("/")
    parents = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    included = True
    for negated, regex in dockerignore_rules():
        if any(regex.match(candidate) for candidate in parents):
            included = negated
    return included


@pytest.mark.parametrize(
    "path",
    [
        "profile/search/search.toml",
        "profile/search/candidate.md",
        "profile/search/filters.md",
        "profile/search/target_roles.md",
    ],
)
def test_build_context_includes_profile_search(path):
    assert in_build_context(path)


@pytest.mark.parametrize(
    "path",
    [
        "profile/config.toml",
        "profile/resume.docx",
        "profile/refine/rationale.md",
        "profile/refine/candidate.md",
        "profile/notes.md",
        "profile/.hidden",
        "profile/packets/acme/resume.docx",
        "profile/searchlight/secret.md",
        "profile/decisions.json",
    ],
)
def test_build_context_excludes_everything_else_under_profile(path):
    assert not in_build_context(path)


def test_build_context_keeps_the_app_and_drops_secrets():
    assert in_build_context("src/jsa/cli.py")
    assert in_build_context("pyproject.toml")
    assert in_build_context("uv.lock")
    assert not in_build_context(".env")
    assert not in_build_context(".git/config")


# --- the build context ships exactly the validated search profile (issue #52; XC-11, XC-13) ----------

DECOY = "DECOY: a profile that was never validated\n"
SHIPPED_FRAGMENT = "The validated candidate.\n"


def dockerfile_copy_sources() -> list[str]:
    """The project paths the Dockerfile copies into the image, apart from the profile."""
    sources = []
    for keyword, args in dockerfile_instructions():
        if keyword == "COPY" and "--from" not in args:
            words = [w for w in args.split() if not w.startswith("--")]
            sources += [w.removeprefix("./") for w in words[:-1]]
    return [source for source in sources if not source.startswith("profile")]


@pytest.fixture
def project(fly, tmp_path, monkeypatch):
    """A project folder (the Dockerfile's inputs plus a decoy ./profile) as the working directory.

    The validated profile lives elsewhere, as when JSA_PROFILE_DIR points outside the project.
    Returns the validated profile, the decoy ./profile, and the directory the build context is copied to.
    """
    root = tmp_path / "project"
    root.mkdir()
    for name in (*dockerfile_copy_sources(), "Dockerfile", ".dockerignore", "fly.toml"):
        source = REPO_ROOT / name
        if source.is_dir():
            shutil.copytree(
                source, root / name, ignore=shutil.ignore_patterns("__pycache__")
            )
        else:
            shutil.copy2(source, root / name)
    decoy = root / "profile"
    (decoy / "search").mkdir(parents=True)
    (decoy / "search" / "candidate.md").write_text(DECOY)
    (decoy / "search" / "decoy_only.md").write_text(DECOY)
    (decoy / "search" / "search.toml").write_text(DECOY)
    (decoy / "refine").mkdir()
    (decoy / "refine" / "rationale.md").write_text(DECOY)
    (decoy / "config.toml").write_text(DECOY)
    validated, _ = fly
    (validated / "search" / "candidate.md").write_text(SHIPPED_FRAGMENT)
    (validated / "refine").mkdir()
    (validated / "refine" / "rationale.md").write_text("never ships\n")
    snapshot = tmp_path / "context"
    monkeypatch.setenv("STUB_SNAPSHOT", str(snapshot))
    monkeypatch.chdir(root)
    return validated, decoy, snapshot


def tree(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def test_build_context_carries_the_validated_search_profile_not_the_project_profile(
    project, monkeypatch, capsys
):
    validated, _, snapshot = project
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert tree(snapshot / "profile" / "search") == tree(validated / "search")


def test_build_context_holds_nothing_from_the_project_profile(
    project, monkeypatch, capsys
):
    _, _, snapshot = project
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    for path, content in tree(snapshot).items():
        assert content != DECOY.encode(), path
    assert not (snapshot / "profile" / "search" / "decoy_only.md").exists()


def test_build_context_profile_holds_only_search(project, monkeypatch, capsys):
    validated, _, snapshot = project
    (validated / "notes.txt").write_text("private\n")
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    shipped = {
        path.split("/")[1] for path in tree(snapshot) if path.startswith("profile/")
    }
    assert shipped == {"search"}


def test_build_context_ships_nested_search_files(project, monkeypatch, capsys):
    validated, _, snapshot = project
    (validated / "search" / "extra").mkdir()
    (validated / "search" / "extra" / "more.md").write_text("nested\n")
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    assert (snapshot / "profile" / "search" / "extra" / "more.md").read_text() == (
        "nested\n"
    )


def test_dry_run_lists_the_files_the_build_context_ships(project, monkeypatch, capsys):
    validated, _, snapshot = project
    (validated / "search" / "extra").mkdir()
    (validated / "search" / "extra" / "more.md").write_text("nested\n")
    _, listing = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    for path in tree(snapshot / "profile" / "search"):
        assert Path(path).name in listing
    assert "decoy_only.md" not in listing


def test_smoke_build_context_carries_the_validated_search_profile(
    project, monkeypatch, capsys
):
    validated, _, snapshot = project
    monkeypatch.setenv("STUB_MACHINES", machines())
    assert jsa_deploy(monkeypatch, capsys, "--smoke")[0] == 0
    assert tree(snapshot / "profile" / "search") == tree(validated / "search")


def test_build_context_carries_what_the_dockerfile_copies(project, monkeypatch, capsys):
    _, _, snapshot = project
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    assert (snapshot / "Dockerfile").read_bytes() == (
        REPO_ROOT / "Dockerfile"
    ).read_bytes()
    for source in dockerfile_copy_sources():
        assert (snapshot / source).exists(), source
    assert (snapshot / "src" / "jsa" / "cli.py").is_file()


def test_every_dockerfile_copy_source_is_a_build_input():
    missing = set(dockerfile_copy_sources()) - set(BUILD_INPUTS)
    assert not missing, (
        f"the Dockerfile copies {sorted(missing)}, which deploy never stages"
    )


def test_build_context_leaves_out_secrets_and_dev_files(project, monkeypatch, capsys):
    _, _, snapshot = project
    root = Path.cwd()
    (root / ".env").write_text("SECRET=1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("x\n")
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    for path in tree(snapshot):
        assert in_build_context(path), path


def test_default_profile_dir_ships_its_own_search_profile(project, monkeypatch, capsys):
    _, decoy, snapshot = project
    monkeypatch.delenv("JSA_PROFILE_DIR")
    shutil.rmtree(decoy)
    copy_example(decoy)
    assert jsa_deploy(monkeypatch, capsys)[0] == 0
    assert tree(snapshot / "profile" / "search") == tree(decoy / "search")
    assert not (snapshot / "profile" / "config.toml").exists()


def test_an_invalid_validated_profile_builds_nothing_even_with_a_valid_project_profile(
    project, monkeypatch, capsys
):
    validated, decoy, snapshot = project
    shutil.rmtree(decoy)
    copy_example(decoy)
    (validated / "search" / "filters.md").unlink()
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert not snapshot.exists()


# --- fly.toml -------------------------------------------------------------------------


def keys_everywhere(node) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {k for v in node.values() for k in keys_everywhere(v)}
    if isinstance(node, list):
        return {k for v in node for k in keys_everywhere(v)}
    return set()


def fly_toml() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_fly_toml_carries_no_identity():
    config = fly_toml()
    assert "app" not in config
    assert "primary_region" not in config


def test_fly_toml_has_no_release_block_and_no_schedule():
    config = fly_toml()
    assert "deploy" not in config
    assert "schedule" not in keys_everywhere(config)
    assert "release_command" not in keys_everywhere(config)


def test_fly_toml_sets_1024_mb_of_memory():
    (vm,) = fly_toml()["vm"]
    memory = str(vm["memory"]).lower().replace(" ", "")
    assert memory in ("1024", "1024mb", "1gb")


# --- README and .env.example ----------------------------------------------------------


README = REPO_ROOT / "README.md"


def readme_walkthrough() -> str:
    text = README.read_text()
    match = re.search(
        r"^(#+)\s*Using this for your own search\s*$", text, flags=re.MULTILINE
    )
    assert match, "README.md has no 'Using this for your own search' section"
    rest = text[match.end() :]
    end = re.search(rf"^#{{1,{len(match.group(1))}}}\s", rest, flags=re.MULTILINE)
    return rest[: end.start()] if end else rest


def test_readme_walkthrough_is_ordered_from_fresh_clone_to_deploy():
    section = readme_walkthrough()
    steps = [
        "uv sync",
        ".env",
        "cp -r profile.example profile",
        "jsa init-db",
        "fly auth login",
        "fly apps create",
        "fly secrets import",
        "--stage",
        "jsa deploy --smoke",
    ]
    positions = []
    for step in steps:
        assert step in section, step
        positions.append(section.index(step))
    assert positions == sorted(positions)
    assert re.search(r"jsa deploy(?! --)", section[positions[-1] :])


def staged_filter_keys(section, app_placeholder):
    """Keys in the walkthrough's `grep -E` filter piped to `fly secrets import -a <app_placeholder>`."""
    lines = [
        line
        for line in section.splitlines()
        if "TURSO_DATABASE_URL|" in line
        and re.search(rf"-a {re.escape(app_placeholder)}\s*$", line)
    ]
    return [set(re.findall(r"[A-Z][A-Z_]+(?=[|)])", line)) for line in lines]


def test_readme_stages_only_the_five_cloud_keys_from_env():
    # PRD 06 setup step 3: the search app gets only its five keys, no other credential.
    section = readme_walkthrough()
    (keys,) = staged_filter_keys(section, "<app>")
    assert keys == {
        "TURSO_DATABASE_URL",
        "TURSO_AUTH_TOKEN",
        "JSA_SEARCH_ANTHROPIC_API_KEY",
        "PERPLEXITY_API_KEY",
        "GEMINI_API_KEY",
    }
    assert "fly secrets import --stage" in section


def test_readme_stages_only_the_inbox_keys_to_the_inbox_app():
    # PRD 06 setup step 4: the inbox app gets only its six keys; the walkthrough's
    # filter line, where present, must not leak the search app's other credentials.
    for keys in staged_filter_keys(readme_walkthrough(), "<inbox app>"):
        assert keys == {
            "TURSO_DATABASE_URL",
            "TURSO_AUTH_TOKEN",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "JSA_GWS_CREDENTIALS",
            "JSA_INBOX_GWS_CREDENTIALS",
        }


@pytest.mark.parametrize(
    "item",
    [
        "Turso",
        "Fly",
        "Perplexity",
        "Google AI Studio",
        "Anthropic",
        "TURSO_DATABASE_URL",
        "TURSO_AUTH_TOKEN",
        "PERPLEXITY_API_KEY",
        "GEMINI_API_KEY",
        "JSA_SEARCH_ANTHROPIC_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "Chrome",
        "gws",
        "flyctl",
        "uv",
        "config.toml",
        "resume.docx",
        "Sheet",
    ],
)
def test_readme_setup_inventory_names(item):
    assert item in README.read_text()


def test_readme_states_the_macos_portability_boundary():
    text = README.read_text()
    assert "macOS" in text
    assert re.search(r"\bopen\b", text)


def test_readme_documents_the_local_only_development_path():
    assert "TURSO_DATABASE_URL=file:dev.db" in README.read_text()


def test_readme_warns_not_to_set_both_claude_credentials():
    assert re.search(
        r"never both|exactly one|not both", readme_walkthrough(), re.IGNORECASE
    )


ENV_VARIABLES = (
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "PERPLEXITY_API_KEY",
    "GEMINI_API_KEY",
    "JSA_SEARCH_ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "JSA_PROFILE_DIR",
    "JSA_GWS_BIN",
    "JSA_FLY_BIN",
    "JSA_GENERATE_WORKERS",
)


def env_example_blocks() -> list[list[str]]:
    blocks, block = [], []
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if line.strip():
            block.append(line)
        elif block:
            blocks.append(block)
            block = []
    if block:
        blocks.append(block)
    return blocks


@pytest.mark.parametrize("variable", ENV_VARIABLES)
def test_env_example_lists_each_variable_with_a_comment(variable):
    assignment = re.compile(rf"^#?\s*{variable}=")
    (block,) = [b for b in env_example_blocks() if any(assignment.match(l) for l in b)]
    prose = [
        l for l in block if l.startswith("#") and not re.match(r"^#\s*[A-Z][A-Z_]+=", l)
    ]
    assert prose, f"{variable} has no explanatory comment"


def test_env_example_never_sets_both_claude_credentials():
    lines = (REPO_ROOT / ".env.example").read_text().splitlines()
    live = {
        "CLAUDE_CODE_OAUTH_TOKEN": False,
        "ANTHROPIC_API_KEY": False,
    }
    for line in lines:
        for name in live:
            if re.match(rf"{name}=\S", line):
                live[name] = True
    assert not all(live.values())


def test_env_example_holds_no_real_looking_secrets():
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        match = re.match(r"([A-Z_]+)=(.+)", line)
        if match:
            assert not re.search(r"sk-|eyJ|AIza", match.group(2))


# --- the inbox machine (issue #89; PRD 06 "Deployment sequence" step 4, "`jsa deploy`" steps 1 and 4) ---

INBOX_APP = "jsa-example-inbox"
INBOX_REQUIRED_SECRETS = (
    "TURSO_DATABASE_URL",
    "TURSO_AUTH_TOKEN",
    "JSA_GWS_CREDENTIALS",
    "JSA_INBOX_GWS_CREDENTIALS",
)
CLAUDE_CREDENTIALS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


def secrets_json(*names: str) -> str:
    return json.dumps([{"name": name} for name in names])


def edit_config(profile: Path, old: str, new: str = "", *, pattern: bool = False):
    path = profile / "config.toml"
    text = path.read_text(encoding="utf-8")
    edited = (
        re.sub(old, new, text, flags=re.MULTILINE)
        if pattern
        else text.replace(old, new)
    )
    assert edited != text, old
    path.write_text(edited, encoding="utf-8")


@pytest.fixture
def inbox(fly, monkeypatch):
    """The `fly` fixture with `[inbox]` set, and the inbox app holding every secret it needs."""
    profile, log = fly
    edit_config(
        profile,
        r"^# (?=\[inbox\]|app\s+=|senders\s+=|drive_folder_id\s+=)",
        pattern=True,
    )
    monkeypatch.setenv(
        "STUB_INBOX_SECRETS",
        secrets_json(*INBOX_REQUIRED_SECRETS, CLAUDE_CREDENTIALS[0]),
    )
    return profile, log


def on_app(all_calls: list[list[str]], app: str) -> list[list[str]]:
    return [
        argv
        for argv in all_calls
        if has_flag(argv, "-a", app) or f"--app={app}" in argv
    ]


def machine_files(argv: list[str]) -> dict[str, str]:
    """Target path in the machine -> local source, from every `--file-local` option."""
    values = []
    for index, arg in enumerate(argv):
        if arg == "--file-local" and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif arg.startswith("--file-local="):
            values.append(arg.partition("=")[2])
    return dict(value.split("=", 1) for value in values)


def launched_nothing(all_calls: list[list[str]]) -> bool:
    return (
        not pick(all_calls, "deploy")
        and not pick_machine(all_calls, "run")
        and not pick_machine(all_calls, "update")
    )


@pytest.mark.parametrize("tool", ["gws", "pandoc", "typst"])
def test_image_adds_each_inbox_tool_at_a_pinned_version(tool):
    text = (REPO_ROOT / "Dockerfile").read_text()
    assert tool in text
    assert re.search(rf"{tool}\S*\s*[=-]\s*v?\d+\.\d+", text, re.IGNORECASE), tool
    assert "latest" not in text.lower()


@pytest.mark.parametrize("existing", [(), ("hourly",)], ids=["create", "update"])
def test_inbox_machine_runs_jsa_inbox_with_its_files_in_the_inbox_app(
    inbox, monkeypatch, capsys, existing
):
    profile, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines(*existing))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    verb = "update" if existing else "run"
    (build,) = pick(all_calls, "deploy")
    (launch,) = pick_machine(on_app(all_calls, INBOX_APP), verb)
    assert has_flag(launch, "--entrypoint", "jsa inbox")
    assert has_flag(launch, "--schedule", "hourly")
    assert has_flag(launch, "--vm-memory", "1024")
    assert "--rm" not in launch
    assert any(label_of(build) in arg for arg in launch)
    files = machine_files(launch)
    assert {Path(target).name: source for target, source in files.items()} == {
        "config.toml": str(profile / "config.toml"),
        "resume.docx": str(profile / "resume.docx"),
    }
    assert all(Path(target).is_absolute() for target in files)
    assert all(Path(target).parent.name == "profile" for target in files)
    # The search machine is swapped as before: no entrypoint override, no machine files.
    (search,) = pick_machine(on_app(all_calls, "jsa-example"), verb)
    assert "--entrypoint" not in search
    assert not any(arg.startswith("--entrypoint=") for arg in search)
    assert machine_files(search) == {}


def test_each_app_is_swapped_by_its_own_hourly_machine(inbox, monkeypatch, capsys):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines(None, "hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    assert len(pick_machine(on_app(all_calls, "jsa-example"), "run")) == 1
    assert pick_machine(on_app(all_calls, "jsa-example"), "update") == []
    (update,) = pick_machine(on_app(all_calls, INBOX_APP), "update")
    assert "m1" in update
    assert "m0" not in update
    assert pick_machine(on_app(all_calls, INBOX_APP), "run") == []


def test_inbox_machines_without_the_hourly_schedule_do_not_count(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines(None, "daily"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    inbox_calls = on_app(calls(log), INBOX_APP)
    assert len(pick_machine(inbox_calls, "run")) == 1
    assert pick_machine(inbox_calls, "update") == []


def test_more_than_one_hourly_inbox_machine_is_an_error_and_changes_none(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines("hourly", "hourly"))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "m0" in output and "m1" in output
    inbox_calls = on_app(calls(log), INBOX_APP)
    assert pick_machine(inbox_calls, "run") == []
    assert pick_machine(inbox_calls, "update") == []


def test_inbox_create_retries_while_the_registry_catches_up(inbox, monkeypatch, capsys):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    runs = pick_machine(on_app(calls(log), INBOX_APP), "run")
    assert len(runs) == 3
    assert all(has_flag(run, "--entrypoint", "jsa inbox") for run in runs)


def test_an_inbox_create_that_left_a_machine_behind_is_not_retried(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines())
    monkeypatch.setenv("STUB_RUN_CREATES_ON", "1")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert "Traceback" not in output
    assert len(pick_machine(on_app(calls(log), INBOX_APP), "run")) == 1


def test_inbox_update_retries_while_the_registry_catches_up(inbox, monkeypatch, capsys):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines())
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_UPDATE_FAILS", "2")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    updates = pick_machine(on_app(calls(log), INBOX_APP), "update")
    assert len(updates) == 3
    assert all(machine_files(update) for update in updates)


@pytest.mark.parametrize("existing", [(), ("hourly",)], ids=["create", "update"])
def test_config_and_resume_never_enter_the_build_context(
    inbox, monkeypatch, capsys, tmp_path, existing
):
    profile, _ = inbox
    snapshot = tmp_path / "context"
    monkeypatch.setenv("STUB_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("STUB_MACHINES", machines(*existing))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    staged = tree(snapshot)
    assert staged
    resume = (profile / "resume.docx").read_bytes()
    config = (profile / "config.toml").read_bytes()
    assert not [
        name for name in staged if Path(name).name in ("resume.docx", "config.toml")
    ]
    assert resume not in staged.values()
    assert config not in staged.values()


# --- the cover letter as an inbox machine file (issue #112; PRD 06 `jsa deploy` step 4) ---


def held(machine_id: str, *guest_paths: str) -> str:
    """`fly machine list --json` output for one hourly machine already holding these machine files."""
    return json.dumps(
        [
            {
                "id": machine_id,
                "state": "stopped",
                "config": {
                    "schedule": "hourly",
                    "files": [{"guest_path": path} for path in guest_paths],
                },
            }
        ]
    )


@pytest.mark.parametrize("existing", [(), ("hourly",)], ids=["create", "update"])
@pytest.mark.parametrize("name", ["cover_letter.docx", "cover_letter.pdf"])
def test_a_cover_letter_ships_to_the_inbox_machine_as_a_machine_file(
    inbox, monkeypatch, capsys, existing, name
):
    profile, log = inbox
    (profile / name).write_bytes(b"THE COVER LETTER")
    monkeypatch.setenv("STUB_MACHINES", machines(*existing))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    verb = "update" if existing else "run"
    (launch,) = pick_machine(on_app(calls(log), INBOX_APP), verb)
    files = {
        Path(target).name: source
        for target, source in machine_files(launch).items()
        if source
    }
    assert files == {
        "config.toml": str(profile / "config.toml"),
        "resume.docx": str(profile / "resume.docx"),
        name: str(profile / name),
    }
    target = next(t for t in machine_files(launch) if Path(t).name == name)
    assert Path(target).is_absolute() and Path(target).parent.name == "profile"


def test_the_search_machine_never_gets_the_cover_letter(inbox, monkeypatch, capsys):
    profile, log = inbox
    (profile / "cover_letter.docx").write_bytes(b"THE COVER LETTER")
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    (search,) = pick_machine(on_app(calls(log), "jsa-example"), "update")
    assert machine_files(search) == {}


@pytest.mark.parametrize("existing", [(), ("hourly",)], ids=["create", "update"])
def test_the_cover_letter_never_enters_the_build_context(
    inbox, monkeypatch, capsys, tmp_path, existing
):
    profile, _ = inbox
    (profile / "cover_letter.docx").write_bytes(b"A DISTINCTIVE COVER LETTER")
    snapshot = tmp_path / "context"
    monkeypatch.setenv("STUB_SNAPSHOT", str(snapshot))
    monkeypatch.setenv("STUB_MACHINES", machines(*existing))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    staged = tree(snapshot)
    assert staged
    assert not [name for name in staged if Path(name).name.startswith("cover_letter")]
    assert b"A DISTINCTIVE COVER LETTER" not in staged.values()


@pytest.mark.parametrize(
    "path",
    ["profile/cover_letter.docx", "profile/cover_letter.pdf"],
)
def test_build_context_excludes_the_cover_letter(path):
    assert not in_build_context(path)


def test_without_a_cover_letter_the_inbox_machine_gets_only_config_and_resume(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    (launch,) = pick_machine(on_app(calls(log), INBOX_APP), "update")
    assert sorted(Path(target).name for target in machine_files(launch)) == [
        "config.toml",
        "resume.docx",
    ]


def test_a_cover_letter_removed_from_the_profile_leaves_the_inbox_machine(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv(
        "STUB_INBOX_MACHINES",
        held(
            "m0",
            "/app/profile/config.toml",
            "/app/profile/resume.docx",
            "/app/profile/cover_letter.docx",
        ),
    )
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    (update,) = pick_machine(on_app(calls(log), INBOX_APP), "update")
    files = machine_files(update)
    # Fly removes a machine file that is set to an empty source.
    assert files["/app/profile/cover_letter.docx"] == ""
    assert files["/app/profile/config.toml"]
    assert files["/app/profile/resume.docx"]


def test_a_renamed_cover_letter_replaces_the_old_one_on_the_inbox_machine(
    inbox, monkeypatch, capsys
):
    profile, log = inbox
    (profile / "cover_letter.pdf").write_bytes(b"THE COVER LETTER")
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv(
        "STUB_INBOX_MACHINES",
        held(
            "m0",
            "/app/profile/config.toml",
            "/app/profile/resume.docx",
            "/app/profile/cover_letter.docx",
        ),
    )
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    (update,) = pick_machine(on_app(calls(log), INBOX_APP), "update")
    files = machine_files(update)
    assert files["/app/profile/cover_letter.pdf"] == str(profile / "cover_letter.pdf")
    assert files["/app/profile/cover_letter.docx"] == ""


def test_a_kept_cover_letter_is_not_cleared_from_the_inbox_machine(
    inbox, monkeypatch, capsys
):
    profile, log = inbox
    (profile / "cover_letter.docx").write_bytes(b"THE COVER LETTER")
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv(
        "STUB_INBOX_MACHINES", held("m0", "/app/profile/cover_letter.docx")
    )
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    (update,) = pick_machine(on_app(calls(log), INBOX_APP), "update")
    assert machine_files(update)["/app/profile/cover_letter.docx"] == str(
        profile / "cover_letter.docx"
    )


@pytest.mark.parametrize("flags", [(), ("--dry-run",), ("--smoke",)])
def test_more_than_one_cover_letter_aborts_the_deploy_before_building_naming_both(
    inbox, monkeypatch, capsys, flags
):
    profile, log = inbox
    (profile / "cover_letter.docx").write_bytes(b"ONE")
    (profile / "cover_letter.pdf").write_bytes(b"TWO")
    code, output = jsa_deploy(monkeypatch, capsys, *flags)
    assert_aborted_before_building(code, calls(log))
    assert "cover_letter.docx" in output and "cover_letter.pdf" in output


def test_dry_run_lists_the_cover_letter_among_the_inbox_machine_files(
    inbox, monkeypatch, capsys
):
    profile, log = inbox
    (profile / "cover_letter.docx").write_bytes(b"THE COVER LETTER")
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert launched_nothing(calls(log))
    last_image_file = max(output.index(path) for path in shipped_search_files(profile))
    assert output.index("cover_letter.docx") > last_image_file


def test_dry_run_without_a_cover_letter_lists_none(inbox, monkeypatch, capsys):
    _, _ = inbox
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "cover_letter" not in output


# --- validation before building ---


def assert_aborted_before_building(code: int, all_calls: list[list[str]]) -> None:
    assert code != 0
    assert launched_nothing(all_calls)


@pytest.mark.parametrize(
    "flags", [(), ("--dry-run",), ("--smoke",)], ids=["deploy", "dry-run", "smoke"]
)
def test_a_missing_resume_aborts_before_building(inbox, monkeypatch, capsys, flags):
    profile, log = inbox
    (profile / "resume.docx").unlink()
    code, output = jsa_deploy(monkeypatch, capsys, *flags)
    assert_aborted_before_building(code, calls(log))
    assert "resume.docx" in output


def test_an_empty_resume_aborts_before_building(inbox, monkeypatch, capsys):
    profile, log = inbox
    (profile / "resume.docx").write_bytes(b"")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert_aborted_before_building(code, calls(log))
    assert "resume.docx" in output


INBOX_PROFILE_BREAKS = {
    "tracker_spreadsheet_id": (
        r"^tracker_spreadsheet_id\s*=.*\n",
        "tracker_spreadsheet_id",
    ),
    "checklist": (r"^\[agents\.checklist\]\n(?:(?![\[#]).+\n)*", "checklist"),
    "redline": (r"^\[agents\.redline\]\n(?:(?![\[#]).+\n)*", "redline"),
    "inbox_app": (r'^app\s*=\s*"jsa-example-inbox"\n', "app"),
    "drive_folder_id": (r"^drive_folder_id\s*=.*\n", "drive_folder_id"),
    "senders": (r"^senders\s*=.*\n", "senders"),
}


@pytest.mark.parametrize("name", list(INBOX_PROFILE_BREAKS))
@pytest.mark.parametrize("flags", [(), ("--dry-run",)], ids=["deploy", "dry-run"])
def test_a_missing_inbox_profile_value_aborts_before_building(
    inbox, monkeypatch, capsys, name, flags
):
    profile, log = inbox
    pattern, mention = INBOX_PROFILE_BREAKS[name]
    edit_config(profile, pattern, pattern=True)
    code, output = jsa_deploy(monkeypatch, capsys, *flags)
    assert_aborted_before_building(code, calls(log))
    assert mention in output
    assert "config.toml" in output


def test_an_unknown_inbox_key_raises_at_load(inbox, monkeypatch, capsys):
    profile, log = inbox
    edit_config(profile, r"^senders\s*=", 'colour = "blue"\nsenders =', pattern=True)
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert_aborted_before_building(code, calls(log))
    assert "colour" in output
    assert "config.toml" in output


@pytest.mark.parametrize("missing", INBOX_REQUIRED_SECRETS)
@pytest.mark.parametrize(
    "flags", [(), ("--dry-run",), ("--smoke",)], ids=["deploy", "dry-run", "smoke"]
)
def test_a_missing_inbox_secret_aborts_naming_it(
    inbox, monkeypatch, capsys, missing, flags
):
    _, log = inbox
    held = [name for name in INBOX_REQUIRED_SECRETS if name != missing]
    monkeypatch.setenv("STUB_INBOX_SECRETS", secrets_json(*held, CLAUDE_CREDENTIALS[0]))
    code, output = jsa_deploy(monkeypatch, capsys, *flags)
    assert_aborted_before_building(code, calls(log))
    assert missing in output


def test_an_inbox_app_with_no_claude_credential_aborts(inbox, monkeypatch, capsys):
    _, log = inbox
    monkeypatch.setenv("STUB_INBOX_SECRETS", secrets_json(*INBOX_REQUIRED_SECRETS))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert_aborted_before_building(code, calls(log))
    assert all(name in output for name in CLAUDE_CREDENTIALS)


def test_every_missing_inbox_secret_is_named(inbox, monkeypatch, capsys):
    monkeypatch.setenv("STUB_INBOX_SECRETS", "[]")
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code != 0
    assert all(name in output for name in INBOX_REQUIRED_SECRETS)


@pytest.mark.parametrize("credential", CLAUDE_CREDENTIALS)
def test_either_claude_credential_satisfies_the_inbox_check(
    inbox, monkeypatch, capsys, credential
):
    _, log = inbox
    monkeypatch.setenv(
        "STUB_INBOX_SECRETS", secrets_json(*INBOX_REQUIRED_SECRETS, credential)
    )
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    assert pick(calls(log), "deploy")


def test_an_unreadable_secret_listing_aborts_before_building(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_FAIL", "secrets")
    code, _ = jsa_deploy(monkeypatch, capsys)
    assert_aborted_before_building(code, calls(log))


def test_the_inbox_secrets_are_read_from_the_inbox_app_and_never_set(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    for flags in ((), ("--smoke",), ("--dry-run",)):
        assert jsa_deploy(monkeypatch, capsys, *flags)[0] == 0
    inbox_reads = on_app(secret_calls(log), INBOX_APP)
    assert inbox_reads
    for argv in secret_calls(log):
        assert "list" in argv
        assert not {"set", "import", "unset"} & set(argv)


def test_inbox_validation_happens_before_the_build_even_when_only_a_secret_is_missing(
    inbox, monkeypatch, capsys
):
    _, log = inbox
    monkeypatch.setenv("STUB_INBOX_SECRETS", "[]")
    jsa_deploy(monkeypatch, capsys)
    all_calls = calls(log)
    assert pick(all_calls, "deploy") == []


# --- --dry-run and --smoke ---


def test_dry_run_lists_the_inbox_machine_files_after_the_image_files(
    inbox, monkeypatch, capsys
):
    profile, log = inbox
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert launched_nothing(calls(log))
    assert INBOX_APP in output
    last_image_file = max(output.index(path) for path in shipped_search_files(profile))
    assert output.index("config.toml") > last_image_file
    assert output.index("resume.docx") > last_image_file


def shipped_search_files(profile: Path) -> list[str]:
    return [
        path.relative_to(profile).as_posix()
        for path in sorted((profile / "search").rglob("*"))
        if path.is_file()
    ]


def test_dry_run_without_inbox_lists_neither_machine_file(fly, monkeypatch, capsys):
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    assert "resume.docx" not in output
    assert "config.toml" not in output


def test_smoke_leaves_both_scheduled_machines_alone(inbox, monkeypatch, capsys):
    _, log = inbox
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    monkeypatch.setenv("STUB_INBOX_MACHINES", machines("hourly"))
    code, _ = jsa_deploy(monkeypatch, capsys, "--smoke")
    assert code == 0
    all_calls = calls(log)
    assert pick_machine(all_calls, "update") == []
    runs = pick_machine(all_calls, "run")
    assert len(runs) == 1
    assert "--rm" in runs[0]
    assert on_app(runs, INBOX_APP) == []
    assert not machine_files(runs[0])


# --- without [inbox], deploy is as before ---


def test_without_inbox_deploy_touches_only_the_search_app(fly, monkeypatch, capsys):
    _, log = fly
    monkeypatch.setenv("STUB_MACHINES", machines("hourly"))
    code, output = jsa_deploy(monkeypatch, capsys)
    assert code == 0
    all_calls = calls(log)
    assert on_app(all_calls, "jsa-example") == all_calls
    assert not on_app(secret_calls(log), INBOX_APP)
    assert len(pick_machine(all_calls, "update")) == 1
    (update,) = pick_machine(all_calls, "update")
    assert machine_files(update) == {}
    assert "--entrypoint" not in update
    assert "inbox" not in output.lower()


# --- README ---


@pytest.mark.parametrize(
    "item",
    [
        "consent screen",
        "drive.file",
        "<inbox app>",
        "drive_folder_id",
        "JSA_INBOX_GWS_CREDENTIALS",
        "JSA_GWS_CREDENTIALS",
    ],
)
def test_readme_walkthrough_covers_the_inbox_setup(item):
    assert item in readme_walkthrough()
