"""Print the tools a JobPts MCP scope actually exposes.

Stage 2 of the workshop depends on knowing the real tool names: the agent's
allowlist is matched against them, and the system prompt documents them. A name
that drifts on the server side silently removes the tool from the agent, so this
probe is the first thing to run against a new endpoint.

    python scripts/probe_mcp.py                    # scope=analytics
    python scripts/probe_mcp.py --scope shared     # any other scope
    python scripts/probe_mcp.py --schemas          # include input schemas

Reads MCP_BASE_URL and MCP_USER_EMAIL from the environment (or .env).
"""

import argparse
import asyncio
import json
import os
import sys

from dotenv import load_dotenv
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv(interpolate=False)

DEFAULT_BASE_URL = os.getenv("MCP_BASE_URL", "https://jobptsapi.semoscloud.com")
DEFAULT_EMAIL = os.getenv("MCP_USER_EMAIL", "")


async def probe(base_url: str, scope: str, email: str, show_schemas: bool) -> int:
    url = f"{base_url.rstrip('/')}/mcp/sse?scope={scope}"
    print(f"Connecting to {url}")
    print(f"  x-mcp-user-email: {email or '(none)'}\n")

    client = MultiServerMCPClient(
        {
            scope: {
                "url": url,
                "transport": "sse",
                "headers": {"x-mcp-user-email": email} if email else {},
            }
        }
    )
    tools = await client.get_tools()

    if not tools:
        print("No tools returned. Check the scope name and the user email.")
        return 1

    print(f"{len(tools)} tool(s) in scope {scope!r}:\n")
    for tool in tools:
        print(f"  {tool.name}")
        description = (tool.description or "").strip().splitlines()
        if description:
            print(f"      {description[0][:140]}")
        if show_schemas:
            schema = getattr(tool, "args_schema", None)
            if isinstance(schema, dict):
                print(f"      args: {json.dumps(schema.get('properties', {}), indent=8)[:1200]}")
        print()

    print("Copy these names verbatim into ALLOWED_TOOLS in src/mcp_client.py.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--scope", default="analytics")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--schemas", action="store_true", help="print input schemas")
    args = parser.parse_args()
    return asyncio.run(probe(args.base_url, args.scope, args.email, args.schemas))


if __name__ == "__main__":
    sys.exit(main())
