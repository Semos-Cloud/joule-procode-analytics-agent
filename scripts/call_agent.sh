#!/usr/bin/env bash
# Send one message to the agent over A2A, exactly the way Joule does.
#
#   ./scripts/call_agent.sh "Which of my team members moved most on engagement last quarter?"
#
# Prints the raw JSON-RPC response. Look for two artifacts:
#   artifacts[0] -> "response", the markdown narration
#   artifacts[1] -> "ui", the structured contract (present when a widget applies)
#
# GATEWAY overrides the host, e.g. GATEWAY=https://<tunnel> ./scripts/call_agent.sh "..."

set -euo pipefail

GATEWAY="${GATEWAY:-http://localhost:9000}"
AGENT_PATH="${AGENT_PATH:-analytics-agent}"
MESSAGE="${1:-Which of my team members moved most on engagement last quarter?}"

# Joule sends a conversationid so turns land on the same thread. Reuse one value
# across calls to continue a conversation.
CONVERSATION_ID="${CONVERSATION_ID:-workshop-demo-1}"

echo "POST ${GATEWAY}/${AGENT_PATH}"
echo "  conversationid: ${CONVERSATION_ID}"
echo "  message: ${MESSAGE}"
echo

curl -sS -X POST "${GATEWAY}/${AGENT_PATH}" \
  -H "Content-Type: application/json" \
  -H "conversationid: ${CONVERSATION_ID}" \
  -d "$(cat <<JSON
{
  "jsonrpc": "2.0",
  "id": "1",
  "method": "message/send",
  "params": {
    "message": {
      "role": "user",
      "parts": [{ "kind": "text", "text": $(printf '%s' "$MESSAGE" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))') }],
      "messageId": "msg-$(date +%s)"
    }
  }
}
JSON
)" | python3 -m json.tool
