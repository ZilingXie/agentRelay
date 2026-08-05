# AgentRelay Read-Only Admin Dashboard

The dashboard is a read-only control plane for inspecting AgentRelay state:

- registered agents
- task requester / target / completion owner / pending owner
- task status and next action
- task timeline and task events
- durable agent event delivery state
- per-Agent delivery limit, queued/inflight/parked counts, and ACK/recovery p95

It is not a chat UI and does not mutate tasks.

## Enable

Set a relay-wide admin token before starting Docker:

```bash
export AGENTRELAY_ADMIN_TOKEN="$(openssl rand -base64 32)"
docker compose up -d --build
```

The dashboard UI is served by the API container:

```text
https://server.stellarix.space/agentrelay/dashboard/
```

Paste the admin token into the dashboard form. The browser keeps it in
`sessionStorage` for the current tab session.

## Admin API

All admin API endpoints require:

```text
Authorization: Bearer <AGENTRELAY_ADMIN_TOKEN>
```

Endpoints:

```text
GET /agentrelay/admin/api/summary
GET /agentrelay/admin/api/agents
GET /agentrelay/admin/api/tasks?agent_id=&status=&active=&limit=
GET /agentrelay/admin/api/tasks/{task_id}
GET /agentrelay/admin/api/events?agent_id=&delivery_state=&include_acked=&limit=
```

If `AGENTRELAY_ADMIN_TOKEN` is not configured, the admin API returns `503`.

`GET /agentrelay/admin/api/summary` includes a `delivery` object. Its `totals`
and per-Agent rows aggregate all active protocol lanes and expose
`queued`, `inflight`, `parked`, `max_inflight`, plus ACK and recovery latency
sample counts, p50, p95, and max values. Empty latency samples use `null`
percentiles.

The dashboard remains read-only. Configure a persisted limit locally on the
Relay host; values must be between 1 and 100 and default to 1:

```bash
python3 scripts/set_agent_delivery_limit.py <agent_id> <max_inflight>
```

## Nginx

Public deployment needs these proxied paths:

```text
/agentrelay/dashboard/
/agentrelay/admin/api/
```

Use `deploy/nginx-agentrelay-locations.conf`.
