#!/usr/bin/env bash
# Deploy the agent to Cloud Foundry as a single app.
#
#   ./scripts/cf_deploy.sh
#
# Why not just `cf push`: two things must be in the environment *before* uvicorn
# imports the app.
#
#   1. src/gateway/cards.py reads A2A_GATEWAY_BASE_URL at import time. Set it
#      afterwards and the Agent Card advertises localhost to Joule until the
#      next restage.
#   2. The AICORE_* credentials are secrets, so they are not in manifest.yml.
#
# So: push --no-start, set the environment, then start.

set -euo pipefail

cd "$(dirname "$0")/.."

APP_NAME="${APP_NAME:-joule-analytics-agent}"

# Keep the route in manifest.yml as the single source of truth.
ROUTE="$(grep -E '^\s*- route:' manifest.yml | head -1 | sed 's/.*route:[[:space:]]*//')"
if [[ -z "$ROUTE" ]]; then
  echo "Could not read the route from manifest.yml" >&2
  exit 1
fi

# Values that come from .env. AICORE_* are the secrets; the rest are per-tenant
# settings that would be noise in a committed manifest. Anything not listed here
# is either in manifest.yml or belongs only to the local two-process setup
# (LANGGRAPH_API_URL, DATABASE_URI, REDIS_URI).
ENV_KEYS=(
  AICORE_CLIENT_ID
  AICORE_CLIENT_SECRET
  AICORE_AUTH_URL
  AICORE_BASE_URL
  AICORE_RESOURCE_GROUP
  MCP_BASE_URL
  MCP_SCOPE
  MCP_USER_EMAIL
  DEV_FALLBACK_USER_EMAIL
  AGENT_MODEL
  UI_SYNTH_MODEL
  MAX_TOKENS
)

cf target >/dev/null 2>&1 || { echo "Not logged in. Run: cf login" >&2; exit 1; }

if [[ ! -f requirements.txt ]]; then
  echo "==> requirements.txt is missing. Compiling from requirements.in"
  uv pip compile requirements.in -o requirements.txt
fi

echo "==> Pushing $APP_NAME (not starting yet)"
cf push "$APP_NAME" -f manifest.yml --no-start

echo "==> Setting environment"

# The Agent Card must advertise the URL Joule actually calls.
cf set-env "$APP_NAME" A2A_GATEWAY_BASE_URL "https://${ROUTE}" >/dev/null
echo "    A2A_GATEWAY_BASE_URL = https://${ROUTE}"

if [[ -f .env ]]; then
  # Parsed here rather than sourced. AICORE_CLIENT_SECRET contains `$`, and
  # `source`-ing .env would let the shell expand it into an empty string — the
  # same truncation src/config.py works around on the dotenv side. Nothing below
  # is ever evaluated.
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "${line// }" || "${line:0:1}" == "#" ]] && continue
    [[ "$line" != *"="* ]] && continue

    key="${line%%=*}"
    val="${line#*=}"
    key="${key#export }"
    key="${key// }"

    # Strip one layer of surrounding quotes if present.
    if [[ "${val:0:1}" == '"' && "${val: -1}" == '"' ]]; then val="${val:1:${#val}-2}"
    elif [[ "${val:0:1}" == "'" && "${val: -1}" == "'" ]]; then val="${val:1:${#val}-2}"
    fi

    [[ -z "$val" ]] && continue

    for wanted in "${ENV_KEYS[@]}"; do
      if [[ "$key" == "$wanted" ]]; then
        cf set-env "$APP_NAME" "$key" "$val" >/dev/null
        if [[ "$key" == *SECRET* ]]; then
          echo "    $key = (${#val} chars)"
        else
          echo "    $key = $val"
        fi
        break
      fi
    done
  done < .env
else
  echo "    WARNING: no .env found — AI Core credentials are not set." >&2
fi

echo "==> Starting $APP_NAME"
cf start "$APP_NAME"

echo
echo "==> Verifying"
curl -fsS "https://${ROUTE}/health" && echo
echo
echo "Agent Card:  https://${ROUTE}/analytics-agent/.well-known/agent.json"
echo "Smoke test:  GATEWAY=https://${ROUTE} ./scripts/call_agent.sh \"Which of my team members moved most on engagement last quarter?\""
echo
echo "BTP destination ANALYTICS_AGENT -> https://${ROUTE}/analytics-agent"
