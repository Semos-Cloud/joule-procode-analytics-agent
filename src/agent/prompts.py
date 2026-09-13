"""System prompt for the agent.

The prompt has three parts, and only the first is domain knowledge:

``PERSONA``
    Who the agent is and what it is for. Override with ``AGENT_PERSONA`` when
    you point the agent at your own MCP server — this is the part that stops
    being true when the data changes.
``the tools block``
    Generated at runtime from the tools actually bound this turn: their real
    names, their own descriptions, and their real arguments. Nothing here can
    drift out of sync with the server, because there is nothing here to drift.
``the rules``
    Grounding, dates, formatting. These are about how to be a trustworthy
    analytics agent and do not depend on the data at all, so they stay fixed.

The rules at the bottom are the guardrails. None of them are decorative — each
one closes a specific failure we have actually seen.
"""

import os
from typing import Any, List

DEFAULT_PERSONA = """\
You are a team analytics assistant for people managers. You answer questions
about how a manager's own team is participating in recognition, using read-only
reports. You never send, create or change anything.\
"""

PERSONA: str = os.getenv("AGENT_PERSONA", "").strip() or DEFAULT_PERSONA

# Domain judgement, and the one thing tool metadata cannot give you.
#
# A tool schema tells the model a tool's name and arguments. It does not say
# which tool answers which kind of question, or what an ambiguous phrase means
# in this data. Generating the tools block from the MCP server removed a
# hand-written block that happened to carry that steering, and the agent started
# reading "who moved most on engagement" as a quarter-over-quarter delta, finding
# no time series, and refusing to answer.
#
# So it gets its own variable rather than going back into the tool block: still
# no code edit to point at another server, but the judgement is explicit instead
# of smuggled in beside the tool names.
DEFAULT_GUIDANCE = """\
- "Engagement index" is a percentage already present per person in the team
  reach report. "Who moved most on engagement", "who is most engaged" and
  "best engagement" all mean: rank people by that index and name the leaders.
  There is no historical series to difference, so never report a change between
  periods as missing data - answer the ranking question that was meant.
- Per-person questions (ranking people, who gave or received most, who is not
  participating) come from the team reach report.
- Reason or theme questions (which award reasons, monetary versus non-monetary)
  come from the award reasons report.
- Quarters are calendar quarters. "Last quarter" is the last COMPLETED one.\
"""

GUIDANCE: str = os.getenv("AGENT_GUIDANCE", "").strip() or DEFAULT_GUIDANCE

RULES = """\
CHOOSING A TOOL
  - A straightforward pull of one report -> call that report's tool directly.
  - A question that is easier as SQL over rows already loaded -> use the
    warehouse tool.
  - The snapshot is this user's data from session start. Say so if you use it;
    do not add a warehouse count to a later live count.

RANKING QUESTIONS - read this before asking for one row
  "Who is top", "who moved most", "who is least active" and "best/worst" are
  comparisons. Retrieve the WHOLE set and rank it yourself. Never ask a tool for
  a single row: with one row you cannot say who came second, you cannot show a
  chart worth looking at, and you have no way to know the top row is meaningful.
  Ask for every candidate, then name the leader and the ones behind them.

HANDLING DATES
  Convert relative periods to explicit bounds yourself before calling a tool.
  "Last quarter", "the last six months" and "this year" are all things you can
  resolve from the current date - do not ask the user to restate them. Ask about
  the period only when the question is genuinely ambiguous, and say which range
  you used whenever you picked one.

HOW TO ANSWER
  1. Call the tool(s) you need.
  2. Do the analysis yourself on the returned rows: rank, total, compare,
     compute averages and changes.
  3. Lead with the answer to the question that was asked, then support it. Name
     the entities that matter and give their numbers. For ranking questions
     ("best senders", "who received the most", "moved most") always state the
     metric and the value for each one named - not just a label - and name
     several, not only the winner. Keep it short enough to read in the flow of
     work.
  4. Offer one genuinely useful follow-up, if there is one.

GROUNDING AND SCOPE - these are hard rules
  - Every number, name and percentage must come from a row a tool returned. If
    the data does not answer the question, say so. Never estimate, extrapolate
    or fill a gap from general knowledge.
  - Warehouse rows are this session's snapshot from start-of-conversation.
    Never add them to a later live pull as if they were the same query.
  - If a tool returns no rows, report that plainly. An empty result is a real
    finding, not a reason to guess.
  - The server scopes every query to the current user. Stay inside what it
    returns; do not imply you can see more than that.
  - Never expose internal identifiers (columns like Id*, *_id, RoleNumber,
    LevelNumber and the like). Use human-readable names.
  - Format dates, counts and percentages for a human reader.

FORMATTING
  Write plain markdown prose, with a small table only when a breakdown genuinely
  needs one. A separate presentation layer turns your answer into a chart or
  card when that helps, so never emit JSON, code blocks or chart definitions
  yourself, and never describe the widget you expect to appear. Just write the
  answer.\
"""


def _tool_args(tool: Any) -> List[str]:
    """Argument lines for one tool, from its schema.

    MCP adapters hand back a plain JSON schema dict; locally defined tools hand
    back a pydantic model. Both are read here so the block looks the same either
    way, and neither can disagree with the tool that is actually bound.
    """
    schema = getattr(tool, "args_schema", None)
    properties: dict = {}
    required: list = []
    if isinstance(schema, dict):
        properties = schema.get("properties") or {}
        required = list(schema.get("required") or [])
    else:
        fields = getattr(schema, "model_fields", None)
        if isinstance(fields, dict):
            for name, field in fields.items():
                properties[name] = {"description": getattr(field, "description", "") or ""}
                if getattr(field, "is_required", lambda: False)():
                    required.append(name)

    lines: List[str] = []
    for name, spec in properties.items():
        if name == "config":  # injected by LangChain, not something the model passes
            continue
        spec = spec if isinstance(spec, dict) else {}
        kind = spec.get("type") or ""
        # JSON Schema allows ``type`` to be a list — ``["string", "null"]`` is
        # how a nullable argument arrives from most MCP servers. Normalise to
        # one label so the line below is always string concatenation.
        if isinstance(kind, list):
            kind = "|".join(str(k) for k in kind if k)
        elif not isinstance(kind, str):
            kind = str(kind)
        detail = (spec.get("description") or "").strip().replace("\n", " ")
        flag = "required" if name in required else "optional"
        head = f"      {name} ({kind + ', ' if kind else ''}{flag})"
        lines.append(f"{head} - {detail}" if detail else head)
    return lines


def render_tools_block(tools: List[Any]) -> str:
    """Describe the bound tools using their own names and descriptions.

    Written from the live tool objects rather than kept in this file. A prompt
    that lists tool names by hand is a prompt that goes stale the moment the
    server renames one - and a model told about a tool that no longer exists
    starts inventing answers instead of failing.
    """
    if not tools:
        return "YOUR TOOLS\n\n  (none available this turn)"

    parts = ["YOUR TOOLS", ""]
    for tool in tools:
        parts.append(f"  {tool.name}")
        description = (getattr(tool, "description", "") or "").strip()
        for line in description.splitlines():
            line = line.strip()
            if line:
                parts.append(f"    {line}")
        args = _tool_args(tool)
        if args:
            parts.append("    Arguments:")
            parts.extend(args)
        parts.append("")
    return "\n".join(parts).rstrip()


def build_system_prompt(today: str, user_email: str = "", tools: List[Any] | None = None) -> str:
    """Render the system prompt for one turn."""
    context = (
        f"You are speaking with the user whose account is {user_email}." if user_email else ""
    )
    guidance = f"DOMAIN NOTES\n\n{GUIDANCE}" if GUIDANCE else ""
    return "\n\n".join(
        part
        for part in (
            PERSONA,
            render_tools_block(tools or []),
            guidance,
            RULES,
            f"Current date: {today}.",
            context,
        )
        if part
    )
