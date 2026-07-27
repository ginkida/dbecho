import json
import sys

import pytest
from unittest.mock import MagicMock

import dbecho.server as server
from dbecho.db import QueryResult
from dbecho.server import _format_cell, _format_table, _format_size, _to_json


@pytest.fixture
def mock_manager(monkeypatch):
    """Inject a mock DatabaseManager as the server singleton."""
    mgr = MagicMock()
    monkeypatch.setattr(server, "_manager", mgr)
    return mgr


def _table_info(name="t", **kwargs):
    """A TableInfo with only the fields a formatting test cares about."""
    from dbecho.db import TableInfo

    defaults = dict(name=name, comment=None, columns=[], row_count=0, size_bytes=0)
    return TableInfo(**{**defaults, **kwargs})


class TestFormatTable:
    def test_no_columns(self):
        assert _format_table([], []) == "(no columns)"

    def test_no_rows(self):
        assert _format_table(["a", "b"], []) == "(no rows)"

    def test_basic(self):
        result = _format_table(["name", "age"], [["Alice", 30], ["Bob", 25]])
        assert "name" in result
        assert "age" in result
        assert "Alice" in result
        assert "30" in result

    def test_none_values(self):
        result = _format_table(["a"], [[None]])
        assert "NULL" in result

    def test_short_row_padded(self):
        result = _format_table(["a", "b", "c"], [[1]])
        assert "NULL" in result

    def test_long_values_truncated(self):
        long_val = "x" * 100
        result = _format_table(["col"], [[long_val]])
        lines = result.split("\n")
        for line in lines:
            assert len(line) <= 62  # 60 + " |" padding

    def test_separator_alignment(self):
        result = _format_table(["name", "value"], [["a", "b"]])
        lines = result.split("\n")
        assert len(lines) == 3  # header, separator, 1 row
        assert "+-" in lines[1]


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(0) == "0 B"
        assert _format_size(512) == "512 B"

    def test_kilobytes(self):
        assert _format_size(1024) == "1.0 KB"
        assert _format_size(1536) == "1.5 KB"

    def test_megabytes(self):
        assert _format_size(1048576) == "1.0 MB"

    def test_gigabytes(self):
        assert _format_size(1073741824) == "1.0 GB"

    def test_terabytes(self):
        assert _format_size(1099511627776) == "1.0 TB"

    def test_float_input(self):
        assert _format_size(1024.0) == "1.0 KB"


class TestFormatCell:
    def test_none(self):
        assert _format_cell(None) == "NULL"

    def test_bytes_not_dumped(self):
        assert _format_cell(b"\x00\x01\x02") == "<3 bytes>"
        assert _format_cell(memoryview(b"abcd")) == "<4 bytes>"

    def test_json_types(self):
        assert _format_cell({"a": 1}) == '{"a": 1}'
        assert _format_cell([1, 2]) == "[1, 2]"

    def test_huge_cell_capped_early(self):
        # A multi-MB cell must never be measured/padded at full length.
        s = _format_cell("x" * 1_000_000)
        assert len(s) == 200
        assert s.endswith("…")

    def test_truncation_is_marked(self):
        result = _format_table(["col"], [["x" * 100]])
        assert "…" in result


class TestToJson:
    def test_round_trip(self):
        result = QueryResult(
            columns=["id", "name"],
            rows=[[1, "Alice"]],
            row_count=1,
            truncated=True,
        )
        parsed = json.loads(_to_json(result))
        assert parsed["columns"] == ["id", "name"]
        assert parsed["rows"] == [[1, "Alice"]]
        assert parsed["truncated"] is True

    def test_non_json_types_stringified(self):
        from decimal import Decimal
        from datetime import date

        result = QueryResult(
            columns=["d", "n"],
            rows=[[date(2026, 1, 1), Decimal("1.5")]],
            row_count=1,
        )
        parsed = json.loads(_to_json(result))
        assert parsed["rows"] == [["2026-01-01", "1.5"]]


class TestQueryTool:
    def test_value_error_is_friendly(self, mock_manager):
        mock_manager.query.side_effect = ValueError("Only SELECT allowed")
        result = server.query("db", "DELETE FROM x")
        assert result == "Query error: Only SELECT allowed"

    def test_timeout_surfaces_budget(self, mock_manager):
        mock_manager.query.side_effect = TimeoutError(
            "Operation exceeded the 30s query timeout"
        )
        result = server.query("db", "SELECT 1")
        assert result == "Query error: Operation exceeded the 30s query timeout"

    def test_unexpected_error_does_not_leak(self, mock_manager):
        mock_manager.query.side_effect = RuntimeError(
            "dsn=postgres://admin:hunter2@prod-db:5432/secret"
        )
        result = server.query("db", "SELECT 1")
        assert result == "Query error: unexpected failure (check server logs)"
        assert "hunter2" not in result
        assert "prod-db" not in result

    def test_json_format(self, mock_manager):
        mock_manager.query.return_value = QueryResult(
            columns=["id"], rows=[[1]], row_count=1
        )
        parsed = json.loads(server.query("db", "SELECT 1", format="json"))
        assert parsed["rows"] == [[1]]

    def test_unknown_format_rejected(self, mock_manager):
        result = server.query("db", "SELECT 1", format="csv")
        assert "unknown format" in result
        mock_manager.query.assert_not_called()

    def test_truncated_suffix_suggests_offset(self, mock_manager):
        mock_manager.query.return_value = QueryResult(
            columns=["id"], rows=[[1], [2]], row_count=2, truncated=True
        )
        result = server.query("db", "SELECT id FROM t")
        assert "offset=2" in result


class TestSampleTool:
    def test_limit_clamped_high(self, mock_manager):
        mock_manager.get_sample.return_value = QueryResult(
            columns=["id"], rows=[], row_count=0
        )
        server.sample("db", "users", limit=999)
        mock_manager.get_sample.assert_called_once_with("db", "users", 50)

    def test_limit_clamped_low(self, mock_manager):
        mock_manager.get_sample.return_value = QueryResult(
            columns=["id"], rows=[], row_count=0
        )
        server.sample("db", "users", limit=-5)
        mock_manager.get_sample.assert_called_once_with("db", "users", 1)


class TestCompareTool:
    def test_duplicate_databases_deduped(self, mock_manager):
        mock_manager.query.return_value = QueryResult(
            columns=["n"], rows=[[1]], row_count=1
        )
        server.compare("SELECT 1", databases=["a", "a", "b", "a"])
        assert mock_manager.query.call_count == 2

    def test_cap_is_visible_in_output(self, mock_manager):
        # Dropping databases beyond the cap must never be silent.
        mock_manager.query.return_value = QueryResult(
            columns=["n"], rows=[[1]], row_count=1
        )
        many = [f"db{i}" for i in range(25)]
        result = server.compare("SELECT 1", databases=many)
        assert mock_manager.query.call_count == 20
        assert "first 20 of 25" in result

    def test_invalid_sql_rejected_before_any_query(self, mock_manager):
        result = server.compare("DROP TABLE x")
        assert result.startswith("Query error:")
        mock_manager.query.assert_not_called()


class TestHealthTool:
    def test_error_category_shown(self, mock_manager):
        mock_manager.database_names = ["prod"]
        mock_manager.check_connection.return_value = {
            "status": "error",
            "error": "authentication failed",
        }
        result = server.health()
        assert result == "[FAIL] prod: authentication failed"


class TestSchemaTool:
    def test_empty_database_reports_configured_schema(self, mock_manager):
        mock_manager.get_schema.return_value = []
        mock_manager.get_database.return_value.schema = "analytics"
        result = server.schema("events")
        assert result == "No tables found in 'events' (schema 'analytics')."


class TestSummaryTool:
    def test_unexpected_error_does_not_leak(self, mock_manager):
        mock_manager.database_names = ["prod"]
        mock_manager.get_schema.side_effect = RuntimeError(
            'connection to "prod-db.internal" failed for user "admin"'
        )
        result = server.summary()
        assert "unavailable (check server logs)" in result
        assert "admin" not in result
        assert "prod-db.internal" not in result


class TestDescribeTool:
    def test_renders_columns_and_indexes(self, mock_manager):
        from dbecho.db import TableInfo, ColumnInfo

        mock_manager.get_table_schema.return_value = TableInfo(
            name="users",
            comment="User accounts",
            columns=[
                ColumnInfo(
                    name="id",
                    data_type="integer",
                    nullable=False,
                    default=None,
                    is_primary_key=True,
                )
            ],
            row_count=100,
            size_bytes=8192,
        )
        mock_manager.get_indexes.return_value = [
            {
                "table": "users",
                "name": "users_email_key",
                "unique": True,
                "columns": "email",
            }
        ]
        result = server.describe("db", "users")
        assert "users" in result
        assert "id: integer NOT NULL [PK]" in result
        assert "users_email_key UNIQUE (email)" in result

    def test_value_error_is_friendly(self, mock_manager):
        mock_manager.get_table_schema.side_effect = ValueError("Invalid identifier")
        result = server.describe("db", "bad;name")
        assert result.startswith("Describe error:")


class TestFindTool:
    def test_formats_matches_and_skips_empty_dbs(self, mock_manager):
        mock_manager.database_names = ["db1", "db2"]
        mock_manager.find_objects.side_effect = [
            {
                "database": "db1",
                "tables": ["user_emails"],
                "columns": [{"table": "users", "column": "email", "type": "text"}],
                "truncated": False,
            },
            {"database": "db2", "tables": [], "columns": [], "truncated": False},
        ]
        result = server.find("email")
        assert "## db1" in result
        assert "Tables: user_emails" in result
        assert "users.email: text" in result
        assert "## db2" not in result

    def test_no_matches(self, mock_manager):
        mock_manager.database_names = ["db1"]
        mock_manager.find_objects.return_value = {
            "database": "db1",
            "tables": [],
            "columns": [],
            "truncated": False,
        }
        result = server.find("nothing")
        assert "No tables or columns matching 'nothing'" in result

    def test_bad_pattern_fails_once(self, mock_manager):
        mock_manager.database_names = ["db1", "db2"]
        mock_manager.find_objects.side_effect = ValueError(
            "pattern must be a non-empty string"
        )
        result = server.find(" ")
        assert result == "Find error: pattern must be a non-empty string"
        assert mock_manager.find_objects.call_count == 1

    def test_unknown_database(self, mock_manager):
        mock_manager.get_database.side_effect = ValueError(
            "Unknown database 'x'. Available: db1"
        )
        result = server.find("email", database="x")
        assert result.startswith("Find error: Unknown database 'x'")

    def test_single_database_scope(self, mock_manager):
        mock_manager.find_objects.return_value = {
            "database": "db1",
            "tables": ["users"],
            "columns": [],
            "truncated": False,
        }
        result = server.find("user", database="db1")
        mock_manager.find_objects.assert_called_once_with("db1", "user")
        assert "## db1" in result

    def test_unexpected_error_sanitized(self, mock_manager):
        mock_manager.database_names = ["db1"]
        mock_manager.find_objects.side_effect = RuntimeError(
            "password=hunter2 host=10.0.0.1"
        )
        result = server.find("email")
        assert "hunter2" not in result
        assert "unexpected failure" in result

    def test_truncation_note(self, mock_manager):
        mock_manager.database_names = ["db1"]
        mock_manager.find_objects.return_value = {
            "database": "db1",
            "tables": ["t"],
            "columns": [],
            "truncated": True,
        }
        result = server.find("t")
        assert "truncated" in result


class TestExplainTool:
    def test_renders_plan_summary(self, mock_manager):
        mock_manager.explain.return_value = {
            "node_type": "Seq Scan",
            "total_cost": 15.5,
            "estimated_rows": 100,
            "plan": [{"Plan": {"Node Type": "Seq Scan"}}],
        }
        result = server.explain("db", "SELECT 1")
        assert "Seq Scan" in result
        assert "15.5" in result
        assert "100" in result


class TestTrendTool:
    def test_json_format(self, mock_manager):
        mock_manager.get_trend.return_value = QueryResult(
            columns=["period", "count"], rows=[["2026-01", 42]], row_count=1
        )
        parsed = json.loads(server.trend("db", "orders", "created_at", format="json"))
        assert parsed["columns"] == ["period", "count"]
        assert parsed["rows"] == [["2026-01", 42]]

    def test_unknown_format_rejected(self, mock_manager):
        result = server.trend("db", "orders", "created_at", format="csv")
        assert "unknown format" in result
        mock_manager.get_trend.assert_not_called()

    def test_table_format_default(self, mock_manager):
        mock_manager.get_trend.return_value = QueryResult(
            columns=["period", "count"], rows=[["2026-01", 42]], row_count=1
        )
        result = server.trend("db", "orders", "created_at")
        assert "2026-01" in result
        assert "42" in result


class TestMain:
    def test_version_flag_prints_and_exits(self, monkeypatch, capsys):
        # Must short-circuit before config load: leave _manager unset and
        # stub mcp so an accidental fall-through is visible.
        monkeypatch.setattr(server, "_manager", None)
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--version"])

        server.main()

        out = capsys.readouterr().out
        assert out.startswith("dbecho ")
        assert out.strip() != "dbecho"
        mock_mcp.run.assert_not_called()

    def test_check_all_ok_exits_zero(self, mock_manager, monkeypatch, capsys):
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--check"])
        mock_manager.database_names = ["blog", "stats"]
        mock_manager.check_connection.side_effect = [
            {"status": "ok", "version": "PostgreSQL 16.1", "size": "110 MB"},
            {"status": "ok", "version": "PostgreSQL 16.1", "size": "28 MB"},
        ]

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "[OK] blog: PostgreSQL 16.1 | 110 MB" in out
        assert "[OK] stats" in out
        mock_mcp.run.assert_not_called()

    def test_check_failure_exits_nonzero(self, mock_manager, monkeypatch, capsys):
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--check"])
        mock_manager.database_names = ["blog", "stats"]
        mock_manager.check_connection.side_effect = [
            {"status": "ok", "version": "PostgreSQL 16.1", "size": "110 MB"},
            {"status": "error", "error": "connection failed (host unreachable)"},
        ]

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "[OK] blog" in out
        assert "[FAIL] stats: connection failed (host unreachable)" in out
        mock_mcp.run.assert_not_called()

    def test_broken_config_exits_one(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(
            sys, "argv", ["dbecho", "--check", "--config=/nonexistent/dbecho.toml"]
        )

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 1

    def test_version_falls_back_when_metadata_missing(self, monkeypatch, capsys):
        # When the package metadata is absent (e.g. a bare source checkout),
        # --version must print "dbecho unknown" rather than raising.
        from importlib.metadata import PackageNotFoundError

        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(
            server, "_package_version", MagicMock(side_effect=PackageNotFoundError)
        )
        monkeypatch.setattr(sys, "argv", ["dbecho", "--version"])

        server.main()

        assert capsys.readouterr().out.strip() == "dbecho unknown"

    def test_help_flag_prints_usage(self, monkeypatch, capsys):
        # Must short-circuit before config load: no manager, no MCP loop.
        monkeypatch.setattr(server, "_manager", None)
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--help"])

        server.main()

        out = capsys.readouterr().out
        assert "Usage:" in out
        assert "--check" in out
        mock_mcp.run.assert_not_called()

    def test_short_help_flag_prints_usage(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "_manager", None)
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "-h"])

        server.main()

        assert "Usage:" in capsys.readouterr().out
        mock_mcp.run.assert_not_called()

    def test_unknown_flag_exits_two_without_loading_config(self, monkeypatch, capsys):
        # A typo'd flag must not silently start a normal server.
        monkeypatch.setattr(server, "_manager", None)
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--verison"])

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 2
        captured = capsys.readouterr()
        assert "unknown argument: --verison" in captured.err
        assert "Usage:" in captured.err
        assert captured.out == ""
        mock_mcp.run.assert_not_called()
        assert server._manager is None

    def test_config_without_path_exits_two(self, monkeypatch):
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--config"])

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 2

    def test_config_value_is_not_an_unknown_argument(self, monkeypatch):
        # The path after --config must be consumed, not flagged. Reaching the
        # config load (exit 1, not 2) is the proof.
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(
            sys, "argv", ["dbecho", "--config", "/nonexistent/dbecho.toml"]
        )

        with pytest.raises(SystemExit) as exc:
            server.main()

        assert exc.value.code == 1

    def test_help_wins_over_unknown_argument(self, monkeypatch, capsys):
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--bogus", "--help"])

        server.main()

        assert "Usage:" in capsys.readouterr().out

    def test_version_wins_over_check(self, mock_manager, monkeypatch, capsys):
        # --version short-circuits before config load and --check, so no DB is
        # pinged and the MCP loop never starts.
        mock_mcp = MagicMock()
        monkeypatch.setattr(server, "mcp", mock_mcp)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--version", "--check"])

        server.main()

        assert capsys.readouterr().out.startswith("dbecho ")
        mock_manager.check_connection.assert_not_called()
        mock_mcp.run.assert_not_called()


class TestArgvValidation:
    def test_bare_invocation_is_valid(self):
        assert server._argv_error(["dbecho"]) is None

    def test_known_flags_are_valid(self):
        assert server._argv_error(["dbecho", "--version"]) is None
        assert server._argv_error(["dbecho", "--check"]) is None
        assert server._argv_error(["dbecho", "--help"]) is None
        assert server._argv_error(["dbecho", "-h"]) is None

    def test_config_both_spellings_are_valid(self):
        assert server._argv_error(["dbecho", "--config", "/x/dbecho.toml"]) is None
        assert server._argv_error(["dbecho", "--config=/x/dbecho.toml"]) is None

    def test_config_path_is_consumed_not_flagged(self):
        # A path that looks like a flag is still the --config value.
        assert server._argv_error(["dbecho", "--config", "--check"]) is None

    def test_missing_config_value(self):
        assert server._argv_error(["dbecho", "--config"]) == "--config requires a path"
        assert (
            server._argv_error(["dbecho", "--config="]) == "--config= requires a path"
        )

    def test_unknown_single_and_plural(self):
        assert server._argv_error(["dbecho", "--verison"]) == (
            "unknown argument: --verison"
        )
        assert server._argv_error(["dbecho", "--a", "extra"]) == (
            "unknown arguments: --a extra"
        )

    def test_flags_combine_with_config(self):
        argv = ["dbecho", "--config=/x/dbecho.toml", "--check"]
        assert server._argv_error(argv) is None


class TestPartitionedRendering:
    def test_schema_marks_partitioned_parent(self, mock_manager):
        mock_manager.get_schema.return_value = [
            _table_info(name="events", row_count=194_727, is_partitioned=True),
            _table_info(name="projects", row_count=6),
        ]
        mock_manager.schema_truncated.return_value = False
        result = server.schema("analytics")
        assert "## events [partitioned] (~194,727 rows est." in result
        assert "## projects (~6 rows est." in result

    def test_describe_marks_partitioned_parent(self, mock_manager):
        mock_manager.get_table_schema.return_value = _table_info(
            name="events",
            row_count=194_727,
            size_bytes=112_197_632,
            is_partitioned=True,
        )
        mock_manager.get_indexes.return_value = []
        assert "## events [partitioned] (" in server.describe("analytics", "events")


class TestListDatabasesTool:
    def test_lists_names_and_descriptions(self, mock_manager):
        from dbecho.config import DatabaseConfig

        mock_manager.database_names = ["blog", "taylor"]
        mock_manager.get_database.side_effect = [
            DatabaseConfig(name="blog", url="x", description="personal blog"),
            DatabaseConfig(name="taylor", url="x"),
        ]
        result = server.list_databases()
        assert "blog" in result and "personal blog" in result
        assert "taylor" in result

    def test_errors_propagate(self, mock_manager):
        # list_databases is the one tool that does not swallow failures: a
        # broken config must surface, not look like an empty install.
        mock_manager.database_names = ["blog"]
        mock_manager.get_database.side_effect = RuntimeError("config exploded")
        with pytest.raises(RuntimeError, match="config exploded"):
            server.list_databases()


class TestAnalyzeTool:
    def _stats(self, **overrides):
        stats = {
            "database": "blog",
            "table": "posts",
            "row_count": 1234,
            "columns": {
                "views": {
                    "type": "integer",
                    "distinct": 900,
                    "null_count": 4,
                    "null_pct": 0.3,
                    "min": 0,
                    "max": 5000,
                    "avg": 42.5,
                },
                "published_at": {
                    "type": "date",
                    "distinct": 300,
                    "null_count": 0,
                    "null_pct": 0.0,
                    "min": "2024-01-01",
                    "max": "2026-07-01",
                },
                "lang": {
                    "type": "text",
                    "distinct": 2,
                    "null_count": 0,
                    "null_pct": 0.0,
                    "top_values": [
                        {"value": "ru", "count": 800, "pct": 64.8},
                        {"value": "en", "count": 434, "pct": 35.2},
                    ],
                },
            },
        }
        stats.update(overrides)
        return stats

    def test_renders_numeric_temporal_and_top_values(self, mock_manager):
        mock_manager.get_table_stats.return_value = self._stats()
        result = server.analyze("blog", "posts")
        assert "Table: blog.posts" in result
        assert "Rows: 1,234" in result
        assert "min: 0  max: 5000  avg: 42.5" in result  # numeric branch
        assert "range: 2024-01-01 .. 2026-07-01" in result  # temporal branch
        assert "top: ru(800, 64.8%), en(434, 35.2%)" in result
        assert "null: 4 (0.3%)" in result

    def test_top_values_capped_at_five(self, mock_manager):
        many = [{"value": f"v{i}", "count": 1, "pct": 0.1} for i in range(9)]
        mock_manager.get_table_stats.return_value = self._stats(
            columns={
                "tag": {
                    "type": "text",
                    "distinct": 9,
                    "null_count": 0,
                    "null_pct": 0.0,
                    "top_values": many,
                }
            }
        )
        result = server.analyze("blog", "posts")
        assert "v4" in result and "v5" not in result

    def test_skipped_columns_are_reported(self, mock_manager):
        mock_manager.get_table_stats.return_value = self._stats(
            skipped_columns=["embedding", "payload"]
        )
        result = server.analyze("blog", "posts")
        assert "skipped columns after failed probes: embedding, payload" in result

    def test_error_contract(self, mock_manager):
        mock_manager.get_table_stats.side_effect = ValueError("Unknown table 'ghost'")
        assert server.analyze("blog", "ghost") == "Analyze error: Unknown table 'ghost'"

        mock_manager.get_table_stats.side_effect = TimeoutError(
            "exceeded the 30s budget"
        )
        assert "exceeded the 30s budget" in server.analyze("blog", "posts")

        mock_manager.get_table_stats.side_effect = RuntimeError(
            "host=prod-db user=admin password=hunter2"
        )
        out = server.analyze("blog", "posts")
        assert out == "Analyze error: unexpected failure (check server logs)"
        assert "hunter2" not in out and "prod-db" not in out


class TestAnomaliesTool:
    def test_renders_issues(self, mock_manager):
        mock_manager.find_anomalies.return_value = {
            "database": "blog",
            "table": "posts",
            "row_count": 500,
            "anomalies": [
                {
                    "type": "high_null_rate",
                    "column": "summary",
                    "detail": "94.0% NULL (470/500)",
                },
                {
                    "type": "outliers",
                    "column": "views",
                    "detail": "3 values beyond 3 sigma",
                },
            ],
        }
        result = server.anomalies("blog", "posts")
        assert "Anomaly report: blog.posts" in result
        assert "Found 2 issue(s)" in result
        assert "[high_null_rate] summary: 94.0% NULL (470/500)" in result

    def test_clean_table(self, mock_manager):
        mock_manager.find_anomalies.return_value = {
            "database": "blog",
            "table": "tags",
            "row_count": 40,
            "anomalies": [],
        }
        assert "No anomalies detected." in server.anomalies("blog", "tags")

    def test_unexpected_error_does_not_leak(self, mock_manager):
        mock_manager.find_anomalies.side_effect = RuntimeError(
            'connection to "prod-db.internal" failed'
        )
        out = server.anomalies("blog", "posts")
        assert out == "Anomalies error: unexpected failure (check server logs)"
        assert "prod-db.internal" not in out


class TestErdTool:
    def test_renders_tables_pks_and_relationships(self, mock_manager):
        from dbecho.db import ColumnInfo, ForeignKey

        mock_manager.get_schema.return_value = [
            _table_info(
                name="posts",
                row_count=500,
                columns=[
                    ColumnInfo("id", "bigint", False, None, is_primary_key=True),
                    ColumnInfo("title", "text", False, None),
                ],
            ),
            _table_info(name="post_tag", row_count=1200),
        ]
        mock_manager.get_foreign_keys.return_value = [
            ForeignKey("post_tag", "post_id", "posts", "id")
        ]
        result = server.erd("blog")
        assert "[posts] (PK: id) -- ~500 rows (est.)" in result
        assert "[post_tag] -- ~1,200 rows (est.)" in result
        assert "post_tag.post_id -> posts.id" in result

    def test_partitioned_parent_is_marked(self, mock_manager):
        # Every listing that shows a partitioned parent marks it the same way
        # (schema, describe, erd, the summary resource) — an unmarked parent
        # reads as a normal table whose rows are unexplained.
        mock_manager.get_schema.return_value = [
            _table_info(name="events", row_count=12_000, is_partitioned=True)
        ]
        mock_manager.get_foreign_keys.return_value = []
        assert "[events] [partitioned] -- ~12,000 rows (est.)" in server.erd(
            "analytics"
        )

    def test_no_relationships(self, mock_manager):
        mock_manager.get_schema.return_value = [_table_info(name="events")]
        mock_manager.get_foreign_keys.return_value = []
        assert "No foreign key relationships found." in server.erd("analytics")

    def test_unexpected_error_does_not_leak(self, mock_manager):
        mock_manager.get_schema.side_effect = RuntimeError("dsn=postgres://u:p@h/db")
        out = server.erd("blog")
        assert out == "ERD error: unexpected failure (check server logs)"
        assert "postgres://" not in out


class TestSummaryToolRendering:
    def test_renders_totals_and_largest(self, mock_manager):
        from dbecho.config import DatabaseConfig

        mock_manager.database_names = ["blog"]
        mock_manager.get_database.return_value = DatabaseConfig(
            name="blog", url="x", description="personal blog"
        )
        mock_manager.get_schema.return_value = [
            _table_info(name="small", row_count=10, size_bytes=1024),
            _table_info(name="big", row_count=9000, size_bytes=1048576),
        ]
        result = server.summary()
        assert "## blog -- personal blog" in result
        assert "Tables: 2" in result
        assert "Total rows: ~9,010 (est.)" in result
        assert "Largest: big(~9,000), small(~10)" in result

    def test_value_error_is_shown_per_database(self, mock_manager):
        from dbecho.config import DatabaseConfig

        mock_manager.database_names = ["blog"]
        mock_manager.get_database.return_value = DatabaseConfig(name="blog", url="x")
        mock_manager.get_schema.side_effect = ValueError("Unknown database 'blog'")
        assert "Error: Unknown database 'blog'" in server.summary()

    def test_no_databases(self, mock_manager):
        mock_manager.database_names = []
        assert server.summary() == "No databases configured."


class TestResources:
    def test_databases_resource_delegates(self, mock_manager):
        from dbecho.config import DatabaseConfig

        mock_manager.database_names = ["blog"]
        mock_manager.get_database.return_value = DatabaseConfig(name="blog", url="x")
        assert "blog" in server.resource_databases()

    def test_databases_resource_stays_generic_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            server, "list_databases", MagicMock(side_effect=RuntimeError("dsn leak"))
        )
        out = server.resource_databases()
        assert out == "Error loading databases (check server logs)"
        assert "dsn leak" not in out

    def test_schema_resource_delegates(self, mock_manager):
        mock_manager.get_schema.return_value = []
        mock_manager.get_database.return_value.schema = "public"
        assert "No tables found in 'blog'" in server.resource_schema("blog")

    def test_schema_resource_reports_config_errors(self, monkeypatch):
        monkeypatch.setattr(
            server, "schema", MagicMock(side_effect=ValueError("bad config"))
        )
        assert server.resource_schema("blog") == (
            "Error loading schema for 'blog': bad config"
        )

    def test_schema_resource_stays_generic_on_failure(self, monkeypatch):
        monkeypatch.setattr(
            server, "schema", MagicMock(side_effect=RuntimeError("user=admin"))
        )
        out = server.resource_schema("blog")
        assert out == "Error loading schema for 'blog' (check server logs)"
        assert "admin" not in out

    def test_summary_resource_renders(self, mock_manager):
        mock_manager.get_schema.return_value = [
            _table_info(name="posts", row_count=500, size_bytes=1048576),
            _table_info(name="events", row_count=9000, is_partitioned=True),
        ]
        out = server.resource_summary("blog")
        assert "Database: blog" in out
        assert "Tables: 2" in out
        assert "Total rows: ~9,500 (est.)" in out
        assert "- events [partitioned]: ~9,000 rows" in out
        # largest first
        assert out.index("events") < out.index("posts")

    def test_summary_resource_caps_the_listing(self, mock_manager):
        mock_manager.get_schema.return_value = [
            _table_info(name=f"t{i:03d}", row_count=1000 - i) for i in range(80)
        ]
        out = server.resource_summary("blog")
        listed = [line for line in out.splitlines() if line.startswith("  - ")]
        assert len(listed) == server._MAX_RESOURCE_TABLES
        assert "Tables: 80" in out  # the true count is still reported
        assert "30 more tables not listed" in out
        assert "t000" in out and "t079" not in out  # kept the largest

    def test_summary_resource_error_paths(self, mock_manager):
        mock_manager.get_schema.side_effect = ValueError("Unknown database 'ghost'")
        assert server.resource_summary("ghost") == (
            "Error loading summary for 'ghost': Unknown database 'ghost'"
        )

        mock_manager.get_schema.side_effect = RuntimeError("password=hunter2")
        out = server.resource_summary("blog")
        assert out == "Error loading summary for 'blog' (check server logs)"
        assert "hunter2" not in out


class TestPrompts:
    def test_explore_database_names_the_database_and_tools(self):
        out = server.explore_database("blog")
        assert "'blog'" in out
        for tool in ("schema", "summary", "sample", "anomalies"):
            assert tool in out

    def test_compare_databases_prompt(self):
        out = server.compare_databases()
        assert "list_databases" in out and "compare" in out

    def test_data_quality_report_prompt(self):
        out = server.data_quality_report("taylor")
        assert "'taylor'" in out and "anomalies" in out


class TestConfigArgResolution:
    def test_space_separated_config_flag_is_honoured(self, monkeypatch, tmp_path):
        cfg = tmp_path / "custom.toml"
        cfg.write_text(
            '[databases.scratch]\nurl = "postgresql://localhost:5432/scratch"\n'
        )
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(sys, "argv", ["dbecho", "--config", str(cfg)])

        mgr = server._get_manager()

        assert mgr.database_names == ["scratch"]
        # Singleton: a second call must not re-read the file.
        assert server._get_manager() is mgr

    def test_equals_form_config_flag_is_honoured(self, monkeypatch, tmp_path):
        cfg = tmp_path / "custom.toml"
        cfg.write_text('[databases.other]\nurl = "postgresql://localhost:5432/other"\n')
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(sys, "argv", ["dbecho", f"--config={cfg}"])

        assert server._get_manager().database_names == ["other"]

    def test_missing_config_path_is_reported(self, monkeypatch, tmp_path):
        monkeypatch.setattr(server, "_manager", None)
        monkeypatch.setattr(
            sys, "argv", ["dbecho", f"--config={tmp_path / 'nope.toml'}"]
        )
        with pytest.raises(FileNotFoundError, match="Config not found"):
            server._get_manager()


class TestErrorContract:
    """Every tool must turn ValueError into a friendly message, pass a
    TimeoutError's text through (it names the configured budget), and reduce
    anything else to a generic line — raw exception text can embed the DSN,
    host or user. Asserted for all tools at once so a new tool that forgets a
    handler, or a refactor that starts leaking, fails here.
    """

    # (tool, manager method it calls, args)
    CASES = [
        ("schema", "get_schema", ("blog",)),
        ("describe", "get_table_schema", ("blog", "posts")),
        ("query", "query", ("blog", "SELECT 1")),
        ("explain", "explain", ("blog", "SELECT 1")),
        ("analyze", "get_table_stats", ("blog", "posts")),
        ("trend", "get_trend", ("blog", "posts", "created_at")),
        ("anomalies", "find_anomalies", ("blog", "posts")),
        ("sample", "get_sample", ("blog", "posts")),
        ("erd", "get_schema", ("blog",)),
    ]

    LEAKY = 'connection to host "prod-db.internal" failed: user=admin password=hunter2'

    @pytest.mark.parametrize("tool_name,method,args", CASES)
    def test_value_error_is_friendly(self, mock_manager, tool_name, method, args):
        getattr(mock_manager, method).side_effect = ValueError("Unknown table 'ghost'")
        out = getattr(server, tool_name)(*args)
        assert "Unknown table 'ghost'" in out
        assert "error:" in out.lower()

    @pytest.mark.parametrize("tool_name,method,args", CASES)
    def test_timeout_text_reaches_the_agent(
        self, mock_manager, tool_name, method, args
    ):
        getattr(mock_manager, method).side_effect = TimeoutError(
            "Operation exceeded the 30s query timeout"
        )
        assert "exceeded the 30s query timeout" in getattr(server, tool_name)(*args)

    @pytest.mark.parametrize("tool_name,method,args", CASES)
    def test_unexpected_error_never_leaks(self, mock_manager, tool_name, method, args):
        getattr(mock_manager, method).side_effect = RuntimeError(self.LEAKY)
        out = getattr(server, tool_name)(*args)
        assert "check server logs" in out
        for secret in ("prod-db.internal", "admin", "hunter2"):
            assert secret not in out


class TestFindToolFanOut:
    def test_unknown_database_is_reported_once(self, mock_manager):
        mock_manager.get_database.side_effect = ValueError("Unknown database 'ghost'")
        assert server.find("email", database="ghost") == (
            "Find error: Unknown database 'ghost'"
        )
        mock_manager.find_objects.assert_not_called()

    def test_pattern_error_fails_once_not_per_database(self, mock_manager):
        mock_manager.database_names = ["a", "b", "c"]
        mock_manager.find_objects.side_effect = ValueError("pattern is too long")
        out = server.find("x" * 500)
        assert out == "Find error: pattern is too long"
        assert mock_manager.find_objects.call_count == 1

    def test_one_unreachable_database_does_not_sink_the_rest(self, mock_manager):
        mock_manager.database_names = ["good", "broken"]
        mock_manager.find_objects.side_effect = [
            {
                "database": "good",
                "tables": ["emails"],
                "columns": [],
                "truncated": False,
            },
            RuntimeError("connection to prod-db.internal failed"),
        ]
        out = server.find("email")
        assert "## good" in out and "Tables: emails" in out
        assert "## broken" in out and "check server logs" in out
        assert "prod-db.internal" not in out

    def test_timeout_is_per_database(self, mock_manager):
        mock_manager.database_names = ["slow"]
        mock_manager.find_objects.side_effect = TimeoutError("exceeded the 30s budget")
        out = server.find("email")
        assert "## slow" in out and "exceeded the 30s budget" in out

    def test_no_matches_reports_how_many_were_searched(self, mock_manager):
        mock_manager.database_names = ["a", "b"]
        mock_manager.find_objects.return_value = {
            "database": "a",
            "tables": [],
            "columns": [],
            "truncated": False,
        }
        out = server.find("zzz")
        assert out.startswith("No tables or columns matching 'zzz' in 2 database(s).")
        # A partition name that exists can still miss, so say why.
        assert "Partition children are not searched" in out

    def test_truncation_is_flagged(self, mock_manager):
        mock_manager.database_names = ["a"]
        mock_manager.find_objects.return_value = {
            "database": "a",
            "tables": [f"t{i}" for i in range(100)],
            "columns": [],
            "truncated": True,
        }
        assert "match list truncated" in server.find("t")


class TestSchemaTruncationNotice:
    def test_cap_is_announced(self, mock_manager):
        mock_manager.get_schema.return_value = [_table_info(name="t1")]
        mock_manager.schema_truncated.return_value = True
        assert "table list truncated" in server.schema("huge")


class TestCompareErrorBranches:
    def test_per_database_errors_are_isolated(self, mock_manager):
        mock_manager.query.side_effect = [
            QueryResult(columns=["n"], rows=[[1]], row_count=1),
            ValueError("Unknown database 'ghost'"),
            TimeoutError("exceeded the 30s budget"),
            RuntimeError("password=hunter2 host=prod-db"),
        ]
        out = server.compare("SELECT 1", databases=["ok", "ghost", "slow", "broken"])
        assert "## ok" in out and "(1 rows)" in out
        assert "Error: Unknown database 'ghost'" in out
        assert "Error: exceeded the 30s budget" in out
        assert "unexpected failure (check server logs)" in out
        assert "hunter2" not in out and "prod-db" not in out


class TestExplainRendering:
    def test_renders_plan_summary_and_json(self, mock_manager):
        mock_manager.explain.return_value = {
            "node_type": "Seq Scan",
            "total_cost": 1234.5,
            "estimated_rows": 4200,
            "plan": {"Node Type": "Seq Scan", "Relation Name": "posts"},
        }
        out = server.explain("blog", "SELECT * FROM posts")
        assert "Node: Seq Scan" in out
        assert "Estimated total cost: 1234.5" in out
        assert "Estimated rows: 4200" in out
        assert '"Relation Name": "posts"' in out


class TestHealthAndSizeEdges:
    def test_healthy_database_line(self, mock_manager):
        mock_manager.database_names = ["blog"]
        mock_manager.check_connection.return_value = {
            "status": "ok",
            "version": "PostgreSQL 16.1",
            "size": "110 MB",
        }
        out = server.health()
        assert "[OK] blog" in out and "PostgreSQL 16.1" in out and "110 MB" in out

    def test_petabyte_scale_size(self):
        assert _format_size(1024**5) == "1.0 PB"


class TestOmittedColumnsNote:
    def test_analyze_states_the_cap(self, mock_manager):
        mock_manager.get_table_stats.return_value = {
            "database": "wide_db",
            "table": "wide",
            "row_count": 100,
            "column_count": 90,
            "columns": {
                "c0": {"type": "jsonb", "distinct": 5, "null_count": 0, "null_pct": 0.0}
            },
            "omitted_columns": [f"c{i}" for i in range(80, 90)],
        }
        out = server.analyze("wide_db", "wide")
        assert "profiled the first 80 of 90 columns in ordinal order" in out
        assert "10 omitted: c80, c81, c82, c83, c84, … (+5 more)" in out
        assert "Use query for the remaining columns." in out

    def test_anomalies_states_the_cap(self, mock_manager):
        mock_manager.find_anomalies.return_value = {
            "database": "wide_db",
            "table": "wide",
            "row_count": 100,
            "column_count": 82,
            "anomalies": [],
            "omitted_columns": ["c80", "c81"],
        }
        out = server.anomalies("wide_db", "wide")
        assert "checked the first 80 of 82 columns" in out
        assert "2 omitted: c80, c81." in out
        assert "+" not in out.split("omitted:")[1]  # no "+N more" for a short list

    def test_no_note_when_nothing_was_omitted(self, mock_manager):
        mock_manager.find_anomalies.return_value = {
            "database": "blog",
            "table": "tags",
            "row_count": 40,
            "column_count": 4,
            "anomalies": [],
        }
        out = server.anomalies("blog", "tags")
        assert "omitted" not in out

    def test_helper_returns_empty_string_without_omissions(self):
        assert server._omitted_columns_note({"column_count": 3}, "profiled") == ""
