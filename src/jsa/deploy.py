"""`jsa deploy` (PRD 06): validate the search profile as the cloud will, then build the image and swap it onto the scheduled machine."""

import json
import shutil
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, time
from itertools import pairwise
from pathlib import Path
from time import sleep

from jsa.config import fly_bin
from jsa.errors import JsaError
from jsa.profile import (
    FlyConfig,
    Schedule,
    SearchConfig,
    fly_settings,
    load_config,
    load_search_config,
    profile_dir,
)
from jsa.refine import proposal_pending, refine_dir
from jsa.search_prompt import SEARCH_DIR, assemble_search_prompt_for
from jsa.tools import run_tool

MEMORY_MB = "1024"
SCHEDULE = "hourly"
# Fly's registry can lag a push by a few seconds, so a launch right after it may not find the image.
LAUNCH_ATTEMPTS = 5
LAUNCH_RETRY_SECONDS = 5
# A wake that slips past 23:59 lands on the next day and misses the day (PRD 01).
LATEST_SAFE_RUN_AT = time(22, 59)
# What the Dockerfile copies from the build context, apart from the profile.
BUILD_INPUTS = (
    "Dockerfile",
    ".dockerignore",
    "fly.toml",
    "pyproject.toml",
    "uv.lock",
    "src",
)
_WEEK_MINUTES = 7 * 24 * 60
_DAY_MINUTES = 24 * 60


def _clock(minute: int) -> str:
    minute %= _WEEK_MINUTES
    day = list(Schedule.model_fields)[minute // _DAY_MINUTES]
    return f"{day[:3].title()} {minute % _DAY_MINUTES // 60:02d}:{minute % 60:02d}"


def unsearched_gaps(config: SearchConfig) -> list[tuple[int, int]]:
    """Stretches of the week no search window covers, as (start, end) minutes after Monday 00:00 local.

    Each search covers the `window_hours` before its day's `run_at`; the week wraps, so a gap's end
    may pass the week's end. Pure (XC-9).
    """
    run_at = config.run_at.hour * 60 + config.run_at.minute
    spans: list[tuple[int, int]] = []
    for weekday, day in enumerate(Schedule.model_fields):
        for search in getattr(config.schedule, day):
            length = search.window_hours * 60
            if length >= _WEEK_MINUTES:
                return []
            start = (weekday * _DAY_MINUTES + run_at - length) % _WEEK_MINUTES
            spans.append((start, min(start + length, _WEEK_MINUTES)))
            if start + length > _WEEK_MINUTES:
                spans.append((0, start + length - _WEEK_MINUTES))
    if not spans:
        return [(0, _WEEK_MINUTES)]
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    gaps = [(a[1], b[0]) for a, b in pairwise(merged)]
    wrapped = merged[0][0] + _WEEK_MINUTES - merged[-1][1]
    if wrapped > 0:
        gaps.append((merged[-1][1], merged[0][0] + _WEEK_MINUTES))
    return gaps


def deploy_warnings(config: SearchConfig, *, pending: bool) -> list[str]:
    """What the user should know before shipping; none of it stops the deploy. Pure (XC-9)."""
    warnings = []
    gaps = unsearched_gaps(config)
    if gaps:
        spans = ", ".join(
            f"{_clock(start)} to {_clock(end)} ({(end - start) / 60:g}h)"
            for start, end in gaps
        )
        warnings.append(
            f"the schedule never searches these hours ({config.timezone}): {spans}"
        )
    if config.run_at > LATEST_SAFE_RUN_AT:
        warnings.append(
            f"run_at {config.run_at:%H:%M} is after {LATEST_SAFE_RUN_AT:%H:%M}: "
            "a late wake can cross midnight and miss the day"
        )
    if pending:
        warnings.append(
            "a refine proposal is pending and will not ship; accept or reject it first"
        )
    return warnings


def shipped_files() -> list[str]:
    """The profile files the image carries, as the image lays them out."""
    root = profile_dir() / SEARCH_DIR
    return sorted(
        f"profile/{SEARCH_DIR}/{path.relative_to(root).as_posix()}"
        for path in root.rglob("*")
        if path.is_file()
    )


def validate() -> tuple[FlyConfig, list[str]]:
    """Load and assemble exactly as the cloud will (XC-13); raises before any build."""
    config = load_search_config()
    assemble_search_prompt_for(config, "the deploy-time check")
    fly = fly_settings(load_config())
    return fly, deploy_warnings(config, pending=proposal_pending(refine_dir()))


def _fly(args: Sequence[str], app: str, *, capture: bool = False) -> str:
    command = [fly_bin(), *args, "-a", app]
    result = run_tool(command, capture=capture)
    if result.returncode != 0:
        detail = (result.stderr or "").strip().partition("\n")[0]
        subcommand = " ".join(arg for arg in args[:2] if not arg.startswith("-"))
        raise JsaError(
            f"fly {subcommand} exited {result.returncode}"
            + (f": {detail}" if detail else "")
        )
    return result.stdout or ""


def _launch(args: Sequence[str], app: str) -> None:
    """A `fly` call that starts the pushed image, retried for registry lag (PRD 06)."""
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        try:
            _fly(args, app)
            return
        except JsaError:
            if attempt == LAUNCH_ATTEMPTS:
                raise
            sleep(LAUNCH_RETRY_SECONDS)


def _stage_build_context(stage: Path) -> None:
    """Lay out the project with the validated search profile at ./profile/search, wherever it lives (XC-13)."""
    for name in BUILD_INPUTS:
        source = Path(name)
        if source.is_dir():
            shutil.copytree(source, stage / name)
        else:
            shutil.copy2(source, stage / name)
    shutil.copytree(profile_dir() / SEARCH_DIR, stage / "profile" / SEARCH_DIR)


def _build_and_push(app: str) -> str:
    label = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with tempfile.TemporaryDirectory() as stage:
        _stage_build_context(Path(stage))
        _fly(["deploy", "--build-only", "--push", "--image-label", label, stage], app)
    return f"registry.fly.io/{app}:{label}"


def _scheduled_machines(app: str) -> list[str]:
    try:
        machines = json.loads(_fly(["machine", "list", "--json"], app, capture=True))
        return [
            machine["id"]
            for machine in machines
            if machine["config"].get("schedule") == SCHEDULE
        ]
    except ValueError, KeyError, TypeError, AttributeError:
        raise JsaError("fly machine list did not return a list of machines") from None


def _swap_in(image: str, fly: FlyConfig) -> None:
    ids = _scheduled_machines(fly.app)
    if len(ids) > 1:
        raise JsaError(
            f"{len(ids)} machines carry the {SCHEDULE} schedule in {fly.app} "
            f"({', '.join(ids)}). Remove all but one in Fly, then deploy again."
        )
    if not ids:
        _launch(
            [
                "machine",
                "run",
                image,
                "--schedule",
                SCHEDULE,
                "--vm-memory",
                MEMORY_MB,
                "--region",
                fly.region,
            ],
            fly.app,
        )
        print(f"Created the {SCHEDULE} machine from {image}.")
        return
    _launch(
        [
            "machine",
            "update",
            ids[0],
            "--image",
            image,
            "--vm-memory",
            MEMORY_MB,
            "--schedule",
            SCHEDULE,
            "--yes",
        ],
        fly.app,
    )
    print(f"Updated machine {ids[0]} to {image}.")


def _smoke(image: str, fly: FlyConfig) -> None:
    # An ungated cron skips the gate and the daily claim; its postings are real (PRD 06).
    _launch(
        [
            "machine",
            "run",
            image,
            "--rm",
            "--entrypoint",
            "jsa cron --ungated",
            "--vm-memory",
            MEMORY_MB,
            "--region",
            fly.region,
        ],
        fly.app,
    )
    print(f"Smoke machine ran; read its output with `fly logs -a {fly.app}`.")


def deploy(*, dry_run: bool, smoke: bool) -> None:
    fly, warnings = validate()
    print("The search profile is valid.")
    for warning in warnings:
        print(f"warning: {warning}")
    if dry_run:
        print("Files that would ship:")
        for path in shipped_files():
            print(f"  {path}")
        return
    image = _build_and_push(fly.app)
    if smoke:
        _smoke(image, fly)
    else:
        _swap_in(image, fly)
