"""`jsa deploy` and the deployment artifacts (issue #17; PRD 06 "Deployment image", "Fly configuration", "`jsa deploy`",
"Entry Point & First-Time Experience", "Configuration surface"; XC-1, XC-11, XC-13, XC-14).

`fly` is replaced by a stub script reached through `JSA_FLY_BIN`, which logs each invocation's argv and
answers `machine list --json` from $STUB_MACHINES; the profile is a temporary copy of `profile.example/`.
No test reaches the network, and none needs the database.
"""

import json
import re
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

# Logs argv to $STUB_LOG, answers a machine listing from $STUB_MACHINES, and fails any call whose
# subcommand is named in $STUB_FAIL (exit 1). `machine update` fails its first $STUB_UPDATE_FAILS
# attempts, as a registry that hasn't caught up with the push would.
STUB = """\
#!{python}
import json, os, sys

argv = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\\n")
words = [a for a in argv if not a.startswith("-")]
if words[:1] and words[0] in os.environ.get("STUB_FAIL", "").split(","):
    print("boom", file=sys.stderr)
    sys.exit(1)
if "list" in words or "ls" in words:
    print(os.environ.get("STUB_MACHINES", "[]"))
    sys.exit(0)
if "update" in words:
    counter = os.environ["STUB_LOG"] + ".updates"
    seen = int(open(counter).read()) if os.path.exists(counter) else 0
    open(counter, "w").write(str(seen + 1))
    if seen < int(os.environ.get("STUB_UPDATE_FAILS", "0")):
        print("Error: image not found", file=sys.stderr)
        sys.exit(1)
"""

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
    for name in ("STUB_FAIL", "STUB_MACHINES", "STUB_UPDATE_FAILS"):
        monkeypatch.delenv(name, raising=False)
    # A retry for registry lag must not slow the suite, whichever way the app sleeps.
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
        '[runners.gemini]\nagent = "deep-research-preview-04-2026"\n', ""
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
        broken('"claude-opus-5-5"', '"claude-future-model-9"').replace(
            "deep-research-preview-04-2026", "deep-research-not-yet-invented"
        ),
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


def test_dry_run_lists_the_search_files_and_runs_no_fly_command(
    fly, monkeypatch, capsys
):
    _, log = fly
    code, output = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0
    for name in (*(f"{f}.md" for f in FRAGMENTS), "search.toml"):
        assert name in output
    assert calls(log) == []


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


def test_dry_run_with_no_fly_binary_installed_still_succeeds(
    fly, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("JSA_FLY_BIN", str(tmp_path / "no-such-fly"))
    code, _ = jsa_deploy(monkeypatch, capsys, "--dry-run")
    assert code == 0


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
    for argv in calls(log):
        assert not any(word.startswith("secret") for word in argv)


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
        "fly secrets set",
        "--stage",
        "jsa deploy --smoke",
    ]
    positions = []
    for step in steps:
        assert step in section, step
        positions.append(section.index(step))
    assert positions == sorted(positions)
    assert re.search(r"jsa deploy(?! --)", section[positions[-1] :])


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
