"""The Agent Card — how Joule discovers what this agent can do.

A2A agents advertise themselves at ``/.well-known/agent.json``. Joule's
orchestrator fetches that document, reads the skills, and uses them to decide
when this agent is the right one to invoke.

``examples`` earn their keep: they are the clearest signal of what the agent
handles, both to Joule and to a human reading the card.

The ``url`` must be the address Joule can actually reach. During the workshop
that is the tunnel, not localhost, which is why it comes from the environment.
"""

import os

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

GATEWAY_BASE_URL = os.environ.get("A2A_GATEWAY_BASE_URL", "http://localhost:9000")

AGENT_PATH = "team-analytics-agent"

AGENT_CARD = AgentCard(
    name="Team Analytics Agent",
    description=(
        "Answers a manager's questions about how their own team is participating in "
        "recognition: engagement index per person, who is giving and receiving "
        "recognition, and which award reasons the team uses. Read-only."
    ),
    url=f"{GATEWAY_BASE_URL}/{AGENT_PATH}/",
    version="1.0.0",
    default_input_modes=["text", "text/plain"],
    default_output_modes=["text", "text/plain"],
    capabilities=AgentCapabilities(streaming=True, push_notifications=False),
    skills=[
        AgentSkill(
            id="team-engagement-analytics",
            name="Team Engagement Analytics",
            description=(
                "Per-member recognition reach and engagement index for the manager's "
                "team, over any period. Ranks people, spots who is not participating, "
                "and compares recognitions given versus received."
            ),
            tags=["analytics", "engagement", "recognition", "team", "manager"],
            examples=[
                "Which of my team members moved most on engagement last quarter?",
                "Who on my team has received the most recognitions this year?",
                "Is anyone on my team not participating in recognition?",
                "Show me engagement index by team member for the last six months.",
            ],
        ),
        AgentSkill(
            id="award-reason-breakdown",
            name="Award Reason Breakdown",
            description=(
                "Recognition counts grouped by award reason for the whole team, "
                "including the monetary versus non-monetary mix."
            ),
            tags=["analytics", "recognition", "award reasons", "team"],
            examples=[
                "What are my team most often recognised for?",
                "Break down our recognitions by award reason last quarter.",
                "What is our monetary versus non-monetary mix?",
            ],
        ),
    ],
)
