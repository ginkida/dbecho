from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import psycopg
from psycopg.sql import SQL, Identifier, Literal

from dbecho.config import Config, DatabaseConfig

NUMERIC_TYPES = frozenset(
    {
        "integer",
        "bigint",
        "smallint",
        "numeric",
        "real",
        "double precision",
        "decimal",
        "serial",
        "bigserial",
    }
)
TEMPORAL_TYPES = frozenset(
    {
        "timestamp without time zone",
        "timestamp with time zone",
        "date",
        "time without time zone",
        "time with time zone",
    }
)
TEXT_TYPES = frozenset(
    {
        "character varying",
        "varchar",
        "character",
        "char",
        "text",
    }
)

_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
_ALLOWED_SQL_PREFIXES = ("SELECT", "WITH", "SHOW")
_MAX_COLUMNS_FOR_STATS = 80


def _strip_strings_and_comments(sql: str) -> str:
    """Replace SQL string literals, quoted identifiers, and comments with
    single spaces so structural checks don't trip on their content.

    Handles: single-quoted strings (with '' escape), double-quoted identifiers
    (with "" escape), dollar-quoted strings ($tag$...$tag$), line comments
    (-- to end of line), block comments (/* ... */, nested).
    """
    out: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            while i < n and sql[i] != "\n":
                i += 1
            out.append(" ")
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            depth = 1
            i += 2
            while i + 1 < n and depth > 0:
                if sql[i] == "/" and sql[i + 1] == "*":
                    depth += 1
                    i += 2
                elif sql[i] == "*" and sql[i + 1] == "/":
                    depth -= 1
                    i += 2
                else:
                    i += 1
            out.append(" ")
            continue
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if c == '"':
            i += 1
            while i < n:
                if sql[i] == '"':
                    if i + 1 < n and sql[i + 1] == '"':
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            out.append(" ")
            continue
        if c == "$":
            j = i + 1
            while j < n and (sql[j] == "_" or sql[j].isalnum()):
                j += 1
            if j < n and sql[j] == "$":
                tag = sql[i : j + 1]
                k = j + 1
                while k < n:
                    if sql[k : k + len(tag)] == tag:
                        k += len(tag)
                        break
                    k += 1
                i = k
                out.append(" ")
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _validate_explain_body(clean: str) -> None:
    """Enforce: the body of EXPLAIN ANALYZE must be SELECT/WITH/SHOW.

    Handles both `EXPLAIN ANALYZE body` and `EXPLAIN (ANALYZE, ...) body`.
    Plain EXPLAIN (no ANALYZE) isn't checked — it only plans, doesn't execute.
    """
    rest = clean[len("EXPLAIN") :].lstrip()
    has_analyze = False
    if rest.startswith("("):
        depth = 1
        j = 1
        while j < len(rest) and depth > 0:
            if rest[j] == "(":
                depth += 1
            elif rest[j] == ")":
                depth -= 1
            j += 1
        opts = rest[1 : j - 1] if j > 1 else ""
        if re.search(r"\bANALY[SZ]E\b", opts, re.IGNORECASE):
            has_analyze = True
        rest = rest[j:].lstrip()
    tokens = rest.split(None, 1)
    if tokens and tokens[0].upper() in ("ANALYZE", "ANALYSE"):
        has_analyze = True
        rest = tokens[1] if len(tokens) > 1 else ""
    if has_analyze:
        body_word = rest.split(None, 1)[0].upper() if rest else ""
        if body_word and body_word not in ("SELECT", "WITH", "SHOW"):
            raise ValueError("EXPLAIN ANALYZE is only allowed with SELECT queries")


def _looks_like_identifier_column(name: str) -> bool:
    """Columns where duplicates suggest a data-quality issue.

    Matches `id` exactly (not `*_id`, which are FK columns and legitimately
    duplicate), or `email`/`uuid` as whole tokens in an underscore-split name.
    """
    lower = name.lower()
    if lower == "id":
        return True
    parts = lower.split("_")
    return any(p in {"email", "uuid"} for p in parts)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list]
    row_count: int
    truncated: bool = False


@dataclass
class TableInfo:
    name: str
    comment: str | None
    columns: list[ColumnInfo]
    row_count: int = 0
    size_bytes: int = 0


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    default: str | None
    is_primary_key: bool = False


@dataclass
class ForeignKey:
    source_table: str
    source_column: str
    target_table: str
    target_column: str


class DatabaseManager:
    def __init__(self, config: Config):
        self._databases: dict[str, DatabaseConfig] = {
            db.name: db for db in config.databases
        }
        self._settings = config.settings
        self._schema_cache: dict[str, list[TableInfo]] = {}

    @property
    def database_names(self) -> list[str]:
        return list(self._databases.keys())

    def get_database(self, name: str) -> DatabaseConfig:
        if name not in self._databases:
            available = ", ".join(self._databases.keys())
            raise ValueError(f"Unknown database '{name}'. Available: {available}")
        return self._databases[name]

    @contextmanager
    def _connect(self, db: DatabaseConfig) -> Generator[psycopg.Connection, None, None]:
        conn = psycopg.connect(
            db.url,
            options="-c default_transaction_read_only=on",
            connect_timeout=10,
        )
        try:
            yield conn
        finally:
            conn.close()

    def _validate_identifier(self, name: str) -> str:
        if not _SAFE_TABLE_RE.match(name):
            raise ValueError(f"Invalid identifier: {name!r}")
        return name

    @staticmethod
    def _public_table(name: str) -> Identifier:
        return Identifier("public", name)

    def _new_deadline(self) -> float:
        return time.monotonic() + self._settings.query_timeout

    @staticmethod
    def _remaining_timeout_ms(deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Operation exceeded query timeout")
        return max(1, math.ceil(remaining * 1000))

    def _execute(
        self, cur, query, params=None, *, deadline: float | None = None
    ) -> None:
        if deadline is not None:
            timeout_ms = self._remaining_timeout_ms(deadline)
            cur.execute(
                SQL("SET LOCAL statement_timeout = {}").format(Literal(timeout_ms))
            )
        if params is None:
            cur.execute(query)
        else:
            cur.execute(query, params)

    def _ensure_public_table_exists(
        self,
        cur,
        database: str,
        table: str,
        *,
        deadline: float,
    ) -> None:
        self._execute(
            cur,
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public' AND tablename = %s",
            (table,),
            deadline=deadline,
        )
        if cur.fetchone()[0] == 0:
            raise ValueError(f"Table '{table}' not found in database '{database}'")

    def check_connection(self, database: str) -> dict:
        db = self.get_database(database)
        try:
            with self._connect(db) as conn:
                with conn.cursor() as cur:
                    deadline = self._new_deadline()
                    self._execute(
                        cur,
                        "SELECT version(), current_database()",
                        deadline=deadline,
                    )
                    version, db_name = cur.fetchone()

                    size = "unknown"
                    try:
                        self._execute(
                            cur,
                            "SELECT pg_size_pretty(pg_database_size(current_database()))",
                            deadline=deadline,
                        )
                        size = cur.fetchone()[0]
                    except Exception:
                        conn.rollback()

                    return {
                        "status": "ok",
                        "version": version.split(",")[0],
                        "database": db_name,
                        "size": size,
                    }
        except Exception as e:
            return {"status": "error", "error": str(e)}

    @staticmethod
    def validate_sql(sql: str) -> str:
        """Validate and normalize a user-supplied SQL string.

        Returns the stripped SQL or raises ValueError.
        """
        stripped = sql.strip().rstrip(";").strip()
        if not stripped:
            raise ValueError("Empty query")

        clean = _strip_strings_and_comments(stripped).strip()
        if not clean:
            raise ValueError("Empty query")
        if ";" in clean:
            raise ValueError("Multiple statements are not allowed")

        first_word = clean.split()[0].upper()
        if first_word == "EXPLAIN":
            _validate_explain_body(clean)
        elif first_word not in _ALLOWED_SQL_PREFIXES:
            raise ValueError(
                f"Only SELECT/WITH/EXPLAIN/SHOW queries allowed, got: {first_word}"
            )
        return stripped

    def query(self, database: str, sql: str) -> QueryResult:
        sql = self.validate_sql(sql)
        db = self.get_database(database)
        limit = self._settings.row_limit

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._execute(cur, sql, deadline=deadline)

                if cur.description is None:
                    return QueryResult(columns=[], rows=[], row_count=0)

                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(limit + 1)

                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]

                return QueryResult(
                    columns=columns,
                    rows=[list(row) for row in rows],
                    row_count=len(rows),
                    truncated=truncated,
                )

    def get_schema(self, database: str, use_cache: bool = False) -> list[TableInfo]:
        if use_cache and database in self._schema_cache:
            return list(self._schema_cache[database])

        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                # Tables with sizes and row counts
                self._execute(
                    cur,
                    """
                    SELECT
                        t.table_name,
                        obj_description((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) AS comment,
                        COALESCE(s.n_live_tup, 0) AS row_count,
                        COALESCE(pg_total_relation_size((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass), 0) AS size_bytes
                    FROM information_schema.tables t
                    LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name AND s.schemaname = t.table_schema
                    WHERE t.table_schema = 'public' AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name
                """,
                    deadline=deadline,
                )
                table_rows = cur.fetchall()

                # Columns (only for BASE TABLEs we found above)
                table_names = [r[0] for r in table_rows]
                self._execute(
                    cur,
                    """
                    SELECT table_name, column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = ANY(%s)
                    ORDER BY table_name, ordinal_position
                """,
                    (table_names,),
                    deadline=deadline,
                )
                col_rows = cur.fetchall()

                # Primary keys
                self._execute(
                    cur,
                    """
                    SELECT tc.table_name, kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = 'public'
                """,
                    deadline=deadline,
                )
                pk_set = {(r[0], r[1]) for r in cur.fetchall()}

        columns_by_table: dict[str, list[ColumnInfo]] = {}
        for tname, cname, dtype, nullable, default in col_rows:
            columns_by_table.setdefault(tname, []).append(
                ColumnInfo(
                    name=cname,
                    data_type=dtype,
                    nullable=nullable == "YES",
                    default=default,
                    is_primary_key=(tname, cname) in pk_set,
                )
            )

        tables = []
        for tname, comment, row_count, size_bytes in table_rows:
            tables.append(
                TableInfo(
                    name=tname,
                    comment=comment,
                    columns=columns_by_table.get(tname, []),
                    row_count=row_count,
                    size_bytes=size_bytes,
                )
            )

        if use_cache:
            self._schema_cache[database] = list(tables)
        return tables

    def get_foreign_keys(self, database: str) -> list[ForeignKey]:
        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._execute(
                    cur,
                    """
                    SELECT
                        src.relname AS source_table,
                        src_att.attname AS source_column,
                        tgt.relname AS target_table,
                        tgt_att.attname AS target_column
                    FROM pg_constraint c
                    JOIN pg_class src ON src.oid = c.conrelid
                    JOIN pg_namespace src_ns ON src_ns.oid = src.relnamespace
                    JOIN pg_class tgt ON tgt.oid = c.confrelid
                    JOIN pg_namespace tgt_ns ON tgt_ns.oid = tgt.relnamespace
                    JOIN LATERAL generate_subscripts(c.conkey, 1) AS pos(i) ON TRUE
                    JOIN pg_attribute src_att
                        ON src_att.attrelid = c.conrelid
                        AND src_att.attnum = c.conkey[pos.i]
                    JOIN pg_attribute tgt_att
                        ON tgt_att.attrelid = c.confrelid
                        AND tgt_att.attnum = c.confkey[pos.i]
                    WHERE c.contype = 'f'
                        AND src_ns.nspname = 'public'
                        AND tgt_ns.nspname = 'public'
                    ORDER BY src.relname, c.conname, pos.i
                """,
                    deadline=deadline,
                )
                return [ForeignKey(*row) for row in cur.fetchall()]

    def get_table_stats(self, database: str, table: str) -> dict:
        self._validate_identifier(table)
        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_public_table_exists(
                    cur, database, table, deadline=deadline
                )

                tbl = self._public_table(table)

                self._execute(
                    cur,
                    SQL("SELECT COUNT(*) FROM {}").format(tbl),
                    deadline=deadline,
                )
                row_count = cur.fetchone()[0]

                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                """,
                    (table,),
                    deadline=deadline,
                )
                columns = cur.fetchall()

                if len(columns) > _MAX_COLUMNS_FOR_STATS:
                    raise ValueError(
                        f"Table '{table}' has {len(columns)} columns "
                        f"(max {_MAX_COLUMNS_FOR_STATS} for stats)"
                    )

                stats = {
                    "table": table,
                    "database": database,
                    "row_count": row_count,
                    "columns": {},
                }

                for col_name, data_type in columns:
                    col = Identifier(col_name)
                    col_stats: dict = {"type": data_type}

                    self._execute(
                        cur,
                        SQL("SELECT COUNT(*) FROM {} WHERE {} IS NULL").format(
                            tbl, col
                        ),
                        deadline=deadline,
                    )
                    null_count = cur.fetchone()[0]
                    col_stats["null_count"] = null_count
                    col_stats["null_pct"] = (
                        round(null_count / row_count * 100, 1) if row_count > 0 else 0
                    )

                    self._execute(
                        cur,
                        SQL("SELECT COUNT(DISTINCT {}) FROM {}").format(col, tbl),
                        deadline=deadline,
                    )
                    col_stats["distinct"] = cur.fetchone()[0]

                    if data_type in NUMERIC_TYPES:
                        self._execute(
                            cur,
                            SQL(
                                "SELECT MIN({}), MAX({}), AVG({})::numeric(20,2) FROM {}"
                            ).format(col, col, col, tbl),
                            deadline=deadline,
                        )
                        min_val, max_val, avg_val = cur.fetchone()
                        col_stats["min"] = min_val
                        col_stats["max"] = max_val
                        col_stats["avg"] = (
                            float(avg_val) if avg_val is not None else None
                        )

                    elif data_type in TEMPORAL_TYPES:
                        self._execute(
                            cur,
                            SQL("SELECT MIN({}), MAX({}) FROM {}").format(
                                col, col, tbl
                            ),
                            deadline=deadline,
                        )
                        min_val, max_val = cur.fetchone()
                        col_stats["min"] = str(min_val) if min_val else None
                        col_stats["max"] = str(max_val) if max_val else None

                    if (
                        data_type in TEXT_TYPES
                        and col_stats["distinct"] <= 50
                        and row_count > 0
                    ):
                        self._execute(
                            cur,
                            SQL(
                                "SELECT {}, COUNT(*) as cnt FROM {} WHERE {} IS NOT NULL "
                                "GROUP BY {} ORDER BY cnt DESC LIMIT 10"
                            ).format(col, tbl, col, col),
                            deadline=deadline,
                        )
                        col_stats["top_values"] = [
                            {"value": r[0], "count": r[1]} for r in cur.fetchall()
                        ]

                    stats["columns"][col_name] = col_stats

                return stats

    def get_trend(
        self,
        database: str,
        table: str,
        date_column: str,
        value_column: str | None = None,
        period: str = "month",
    ) -> QueryResult:
        self._validate_identifier(table)
        self._validate_identifier(date_column)
        if value_column:
            self._validate_identifier(value_column)

        period_map = {
            "day": "day",
            "week": "week",
            "month": "month",
            "quarter": "quarter",
            "year": "year",
        }
        if period not in period_map:
            raise ValueError(f"Invalid period '{period}'. Use: {', '.join(period_map)}")

        tbl = self._public_table(table)
        date_col = Identifier(date_column)

        if value_column:
            val_col = Identifier(value_column)
            q = SQL(
                "SELECT date_trunc(%s, {date_col})::date AS period, "
                "COUNT(*) AS count, "
                "AVG({val_col})::numeric(20,2) AS avg_value, "
                "SUM({val_col})::numeric(20,2) AS total "
                "FROM {tbl} WHERE {date_col} IS NOT NULL "
                "GROUP BY period ORDER BY period"
            ).format(date_col=date_col, val_col=val_col, tbl=tbl)
        else:
            q = SQL(
                "SELECT date_trunc(%s, {date_col})::date AS period, "
                "COUNT(*) AS count "
                "FROM {tbl} WHERE {date_col} IS NOT NULL "
                "GROUP BY period ORDER BY period"
            ).format(date_col=date_col, tbl=tbl)

        db = self.get_database(database)
        limit = self._settings.row_limit

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_public_table_exists(
                    cur, database, table, deadline=deadline
                )
                self._execute(cur, q, (period,), deadline=deadline)

                if cur.description is None:
                    return QueryResult(columns=[], rows=[], row_count=0)

                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchmany(limit + 1)
                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]

                return QueryResult(
                    columns=columns,
                    rows=[list(row) for row in rows],
                    row_count=len(rows),
                    truncated=truncated,
                )

    def get_sample(self, database: str, table: str, limit: int = 5) -> QueryResult:
        self._validate_identifier(table)
        limit = min(limit, self._settings.row_limit)
        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_public_table_exists(
                    cur, database, table, deadline=deadline
                )
                self._execute(
                    cur,
                    SQL("SELECT * FROM {} LIMIT %s").format(self._public_table(table)),
                    (limit,),
                    deadline=deadline,
                )
                if cur.description is None:
                    return QueryResult(columns=[], rows=[], row_count=0)

                columns = [desc[0] for desc in cur.description]
                rows = cur.fetchall()
                return QueryResult(
                    columns=columns,
                    rows=[list(row) for row in rows],
                    row_count=len(rows),
                )

    def find_anomalies(self, database: str, table: str) -> dict:
        self._validate_identifier(table)
        db = self.get_database(database)

        anomalies = []

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_public_table_exists(
                    cur, database, table, deadline=deadline
                )

                tbl = self._public_table(table)

                self._execute(
                    cur,
                    SQL("SELECT COUNT(*) FROM {}").format(tbl),
                    deadline=deadline,
                )
                row_count = cur.fetchone()[0]

                if row_count == 0:
                    return {
                        "table": table,
                        "database": database,
                        "row_count": 0,
                        "anomalies": [],
                    }

                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = 'public' AND table_name = %s
                    ORDER BY ordinal_position
                """,
                    (table,),
                    deadline=deadline,
                )
                columns = cur.fetchall()

                if len(columns) > _MAX_COLUMNS_FOR_STATS:
                    raise ValueError(
                        f"Table '{table}' has {len(columns)} columns "
                        f"(max {_MAX_COLUMNS_FOR_STATS} for anomaly detection)"
                    )

                for col_name, data_type in columns:
                    col = Identifier(col_name)

                    # High null rate
                    self._execute(
                        cur,
                        SQL("SELECT COUNT(*) FROM {} WHERE {} IS NULL").format(
                            tbl, col
                        ),
                        deadline=deadline,
                    )
                    null_count = cur.fetchone()[0]
                    null_pct = round(null_count / row_count * 100, 1)
                    if null_pct > 50:
                        anomalies.append(
                            {
                                "type": "high_null_rate",
                                "column": col_name,
                                "detail": f"{null_pct}% NULL ({null_count:,}/{row_count:,})",
                            }
                        )

                    # Single value dominance
                    self._execute(
                        cur,
                        SQL("SELECT COUNT(DISTINCT {}) FROM {}").format(col, tbl),
                        deadline=deadline,
                    )
                    distinct = cur.fetchone()[0]
                    if distinct == 1 and row_count > 1:
                        anomalies.append(
                            {
                                "type": "single_value",
                                "column": col_name,
                                "detail": f"Only 1 distinct value in {row_count:,} rows",
                            }
                        )

                    # Numeric outliers (IQR method)
                    if data_type in NUMERIC_TYPES and distinct > 4:
                        self._execute(
                            cur,
                            SQL("""
                            SELECT
                                percentile_cont(0.25) WITHIN GROUP (ORDER BY {}),
                                percentile_cont(0.75) WITHIN GROUP (ORDER BY {})
                            FROM {}
                            WHERE {} IS NOT NULL
                        """).format(col, col, tbl, col),
                            deadline=deadline,
                        )
                        q1, q3 = cur.fetchone()
                        if q1 is not None and q3 is not None:
                            iqr = float(q3 - q1)
                            if iqr > 0:
                                lower = float(q1) - 1.5 * iqr
                                upper = float(q3) + 1.5 * iqr
                                self._execute(
                                    cur,
                                    SQL(
                                        "SELECT COUNT(*) FROM {} WHERE {} < %s OR {} > %s"
                                    ).format(tbl, col, col),
                                    (lower, upper),
                                    deadline=deadline,
                                )
                                outlier_count = cur.fetchone()[0]
                                if outlier_count > 0:
                                    anomalies.append(
                                        {
                                            "type": "outliers",
                                            "column": col_name,
                                            "detail": f"{outlier_count:,} outliers (IQR: {lower:.1f}..{upper:.1f})",
                                        }
                                    )

                    # Date in the future (only for date/timestamp, not time)
                    if data_type in (
                        "timestamp without time zone",
                        "timestamp with time zone",
                        "date",
                    ):
                        self._execute(
                            cur,
                            SQL("SELECT COUNT(*) FROM {} WHERE {} > NOW()").format(
                                tbl, col
                            ),
                            deadline=deadline,
                        )
                        future_count = cur.fetchone()[0]
                        if future_count > 0:
                            anomalies.append(
                                {
                                    "type": "future_dates",
                                    "column": col_name,
                                    "detail": f"{future_count:,} rows with dates in the future",
                                }
                            )

                    # Possible duplicates on unique-looking columns
                    if distinct == row_count - null_count and distinct > 10:
                        pass  # Looks unique, no anomaly
                    elif _looks_like_identifier_column(col_name):
                        dup_count = row_count - null_count - distinct
                        if dup_count > 0:
                            anomalies.append(
                                {
                                    "type": "possible_duplicates",
                                    "column": col_name,
                                    "detail": f"{dup_count:,} possible duplicate values",
                                }
                            )

        return {
            "table": table,
            "database": database,
            "row_count": row_count,
            "anomalies": anomalies,
        }
