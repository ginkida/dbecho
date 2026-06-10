import json

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
