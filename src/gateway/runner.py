"""How the gateway actually runs a turn.

There are two ways, and the gateway does not care which it gets:

``HttpRunner``
    Posts to a LangGraph server and reads its SSE stream. This is the local
    workshop loop — ``langgraph dev`` on :2024 gives you LangGraph Studio, the
    graph visualisation and per-node traces while you are teaching Stage 3.

``LocalRunner``
    Imports the graph and calls it in this process. No LangGraph server, no
    Postgres, no Redis — one uvicorn process is the whole deployable. This is
    what runs on Cloud Foundry.

Both return the same ``(narration, ui_payload_or_None)`` and both finish the UI
payload the same way, so what Joule receives is identical either way.

Pick with ``AGENT_RUNTIME=local|http``.

One consequence of ``LocalRunner`` worth being explicit about: conversation
state lives in this process. The checkpointer is in memory and
``src/warehouse/store.py`` keys its DuckDB catalogs by thread id in a module
global, so **the app must run as a single instance with a single worker**. A
second replica would round-robin a manager onto a process that has never heard
of their conversation.
"""

import asyncio
import json
import logging
import os

from src.ui_contract import build_joule_manifest, validate_contract

logger = logging.getLogger(__name__)

AGENT_RUNTIME = (os.environ.get("AGENT_RUNTIME") or "http").strip().lower()
LANGGRAPH_API_URL = os.environ.get("LANGGRAPH_API_URL") or "http://localhost:2024"
LANGGRAPH_GRAPH_ID = os.environ.get("LANGGRAPH_GRAPH_ID") or "team_analytics"
A2A_TIMEOUT_SECONDS = int(os.environ.get("A2A_TIMEOUT_SECONDS") or "120")

NO_RESPONSE = "(no response from agent)"


# ── shared helpers ───────────────────────────────────────────────────────────


def message_text(content) -> str:
    """Flatten an AI message ``content`` field to plain text.

    Gen AI Hub often sends content as a list of blocks rather than a string.
    Treating only strings left the narration empty and Joule showed
    "(no response from agent)" even though the agent had answered.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str) and block:
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text") or block.get("content")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "".join(parts)
    return ""


def finalize_ui(ui: dict | None) -> dict | None:
    """Validate the contract and bake the ui5integrationCard manifest.

    Runs for both runners so the DataPart Joule sees does not depend on how the
    graph was invoked. Validation is repeated even though the graph already
    validated: it is cheap, idempotent, and for ``HttpRunner`` the payload
    arrived over a network boundary.
    """
    if ui is None:
        return None
    ui = validate_contract(
        ui.get("render"), ui.get("fields"), ui.get("items"), ui.get("actions"), ui.get("chart")
    )
    manifest = build_joule_manifest(ui["render"], ui["fields"], ui["items"], ui["actions"])
    if manifest is not None:
        ui["manifest"] = manifest
    logger.info("UI DataPart: render=%s manifest=%s", ui["render"], manifest is not None)
    return ui


def _find_ui(container: dict) -> dict | None:
    """Pull the ``ui`` payload out of a dict of node outputs.

    Scans by shape rather than by node name, so renaming the synth node does not
    silently break rendering.
    """
    if not isinstance(container, dict):
        return None
    found = None
    for node_output in container.values():
        if isinstance(node_output, dict) and isinstance(node_output.get("ui"), dict):
            found = node_output["ui"]
    return found


# ── in-process ───────────────────────────────────────────────────────────────


class LocalRunner:
    """Runs the compiled graph in this process."""

    def __init__(self) -> None:
        # Imported here, not at module scope: Dockerfile.gateway builds a thin
        # image with no langchain/langgraph in it, and that image only ever uses
        # HttpRunner. A top-level import would break it.
        from langgraph.checkpoint.memory import InMemorySaver

        from src.agent.graph import build_graph

        self._graph = build_graph(checkpointer=InMemorySaver())
        logger.info("Runner: in-process graph (single instance, in-memory state)")

    async def run(self, message_text_in: str, configurable: dict) -> tuple:
        state = await asyncio.wait_for(
            self._graph.ainvoke(
                {"messages": [{"role": "user", "content": message_text_in}]},
                {"configurable": configurable},
            ),
            timeout=A2A_TIMEOUT_SECONDS,
        )
        return _last_ai_text(state.get("messages") or []), finalize_ui(state.get("ui"))


def _last_ai_text(messages: list) -> str:
    """The agent's prose from a finished run.

    Walk backwards rather than taking ``messages[-1]``: the run ends at
    ``ui_synth``, which makes a forced tool call and can leave a trailing
    AIMessage carrying no text. Taking the last message would ship an empty
    narration on exactly the turns that produced a widget.
    """
    for msg in reversed(messages):
        if getattr(msg, "type", None) != "ai":
            continue
        text = message_text(getattr(msg, "content", None))
        if text.strip():
            return text
    return NO_RESPONSE


# ── over HTTP to a LangGraph server ──────────────────────────────────────────


class HttpRunner:
    """Posts to a LangGraph server and consumes its SSE stream."""

    def __init__(self) -> None:
        self._assistant_id: str | None = None
        logger.info("Runner: LangGraph server at %s", LANGGRAPH_API_URL)

    async def run(self, message_text_in: str, configurable: dict) -> tuple:
        import aiohttp

        timeout = aiohttp.ClientTimeout(total=A2A_TIMEOUT_SECONDS)
        thread = configurable["thread_id"]

        async with aiohttp.ClientSession(timeout=timeout) as session:
            assistant_id = await self._resolve_assistant_id(session)

            async with session.post(
                f"{LANGGRAPH_API_URL}/threads", json={"thread_id": thread}
            ) as resp:
                if resp.status not in (200, 201, 409):
                    logger.debug("Thread create returned %s", resp.status)

            payload = {
                "assistant_id": assistant_id,
                "input": {"messages": [{"role": "user", "content": message_text_in}]},
                "config": {"configurable": configurable},
                # `messages` carries the streamed prose; `updates` carries the
                # synth node's `ui` state delta. We need both.
                "stream_mode": ["messages", "updates"],
            }

            async with session.post(
                f"{LANGGRAPH_API_URL}/threads/{thread}/runs/stream",
                json=payload,
                headers={"Accept": "text/event-stream"},
            ) as resp:
                if resp.status != 200:
                    raise RuntimeError(f"LangGraph returned {resp.status}: {await resp.text()}")
                narration, ui = await self._consume(resp)

        return narration, finalize_ui(ui)

    async def _resolve_assistant_id(self, session) -> str:
        """Look up the LangGraph assistant UUID for our graph id, once."""
        if self._assistant_id:
            return self._assistant_id
        async with session.post(
            f"{LANGGRAPH_API_URL}/assistants/search",
            json={"graph_id": LANGGRAPH_GRAPH_ID, "limit": 1},
        ) as resp:
            data = await resp.json()
        if not data:
            raise RuntimeError(
                f"No LangGraph assistant found for graph_id={LANGGRAPH_GRAPH_ID!r}. "
                f"Is the LangGraph server running at {LANGGRAPH_API_URL}?"
            )
        self._assistant_id = data[0]["assistant_id"]
        logger.info("Resolved graph %r -> assistant %s", LANGGRAPH_GRAPH_ID, self._assistant_id)
        return self._assistant_id

    @staticmethod
    async def _consume(resp) -> tuple:
        """Read the SSE stream into (final narration, raw ui payload).

        Read in fixed-size chunks rather than by line: a single SSE data line can
        carry a few hundred rows of tool output and would overflow a line buffer.
        """
        narration = ""
        ui = None

        event_type = ""
        data_buffer = ""
        raw = b""

        def _flush_event() -> None:
            nonlocal event_type, data_buffer, narration, ui
            if not data_buffer:
                event_type, data_buffer = "", ""
                return
            try:
                data = json.loads(data_buffer)
            except json.JSONDecodeError:
                data = None
            if data is not None and event_type.startswith("messages"):
                # LangGraph 0.13 emits messages / messages/partial /
                # messages/complete.
                #
                # ui_synth then emits a second AIMessage that is only a tool call
                # (no text). Ignore those: overwriting narration with an empty
                # string shipped Joule's placeholder as the text artifact even
                # when the agent had already answered.
                msg = data[0] if isinstance(data, list) and data else data
                if isinstance(msg, dict) and msg.get("type") == "ai":
                    text = message_text(msg.get("content"))
                    if text:
                        narration = text
            elif data is not None and event_type == "updates":
                found = _find_ui(data)
                if found is not None:
                    ui = found
            event_type, data_buffer = "", ""

        async for chunk in resp.content.iter_chunked(65536):
            raw += chunk
            while b"\n" in raw:
                line_bytes, raw = raw.split(b"\n", 1)
                line = line_bytes.rstrip(b"\r").decode("utf-8", errors="replace")

                if line.startswith("event:"):
                    event_type, data_buffer = line[6:].strip(), ""
                elif line.startswith("data:"):
                    data_buffer += line[5:]
                elif not line:
                    _flush_event()
        if data_buffer:
            _flush_event()

        return narration or NO_RESPONSE, ui


# ── selection ────────────────────────────────────────────────────────────────

_runner = None


def get_runner():
    """The process-wide runner.

    Built once and cached: ``LocalRunner`` owns the checkpointer holding every
    live conversation, so a second instance would be a second set of threads.
    """
    global _runner
    if _runner is None:
        _runner = LocalRunner() if AGENT_RUNTIME == "local" else HttpRunner()
    return _runner
