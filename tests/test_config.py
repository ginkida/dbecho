import pytest
from pathlib import Path
from dbecho.config import load_config


def write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "dbecho.toml"
    p.write_text(content, encoding="utf-8")
    return p


def test_load_valid_config(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.blog]
url = "postgres://localhost:5432/blog"
description = "My blog"

[databases.app]
url = "postgres://localhost:5432/app"

[settings]
row_limit = 100
query_timeout = 10
""",
    )
    config = load_config(p)
    assert len(config.databases) == 2
    assert config.databases[0].name == "blog"
    assert config.databases[0].url == "postgres://localhost:5432/blog"
    assert config.databases[0].description == "My blog"
    assert config.databases[1].name == "app"
    assert config.databases[1].description == ""
    assert config.settings.row_limit == 100
    assert config.settings.query_timeout == 10


def test_load_default_settings(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"
""",
    )
    config = load_config(p)
    assert config.settings.row_limit == 500
    assert config.settings.query_timeout == 30


def test_missing_url(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
description = "no url"
""",
    )
    with pytest.raises(ValueError, match="missing required 'url'"):
        load_config(p)


def test_empty_url(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = ""
""",
    )
    with pytest.raises(ValueError, match="non-empty string"):
        load_config(p)


def test_no_databases(tmp_path):
    p = write_toml(
        tmp_path,
        """
[settings]
row_limit = 100
""",
    )
    with pytest.raises(ValueError, match="No databases configured"):
        load_config(p)


def test_unknown_settings(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"

[settings]
row_limit = 100
unknown_key = true
""",
    )
    with pytest.raises(ValueError, match="Unknown settings"):
        load_config(p)


def test_invalid_row_limit(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"

[settings]
row_limit = 0
""",
    )
    with pytest.raises(ValueError, match="row_limit"):
        load_config(p)


def test_invalid_query_timeout(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"

[settings]
query_timeout = -1
""",
    )
    with pytest.raises(ValueError, match="query_timeout"):
        load_config(p)


def test_non_integer_setting_rejected(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"

[settings]
row_limit = true
""",
    )
    with pytest.raises(ValueError, match="integer"):
        load_config(p)


def test_invalid_databases_type(tmp_path):
    p = write_toml(
        tmp_path,
        """
databases = "not a table"
""",
    )
    with pytest.raises(ValueError, match="must be a table"):
        load_config(p)


def test_env_var_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DB_URL", "postgres://localhost/fromenv")
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "${TEST_DB_URL}"
""",
    )
    config = load_config(p)
    assert config.databases[0].url == "postgres://localhost/fromenv"


def test_env_var_partial_expansion(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PASS", "secret123")
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://user:${DB_PASS}@localhost/db"
""",
    )
    config = load_config(p)
    assert config.databases[0].url == "postgres://user:secret123@localhost/db"


def test_env_var_missing(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "${DEFINITELY_NOT_SET_12345}"
""",
    )
    with pytest.raises(ValueError, match="Environment variable"):
        load_config(p)


def test_unknown_database_key_rejected(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"
descreption = "typo"
""",
    )
    with pytest.raises(ValueError, match="unknown keys.*descreption"):
        load_config(p)


def test_non_string_description_rejected(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/db1"
description = 42
""",
    )
    with pytest.raises(ValueError, match="description must be a string"):
        load_config(p)


def test_no_env_var_plain_url(tmp_path):
    p = write_toml(
        tmp_path,
        """
[databases.db1]
url = "postgres://localhost/plain"
""",
    )
    config = load_config(p)
    assert config.databases[0].url == "postgres://localhost/plain"
