"""STAGE 3 — the agent, written out in full.

The whole graph is five nodes:

    START -> hydrate -> agent <-> tools
                          |
                          +----> ui_synth -> END

``hydrate``
    Once per LangGraph thread, pulls this manager's MCP reports into a
    private in-memory DuckDB. Later turns on the same thread skip the load.
``agent``
    Binds the MCP tools plus the session DuckDB text-to-SQL tool, and answers.
    Loops back through ``tools`` for as long as it keeps calling them.
``tools``
    Executes those tool calls. MCP tools are built per request, because which
    tools you get depends on *who is asking* — identity is part of the
    connection. The warehouse tool reads the catalog ``hydrate`` built.
``ui_synth``
    STAGE 4a. Runs once, after the prose is finished, and decides what widget
    should accompany it. Lives in :mod:`src.agent.ui_synth`.

There is no agent framework or factory in the way on purpose: everything the
agent does is visible in this file.
"""

import logging
import os
from datetime import UTC, datetime
from typing import Any, Dict

from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from src.agent.prompts import build_system_prompt
from src.agent.state import AgentState
from src.agent.ui_synth import make_ui_synth_node
from src.config import AGENT_MODEL, aload_chat_model
from src.mcp_client import load_mcp_tools
from src.warehouse.hydrate import hydrate_session
from src.warehouse.store import session_id_from_config
from src.warehouse.tool import query_local_warehouse

logger = logging.getLogger(__name__)


async def _tools_for(email: str) -> list:
    """MCP tools for this manager, plus the local warehouse tool."""
    return [*(await load_mcp_tools(email)), query_local_warehouse]


def _user_email(config: RunnableConfig | None) -> str:
    """The manager this turn belongs to.

    In production the A2A gateway puts this in the config after decoding the
    Joule principal-propagation JWT. The environment fallback exists so the
    agent is runnable locally without a token.
    """
    configurable = (config or {}).get("configurable") or {}
    return (
        configurable.get("user_email")
        or os.getenv("DEV_FALLBACK_USER_EMAIL")
        or os.getenv("MCP_USER_EMAIL", "")
    )


# ── session hydrate ──────────────────────────────────────────────────────────


async def hydrate_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Load this thread's DuckDB catalog on first turn; reuse it after that."""
    session_id = session_id_from_config(config)
    summary = await hydrate_session(session_id, _user_email(config))
    return {"warehouse": summary}


# ── agent node ───────────────────────────────────────────────────────────────


async def agent_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Answer the user, calling MCP and warehouse tools as needed."""
    email = _user_email(config)
    tools = await _tools_for(email)

    llm = (await aload_chat_model(AGENT_MODEL)).bind_tools(tools, parallel_tool_calls=False)

    # The tools bound this turn are the same list the prompt describes, so the
    # model can never be told about a tool it does not have.
    system = build_system_prompt(
        today=datetime.now(UTC).strftime("%Y-%m-%d"),
        user_email=email,
        tools=tools,
    )
    response = await llm.ainvoke([SystemMessage(content=system), *state["messages"]], config)
    return {"messages": [response]}


async def tools_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Execute tool calls as the current user.

    The MCP list is resolved per request rather than at import time because the
    ``x-mcp-user-email`` header is baked into the connection: two managers get
    two different clients, each scoped to their own team by the server. The
    warehouse tool is appended after that list and reads this thread's catalog.
    """
    tools = await _tools_for(_user_email(config))
    # handle_tool_errors=True: the MCP server rejects a bad argument (a missing
    # IdUsers, an unparseable date) with a tool error, and the default handler
    # re-raises anything that is not client-side validation — ending the turn.
    # Handing the message back lets the model correct the call instead.
    return await ToolNode(tools, handle_tool_errors=True).ainvoke(state, config)


def route_after_agent(state: AgentState) -> str:
    """Loop through tools while the model is still working; otherwise synthesise UI."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "ui_synth"


# ── graph ────────────────────────────────────────────────────────────────────


def build_graph(checkpointer=None):
    """Compile the graph.

    ``checkpointer`` is left unset for the module-level ``graph`` below: the
    LangGraph server (``langgraph dev``, or the API container) supplies its own
    and rejects a graph that brought one. The in-process runner used by the
    deployed gateway has no server to do that, so it passes an ``InMemorySaver``
    — without one, every turn would start a fresh conversation.
    """
    builder = StateGraph(AgentState)

    builder.add_node("hydrate", hydrate_node)
    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("ui_synth", make_ui_synth_node())

    builder.add_edge(START, "hydrate")
    builder.add_edge("hydrate", "agent")
    builder.add_conditional_edges("agent", route_after_agent, ["tools", "ui_synth"])
    builder.add_edge("tools", "agent")
    builder.add_edge("ui_synth", END)

    return builder.compile(checkpointer=checkpointer)


graph = build_graph()
