"""STAGE 4a — the UI synthesizer node.

Runs once after the agent has finished its reply. It makes one **forced**
structured-output call over the turn that just happened, asking a single
question: does this reply deserve a widget, and if so, what data goes in it?

It returns no message, on purpose. Narration is the agent's channel and has
already streamed to the user; this node writes only ``state["ui"]``. That
separation is what keeps a chart from ever appearing as JSON in the chat.

The model doing the synthesis is small and gets things wrong in predictable
ways, so the deterministic guards below matter as much as the prompt. Each one
exists because of a specific failure:

* **Offer guard** — the assistant asks "would you like a chart?" and the synth
  renders the chart, so the answer appears next to its own question.
* **Chart-type fidelity** — the manager asks for a donut, the synth infers "bar"
  from the data shape. The assistant's own wording is the only intent signal
  reaching this node, so it wins.
* **Degenerate choice** — the synth emits ``[{"title": "1"}, {"title": "2"}]``,
  leaving the real labels in the prose.
* **Link actions** — actions are post-backs the user taps to reply, so a URL in
  a ``value`` would post the URL back as the user's next message.
"""

import logging
import re
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from src.config import UI_SYNTH_MODEL, load_chat_model
from src.ui_contract import UI_SYNTH_SYSTEM, FinalResponse, validate_contract

logger = logging.getLogger(__name__)

# An offer to *produce* an artifact, keyed by the render it would have produced,
# so a chart the assistant already presented ("here is your chart: ...") is not
# vetoed and an offer of a different artifact type does not veto this one.
_OFFER_VERB = r"(?:would you like|do you want|shall i|should i|want me to|can i)"
_RENDER_OFFER_RE = {
    "chart": re.compile(
        _OFFER_VERB + r"[^?]{0,120}\b(?:chart|graph|visuali[sz]\w*|plot|diagram)\b[^?]*\?",
        re.I | re.S,
    ),
    "card": re.compile(_OFFER_VERB + r"[^?]{0,120}\bcard\b[^?]*\?", re.I | re.S),
    "list": re.compile(_OFFER_VERB + r"[^?]{0,120}\blist\b[^?]*\?", re.I | re.S),
}

_URL_RE = re.compile(r"https?://", re.I)

# Two-tier match, so an incidental "in a column format" (describing a table)
# never beats an explicit "...as a donut chart". The LAST match wins, since the
# concluding sentence is the one declaring what is actually being rendered.
_CHART_TYPE_WORD = r"(donut|doughnut|pie|bar|line|column)"
_CHART_TYPE_NAMED_RE = re.compile(rf"\b{_CHART_TYPE_WORD}\s+(?:chart|graph)\b", re.I)
_CHART_TYPE_ANY_RE = re.compile(rf"\b{_CHART_TYPE_WORD}\b", re.I)


def _named_chart_type(text: str) -> str | None:
    """The chart type the assistant named in prose, normalised, or None."""
    matches = _CHART_TYPE_NAMED_RE.findall(text) or _CHART_TYPE_ANY_RE.findall(text)
    if not matches:
        return None
    return matches[-1].lower().replace("doughnut", "donut")


def _current_turn(messages: List[Any]) -> List[Any]:
    """Messages from the last HumanMessage onward.

    A self-contained slice (human ask -> tool steps -> final reply), so the
    forced call never starts on an orphaned ToolMessage, which the model API
    rejects outright.
    """
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            return messages[i:]
    return messages[-8:]


def _last_assistant_text(messages: List[Any]) -> str:
    """Plain text of the newest assistant message in the turn."""
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            return content if isinstance(content, str) else ""
        if isinstance(m, HumanMessage):
            break
    return ""


def _latest_reply_index(turn: List[Any]) -> int | None:
    """Index of the newest assistant message carrying real text."""
    for i in range(len(turn) - 1, -1, -1):
        m = turn[i]
        if isinstance(m, AIMessage):
            content = m.content
            if isinstance(content, list):
                content = " ".join(
                    b.get("text", "") for b in content if isinstance(b, dict)
                )
            if isinstance(content, str) and content.strip():
                return i
    return None


def _compact_transcript(turn: List[Any], max_tool_chars: int = 3000) -> str:
    """Flatten only the decision-relevant tail of the turn.

    The UI decision needs exactly two things: the reply the manager is looking at,
    and the data backing it — the tool results from that reply's own round, i.e.
    the ToolMessages contiguous immediately before it.

    The manager's original question is deliberately left out. Including it primed
    the synth to render whatever was asked for, even when the assistant had only
    *offered* to produce it, which is the chart-next-to-its-own-question bug.
    """
    reply_idx = _latest_reply_index(turn)
    parts: List[str] = []

    if reply_idx is not None:
        j = reply_idx - 1
        round_parts: List[str] = []
        while j >= 0 and isinstance(turn[j], ToolMessage):
            m = turn[j]
            if m.name:
                content = m.content if isinstance(m.content, str) else str(m.content)
                round_parts.append(
                    f"DATA FROM TOOL `{m.name}`:\n{content[:max_tool_chars]}"
                )
            j -= 1
        parts.extend(reversed(round_parts))

    parts.append(
        "ASSISTANT'S LATEST MESSAGE (the one the user is looking at):\n"
        + _last_assistant_text(turn)
    )
    return "\n\n---\n\n".join(parts)


def make_ui_synth_node(llm=None):
    """Build the UI synthesizer node.

    ``llm`` is resolved on first use rather than at import time, so the graph can
    be imported — and ``langgraph dev`` can start — before credentials are
    present. Pass a model explicitly to pin one, mainly for tests.
    """
    synth = llm.with_structured_output(FinalResponse) if llm is not None else None

    def _runnable():
        nonlocal synth
        if synth is None:
            synth = load_chat_model(UI_SYNTH_MODEL).with_structured_output(FinalResponse)
        return synth

    async def _synth(payload: List[Any], config: RunnableConfig) -> Dict[str, Any]:
        result = await _runnable().ainvoke(payload, config)
        if isinstance(result, FinalResponse):
            return result.model_dump(exclude_none=True)
        return dict(result or {})

    async def ui_synth_node(state: Dict[str, Any], config: RunnableConfig) -> Dict[str, Any]:
        turn = _current_turn(list(state.get("messages") or []))
        payload = [
            SystemMessage(content=UI_SYNTH_SYSTEM),
            HumanMessage(content=_compact_transcript(turn)),
        ]

        args: Dict[str, Any] = {}
        try:
            args = await _synth(payload, config)
            # A choice with fewer than two options means the options never made
            # it into the payload; a one-button widget is worse than prose.
            # Retry once, then give up and let the narration carry it.
            if args.get("render") == "choice" and len(args.get("actions") or []) < 2:
                logger.info("ui_synth: degenerate choice -> retrying once")
                args = await _synth(payload, config)
        except Exception as e:
            # UI synthesis must never break a reply the user already received.
            logger.warning("ui_synth failed (%s) -> render=text", e)
            args = {"render": "text"}

        reply_text = _last_assistant_text(turn)

        offer_re = _RENDER_OFFER_RE.get(args.get("render"))
        if offer_re and offer_re.search(reply_text):
            logger.info(
                "ui_synth: assistant only offers to produce %r -> text", args.get("render")
            )
            args = {"render": "text"}

        actions = args.get("actions")
        if isinstance(actions, list):
            cleaned = [
                a
                for a in actions
                if not (isinstance(a, dict) and _URL_RE.search(str(a.get("value") or "")))
            ]
            if len(cleaned) != len(actions):
                logger.info("ui_synth: dropped %d link action(s)", len(actions) - len(cleaned))
                args["actions"] = cleaned

        if args.get("render") == "chart" and isinstance(args.get("chart"), dict):
            named = _named_chart_type(reply_text)
            if named and named != args["chart"].get("chart_type"):
                logger.info("ui_synth: honouring chart type %r named in the reply", named)
                args["chart"]["chart_type"] = named

        contract = validate_contract(
            args.get("render"),
            args.get("fields"),
            args.get("items"),
            args.get("actions"),
            args.get("chart"),
        )
        logger.info("ui_synth -> render=%r", contract["render"])
        return {"ui": contract}

    return ui_synth_node
