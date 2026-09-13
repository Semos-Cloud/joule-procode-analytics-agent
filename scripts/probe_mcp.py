"""Print the tools an MCP scope actually exposes.

Run this first when pointing the agent at a new server. It connects exactly the
way :mod:`src.mcp_client` does — same URL, transport and identity header — so a
scope that probes clean is one the agent can reach.

    python scripts/probe_mcp.py                    # the configured scope
    python scripts/probe_mcp.py --scope shared     # any other scope
    python scripts/probe_mcp.py --schemas          # include input schemas

Reads MCP_BASE_URL, MCP_SCOPE, MCP_URL, MCP_URL_TEMPLATE, MCP_TRANSPORT,
MCP_USER_HEADER and MCP_USER_EMAIL from the environment (or .env).
"""

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(interpolate=False)

# Import the real connection settings rather than restating them: a probe that
# connects differently from the agent can report success the agent cannot repeat.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.mcp_client import (  # noqa: E402
    MCP_TRANSPORT,
    MCP_USER_HEADER,
    scope_url,
    table_name_for_tool,
)

DEFAULT_BASE_URL = os.getenv("MCP_BASE_URL") or "https://jobptsapi.semoscloud.com"
DEFAULT_SCOPE = os.getenv("MCP_SCOPE") or "analytics"
DEFAULT_EMAIL = os.getenv("MCP_USER_EMAIL", "")


async def probe(base_url: str, scope: str, email: str, show_schemas: bool) -> int:
    url = scope_url(base_url, scope)
    print(f"Connecting to {url}")
    print(f"  transport: {MCP_TRANSPORT}")
    print(f"  {MCP_USER_HEADER}: {email or '(none)'}\n")

    client = MultiServerMCPClient(
        {
            scope: {
                "url": url,
                "transport": MCP_TRANSPORT,
                "headers": {MCP_USER_HEADER: email} if email else {},
            }
        }
    )
    tools = await client.get_tools()

    if not tools:
        print("No tools returned. Check the scope name and the user email.")
        return 1

    print(f"{len(tools)} tool(s) in scope {scope!r}:\n")
    for tool in tools:
        print(f"  {tool.name}   (warehouse table: {table_name_for_tool(tool.name)})")
        description = (tool.description or "").strip().splitlines()
        if description:
            print(f"      {description[0][:140]}")
        if show_schemas:
            schema = getattr(tool, "args_schema", None)
            if isinstance(schema, dict):
                print(f"      args: {json.dumps(schema.get('properties', {}), indent=8)[:1200]}")
        print()

    print(
        "Set MCP_ALLOWED_TOOLS to the names you want the agent to have "
        "(comma-separated), or leave it blank to allow all of them."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--scope", default=DEFAULT_SCOPE)
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--schemas", action="store_true", help="print input schemas")
    args = parser.parse_args()
    return asyncio.run(probe(args.base_url, args.scope, args.email, args.schemas))


if __name__ == "__main__":
    sys.exit(main())
