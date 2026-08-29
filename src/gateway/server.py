"""STAGE 4b — the A2A gateway.

Joule speaks A2A. LangGraph speaks its own REST API. This is the adapter, and it
is deliberately thin: it holds no agent logic, no routing and no prompt.

What it actually does:

1. Decodes the manager's identity from the BTP principal-propagation JWT and
   passes it into the run, so the MCP server scopes the data to the right team.
2. Maps Joule's ``conversationid`` onto a stable LangGraph thread, so a
   conversation keeps its history across turns.
3. Streams the run and emits **two artifacts**:
   - ``response`` — the markdown narration, always present;
   - ``ui`` — the structured contract as a DataPart, present only when the
     synth node produced one, with a pre-baked ui5integrationCard manifest
     attached for the render types Joule cannot build from YAML.

That second artifact is the whole generative-UI mechanism. Everything else here
is plumbing.
"""

import base64
import json
import logging
import os
import uuid as uuid_mod
from contextvars import ContextVar

import aiohttp
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.apps import A2AStarletteApplication
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import TaskArtifactUpdateEvent, TaskState, TaskStatus, TaskStatusUpdateEvent
from a2a.utils import new_agent_text_message, new_data_artifact, new_task, new_text_artifact
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.gateway.cards import AGENT_CARD, AGENT_PATH
from src.ui_contract import build_joule_manifest, validate_contract

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

LANGGRAPH_API_URL = os.environ.get("LANGGRAPH_API_URL", "http://localhost:8000")
LANGGRAPH_GRAPH_ID = os.environ.get("LANGGRAPH_GRAPH_ID", "team_analytics")
A2A_TIMEOUT_SECONDS = int(os.environ.get("A2A_TIMEOUT_SECONDS", "120"))
DEV_FALLBACK_USER_EMAIL = os.environ.get("DEV_FALLBACK_USER_EMAIL", "")

# Captures the inbound Authorization header for the executor, which the A2A SDK
# does not hand through directly.
_auth_header: ContextVar[str] = ContextVar("_auth_header", default="")

# Any string conversation id has to become a valid UUID for LangGraph. uuid5 is
# deterministic, so the same conversation lands on the same thread on every
# replica and no mapping table is needed.
_THREAD_NAMESPACE = uuid_mod.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")

_assistant_id: str | None = None


async def _resolve_assistant_id(session: aiohttp.ClientSession) -> str:
    """Look up the LangGraph assistant UUID for our graph id, once."""
    global _assistant_id
    if _assistant_id:
        return _assistant_id
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
    _assistant_id = data[0]["assistant_id"]
    logger.info("Resolved graph %r -> assistant %s", LANGGRAPH_GRAPH_ID, _assistant_id)
    return _assistant_id


def _user_email_from_jwt(authorization: str) -> str:
    """Read the user out of the BTP principal-propagation JWT.

    BTP signs and attaches this token when the destination is configured with
    principal propagation. We read ``email`` (falling back to ``user_name`` then
    ``sub``), because the MCP server identifies managers by email address.

    The signature is not verified here: the token arrives over the internal BTP
    network from a trusted issuer. A public deployment should validate it
    properly before trusting the claim.
    """
    if not authorization.startswith("Bearer "):
        return ""
    parts = authorization[len("Bearer "):].split(".")
    if len(parts) != 3:
        return ""
    payload_b64 = parts[1]
    payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as exc:
        logger.warning("Could not decode JWT payload: %s", exc)
        return ""
    return claims.get("email") or claims.get("user_name") or claims.get("sub") or ""


def _thread_id(conversation_id: str | None) -> str:
    if not conversation_id:
        return str(uuid_mod.uuid4())
    try:
        return str(uuid_mod.UUID(conversation_id))
    except (ValueError, AttributeError, TypeError):
        return str(uuid_mod.uuid5(_THREAD_NAMESPACE, conversation_id))


class TeamAnalyticsExecutor(AgentExecutor):
    """Forwards one A2A request to the LangGraph run and streams the result back."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task(context.message)
        await event_queue.enqueue_event(task)
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=False,
                status=TaskStatus(
                    state=TaskState.working,
                    message=new_agent_text_message("Looking at your team's data..."),
                ),
            )
        )

        message_text = self._extract_text(context)
        thread = _thread_id(self._extract_conversation_id(context))
        logger.info("Received: %r (thread %s)", message_text[:200], thread)

        try:
            text, ui = await self._run(message_text, thread)
        except Exception as exc:
            logger.exception("Run failed: %s", exc)
            text, ui = "Sorry, I could not complete that request. Please try again.", None

        await event_queue.enqueue_event(
            TaskArtifactUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                artifact=new_text_artifact(name="response", text=text),
            )
        )

        if ui is not None:
            await event_queue.enqueue_event(
                TaskArtifactUpdateEvent(
                    task_id=context.task_id,
                    context_id=context.context_id,
                    artifact=new_data_artifact(name="ui", data=ui),
                )
            )

        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                final=True,
                status=TaskStatus(state=TaskState.completed),
            )
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("cancel is not supported")

    async def _run(self, message_text: str, thread: str) -> tuple:
        """Run the graph and return ``(narration, ui_payload_or_None)``."""
        configurable = {
            "thread_id": thread,
            "user_email": _user_email_from_jwt(_auth_header.get("")) or DEV_FALLBACK_USER_EMAIL,
            "client": "joule",
        }

        timeout = aiohttp.ClientTimeout(total=A2A_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            assistant_id = await _resolve_assistant_id(session)

            async with session.post(
                f"{LANGGRAPH_API_URL}/threads", json={"thread_id": thread}
            ) as resp:
                if resp.status not in (200, 201, 409):
                    logger.debug("Thread create returned %s", resp.status)

            payload = {
                "assistant_id": assistant_id,
                "input": {"messages": [{"role": "user", "content": message_text}]},
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
                return await self._consume(resp)

    @staticmethod
    async def _consume(resp) -> tuple:
        """Read the SSE stream into (final narration, ui payload).

        Read in fixed-size chunks rather than by line: a single SSE data line can
        carry a few hundred rows of tool output and would overflow a line buffer.
        """
        narration = ""
        current_message_id = None
        ui = None

        event_type = ""
        data_buffer = ""
        raw = b""

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
                    if data_buffer:
                        try:
                            data = json.loads(data_buffer)
                        except json.JSONDecodeError:
                            data = None

                        if data is not None and event_type == "messages/partial":
                            if isinstance(data, list) and data:
                                msg = data[0]
                                if msg.get("type") == "ai":
                                    # A new message id means a fresh assistant turn
                                    # (for example after a tool call), so restart
                                    # the accumulation rather than concatenating.
                                    if msg.get("id") != current_message_id:
                                        current_message_id = msg.get("id")
                                        narration = ""
                                    content = msg.get("content")
                                    if isinstance(content, str) and content:
                                        narration = content
                        elif data is not None and event_type == "updates":
                            found = TeamAnalyticsExecutor._find_ui(data)
                            if found is not None:
                                ui = found
                    event_type, data_buffer = "", ""

        if ui is not None:
            # Re-validate: the payload was already checked in-graph, but the
            # stream is untrusted input to this process and the check is cheap
            # and idempotent.
            ui = validate_contract(
                ui.get("render"), ui.get("fields"), ui.get("items"), ui.get("actions"), ui.get("chart")
            )
            manifest = build_joule_manifest(ui["render"], ui["fields"], ui["items"], ui["actions"])
            if manifest is not None:
                ui["manifest"] = manifest
            logger.info("UI DataPart: render=%s manifest=%s", ui["render"], manifest is not None)

        return narration or "(no response from agent)", ui

    @staticmethod
    def _find_ui(update: dict) -> dict | None:
        """Pull the ``ui`` payload out of an updates event.

        Scans node outputs by shape rather than by node name, so renaming the
        synth node does not silently break rendering.
        """
        if not isinstance(update, dict):
            return None
        found = None
        for node_output in update.values():
            if isinstance(node_output, dict) and isinstance(node_output.get("ui"), dict):
                found = node_output["ui"]
        return found

    @staticmethod
    def _extract_text(context: RequestContext) -> str:
        if context.message and context.message.parts:
            for part in context.message.parts:
                if hasattr(part, "root") and hasattr(part.root, "text"):
                    return part.root.text
                if hasattr(part, "text"):
                    return part.text
        return ""

    @staticmethod
    def _extract_conversation_id(context: RequestContext) -> str | None:
        """Joule's conversation id, however it spelled the header."""
        try:
            headers = context.call_context.state.get("headers") or {}
        except (AttributeError, TypeError):
            return None
        accepted = {
            "conversationid",
            "conversation-id",
            "conversation_id",
            "x-conversationid",
            "x-conversation-id",
        }
        for key, value in headers.items():
            if isinstance(key, str) and key.lower() in accepted and value:
                return value
        return None


class AuthContextMiddleware(BaseHTTPMiddleware):
    """Make the Authorization header visible to the executor."""

    async def dispatch(self, request: Request, call_next):
        token = _auth_header.set(request.headers.get("Authorization", ""))
        try:
            return await call_next(request)
        finally:
            _auth_header.reset(token)


def _build_app() -> Starlette:
    handler = DefaultRequestHandler(
        agent_executor=TeamAnalyticsExecutor(),
        task_store=InMemoryTaskStore(),
    )
    a2a_app = A2AStarletteApplication(agent_card=AGENT_CARD, http_handler=handler)

    routes = []
    for route in a2a_app.routes():
        routes.append(
            Route(
                f"/{AGENT_PATH}{route.path}",
                route.endpoint,
                methods=route.methods,
                name=f"{AGENT_PATH}_{route.name}" if route.name else None,
            )
        )
        # Joule posts to /path, not /path/. Registering both avoids a 307
        # redirect that would drop the POST body.
        if route.path == "/" and "POST" in (route.methods or set()):
            routes.append(
                Route(
                    f"/{AGENT_PATH}",
                    route.endpoint,
                    methods=route.methods,
                    name=f"{AGENT_PATH}_rpc_noslash",
                )
            )

    async def health(_request):
        return JSONResponse({"status": "ok", "agent": AGENT_CARD.name, "path": f"/{AGENT_PATH}"})

    routes.append(Route("/health", health))
    logger.info("A2A agent registered at /%s -> graph %r", AGENT_PATH, LANGGRAPH_GRAPH_ID)

    return Starlette(routes=routes, middleware=[Middleware(AuthContextMiddleware)])


app = _build_app()
