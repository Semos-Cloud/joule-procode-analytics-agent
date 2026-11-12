"""LangChain tool: English question → DuckDB SQL → this session's rows.

The agent never writes SQL. It asks a question; this tool compiles it against
the catalog loaded when the conversation started.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from src.config import AGENT_MODEL, aload_chat_model
from src.warehouse.hydrate import hydrate_session
from src.warehouse.sql_guard import (
    apply_limit,
    ensure_readonly,
    extract_sql,
    looks_like_sql,
    strip_internal_ids,
)
from src.warehouse.store import CANNOT_ANSWER, fetch_rows, schema_text, session_id_from_config

logger = logging.getLogger(__name__)


class SnapshotCannotAnswer(ValueError):
    """The text-to-SQL step said no table holds what the question needs."""


def _sql_system(session_id: str) -> str:
    return (
        "You translate one analytics question into a single DuckDB query.\n\n"
        f"{schema_text(session_id)}\n\n"
        "Return only the SQL. No markdown fence, no commentary."
    )


def _message_text(message: Any) -> str:
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


async def _generate_sql(question: str, session_id: str, *, prior_sql: str = "", error: str = "") -> str:
    llm = await aload_chat_model(AGENT_MODEL, stream_usage=False)
    human = question
    if prior_sql and error:
        human = (
            f"Question: {question}\n\n"
            f"This SQL failed:\n{prior_sql}\n\n"
            f"Error: {error}\n\nWrite a corrected query."
        )
    reply = await llm.ainvoke(
        [SystemMessage(content=_sql_system(session_id)), HumanMessage(content=human)]
    )
    text = _message_text(reply).strip().strip("`").strip()
    # Checked before extract_sql, which would otherwise cut the reason at its
    # first "from" and hand DuckDB half a sentence.
    if text.upper().startswith(CANNOT_ANSWER):
        reason = text[len(CANNOT_ANSWER) :].lstrip(" :-").strip() or "the data is not loaded"
        raise SnapshotCannotAnswer(
            f"The session snapshot cannot answer this: {reason}. Use a live report "
            "tool if one holds it, otherwise tell the user the data is not available."
        )
    return extract_sql(text)


async def run_warehouse_question(question: str, session_id: str) -> dict[str, Any]:
    """Compile (if needed), guard, execute against *session_id*."""
    generated = False
    if looks_like_sql(question):
        sql = question
    else:
        sql = await _generate_sql(question, session_id)
        generated = True

    sql = apply_limit(ensure_readonly(sql))
    logger.info("warehouse SQL [%s]: %s", session_id, sql)

    try:
        rows = fetch_rows(sql, session_id)
    except Exception as exc:
        if not generated:
            raise
        logger.info("warehouse SQL failed (%s) — retrying once", exc)
        sql = apply_limit(
            ensure_readonly(await _generate_sql(question, session_id, prior_sql=sql, error=str(exc)))
        )
        logger.info("warehouse SQL (retry) [%s]: %s", session_id, sql)
        rows = fetch_rows(sql, session_id)

    return {
        "sql": sql,
        "row_count": len(rows),
        "rows": strip_internal_ids(rows),
    }


@tool
async def query_local_warehouse(question: str, config: RunnableConfig) -> str:
    """Query this conversation's DuckDB snapshot with a natural-language question.

    The snapshot is this manager's MCP reports, loaded once when the session
    started. Use it for SQL the two live tool calls would make you do by hand:
    windows, multi-column filters, ranking with several measures at once.

    Each table is one report at its own grain. A team-wide report (totals per
    award reason, say) has no person column and cannot say anything about one
    person, and two tables can only be combined on a column they share. For a
    person's own recognitions — messages, skills, who recognised them — use the
    live per-person recognitions report instead.

    Pass the question in English. Do not write SQL — this tool compiles it.
    """
    session_id = session_id_from_config(config)
    email = ((config or {}).get("configurable") or {}).get("user_email") or ""
    try:
        await hydrate_session(session_id, email)
        payload = await run_warehouse_question(question, session_id)
    except Exception as exc:
        logger.warning("warehouse query failed: %s", exc)
        return json.dumps({"error": str(exc), "rows": [], "row_count": 0})
    return json.dumps(payload, default=str)
