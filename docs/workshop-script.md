# Workshop script — the SAP layer

**~20 minutes. Five stops. The Joule files are the payoff, so budget half the time for stops 4–5.**

---

## Stop 0 — Frame it (1 min, no code)

> "Most agent demos stop at conversation. We're going to build one that reads real transaction data and hands a manager a **chart inside Joule** — in the flow of work. Everything runs on SAP Business AI Platform: Gen AI Hub for the model, MCP for data, A2A for invocation, Joule for delivery. LangGraph is just the agent framework."

Then set the hook you'll pay off at stop 4:

> "There's one thing about Joule that breaks the obvious design. I'll show you what it is and how we got around it."

---

## Stop 1 — Gen AI Hub is the whole model config

**File: [src/config.py](../src/config.py) — 30 seconds, don't linger**

Point at `init_llm(name, proxy_client=...)`.

> "There's no endpoint URL here. Gen AI Hub resolves a model *name* against the deployments in your AI Core resource group — the five `AICORE_*` service-key values are the entire configuration. Swapping models is a string. Swapping providers is this one file."

**The war story** (this is what they'll remember): `AICORE_CLIENT_SECRET` contains a `$`. Both dotenv interpolation and shell `source` silently truncate it, and the SDK reports it as *"Could not retrieve Authorization token."*

> "That message is a lie. It's not an auth problem, it's a `$`. Lines 27–33 exist entirely to work around it."

---

## Stop 2 — MCP is the security boundary

**File: [src/mcp_client.py](../src/mcp_client.py) — 3 min**

Show line 132:

```python
"headers": {MCP_USER_HEADER: effective_email} if effective_email else {}
```

> "The agent has no database driver. No SQL against the system of record. The manager's email goes in the header, and the **server** scopes every query to that person's team. The agent can't ask for someone else's data because it never writes the query. This is why tools are loaded per request — identity is part of the connection."

Then the allowlist at line 148:

> "The scope exposes ten tools. We give it three. Reach it doesn't need is surface it can get wrong."

Show that a missing tool **raises at startup**:

> "An agent quietly missing its data tool starts guessing. One that won't start is easier to debug at 9am in front of a room. This was a real bug — an allowlist and a prompt disagreed about whether a tool was called `get_award_reasons_data` or `get_monetary_and_nonMonetary_award_reasons_data`."

Run it live — this is the single best de-risking move you can show:

```bash
.venv/bin/python scripts/probe_mcp.py
```

> "The probe connects exactly the way the agent does. If it lists your tools, the agent can reach them. Run this before every session."

---

## Stop 3 — The Agent Card is how Joule finds you

**File: [src/gateway/cards.py](../src/gateway/cards.py) — 2 min**

> "A2A agents advertise themselves at `/.well-known/agent.json`. Joule's orchestrator fetches that document and reads the skills."

Point at `examples`:

> "These aren't documentation. Joule uses them for routing. When you change domains, rewrite these first."

Then the `url` field:

> "This must be the address Joule can **actually reach** — your tunnel or your CF route, never localhost. And note: cards.py reads `A2A_GATEWAY_BASE_URL` at **import time**. Set it after the app is running and your card advertises localhost to Joule until the next restage. That's why [cf_deploy.sh](../scripts/cf_deploy.sh) pushes with `--no-start` first."

---

## Stop 4 — The constraint, and the design that survives it ⭐

**Files: [src/ui_contract.py](../src/ui_contract.py), [src/agent/ui_synth.py](../src/agent/ui_synth.py) — 5 min. This is the centerpiece.**

State the constraint plainly, then pause:

> "Joule's capability YAML substitutes values with SpEL — angle-bracket question-mark. **SpEL returns strings.** You cannot loop an array into chart rows or buttons from YAML. So every design where the agent 'describes a chart in its answer' dies right here."

Now the resolution:

> "We stopped treating narration and structure as one channel. The agent writes **prose and nothing else** — it's explicitly told a presentation layer exists, so it never emits JSON into the chat. Then a second, forced LLM call looks at the *finished* turn and emits a small typed payload: what widget should accompany this."

Show the graph edge in [graph.py:145](../src/agent/graph.py#L145) — `agent → ui_synth → END`.

Two consequences, say them as the takeaway:

1. **The answer streams immediately.** Structure is decided afterwards and never blocks the first token.
2. **It degrades safely.** `text` is always present. A malformed payload falls back to text instead of reaching Joule half-built.

> "And the payload is data, never markup. Same agent can feed Joule, a web app, or anything else — one adapter per client."

**If you have time, one guard** — pick the offer guard, it gets a laugh:

> "The agent ends its answer with 'would you like me to chart this?' — and the synth renders the chart. So the answer appears next to its own question. We also had to *remove the manager's original question* from what the synth sees, because including it primed the synth to render whatever was asked for even when the agent had only offered."

---

## Stop 5 — The Joule Skill ⭐

**Folder: [joule/team_analytics_capability/](../joule/team_analytics_capability/) — 6 min. Walk all four files in order.**

### 5a. [capability.sapdas.yaml](../joule/team_analytics_capability/capability.sapdas.yaml) — 30 sec

Point at `system_aliases`:

> "`ANALYTICS_AGENT` is a logical name bound to a BTP destination. That indirection is why moving the gateway from ngrok to Cloud Foundry is a **destination edit in the cockpit** — not a redeploy of the capability."

### 5b. [scenarios/analytics_scenario.yaml](../joule/team_analytics_capability/scenarios/analytics_scenario.yaml) — 1 min

> "This is the routing layer. Joule compares the user's utterance against this description, so it's written for a matcher, not for a person."

Read the last sentence out loud:

> "Notice it says what is **out** of scope: don't use this to send, draft or nominate a recognition — those are writes, and they belong to a different agent. Negative examples do real work in intent matching."

### 5c. [functions/analytics_function.yaml](../joule/team_analytics_capability/functions/analytics_function.yaml) — 4 min, the payoff

Walk the five action groups top to bottom:

**Group 1** — `agent-request` through the alias. One line, that's the whole invocation.

**Group 2** — always show the narration:

```
apiResponse.body.artifacts[0].parts[0].text
```

> "Artifact zero is always there. Everything after this is additive."

**Group 3** — the pre-baked passthrough. This is the design paying off:

> "For card, list, choice and form, the gateway already built a `ui5integrationCard` **in Python** — `build_joule_manifest`. So the YAML is a one-line passthrough instead of fragile Handlebars. Deterministic, and unit-testable."

Then the bracket-notation gotcha — this one will save someone a day:

> "Note `data['manifest']`, not `data.manifest`. Chart payloads have no `manifest` key, and SpEL on a Java Map throws **EL1008E** for a missing property instead of returning null. That gets logged as `ACTION_GROUP_FAILED` and Joule still emits a '(no response from agent)' bubble on a turn that worked perfectly. Bracket notation is `Map.get` — evaluates to false and skips cleanly."

**Group 5** — the chart, and why it's different:

> "The contract carries chart data **wide** — one row per person, a column per measure. A UI5 Analytical card wants it **long** — one row per category-series pair — so a single static binding can drive any number of series."

Point at the nested `eachJoin`:

> "That's the unpivot. We do it *here* rather than in the agent, because a web app consuming the same DataPart wants the wide form. Keeping the contract client-neutral is the whole point. And because of the unpivot, the manifest below uses only fixed bindings — `{label}`, `{series}`, `{value}` — never a dynamic key. That's what makes it work under SpEL at all."

### 5d. The destination (say it, don't show it) — 30 sec

| Setting | Value |
|---|---|
| Name | `ANALYTICS_AGENT` |
| URL | `https://<host>/analytics-agent` |

> "The path must match `AGENT_PATH` **exactly** — a mismatch is a bare 404 with nothing in the Joule log to explain it. That's the one thing to get right here."

---

## Close — the worked example (2 min)

Run it live, then narrate what just happened:

> *"Which of my team members moved most on engagement last quarter?"*
>
> 1. Joule matches the scenario, invokes over A2A.
> 2. The agent resolves "last quarter" to explicit bounds itself — never asks the user to restate a date.
> 3. Calls the team reach tool, scoped by the server to **this** manager's team.
> 4. Ranks the rows, writes a short answer.
> 5. `ui_synth` sees numbers worth plotting, emits `render: chart`.
> 6. Gateway ships narration + DataPart. Joule renders markdown, then the Analytical card.

Final line:

> "Four boundaries, and each one is doing real work. The MCP server owns identity. The agent owns reasoning. The contract owns structure. Joule owns presentation. Nothing in the contract, the gateway, or the Joule files knows anything about recognition data — point it at your own MCP server and it's three environment variables and one paragraph of English."

---

## Only if asked

### "How does it know *which* manager is asking?"

Expect this after stop 2. It's the sharpest question in the room, and the answer is short and confident — don't get drawn into the demo's plumbing.

> "Two layers, and they're separate on purpose.
>
> **The scoping mechanism** is what you're looking at: identity travels in the request header, and the MCP server enforces it. The agent never writes the query, so it structurally cannot reach another manager's team. That part is real and it's what I'd want you to take away.
>
> **Where that identity comes from** is an identity-federation problem, and it's deliberately not in today's scope — today the agent runs as a configured user so we can focus on the agent itself. In our production A2A gateway it's fully solved: the signed-in user's token is carried through IAS app-to-app exchange, verified at the gateway against a per-issuer trust registry, and the verified principal is forwarded downstream. No shared service account anywhere in the chain.
>
> The code in front of you already reads a bearer token if one arrives — that's the same gateway line that logs the identity on every turn. Wiring it up is destination configuration, not an agent change."

**Then stop.** If they push for detail, offer the handbook rather than improvising:

> "There's an implementation handbook for it — happy to share it after. Short version: you can't hang dependencies off the Joule IAS app because it's SAP-managed, so you put a proxy app you own in between and do two exchanges."

**Do not** say the demo is using it. If someone has seen the logs, own it plainly: *"Correct — auth is off on that destination today, by design. That's exactly why the gateway logs which source the identity came from."*

### "Why not just let the LLM emit the card JSON?"

> Non-deterministic, untestable, and it leaks into the chat when it goes wrong. Building it in Python makes it unit-testable ([tests/test_ui_contract.py](../tests/test_ui_contract.py)).

### "Can this scale?"

> Not in this shape, on purpose. `instances: 1`, `--workers 1` — the checkpointer is in memory and the DuckDB catalogs are keyed by thread id in a module global. A second replica strands managers mid-conversation. That's the right trade for a workshop and the wrong one for production; production is the `docker-compose.yaml` shape with Postgres behind the checkpointer.

---

## Pre-flight, day of

```bash
.venv/bin/python scripts/probe_mcp.py      # tools list clean
curl localhost:9000/health                 # check "runtime" and "url"
./scripts/call_agent.sh "..."              # WARM IT — the first call is always slow
```

The Gen AI Hub proxy client and the MCP SSE handshake are both lazy. Never let the first call of the day happen in front of the room.
