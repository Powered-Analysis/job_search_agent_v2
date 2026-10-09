"""The user's profile directory (XC-11, PRD 06): typed loading of its two TOML files."""

import os
import re
import tomllib
from datetime import date, time
from pathlib import Path
from typing import Annotated, Literal, NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from jsa.errors import JsaError

# An unknown key raises, so a typo never silently falls back to a default.
_STRICT = ConfigDict(extra="forbid", frozen=True)
# XC-14: model and agent are checked only for being non-empty, never against a list of allowed values.
NonEmptyStr = Annotated[str, StringConstraints(strict=True, min_length=1)]
COVER_LETTER_PREFIX = "cover_letter"
_COVER_LETTER = re.compile(rf"{COVER_LETTER_PREFIX}\..+")
_RUN_AT = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
Effort = Literal["low", "medium", "high", "xhigh", "max"]


def profile_dir() -> Path:
    return Path(os.environ.get("JSA_PROFILE_DIR") or "profile")


class AgentSettings(BaseModel):
    model_config = _STRICT
    model: NonEmptyStr
    effort: Effort


class ScheduledSearch(BaseModel):
    model_config = _STRICT
    agent: Literal["perplexity", "claude", "gemini"]
    window_hours: Annotated[StrictInt, Field(gt=0)]


class Schedule(BaseModel):
    """Each weekday's ordered searches; a day left out runs nothing."""

    model_config = _STRICT
    monday: list[ScheduledSearch] = []
    tuesday: list[ScheduledSearch] = []
    wednesday: list[ScheduledSearch] = []
    thursday: list[ScheduledSearch] = []
    friday: list[ScheduledSearch] = []
    saturday: list[ScheduledSearch] = []
    sunday: list[ScheduledSearch] = []

    def searches(self) -> list[ScheduledSearch]:
        return [
            search for day in type(self).model_fields for search in getattr(self, day)
        ]

    def on(self, day: date) -> list[ScheduledSearch]:
        # The fields run Monday to Sunday, the order of `date.weekday()`.
        return getattr(self, tuple(type(self).model_fields)[day.weekday()])


class Runners(BaseModel):
    """Claude is the one search runner with settings; Perplexity and Gemini are pinned (XC-14)."""

    model_config = _STRICT
    claude: AgentSettings | None = None


class Verification(BaseModel):
    model_config = _STRICT
    # No default: the user decides which postings verification admits (XC-5).
    mode: Literal["strict", "best_effort"]


class SearchConfig(BaseModel):
    """`profile/search/search.toml`."""

    model_config = _STRICT
    timezone: NonEmptyStr
    run_at: time
    schedule: Schedule = Schedule()
    runners: Runners = Runners()
    verification: Verification

    @field_validator("timezone")
    @classmethod
    def _iana_name(cls, name: str) -> str:
        try:
            ZoneInfo(name)
        except ZoneInfoNotFoundError, ValueError:
            raise ValueError(f"{name!r} is not an IANA timezone name") from None
        return name

    @field_validator("run_at", mode="before")
    @classmethod
    def _hh_mm(cls, value: object) -> time:
        match = _RUN_AT.fullmatch(value) if isinstance(value, str) else None
        if not match:
            raise ValueError(f'{value!r} is not a "HH:MM" 24-hour time')
        return time(int(match[1]), int(match[2]))

    @model_validator(mode="after")
    def _scheduled_runners_are_configured(self) -> SearchConfig:
        scheduled = {search.agent for search in self.schedule.searches()}
        if "claude" in scheduled and self.runners.claude is None:
            raise ValueError(
                "claude is scheduled but there is no [runners.claude] table"
            )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class FlyConfig(BaseModel):
    model_config = _STRICT
    app: NonEmptyStr
    region: NonEmptyStr


class InboxConfig(BaseModel):
    """`[inbox]`: the jobs mailbox's senders, and (for deploy and packets) its app and Drive folder."""

    model_config = _STRICT
    senders: Annotated[list[NonEmptyStr], Field(min_length=1)]
    app: NonEmptyStr | None = None
    drive_folder_id: NonEmptyStr | None = None


class AgentsConfig(BaseModel):
    model_config = _STRICT
    checklist: AgentSettings | None = None
    redline: AgentSettings | None = None
    refine: AgentSettings | None = None


class Config(BaseModel):
    """`profile/config.toml`. Every key is optional here; each command requires its own."""

    model_config = _STRICT
    candidate_name: str | None = None
    tracker_spreadsheet_id: NonEmptyStr | None = None
    # validate_default: pydantic skips validators on defaults, and the default needs its `~` expanded too.
    packets_dir: Path = Field(
        Path("~/Documents/Job Applications"), validate_default=True
    )
    fly: FlyConfig | None = None
    inbox: InboxConfig | None = None
    agents: AgentsConfig = AgentsConfig()

    @field_validator("packets_dir")
    @classmethod
    def _expand_home(cls, path: Path) -> Path:
        return path.expanduser()


SEARCH_TOML = "search/search.toml"


def _pointer(relative_path: str) -> str:
    return f"Copy the shape from profile.example/{relative_path}."


def _load[Model: BaseModel](relative_path: str, model: type[Model]) -> Model:
    path = profile_dir() / relative_path
    pointer = _pointer(relative_path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise JsaError(f"{path} is missing. {pointer}") from None
    except tomllib.TOMLDecodeError as error:
        raise JsaError(f"{path} is not valid TOML: {error}. {pointer}") from None
    try:
        return model.model_validate(raw)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(map(str, problem['loc'])) or 'file'}: "
            f"{problem['msg'].removeprefix('Value error, ')}"
            for problem in error.errors()
        )
        raise JsaError(f"{path} is invalid: {problems}. {pointer}") from None


def load_config() -> Config:
    return _load("config.toml", Config)


def load_search_config() -> SearchConfig:
    return _load(SEARCH_TOML, SearchConfig)


def claude_settings(config: SearchConfig) -> AgentSettings:
    if config.runners.claude is None:
        raise JsaError(
            f"{profile_dir() / SEARCH_TOML} has no [runners.claude] table. "
            f"{_pointer(SEARCH_TOML)}"
        )
    return config.runners.claude


def tracker_spreadsheet_id() -> str:
    """The tracker Sheet's id; a missing or empty value raises before any row is processed."""
    spreadsheet_id = load_config().tracker_spreadsheet_id
    if not spreadsheet_id:
        raise JsaError(
            f"tracker_spreadsheet_id is missing or empty in {profile_dir() / 'config.toml'}. "
            f"{_pointer('config.toml')}"
        )
    return spreadsheet_id


def base_resume() -> Path:
    """The single base resume (XC-11); a missing or empty file raises before any row is processed."""
    path = profile_dir() / "resume.docx"
    if not path.is_file() or path.stat().st_size == 0:
        raise JsaError(f"{path} is missing or empty. {_pointer('resume.docx')}")
    return path


def cover_letter() -> Path | None:
    """The optional cover letter (XC-11); more than one match raises, since the app won't guess which is meant."""
    matches = sorted(
        path
        for path in profile_dir().glob(f"{COVER_LETTER_PREFIX}.*")
        if path.is_file() and _COVER_LETTER.fullmatch(path.name)
    )
    if len(matches) > 1:
        raise JsaError(
            f"{profile_dir()} holds more than one cover letter: "
            f"{', '.join(path.name for path in matches)}. Keep one."
        )
    return matches[0] if matches else None


class PacketSources(NamedTuple):
    """The profile files each packet starts from."""

    resume: Path
    cover_letter: Path | None


def packet_sources() -> PacketSources:
    return PacketSources(base_resume(), cover_letter())


def _agent_settings(config: Config, name: str) -> AgentSettings:
    """An agent's model and effort (XC-14); a missing table raises before any model call."""
    settings = getattr(config.agents, name)
    if settings is None:
        raise JsaError(
            f"{profile_dir() / 'config.toml'} has no [agents.{name}] table. "
            f"{_pointer('config.toml')}"
        )
    return settings


def fly_settings(config: Config) -> FlyConfig:
    """The Fly app and region `jsa deploy` passes to `fly`; a missing table raises before any build."""
    if config.fly is None:
        raise JsaError(
            f"{profile_dir() / 'config.toml'} has no [fly] table. {_pointer('config.toml')}"
        )
    return config.fly


def inbox_settings(config: Config) -> InboxConfig:
    """The `[inbox]` table `jsa inbox` reads; a missing table raises before any mail is read."""
    if config.inbox is None:
        raise JsaError(
            f"{profile_dir() / 'config.toml'} has no [inbox] table. {_pointer('config.toml')}"
        )
    return config.inbox


def _inbox_key(config: Config, key: str) -> str:
    """A required `[inbox]` key; a missing one raises naming the file."""
    value = getattr(inbox_settings(config), key)
    if not value:
        raise JsaError(
            f"[inbox] {key} is missing in {profile_dir() / 'config.toml'}. "
            f"{_pointer('config.toml')}"
        )
    return value


def inbox_app(config: Config) -> str:
    """The inbox's Fly app, which `jsa deploy` ships the inbox machine to."""
    return _inbox_key(config, "app")


def inbox_drive_folder(config: Config) -> str:
    """The Drive packets folder's id; a missing one raises before any packet is built."""
    return _inbox_key(config, "drive_folder_id")


def checklist_settings(config: Config) -> AgentSettings:
    return _agent_settings(config, "checklist")


def redline_settings(config: Config) -> AgentSettings:
    return _agent_settings(config, "redline")


class PacketAgents(NamedTuple):
    """The two agents `jsa generate` runs on each packet."""

    checklist: AgentSettings
    redline: AgentSettings


def packet_agents(config: Config) -> PacketAgents:
    return PacketAgents(checklist_settings(config), redline_settings(config))


def refine_settings(config: Config) -> AgentSettings:
    return _agent_settings(config, "refine")
