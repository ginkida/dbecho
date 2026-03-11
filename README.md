# dbecho

<p align="center">
  <img src="https://minio.ginkida.dev/minion/github/dbecho.png" alt="dbecho mascot" width="200">
</p>

> Talk to your PostgreSQL databases through AI. No dashboards, no BI tools, just questions and answers.

**dbecho** is an [MCP server](https://modelcontextprotocol.io/) that gives AI agents (Claude Code, Cursor, Windsurf, or any MCP client) direct read-only access to your PostgreSQL databases. Point it at your databases, ask questions in plain language, get instant analytics.

```
You: What are my most popular blog posts and why?
Claude: [runs schema → query → analyze → trend across 29 tables]
       Here's what the data shows...
```

## What can it do?

**11 tools** that cover the full analytics workflow:

| Tool | Purpose |
|------|---------|
| `list_databases` | Show all connected databases |
| `health` | Check connectivity, PostgreSQL version, database size |
| `schema` | Full schema: tables, columns, types, PKs, row counts, sizes |
| `query` | Run read-only SQL (SELECT, WITH, EXPLAIN, SHOW) |
| `analyze` | Profile a table: nulls, cardinality, distributions, top values |
| `compare` | Same query across multiple databases, side by side |
| `summary` | Overview: table counts, total rows, largest tables |
| `trend` | Time-series: counts/averages grouped by day/week/month/year |
| `anomalies` | Data quality: high nulls, outliers, duplicates, future dates |
| `sample` | Preview rows from any table |
| `erd` | Entity-relationship diagram: PKs and foreign keys |

Plus **3 MCP Resources** (schema/summary per database) and **3 MCP Prompts** (guided exploration, cross-database comparison, data quality audit).

## Why dbecho?

**The problem:** You have PostgreSQL databases across projects. Getting answers means context-switching to psql, pgAdmin, or a BI tool, writing SQL, formatting results, then bringing insights back to your conversation.

**The fix:** dbecho stays inside your AI agent's workflow. The agent explores schema, writes SQL, cross-references tables, and builds analysis without you leaving the conversation. One config file, zero context switches.

What makes it different from just giving an agent a connection string:
- **Multi-database.** Connect 1 or 20 databases. Compare across them with one tool call.
- **Safe by default.** Read-only connections, query timeouts, row limits, SQL injection prevention. You can't accidentally `DROP TABLE`.
- **Agent-optimized output.** Schema, stats, and query results are formatted so LLMs parse them efficiently, not as raw psql dumps.
- **Zero infrastructure.** No containers, no web UI, no background processes. A Python package that speaks MCP over stdio.

## Real-world example

Connected to a blog database (29 tables, 34 MB), asked Claude to analyze post performance:

```
You: Analyze my blog posts and tell me what topics I should write about.
```

Claude autonomously ran 15+ queries across `post_posts`, `post_engagements`, `post_reactions`, `tags`, `topics`, `subscribers`, and `comment_comments`. Discovered:

- Posts with brand names in titles average 1800-3000 views (vs 400 without)
- 63% of readers leave in the first quarter of a post
- Investigation-style posts get 80-100% engagement rate
- 87% of 32,754 unique visitors never return (retention problem)
- Only 47 subscribers from all that traffic (0.07% conversion)
- Email bounce rate of 83% among confirmed subscribers

That's the kind of analysis that takes hours manually. dbecho made it a conversation.

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

Requires Python 3.10+.

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
row_limit = 500       # max rows returned per query (default: 500)
query_timeout = 30    # seconds before query is killed (default: 30)
```

Environment variables work with `${VAR}` syntax:

```toml
[databases.production]
url = "${DATABASE_URL}"
description = "Production (read replica)"
```

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
- **Query whitelist.** Only `SELECT`, `WITH`, `EXPLAIN`, and `SHOW` statements are allowed. Checked before execution.
- **SQL injection prevention.** All table/column identifiers use `psycopg.sql.Identifier()` parameterization. User input is validated against `^[a-zA-Z_][a-zA-Z0-9_]*$`.
- **Query timeout.** Default 30 seconds. Prevents runaway queries from locking your database.
- **Row limit.** Default 500 rows per query. Prevents the agent from pulling entire tables into context.
- **Local only.** No network calls, no telemetry, no cloud. Data stays on your machine.

## Architecture

```
src/dbecho/
  config.py   TOML config loading, env var expansion, validation
  db.py       DatabaseManager: connections, schema cache, queries, stats, trends, anomalies
  server.py   FastMCP server: 11 tools, 3 resources, 3 prompts
```

~1000 lines of Python total. No framework beyond `mcp` and `psycopg`.

## Development

```bash
git clone https://github.com/ginkida/dbecho.git
cd dbecho
pip install -e ".[dev]"
pytest -v
```

Tests are fully mocked, no PostgreSQL instance needed. CI runs on Python 3.10-3.13.

## License

MIT
