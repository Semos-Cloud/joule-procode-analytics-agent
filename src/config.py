"""Model configuration — SAP Gen AI Hub (AI Core).

Gen AI Hub is the LLM proxy on SAP Business AI Platform. It resolves a *model
name* against the deployments in your AI Core resource group, which is why there
is no per-model endpoint URL here: the AICORE_* service-key values are the whole
configuration.

Swapping to another provider means changing this one file. Nothing in the agent,
the tools, or the UI contract knows which model is behind ``load_chat_model``.
"""

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from gen_ai_hub.proxy.core import get_proxy_client
from gen_ai_hub.proxy.langchain.init_models import init_llm
from langchain_core.language_models import BaseChatModel

# LangGraph loads ``langgraph.json`` ``env`` first, and that pass interpolates
# ``$`` in values. The AI Core client secret contains ``$``, so a truncated
# secret is already in ``os.environ`` by the time this module imports. Reload
# AICORE_* from the file without interpolation and overwrite.
load_dotenv(interpolate=False)
_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"
if _ENV_FILE.is_file():
    for _key, _val in dotenv_values(_ENV_FILE, interpolate=False).items():
        # Blank means unset: an empty .env line must not clobber a real value.
        if _key and _key.startswith("AICORE_") and _val:
            os.environ[_key] = _val

# The AI Core SDK uses ``requests.post`` for the OAuth token. ``requests``
# honours HTTP(S)_PROXY. A Cursor/sandbox proxy that MCP/httpx can get through
# still 403s that call, and the SDK wraps it as "Could not retrieve
# Authorization token" with no status. Direct to IAS/AI Core; do not proxy.
for _proxy_key in (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
):
    os.environ.pop(_proxy_key, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

logger = logging.getLogger(__name__)
_secret = os.environ.get("AICORE_CLIENT_SECRET", "")
logger.info(
    "AI Core env: secret_len=%s has_dollar=%s auth_url_set=%s",
    len(_secret),
    "$" in _secret,
    bool(os.environ.get("AICORE_AUTH_URL")),
)

# The model that does the reasoning and calls the MCP tools.
AGENT_MODEL: str = os.getenv("AGENT_MODEL") or "gpt-4.1-mini"

# The model behind the Stage 4 UI synthesizer. It only emits a small structured
# payload, so it does not need to be as capable as the reasoning model.
UI_SYNTH_MODEL: str = os.getenv("UI_SYNTH_MODEL") or AGENT_MODEL

MAX_TOKENS: int = int(os.getenv("MAX_TOKENS") or "4096")


@lru_cache(maxsize=1)
def _proxy_client():
    """The shared Gen AI Hub proxy client.

    Building it costs an OAuth token request plus a deployment listing, so it is
    created once and reused. It refreshes its own token and re-lists deployments
    when a model lookup misses.
    """
    return get_proxy_client()


@lru_cache(maxsize=8)
def load_chat_model(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    stream_usage: bool = True,
) -> BaseChatModel:
    """Return a chat model served by Gen AI Hub.

    ``model`` must name a deployment that exists in ``AICORE_RESOURCE_GROUP``;
    an undeployed name fails here rather than at the first token.

    ``stream_usage`` must stay off for structured-output / tool-choice calls.
    Gen AI Hub's ChatOpenAI wrapper forwards ``deployment_id`` into
    ``AsyncCompletions.stream()``, which the current OpenAI client rejects.
    The agent node uses ``ainvoke`` and is fine with streaming usage on.
    """
    name = model or AGENT_MODEL
    try:
        llm = init_llm(
            name,
            proxy_client=_proxy_client(),
            temperature=temperature,
            max_tokens=max_tokens or MAX_TOKENS,
        )
    except Exception:
        logger.exception("Gen AI Hub init_llm failed for %s", name)
        raise
    llm.stream_usage = stream_usage
    logger.info("Gen AI Hub model ready: %s stream_usage=%s", name, stream_usage)
    return llm


async def aload_chat_model(
    model: str | None = None,
    temperature: float = 0.0,
    max_tokens: int | None = None,
    stream_usage: bool = True,
) -> BaseChatModel:
    """``load_chat_model`` moved off the event loop.

    The first call for a model is blocking I/O: Gen AI Hub fetches an OAuth
    token and lists deployments through the SAP AI Core SDK, which is built on
    synchronous ``requests``. Called straight from an async node that trips
    LangGraph's blocking-call guard, and because the SDK catches every
    exception around the token request it reports the guard's ``BlockingError``
    as "Could not retrieve Authorization token" — an auth message for what is
    not an auth problem. Later calls hit the ``lru_cache`` and cost only the
    thread hop.
    """
    return await asyncio.to_thread(
        load_chat_model, model, temperature, max_tokens, stream_usage
    )
