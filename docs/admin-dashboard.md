# AgentRelay Read-Only Admin Dashboard

The dashboard is a read-only control plane for inspecting AgentRelay state:

- registered agents
- task requester / target / completion owner / pending owner
- task status and next action
- task timeline and task events
- durable agent event delivery state

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

## Nginx

Public deployment needs these proxied paths:

```text
/agentrelay/dashboard/
/agentrelay/admin/api/
```

Use `deploy/nginx-agentrelay-locations.conf`.
