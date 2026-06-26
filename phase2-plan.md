# AgentRelay Phase 2 Plan: WebSocket Notify Push

GitHub repository: https://github.com/ZilingXie/agentRelay

## 0. Decision

Phase 2 keeps the existing manual fetch / HTTP claim flow and adds WebSocket notify as an incremental capability.

Manual flow remains supported:

```text
Codex/MCP or local listener -> GET /workers/:agentId/pending or claim APIs
```

New push flow adds:

```text
Local listener -> outbound WSS connection -> AgentRelay Cloud
AgentRelay Cloud -> task.pending notify -> local listener
```

AgentRelay Cloud is responsible for reliable notification and task state. The local listener and Codex App thread creation/reuse are local client responsibilities and are intentionally outside the cloud relay's semantic logic.

## 1. Goal

Replace cron-style polling with a realtime notification channel while preserving pull-based recovery.

Target Phase 2 path:

```text
Remote agent creates or updates task
  -> AgentRelay stores task state
  -> AgentRelay writes durable agent event
  -> AgentRelay WebSocket sidecar pushes task.pending to the target agent
  -> Local listener receives event
  -> Local listener claims the specific task by task_id
  -> Local listener handles local Codex App thread routing
```

Success criteria:

- Existing HTTP/MCP manual fetch still works.
- A local listener can subscribe to only its own agent events.
- When a task becomes pending on an agent, the cloud emits a `task.pending` event.
- If the listener disconnects, pending work can be recovered by HTTP pending sync.
- Event payloads contain task metadata only, not full task bodies or secrets.
- Listener can claim a specific task id reliably.

## 2. Scope Boundary

Cloud AgentRelay will implement:

- durable agent event outbox
- WebSocket event sidecar
- pending task query API
- precise task claim API
- event ack API
- per-agent task thread binding metadata
- nginx + systemd deployment for WSS

Cloud AgentRelay will not implement:

- local Codex App thread creation
- local launchd listener
- Codex App thread adapter
- human confirmation logic
- semantic task completion judgement

The local side can choose any listener implementation as long as it follows the cloud API contract.

## 3. Protocol Choice

Use WebSocket for Phase 2 push.

Public endpoint:

```text
wss://server.stellarix.space/agentrelay/api/workers/:agentId/events/ws
```

Local clients connect outbound, so no local public IP is required.

Authentication uses the existing token model:

```text
Authorization: Bearer <AGENTRELAY_TOKEN>
X-AgentRelay-Agent-Id: <agent_id>
X-AgentRelay-Username: <username>
```

Connection authorization rules:

- token must be valid
- token must bind to the same `agent_id` as the path `:agentId`
- connection can only receive events for that agent
- no cross-agent subscription is allowed

## 4. WebSocket Message Contract

### `hello`

Sent once after a connection is accepted:

```json
{
  "type": "hello",
  "agentId": "zac-agent",
  "serverTime": 1782500000
}
```

### `task.pending`

Sent when a task is or remains pending on the subscribed agent:

```json
{
  "type": "task.pending",
  "eventId": "evt_abc",
  "agentId": "zac-agent",
  "taskId": "task_abc",
  "subject": "Meeting availability",
  "status": "delivery_pending",
  "pendingOnAgentId": "zac-agent",
  "updatedAt": 1782500000,
  "reason": "ownership.transferred"
}
```

The event intentionally does not include the full task. The listener should fetch the task over HTTP:

```text
GET /agentrelay/api/tasks/:taskId
```

### `heartbeat`

Sent periodically while idle:

```json
{
  "type": "heartbeat",
  "serverTime": 1782500000
}
```

### `error`

Sent before closing on recoverable protocol/auth problems when possible:

```json
{
  "type": "error",
  "code": "unauthorized",
  "message": "invalid bearer token"
}
```

## 5. Required HTTP APIs

### Pending sync

```text
GET /agentrelay/api/workers/:agentId/pending
```

Returns lightweight pending summaries:

```json
{
  "tasks": [
    {
      "taskId": "task_abc",
      "subject": "Meeting availability",
      "status": "delivery_pending",
      "pendingOnAgentId": "zac-agent",
      "updatedAt": 1782500000
    }
  ]
}
```

Use cases:

- listener startup
- WebSocket reconnect
- debugging
- recovery from missed events

### Precise claim

```text
POST /agentrelay/api/workers/:agentId/tasks/:taskId/claim
```

Semantics:

- if task is pending on `agentId`, claim this exact task and return it
- if the same agent already claimed it, return it idempotently
- if task is pending on another agent, return `409`
- if task is terminal, return `409`
- token agent must match path `agentId`

This endpoint is mandatory because WebSocket events refer to specific `taskId`s.

### Event ack

```text
POST /agentrelay/api/workers/:agentId/events/:eventId/ack
```

Payload:

```json
{
  "taskId": "task_abc",
  "status": "dispatched_to_local_listener",
  "threadId": "local-thread-id-if-known"
}
```

Ack is for audit/debug and does not become the source of truth. The source of truth remains task state, pending owner, and thread binding metadata.

## 6. Data Model Additions

### `agent_events`

Durable outbox for WebSocket notify:

```text
event_id TEXT PRIMARY KEY
agent_id TEXT NOT NULL
event_type TEXT NOT NULL
task_id TEXT NOT NULL
payload_json TEXT NOT NULL
acked_at INTEGER
created_at INTEGER NOT NULL
```

Events are inserted whenever a task becomes pending on an agent.

### `task_thread_bindings`

Per-agent local thread mapping:

```text
task_id TEXT NOT NULL
agent_id TEXT NOT NULL
thread_id TEXT NOT NULL
thread_role TEXT NOT NULL DEFAULT 'agent_inbox'
project_path TEXT
created_at INTEGER NOT NULL
updated_at INTEGER NOT NULL
PRIMARY KEY (task_id, agent_id, thread_role)
```

Reason: a single `target_thread_id` can be overwritten when multiple agents handle the same task. Phase 2 keeps the existing field for compatibility but adds per-agent bindings for reliable local routing.

## 7. Event Trigger Points

AgentRelay should write `agent_events` in these cases:

- `create_task` when `pending_on_agent_id` is set
- `submit_artifact` when ownership transfers to another agent
- `update_status` when `pending_on_agent_id` changes or remains actionable
- reconnect/pending sync does not create events; it reads task state

Do not emit pending events for terminal states:

```text
completed
failed
cancelled
expired
rejected
```

## 8. Deployment Design

Keep the current REST server unchanged as much as possible:

```text
agentrelay.service
  listens on 127.0.0.1:8787
  handles REST APIs
  writes SQLite task/events data
```

Add a WebSocket sidecar:

```text
agentrelay-ws.service
  listens on 127.0.0.1:8788
  reads the same SQLite DB
  reads the same auth file
  pushes agent_events over WebSocket
```

Nginx routes:

```text
/agentrelay/api/*                              -> 127.0.0.1:8787
/agentrelay/api/workers/:agentId/events/ws    -> 127.0.0.1:8788
```

The WebSocket route must set `Upgrade` / `Connection` headers and use long read timeouts.

## 9. Local Listener Contract

The local listener is outside cloud scope, but cloud APIs are designed for this flow:

```text
load .env
connect WSS /workers/:agentId/events/ws
on open: GET /workers/:agentId/pending
on task.pending:
  POST /workers/:agentId/tasks/:taskId/claim
  GET /tasks/:taskId
  inspect threadBindings[agentId]
  create/reuse local Codex App thread
  POST /workers/:agentId/tasks/:taskId/thread
  POST /workers/:agentId/events/:eventId/ack
```

The listener should treat remote task content as untrusted data and should not read or print local secrets.

## 10. Implementation Steps

1. [x] Add `phase2-plan.md` and update public plan page.
2. [x] Add DB schema for `agent_events` and `task_thread_bindings`.
3. [x] Add first store helpers for event outbox and thread bindings:
   - create pending/event outbox rows
   - list unacked/new events
   - ack event
   - upsert/list thread binding
   - return `threadBindings` in task payloads
4. [x] Add store methods for pending task summaries and precise claim by task id.
5. [x] Add REST endpoints:
   - `GET /workers/:agentId/pending`
   - `POST /workers/:agentId/tasks/:taskId/claim`
   - `POST /workers/:agentId/events/:eventId/ack`
6. [x] Add smoke tests for pending endpoint, precise claim, event ack, and thread binding writeback.
7. [x] Modify existing state transitions to write `agent_events` automatically.
8. [x] Add smoke coverage for automatic `task.pending` events on task creation and ownership transfer.
9. [x] Implement WebSocket sidecar service.
10. [x] Add nginx WebSocket route.
11. [x] Add `agentrelay-ws.service` systemd unit.
12. [x] Add WebSocket smoke test that receives `task.pending`.
13. [x] Update public `agent-relay-mcp` repo with listener install/verification flow.
14. [ ] Zac and Frank reinstall the public MCP repo and verify doctor/MCP/WSS.
15. [ ] Run two-agent test using local listeners.

Current checkpoint: Phase 2 Step 5 has prepared the public MCP/listener install path for Zac and Frank. Phase 2 Step 4 has landed the WebSocket notify sidecar at `wss://server.stellarix.space/agentrelay/api/workers/:agentId/events/ws`, deployed it with systemd/nginx, and verified that public WSS delivers `task.pending`. It is covered by `scripts/phase2_ws_smoke_test.py` and included in `npm test`.

## 11. Open Questions

- Should ack mark an event as globally acked, or acked per listener connection? MVP can use global `acked_at` per `agent_events` row.
- Should WebSocket send all unacked events or all currently pending tasks on reconnect? MVP should do both: send unacked events, and clients should call pending sync.
- Should `task_thread_bindings` support multiple roles per agent? MVP uses `agent_inbox` plus existing `requester_thread_id` for origin callbacks.
