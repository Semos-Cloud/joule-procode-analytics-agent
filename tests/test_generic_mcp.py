"""Tests for pointing the agent at an MCP server this repo has never seen.

The connection is configuration (URL, transport, identity header, allowlist) and
the prompt's tool descriptions are generated from the server's own metadata.
These cover the parts that make that true, because the failure mode they prevent
is silent: a prompt that describes tools the agent does not have, or a warehouse
that quietly loads nothing.
"""

import pytest

import src.mcp_client as mcp
from src.agent.prompts import build_system_prompt, render_tools_block
from src.mcp_client import table_name_for_tool
from src.warehouse.hydrate import _tables_for


class FakeTool:
    def __init__(self, name, description="", args_schema=None):
        self.name = name
        self.description = description
        self.args_schema = args_schema


class TestTableNaming:
    @pytest.mark.parametrize(
        "tool_name, expected",
        [
            ("get_my_teams_reach_data", "my_teams_reach"),
            ("get_award_reasons_data", "award_reasons"),
            ("listOpenOrders", "open_orders"),
            ("SalesOrders", "sales_orders"),
            ("fetch_invoices_report", "invoices"),
            ("2024_report", "t_2024"),
        ],
    )
    def test_derives_a_sensible_table_name(self, tool_name, expected):
        assert table_name_for_tool(tool_name) == expected

    def test_never_returns_an_empty_identifier(self):
        assert table_name_for_tool("get_") == "report"

    def test_collisions_are_suffixed_not_overwritten(self):
        tables = _tables_for([FakeTool("get_orders"), FakeTool("orders"), FakeTool("listOrders")])
        assert sorted(tables) == ["orders", "orders_2", "orders_3"]


class TestHydrateSkipsParameterisedTools:
    """hydrate calls tools with no arguments, so a tool that requires one is a
    live lookup rather than a snapshot and must not become an empty table."""

    def test_json_schema_required_args(self):
        tables = _tables_for(
            [
                FakeTool("get_orders", args_schema={"properties": {"region": {}}, "required": ["region"]}),
                FakeTool("get_customers", args_schema={"properties": {"since": {}}, "required": []}),
            ]
        )
        assert sorted(tables) == ["customers"]

    def test_tool_without_schema_is_kept(self):
        assert sorted(_tables_for([FakeTool("get_countries")])) == ["countries"]


class TestGeneratedToolsBlock:
    def test_uses_the_servers_own_name_and_description(self):
        block = render_tools_block(
            [FakeTool("get_open_orders", "Every order that is still open.")]
        )
        assert "get_open_orders" in block
        assert "Every order that is still open." in block

    def test_renders_arguments_with_required_flag(self):
        block = render_tools_block(
            [
                FakeTool(
                    "get_orders",
                    "Orders.",
                    args_schema={
                        "properties": {
                            "region": {"type": "string", "description": "Sales region"},
                            "since": {"type": "string"},
                        },
                        "required": ["region"],
                    },
                )
            ]
        )
        assert "region (string, required) - Sales region" in block
        assert "since (string, optional)" in block

    def test_omits_the_injected_config_argument(self):
        block = render_tools_block(
            [FakeTool("t", "d", args_schema={"properties": {"config": {}, "question": {}}})]
        )
        assert "question" in block
        assert "config" not in block

    def test_no_tools_is_stated_not_faked(self):
        assert "(none available this turn)" in render_tools_block([])

    def test_prompt_describes_only_the_tools_it_was_given(self):
        prompt = build_system_prompt("2026-01-01", "a@b.com", [FakeTool("only_this_one", "x")])
        assert "only_this_one" in prompt
        # The two tools this repo happens to ship with must not leak in.
        assert "get_award_reasons_data" not in prompt
        assert "a@b.com" in prompt


class TestConnectionIsConfiguration:
    def test_template_builds_the_default_shape(self, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_URL", "")
        monkeypatch.setattr(mcp, "MCP_URL_TEMPLATE", "{base}/mcp/sse?scope={scope}")
        assert mcp.scope_url("https://host/", "analytics") == "https://host/mcp/sse?scope=analytics"

    def test_custom_template_is_honoured(self, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_URL", "")
        monkeypatch.setattr(mcp, "MCP_URL_TEMPLATE", "{base}/api/{scope}/mcp")
        assert mcp.scope_url("https://host", "sales") == "https://host/api/sales/mcp"

    def test_full_url_overrides_the_template(self, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_URL", "https://elsewhere/mcp")
        assert mcp.scope_url("https://host", "sales") == "https://elsewhere/mcp"

    def test_blank_allowlist_means_every_tool(self):
        assert mcp._parse_allowed("") == frozenset()
        assert mcp._parse_allowed("  ,  ") == frozenset()

    def test_allowlist_is_trimmed(self):
        assert mcp._parse_allowed(" a , b ,") == frozenset({"a", "b"})
