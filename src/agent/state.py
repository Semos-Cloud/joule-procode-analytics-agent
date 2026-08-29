"""Conversation state for the team analytics agent.

Two channels, deliberately separate:

``messages``
    The conversation. Prose the user reads, streamed token by token.
``ui``
    The structured widget for the current turn, written once by the synth node
    at the end. Clients read it off the ``updates`` stream.

Keeping them apart is what lets the answer stream immediately while the chart is
decided afterwards, from the finished turn.
"""

from typing import Annotated, Any, Dict, List

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from typing_extensions import NotRequired, TypedDict


class AgentState(TypedDict):
    """State of one analytics conversation."""

    messages: Annotated[List[AnyMessage], add_messages]
    ui: NotRequired[Dict[str, Any]]
