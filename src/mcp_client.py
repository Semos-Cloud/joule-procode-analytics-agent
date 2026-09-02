"""STAGE 2 — data access.

The agent has no driver and no SQL against the live business system. Everything
it can reach *there* comes through an MCP server, which owns the connection and
enforces that a manager only ever sees their own team. (The DuckDB mart in ``src/warehouse`` is a per-session snapshot of these
same reports, loaded when the conversation starts; it is not a second
path into the system of record.)

Two things make that boundary real rather than decorative:

* **Identity travels with the request.** The manager's email is sent as the
  ``x-mcp-user-email`` header, and the server scopes every query to that
  person's team. The agent cannot ask for someone else's data because it never
  writes the query.
* **The agent sees a subset of the scope.** The ``analytics`` scope exposes nine
  tools; :data:`ALLOWED_TOOLS` narrows that to the two this agent is allowed to
  call. Extra reach the agent doesn't need is extra surface to get wrong.

Run ``python scripts/probe_mcp.py`` to list what a scope really exposes.
"""

import logging
import os
from typing import Dict, List, Tuple

from dotenv import load_dotenv
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(interpolate=False)

logger = logging.getLogger(__name__)

MCP_BASE_URL: str = os.getenv("MCP_BASE_URL", "https://jobptsapi.semoscloud.com")
MCP_SCOPE: str = os.getenv("MCP_SCOPE", "analytics")
MCP_USER_EMAIL: str = os.getenv("MCP_USER_EMAIL", "")

# The only tools this agent may call, out of everything the scope exposes.
#
# These names are matched exactly against the server. If the server renames one,
# the tool would silently disappear from the agent and it would start guessing
# answers instead of failing — so :func:`load_mcp_tools` treats a missing name as
# a startup error. Verify with `python scripts/probe_mcp.py` after any server
# change.
ALLOWED_TOOLS: frozenset = frozenset(
    {
        "get_my_teams_reach_data",
        "get_award_reasons_data",
    }
)

# Cache tools per (email, url): each manager gets a client bound to their own
# identity header, and rebuilding it on every turn would re-handshake the SSE
# connection.
_cache: Dict[Tuple[str, str], List[BaseTool]] = {}

# MCP clients must outlive the call that created them — tool invocations
# round-trip over the SSE connection they hold open.
_clients: List[MultiServerMCPClient] = []


def scope_url(base_url: str | None = None, scope: str | None = None) -> str:
    """The SSE endpoint for one MCP scope."""
    return f"{(base_url or MCP_BASE_URL).rstrip('/')}/mcp/sse?scope={scope or MCP_SCOPE}"


async def load_mcp_tools(email: str = "") -> List[BaseTool]:
    """Connect to the MCP server as *email* and return the allowed tools.

    Raises:
        RuntimeError: if the server exposes none of :data:`ALLOWED_TOOLS`, or is
            missing one of them. Failing at startup is deliberate: an agent that
            is quietly missing its data tool is worse than one that won't start.
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
                "transport": "sse",
                "headers": {"x-mcp-user-email": effective_email},
            }
        }
    )
    _clients.append(client)

    available = await client.get_tools()
    by_name = {tool.name: tool for tool in available}

    missing = ALLOWED_TOOLS - by_name.keys()
    if missing:
        raise RuntimeError(
            f"MCP scope {MCP_SCOPE!r} at {url} does not expose {sorted(missing)}. "
            f"It offers {sorted(by_name)}. Update ALLOWED_TOOLS to match the "
            f"server (run scripts/probe_mcp.py to see the current names)."
        )

    tools = [by_name[name] for name in sorted(ALLOWED_TOOLS)]
    logger.info(
        "MCP scope %r: %d tool(s) offered, %d allowed for this agent (%s)",
        MCP_SCOPE,
        len(available),
        len(tools),
        ", ".join(t.name for t in tools),
    )

    _cache[key] = tools
    return tools
