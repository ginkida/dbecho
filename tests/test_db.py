import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from dbecho.db import (
    DatabaseManager,
    _SAFE_TABLE_RE,
)
from dbecho.config import Config, DatabaseConfig, Settings


def make_manager(**kwargs) -> DatabaseManager:
    config = Config(
        databases=[DatabaseConfig(name="test", url="postgres://localhost/test")],
        settings=Settings(**kwargs),
    )
    return DatabaseManager(config)


def make_mock_cursor(rows_by_query: list[list[tuple]]):
    """Create a mock cursor that returns different results for sequential execute() calls.

    rows_by_query: list of row-lists. Each execute() consumes the next entry.
    """
    cur = MagicMock()
    call_index = [0]

    def on_execute(sql, params=None):
        pass

    def on_fetchone():
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(rows_by_query) and rows_by_query[idx]:
            return rows_by_query[idx][0]
        return None

    def on_fetchall():
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(rows_by_query):
            return rows_by_query[idx]
        return []

    def on_fetchmany(size=None):
        idx = call_index[0]
        call_index[0] += 1
        if idx < len(rows_by_query):
            return rows_by_query[idx][:size] if size else rows_by_query[idx]
        return []

    cur.execute = MagicMock(side_effect=on_execute)
    cur.fetchone = MagicMock(side_effect=on_fetchone)
    cur.fetchall = MagicMock(side_effect=on_fetchall)
    cur.fetchmany = MagicMock(side_effect=on_fetchmany)
    cur.description = [("col1",), ("col2",)]

    return cur, call_index


# ---------------------------------------------------------------------------
# Basic tests (no mocking needed)
# ---------------------------------------------------------------------------


class TestValidateSql:
    def test_select_allowed(self):
        assert DatabaseManager.validate_sql("SELECT 1") == "SELECT 1"

    def test_with_allowed(self):
        result = DatabaseManager.validate_sql(
            "WITH cte AS (SELECT 1) SELECT * FROM cte"
        )
        assert result.startswith("WITH")

    def test_show_allowed(self):
        assert (
            DatabaseManager.validate_sql("SHOW server_version") == "SHOW server_version"
        )

    def test_explain_select_allowed(self):
        result = DatabaseManager.validate_sql("EXPLAIN SELECT 1")
        assert result == "EXPLAIN SELECT 1"

    def test_explain_analyze_select_allowed(self):
        result = DatabaseManager.validate_sql("EXPLAIN ANALYZE SELECT 1")
        assert result == "EXPLAIN ANALYZE SELECT 1"

    def test_explain_analyze_delete_blocked(self):
        with pytest.raises(ValueError, match="only allowed with SELECT"):
            DatabaseManager.validate_sql("EXPLAIN ANALYZE DELETE FROM users")

    def test_explain_analyse_update_blocked(self):
        with pytest.raises(ValueError, match="only allowed with SELECT"):
            DatabaseManager.validate_sql("EXPLAIN ANALYSE UPDATE users SET x=1")

    def test_delete_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("DELETE FROM users")

    def test_insert_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("INSERT INTO users VALUES (1)")

    def test_update_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("UPDATE users SET x=1")

    def test_drop_blocked(self):
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("DROP TABLE users")

    def test_semicolon_blocked(self):
        with pytest.raises(ValueError, match="Multiple statements"):
            DatabaseManager.validate_sql("SELECT 1; DROP TABLE users")

    def test_empty_blocked(self):
        with pytest.raises(ValueError, match="Empty query"):
            DatabaseManager.validate_sql("   ")

    def test_trailing_semicolon_stripped(self):
        assert DatabaseManager.validate_sql("SELECT 1;") == "SELECT 1"

    def test_case_insensitive(self):
        assert DatabaseManager.validate_sql("select 1") == "select 1"

    def test_semicolon_in_string_literal_passes(self):
        assert (
            DatabaseManager.validate_sql("SELECT 'a;b' FROM t") == "SELECT 'a;b' FROM t"
        )

    def test_escaped_quote_in_string_passes(self):
        result = DatabaseManager.validate_sql("SELECT 'he''llo' AS x")
        assert "he''llo" in result

    def test_leading_line_comment_passes(self):
        result = DatabaseManager.validate_sql("-- hello\nSELECT 1")
        assert "SELECT 1" in result

    def test_leading_block_comment_passes(self):
        result = DatabaseManager.validate_sql("/* hello */ SELECT 1")
        assert "SELECT 1" in result

    def test_nested_block_comment_passes(self):
        result = DatabaseManager.validate_sql("/* /* nested */ */ SELECT 1")
        assert "SELECT 1" in result

    def test_dollar_quoted_string_with_semicolon_passes(self):
        result = DatabaseManager.validate_sql("SELECT $$a;b$$ AS x")
        assert "$$a;b$$" in result

    def test_dollar_quoted_tagged_string_passes(self):
        result = DatabaseManager.validate_sql("SELECT $tag$a;b$tag$ AS x")
        assert "$tag$" in result

    def test_explain_paren_analyze_delete_blocked(self):
        with pytest.raises(ValueError, match="only allowed with SELECT"):
            DatabaseManager.validate_sql("EXPLAIN (ANALYZE) DELETE FROM users")

    def test_explain_paren_analyze_buffers_select_passes(self):
        result = DatabaseManager.validate_sql("EXPLAIN (ANALYZE, BUFFERS) SELECT 1")
        assert result.startswith("EXPLAIN")

    def test_explain_paren_format_json_select_passes(self):
        result = DatabaseManager.validate_sql("EXPLAIN (FORMAT JSON) SELECT 1")
        assert result.startswith("EXPLAIN")

    def test_explain_plain_delete_still_allowed(self):
        # Plain EXPLAIN only plans (doesn't execute), so any body is fine.
        result = DatabaseManager.validate_sql("EXPLAIN DELETE FROM users")
        assert result.startswith("EXPLAIN")


class TestIdentifierValidation:
    def test_valid_identifiers(self):
        mgr = make_manager()
        assert mgr._validate_identifier("users") == "users"
        assert mgr._validate_identifier("my_table") == "my_table"
        assert mgr._validate_identifier("_private") == "_private"
        assert mgr._validate_identifier("Table123") == "Table123"

    def test_invalid_identifiers(self):
        mgr = make_manager()
        for bad in [
            "table name",
            "123start",
            "table;drop",
            "",
            'table"name',
            "a-b",
            "a.b",
        ]:
            with pytest.raises(ValueError, match="Invalid identifier"):
                mgr._validate_identifier(bad)


class TestDatabaseLookup:
    def test_get_known(self):
        mgr = make_manager()
        db = mgr.get_database("test")
        assert db.name == "test"

    def test_get_unknown(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Unknown database"):
            mgr.get_database("nonexistent")

    def test_names(self):
        config = Config(
            databases=[
                DatabaseConfig(name="a", url="postgres://localhost/a"),
                DatabaseConfig(name="b", url="postgres://localhost/b"),
            ]
        )
        mgr = DatabaseManager(config)
        assert mgr.database_names == ["a", "b"]


class TestRegex:
    def test_safe_table_regex(self):
        assert _SAFE_TABLE_RE.match("users")
        assert _SAFE_TABLE_RE.match("_private")
        assert _SAFE_TABLE_RE.match("MyTable123")
        assert not _SAFE_TABLE_RE.match("")
        assert not _SAFE_TABLE_RE.match("123abc")
        assert not _SAFE_TABLE_RE.match("has space")
        assert not _SAFE_TABLE_RE.match("semi;colon")
        assert not _SAFE_TABLE_RE.match("quote'mark")
        assert not _SAFE_TABLE_RE.match("dash-name")


# ---------------------------------------------------------------------------
# Mocked database tests
# ---------------------------------------------------------------------------


@contextmanager
def mock_connection(cursor):
    """Patch DatabaseManager._connect to return a mock connection with given cursor."""
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    @contextmanager
    def fake_connect(self, db):
        yield conn

    with patch.object(DatabaseManager, "_connect", fake_connect):
        yield conn


class TestCheckConnection:
    def test_success(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                ("PostgreSQL 16.2, compiled by gcc", "testdb"),
                ("120 MB",),
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.check_connection("test")

        assert result["status"] == "ok"
        assert "PostgreSQL 16.2" in result["version"]
        assert result["database"] == "testdb"
        assert result["size"] == "120 MB"

    def test_size_fails_gracefully(self):
        cur = MagicMock()

        def fake_execute(sql, params=None):
            if isinstance(sql, str) and "pg_size_pretty" in sql:
                raise Exception("permission denied")

        cur.execute = MagicMock(side_effect=fake_execute)
        cur.fetchone = MagicMock(return_value=("PostgreSQL 16.2, compiled", "testdb"))

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        @contextmanager
        def fake_connect(self, db):
            yield conn

        with patch.object(DatabaseManager, "_connect", fake_connect):
            mgr = make_manager()
            result = mgr.check_connection("test")

        assert result["status"] == "ok"
        assert result["size"] == "unknown"

    def test_connection_fails(self):
        @contextmanager
        def fail_connect(self, db):
            raise Exception("connection refused")
            yield  # noqa: F841

        with patch.object(DatabaseManager, "_connect", fail_connect):
            mgr = make_manager()
            result = mgr.check_connection("test")

        assert result["status"] == "error"
        assert "connection refused" in result["error"]


class TestQuery:
    def test_basic_query(self):
        cur = MagicMock()
        cur.description = [("id",), ("name",)]
        cur.fetchmany = MagicMock(return_value=[(1, "Alice"), (2, "Bob")])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.query("test", "SELECT id, name FROM users")

        assert result.columns == ["id", "name"]
        assert result.rows == [[1, "Alice"], [2, "Bob"]]
        assert result.row_count == 2
        assert not result.truncated

    def test_truncation(self):
        cur = MagicMock()
        cur.description = [("id",)]
        # row_limit=2, fetchmany(3) returns 3 rows → truncated
        cur.fetchmany = MagicMock(return_value=[(1,), (2,), (3,)])

        with mock_connection(cur):
            mgr = make_manager(row_limit=2)
            result = mgr.query("test", "SELECT id FROM users")

        assert result.row_count == 2
        assert result.truncated

    def test_no_results(self):
        cur = MagicMock()
        cur.description = None

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.query("test", "SELECT 1")

        assert result.columns == []
        assert result.rows == []
        assert result.row_count == 0


class TestGetSchema:
    def test_schema_with_cache(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            side_effect=[
                # table_rows
                [("users", "User accounts", 100, 8192)],
                # col_rows
                [("users", "id", "integer", "NO", "nextval('users_id_seq')")],
                # pk_set
                [("users", "id")],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            tables = mgr.get_schema("test", use_cache=True)

        assert len(tables) == 1
        assert tables[0].name == "users"
        assert tables[0].comment == "User accounts"
        assert tables[0].row_count == 100
        assert tables[0].size_bytes == 8192
        assert len(tables[0].columns) == 1
        assert tables[0].columns[0].name == "id"
        assert tables[0].columns[0].is_primary_key

        # Second call uses cache (no new DB calls) but returns a fresh copy
        # so callers can't corrupt the cache by mutating the list.
        tables2 = mgr.get_schema("test", use_cache=True)
        assert tables2 is not tables
        assert tables2 == tables
        tables.append("INJECTED")
        tables3 = mgr.get_schema("test", use_cache=True)
        assert "INJECTED" not in tables3

    def test_schema_default_is_fresh(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            side_effect=[
                [("posts", None, 50, 4096)],
                [("posts", "title", "text", "NO", None)],
                [],
                # Second call (no cache)
                [("posts", None, 55, 4096)],
                [("posts", "title", "text", "NO", None)],
                [],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            tables1 = mgr.get_schema("test")
            tables2 = mgr.get_schema("test")

        assert tables1[0].row_count == 50
        assert tables2[0].row_count == 55
        assert tables1 is not tables2


class TestGetForeignKeys:
    def test_returns_fk_list(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            return_value=[
                ("posts", "user_id", "users", "id"),
                ("comments", "post_id", "posts", "id"),
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            fks = mgr.get_foreign_keys("test")

        assert len(fks) == 2
        assert fks[0].source_table == "posts"
        assert fks[0].target_table == "users"

    def test_no_foreign_keys(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager()
            fks = mgr.get_foreign_keys("test")

        assert fks == []

    def test_uses_pg_catalog_mapping(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager()
            mgr.get_foreign_keys("test")

        sql = cur.execute.call_args[0][0]
        assert "pg_constraint" in sql
        assert "position_in_unique_constraint" not in sql


class TestGetTableStats:
    def test_basic_stats(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists check
                (100,),  # row count
                (5,),  # null count for 'id'
                (100,),  # distinct for 'id'
                (1, 100, 50.5),  # min/max/avg for 'id' (numeric)
            ]
        )
        cur.fetchall = MagicMock(
            side_effect=[
                [("id", "integer")],  # columns
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            stats = mgr.get_table_stats("test", "users")

        assert stats["table"] == "users"
        assert stats["database"] == "test"
        assert stats["row_count"] == 100
        assert "id" in stats["columns"]
        assert stats["columns"]["id"]["type"] == "integer"

    def test_table_not_found(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (0,),  # table does not exist
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="not found"):
                mgr.get_table_stats("test", "nonexistent")

    def test_invalid_table_name(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.get_table_stats("test", "table;drop")


class TestGetTrend:
    def test_basic_trend(self):
        cur = MagicMock()
        cur.description = [("period",), ("count",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchmany = MagicMock(
            return_value=[
                ("2025-01-01", 42),
                ("2025-02-01", 55),
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.get_trend("test", "users", "created_at")

        assert result.columns == ["period", "count"]
        assert len(result.rows) == 2

    def test_invalid_period(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid period"):
            mgr.get_trend("test", "users", "created_at", period="hourly")

    def test_invalid_column(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.get_trend("test", "users", "bad;col")


class TestGetSample:
    def test_basic_sample(self):
        cur = MagicMock()
        cur.description = [("id",), ("name",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[(1, "Alice"), (2, "Bob")])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.get_sample("test", "users", 5)

        assert result.columns == ["id", "name"]
        assert len(result.rows) == 2

    def test_limit_capped(self):
        cur = MagicMock()
        cur.description = [("id",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(row_limit=10)
            mgr.get_sample("test", "users", 999)

        # Check LIMIT was capped to row_limit (10)
        call_args = cur.execute.call_args
        assert call_args[0][1] == (10,)


class TestFindAnomalies:
    def test_empty_table(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (0,),  # row count = 0
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "empty_table")

        assert result["row_count"] == 0
        assert result["anomalies"] == []

    def test_high_null_rate(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (80,),  # null count for 'bio' = 80%
                (20,),  # distinct for 'bio'
            ]
        )
        cur.fetchall = MagicMock(
            side_effect=[
                [("bio", "text")],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "users")

        anomaly_types = [a["type"] for a in result["anomalies"]]
        assert "high_null_rate" in anomaly_types

    def test_single_value(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (0,),  # null count = 0
                (1,),  # distinct = 1 → single_value
            ]
        )
        cur.fetchall = MagicMock(
            side_effect=[
                [("status", "text")],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "orders")

        anomaly_types = [a["type"] for a in result["anomalies"]]
        assert "single_value" in anomaly_types

    def test_invalid_table(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.find_anomalies("test", "bad;table")

    def test_id_substring_not_flagged_as_duplicate(self):
        # 'paid' contains 'id' as substring but is not an identifier column.
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (0,),  # null count for 'paid'
                (50,),  # distinct for 'paid' — not unique
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("paid", "boolean")]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "orders")

        types = [a["type"] for a in result["anomalies"]]
        assert "possible_duplicates" not in types

    def test_fk_style_id_column_not_flagged_as_duplicate(self):
        # 'user_id' is an FK; duplicates are legitimate, not an anomaly.
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (0,),  # null count
                (12,),  # distinct
                (None, None),  # percentile_cont — skip IQR
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("user_id", "integer")]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "posts")

        types = [a["type"] for a in result["anomalies"]]
        assert "possible_duplicates" not in types

    def test_email_column_flagged_as_duplicate(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (0,),  # null count
                (50,),  # distinct — duplicates present
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("user_email", "text")]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "users")

        types = [a["type"] for a in result["anomalies"]]
        assert "possible_duplicates" in types

    def test_bare_id_column_flagged_as_duplicate(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                (0,),  # null count
                (80,),  # distinct — duplicates present
                (None, None),  # percentile_cont — skip IQR
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("id", "integer")]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "users")

        types = [a["type"] for a in result["anomalies"]]
        assert "possible_duplicates" in types


class TestTimeoutBudget:
    def test_execute_rejects_expired_deadline(self):
        mgr = make_manager()
        cur = MagicMock()

        with patch("dbecho.db.time.monotonic", return_value=10.0):
            with pytest.raises(TimeoutError, match="query timeout"):
                mgr._execute(cur, "SELECT 1", deadline=10.0)

        cur.execute.assert_not_called()
