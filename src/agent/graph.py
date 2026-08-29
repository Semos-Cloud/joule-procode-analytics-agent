"""STAGE 3 — the agent, written out in full.

The whole graph is four nodes and two edges' worth of routing:

    START -> agent <-> tools
               |
               +----> ui_synth -> END

``agent``
    Binds the MCP tools and answers. Loops back through ``tools`` for as long as
    it keeps calling them.
``tools``
    Executes MCP tool calls. Built per request, because which tools you get
    depends on *who is asking* — identity is part of the connection.
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
from src.config import AGENT_MODEL, load_chat_model
from src.mcp_client import load_mcp_tools

logger = logging.getLogger(__name__)


def _user_email(config: RunnableConfig | None) -> str:
    """The manager this turn belongs to.

    In production the A2A gateway puts this in the config after decoding the
    Joule principal-propagation JWT. The environment fallback exists so the
    agent is runnable locally without a token.
    """
    configurable = (config or {}).get("configurable") or {}
    return configurable.get("user_email") or os.getenv("DEV_FALLBACK_USER_EMAIL", "")


# ── agent node ───────────────────────────────────────────────────────────────


async def agent_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Answer the user, calling MCP tools as needed."""
    email = _user_email(config)
    tools = await load_mcp_tools(email)

    llm = load_chat_model(AGENT_MODEL).bind_tools(tools, parallel_tool_calls=False)

    system = build_system_prompt(
        today=datetime.now(UTC).strftime("%Y-%m-%d"),
        user_email=email,
    )
    response = await llm.ainvoke([SystemMessage(content=system), *state["messages"]], config)
    return {"messages": [response]}


async def tools_node(state: AgentState, config: RunnableConfig) -> Dict[str, Any]:
    """Execute MCP tool calls as the current user.

    The tool list is resolved per request rather than at import time because the
    ``x-mcp-user-email`` header is baked into the connection: two managers get
    two different clients, each scoped to their own team by the server.
    """
    tools = await load_mcp_tools(_user_email(config))
    return await ToolNode(tools).ainvoke(state, config)


def route_after_agent(state: AgentState) -> str:
    """Loop through tools while the model is still working; otherwise synthesise UI."""
    last = state["messages"][-1]
    if getattr(last, "tool_calls", None):
        return "tools"
    return "ui_synth"


# ── graph ────────────────────────────────────────────────────────────────────


def build_graph():
    builder = StateGraph(AgentState)

    builder.add_node("agent", agent_node)
    builder.add_node("tools", tools_node)
    builder.add_node("ui_synth", make_ui_synth_node())

    builder.add_edge(START, "agent")
    builder.add_conditional_edges("agent", route_after_agent, ["tools", "ui_synth"])
    builder.add_edge("tools", "agent")
    builder.add_edge("ui_synth", END)

    return builder.compile()


graph = build_graph()
