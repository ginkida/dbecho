# dbecho

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![MCPAmpel](https://img.shields.io/endpoint?url=https://mcpampel.com/badge/ginkida/dbecho.json)](https://mcpampel.com/repo/ginkida/dbecho)

<p align="center">
  <img src="https://minio.ginkida.dev/minion/github/dbecho.png" alt="dbecho mascot" width="400" height="400">
</p>

> Talk to your PostgreSQL databases through AI. No dashboards, no BI tools, just questions and answers.

**dbecho** is an [MCP server](https://modelcontextprotocol.io/) that gives AI agents (Claude Code, Cursor, Windsurf, or any MCP client) direct read-only access to your PostgreSQL databases. Point it at your databases, ask questions in plain language, get instant analytics.

```
You: What are my most popular blog posts and why?
Claude: [runs schema → query → analyze → trend across 29 tables]
       Here's what the data shows...
```

## What can it do?

**14 tools** that cover the full analytics workflow:

| Tool | Purpose |
|------|---------|
| `list_databases` | Show all connected databases |
| `health` | Check connectivity, PostgreSQL version, database size |
| `schema` | Full schema: tables, columns, types, PKs, row counts, sizes |
| `describe` | One table in depth: columns, PK, indexes, size — cheaper than `schema` |
| `find` | Locate tables and columns by name substring, across all databases at once |
| `query` | Run read-only SQL (SELECT, WITH, EXPLAIN, SHOW), with offset paging and JSON output |
| `explain` | Query plan with estimated cost/rows — judge a query before running it |
| `analyze` | Profile a table: nulls, cardinality, distributions, top values |
| `compare` | Same query across multiple databases, side by side |
| `summary` | Overview: table counts, total rows, largest tables |
| `trend` | Time-series: counts/averages grouped by day/week/month/year, with JSON output |
| `anomalies` | Data quality: high nulls, outliers, duplicates, future dates |
| `sample` | Preview rows from any table |
| `erd` | Entity-relationship diagram: PKs and foreign keys |

Plus **3 MCP Resources** (schema/summary per database) and **3 MCP Prompts** (guided exploration, cross-database comparison, data quality audit).

**Partitioned tables** are reported as the parent, not as a pile of children:
`schema`, `find`, `summary` and `erd` hide partition children and mark the parent
`[partitioned]`, with row estimates and size summed across the whole partition
tree (a parent stores nothing itself, so the raw catalog would show the biggest
table in the database as empty). Query the parent and let PostgreSQL prune;
`describe <child>` still works if you name a partition explicitly.

## Why dbecho?

**The problem:** You have PostgreSQL databases across projects. Getting answers means context-switching to psql, pgAdmin, or a BI tool, writing SQL, formatting results, then bringing insights back to your conversation.

**The fix:** dbecho stays inside your AI agent's workflow. The agent explores schema, writes SQL, cross-references tables, and builds analysis without you leaving the conversation. One config file, zero context switches.

What makes it different from just giving an agent a connection string:
- **Multi-database.** Connect 1 or 20 databases. Compare across them with one tool call.
- **Safe by default.** Read-only connections, query timeouts, row limits, SQL injection prevention. You can't accidentally `DROP TABLE`.
- **Agent-optimized output.** Schema, stats, and query results are formatted so LLMs parse them efficiently, not as raw psql dumps.
- **Zero infrastructure.** No containers, no web UI, no background processes. A Python package that speaks MCP over stdio.

## Example session

```
You: Show me what's in the database

Claude: [calls list_databases → schema → summary]

       Database: ecommerce (12 tables, 847 MB)
       Largest: orders (2.1M rows), products (45K rows), users (128K rows)

You: How are sales trending this quarter?

Claude: [calls trend on orders table, grouped by week]

       Week        | orders | total_revenue
       2026-01-06  | 3,241  | $482,100
       2026-01-13  | 3,892  | $571,340
       2026-01-20  | 2,107  | $298,500  ← 46% drop
       ...
       Significant drop in week of Jan 20. Let me check what happened.

       [calls query to investigate, then anomalies on orders table]

       Found: 89% of orders that week have status='cancelled'.
       The cancellation spike correlates with a payment gateway outage
       (payments table shows 0 successful transactions Jan 19-21).

You: Any data quality issues I should worry about?

Claude: [calls anomalies on each table]

       Found 4 issues:
       - users.email: 342 possible duplicates
       - orders.shipped_at: 127 dates in the future
       - products.price: 23 outliers (IQR: $5..$89, found items at $0 and $9,999)
       - events.user_id: 94.2% NULL (missing user attribution)
```

One conversation, zero context switches. The agent picks the right tools automatically.

## Quick start

### 1. Install

```bash
pip install dbecho
```

Or from source:

```bash
git clone https://github.com/ginkida/dbecho.git
cd dbecho
pip install .
```

Requires Python 3.10+ and PostgreSQL 12 or newer.

### 2. Configure

Create `dbecho.toml` in your project directory:

```toml
[databases.myapp]
url = "postgres://user:pass@localhost:5432/myapp"
description = "Main application"

[databases.analytics]
url = "postgres://user:pass@localhost:5432/analytics"
description = "Analytics warehouse"

[settings]
row_limit = 500           # max rows returned per query (default: 500)
query_timeout = 30        # seconds before query is killed (default: 30)
max_profile_rows = 5000000  # refuse analyze/anomalies above this row count
redact_sensitive = true   # redact password/token/secret-like columns in output
```

Environment variables work with `${VAR}` syntax:

```toml
[databases.production]
url = "${DATABASE_URL}"
description = "Production (read replica)"
```

If an app keeps its tables outside `public`, set `schema` (default `"public"`,
lowercase identifier). All metadata tools (`schema`, `describe`, `analyze`, ...)
target it, and it leads `search_path` so raw queries can use unqualified table
names:

```toml
[databases.events]
url = "${EVENTS_DATABASE_URL}"
schema = "analytics"
```

Verify the config and connectivity before wiring up your MCP client:

```bash
dbecho --check     # validate config + ping every database
dbecho --version   # print "dbecho <version>"
dbecho --help      # usage and flags
```

`--check` prints a `[OK]`/`[FAIL] <name>: …` line per database and exits 0 only
when every database responds (non-zero otherwise), so it drops straight into a
CI or healthcheck script.

Exit codes: `0` success, `1` config or startup failure (also a failed `--check`),
`2` usage error. Unrecognised arguments are rejected rather than ignored, so a
typo like `--verison` never starts a server that quietly did nothing.

### 3. Connect to your MCP client

**Claude Code** (project-level, recommended):

Create `.mcp.json` in your project root:

```json
{
  "mcpServers": {
    "dbecho": {
      "command": "dbecho",
      "args": ["--config", "/path/to/dbecho.toml"]
    }
  }
}
```

**Claude Code** (global):

Add to `~/.claude.json`:

```json
{
  "mcpServers": {
    "dbecho": {
      "command": "dbecho"
    }
  }
}
```

When no `--config` is passed, dbecho searches for config in:
1. `./dbecho.toml` (current directory)
2. `~/.config/dbecho/config.toml`
3. `~/.dbecho.toml`

**Other MCP clients** (Cursor, Windsurf, etc.): use the same command/args in your client's MCP server configuration.

### 4. Ask questions

```
Show me a summary of all my databases
How many users signed up each month this year?
Compare order counts between staging and production
Find data quality issues in the events table
What's the relationship between users, orders, and products?
Which columns have the most nulls?
Show me the trend of daily revenue for the last 90 days
```

The agent picks the right tools automatically. You don't need to know the tool names.

## Safety

dbecho is designed to be safe to point at any database, including production:

- **Read-only connections.** Every connection sets `default_transaction_read_only=on` at the PostgreSQL level. Even if someone crafts malicious SQL, the database rejects writes.
- **Query whitelist.** Only `SELECT`, `WITH`, `EXPLAIN`, and `SHOW` statements are allowed — and the validator independently rejects data-modifying CTEs (`WITH x AS (DELETE ...) SELECT ...`), `SELECT INTO`, and `EXPLAIN ANALYZE` over write statements, so it doesn't rely on the read-only connection alone.
- **Blocked functions.** Filesystem, large-object, `dblink`, and `set_config` functions are rejected even though the transaction is read-only — they are exfiltration/escape vectors.
- **SQL injection prevention.** All table/column identifiers use `psycopg.sql.Identifier()` parameterization. User input is validated against `^[a-zA-Z_][a-zA-Z0-9_]*\Z` (`\Z`, not `$`, so a trailing newline can't sneak past). The one identifier-shaped value placed outside `Identifier()` is the per-database `schema` config option — it is embedded in the connection's `search_path` — and it is validated against the same shape (lowercase-only) at config load, before any connection exists.
- **Query timeout.** Default 30 seconds, enforced as one shared budget across multi-query tools via `statement_timeout`, with session-level timeout backstops on every connection.
- **Row limit.** Default 500 rows per query. Prevents the agent from pulling entire tables into context. Full-table profiling (`analyze`/`anomalies`) additionally refuses tables above `max_profile_rows` — exact stats over 50M rows cannot be made cheap, so that one is a refusal, and the message names the setting that raises it.
- **Column cap.** `analyze`/`anomalies` probe at most 80 columns (each costs its own queries). A wider table is profiled partially rather than refused, and the result always states how many columns were skipped and names them — a partial profile that looks complete would be worse than an error.
- **Sensitive-column redaction.** Values of columns that look like secrets (`password`, `token`, `api_key`, `secret`, ...) are replaced with `<redacted>` in `query`/`sample`/`analyze` output (default on; `redact_sensitive = false` to disable). This is name-based harm reduction, **not** a hermetic control — `query` is an open read channel by design.
- **Sanitized errors.** Connection failures are reported to the agent as coarse categories (`authentication failed`, `connection refused`, ...); full details go to the server log only, so hostnames/usernames never leak into the conversation.
- **Local only.** No network calls, no telemetry, no cloud. Data stays on your machine.

For production databases, the strongest setup is still a **least-privilege role**: a PostgreSQL user with `SELECT` only on the tables/views you want exposed. dbecho's layers protect against accidents and prompt-injected agents; the database's own grants are the final word.

## Architecture

```
src/dbecho/
  config.py   TOML config loading, env var expansion, validation
  db.py       DatabaseManager: connections, SQL validation, schema, queries, stats, trends, anomalies
  server.py   FastMCP server: 14 tools, 3 resources, 3 prompts
```

~2700 lines of Python total. No framework beyond `mcp` and `psycopg`.

## Development

```bash
git clone https://github.com/ginkida/dbecho.git
cd dbecho
pip install -e ".[dev]"
pytest -v
```

Tests are fully mocked, no PostgreSQL instance needed. CI runs on Python 3.10-3.13.

```bash
coverage run --branch -m pytest -q
coverage report --show-missing --include='src/*'
```

Because the suite never touches a server, catalog-level SQL (partition trees,
`pg_stat_user_tables` joins, index introspection) should be checked by hand
against a throwaway instance before it lands — e.g.
`docker run --rm -d -e POSTGRES_HOST_AUTH_METHOD=trust -p 55434:5432 postgres:16`,
then point a scratch `dbecho.toml` at it. Mocks pin the plumbing; only a real
server tells you the query is right.

## License

MIT
