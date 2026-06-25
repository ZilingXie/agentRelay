# AgentRelay Phase 1 Auth

Date: 2026-06-25

## Model

The cloud relay issues one identity per local agent:

```text
username: zac
agent_id: zac-agent
token: generated-secret-token
```

The local MCP client stores these values in its private `.env` file and sends:

```text
Authorization: Bearer <token>
X-AgentRelay-Agent-Id: <agent_id>
X-AgentRelay-Username: <username>
```

## Generate a token

```bash
python3 scripts/generate_agent_token.py --username zac --agent-id zac-agent
python3 scripts/generate_agent_token.py --username frank --agent-id frank-agent
```

## Configure the relay

For quick Phase 1 deployment, set `AGENTRELAY_TOKENS`:

```bash
export AGENTRELAY_TOKENS='zac:zac-agent:ZAC_TOKEN,frank:frank-agent:FRANK_TOKEN'
```

Or use a JSON file:

```json
[
  {"username": "zac", "agent_id": "zac-agent", "token": "ZAC_TOKEN"},
  {"username": "frank", "agent_id": "frank-agent", "token": "FRANK_TOKEN"}
]
```

Then start the relay with:

```bash
AGENTRELAY_AUTH_FILE=./data/agentrelay-auth.json \
AGENTRELAY_DB_PATH=./data/agentrelay.sqlite3 \
python3 -m server.app
```

If no `AGENTRELAY_TOKENS` or `AGENTRELAY_AUTH_FILE` is configured, auth is disabled for local smoke tests.

## Boundary enforcement

When auth is enabled:

- health endpoints remain public
- listing agents and reading tasks require a valid token
- `zac-agent` can create tasks only with `from=zac-agent`
- `frank-agent` can claim only `/workers/frank-agent/claim`
- artifacts, deliveries, thread mapping, and close calls must act as the authenticated agent

## API prefix

The server accepts both local and public prefixes:

```text
/agentrelay/health
/agentrelay/api/health
```

This allows nginx to expose the authenticated relay as:

```text
https://server.stellarix.space/agentrelay/api
```
