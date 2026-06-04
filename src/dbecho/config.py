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

# Upper bounds are intentional safety guards: row_limit caps client-side fetch
# size, so an operator typo (e.g. row_limit = 100000000) must not silently turn
# every query into a multi-GB pull that can OOM the stdio server.
_MAX_ROW_LIMIT = 1_000_000
_MAX_QUERY_TIMEOUT = 3600  # seconds
_MAX_PROFILE_ROWS = 1_000_000_000


@dataclass
class DatabaseConfig:
    name: str
    url: str
    description: str = ""


@dataclass
class Settings:
    row_limit: int = 500
    query_timeout: int = 30
    # Tables above this row count are refused by the full-table profilers
    # (analyze/anomalies) so a single tool call cannot pin the DB scanning
    # every column. Targeted queries are still available via `query`.
    max_profile_rows: int = 5_000_000
    # Replace values of obviously-sensitive columns (password/token/secret/...)
    # with "<redacted>" in sample/analyze/query output. Harm reduction, not a
    # hermetic control — see README.
    redact_sensitive: bool = True

    def __post_init__(self) -> None:
        for field_name, upper in (
            ("row_limit", _MAX_ROW_LIMIT),
            ("query_timeout", _MAX_QUERY_TIMEOUT),
            ("max_profile_rows", _MAX_PROFILE_ROWS),
        ):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"[settings].{field_name} must be an integer")
            if value <= 0:
                raise ValueError(f"[settings].{field_name} must be greater than 0")
            if value > upper:
                raise ValueError(
                    f"[settings].{field_name} must not exceed {upper:,} (safety guard)"
                )
        if not isinstance(self.redact_sensitive, bool):
            raise ValueError("[settings].redact_sensitive must be a boolean")


@dataclass
class Config:
    databases: list[DatabaseConfig] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)


_SETTINGS_FIELDS = {f.name for f in Settings.__dataclass_fields__.values()}
_DATABASE_FIELDS = {"url", "description"}


def _expand_env(value: str) -> str:
    """Replace ${VAR} placeholders with environment variable values."""

    def _replace(match: re.Match) -> str:
        var = match.group(1).strip()
        if not var:
            raise ValueError("Empty ${} placeholder in URL")
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
