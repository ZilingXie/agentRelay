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

## Create or replace an identity

Use `scripts/create_agent_identity.sh` when you want to create a new user/agent or rotate an existing user's token.

For a new third-party agent integration, prefer the conformance-gated flow in
`docs/third-party-agent-onboarding.md`. The quick identity helper is best for
trusted existing users and token rotation.

```bash
cd /home/ubuntu/agentRelay
scripts/create_agent_identity.sh <username>
```

Examples:

```bash
scripts/create_agent_identity.sh zac
scripts/create_agent_identity.sh "Zac Xie" zac-xie-agent
```

The command:

- creates or replaces the auth identity in `data/agentrelay-auth.json`
- creates or updates the matching row in the `agents` registry table
- writes a local-copy env file under `data/local-env/<username>.env`
- restarts the running relay when Docker Compose or legacy systemd services are detected

You only need the username. If `agent_id` is omitted, it is derived from the username:

```text
zac -> zac-agent
Zac Xie -> zac-xie-agent
```

Copy the generated env values into the user's local `agent-relay-mcp/.env` privately:

```text
AGENTRELAY_BASE_URL=https://server.stellarix.space/agentrelay/api
AGENTRELAY_AGENT_ID=<agent_id>
AGENTRELAY_USERNAME=<username>
AGENTRELAY_TOKEN=<token>
```

Do not paste tokens into chats, commits, screenshots, or logs.

Use `generate_agent_token.py` only when you want to print a token without writing it to the active relay auth file:

```bash
python3 scripts/generate_agent_token.py zac
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

If identities already exist but agents are missing from `agentrelay_list_agents`, run:

```bash
python3 scripts/sync_agents_from_auth.py
docker compose restart agentrelay-api agentrelay-ws
```

This creates missing agent registry rows without rotating or printing tokens.
