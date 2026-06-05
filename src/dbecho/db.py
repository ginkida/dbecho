from __future__ import annotations

import logging
import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Generator

import psycopg
from psycopg.sql import SQL, Identifier, Literal

from dbecho.config import Config, DatabaseConfig

logger = logging.getLogger("dbecho")

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
# date_trunc has no overload for time/timetz — trend grouping needs these.
_DATE_TRUNCABLE_TYPES = frozenset(
    {
        "date",
        "timestamp without time zone",
        "timestamp with time zone",
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

# \Z (not $) so a trailing newline cannot sneak past — $ matches before a
# final \n in Python, which would weaken this defense-in-depth check.
_SAFE_TABLE_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*\Z")
_ALLOWED_SQL_PREFIXES = ("SELECT", "WITH", "SHOW")
_MAX_COLUMNS_FOR_STATS = 80
_MAX_SCHEMA_TABLES = 5000  # cap materialized table metadata to bound memory
_MAX_FK_ROWS = 10000  # cap relationship/index result sets

# Data-modifying keywords. The read-only connection is the real write barrier,
# but a first-keyword whitelist alone lets data-modifying CTEs
# (WITH x AS (DELETE ... RETURNING *) SELECT ...) and EXPLAIN ANALYZE write
# bodies through. This regex makes the validator independently sufficient so the
# whitelist is not wholly reliant on the GUC. Applied only to *executing* paths
# over comment/string-stripped SQL; \b + prior stripping avoid false positives
# (update_time, created_at, 'DELETE' literal all pass).
# INTO is included because in a SELECT body it only occurs in SELECT ... INTO
# <new table> (a write); INSERT INTO is already caught by INSERT.
# COPY/MERGE are deliberately absent: both are legal unquoted column names
# (non-reserved keywords, e.g. ad "copy") and neither can modify data from
# inside a SELECT/WITH body — COPY is only valid as a first keyword (already
# whitelisted away) and a data-modifying MERGE always requires INTO.
_WRITE_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|TRUNCATE|CREATE|ALTER|DROP|GRANT|REVOKE|INTO)\b",
    re.IGNORECASE,
)

# Functions that read/write the server filesystem, manipulate large objects,
# reach other databases, or flip GUCs. Even under a read-only transaction these
# are exfiltration / escape vectors, so block them as defense-in-depth. The
# trailing "(" requires an actual call, so a column merely *named* set_config
# or dblink still passes; \b + string/identifier stripping keep prefixed names
# (dblink_count, set_config_value) safe too.
_BLOCKED_FUNC_RE = re.compile(
    r"\b(pg_read_file|pg_read_binary_file|pg_ls_dir|pg_stat_file|lo_import|"
    r"lo_export|lo_put|lo_get|lo_from_bytea|dblink|dblink_exec|set_config)\s*\(",
    re.IGNORECASE,
)

# Metadata ABOUT a secret is not the secret: token_count, password_changed_at,
# otp_enabled, has_password are legitimate analytics columns (counts, flags,
# timestamps) and must not be blanked. Checked before the sensitivity match.
_NONSECRET_AFFIX_RE = re.compile(
    r"^(?:has|is|num)_"
    r"|_(?:count|at|on|date|time|enabled|disabled|required|verified|type|id|"
    r"len|length|changed|reset|expires|expiry|used|sent|attempts)$"
)

# Column-name tokens that strongly imply secrets/PII. Matched as whole
# underscore-split parts (plus a few multi-word substrings) to avoid flagging
# benign names like author/session_id/sort_key.
_SENSITIVE_PARTS = frozenset(
    {
        "password",
        "passwd",
        "passphrase",
        "pwd",
        "secret",
        "token",
        "apikey",
        "credential",
        "credentials",
        "otp",
        "cvv",
        "ssn",
    }
)
_SENSITIVE_SUBSTRINGS = (
    "password",
    "passwd",
    "passphrase",
    "api_key",
    "apikey",
    "secret_key",
    "private_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "session_token",
    "credit_card",
    "card_number",
)

_REDACTED = "<redacted>"


def _is_sensitive_column(name: str) -> bool:
    """True if a column name looks like it holds a secret or sensitive PII."""
    lower = name.lower()
    if _NONSECRET_AFFIX_RE.search(lower):
        return False
    if any(s in lower for s in _SENSITIVE_SUBSTRINGS):
        return True
    parts = re.split(r"[^a-z0-9]+", lower)
    return any(p in _SENSITIVE_PARTS for p in parts)


def _redact_rows(columns: list[str], rows: list[list]) -> list[list]:
    """Replace non-null cells of sensitive columns with a redaction marker.

    Name-based and best-effort: an aliased ``SELECT password AS x`` would slip
    past. This is harm reduction layered on top of read-only access and a
    least-privilege DB role — not a complete control.
    """
    sensitive = [i for i, c in enumerate(columns) if _is_sensitive_column(c)]
    if not sensitive:
        return rows
    out = []
    for row in rows:
        r = list(row)
        for i in sensitive:
            if i < len(r) and r[i] is not None:
                r[i] = _REDACTED
        out.append(r)
    return out


def _safe_conn_error(exc: Exception) -> str:
    """Map a connection exception to a coarse category that does not leak the
    host/port/user/dbname embedded in raw libpq error text."""
    text = str(exc).lower()
    if "password" in text or "authentication" in text or "role" in text:
        return "authentication failed"
    if "does not exist" in text:
        return "database not found"
    if (
        "could not translate host" in text
        or "name or service not known" in text
        or "could not resolve" in text
        or "nodename nor servname" in text
    ):
        return "host resolution failed"
    if "timeout" in text or "timed out" in text:
        return "connection timed out"
    if "refused" in text:
        return "connection refused"
    return "connection error"


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
            # $1, $2... are positional parameters in PostgreSQL, not dollar-quote
            # tags (tags cannot start with a digit). Treating them as quotes
            # would diverge from the server's parsing.
            if j < n and sql[j].isdigit():
                out.append(c)
                i += 1
                continue
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
        # EXPLAIN ANALYZE executes the body, so a data-modifying CTE inside it
        # would actually run — apply the same write/function checks as the
        # directly-executed paths. Plain EXPLAIN only plans, so it is exempt.
        if _WRITE_RE.search(rest):
            raise ValueError(
                "EXPLAIN ANALYZE with data-modifying statements is not allowed"
            )
        if _BLOCKED_FUNC_RE.search(rest):
            raise ValueError(
                "Query uses a blocked function (filesystem/large-object/dblink/"
                "set_config access is not allowed)"
            )


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
        self._schema_truncated: dict[str, bool] = {}

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
        # default_transaction_read_only is the load-bearing write barrier (the
        # SQL whitelist is defense-in-depth on top). The read-only guarantee
        # also depends on the ";"-ban: with one statement per connection, a
        # SET TRANSACTION READ WRITE can never be batched with a write.
        # Session-level statement_timeout / idle_in_transaction timeouts are
        # backstops in case any future code path runs without a deadline; the
        # per-query SET LOCAL still tightens statement_timeout per call.
        timeout_ms = self._settings.query_timeout * 1000
        options = (
            f"-c default_transaction_read_only=on"
            f" -c statement_timeout={timeout_ms}"
            f" -c idle_in_transaction_session_timeout={max(timeout_ms, 30000)}"
        )
        # Non-public schema leads search_path so raw `query` SQL can use
        # unqualified table names; public stays second for extensions
        # (pgvector operators etc.). db.schema is validated as a plain
        # identifier at config load, so embedding it here is safe.
        if db.schema != "public":
            options += f" -c search_path={db.schema},public"
        conn = psycopg.connect(
            db.url,
            options=options,
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
    def _qualified_table(db: DatabaseConfig, name: str) -> Identifier:
        return Identifier(db.schema, name)

    def _new_deadline(self) -> float:
        return time.monotonic() + self._settings.query_timeout

    def _remaining_timeout_ms(self, deadline: float) -> int:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Operation exceeded the {self._settings.query_timeout}s query timeout"
            )
        return max(1, math.ceil(remaining * 1000))

    @contextmanager
    def _savepoint(self, cur, name: str) -> Generator[None, None, None]:
        """Run a block inside a savepoint so a failed probe does not abort the
        whole transaction. Always releases the savepoint (rolled back or not)
        to avoid accumulating one per column."""
        sp = Identifier(name)
        cur.execute(SQL("SAVEPOINT {}").format(sp))
        try:
            yield
        except BaseException:
            try:
                cur.execute(SQL("ROLLBACK TO SAVEPOINT {}").format(sp))
                cur.execute(SQL("RELEASE SAVEPOINT {}").format(sp))
            except Exception:
                # A failed cleanup (e.g. dead connection) must never mask the
                # original probe error — the caller decides what to do with it.
                logger.warning("Savepoint %s cleanup failed", name)
            raise
        else:
            cur.execute(SQL("RELEASE SAVEPOINT {}").format(sp))

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

    def _ensure_table_exists(
        self,
        cur,
        db: DatabaseConfig,
        table: str,
        *,
        deadline: float,
    ) -> None:
        self._execute(
            cur,
            "SELECT COUNT(*) FROM pg_tables WHERE schemaname = %s AND tablename = %s",
            (db.schema, table),
            deadline=deadline,
        )
        if cur.fetchone()[0] == 0:
            raise ValueError(
                f"Table '{table}' not found in database '{db.name}' "
                f"(schema '{db.schema}')"
            )

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
            # Raw libpq errors embed host/port/user/dbname — log the detail for
            # the operator, return only a coarse category to the agent.
            logger.warning("Connection to '%s' failed: %s", database, e)
            return {"status": "error", "error": _safe_conn_error(e)}

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
        else:
            # Defense-in-depth beyond the first-keyword whitelist: block
            # data-modifying CTEs (WITH x AS (DELETE ...) SELECT ...) and
            # dangerous functions even though the read-only transaction would
            # also stop the writes. Checked on the stripped form so string
            # literals / quoted identifiers / comments cannot false-positive.
            if _WRITE_RE.search(clean):
                raise ValueError(
                    "Data-modifying statements are not allowed (if an "
                    'identifier shares a keyword name, double-quote it: "update")'
                )
            if _BLOCKED_FUNC_RE.search(clean):
                raise ValueError(
                    "Query uses a blocked function (filesystem/large-object/"
                    "dblink/set_config access is not allowed)"
                )
        return stripped

    def query(self, database: str, sql: str, offset: int = 0) -> QueryResult:
        sql = self.validate_sql(sql)
        db = self.get_database(database)
        if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        limit = self._settings.row_limit

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._execute(cur, sql, deadline=deadline)

                if cur.description is None:
                    return QueryResult(columns=[], rows=[], row_count=0)

                columns = [desc[0] for desc in cur.description]
                # Result-level pagination: advancing the cursor works uniformly
                # for SELECT/WITH/SHOW/EXPLAIN and never rewrites user SQL
                # (which would break queries with their own LIMIT/ORDER BY).
                # Skipped rows still cross the wire, so large offsets aren't free.
                if offset:
                    cur.fetchmany(offset)
                rows = cur.fetchmany(limit + 1)

                truncated = len(rows) > limit
                if truncated:
                    rows = rows[:limit]

                rows = [list(row) for row in rows]
                if self._settings.redact_sensitive:
                    rows = _redact_rows(columns, rows)

                return QueryResult(
                    columns=columns,
                    rows=rows,
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
                    WHERE t.table_schema = %s AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name
                    LIMIT %s
                """,
                    (db.schema, _MAX_SCHEMA_TABLES + 1),
                    deadline=deadline,
                )
                table_rows = cur.fetchall()
                truncated = len(table_rows) > _MAX_SCHEMA_TABLES
                if truncated:
                    logger.warning(
                        "Schema for '%s' truncated to %d tables",
                        database,
                        _MAX_SCHEMA_TABLES,
                    )
                    table_rows = table_rows[:_MAX_SCHEMA_TABLES]
                self._schema_truncated[database] = truncated

                # Columns (only for BASE TABLEs we found above)
                table_names = [r[0] for r in table_rows]
                self._execute(
                    cur,
                    """
                    SELECT table_name, column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = ANY(%s)
                    ORDER BY table_name, ordinal_position
                """,
                    (db.schema, table_names),
                    deadline=deadline,
                )
                col_rows = cur.fetchall()

                # Primary keys (scoped to the tables selected above so a
                # truncated table list also bounds this result set)
                self._execute(
                    cur,
                    """
                    SELECT tc.table_name, kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
                        AND tc.table_name = ANY(%s)
                """,
                    (db.schema, table_names),
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

    def schema_truncated(self, database: str) -> bool:
        """True if the most recent get_schema() for this database hit the
        table cap and the returned list is incomplete."""
        return self._schema_truncated.get(database, False)

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
                        AND src_ns.nspname = %s
                        AND tgt_ns.nspname = %s
                    ORDER BY src.relname, c.conname, pos.i
                    LIMIT %s
                """,
                    (db.schema, db.schema, _MAX_FK_ROWS),
                    deadline=deadline,
                )
                return [ForeignKey(*row) for row in cur.fetchall()]

    def _check_profile_row_limit(self, table: str, row_count: int) -> None:
        limit = self._settings.max_profile_rows
        if row_count > limit:
            raise ValueError(
                f"Table '{table}' has {row_count:,} rows "
                f"(max {limit:,} for full profiling); use a targeted query instead"
            )

    def _column_stats(
        self,
        cur,
        tbl: Identifier,
        col_name: str,
        data_type: str,
        row_count: int,
        *,
        deadline: float,
    ) -> dict:
        col = Identifier(col_name)
        col_stats: dict = {"type": data_type}

        self._execute(
            cur,
            SQL("SELECT COUNT(*) FROM {} WHERE {} IS NULL").format(tbl, col),
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
            # Unbounded numeric: a (20,2) cast allows only 18 integer digits
            # and overflows on ordinary SUM/AVG over bigint columns.
            self._execute(
                cur,
                SQL("SELECT MIN({}), MAX({}), AVG({})::numeric FROM {}").format(
                    col, col, col, tbl
                ),
                deadline=deadline,
            )
            min_val, max_val, avg_val = cur.fetchone()
            col_stats["min"] = min_val
            col_stats["max"] = max_val
            col_stats["avg"] = float(avg_val) if avg_val is not None else None

        elif data_type in TEMPORAL_TYPES:
            self._execute(
                cur,
                SQL("SELECT MIN({}), MAX({}) FROM {}").format(col, col, tbl),
                deadline=deadline,
            )
            min_val, max_val = cur.fetchone()
            col_stats["min"] = str(min_val) if min_val else None
            col_stats["max"] = str(max_val) if max_val else None

        if data_type in TEXT_TYPES and col_stats["distinct"] <= 50 and row_count > 0:
            self._execute(
                cur,
                SQL(
                    "SELECT {}, COUNT(*) as cnt FROM {} WHERE {} IS NOT NULL "
                    "GROUP BY {} ORDER BY cnt DESC LIMIT 10"
                ).format(col, tbl, col, col),
                deadline=deadline,
            )
            redact = self._settings.redact_sensitive and _is_sensitive_column(col_name)
            col_stats["top_values"] = [
                {
                    "value": _REDACTED if redact else r[0],
                    "count": r[1],
                    "pct": round(r[1] / row_count * 100, 1),
                }
                for r in cur.fetchall()
            ]

        return col_stats

    def get_table_stats(self, database: str, table: str) -> dict:
        self._validate_identifier(table)
        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_table_exists(cur, db, table, deadline=deadline)

                tbl = self._qualified_table(db, table)

                self._execute(
                    cur,
                    SQL("SELECT COUNT(*) FROM {}").format(tbl),
                    deadline=deadline,
                )
                row_count = cur.fetchone()[0]
                self._check_profile_row_limit(table, row_count)

                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """,
                    (db.schema, table),
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

                skipped: list[str] = []
                for idx, (col_name, data_type) in enumerate(columns):
                    try:
                        with self._savepoint(cur, f"dbecho_col_{idx}"):
                            col_stats = self._column_stats(
                                cur,
                                tbl,
                                col_name,
                                data_type,
                                row_count,
                                deadline=deadline,
                            )
                    except (TimeoutError, psycopg.errors.QueryCanceled):
                        # Budget exhausted: abandon the whole profile cleanly
                        # instead of silently skipping every remaining column.
                        raise
                    except Exception:
                        # One bad column (unorderable type, ...) must not abort
                        # the transaction and take the whole profile down.
                        logger.warning(
                            "analyze: skipped column %s.%s.%s after a failed probe",
                            database,
                            table,
                            col_name,
                        )
                        skipped.append(col_name)
                        continue
                    stats["columns"][col_name] = col_stats

                if skipped:
                    stats["skipped_columns"] = skipped

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

        db = self.get_database(database)
        tbl = self._qualified_table(db, table)
        date_col = Identifier(date_column)

        if value_column:
            val_col = Identifier(value_column)
            # round(...::numeric, n) instead of ::numeric(20,2): the fixed
            # precision overflows on large SUM/AVG (only 18 integer digits).
            q = SQL(
                "SELECT date_trunc(%s, {date_col})::date AS period, "
                "COUNT(*) AS count, "
                "round(AVG({val_col})::numeric, 4) AS avg_value, "
                "round(SUM({val_col})::numeric, 2) AS total "
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

        limit = self._settings.row_limit

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_table_exists(cur, db, table, deadline=deadline)

                # Validate column types up front so the agent gets a clear
                # ValueError instead of an opaque Postgres error from
                # date_trunc/AVG on the wrong type.
                wanted = [date_column] + ([value_column] if value_column else [])
                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                        AND column_name = ANY(%s)
                """,
                    (db.schema, table, wanted),
                    deadline=deadline,
                )
                col_types = {r[0]: r[1] for r in cur.fetchall()}

                if date_column not in col_types:
                    raise ValueError(
                        f"Column '{date_column}' not found in table '{table}'"
                    )
                if col_types[date_column] not in _DATE_TRUNCABLE_TYPES:
                    raise ValueError(
                        f"date_column '{date_column}' has type "
                        f"'{col_types[date_column]}', expected a date/timestamp type"
                    )
                if value_column:
                    if value_column not in col_types:
                        raise ValueError(
                            f"Column '{value_column}' not found in table '{table}'"
                        )
                    if col_types[value_column] not in NUMERIC_TYPES:
                        raise ValueError(
                            f"value_column '{value_column}' has type "
                            f"'{col_types[value_column]}', expected a numeric type"
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
                self._ensure_table_exists(cur, db, table, deadline=deadline)
                self._execute(
                    cur,
                    SQL("SELECT * FROM {} LIMIT %s").format(
                        self._qualified_table(db, table)
                    ),
                    (limit,),
                    deadline=deadline,
                )
                if cur.description is None:
                    return QueryResult(columns=[], rows=[], row_count=0)

                columns = [desc[0] for desc in cur.description]
                rows = [list(row) for row in cur.fetchall()]
                if self._settings.redact_sensitive:
                    rows = _redact_rows(columns, rows)
                return QueryResult(
                    columns=columns,
                    rows=rows,
                    row_count=len(rows),
                )

    def _column_anomalies(
        self,
        cur,
        tbl: Identifier,
        col_name: str,
        data_type: str,
        row_count: int,
        *,
        deadline: float,
    ) -> list[dict]:
        col = Identifier(col_name)
        found: list[dict] = []

        # High null rate
        self._execute(
            cur,
            SQL("SELECT COUNT(*) FROM {} WHERE {} IS NULL").format(tbl, col),
            deadline=deadline,
        )
        null_count = cur.fetchone()[0]
        null_pct = round(null_count / row_count * 100, 1)
        if null_pct > 50:
            found.append(
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
            found.append(
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
                        SQL("SELECT COUNT(*) FROM {} WHERE {} < %s OR {} > %s").format(
                            tbl, col, col
                        ),
                        (lower, upper),
                        deadline=deadline,
                    )
                    outlier_count = cur.fetchone()[0]
                    if outlier_count > 0:
                        found.append(
                            {
                                "type": "outliers",
                                "column": col_name,
                                "detail": f"{outlier_count:,} outliers (IQR: {lower:.1f}..{upper:.1f})",
                            }
                        )

        # Date in the future. Compare against the matching "now" for the type:
        # NOW() is a timestamptz, so comparing date / timestamp-without-tz
        # columns against it is session-timezone-dependent (off by hours).
        future_now = {
            "date": "CURRENT_DATE",
            "timestamp without time zone": "LOCALTIMESTAMP",
            "timestamp with time zone": "NOW()",
        }.get(data_type)
        if future_now:
            self._execute(
                cur,
                SQL("SELECT COUNT(*) FROM {} WHERE {} > {}").format(
                    tbl, col, SQL(future_now)
                ),
                deadline=deadline,
            )
            future_count = cur.fetchone()[0]
            if future_count > 0:
                found.append(
                    {
                        "type": "future_dates",
                        "column": col_name,
                        "detail": f"{future_count:,} rows with dates in the future",
                    }
                )

        # Possible duplicates: identifier-looking columns (id/email/uuid), or
        # nearly-unique columns that likely miss a uniqueness constraint.
        non_null = row_count - null_count
        dup_count = non_null - distinct
        looks_unique = distinct == non_null and distinct > 10
        if not looks_unique and dup_count > 0 and distinct > 1:
            nearly_unique = non_null > 0 and distinct / non_null > 0.95
            if _looks_like_identifier_column(col_name) or nearly_unique:
                found.append(
                    {
                        "type": "possible_duplicates",
                        "column": col_name,
                        "detail": (
                            f"{dup_count:,} duplicate rows beyond "
                            f"{distinct:,} distinct values"
                        ),
                    }
                )

        return found

    def find_anomalies(self, database: str, table: str) -> dict:
        self._validate_identifier(table)
        db = self.get_database(database)

        anomalies = []
        skipped: list[str] = []

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_table_exists(cur, db, table, deadline=deadline)

                tbl = self._qualified_table(db, table)

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

                self._check_profile_row_limit(table, row_count)

                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """,
                    (db.schema, table),
                    deadline=deadline,
                )
                columns = cur.fetchall()

                if len(columns) > _MAX_COLUMNS_FOR_STATS:
                    raise ValueError(
                        f"Table '{table}' has {len(columns)} columns "
                        f"(max {_MAX_COLUMNS_FOR_STATS} for anomaly detection)"
                    )

                for idx, (col_name, data_type) in enumerate(columns):
                    try:
                        with self._savepoint(cur, f"dbecho_col_{idx}"):
                            found = self._column_anomalies(
                                cur,
                                tbl,
                                col_name,
                                data_type,
                                row_count,
                                deadline=deadline,
                            )
                    except (TimeoutError, psycopg.errors.QueryCanceled):
                        raise
                    except Exception:
                        logger.warning(
                            "anomalies: skipped column %s.%s.%s after a failed probe",
                            database,
                            table,
                            col_name,
                        )
                        skipped.append(col_name)
                        continue
                    anomalies.extend(found)

        result = {
            "table": table,
            "database": database,
            "row_count": row_count,
            "anomalies": anomalies,
        }
        if skipped:
            result["skipped_columns"] = skipped
        return result

    def get_table_schema(self, database: str, table: str) -> TableInfo:
        """Schema for a single table — far cheaper than get_schema() when the
        agent only needs one table out of a large database."""
        self._validate_identifier(table)
        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._ensure_table_exists(cur, db, table, deadline=deadline)

                self._execute(
                    cur,
                    """
                    SELECT
                        obj_description((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass) AS comment,
                        COALESCE(s.n_live_tup, 0) AS row_count,
                        COALESCE(pg_total_relation_size((quote_ident(t.table_schema) || '.' || quote_ident(t.table_name))::regclass), 0) AS size_bytes
                    FROM information_schema.tables t
                    LEFT JOIN pg_stat_user_tables s ON s.relname = t.table_name AND s.schemaname = t.table_schema
                    WHERE t.table_schema = %s AND t.table_name = %s
                        AND t.table_type = 'BASE TABLE'
                """,
                    (db.schema, table),
                    deadline=deadline,
                )
                row = cur.fetchone()
                comment, row_count, size_bytes = row if row else (None, 0, 0)

                self._execute(
                    cur,
                    """
                    SELECT column_name, data_type, is_nullable, column_default
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = %s
                    ORDER BY ordinal_position
                """,
                    (db.schema, table),
                    deadline=deadline,
                )
                col_rows = cur.fetchall()

                self._execute(
                    cur,
                    """
                    SELECT kcu.column_name
                    FROM information_schema.table_constraints tc
                    JOIN information_schema.key_column_usage kcu
                        ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
                    WHERE tc.constraint_type = 'PRIMARY KEY' AND tc.table_schema = %s
                        AND tc.table_name = %s
                """,
                    (db.schema, table),
                    deadline=deadline,
                )
                pk_cols = {r[0] for r in cur.fetchall()}

        columns = [
            ColumnInfo(
                name=cname,
                data_type=dtype,
                nullable=nullable == "YES",
                default=default,
                is_primary_key=cname in pk_cols,
            )
            for cname, dtype, nullable, default in col_rows
        ]
        return TableInfo(
            name=table,
            comment=comment,
            columns=columns,
            row_count=row_count,
            size_bytes=size_bytes,
        )

    def get_indexes(self, database: str, table: str | None = None) -> list[dict]:
        """Non-primary-key indexes in the configured schema (optionally one
        table): name, columns, uniqueness. PKs are already shown as [PK] in
        schemas."""
        if table is not None:
            self._validate_identifier(table)
        db = self.get_database(database)

        sql = """
            SELECT
                t.relname AS table_name,
                i.relname AS index_name,
                ix.indisunique AS is_unique,
                array_to_string(
                    array_agg(COALESCE(a.attname, '(expr)') ORDER BY k.ord), ', '
                ) AS columns
            FROM pg_index ix
            JOIN pg_class i ON i.oid = ix.indexrelid
            JOIN pg_class t ON t.oid = ix.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            JOIN LATERAL unnest(ix.indkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
            LEFT JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
            WHERE n.nspname = %s AND NOT ix.indisprimary
        """
        params: list = [db.schema]
        if table is not None:
            sql += " AND t.relname = %s"
            params.append(table)
        sql += """
            GROUP BY t.relname, i.relname, ix.indisunique
            ORDER BY t.relname, i.relname
            LIMIT %s
        """
        params.append(_MAX_FK_ROWS)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                self._execute(cur, sql, tuple(params), deadline=deadline)
                return [
                    {
                        "table": r[0],
                        "name": r[1],
                        "unique": r[2],
                        "columns": r[3],
                    }
                    for r in cur.fetchall()
                ]

    def explain(self, database: str, sql: str) -> dict:
        """Planner estimates (cost, rows) for a SELECT/WITH query without
        executing it. EXPLAIN without ANALYZE only plans."""
        validated = self.validate_sql(sql)
        first_word = _strip_strings_and_comments(validated).strip().split()[0].upper()
        if first_word not in ("SELECT", "WITH"):
            raise ValueError("explain only supports SELECT and WITH queries")

        db = self.get_database(database)

        with self._connect(db) as conn:
            with conn.cursor() as cur:
                deadline = self._new_deadline()
                # validated is whitelist-checked (no writes, no blocked
                # functions, no ";"), and plain EXPLAIN never executes the body.
                self._execute(
                    cur,
                    SQL("EXPLAIN (FORMAT JSON) ") + SQL(validated),
                    deadline=deadline,
                )
                row = cur.fetchone()

        plan_doc = row[0] if row else None
        node = {}
        if isinstance(plan_doc, list) and plan_doc and isinstance(plan_doc[0], dict):
            node = plan_doc[0].get("Plan", {}) or {}
        return {
            "node_type": node.get("Node Type"),
            "total_cost": node.get("Total Cost"),
            "estimated_rows": node.get("Plan Rows"),
            "plan": plan_doc,
        }
