"""STAGE 2 — data access.

The agent has no driver and no SQL against the live business system. Everything
it can reach *there* comes through an MCP server, which owns the connection and
enforces that a user only ever sees their own data. (The DuckDB mart in
``src/warehouse`` is a per-session snapshot of these same reports, loaded when
the conversation starts; it is not a second path into the system of record.)

Two things make that boundary real rather than decorative:

* **Identity travels with the request.** The user's email is sent as a header
  (``MCP_USER_HEADER``, by default ``x-mcp-user-email``), and the server scopes
  every query to that person. The agent cannot ask for someone else's data
  because it never writes the query.
* **The agent can be given a subset of the scope.** ``MCP_ALLOWED_TOOLS``
  narrows what the agent may call. Reach it doesn't need is surface it can get
  wrong.

Everything about the connection is environment-driven, so pointing this at your
own MCP server is configuration rather than a code change:

    MCP_BASE_URL        host of the server
    MCP_SCOPE           scope name, substituted into the URL template
    MCP_URL             full URL; overrides the template entirely
    MCP_URL_TEMPLATE    default "{base}/mcp/sse?scope={scope}"
    MCP_TRANSPORT       "sse" (default) or "streamable_http"
    MCP_USER_HEADER     identity header name
    MCP_ALLOWED_TOOLS   comma-separated; blank means "every tool in the scope"

Run ``python scripts/probe_mcp.py`` to list what a scope really exposes.
"""

import logging
import os
import re
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(interpolate=False)

logger = logging.getLogger(__name__)

MCP_BASE_URL: str = os.getenv("MCP_BASE_URL") or "https://jobptsapi.semoscloud.com"
MCP_SCOPE: str = os.getenv("MCP_SCOPE") or "analytics"
MCP_USER_EMAIL: str = os.getenv("MCP_USER_EMAIL", "")
MCP_URL: str = os.getenv("MCP_URL", "")
MCP_URL_TEMPLATE: str = os.getenv("MCP_URL_TEMPLATE") or "{base}/mcp/sse?scope={scope}"
MCP_TRANSPORT: str = os.getenv("MCP_TRANSPORT") or "sse"
MCP_USER_HEADER: str = os.getenv("MCP_USER_HEADER") or "x-mcp-user-email"


def _parse_allowed(raw: str) -> frozenset:
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


# The only tools this agent may call, out of everything the scope exposes.
#
# Blank (the default) means "whatever this scope offers" — which is what lets an
# attendee point at their own server without editing code.
#
# When it IS set, the names are matched exactly and a missing one is a startup
# error. That guard is deliberate: if a server renames a tool, the tool would
# silently disappear from the agent and it would start guessing answers instead
# of failing. Verify with `python scripts/probe_mcp.py` after any server change.
ALLOWED_TOOLS: frozenset = _parse_allowed(os.getenv("MCP_ALLOWED_TOOLS", ""))

# Cache tools per (email, url): each user gets a client bound to their own
# identity header, and rebuilding it on every turn would re-handshake the
# connection.
_cache: Dict[Tuple[str, str], List[BaseTool]] = {}

# MCP clients must outlive the call that created them — tool invocations
# round-trip over the connection they hold open.
_clients: List[MultiServerMCPClient] = []


def scope_url(base_url: str | None = None, scope: str | None = None) -> str:
    """The endpoint for one MCP scope."""
    if MCP_URL:
        return MCP_URL
    return MCP_URL_TEMPLATE.format(
        base=(base_url or MCP_BASE_URL).rstrip("/"),
        scope=scope or MCP_SCOPE,
    )


def table_name_for_tool(name: str) -> str:
    """A DuckDB table name for the rows a tool returns.

    Derived rather than mapped, so a server this repo has never seen still gets
    sensible table names in the text-to-SQL schema:

        get_my_teams_reach_data  -> my_teams_reach
        get_award_reasons_data   -> award_reasons
        listOpenOrders           -> open_orders
    """
    base = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    base = re.sub(r"^(get|fetch|list|read|query)_", "", base)
    base = re.sub(r"_(data|report|rows|result|results)$", "", base)
    base = re.sub(r"[^a-z0-9_]", "_", base).strip("_")
    if not base:
        base = "report"
    if base[0].isdigit():
        base = f"t_{base}"
    return base


async def load_mcp_tools(email: str = "") -> List[BaseTool]:
    """Connect to the MCP server as *email* and return the tools it may call.

    Raises:
        RuntimeError: if the scope exposes no tools at all, or if
            :data:`ALLOWED_TOOLS` names one the server does not have. Failing
            here is deliberate: an agent quietly missing its data tool is worse
            than one that will not start.
    """
    effective_email = email or MCP_USER_EMAIL
    url = scope_url()
    key = (effective_email, url)

    if key in _cache:
        return _cache[key]

    client = MultiServerMCPClient(
        {
            MCP_SCOPE: {
                "url": url,
                "transport": MCP_TRANSPORT,
                "headers": {MCP_USER_HEADER: effective_email} if effective_email else {},
            }
        }
    )
    _clients.append(client)

    available = await client.get_tools()
    by_name = {tool.name: tool for tool in available}

    if not by_name:
        raise RuntimeError(
            f"MCP scope {MCP_SCOPE!r} at {url} exposed no tools. Check the scope "
            f"name, the transport ({MCP_TRANSPORT!r}) and the identity header "
            f"({MCP_USER_HEADER!r}). Run scripts/probe_mcp.py to see what it offers."
        )

    if ALLOWED_TOOLS:
        missing = ALLOWED_TOOLS - by_name.keys()
        if missing:
            raise RuntimeError(
                f"MCP scope {MCP_SCOPE!r} at {url} does not expose {sorted(missing)}. "
                f"It offers {sorted(by_name)}. Update MCP_ALLOWED_TOOLS to match the "
                f"server, or leave it blank to allow everything the scope offers "
                f"(run scripts/probe_mcp.py to see the current names)."
            )
        tools = [by_name[name] for name in sorted(ALLOWED_TOOLS)]
    else:
        tools = [by_name[name] for name in sorted(by_name)]

    logger.info(
        "MCP scope %r: %d tool(s) offered, %d allowed for this agent (%s)",
        MCP_SCOPE,
        len(available),
        len(tools),
        ", ".join(t.name for t in tools),
    )

    _cache[key] = tools
    return tools
