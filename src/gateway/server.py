"""STAGE 4b — the A2A gateway.

Joule speaks A2A. LangGraph speaks its own REST API. This is the adapter, and it
is deliberately thin: it holds no agent logic, no routing and no prompt.

What it actually does:

1. Decodes the manager's identity from the BTP principal-propagation JWT and
   passes it into the run, so the MCP server scopes the data to the right team.
2. Maps Joule's ``conversationid`` onto a stable LangGraph thread, so a
   conversation keeps its history across turns.
3. Runs the turn — see :mod:`src.gateway.runner`, which either posts to a
   LangGraph server or calls the graph in this process — and emits **two
   artifacts**:
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
from src.gateway.runner import AGENT_RUNTIME, get_runner

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# Who the agent answers as when no identity arrives in the request.
#
# MCP_USER_EMAIL is the one knob to set: it is the same value probe_mcp.py uses,
# and the MCP server scopes every query to that person's team. Attendees put
# their own address there and get their own data. DEV_FALLBACK_USER_EMAIL stays
# supported so existing .env files keep working, but it is no longer required —
# two variables meaning "who am I" is one too many.
DEV_FALLBACK_USER_EMAIL = (
    os.environ.get("DEV_FALLBACK_USER_EMAIL") or os.environ.get("MCP_USER_EMAIL", "")
)

# Captures the inbound Authorization header for the executor, which the A2A SDK
# does not hand through directly.
_auth_header: ContextVar[str] = ContextVar("_auth_header", default="")

# Any string conversation id has to become a valid UUID for LangGraph. uuid5 is
# deterministic, so the same conversation lands on the same thread on every
# replica and no mapping table is needed.
_THREAD_NAMESPACE = uuid_mod.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


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
    """Forwards one A2A request to the agent and returns the result."""

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

        # Log which of the two this came from. Without it, a destination whose
        # principal propagation is misconfigured is invisible: the JWT lookup
        # returns "", every manager silently gets DEV_FALLBACK_USER_EMAIL's
        # team, and the answer still looks perfectly correct.
        jwt_email = _user_email_from_jwt(_auth_header.get(""))
        email = jwt_email or DEV_FALLBACK_USER_EMAIL
        logger.info(
            "Identity: %s (source=%s)", email or "(none)", "jwt" if jwt_email else "fallback"
        )
        if not jwt_email:
            self._explain_missing_identity(context)

        configurable = {
            "thread_id": thread,
            "user_email": email,
            "client": "joule",
        }

        try:
            text, ui = await get_runner().run(message_text, configurable)
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

    @staticmethod
    def _explain_missing_identity(context: RequestContext) -> None:
        """Say *why* the JWT produced no user, on the failure path only.

        A destination whose principal propagation is misconfigured is otherwise
        silent: everyone quietly gets DEV_FALLBACK_USER_EMAIL's team and the
        answers still look right. This distinguishes "no Authorization header
        arrived" from "one arrived that we could not read".

        Shapes and names only — never a token value or a claim value.
        """
        raw = _auth_header.get("")
        if not raw:
            shape = "absent"
        else:
            scheme, _, rest = raw.partition(" ")
            shape = f"scheme={scheme!r} segments={len(rest.split('.')) if rest else 0}"
        try:
            names = sorted(context.call_context.state.get("headers") or {})
        except (AttributeError, TypeError):
            names = []
        logger.info(
            "No identity in request: Authorization %s; headers seen: %s. "
            "Check the BTP destination forwards the user token.",
            shape,
            ", ".join(names) or "(none visible)",
        )

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
        return JSONResponse(
            {
                "status": "ok",
                "agent": AGENT_CARD.name,
                "path": f"/{AGENT_PATH}",
                "runtime": AGENT_RUNTIME,
                "url": AGENT_CARD.url,
            }
        )

    routes.append(Route("/health", health))
    logger.info("A2A agent registered at /%s (runtime=%s)", AGENT_PATH, AGENT_RUNTIME)

    return Starlette(routes=routes, middleware=[Middleware(AuthContextMiddleware)])


app = _build_app()
