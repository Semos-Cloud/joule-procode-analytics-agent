"""Model configuration — SAP Gen AI Hub (AI Core).

Gen AI Hub is the LLM proxy on SAP Business AI Platform. It resolves a *model
name* against the deployments in your AI Core resource group, which is why there
is no per-model endpoint URL here: the AICORE_* service-key values are the whole
configuration.

Swapping to another provider means changing this one file. Nothing in the agent,
the tools, or the UI contract knows which model is behind ``load_chat_model``.
"""

import logging
import os
from functools import lru_cache

from dotenv import load_dotenv
from gen_ai_hub.proxy.core import get_proxy_client
from gen_ai_hub.proxy.langchain.init_models import init_llm
from langchain_core.language_models import BaseChatModel

load_dotenv()

logger = logging.getLogger(__name__)

# The model that does the reasoning and calls the MCP tools.
AGENT_MODEL: str = os.getenv("AGENT_MODEL", "gpt-4.1-mini")

# The model behind the Stage 4 UI synthesizer. It only emits a small structured
# payload, so it does not need to be as capable as the reasoning model.
UI_SYNTH_MODEL: str = os.getenv("UI_SYNTH_MODEL", AGENT_MODEL)

MAX_TOKENS: int = int(os.getenv("MAX_TOKENS", "4096"))


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
) -> BaseChatModel:
    """Return a chat model served by Gen AI Hub.

    ``model`` must name a deployment that exists in ``AICORE_RESOURCE_GROUP``;
    an undeployed name fails here rather than at the first token.
    """
    name = model or AGENT_MODEL
    llm = init_llm(
        name,
        proxy_client=_proxy_client(),
        temperature=temperature,
        max_tokens=max_tokens or MAX_TOKENS,
    )
    # init_llm filters this constructor kwarg out, so it has to be set after the
    # fact. Without it, streamed responses carry no usage_metadata and token
    # counts are silently lost.
    llm.stream_usage = True
    logger.info("Gen AI Hub model ready: %s", name)
    return llm
