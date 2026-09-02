"""Load this conversation's DuckDB catalog from the live MCP reports.

Runs once per LangGraph thread. The rows stay in memory for that session only;
nothing is written to a file.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from src.mcp_client import load_mcp_tools
from src.warehouse.store import has_session, load_session, session_summary

logger = logging.getLogger(__name__)

# The two reports become the two tables the text-to-SQL tool can see.
_TABLE_FOR_TOOL = {
    "get_my_teams_reach_data": "team_reach",
    "get_award_reasons_data": "award_reasons",
}


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
    by_name = {tool.name: tool for tool in tools}
    tables: dict[str, list[dict[str, Any]]] = {}
    for tool_name, table in _TABLE_FOR_TOOL.items():
        tool = by_name.get(tool_name)
        if tool is None:
            tables[table] = []
            continue
        try:
            tables[table] = await _tool_rows(tool)
        except Exception:
            logger.exception("warehouse hydrate: %s failed", tool_name)
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
