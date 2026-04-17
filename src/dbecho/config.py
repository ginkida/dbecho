from __future__ import annotations

import os
import re

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib
from dataclasses import dataclass, field
from pathlib import Path

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class DatabaseConfig:
    name: str
    url: str
    description: str = ""


@dataclass
class Settings:
    row_limit: int = 500
    query_timeout: int = 30

    def __post_init__(self) -> None:
        for field_name, value in (
            ("row_limit", self.row_limit),
            ("query_timeout", self.query_timeout),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"[settings].{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"[settings].{field_name} must be greater than 0")


@dataclass
class Config:
    databases: list[DatabaseConfig] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)


_SETTINGS_FIELDS = {f.name for f in Settings.__dataclass_fields__.values()}
_DATABASE_FIELDS = {"url", "description"}


def _expand_env(value: str) -> str:
    """Replace ${VAR} placeholders with environment variable values."""

    def _replace(match: re.Match) -> str:
        var = match.group(1)
        result = os.environ.get(var)
        if result is None:
            raise ValueError(f"Environment variable '{var}' is not set")
        return result

    return _ENV_VAR_RE.sub(_replace, value)


def load_config(path: Path) -> Config:
    text = path.read_text(encoding="utf-8")
    data = tomllib.loads(text)

    raw_settings = data.get("settings", {})
    if not isinstance(raw_settings, dict):
        raise ValueError(
            f"[settings] must be a table, got {type(raw_settings).__name__}"
        )
    unknown = set(raw_settings) - _SETTINGS_FIELDS
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    settings = Settings(**raw_settings)

    raw_databases = data.get("databases", {})
    if not isinstance(raw_databases, dict):
        raise ValueError(
            f"[databases] must be a table, got {type(raw_databases).__name__}"
        )

    databases = []
    for name, db_data in raw_databases.items():
        if not isinstance(db_data, dict):
            raise ValueError(f"[databases.{name}] must be a table")
        unknown = set(db_data) - _DATABASE_FIELDS
        if unknown:
            raise ValueError(
                f"[databases.{name}] has unknown keys: {', '.join(sorted(unknown))}"
            )
        if "url" not in db_data:
            raise ValueError(f"[databases.{name}] missing required 'url' field")
        url = db_data["url"]
        if not isinstance(url, str) or not url.strip():
            raise ValueError(f"[databases.{name}].url must be a non-empty string")
        url = _expand_env(url)
        if not url.strip():
            raise ValueError(
                f"[databases.{name}].url is empty after environment variable expansion"
            )
        description = db_data.get("description", "")
        if not isinstance(description, str):
            raise ValueError(f"[databases.{name}].description must be a string")
        databases.append(
            DatabaseConfig(
                name=name,
                url=url,
                description=description,
            )
        )

    if not databases:
        raise ValueError(
            "No databases configured. Add at least one [databases.<name>] section."
        )

    return Config(databases=databases, settings=settings)


def find_config() -> Path | None:
    candidates = [
        Path.cwd() / "dbecho.toml",
        Path.home() / ".config" / "dbecho" / "config.toml",
        Path.home() / ".dbecho.toml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None
