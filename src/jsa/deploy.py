"""`jsa deploy` (PRD 06): validate the profile as the cloud will, then build the image and swap it onto the scheduled machines."""

import json
import shutil
import tempfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, time
from itertools import pairwise
from pathlib import Path
from time import sleep

from jsa.config import SEARCH_AGENT_KEYS, fly_bin
from jsa.errors import JsaError
from jsa.profile import (
    Config,
    FlyConfig,
    Schedule,
    SearchConfig,
    base_resume,
    checklist_settings,
    cover_letter,
    fly_settings,
    inbox_app,
    inbox_drive_folder,
    load_config,
    load_search_config,
    profile_dir,
    redline_settings,
    tracker_spreadsheet_id,
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
# The inbox machine's profile files, set as machine files so they never enter the image (XC-11).
# The image's working directory is /app, where the app looks for `profile/`.
MACHINE_PROFILE_DIR = "/app/profile"
INBOX_FILES = ("config.toml", "resume.docx")
INBOX_ENTRYPOINT = "jsa inbox"
DATABASE_SECRETS = ("TURSO_DATABASE_URL", "TURSO_AUTH_TOKEN")
INBOX_SECRETS = (
    *DATABASE_SECRETS,
    "JSA_GWS_CREDENTIALS",
    "JSA_INBOX_GWS_CREDENTIALS",
)
INBOX_CLAUDE_SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
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


def _validate_inbox(config: Config) -> str:
    """The inbox app's name, once everything the inbox machine needs is in the profile."""
    app = inbox_app(config)
    inbox_drive_folder(config)
    base_resume()
    cover_letter()
    tracker_spreadsheet_id()
    checklist_settings(config)
    redline_settings(config)
    return app


def _secret_names(app: str) -> set[str]:
    try:
        secrets = json.loads(_fly(["secrets", "list", "--json"], app, capture=True))
        return {secret["name"] for secret in secrets}
    except ValueError, KeyError, TypeError:
        raise JsaError("fly secrets list did not return a list of secrets") from None


def missing_search_secrets(config: SearchConfig, held: set[str]) -> list[str]:
    """The secrets the schedule needs that the search app doesn't hold: the database's and each scheduled agent's key. Pure (XC-9)."""
    scheduled = dict.fromkeys(search.agent for search in config.schedule.searches())
    needed = [*DATABASE_SECRETS, *(SEARCH_AGENT_KEYS[agent] for agent in scheduled)]
    return [name for name in needed if name not in held]


def _check_search_secrets(app: str, config: SearchConfig) -> None:
    """Aborts when the search app lacks a secret the schedule needs; deploy never sets one."""
    if missing := missing_search_secrets(config, _secret_names(app)):
        raise JsaError(
            f"the search app {app} lacks these secrets: {', '.join(missing)}. "
            "Stage them from .env first (README, set up Fly)."
        )


def _check_inbox_secrets(app: str) -> None:
    """Aborts when the inbox app lacks a secret; deploy never sets one."""
    held = _secret_names(app)
    missing = [name for name in INBOX_SECRETS if name not in held]
    if not held.intersection(INBOX_CLAUDE_SECRETS):
        missing.append(" or ".join(INBOX_CLAUDE_SECRETS))
    if missing:
        raise JsaError(
            f"the inbox app {app} lacks these secrets: {', '.join(missing)}. "
            "Stage them from .env first (README, set up the inbox)."
        )


def validate() -> tuple[FlyConfig, str | None, list[str]]:
    """Load and assemble exactly as the cloud will (XC-13); raises before any build.

    Returns the search app's Fly settings, the inbox app's name (None without `[inbox]`), and warnings.
    """
    config = load_search_config()
    assemble_search_prompt_for(config, "the deploy-time check")
    profile = load_config()
    fly = fly_settings(profile)
    app = _validate_inbox(profile) if profile.inbox is not None else None
    _check_search_secrets(fly.app, config)
    if app is not None:
        _check_inbox_secrets(app)
    return (
        fly,
        app,
        deploy_warnings(config, pending=proposal_pending(refine_dir())),
    )


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


def _launch(
    args: Sequence[str],
    app: str,
    *,
    may_retry: Callable[[], bool] = lambda: True,
) -> None:
    """A `fly` call that starts the pushed image, retried for registry lag (PRD 06).

    `may_retry` is asked after each failure: a failed call can leave work behind, and a retry must not repeat it.
    """
    for attempt in range(1, LAUNCH_ATTEMPTS + 1):
        try:
            _fly(args, app)
            return
        except JsaError:
            if attempt == LAUNCH_ATTEMPTS or not may_retry():
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


def _scheduled_machines(app: str) -> list[dict]:
    try:
        machines = json.loads(_fly(["machine", "list", "--json"], app, capture=True))
        return [
            machine
            for machine in machines
            if machine["config"].get("schedule") == SCHEDULE
        ]
    except ValueError, KeyError, TypeError, AttributeError:
        raise JsaError("fly machine list did not return a list of machines") from None


def _held_files(machine: dict) -> list[str]:
    """The profile files a machine already holds as machine files."""
    return [
        file["guest_path"]
        for file in machine["config"].get("files") or []
        if file["guest_path"].startswith(f"{MACHINE_PROFILE_DIR}/")
    ]


def _swap_in(
    image: str,
    app: str,
    region: str,
    *options: str,
    machine_files: dict[str, Path] | None = None,
) -> None:
    """Create or update the app's one `hourly` machine; `options` go on both the create and the update.

    `machine_files` are all the profile files the machine holds. An update clears any other file the
    machine already holds under the profile, such as a cover letter since removed (PRD 06).
    """
    machines = _scheduled_machines(app)
    ids = [machine["id"] for machine in machines]
    machine_files = machine_files or {}
    files = [
        f"--file-local={target}={source}" for target, source in machine_files.items()
    ]
    if len(ids) > 1:
        raise JsaError(
            f"{len(ids)} machines carry the {SCHEDULE} schedule in {app} "
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
                region,
                *options,
                *files,
            ],
            app,
            # `machine run` can fail after it created the machine, such as on a slow start; a retry would add a second one.
            may_retry=lambda: not _scheduled_machines(app),
        )
        print(f"Created the {SCHEDULE} machine from {image} in {app}.")
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
            *options,
            *files,
            # Fly removes a machine file that is set to an empty path.
            *(
                f"--file-local={held}="
                for held in _held_files(machines[0])
                if held not in machine_files
            ),
        ],
        app,
    )
    print(f"Updated machine {ids[0]} to {image} in {app}.")


def inbox_machine_files() -> dict[str, Path]:
    """Where each inbox machine file lands in the machine, and the profile file it comes from."""
    sources = [profile_dir() / name for name in INBOX_FILES]
    if (letter := cover_letter()) is not None:
        sources.append(letter)
    return {f"{MACHINE_PROFILE_DIR}/{source.name}": source for source in sources}


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
    fly, inbox, warnings = validate()
    print("The search profile is valid.")
    if inbox is not None:
        print(f"The inbox profile and the {inbox} app's secrets are in place.")
    for warning in warnings:
        print(f"warning: {warning}")
    if dry_run:
        print("Files that would ship:")
        for path in shipped_files():
            print(f"  {path}")
        if inbox is not None:
            print(f"Files that would be set on the {inbox} machine:")
            for target, source in inbox_machine_files().items():
                print(f"  {target} (from {source})")
        return
    # The inbox machine runs this same image from the search app's registry; Fly shares it across the organization's apps.
    image = _build_and_push(fly.app)
    if smoke:
        _smoke(image, fly)
        return
    _swap_in(image, fly.app, fly.region)
    if inbox is not None:
        _swap_in(
            image,
            inbox,
            fly.region,
            "--entrypoint",
            INBOX_ENTRYPOINT,
            machine_files=inbox_machine_files(),
        )
