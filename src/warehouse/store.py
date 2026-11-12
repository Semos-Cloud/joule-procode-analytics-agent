"""Per-session in-memory DuckDB. Nothing is written to disk.

Each LangGraph thread gets its own catalog, created when that conversation
starts and dropped when the process forgets it. Two managers never share a
connection.
"""

from __future__ import annotations

import json
import re
import threading
from typing import Any, Mapping

import duckdb
from langchain_core.runnables import RunnableConfig

# What the text-to-SQL step replies when the snapshot cannot answer at all.
CANNOT_ANSWER = "CANNOT_ANSWER"

_TABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_lock = threading.Lock()
_sessions: dict[str, duckdb.DuckDBPyConnection] = {}


def session_id_from_config(config: RunnableConfig | None) -> str:
    """LangGraph thread id, falling back to the manager, then a local default."""
    configurable = (config or {}).get("configurable") or {}
    return str(
        configurable.get("thread_id")
        or configurable.get("user_email")
        or "local"
    )


def has_session(session_id: str) -> bool:
    with _lock:
        return session_id in _sessions


def drop_session(session_id: str) -> None:
    with _lock:
        con = _sessions.pop(session_id, None)
    if con is not None:
        con.close()


def reset() -> None:
    """Close every session catalog. Tests use this; the agent does not."""
    with _lock:
        ids = list(_sessions)
    for sid in ids:
        drop_session(sid)


def _ident(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(name)).strip("_") or "col"
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    return f'"{cleaned}"'


def _duck_type(values: list[Any]) -> str:
    present = [v for v in values if v is not None]
    if not present:
        return "VARCHAR"
    if all(isinstance(v, bool) for v in present):
        return "BOOLEAN"
    if all(isinstance(v, int) and not isinstance(v, bool) for v in present):
        return "BIGINT"
    if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in present):
        return "DOUBLE"
    return "VARCHAR"


def _create_table(con: duckdb.DuckDBPyConnection, table: str, rows: list[dict[str, Any]]) -> None:
    if not _TABLE_NAME_RE.match(table):
        raise ValueError(f"invalid table name: {table!r}")
    con.execute(f"DROP TABLE IF EXISTS {table}")
    if not rows:
        con.execute(f"CREATE TABLE {table} (_empty BOOLEAN)")
        return
    keys: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(str(key))
    col_defs = ", ".join(
        f"{_ident(key)} {_duck_type([row.get(key) for row in rows])}" for key in keys
    )
    con.execute(f"CREATE TABLE {table} ({col_defs})")
    placeholders = ", ".join("?" for _ in keys)
    col_sql = ", ".join(_ident(key) for key in keys)
    con.executemany(
        f"INSERT INTO {table} ({col_sql}) VALUES ({placeholders})",
        [tuple(row.get(key) for key in keys) for row in rows],
    )


def load_session(session_id: str, tables: Mapping[str, list[dict[str, Any]]]) -> None:
    """Replace this session's catalog with *tables* (name → list of row dicts)."""
    con = duckdb.connect(":memory:")
    try:
        for name, rows in tables.items():
            _create_table(con, name, list(rows))
    except Exception:
        con.close()
        raise
    with _lock:
        previous = _sessions.pop(session_id, None)
        _sessions[session_id] = con
    if previous is not None:
        previous.close()


class UnrelatedJoinError(ValueError):
    """The query pairs every row of one table with every row of another."""


# Operators a single-row result passes through unchanged on its way up the plan.
_ROW_PRESERVING_OPS = {"PROJECTION", "FILTER"}


def _yields_one_row(node: Mapping[str, Any]) -> bool:
    """True when *node* is an aggregate with no GROUP BY, under projections.

    `x / (SELECT sum(x) FROM t)` and `CROSS JOIN (SELECT avg(x) ...)` are the
    legitimate cross products: one side is a single row, so every row on the
    other side is paired with that one total.
    """
    while node.get("name") in _ROW_PRESERVING_OPS and len(node.get("children") or []) == 1:
        node = node["children"][0]
    return node.get("name") == "UNGROUPED_AGGREGATE"


def _scanned_tables(node: Mapping[str, Any]) -> list[str]:
    found: list[str] = []
    table = (node.get("extra_info") or {}).get("Table")
    if isinstance(table, str):
        found.append(table.rsplit(".", 1)[-1])
    for child in node.get("children") or []:
        found.extend(t for t in _scanned_tables(child) if t not in found)
    return found


def _unrelated_join(node: Mapping[str, Any]) -> list[str] | None:
    """Tables of the first many-by-many CROSS_PRODUCT in the plan, if any."""
    children = node.get("children") or []
    if node.get("name") == "CROSS_PRODUCT" and not any(_yields_one_row(c) for c in children):
        return _scanned_tables(node)
    for child in children:
        hit = _unrelated_join(child)
        if hit is not None:
            return hit
    return None


def _reject_unrelated_join(con: duckdb.DuckDBPyConnection, sql: str) -> None:
    """Refuse a join whose condition does not relate the two sides.

    `a JOIN b ON a.name IN ('x', 'y')` filters one side only. DuckDB plans it
    as a cross product, and every row of *b* inherits the same numbers from *a*:
    a result that looks like an answer and means nothing. This happened in
    production with a team-level award_reasons table "joined" to people.
    """
    plan = json.loads(con.execute(f"EXPLAIN (FORMAT JSON) {sql}").fetchall()[0][1])
    for root in plan:
        tables = _unrelated_join(root)
        if tables is not None:
            named = " and ".join(tables) or "two inputs"
            raise UnrelatedJoinError(
                f"The query pairs every row of {named} with every row of the other: "
                "the join condition does not relate the two tables, so the numbers "
                "would be meaningless. Join only on a column both tables share. If "
                "they share none, this snapshot cannot answer the question."
            )


def fetch_rows(sql: str, session_id: str) -> list[dict[str, Any]]:
    """Run *sql* against this session's catalog."""
    with _lock:
        con = _sessions.get(session_id)
        if con is None:
            raise RuntimeError(
                f"warehouse is not loaded for session {session_id!r}. "
                "It is created once when the conversation starts."
            )
        _reject_unrelated_join(con, sql)
        relation = con.sql(sql)
        columns = list(relation.columns)
        values = relation.fetchall()
    return [dict(zip(columns, row, strict=True)) for row in values]


def schema_text(session_id: str) -> str:
    """Column list for the text-to-SQL prompt, from DESCRIBE on this session."""
    with _lock:
        con = _sessions.get(session_id)
        if con is None:
            return "No tables are loaded for this session yet."
        tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
        columns: dict[str, set[str]] = {}
        parts = [
            "This conversation's DuckDB snapshot. It was loaded once when the "
            "session started, from the manager's MCP reports. Quote mixed-case "
            "column names. Never SELECT identifier columns (Id*, *_id).",
            "",
        ]
        for table in tables:
            cols = con.execute(f"DESCRIBE {table}").fetchall()
            columns[table] = {col[0] for col in cols if col[0] != "_empty"}
            parts.append(f"  {table}")
            for col in cols:
                if col[0] == "_empty":
                    parts.append("    (no rows in this session)")
                    continue
                parts.append(f"    \"{col[0]}\"  {col[1]}")
            parts.append("")
        unrelated = [
            f"  {a} and {b}"
            for i, a in enumerate(tables)
            for b in tables[i + 1 :]
            if columns.get(a) and columns.get(b) and not columns[a] & columns[b]
        ]
        if unrelated:
            parts.append("These pairs share no column and cannot be joined:")
            parts.extend(unrelated)
            parts.append("")
        parts.append(
            "Rules\n"
            "  - One statement. SELECT / WITH / FROM only.\n"
            "  - Prefer LIMIT 20 unless the question needs more (hard cap is 50).\n"
            "  - Ranking questions (top, most, least, best, worst, 'moved most')\n"
            "    return the ORDERED SET, not LIMIT 1. The caller needs the\n"
            "    runners-up to answer properly and to draw a chart.\n"
            "  - Return names and measures, not internal ids.\n"
            "  - Join two tables only on a column they both hold. A condition\n"
            "    that filters one side (ON a.name IN (...)) is not a join.\n"
            "  - A table without a person column says nothing about one person.\n"
            f"  - If no table holds what the question needs, reply with exactly\n"
            f"    {CANNOT_ANSWER}: <one sentence on what is missing>\n"
            "    instead of SQL. Never invent a relationship to produce rows."
        )
        return "\n".join(parts)


def session_summary(session_id: str) -> dict[str, Any]:
    """Row counts per table, for state / logs. Not the data itself."""
    with _lock:
        con = _sessions.get(session_id)
        if con is None:
            return {"loaded": False, "tables": {}}
        tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
        counts = {}
        for table in tables:
            counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    return {"loaded": True, "tables": counts}
