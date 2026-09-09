"""Load this conversation's DuckDB catalog from the live MCP reports.

Runs once per LangGraph thread. The rows stay in memory for that session only;
nothing is written to a file.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.mcp_client import load_mcp_tools, table_name_for_tool
from src.warehouse.store import has_session, load_session, session_summary

logger = logging.getLogger(__name__)


def _required_args(tool: Any) -> list[str]:
    """Argument names the tool cannot be called without."""
    schema = getattr(tool, "args_schema", None)
    if isinstance(schema, dict):
        return list(schema.get("required") or [])
    fields = getattr(schema, "model_fields", None)
    if isinstance(fields, dict):
        return [name for name, f in fields.items() if getattr(f, "is_required", lambda: False)()]
    return []


def _tables_for(tools: list) -> dict:
    """Map each no-argument report tool to a table name.

    Derived from the tool names rather than hardcoded, so an MCP server this
    repo has never seen still produces a usable catalog. Tools that require
    arguments are skipped: hydrate calls them with none, and a report that needs
    parameters is a live lookup, not a snapshot.
    """
    mapping: dict = {}
    taken: set = set()
    for tool in tools:
        required = _required_args(tool)
        if required:
            logger.info(
                "warehouse hydrate: skipping %s (needs %s)", tool.name, ", ".join(required)
            )
            continue
        table = table_name_for_tool(tool.name)
        if table in taken:
            suffix = 2
            while f"{table}_{suffix}" in taken:
                suffix += 1
            table = f"{table}_{suffix}"
        taken.add(table)
        mapping[table] = tool
    return mapping


def coerce_rows(payload: Any) -> list[dict[str, Any]]:
    """Turn whatever an MCP tool returned into a list of row dicts."""
    if payload is None or payload == "":
        return []
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("warehouse hydrate: tool returned non-JSON text")
            return []
    if isinstance(payload, list):
        if payload and isinstance(payload[0], dict):
            first = payload[0]
            if "text" in first and set(first) <= {"type", "text", "data"}:
                return coerce_rows(first.get("text"))
            return [row for row in payload if isinstance(row, dict)]
        return []
    if isinstance(payload, dict):
        for key in ("data", "rows", "result", "items", "value", "records"):
            if isinstance(payload.get(key), list):
                return coerce_rows(payload[key])
        if all(not isinstance(v, (list, dict)) for v in payload.values()):
            return [payload]
    return []


async def _tool_rows(tool: Any) -> list[dict[str, Any]]:
    raw = await tool.ainvoke({})
    return coerce_rows(raw)


async def hydrate_session(session_id: str, email: str) -> dict[str, Any]:
    """Ensure *session_id* has a catalog. No-op if it already does."""
    if has_session(session_id):
        summary = session_summary(session_id)
        summary["cached"] = True
        return summary

    tools = await load_mcp_tools(email)
    tables: dict[str, list[dict[str, Any]]] = {}
    for table, tool in _tables_for(tools).items():
        try:
            tables[table] = await _tool_rows(tool)
        except Exception:
            logger.exception("warehouse hydrate: %s failed", tool.name)
            tables[table] = []

    load_session(session_id, tables)
    summary = session_summary(session_id)
    summary["cached"] = False
    logger.info(
        "warehouse session %s loaded: %s",
        session_id,
        summary.get("tables"),
    )
    return summary
