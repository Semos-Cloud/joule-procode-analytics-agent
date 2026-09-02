"""STAGE 4a — the UI contract.

The problem this solves: Joule's capability YAML substitutes values with SpEL
(``<? ... ?>``), and SpEL returns **strings**. You cannot loop an array into
buttons or chart rows from YAML. So a naive "let the agent describe a chart in
the conversation" approach dies immediately.

The way out is to stop treating narration and structure as the same channel:

* the agent **streams markdown prose** — that is the answer, and it is complete
  on its own;
* a second, forced LLM call then looks at the finished turn and emits a small
  **typed payload** describing the widget that should accompany it.

The payload is transport-agnostic. It contains data — ``render`` plus scalar
``fields``, array ``items``/``actions``, and a ``chart`` spec — and never client
markup. Each client adapts it:

* **Joule**, via the A2A gateway, receives it as a DataPart. For the array-ish
  render types the gateway also ships a pre-baked ``ui5integrationCard``
  manifest (:func:`build_joule_manifest`), so the capability YAML is a
  one-line passthrough instead of fragile Handlebars.
* **Any other client** (a web app talking to LangGraph directly) reads the same
  payload off the stream and renders it with its own components.

Because ``text`` is always present, a client that ignores ``render`` entirely
still shows a complete answer. That is the fallback that makes this safe.

This module is deliberately free of LangChain imports so the slim gateway
container can import it directly.
"""

import logging
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Bump when the vocabulary or field shapes change incompatibly. Shipped in every
# payload so a client can guard against drift.
CONTRACT_VERSION = "2"

RenderType = Literal["text", "choice", "confirm", "card", "status", "list", "form", "chart"]

# render -> required scalar `fields` keys. Single source of truth for the
# validator and the manifest builder.
RENDER_FIELD_SCHEMA: Dict[str, tuple] = {
    "text": (),
    "choice": (),          # options live in `actions`
    "confirm": ("summary",),
    "card": ("title",),
    "status": ("title",),  # `subtitle`, `state` optional
    "list": (),            # rows live in `items`
    "form": (),            # field definitions live in `items`
    "chart": (),           # data lives in `chart`
}

# Renders that are meaningless without their array, and fall back to text.
RENDER_REQUIRES_ACTIONS = frozenset({"choice"})
RENDER_REQUIRES_ITEMS = frozenset({"list", "form"})
RENDER_REQUIRES_CHART = frozenset({"chart"})

# Renders that legitimately carry `items`. Anywhere else it is spurious (a
# choice's options belong in `actions`) and gets dropped, which also stops the
# same list being drawn twice from two slots.
RENDER_KEEPS_ITEMS = frozenset({"card", "list", "form"})

RENDER_TYPES: frozenset = frozenset(RENDER_FIELD_SCHEMA)

# Values allowed inside `fields` and item rows. bool is a subclass of int.
_SCALAR_TYPES = (str, int, float)


# ── structured-output schema ─────────────────────────────────────────────────
#
# Passed to `llm.bind_tools([...], tool_choice=...)` by the synth node. It is a
# plain Pydantic model rather than a LangChain tool so this module stays
# importable from the LangChain-free gateway image.


class ChartSpec(BaseModel):
    """Numeric data to plot. The one place nested arrays are allowed."""

    chart_type: Literal["bar", "column", "line", "pie", "donut"] = "column"
    title: str = ""
    dimensions: List[str] = Field(
        default_factory=list,
        description="Category key(s) in each data row, e.g. ['name'].",
    )
    measures: List[str] = Field(
        default_factory=list,
        description="Numeric key(s) in each data row, e.g. ['engagement_index'].",
    )
    data: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="One flat object per category, holding the dimension and every measure.",
    )


class FinalResponse(BaseModel):
    """The structured widget that should accompany the reply already shown.

    Carries no narration: the user has already seen the assistant's message.
    """

    render: RenderType = "text"
    fields: Dict[str, Any] = Field(
        default_factory=dict,
        description="Scalar metadata only (str/number/bool). No arrays or nested objects.",
    )
    items: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Flat objects: list/card rows, or form field definitions.",
    )
    actions: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Flat objects [{title, value?}] — choice options or card buttons.",
    )
    chart: ChartSpec | None = None


UI_SYNTH_SYSTEM = """\
You are a UI formatter. Look ONLY at the assistant's LATEST message and decide
whether a structured widget should accompany it. You do NOT write narration —
the user already sees the message. Never restate or answer it.

Decide from what the assistant's latest message is DOING, not from what the user
asked earlier.

render options:

  "text"   - no widget. This is the DEFAULT; when in doubt, use it. Plain
             answers, explanations, follow-up questions the user answers by
             typing, and short summaries are all "text".
  "chart"  - the message presents numeric results worth plotting (a ranking, a
             breakdown, a comparison across people or categories). This is
             REQUIRED for "best / most / top / ranked" answers that have
             numbers — never use "list" for those. Put the numbers in `chart`:
               chart: {chart_type: "bar"|"column"|"line"|"pie"|"donut",
                       title, dimensions: [key], measures: [key],
                       data: [{<dimension>: str, <measure>: number}, ...]}
             If the assistant named a chart type (donut/pie/bar/line/column),
             use EXACTLY that; otherwise infer it from the data shape.
             For "best senders" / who gave the most, the measure is sent
             recognitions (or the count the assistant used), the dimension
             is the person's name.
  "list"   - non-numeric rows worth browsing (a catalogue of names or reasons
             with no ranking metric). Never use this when the answer ranks
             people or categories by a number.
             items: [{title, subtitle?, value?, image_url?}, ...]
  "card"   - a single entity summary.
             fields: {title, subtitle?, image_url?}
             items:  [{label, value}, ...]   # optional fact rows
  "status" - reports an outcome.
             fields: {title, subtitle?, state?: "info"|"success"|"error"}

HARD RULE — never invent data. Every number, name and label must already appear
in the assistant's message or in the tool results in context. If you cannot copy
the values, use render="text". Do not build a chart the assistant merely OFFERED
to produce ("would you like me to chart this?") — that arrives next turn, once
the user agrees.

`fields` holds scalars only. `items`/`actions` are arrays of flat objects.
`chart` is the only place nested numeric arrays belong.
"""


# ── validation ───────────────────────────────────────────────────────────────


def _scalar_dict(value: Any) -> Dict[str, Any]:
    """Keep only the scalar-valued entries of a dict."""
    out: Dict[str, Any] = {}
    if isinstance(value, dict):
        for key, val in value.items():
            if isinstance(val, _SCALAR_TYPES):
                out[str(key)] = val
            else:
                logger.warning(
                    "ui_contract: dropping non-scalar field %r (%s)", key, type(val).__name__
                )
    return out


def _object_list(value: Any) -> List[Dict[str, Any]]:
    """Sanitise an array of flat objects, keeping only scalar values in each."""
    if not isinstance(value, list):
        return []
    out: List[Dict[str, Any]] = []
    for entry in value:
        safe = _scalar_dict(entry)
        if safe:
            out.append(safe)
    return out


def _is_bare_ordinal(title: Any) -> bool:
    """True for a degenerate button label that is just a number ("1", "2.", "#3")."""
    if title is None:
        return True
    stripped = str(title).strip().strip(".)#:").strip()
    return not stripped or stripped.isdigit()


def _drop_degenerate_actions(actions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop the actions array when every title is a bare ordinal.

    Models sometimes emit ``[{"title": "1"}, {"title": "2"}]`` and leave the real
    labels in the prose. Numbered buttons are worse than none, and the narration
    already carries the options, so plain text loses nothing.
    """
    if actions and all(_is_bare_ordinal(a.get("title")) for a in actions):
        logger.warning(
            "ui_contract: all %d action titles are bare ordinals -> dropping actions",
            len(actions),
        )
        return []
    return actions


def _dedup_actions_against_items(
    items: List[Dict[str, Any]], actions: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Drop actions that merely repeat an item.

    The synth sometimes copies one list into both slots, which renders as a
    carousel *and* an identical row of buttons. Items are what you browse;
    actions are meant to be distinct controls, so genuine ones survive.
    """
    if not items or not actions:
        return actions
    keys = set()
    for item in items:
        for k in ("value", "title"):
            v = item.get(k)
            if v is not None:
                keys.add(str(v).strip().casefold())

    deduped = []
    for action in actions:
        candidates = [action.get("value"), action.get("title")]
        if any(c is not None and str(c).strip().casefold() in keys for c in candidates):
            continue
        deduped.append(action)

    if len(deduped) != len(actions):
        logger.info("ui_contract: dropped %d action(s) duplicating items", len(actions) - len(deduped))
    return deduped


def _valid_chart(chart: Any) -> Dict[str, Any] | None:
    """Normalise a chart spec, or None when it cannot be plotted."""
    if not isinstance(chart, dict):
        return None
    data = chart.get("data")
    if not isinstance(data, list) or not data:
        return None
    rows = [_scalar_dict(row) for row in data]
    rows = [row for row in rows if row]
    if not rows:
        return None

    dimensions = [str(d) for d in (chart.get("dimensions") or []) if d]
    measures = [str(m) for m in (chart.get("measures") or []) if m]

    # Recover the axes from the rows when the model omitted them: a chart with
    # good data and missing metadata is worth saving.
    if not dimensions or not measures:
        first = rows[0]
        if not dimensions:
            dimensions = [k for k, v in first.items() if isinstance(v, str)][:1]
        if not measures:
            measures = [
                k for k, v in first.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool) and k not in dimensions
            ]
    if not dimensions or not measures:
        return None

    return {
        "chart_type": chart.get("chart_type") or "column",
        "title": str(chart.get("title") or ""),
        "dimensions": dimensions,
        "measures": measures,
        "data": rows,
    }


def validate_contract(
    render: Any,
    fields: Any = None,
    items: Any = None,
    actions: Any = None,
    chart: Any = None,
) -> Dict[str, Any]:
    """Normalise a raw payload into ``{version, render, fields, items, actions[, chart]}``.

    Defensive by design, because the input is LLM output: an unknown ``render``
    becomes ``text``, non-scalar values are dropped, and a render whose required
    payload is missing degrades to ``text`` rather than reaching a client
    half-built. The narration still carries the content in every one of those
    cases, so a malformed payload is never fatal.
    """
    safe_render = render if isinstance(render, str) and render in RENDER_TYPES else "text"
    if safe_render != render:
        logger.warning("ui_contract: unknown render %r -> falling back to 'text'", render)

    safe_fields = _scalar_dict(fields)
    safe_items = _object_list(items)
    safe_actions = _drop_degenerate_actions(_object_list(actions))

    if safe_render not in RENDER_KEEPS_ITEMS:
        safe_items = []
    safe_actions = _dedup_actions_against_items(safe_items, safe_actions)

    safe_chart = _valid_chart(chart) if safe_render == "chart" else None

    missing_fields = [k for k in RENDER_FIELD_SCHEMA.get(safe_render, ()) if k not in safe_fields]
    missing = (
        missing_fields
        or (safe_render in RENDER_REQUIRES_ACTIONS and not safe_actions)
        or (safe_render in RENDER_REQUIRES_ITEMS and not safe_items)
        or (safe_render in RENDER_REQUIRES_CHART and safe_chart is None)
    )
    if missing:
        logger.warning(
            "ui_contract: render %r is missing its required payload -> falling back to 'text'",
            safe_render,
        )
        safe_render, safe_fields, safe_items, safe_actions, safe_chart = "text", {}, [], [], None

    payload: Dict[str, Any] = {
        "version": CONTRACT_VERSION,
        "render": safe_render,
        "fields": safe_fields,
        "items": safe_items,
        "actions": safe_actions,
    }
    if safe_chart is not None:
        payload["chart"] = safe_chart
    return payload


# ── Joule manifest builder ───────────────────────────────────────────────────
#
# Bakes a `ui5integrationCard` for the array render types so the capability YAML
# can be a single passthrough of `data.manifest`.
#
# Building this in Python rather than in YAML Handlebars is the whole point: it
# is deterministic, unit-testable, and it sidesteps SpEL's string-only
# substitution. `text`/`confirm`/`status` use Joule's native message types and
# `chart` has its own Analytical card, so all four return None here.

ADAPTIVE_CARD_VERSION = "1.2"


def _submit_action(action: Dict[str, Any]) -> Dict[str, Any]:
    title = str(action.get("title", ""))
    return {"type": "Action.Submit", "title": title, "data": {"value": action.get("value", title)}}


def _adaptive_card(body: List[dict], actions: List[dict] | None = None) -> Dict[str, Any]:
    card: Dict[str, Any] = {
        "type": "AdaptiveCard",
        "version": ADAPTIVE_CARD_VERSION,
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
    }
    if actions:
        card["actions"] = actions
    return card


def _wrap_ui5(adaptive_card: dict) -> Dict[str, Any]:
    """Wrap a bare AdaptiveCard in Joule's ui5integrationCard message shape."""
    return {
        "type": "ui5integrationCard",
        "content": {
            "_version": "1.17.0",
            "sap.app": {"type": "card", "id": "workshop.team_analytics"},
            "sap.card": {"type": "AdaptiveCard", "content": adaptive_card},
        },
    }


def _form_input(field: Dict[str, Any]) -> Dict[str, Any]:
    ftype = str(field.get("type", "text"))
    fid = str(field.get("name", ""))
    label = str(field.get("label", fid))
    if ftype == "select":
        return {
            "type": "Input.ChoiceSet",
            "id": fid,
            "label": label,
            "choices": [{"title": str(o), "value": str(o)} for o in (field.get("options") or [])],
        }
    if ftype == "toggle":
        return {"type": "Input.Toggle", "id": fid, "title": label}
    if ftype in ("number", "date"):
        return {
            "type": "Input.Number" if ftype == "number" else "Input.Date",
            "id": fid,
            "label": label,
        }
    return {"type": "Input.Text", "id": fid, "label": label, "isMultiline": ftype == "textarea"}


def build_joule_manifest(
    render: str,
    fields: Dict[str, Any] | None = None,
    items: List[Dict[str, Any]] | None = None,
    actions: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any] | None:
    """Bake a ``ui5integrationCard`` manifest, or None when Joule renders natively."""
    fields = fields or {}
    items = items or []
    actions = actions or []

    if render == "choice":
        return _wrap_ui5(
            _adaptive_card(
                body=[
                    {
                        "type": "TextBlock",
                        "text": str(fields.get("title", "Choose an option:")),
                        "wrap": True,
                    }
                ],
                actions=[_submit_action(a) for a in actions],
            )
        )

    if render == "card":
        body: List[dict] = [
            {
                "type": "TextBlock",
                "text": str(fields.get("title", "")),
                "weight": "bolder",
                "size": "large",
                "wrap": True,
            }
        ]
        if fields.get("subtitle"):
            body.append(
                {"type": "TextBlock", "text": str(fields["subtitle"]), "isSubtle": True, "wrap": True}
            )
        if fields.get("image_url"):
            body.append({"type": "Image", "url": str(fields["image_url"])})
        if items:
            body.append(
                {
                    "type": "FactSet",
                    "facts": [
                        {"title": str(i.get("label", "")), "value": str(i.get("value", ""))}
                        for i in items
                    ],
                }
            )
        return _wrap_ui5(_adaptive_card(body, [_submit_action(a) for a in actions]))

    if render == "list":
        body = []
        for item in items:
            row: List[dict] = []
            if item.get("image_url"):
                row.append({"type": "Image", "url": str(item["image_url"]), "size": "medium"})
            row.append(
                {"type": "TextBlock", "text": str(item.get("title", "")), "weight": "bolder", "wrap": True}
            )
            if item.get("subtitle"):
                row.append(
                    {"type": "TextBlock", "text": str(item["subtitle"]), "isSubtle": True, "wrap": True}
                )
            container: Dict[str, Any] = {"type": "Container", "items": row}
            if item.get("value") is not None:
                container["selectAction"] = {
                    "type": "Action.Submit",
                    "data": {"value": str(item["value"])},
                }
            body.append(container)
        return _wrap_ui5(_adaptive_card(body))

    if render == "form":
        return _wrap_ui5(
            _adaptive_card(
                [_form_input(f) for f in items],
                [{"type": "Action.Submit", "title": str(fields.get("submit_label", "Submit"))}],
            )
        )

    return None
