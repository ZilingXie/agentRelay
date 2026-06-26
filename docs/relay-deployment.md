# AgentRelay Cloud Deployment

Date: 2026-06-25

## Runtime

AgentRelay runs as a systemd service on the VM:

```bash
sudo systemctl status agentrelay
sudo journalctl -u agentrelay -f
sudo systemctl restart agentrelay
```

Service file:

```text
/etc/systemd/system/agentrelay.service
```

Important settings:

```text
WorkingDirectory=/home/ubuntu/agentRelay
AGENTRELAY_HOST=127.0.0.1
AGENTRELAY_PORT=8787
AGENTRELAY_DB_PATH=/home/ubuntu/agentRelay/data/agentrelay.sqlite3
AGENTRELAY_AUTH_FILE=/home/ubuntu/agentRelay/data/agentrelay-auth.json
```

## HTTPS public API

Nginx exposes the local relay through:

```text
https://server.stellarix.space/agentrelay/api
```

Nginx snippet:

```text
/etc/nginx/snippets/agentrelay-locations.conf
```

It is included from:

```text
/etc/nginx/sites-available/default
```

The reverse proxy maps:

```text
/agentrelay/api/* -> http://127.0.0.1:8787/agentrelay/*
```

## WebSocket notify sidecar

Phase 2 adds a separate WebSocket notify sidecar. REST remains on `agentrelay.service`; the sidecar only streams durable `agent_events` rows to authenticated local listeners.

Runtime:

```bash
sudo systemctl status agentrelay-ws
sudo journalctl -u agentrelay-ws -f
sudo systemctl restart agentrelay-ws
```

Service file:

```text
/etc/systemd/system/agentrelay-ws.service
```

Important settings:

```text
WorkingDirectory=/home/ubuntu/agentRelay
AGENTRELAY_WS_HOST=127.0.0.1
AGENTRELAY_WS_PORT=8788
AGENTRELAY_DB_PATH=/home/ubuntu/agentRelay/data/agentrelay.sqlite3
AGENTRELAY_AUTH_FILE=/home/ubuntu/agentRelay/data/agentrelay-auth.json
```

Public WSS endpoint:

```text
wss://server.stellarix.space/agentrelay/api/workers/<agent_id>/events/ws
```

The nginx snippet routes only this WebSocket path to `127.0.0.1:8788`; all other `/agentrelay/api/*` traffic still goes to REST on `127.0.0.1:8787`.

WebSocket clients must send the same auth headers as REST clients:

```text
Authorization: Bearer <AGENTRELAY_TOKEN>
X-AgentRelay-Agent-Id: <agent_id>
X-AgentRelay-Username: <username>
```

The sidecar sends `hello`, `task.pending`, and `heartbeat` JSON text frames. It does not mark events acked; clients still ack through REST:

```text
POST /agentrelay/api/workers/<agent_id>/events/<event_id>/ack
```

## Auth file

Current token file:

```text
/home/ubuntu/agentRelay/data/agentrelay-auth.json
```

The file is intentionally not committed. Keep mode `0600`.

Create or replace an identity from username only:

```bash
scripts/create_agent_identity.sh zac
```

This updates `data/agentrelay-auth.json` and writes `data/local-env/zac.env` for local `.env` copy/paste.

Generate a token without writing it to the active auth file:

```bash
python3 scripts/generate_agent_token.py zac
```

Default `agent_id` is derived as:

```text
<normalized-username>-agent
```

For example:

```text
zac -> zac-agent
Zac Xie -> zac-xie-agent
```

## Validation

Health is public:

```bash
curl https://server.stellarix.space/agentrelay/api/health
```

Authenticated endpoint:

```bash
curl \
  -H "Authorization: Bearer $AGENTRELAY_TOKEN" \
  -H "X-AgentRelay-Agent-Id: $AGENTRELAY_AGENT_ID" \
  -H "X-AgentRelay-Username: $AGENTRELAY_USERNAME" \
  https://server.stellarix.space/agentrelay/api/agents
```

Account creation note: `scripts/create_agent_identity.sh <username>` creates or replaces the auth token and automatically creates/updates the matching agent registry row, so the agent appears in `agentrelay_list_agents`.

If identities already exist but agents are missing from `agentrelay_list_agents`, run `python3 scripts/sync_agents_from_auth.py` and restart `agentrelay`. This does not rotate or print tokens.
