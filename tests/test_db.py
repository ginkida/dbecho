import pytest
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

from psycopg.sql import SQL, Identifier

from dbecho.db import (
    DatabaseManager,
    _MAX_FIND_RESULTS,
    _SAFE_TABLE_RE,
    _escape_like,
    _is_sensitive_column,
    _safe_conn_error,
)
from dbecho.config import Config, DatabaseConfig, Settings


def make_manager(schema: str = "public", **kwargs) -> DatabaseManager:
    config = Config(
        databases=[
            DatabaseConfig(name="test", url="postgres://localhost/test", schema=schema)
        ],
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

    def test_writable_cte_blocked(self):
        # PostgreSQL executes writes inside CTEs; first-keyword check alone
        # would let this through and rely solely on the read-only connection.
        with pytest.raises(ValueError, match="Data-modifying"):
            DatabaseManager.validate_sql(
                "WITH x AS (DELETE FROM users RETURNING *) SELECT * FROM x"
            )

    def test_writable_cte_insert_blocked(self):
        with pytest.raises(ValueError, match="Data-modifying"):
            DatabaseManager.validate_sql(
                "WITH x AS (INSERT INTO logs VALUES (1) RETURNING *) SELECT * FROM x"
            )

    def test_select_into_blocked(self):
        # SELECT ... INTO creates a table — a write hidden behind SELECT.
        with pytest.raises(ValueError, match="Data-modifying"):
            DatabaseManager.validate_sql("SELECT * INTO new_table FROM users")

    def test_explain_analyze_writable_cte_blocked(self):
        # EXPLAIN ANALYZE executes the body, so writable CTEs must be blocked.
        with pytest.raises(ValueError, match="data-modifying"):
            DatabaseManager.validate_sql(
                "EXPLAIN ANALYZE WITH x AS (UPDATE users SET a = 1 RETURNING *) "
                "SELECT * FROM x"
            )

    def test_set_transaction_read_write_blocked(self):
        # Pins that SET can never reach the DB (first word not whitelisted) —
        # with one statement per connection this is what keeps read-only safe.
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("SET TRANSACTION READ WRITE")
        with pytest.raises(ValueError, match="Only SELECT"):
            DatabaseManager.validate_sql("SET transaction_read_only = off")

    def test_blocked_function_pg_read_file(self):
        with pytest.raises(ValueError, match="blocked function"):
            DatabaseManager.validate_sql("SELECT pg_read_file('/etc/passwd')")

    def test_blocked_function_set_config(self):
        with pytest.raises(ValueError, match="blocked function"):
            DatabaseManager.validate_sql(
                "SELECT set_config('default_transaction_read_only', 'off', false)"
            )

    def test_blocked_function_lo_export(self):
        with pytest.raises(ValueError, match="blocked function"):
            DatabaseManager.validate_sql("SELECT lo_export(1234, '/tmp/x')")

    def test_blocked_function_dblink(self):
        with pytest.raises(ValueError, match="blocked function"):
            DatabaseManager.validate_sql(
                "SELECT * FROM dblink('host=evil', 'SELECT 1') AS t(x int)"
            )

    def test_write_keywords_in_identifiers_pass(self):
        # Keyword-lookalike identifiers must not false-positive (\b + stripping).
        DatabaseManager.validate_sql("SELECT update_time, created_at FROM updates")
        DatabaseManager.validate_sql("SELECT * FROM copy_of_users")
        DatabaseManager.validate_sql("SELECT deleted_at FROM grants_log")
        DatabaseManager.validate_sql("SELECT dblink_count FROM stats")
        DatabaseManager.validate_sql("SELECT into_total, account_into FROM ledger")

    def test_nonreserved_keyword_columns_pass(self):
        # COPY and MERGE are non-reserved keywords — legal unquoted column
        # names (e.g. ad "copy") that cannot modify data mid-SELECT.
        DatabaseManager.validate_sql(
            "SELECT a.copy, count(*) FROM ad_creatives a GROUP BY a.copy"
        )
        DatabaseManager.validate_sql("SELECT merge FROM git_merges")

    def test_blocked_function_name_as_bare_column_passes(self):
        # Only a function CALL is dangerous; a column named set_config is not.
        DatabaseManager.validate_sql("SELECT set_config FROM app_settings")
        DatabaseManager.validate_sql("SELECT dblink FROM connections")

    def test_write_keywords_in_strings_pass(self):
        DatabaseManager.validate_sql("SELECT 'DELETE FROM users' AS note")
        DatabaseManager.validate_sql("SELECT 1 WHERE action = 'drop table'")

    def test_write_keywords_in_quoted_identifiers_pass(self):
        DatabaseManager.validate_sql('SELECT "delete" FROM audit_log')

    def test_positional_param_not_treated_as_dollar_quote(self):
        # $1 is a positional parameter in PostgreSQL, not a dollar-quote tag;
        # a ";" after it must still be caught as multi-statement.
        with pytest.raises(ValueError, match="Multiple statements"):
            DatabaseManager.validate_sql("SELECT $1; DROP TABLE users")


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

    def test_trailing_newline_rejected(self):
        # $ matches before a trailing \n in Python; \Z must not.
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr._validate_identifier("users\n")


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
        # Strict side_effect sequences: an added/missing/reordered fetch in
        # get_trend surfaces as StopIteration instead of silently passing.
        cur = MagicMock()
        cur.description = [("period",), ("count",)]
        cur.fetchone = MagicMock(side_effect=[(1,)])  # table exists
        cur.fetchall = MagicMock(
            side_effect=[[("created_at", "date")]]  # column type check
        )
        cur.fetchmany = MagicMock(
            side_effect=[
                [
                    ("2025-01-01", 42),
                    ("2025-02-01", 55),
                ]
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.get_trend("test", "users", "created_at")

        assert result.columns == ["period", "count"]
        assert len(result.rows) == 2

    def test_time_column_rejected_for_date_trunc(self):
        # time/timetz are temporal but date_trunc has no overload for them.
        cur = MagicMock()
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[("opened_at", "time without time zone")])

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="expected a date/timestamp"):
                mgr.get_trend("test", "shifts", "opened_at")

    def test_invalid_period(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid period"):
            mgr.get_trend("test", "users", "created_at", period="hourly")

    def test_invalid_column(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.get_trend("test", "users", "bad;col")

    def test_non_temporal_date_column_rejected(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[("created_at", "integer")])

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="expected a date/timestamp"):
                mgr.get_trend("test", "users", "created_at")

    def test_non_numeric_value_column_rejected(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(
            return_value=[("created_at", "date"), ("name", "text")]
        )

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="expected a numeric"):
                mgr.get_trend("test", "users", "created_at", value_column="name")

    def test_missing_date_column_rejected(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="not found"):
                mgr.get_trend("test", "users", "ghost_column")

    def test_bad_value_column_rejected_before_connect(self):
        mgr = make_manager()
        with patch.object(DatabaseManager, "_connect") as mock_connect:
            with pytest.raises(ValueError, match="Invalid identifier"):
                mgr.get_trend("test", "users", "created_at", value_column='x"; DROP')
            mock_connect.assert_not_called()


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

    def test_timeout_message_includes_budget(self):
        mgr = make_manager(query_timeout=7)
        cur = MagicMock()

        with patch("dbecho.db.time.monotonic", return_value=10.0):
            with pytest.raises(TimeoutError, match="7s query timeout"):
                mgr._execute(cur, "SELECT 1", deadline=10.0)

    def test_set_local_statement_timeout_emitted_before_query(self):
        # statement_timeout is the only bound on query wall-time; dropping the
        # deadline= arg must be visible to tests.
        mgr = make_manager()
        cur = MagicMock()

        deadline = mgr._new_deadline()
        mgr._execute(cur, "SELECT 1", deadline=deadline)

        assert cur.execute.call_count == 2
        first_call_sql = cur.execute.call_args_list[0][0][0]
        assert "statement_timeout" in repr(first_call_sql)
        assert cur.execute.call_args_list[1][0][0] == "SELECT 1"

    def test_no_set_local_without_deadline(self):
        mgr = make_manager()
        cur = MagicMock()

        mgr._execute(cur, "SELECT 1")

        assert cur.execute.call_count == 1

    def test_budget_shared_across_queries(self):
        mgr = make_manager(query_timeout=10)
        cur = MagicMock()

        # t=0: deadline set to 10. t=5: first query fits. t=12: budget gone.
        with patch("dbecho.db.time.monotonic", side_effect=[0.0, 5.0, 12.0]):
            deadline = mgr._new_deadline()
            mgr._execute(cur, "SELECT 1", deadline=deadline)
            with pytest.raises(TimeoutError, match="10s query timeout"):
                mgr._execute(cur, "SELECT 2", deadline=deadline)


class TestConnectionOptions:
    def test_readonly_and_timeouts_set(self):
        # The read-only option is the load-bearing write barrier — a refactor
        # that drops it must fail this test.
        mgr = make_manager()
        db = mgr.get_database("test")
        fake_conn = MagicMock()

        with patch("dbecho.db.psycopg.connect", return_value=fake_conn) as mock_conn:
            with mgr._connect(db) as conn:
                assert conn is fake_conn

        fake_conn.close.assert_called_once()
        assert mock_conn.call_args.args[0] == "postgres://localhost/test"
        options = mock_conn.call_args.kwargs["options"]
        assert "-c default_transaction_read_only=on" in options
        assert "statement_timeout=30000" in options
        assert "idle_in_transaction_session_timeout=30000" in options
        assert mock_conn.call_args.kwargs["connect_timeout"] == 10

    def test_connection_closed_on_error(self):
        mgr = make_manager()
        db = mgr.get_database("test")
        fake_conn = MagicMock()

        with patch("dbecho.db.psycopg.connect", return_value=fake_conn):
            with pytest.raises(RuntimeError):
                with mgr._connect(db):
                    raise RuntimeError("boom")

        fake_conn.close.assert_called_once()


class TestSchemaScoping:
    def test_search_path_set_for_non_public_schema(self):
        mgr = make_manager(schema="analytics")
        db = mgr.get_database("test")
        fake_conn = MagicMock()

        with patch("dbecho.db.psycopg.connect", return_value=fake_conn) as mock_conn:
            with mgr._connect(db):
                pass

        options = mock_conn.call_args.kwargs["options"]
        assert "-c search_path=analytics,public" in options

    def test_search_path_not_set_for_public_schema(self):
        mgr = make_manager()
        db = mgr.get_database("test")
        fake_conn = MagicMock()

        with patch("dbecho.db.psycopg.connect", return_value=fake_conn) as mock_conn:
            with mgr._connect(db):
                pass

        assert "search_path" not in mock_conn.call_args.kwargs["options"]

    def test_sample_qualifies_table_with_schema(self):
        cur = MagicMock()
        cur.description = [("id",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            mgr.get_sample("test", "events", 5)

        executed = [c.args[0] for c in cur.execute.call_args_list]
        assert (
            SQL("SELECT * FROM {} LIMIT %s").format(Identifier("analytics", "events"))
            in executed
        )

    def test_table_existence_check_uses_schema(self):
        cur = MagicMock()
        cur.description = [("id",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            mgr.get_sample("test", "events", 5)

        checks = [
            c
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str) and "pg_tables" in c.args[0]
        ]
        assert checks and checks[0].args[1] == ("analytics", "events")

    def test_table_not_found_mentions_schema(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(0,)])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            with pytest.raises(ValueError, match="schema 'analytics'"):
                mgr.get_table_stats("test", "missing")

    @staticmethod
    def _assert_no_hardcoded_public(cur):
        # Kills "param passed but SQL still hardcodes 'public'" mutants: a
        # query that gained a schema parameter must not keep the literal too.
        assert not any(
            "'public'" in c.args[0]
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str)
        )

    def test_get_schema_filters_by_configured_schema(self):
        cur, _ = make_mock_cursor([[], [], []])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            assert mgr.get_schema("test") == []

        tables_query = [
            c
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str) and "information_schema.tables" in c.args[0]
        ]
        assert tables_query and tables_query[0].args[1][0] == "analytics"
        self._assert_no_hardcoded_public(cur)

    def test_foreign_keys_filter_by_schema(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            assert mgr.get_foreign_keys("test") == []

        fk_query = [
            c
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str) and "pg_constraint" in c.args[0]
        ]
        assert fk_query and fk_query[0].args[1][:2] == ("analytics", "analytics")
        self._assert_no_hardcoded_public(cur)

    def test_indexes_filter_by_schema(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            assert mgr.get_indexes("test") == []

        idx_query = [
            c
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str) and "pg_index" in c.args[0]
        ]
        assert idx_query and idx_query[0].args[1][0] == "analytics"
        self._assert_no_hardcoded_public(cur)

    def test_trend_qualifies_table_with_schema(self):
        cur = MagicMock()
        cur.description = [("period",), ("count",)]
        cur.fetchone = MagicMock(return_value=(1,))
        cur.fetchall = MagicMock(
            return_value=[("created_at", "timestamp with time zone")]
        )
        cur.fetchmany = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            mgr.get_trend("test", "events", "created_at")

        assert any(
            "Identifier('analytics', 'events')" in repr(c.args[0])
            for c in cur.execute.call_args_list
        )
        self._assert_no_hardcoded_public(cur)

    def test_anomalies_qualifies_table_with_schema(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (5,)])
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            result = mgr.find_anomalies("test", "events")

        assert result["anomalies"] == []
        assert any(
            "Identifier('analytics', 'events')" in repr(c.args[0])
            for c in cur.execute.call_args_list
        )
        self._assert_no_hardcoded_public(cur)

    def test_table_stats_qualifies_table_with_schema(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (5,)])
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            stats = mgr.get_table_stats("test", "events")

        assert stats["columns"] == {}
        assert any(
            "Identifier('analytics', 'events')" in repr(c.args[0])
            for c in cur.execute.call_args_list
        )
        self._assert_no_hardcoded_public(cur)

    def test_describe_filters_by_schema(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (None, 0, 0)])
        cur.fetchall = MagicMock(return_value=[])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            info = mgr.get_table_schema("test", "events")

        assert info.columns == []
        meta_calls = [
            c
            for c in cur.execute.call_args_list
            if isinstance(c.args[0], str) and "information_schema" in c.args[0]
        ]
        assert meta_calls and all(c.args[1][0] == "analytics" for c in meta_calls)
        self._assert_no_hardcoded_public(cur)


class TestRedaction:
    def test_is_sensitive_column(self):
        for name in [
            "password",
            "password_hash",
            "user_passwd",
            "api_key",
            "apikey",
            "access_token",
            "refresh_token",
            "client_secret",
            "secret",
            "reset_token",
            "password_reset_token",
            "otp_code",
            "credential",
            "ssn",
        ]:
            assert _is_sensitive_column(name), name

    def test_is_not_sensitive_column(self):
        for name in [
            "author",
            "session_id",
            "sort_key",
            "pinned_at",
            "email",
            "name",
            "tokenizer",
            "stock_count",
        ]:
            assert not _is_sensitive_column(name), name

    def test_secret_metadata_columns_not_redacted(self):
        # Counts/flags/timestamps ABOUT secrets are legitimate analytics
        # columns (LLM token usage!), not the secrets themselves.
        for name in [
            "token_count",
            "token_type",
            "tokens_used",
            "password_changed_at",
            "password_reset_at",
            "has_password",
            "is_password",
            "otp_enabled",
            "token_expires_at",
            "login_attempts",
        ]:
            assert not _is_sensitive_column(name), name

    def test_query_redacts_sensitive_columns(self):
        cur = MagicMock()
        cur.description = [("id",), ("password_hash",), ("email",)]
        cur.fetchmany = MagicMock(return_value=[(1, "bcrypt$abc", "a@b.c")])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.query("test", "SELECT * FROM users")

        assert result.rows == [[1, "<redacted>", "a@b.c"]]

    def test_query_redaction_keeps_nulls(self):
        cur = MagicMock()
        cur.description = [("token",)]
        cur.fetchmany = MagicMock(return_value=[(None,), ("tok123",)])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.query("test", "SELECT token FROM sessions")

        assert result.rows == [[None], ["<redacted>"]]

    def test_query_redaction_can_be_disabled(self):
        cur = MagicMock()
        cur.description = [("password",)]
        cur.fetchmany = MagicMock(return_value=[("hunter2",)])

        with mock_connection(cur):
            mgr = make_manager(redact_sensitive=False)
            result = mgr.query("test", "SELECT password FROM users")

        assert result.rows == [["hunter2"]]

    def test_sample_redacts_sensitive_columns(self):
        cur = MagicMock()
        cur.description = [("id",), ("api_key",)]
        cur.fetchone = MagicMock(return_value=(1,))  # table exists
        cur.fetchall = MagicMock(return_value=[(1, "sk-live-abc")])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.get_sample("test", "integrations", 5)

        assert result.rows == [[1, "<redacted>"]]


class TestQueryOffset:
    def test_offset_advances_cursor_before_fetch(self):
        cur = MagicMock()
        cur.description = [("id",)]
        cur.fetchmany = MagicMock(side_effect=[[(1,), (2,)], [(3,), (4,)]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.query("test", "SELECT id FROM t ORDER BY id", offset=2)

        assert cur.fetchmany.call_args_list[0][0][0] == 2
        assert result.rows == [[3], [4]]

    def test_no_extra_fetch_without_offset(self):
        cur = MagicMock()
        cur.description = [("id",)]
        cur.fetchmany = MagicMock(return_value=[(1,)])

        with mock_connection(cur):
            mgr = make_manager()
            mgr.query("test", "SELECT id FROM t")

        assert cur.fetchmany.call_count == 1

    def test_negative_offset_rejected(self):
        mgr = make_manager()
        with patch.object(DatabaseManager, "_connect") as mock_connect:
            with pytest.raises(ValueError, match="offset must be"):
                mgr.query("test", "SELECT 1", offset=-1)
            mock_connect.assert_not_called()


class TestIdentifierBoundary:
    def test_get_sample_rejects_bad_table_before_connect(self):
        mgr = make_manager()
        with patch.object(DatabaseManager, "_connect") as mock_connect:
            with pytest.raises(ValueError, match="Invalid identifier"):
                mgr.get_sample("test", 'users"; DROP TABLE x; --')
            mock_connect.assert_not_called()

    def test_get_table_stats_rejects_bad_table_before_connect(self):
        mgr = make_manager()
        with patch.object(DatabaseManager, "_connect") as mock_connect:
            with pytest.raises(ValueError, match="Invalid identifier"):
                mgr.get_table_stats("test", "users; DROP")
            mock_connect.assert_not_called()


class TestProfileRowLimit:
    def test_stats_gated_on_huge_tables(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (5_000,)])

        with mock_connection(cur):
            mgr = make_manager(max_profile_rows=1_000)
            with pytest.raises(ValueError, match="for full profiling"):
                mgr.get_table_stats("test", "events")

    def test_anomalies_gated_on_huge_tables(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (5_000,)])

        with mock_connection(cur):
            mgr = make_manager(max_profile_rows=1_000)
            with pytest.raises(ValueError, match="for full profiling"):
                mgr.find_anomalies("test", "events")

    def test_default_limit_allows_normal_tables(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(1,), (100,)])
        cur.fetchall = MagicMock(side_effect=[[]])  # no columns

        with mock_connection(cur):
            mgr = make_manager()
            stats = mgr.get_table_stats("test", "users")

        assert stats["row_count"] == 100


class TestSavepointIsolation:
    def test_failed_column_probe_is_skipped(self):
        # First column's probe errors (e.g. unorderable type); the second
        # column must still be profiled instead of the whole call failing.
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                Exception("operator does not exist"),  # col1 null probe fails
                (0,),  # col2 null count
                (60,),  # col2 distinct (>50: no top_values fetch)
            ]
        )
        cur.fetchall = MagicMock(
            side_effect=[[("weird", "integer"), ("status", "text")]]
        )

        with mock_connection(cur):
            mgr = make_manager()
            stats = mgr.get_table_stats("test", "orders")

        assert "status" in stats["columns"]
        assert "weird" not in stats["columns"]
        assert stats["skipped_columns"] == ["weird"]

        # Pin the isolation MECHANISM, not just the skip behavior: the failed
        # column must be wrapped in SAVEPOINT → ROLLBACK TO → RELEASE so the
        # transaction survives, and the next column gets its own savepoint.
        executed = [repr(c[0][0]) for c in cur.execute.call_args_list]
        assert any("SAVEPOINT" in s and "dbecho_col_0" in s for s in executed)
        assert any(
            "ROLLBACK TO SAVEPOINT" in s and "dbecho_col_0" in s for s in executed
        )
        assert any("RELEASE SAVEPOINT" in s and "dbecho_col_0" in s for s in executed)
        assert any("SAVEPOINT" in s and "dbecho_col_1" in s for s in executed)

    def test_timeout_during_probe_propagates(self):
        # A timeout must abort the whole profile, not silently skip columns.
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),
                (100,),
                TimeoutError("Operation exceeded the 30s query timeout"),
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("amount", "integer")]])

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(TimeoutError):
                mgr.get_table_stats("test", "orders")

    def test_anomalies_failed_column_probe_is_skipped(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                (100,),  # row count
                Exception("boom"),  # col1 null probe fails
                (80,),  # col2 null count -> high null rate
                (5,),  # col2 distinct
            ]
        )
        cur.fetchall = MagicMock(side_effect=[[("weird", "integer"), ("bio", "text")]])

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_anomalies("test", "users")

        assert result["skipped_columns"] == ["weird"]
        types = [a["type"] for a in result["anomalies"]]
        assert "high_null_rate" in types


class TestGetTableSchema:
    def test_describe_single_table(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            side_effect=[
                (1,),  # table exists
                ("User accounts", 100, 8192),  # comment, rows, size
            ]
        )
        cur.fetchall = MagicMock(
            side_effect=[
                [
                    ("id", "integer", "NO", "nextval('users_id_seq')"),
                    ("email", "text", "YES", None),
                ],
                [("id",)],  # primary key
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            info = mgr.get_table_schema("test", "users")

        assert info.name == "users"
        assert info.comment == "User accounts"
        assert info.row_count == 100
        assert info.size_bytes == 8192
        assert len(info.columns) == 2
        assert info.columns[0].is_primary_key
        assert not info.columns[1].is_primary_key

    def test_invalid_table_rejected(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.get_table_schema("test", "bad;table")

    def test_missing_table_rejected(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(side_effect=[(0,)])

        with mock_connection(cur):
            mgr = make_manager()
            with pytest.raises(ValueError, match="not found"):
                mgr.get_table_schema("test", "ghost")


class TestGetIndexes:
    def test_returns_indexes(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            return_value=[
                ("users", "users_email_key", True, "email"),
                ("posts", "posts_created_idx", False, "created_at"),
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            indexes = mgr.get_indexes("test")

        assert indexes == [
            {
                "table": "users",
                "name": "users_email_key",
                "unique": True,
                "columns": "email",
            },
            {
                "table": "posts",
                "name": "posts_created_idx",
                "unique": False,
                "columns": "created_at",
            },
        ]

    def test_invalid_table_rejected(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Invalid identifier"):
            mgr.get_indexes("test", "bad;name")


class TestEscapeLike:
    def test_metacharacters_escaped(self):
        assert _escape_like("100%") == "100\\%"
        assert _escape_like("a_b") == "a\\_b"
        assert _escape_like("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self):
        assert _escape_like("email") == "email"


class TestFindObjects:
    def test_finds_tables_and_columns(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            side_effect=[
                # matching tables
                [("user_emails",)],
                # matching columns
                [
                    ("users", "email", "character varying"),
                    ("subscribers", "email", "text"),
                ],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_objects("test", "email")

        assert result["database"] == "test"
        assert result["tables"] == ["user_emails"]
        assert result["columns"] == [
            {"table": "users", "column": "email", "type": "character varying"},
            {"table": "subscribers", "column": "email", "type": "text"},
        ]
        assert result["truncated"] is False

    def test_pattern_wildcards_matched_literally(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(side_effect=[[], []])

        with mock_connection(cur):
            mgr = make_manager()
            mgr.find_objects("test", "100%_done")

        like_params = [
            call.args[1]
            for call in cur.execute.call_args_list
            if len(call.args) > 1 and call.args[1]
        ]
        assert like_params, "expected parametrized ILIKE queries"
        for params in like_params:
            assert params[1] == "%100\\%\\_done%"

    def test_empty_pattern_rejected(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="non-empty"):
            mgr.find_objects("test", "   ")

    def test_long_pattern_rejected(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="too long"):
            mgr.find_objects("test", "x" * 201)

    def test_truncation_flag_and_cap(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(
            side_effect=[
                [(f"t{i}",) for i in range(_MAX_FIND_RESULTS + 1)],
                [],
            ]
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.find_objects("test", "t")

        assert result["truncated"] is True
        assert len(result["tables"]) == _MAX_FIND_RESULTS

    def test_targets_configured_schema(self):
        cur = MagicMock()
        cur.fetchall = MagicMock(side_effect=[[], []])

        with mock_connection(cur):
            mgr = make_manager(schema="analytics")
            mgr.find_objects("test", "ev")

        for call in cur.execute.call_args_list:
            if len(call.args) > 1 and call.args[1]:
                assert call.args[1][0] == "analytics"

    def test_unknown_database(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Unknown database"):
            mgr.find_objects("nope", "email")


class TestExplain:
    def test_explain_select(self):
        cur = MagicMock()
        cur.fetchone = MagicMock(
            return_value=(
                [
                    {
                        "Plan": {
                            "Node Type": "Seq Scan",
                            "Total Cost": 15.5,
                            "Plan Rows": 100,
                        }
                    }
                ],
            )
        )

        with mock_connection(cur):
            mgr = make_manager()
            result = mgr.explain("test", "SELECT * FROM users")

        assert result["node_type"] == "Seq Scan"
        assert result["total_cost"] == 15.5
        assert result["estimated_rows"] == 100

        # Pin that the query is PLANNED, not executed: the statement sent to
        # the cursor must be the EXPLAIN (FORMAT JSON) wrapper around the SQL.
        last_sql = repr(cur.execute.call_args_list[-1][0][0])
        assert "EXPLAIN (FORMAT JSON)" in last_sql
        assert "SELECT * FROM users" in last_sql

    def test_explain_rejects_show(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="SELECT and WITH"):
            mgr.explain("test", "SHOW server_version")

    def test_explain_rejects_writes(self):
        mgr = make_manager()
        with pytest.raises(ValueError, match="Only SELECT"):
            mgr.explain("test", "DELETE FROM users")


class TestSafeConnError:
    def test_auth_error_does_not_leak_username(self):
        msg = _safe_conn_error(
            Exception('FATAL: password authentication failed for user "admin"')
        )
        assert msg == "authentication failed"
        assert "admin" not in msg

    def test_unknown_database(self):
        msg = _safe_conn_error(Exception('database "prod_secret" does not exist'))
        assert msg == "database not found"
        assert "prod_secret" not in msg

    def test_host_resolution(self):
        assert (
            _safe_conn_error(
                Exception('could not translate host name "db.internal" to address')
            )
            == "host resolution failed"
        )

    def test_refused(self):
        assert (
            _safe_conn_error(
                Exception("connection to server at 10.0.0.5 port 5432 refused")
            )
            == "connection refused"
        )

    def test_timeout(self):
        assert (
            _safe_conn_error(Exception("connection timed out"))
            == "connection timed out"
        )

    def test_fallback_is_generic(self):
        msg = _safe_conn_error(Exception("weird ssl thing at host 10.1.2.3"))
        assert msg == "connection error"
        assert "10.1.2.3" not in msg
