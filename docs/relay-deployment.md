# AgentRelay Cloud Deployment

Date: 2026-06-25

## Runtime

Current production runs the Docker Compose stack from
`/home/ubuntu/projects/agentrelay/agentRelay`, as documented in
[`docker-deployment.md`](docker-deployment.md). Host nginx terminates TLS and
proxies to the two loopback-bound containers. The old systemd units remain
installed but inactive and are not the current runtime.

```bash
cd /home/ubuntu/projects/agentrelay/agentRelay
sudo docker compose ps
sudo docker compose logs --tail=100 agentrelay-api agentrelay-ws
```

## Inactive Legacy systemd Runtime

These commands describe the retained legacy units, not current production:

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
AGENTRELAY_V05_DB_PATH=/home/ubuntu/agentRelay/data/agentrelay-v05.sqlite3
AGENTRELAY_MUTATION_MODE=legacy
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

## WebSocket Notify Container

The `agentrelay-ws` Compose service streams durable `agent_events` rows to
authenticated local listeners. REST runs in the `agentrelay-api` Compose
service.

Runtime:

```bash
cd /home/ubuntu/projects/agentrelay/agentRelay
sudo docker compose ps agentrelay-ws
sudo docker compose logs -f agentrelay-ws
sudo docker compose restart agentrelay-ws
```

The following unit file is retained only for legacy rollback reference:

```text
/etc/systemd/system/agentrelay-ws.service
```

Important settings:

```text
WorkingDirectory=/home/ubuntu/agentRelay
AGENTRELAY_WS_HOST=127.0.0.1
AGENTRELAY_WS_PORT=8788
AGENTRELAY_DB_PATH=/home/ubuntu/agentRelay/data/agentrelay.sqlite3
AGENTRELAY_V05_DB_PATH=/home/ubuntu/agentRelay/data/agentrelay-v05.sqlite3
AGENTRELAY_MUTATION_MODE=legacy
AGENTRELAY_AUTH_FILE=/home/ubuntu/agentRelay/data/agentrelay-auth.json
```

Protocol v0.5 WebSocket connections additionally carry
`listener_instance_id` and `readiness_epoch` query parameters. In `closed` or
`v05` mode the sidecar runs the persisted due-Event coordinator; an offline
Listener consumes the fixed attempt schedule instead of leaving Events queued.

Changing `AGENTRELAY_MUTATION_MODE` is a maintenance-runbook action, not a
dashboard control. Keep `legacy` until core implementation and Client rehearsal
are complete, use `closed` for preflight, and switch to `v05` only after every
enabled Listener passes capability/readiness admission.

Run the read-only preflight after the Server is deployed in `closed` mode and
the upgraded Listeners have published fresh readiness:

```bash
python3 scripts/protocol_v05_preflight.py \
  --base-url https://server.stellarix.space/agentrelay/api \
  --legacy-db /home/ubuntu/projects/agentrelay/agentRelay/data/agentrelay.sqlite3 \
  --v05-db /home/ubuntu/projects/agentrelay/agentRelay/data/agentrelay-v05.sqlite3 \
  --retirement-report /path/to/retirement-report.json
```

The command reads the admin token only from `AGENTRELAY_ADMIN_TOKEN`, performs
GET requests only, opens the legacy database read-only, and fails unless the
Server is publishing v0.5 in `closed` mode, the native collaboration tables are
empty, the retirement report is exact, and every enabled Agent has fresh v0.5
WebSocket/workspace-v2 readiness. After writes open, use
`--expected-mode v05 --allow-existing-collaboration` for a read-only recheck.

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
/home/ubuntu/projects/agentrelay/agentRelay/data/agentrelay-auth.json
```

The file is intentionally not committed. Keep mode `0600`.

Create or replace an identity from username only:

```bash
scripts/create_agent_identity.sh zac
```

This updates `data/agentrelay-auth.json`, creates or updates the matching agent registry row, writes `data/local-env/zac.env` for local `.env` copy/paste, and restarts the running relay when Docker Compose or legacy systemd services are detected.

Create a user with an explicit agent id:

```bash
scripts/create_agent_identity.sh "Zac Xie" zac-xie-agent
```

For the current Docker deployment, the equivalent manual restart is:

```bash
docker compose restart agentrelay-api agentrelay-ws
```

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

See `docs/relay-auth.md` for account creation, token rotation, and syncing existing identities into the agent registry.
