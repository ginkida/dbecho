from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from dbecho.config import load_config, find_config
from dbecho.db import DatabaseManager

logger = logging.getLogger("dbecho")

# Cap stringification of individual cells well before the 60-char display
# truncation so a multi-MB text/jsonb cell never gets fully measured/padded.
_MAX_CELL_CHARS = 200
# Generous cap on the compare() fan-out so a repeated/attacker-supplied list
# cannot multiply an expensive query arbitrarily.
_MAX_COMPARE_DBS = 20

mcp = FastMCP(
    "dbecho",
    instructions=(
        "dbecho is a multi-database PostgreSQL analytics server. "
        "Start with list_databases or summary to see available databases. "
        "Use schema to explore structure (describe for a single table), "
        "query for SQL (explain to preview cost first), analyze to profile "
        "tables, compare to cross-reference databases, trend for time series, "
        "anomalies to find data issues, sample to preview rows, "
        "erd to see relationships, and health to check connectivity."
    ),
)

_manager: DatabaseManager | None = None


def _get_manager() -> DatabaseManager:
    global _manager
    if _manager is not None:
        return _manager

    config_path = None
    for arg in sys.argv:
        if arg.startswith("--config="):
            config_path = Path(arg.split("=", 1)[1]).expanduser()
            break

    if config_path is None:
        for i, arg in enumerate(sys.argv):
            if arg == "--config" and i + 1 < len(sys.argv):
                config_path = Path(sys.argv[i + 1]).expanduser()
                break

    if config_path is None:
        config_path = find_config()

    if config_path is None or not config_path.exists():
        raise FileNotFoundError(
            "Config not found. Create dbecho.toml in current dir, "
            "~/.config/dbecho/config.toml, or pass --config=<path>"
        )

    config = load_config(config_path)
    _manager = DatabaseManager(config)
    return _manager


def _format_cell(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(v))} bytes>"
    if isinstance(v, (dict, list)):
        s = json.dumps(v, ensure_ascii=False, default=str)
    else:
        s = str(v)
    if len(s) > _MAX_CELL_CHARS:
        s = s[: _MAX_CELL_CHARS - 1] + "…"
    return s


def _fit(value: str, width: int) -> str:
    """Pad to width; mark truncation with an ellipsis instead of a silent cut
    so the agent never mistakes a clipped value for the complete one."""
    if len(value) > width:
        return value[: width - 1] + "…"
    return value.ljust(width)


def _format_table(columns: list[str], rows: list[list]) -> str:
    if not columns:
        return "(no columns)"
    if not rows:
        return "(no rows)"

    ncols = len(columns)
    str_rows = []
    for row in rows:
        padded = list(row) + [None] * (ncols - len(row))
        str_rows.append([_format_cell(v) for v in padded[:ncols]])

    widths = [
        min(max(len(columns[i]), *(len(r[i]) for r in str_rows)), 60)
        for i in range(ncols)
    ]

    header = " | ".join(_fit(c, w) for c, w in zip(columns, widths))
    separator = "-+-".join("-" * w for w in widths)
    body = "\n".join(
        " | ".join(_fit(v, w) for v, w in zip(row, widths)) for row in str_rows
    )

    return f"{header}\n{separator}\n{body}"


def _to_json(result) -> str:
    """Machine-parseable form of a QueryResult; str() fallback covers
    datetime/Decimal/UUID/bytes and anything else non-JSON-native."""
    return json.dumps(
        {
            "columns": result.columns,
            "rows": result.rows,
            "row_count": result.row_count,
            "truncated": result.truncated,
        },
        ensure_ascii=False,
        default=str,
    )


def _format_size(size_bytes: int | float) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} PB"


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_databases() -> str:
    """List all configured PostgreSQL databases with their descriptions."""
    mgr = _get_manager()
    lines = []
    for name in mgr.database_names:
        db = mgr.get_database(name)
        desc = f" -- {db.description}" if db.description else ""
        lines.append(f"- {name}{desc}")
    return "\n".join(lines) if lines else "No databases configured."


@mcp.tool()
def health() -> str:
    """Check connectivity and basic info for all configured databases."""
    mgr = _get_manager()
    lines = []
    for name in mgr.database_names:
        info = mgr.check_connection(name)
        if info["status"] == "ok":
            lines.append(f"[OK] {name}: {info['version']} | {info['size']}")
        else:
            lines.append(f"[FAIL] {name}: {info['error']}")
    return "\n".join(lines)


@mcp.tool()
def schema(database: str) -> str:
    """Get the full schema of a database: tables, columns, types, primary keys, row counts, and sizes.

    Row counts are PostgreSQL planner estimates (0 for never-analyzed tables);
    use analyze or query for exact counts.

    Args:
        database: Name of the database from config (use list_databases to see available).
    """
    mgr = _get_manager()
    try:
        tables = mgr.get_schema(database)
    except ValueError as e:
        return f"Schema error: {e}"
    except TimeoutError as e:
        return f"Schema error: {e}"
    except Exception:
        logger.exception("Schema failed on %s", database)
        return "Schema error: unexpected failure (check server logs)"

    if not tables:
        schema_name = mgr.get_database(database).schema
        return f"No tables found in '{database}' (schema '{schema_name}')."

    lines = [f"Schema for '{database}': {len(tables)} tables\n"]
    for t in tables:
        comment = f"  -- {t.comment}" if t.comment else ""
        size = _format_size(t.size_bytes)
        lines.append(f"## {t.name} (~{t.row_count:,} rows est., {size}){comment}")
        for col in t.columns:
            pk = " [PK]" if col.is_primary_key else ""
            nullable = "NULL" if col.nullable else "NOT NULL"
            default = f" DEFAULT {col.default}" if col.default else ""
            lines.append(f"  {col.name}: {col.data_type} {nullable}{default}{pk}")
        lines.append("")

    if mgr.schema_truncated(database):
        lines.append("(table list truncated — database exceeds the schema cap)")

    return "\n".join(lines)


@mcp.tool()
def query(database: str, sql: str, offset: int = 0, format: str = "table") -> str:
    """Execute a read-only SQL query on a database and return the results.

    Only SELECT, WITH, EXPLAIN, and SHOW queries are allowed.

    Args:
        database: Name of the database from config.
        sql: SQL query to execute (read-only).
        offset: Rows to skip before collecting results — use to page through
            truncated results (stable only with ORDER BY).
        format: "table" (default, ASCII table) or "json" ({columns, rows,
            row_count, truncated}).
    """
    if format not in ("table", "json"):
        return f"Query error: unknown format '{format}'. Use: table, json"
    mgr = _get_manager()
    try:
        result = mgr.query(database, sql, offset=offset)
    except ValueError as e:
        return f"Query error: {e}"
    except TimeoutError as e:
        return f"Query error: {e}"
    except Exception:
        logger.exception("Query failed on %s", database)
        return "Query error: unexpected failure (check server logs)"

    if format == "json":
        return _to_json(result)

    output = _format_table(result.columns, result.rows)
    suffix = (
        f"(truncated to {result.row_count} rows — pass offset={offset + result.row_count} for more)"
        if result.truncated
        else f"({result.row_count} rows)"
    )
    return f"{output}\n\n{suffix}"


@mcp.tool()
def describe(database: str, table: str) -> str:
    """Describe a single table: columns, types, primary key, indexes, row estimate, size.

    Much cheaper than schema when you only need one table.

    Args:
        database: Name of the database from config.
        table: Name of the table to describe.
    """
    mgr = _get_manager()
    try:
        info = mgr.get_table_schema(database, table)
        indexes = mgr.get_indexes(database, table)
    except ValueError as e:
        return f"Describe error: {e}"
    except TimeoutError as e:
        return f"Describe error: {e}"
    except Exception:
        logger.exception("Describe failed on %s.%s", database, table)
        return "Describe error: unexpected failure (check server logs)"

    comment = f"  -- {info.comment}" if info.comment else ""
    size = _format_size(info.size_bytes)
    lines = [f"## {info.name} (~{info.row_count:,} rows est., {size}){comment}"]
    for col in info.columns:
        pk = " [PK]" if col.is_primary_key else ""
        nullable = "NULL" if col.nullable else "NOT NULL"
        default = f" DEFAULT {col.default}" if col.default else ""
        lines.append(f"  {col.name}: {col.data_type} {nullable}{default}{pk}")

    if indexes:
        lines.append("\nIndexes:")
        for ix in indexes:
            unique = " UNIQUE" if ix["unique"] else ""
            lines.append(f"  {ix['name']}{unique} ({ix['columns']})")

    return "\n".join(lines)


@mcp.tool()
def explain(database: str, sql: str) -> str:
    """Show the query plan with estimated cost and row count WITHOUT executing the query.

    Use before running a potentially expensive query to judge whether it fits
    the timeout. Only SELECT and WITH queries can be explained.

    Args:
        database: Name of the database from config.
        sql: SELECT/WITH query to plan (not executed).
    """
    mgr = _get_manager()
    try:
        plan = mgr.explain(database, sql)
    except ValueError as e:
        return f"Explain error: {e}"
    except TimeoutError as e:
        return f"Explain error: {e}"
    except Exception:
        logger.exception("Explain failed on %s", database)
        return "Explain error: unexpected failure (check server logs)"

    lines = [
        f"Node: {plan['node_type']}",
        f"Estimated total cost: {plan['total_cost']}",
        f"Estimated rows: {plan['estimated_rows']}",
        "",
        json.dumps(plan["plan"], ensure_ascii=False, default=str, indent=2),
    ]
    return "\n".join(lines)


@mcp.tool()
def analyze(database: str, table: str) -> str:
    """Profile a table: row count, column types, null percentages, distinct values, min/max/avg for numeric columns, top values for low-cardinality text columns.

    Args:
        database: Name of the database from config.
        table: Name of the table to analyze.
    """
    mgr = _get_manager()
    try:
        stats = mgr.get_table_stats(database, table)
    except ValueError as e:
        return f"Analyze error: {e}"
    except TimeoutError as e:
        return f"Analyze error: {e}"
    except Exception:
        logger.exception("Analyze failed on %s.%s", database, table)
        return "Analyze error: unexpected failure (check server logs)"

    lines = [
        f"Table: {stats['database']}.{stats['table']}",
        f"Rows: {stats['row_count']:,}",
        "",
    ]

    for col_name, col in stats["columns"].items():
        parts = [f"  {col_name} ({col['type']})"]
        parts.append(f"    distinct: {col['distinct']:,}")
        parts.append(f"    null: {col['null_count']:,} ({col['null_pct']}%)")

        if "avg" in col:
            parts.append(f"    min: {col['min']}  max: {col['max']}  avg: {col['avg']}")
        elif "min" in col:
            parts.append(f"    range: {col['min']} .. {col['max']}")

        if "top_values" in col:
            top = ", ".join(
                f"{v['value']}({v['count']}, {v['pct']}%)"
                for v in col["top_values"][:5]
            )
            parts.append(f"    top: {top}")

        lines.append("\n".join(parts))

    if stats.get("skipped_columns"):
        lines.append(
            f"\n(skipped columns after failed probes: "
            f"{', '.join(stats['skipped_columns'])})"
        )

    return "\n".join(lines)


@mcp.tool()
def compare(sql: str, databases: list[str] | None = None) -> str:
    """Run the same SQL query across multiple databases and compare results side by side.

    Args:
        sql: SQL query to execute on each database (must be SELECT).
        databases: List of database names to compare. If omitted, runs on all
            databases (capped at 20 per call).
    """
    mgr = _get_manager()

    try:
        DatabaseManager.validate_sql(sql)
    except ValueError as e:
        return f"Query error: {e}"

    # Dedup (repeats add nothing) and cap so the list cannot multiply an
    # expensive query's cost arbitrarily.
    all_names = list(dict.fromkeys(databases or mgr.database_names))
    db_names = all_names[:_MAX_COMPARE_DBS]

    results = {}
    for name in db_names:
        try:
            result = mgr.query(name, sql)
            results[name] = result
        except ValueError as e:
            results[name] = str(e)
        except TimeoutError as e:
            results[name] = str(e)
        except Exception:
            logger.exception("Compare query failed on %s", name)
            results[name] = "unexpected failure (check server logs)"

    lines = []
    for name, result in results.items():
        lines.append(f"## {name}")
        if isinstance(result, str):
            lines.append(f"Error: {result}")
        else:
            lines.append(_format_table(result.columns, result.rows))
            lines.append(f"({result.row_count} rows)")
        lines.append("")

    if len(all_names) > _MAX_COMPARE_DBS:
        lines.append(
            f"Note: compared the first {_MAX_COMPARE_DBS} of {len(all_names)} "
            f"databases (cap). Pass an explicit databases=[...] subset for the rest."
        )

    return "\n".join(lines)


@mcp.tool()
def summary() -> str:
    """Get a quick overview of all databases: table counts, total rows (planner estimates), largest tables, database sizes."""
    mgr = _get_manager()

    lines = []
    for name in mgr.database_names:
        db = mgr.get_database(name)
        try:
            tables = mgr.get_schema(name)
            total_rows = sum(t.row_count for t in tables)
            total_size = sum(t.size_bytes for t in tables)
            sorted_tables = sorted(tables, key=lambda t: t.row_count, reverse=True)
            top3 = ", ".join(f"{t.name}(~{t.row_count:,})" for t in sorted_tables[:3])

            desc = f" -- {db.description}" if db.description else ""
            lines.append(f"## {name}{desc}")
            lines.append(f"  Tables: {len(tables)}")
            lines.append(f"  Total rows: ~{total_rows:,} (est.)")
            lines.append(f"  Total size: {_format_size(total_size)}")
            if top3:
                lines.append(f"  Largest: {top3}")
            lines.append("")
        except ValueError as e:
            lines.append(f"## {name}")
            lines.append(f"  Error: {e}")
            lines.append("")
        except Exception:
            # Raw exceptions can embed connection details — log for the
            # operator, keep the agent-visible message generic.
            logger.exception("Summary failed on %s", name)
            lines.append(f"## {name}")
            lines.append("  Error: unavailable (check server logs)")
            lines.append("")

    return "\n".join(lines) if lines else "No databases configured."


@mcp.tool()
def trend(
    database: str,
    table: str,
    date_column: str,
    value_column: str | None = None,
    period: str = "month",
) -> str:
    """Analyze time series data: group rows by time period and show counts, averages, and totals.

    Args:
        database: Name of the database from config.
        table: Name of the table.
        date_column: Name of the date/timestamp column to group by.
        value_column: Optional numeric column to aggregate (avg, sum). If omitted, shows counts only.
        period: Grouping period: day, week, month, quarter, year. Default: month.
    """
    mgr = _get_manager()
    try:
        result = mgr.get_trend(database, table, date_column, value_column, period)
    except ValueError as e:
        return f"Trend error: {e}"
    except TimeoutError as e:
        return f"Trend error: {e}"
    except Exception:
        logger.exception("Trend failed on %s.%s", database, table)
        return "Trend error: unexpected failure (check server logs)"

    output = _format_table(result.columns, result.rows)
    suffix = (
        f"(truncated to {result.row_count} rows)"
        if result.truncated
        else f"({result.row_count} rows)"
    )
    return f"{output}\n\n{suffix}"


@mcp.tool()
def anomalies(database: str, table: str) -> str:
    """Find data quality issues in a table: high null rates, single-value columns, numeric outliers, future dates, possible duplicates.

    Args:
        database: Name of the database from config.
        table: Name of the table to check.
    """
    mgr = _get_manager()
    try:
        result = mgr.find_anomalies(database, table)
    except ValueError as e:
        return f"Anomalies error: {e}"
    except TimeoutError as e:
        return f"Anomalies error: {e}"
    except Exception:
        logger.exception("Anomalies failed on %s.%s", database, table)
        return "Anomalies error: unexpected failure (check server logs)"

    lines = [
        f"Anomaly report: {result['database']}.{result['table']}",
        f"Rows: {result['row_count']:,}",
        "",
    ]

    if not result["anomalies"]:
        lines.append("No anomalies detected.")
    else:
        lines.append(f"Found {len(result['anomalies'])} issue(s):\n")
        for a in result["anomalies"]:
            lines.append(f"  [{a['type']}] {a['column']}: {a['detail']}")

    if result.get("skipped_columns"):
        lines.append(
            f"\n(skipped columns after failed probes: "
            f"{', '.join(result['skipped_columns'])})"
        )

    return "\n".join(lines)


@mcp.tool()
def sample(database: str, table: str, limit: int = 5) -> str:
    """Show sample rows from a table to understand the data format.

    Args:
        database: Name of the database from config.
        table: Name of the table.
        limit: Number of rows to return (default 5, max 50).
    """
    limit = max(1, min(limit, 50))
    mgr = _get_manager()
    try:
        result = mgr.get_sample(database, table, limit)
    except ValueError as e:
        return f"Sample error: {e}"
    except TimeoutError as e:
        return f"Sample error: {e}"
    except Exception:
        logger.exception("Sample failed on %s.%s", database, table)
        return "Sample error: unexpected failure (check server logs)"

    return _format_table(result.columns, result.rows) + f"\n\n({result.row_count} rows)"


@mcp.tool()
def erd(database: str) -> str:
    """Show entity-relationship diagram as text: tables, primary keys, and foreign key relationships.

    Args:
        database: Name of the database from config.
    """
    mgr = _get_manager()
    try:
        tables = mgr.get_schema(database)
        fks = mgr.get_foreign_keys(database)
    except ValueError as e:
        return f"ERD error: {e}"
    except TimeoutError as e:
        return f"ERD error: {e}"
    except Exception:
        logger.exception("ERD failed on %s", database)
        return "ERD error: unexpected failure (check server logs)"

    lines = [f"ERD for '{database}'\n"]

    for t in tables:
        pk_cols = [c.name for c in t.columns if c.is_primary_key]
        pk_str = f" (PK: {', '.join(pk_cols)})" if pk_cols else ""
        lines.append(f"[{t.name}]{pk_str} -- ~{t.row_count:,} rows (est.)")

    if fks:
        lines.append("\nRelationships:")
        for fk in fks:
            lines.append(
                f"  {fk.source_table}.{fk.source_column} -> {fk.target_table}.{fk.target_column}"
            )
    else:
        lines.append("\nNo foreign key relationships found.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MCP Resources
# ---------------------------------------------------------------------------


@mcp.resource("dbecho://databases")
def resource_databases() -> str:
    """List of all configured databases."""
    try:
        return list_databases()
    except Exception:
        # Config errors can embed env var names and paths — log, stay generic.
        logger.exception("Resource databases failed")
        return "Error loading databases (check server logs)"


@mcp.resource("dbecho://databases/{database}/schema")
def resource_schema(database: str) -> str:
    """Schema for a specific database."""
    try:
        return schema(database)
    except ValueError as e:
        return f"Error loading schema for '{database}': {e}"
    except Exception:
        logger.exception("Resource schema failed on %s", database)
        return f"Error loading schema for '{database}' (check server logs)"


@mcp.resource("dbecho://databases/{database}/summary")
def resource_summary(database: str) -> str:
    """Summary for a specific database."""
    try:
        mgr = _get_manager()
        tables = mgr.get_schema(database)
        total_rows = sum(t.row_count for t in tables)
        total_size = sum(t.size_bytes for t in tables)
        table_list = "\n".join(
            f"  - {t.name}: ~{t.row_count:,} rows (est.), {_format_size(t.size_bytes)}"
            for t in tables
        )
        return f"Database: {database}\nTables: {len(tables)}\nTotal rows: ~{total_rows:,} (est.)\nTotal size: {_format_size(total_size)}\n\n{table_list}"
    except ValueError as e:
        return f"Error loading summary for '{database}': {e}"
    except Exception:
        logger.exception("Resource summary failed on %s", database)
        return f"Error loading summary for '{database}' (check server logs)"


# ---------------------------------------------------------------------------
# MCP Prompts
# ---------------------------------------------------------------------------


@mcp.prompt()
def explore_database(database: str) -> str:
    """Guided exploration of a database: schema, summary, sample data from key tables."""
    return (
        f"I want to explore the '{database}' database. Please:\n"
        f"1. Show me the schema using the schema tool\n"
        f"2. Give me a summary with the summary tool\n"
        f"3. Show sample data from the 3 largest tables using the sample tool\n"
        f"4. Check for any data anomalies in the largest table using the anomalies tool\n"
        f"5. Summarize what this database is about based on the structure and data"
    )


@mcp.prompt()
def compare_databases() -> str:
    """Compare all databases: find common tables, compare row counts and structures."""
    return (
        "I want to compare all my databases. Please:\n"
        "1. List all databases with the list_databases tool\n"
        "2. Get the schema for each database\n"
        "3. Identify any tables with similar names across databases\n"
        "4. Compare row counts across databases using the compare tool with: SELECT COUNT(*) as total_rows FROM <common_table>\n"
        "5. Summarize the differences and similarities"
    )


@mcp.prompt()
def data_quality_report(database: str) -> str:
    """Run a comprehensive data quality check on all tables in a database."""
    return (
        f"Run a data quality audit on the '{database}' database:\n"
        f"1. Get the schema to see all tables\n"
        f"2. Run the anomalies tool on each table\n"
        f"3. Compile a report with:\n"
        f"   - Tables with the most issues\n"
        f"   - Most common anomaly types\n"
        f"   - Specific recommendations for fixing each issue\n"
        f"4. Rate the overall data quality from 1-10"
    )


def main():
    # stdio MCP uses stdout for the JSON-RPC protocol — force all logging to
    # stderr regardless of what any imported library configured first.
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, force=True)

    # Validate config eagerly so a broken config (missing file, unset ${VAR},
    # malformed TOML) fails loudly at launch for the operator instead of
    # surfacing mid-session on the first tool call. No DB I/O happens here.
    try:
        _get_manager()
    except Exception as e:
        logger.error("Startup failed: %s", e)
        raise SystemExit(1)

    mcp.run()


if __name__ == "__main__":
    main()
