"""The MCP connection authenticates the agent and acts as a real user.

JobPoints trusts ``x-mcp-user-email`` once the agent presents
``x-mcp-service-key``, so these pin the rules around it: the key is sent when it
is configured and never otherwise, a connection with no user email fails before
anything connects, and the probe sends exactly what the agent sends.
``MultiServerMCPClient`` is replaced by a recorder, so no server is contacted.
"""

import asyncio

import pytest

import src.mcp_client as mcp

KEY = "k3y-0f-the-agent"
EMAIL = "manager@example.com"


def _run(coro):
    return asyncio.run(coro)


class FakeTool:
    def __init__(self, name):
        self.name = name


@pytest.fixture
def connections(monkeypatch):
    """Record every MCP client the code builds instead of opening it."""
    calls = []

    class FakeClient:
        def __init__(self, servers):
            calls.extend(servers.values())

        async def get_tools(self):
            return [FakeTool("get_report")]

    monkeypatch.setattr(mcp, "MultiServerMCPClient", FakeClient)
    monkeypatch.setattr(mcp, "_cache", {})
    monkeypatch.setattr(mcp, "_clients", [])
    monkeypatch.setattr(mcp, "ALLOWED_TOOLS", frozenset())
    monkeypatch.setattr(mcp, "MCP_USER_HEADER", "x-mcp-user-email")
    monkeypatch.setattr(mcp, "MCP_USER_EMAIL", "")
    monkeypatch.setattr(mcp, "MCP_SERVICE_KEY", KEY)
    monkeypatch.setattr(mcp, "MCP_SERVICE_KEY_HEADER", "x-mcp-service-key")
    return calls


class TestHeaders:
    def test_sends_service_key_and_user_email(self, connections):
        _run(mcp.load_mcp_tools(EMAIL))

        assert connections[0]["headers"] == {
            "x-mcp-user-email": EMAIL,
            "x-mcp-service-key": KEY,
        }

    def test_never_sends_the_key_as_a_bearer_token(self, connections):
        _run(mcp.load_mcp_tools(EMAIL))

        assert "Authorization" not in connections[0]["headers"]

    def test_blank_key_sends_no_key_header(self, connections, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_SERVICE_KEY", "")

        _run(mcp.load_mcp_tools(EMAIL))

        assert connections[0]["headers"] == {"x-mcp-user-email": EMAIL}

    def test_key_header_name_is_configuration(self, connections, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_SERVICE_KEY_HEADER", "x-api-key")

        _run(mcp.load_mcp_tools(EMAIL))

        assert connections[0]["headers"]["x-api-key"] == KEY


class TestIdentity:
    def test_missing_email_fails_without_connecting(self, connections):
        with pytest.raises(ValueError, match="no one to act as"):
            _run(mcp.load_mcp_tools(""))
        assert connections == []

    def test_falls_back_to_the_configured_user(self, connections, monkeypatch):
        monkeypatch.setattr(mcp, "MCP_USER_EMAIL", "workshop@example.com")

        _run(mcp.load_mcp_tools(""))

        assert connections[0]["headers"]["x-mcp-user-email"] == "workshop@example.com"

    def test_tools_are_cached_per_user(self, connections):
        _run(mcp.load_mcp_tools(EMAIL))
        _run(mcp.load_mcp_tools(EMAIL))
        _run(mcp.load_mcp_tools("other@example.com"))

        assert [c["headers"]["x-mcp-user-email"] for c in connections] == [
            EMAIL,
            "other@example.com",
        ]


class TestProbeConnectsLikeTheAgent:
    def test_probe_sends_the_same_headers(self, connections, monkeypatch, capsys):
        import scripts.probe_mcp as probe_mcp

        recorded = []

        class FakeClient:
            def __init__(self, servers):
                recorded.extend(servers.values())

            async def get_tools(self):
                return []

        monkeypatch.setattr(probe_mcp, "MultiServerMCPClient", FakeClient)

        _run(probe_mcp.probe("https://host", "analytics", EMAIL, False))
        _run(mcp.load_mcp_tools(EMAIL))

        assert recorded[0]["headers"] == connections[0]["headers"]
        # The key is a secret: the probe says whether it is set, never what it is.
        out = capsys.readouterr().out
        assert "x-mcp-service-key: (set)" in out
        assert KEY not in out

    def test_probe_without_email_stops_before_connecting(self, monkeypatch, capsys):
        import scripts.probe_mcp as probe_mcp

        def refuse(_servers):
            raise AssertionError("probe connected without an email")

        monkeypatch.setattr(probe_mcp, "MultiServerMCPClient", refuse)

        assert _run(probe_mcp.probe("https://host", "analytics", "", False)) == 1
        assert "MCP_USER_EMAIL" in capsys.readouterr().out
