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
data/agentrelay-auth.json
```

Do not commit `data/`, `.env`, tokens, sqlite files, or logs.

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

Use temporary host ports first so the current systemd services can keep running:

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

## Production Cutover

Back up runtime data:

```bash
cp -a data "data.bak-$(date +%Y%m%d-%H%M%S)"
```

Stop the current systemd services:

```bash
sudo systemctl stop agentrelay agentrelay-ws
```

Start Docker on the production loopback ports:

```bash
docker compose up -d --build
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

If production is stable, disable the old services but keep the unit files for rollback:

```bash
sudo systemctl disable agentrelay agentrelay-ws
```

## Rollback

```bash
docker compose down
sudo systemctl start agentrelay agentrelay-ws
```

Nginx does not need to change for either cutover or rollback.

## Add A User / Agent

On the relay server:

```bash
cd /home/ubuntu/agentRelay
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
