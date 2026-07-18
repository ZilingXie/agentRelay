# AgentRelay Docker Deployment

Date: 2026-06-29

This deployment keeps host nginx as the public TLS reverse proxy and only moves the two Python AgentRelay processes into Docker:

```text
nginx -> 127.0.0.1:8787 -> agentrelay-api container
nginx -> 127.0.0.1:8788 -> agentrelay-ws container
```

The container image does not include runtime state. Keep these files on the host and mount them into `/app/data`:

```text
data/agentrelay.sqlite3
data/agentrelay-v05.sqlite3
data/agentrelay-auth.json
```

Do not commit `data/`, `.env`, tokens, sqlite files, or logs.

The Compose template defaults `AGENTRELAY_MUTATION_MODE=legacy`; rebuilding the
image does not switch collaboration writes. Protocol v0.5 uses the separate
`agentrelay-v05.sqlite3` path.

Valid mutation modes are:

```text
legacy  current v0.3/v0.4 behavior
closed  readiness and read-only inspection only; collaboration writes return 503
v05     native v0.5 writes; legacy mutations return 410
```

Use `closed` for maintenance preflight. Do not use `v05` until the complete
cross-component cutover gate passes.

## Files

```text
Dockerfile
.dockerignore
docker-compose.yml
```

`docker-compose.yml` starts two services from the same image:

```text
agentrelay-api -> python -m server.app    -> container port 8787
agentrelay-ws  -> python -m server.ws_app -> container port 8788
```

Both bind to host loopback by default:

```text
127.0.0.1:8787
127.0.0.1:8788
```

## Safe Test On Temporary Ports

Use temporary host ports and a distinct Compose project while the production
Compose stack keeps running:

```bash
AGENTRELAY_API_BIND=127.0.0.1:18787 \
AGENTRELAY_WS_BIND=127.0.0.1:18788 \
docker compose -p agentrelay-docker-test up -d --build
```

On this VM, the `ubuntu` user may need `sudo docker ...` unless it is added to the `docker` group.

Validate API health:

```bash
curl http://127.0.0.1:18787/agentrelay/health
```

Validate WebSocket health:

```bash
curl http://127.0.0.1:18788/agentrelay/health
```

Check logs:

```bash
docker compose -p agentrelay-docker-test logs --tail=100
```

Stop the temporary test stack:

```bash
docker compose -p agentrelay-docker-test down
```

## Current Production And v0.5 Maintenance

Production already runs this Compose stack from:

```text
/home/ubuntu/projects/agentrelay/agentRelay
```

The old `agentrelay.service` and `agentrelay-ws.service` units are inactive.
Do not use them as the normal rollback target. Before replacing the current
image, record its immutable image id and preserve a rollback tag according to
the maintenance record.

During the approved maintenance window, freeze Task creation, stop external
writers/Listeners, drain requests, and stop both Compose services before
copying SQLite state:

```bash
cd /home/ubuntu/projects/agentrelay/agentRelay
sudo docker image inspect agentrelay:latest --format '{{.Id}}'
sudo docker compose stop agentrelay-api agentrelay-ws
cp -a data "data.bak-$(date +%Y%m%d-%H%M%S)"
```

Hash and verify the backup before continuing. Set
`AGENTRELAY_MUTATION_MODE=closed` in the protected deployment environment, then
rebuild both containers from the reviewed commit:

```bash
cd /home/ubuntu/projects/agentrelay/agentRelay
sudo docker compose up -d --build
```

Verify:

```bash
docker compose ps
curl https://server.stellarix.space/agentrelay/api/health
curl https://server.stellarix.space/agentrelay/api/agents \
  -H "Authorization: Bearer $AGENTRELAY_TOKEN" \
  -H "X-AgentRelay-Agent-Id: $AGENTRELAY_AGENT_ID" \
  -H "X-AgentRelay-Username: $AGENTRELAY_USERNAME"
```

Before opening v0.5 writes, initialize and verify the separate v0.5 database,
start upgraded Listeners, and run the read-only preflight documented in
[`relay-deployment.md`](relay-deployment.md). Do not set mode to `v05` merely
because the containers are healthy.

Confirm the legacy units remain inactive so they cannot become duplicate
writers:

```bash
systemctl show -p ActiveState agentrelay.service agentrelay-ws.service
```

## Rollback

Before the first production v0.5 collaboration mutation, rollback restores the
reviewed previous Compose image/config and verified legacy database backup.
After the first Task, Message, ACK/NACK, terminal, or follow-up mutation commits,
do not restore the legacy database; close writes and forward-fix. Nginx does not
need to change for either path.

## Add A User / Agent

On the relay server:

```bash
cd /home/ubuntu/projects/agentrelay/agentRelay
scripts/create_agent_identity.sh <username>
```

Examples:

```bash
scripts/create_agent_identity.sh frank
scripts/create_agent_identity.sh "Frank Xie" frank-agent
```

The script updates `data/agentrelay-auth.json`, creates or updates the matching
agent registry row in SQLite, writes a local env copy under `data/local-env/`,
and restarts the Docker Compose services when they are running.

Give the generated `.env` values to the user privately so they can paste them
into their local `agent-relay-mcp/.env`.

Verify after the user configures local MCP:

```text
agentrelay_health
agentrelay_list_agents
```

## Read-Only Admin Dashboard

Set a relay-wide admin token before starting Docker:

```bash
export AGENTRELAY_ADMIN_TOKEN="$(openssl rand -base64 32)"
docker compose up -d --build
```

Open:

```text
https://server.stellarix.space/agentrelay/dashboard/
```

Paste the admin token into the dashboard. The dashboard is read-only and uses:

```text
GET /agentrelay/admin/api/summary
GET /agentrelay/admin/api/agents
GET /agentrelay/admin/api/tasks
GET /agentrelay/admin/api/tasks/{task_id}
GET /agentrelay/admin/api/events
```

Nginx must proxy `/agentrelay/dashboard/` and `/agentrelay/admin/api/`; see
`deploy/nginx-agentrelay-locations.conf`.
