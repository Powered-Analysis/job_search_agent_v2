"""The user's profile directory (XC-11, PRD 06): typed loading of its two TOML files."""

import os
import re
import tomllib
from collections.abc import Collection
from datetime import date, time
from pathlib import Path
from typing import Annotated, Literal, NamedTuple, Protocol
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
PositiveInt = Annotated[StrictInt, Field(gt=0)]
RESUME_DOCX = "resume.docx"
COVER_LETTER_PREFIX = "cover_letter"
_COVER_LETTER = re.compile(rf"{COVER_LETTER_PREFIX}\..+")
_RUN_AT = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
Effort = Literal["low", "medium", "high", "xhigh", "max"]


def profile_dir() -> Path:
    return Path(os.environ.get("JSA_PROFILE_DIR") or "profile")


class ModelSettings(BaseModel):
    model_config = _STRICT
    model: NonEmptyStr
    effort: Effort


class AgentSettings(ModelSettings):
    """The redline runs unattended on the inbox machine, so they may carry a hard stop (PRD 04)."""

    # XC-14: no default number, because the right one depends on the model and effort the user chose.
    wall_clock_seconds: Annotated[float, Field(strict=True, gt=0)] | None = None


class ScheduledSearch(BaseModel):
    model_config = _STRICT
    agent: Literal["perplexity", "claude", "gemini"]
    window_hours: PositiveInt


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
    claude: ModelSettings | None = None


class Verification(BaseModel):
    model_config = _STRICT
    # No default: the user decides which postings verification admits (XC-5).
    mode: Literal["strict", "best_effort"]


class SearchConfig(BaseModel):
    """`profile/search/search.toml`."""

    model_config = _STRICT
    timezone: NonEmptyStr
    run_at: time
    results_per_run: PositiveInt
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
    redline: AgentSettings | None = None
    refine: ModelSettings | None = None


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


CONFIG_TOML = "config.toml"
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
    return _load(CONFIG_TOML, Config)


def load_search_config() -> SearchConfig:
    return _load(SEARCH_TOML, SearchConfig)


def _require_table[T](table: T | None, relative_path: str, name: str) -> T:
    """A settings table the command needs; a missing one raises naming the file and the example."""
    if table is None:
        raise JsaError(
            f"{profile_dir() / relative_path} has no [{name}] table. {_pointer(relative_path)}"
        )
    return table


def claude_settings(config: SearchConfig) -> ModelSettings:
    return _require_table(config.runners.claude, SEARCH_TOML, "runners.claude")


def tracker_spreadsheet_id() -> str:
    """The tracker Sheet's id; a missing or empty value raises before any row is processed."""
    spreadsheet_id = load_config().tracker_spreadsheet_id
    if not spreadsheet_id:
        raise JsaError(
            f"tracker_spreadsheet_id is missing or empty in {profile_dir() / CONFIG_TOML}. "
            f"{_pointer(CONFIG_TOML)}"
        )
    return spreadsheet_id


def _resume_in(folder: Path, relative_path: str) -> Path:
    """`resume.docx` in a folder of the profile; a missing or empty file raises before any row is processed."""
    path = folder / RESUME_DOCX
    if not path.is_file() or path.stat().st_size == 0:
        raise JsaError(f"{path} is missing or empty. {_pointer(relative_path)}")
    return path


def _cover_letter_in(folder: Path, relative_path: str) -> Path | None:
    """The optional cover letter (XC-11); more than one match raises, since the app won't guess which is meant."""
    matches = sorted(
        path
        for path in folder.glob(f"{COVER_LETTER_PREFIX}.*")
        if path.is_file() and _COVER_LETTER.fullmatch(path.name)
    )
    if len(matches) > 1:
        raise JsaError(
            f"{folder} holds more than one cover letter: "
            f"{', '.join(path.name for path in matches)}. Keep one. {_pointer(relative_path)}"
        )
    return matches[0] if matches else None


ROLE_FAMILIES_DIR = "role_families"
_NOT_ALNUM = re.compile(r"[\W_]+")


def role_family_tag(name: str) -> str:
    """The email tag a role family answers to (PRD 03): lowercase, each run of other characters one `-`, none at either end."""
    return _NOT_ALNUM.sub("-", name.lower()).strip("-")


class RoleFamily(NamedTuple):
    """A kind of role the user applies to: a folder under `role_families/` named for it (PRD 04)."""

    name: str
    resume: Path
    cover_letter: Path | None


def role_families() -> list[RoleFamily]:
    """Every role family in name order (PRD 04); an unusable profile raises before any row is processed."""
    root = profile_dir() / ROLE_FAMILIES_DIR
    folders = sorted(root.iterdir()) if root.is_dir() else []
    folders = [folder for folder in folders if folder.is_dir()]
    if not folders:
        raise JsaError(
            f"{root} holds no role family. {_pointer(f'{ROLE_FAMILIES_DIR}/')}"
        )
    families = [
        RoleFamily(
            folder.name,
            _resume_in(folder, f"{ROLE_FAMILIES_DIR}/<name>/{RESUME_DOCX}"),
            _cover_letter_in(folder, f"{ROLE_FAMILIES_DIR}/<name>/"),
        )
        for folder in folders
    ]
    owners: dict[str, str] = {}
    for family in families:
        tag = role_family_tag(family.name)
        if tag in owners:
            raise JsaError(
                f"{root / owners[tag]} and {root / family.name} share the email tag "
                f"{tag!r}. Rename one. {_pointer(f'{ROLE_FAMILIES_DIR}/')}"
            )
        owners[tag] = family.name
    return families


RESUME_SKILLS = "resume_skills.md"


def resume_skills() -> Path | None:
    """The optional skills list (XC-11), or None when the profile has none."""
    path = profile_dir() / RESUME_SKILLS
    return path if path.is_file() else None


def role_family_names() -> list[str]:
    """The role families a prompt lists, validated before any prompt."""
    return [family.name for family in role_families()]


class PacketSources(NamedTuple):
    """The role families (by name) each packet starts from, and the skills list the redline draws on."""

    role_families: dict[str, RoleFamily]
    skills: Path | None


def packet_sources() -> PacketSources:
    """Validates the role families, so a bad profile raises before any row is processed."""
    families = {family.name: family for family in role_families()}
    return PacketSources(families, resume_skills())


class HasRoleFamily(Protocol):
    id: int
    role_family: str | None


def unassigned_reason(
    posting_id: int, role_family: str | None, known: Collection[str]
) -> str | None:
    """Why the profile can't serve a posting's role family, pointing to `jsa revise` (PRD 04); None when it can."""
    if role_family in known:
        return None
    problem = (
        "no role family"
        if role_family is None
        else f"role family {role_family!r} is not in the profile"
    )
    return f"{problem}; `jsa revise --id {posting_id}` gives it one"


def with_role_family[Row: HasRoleFamily](
    rows: list[Row], known: Collection[str]
) -> list[Row]:
    """The rows whose role family the profile has; each other row is flagged and dropped, so it stays queued (PRD 04)."""
    kept = []
    for row in rows:
        if reason := unassigned_reason(row.id, row.role_family, known):
            print(f"flagged: posting {row.id}: {reason}")
        else:
            kept.append(row)
    return kept


def _agent_settings[T: ModelSettings](settings: T | None, name: str) -> T:
    """An agent's model and effort (XC-14); a missing table raises before any model call."""
    return _require_table(settings, CONFIG_TOML, f"agents.{name}")


def fly_settings(config: Config) -> FlyConfig:
    """The Fly app and region `jsa deploy` passes to `fly`; a missing table raises before any build."""
    return _require_table(config.fly, CONFIG_TOML, "fly")


def inbox_settings(config: Config) -> InboxConfig:
    """The `[inbox]` table `jsa inbox` reads; a missing table raises before any mail is read."""
    return _require_table(config.inbox, CONFIG_TOML, "inbox")


def _inbox_key(config: Config, key: str) -> str:
    """A required `[inbox]` key; a missing one raises naming the file."""
    value = getattr(inbox_settings(config), key)
    if not value:
        raise JsaError(
            f"[inbox] {key} is missing in {profile_dir() / CONFIG_TOML}. "
            f"{_pointer(CONFIG_TOML)}"
        )
    return value


def inbox_app(config: Config) -> str:
    """The inbox's Fly app, which `jsa deploy` ships the inbox machine to."""
    return _inbox_key(config, "app")


def inbox_drive_folder(config: Config) -> str:
    """The Drive packets folder's id; a missing one raises before any packet is built."""
    return _inbox_key(config, "drive_folder_id")


def redline_settings(config: Config) -> AgentSettings:
    return _agent_settings(config.agents.redline, "redline")


def refine_settings(config: Config) -> ModelSettings:
    return _agent_settings(config.agents.refine, "refine")
