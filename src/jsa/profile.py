"""The user's profile directory (XC-11, PRD 06): typed loading of its two TOML files."""

import os
import re
import tomllib
from datetime import date, time
from pathlib import Path
from typing import Annotated, Literal
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


class GeminiRunner(BaseModel):
    model_config = _STRICT
    agent: NonEmptyStr


class Runners(BaseModel):
    model_config = _STRICT
    claude: AgentSettings | None = None
    gemini: GeminiRunner | None = None


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
        for agent in ("claude", "gemini"):
            if agent in scheduled and getattr(self.runners, agent) is None:
                raise ValueError(
                    f"{agent} is scheduled but there is no [runners.{agent}] table"
                )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class FlyConfig(BaseModel):
    model_config = _STRICT
    app: NonEmptyStr
    region: NonEmptyStr


class AgentsConfig(BaseModel):
    model_config = _STRICT
    checklist: AgentSettings | None = None
    refine: AgentSettings | None = None


class Config(BaseModel):
    """`profile/config.toml`. Every key is optional here; each command requires its own."""

    model_config = _STRICT
    candidate_name: str | None = None
    tracker_spreadsheet_id: NonEmptyStr | None = None
    packets_dir: Path = Path("~/Documents/Job Applications")
    fly: FlyConfig | None = None
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


def _runner_table[T](config: SearchConfig, agent: str, table: T | None) -> T:
    if table is None:
        raise JsaError(
            f"{profile_dir() / SEARCH_TOML} has no [runners.{agent}] table. "
            f"{_pointer(SEARCH_TOML)}"
        )
    return table


def claude_settings(config: SearchConfig) -> AgentSettings:
    return _runner_table(config, "claude", config.runners.claude)


def gemini_settings(config: SearchConfig) -> GeminiRunner:
    return _runner_table(config, "gemini", config.runners.gemini)


def base_resume() -> Path:
    """The single base resume (XC-11); a missing or empty file raises before any row is processed."""
    path = profile_dir() / "resume.docx"
    if not path.is_file() or path.stat().st_size == 0:
        raise JsaError(f"{path} is missing or empty. {_pointer('resume.docx')}")
    return path
