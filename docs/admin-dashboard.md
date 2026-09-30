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

## Listener Transport Status

`GET /agentrelay/admin/api/agents` rows for durable lanes additionally expose
the split listener status model:

```text
transport_state        connected / disconnected from agent_listener_transport
transport_connected    boolean mirror of transport_state
transport_online       transport session alive AND last Pong within the pong timeout
listener_ready         readiness ready flag (local processing capability)
readiness_fresh        readiness observed within LISTENER_READINESS_MAX_AGE_SECONDS
can_receive_push       transport_online AND listener_ready AND readiness_fresh
status                 online_ready | online_not_ready | offline_with_pending
                       | offline_idle | unknown
connected_at / last_pong_at / disconnected_at / disconnect_reason
pending_event_count / inflight_event_count
```

`last_pong_at` (transport keep-alive) and `observed_at` (readiness) are
independent clocks and are shown separately; transport online status never
implies delivery success — only an ACK does.

Agents can read their own combined status with a worker token via:

```text
GET /agentrelay/workers/{agent_id}/status?protocol_version=agent-collab-v0.6
```

The endpoint is read-only and returns the same combined fields plus
`generated_at`; it never changes Event delivery state. The dashboard Agents
table shows the combined `status` badge, last Pong, last readiness, disconnect
reason, and pending/inflight counts.

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
