# Protocol v0.5 Two-Layer Task And Message Delivery Design

Status: design approved; implementation planned.

Status date: 2026-07-18.

Protocol v0.4 is a completed, immutable historical baseline. Protocol v0.5 is
the next implementation target and replaces the active write protocol during a
maintenance-window cutover. Existing v0.4 docs, schemas, examples, and evidence
remain published and must not be overwritten.

## 1. Goals And Boundaries

Protocol v0.5 separates durable collaboration lifecycle from Message delivery:

- Task lifecycle describes whether the two-Agent collaboration is still open
  and how it ended.
- Message delivery describes whether one immutable Message reached the target
  Listener's durable local Inbox.
- Agent Event outbox state describes Relay transport work only.

Relay owns validation, authoritative persistence, lifecycle transitions,
delivery scheduling, concurrency, expiry, lineage, audit, visibility, and hard
delete prevention. Local Agents own reasoning and execution. Local guardrails
continue to own human confirmation and sensitive-action policy.

The protocol supports exactly one requester Agent, one target Agent, strict
alternation, and one current Message. It does not expose claimed, working,
human-waiting, or local execution-progress states.

## 2. Single Sources Of Truth

Each state belongs to exactly one object:

| Object | Authoritative field | Meaning |
| --- | --- | --- |
| Task | `tasks.status` | collaboration lifecycle |
| Message | `messages.delivery_status` | durable Listener delivery |
| Agent Event | `agent_events.outbox_status` | Relay outbox transport |

The following rules are mandatory:

- v0.5 Tasks have no delivery-status copy.
- `agent_events.outbox_status` never represents Task or Message business state.
- Visibility diagnosis is computed, never persisted, and cannot drive state
  changes.
- Dashboard, dispatcher, MCP, Listener, workspace, and Inbox UI consume the
  Server visibility contract instead of reconstructing status independently.
- All authoritative transitions go through Store transactions.

Legacy `tasks.delivery_status`, `tasks.delivered_at`,
`tasks.delivery_error`, and `agent_events.delivery_state` are not v0.5 truth.

## 3. Task Lifecycle

Task states:

```text
open
completed
expired
failed
```

Allowed transitions:

```text
none -> open
open -> completed
open -> expired
open -> failed
```

Terminal states are immutable. `cancelled` and `archived` remain reserved
vocabulary and are not accepted lifecycle states.

State meanings:

- `open`: collaboration is active regardless of current Message delivery.
- `completed`: requester confirmed the current delivered target response meets
  `done_criteria`.
- `expired`: Relay's clock reached the immutable Task deadline first.
- `failed`: an allowed unrecoverable failure ended the Task.

## 4. Message Delivery

Each Message has one delivery state:

```text
pending
delivered
failed
```

Allowed transitions:

```text
none -> pending
pending -> delivered
pending -> failed
```

State meanings:

- `pending`: Relay persisted the Message; delivery and retry activity may be in
  progress, but the target Listener has not ACKed durable local persistence.
- `delivered`: target Listener persisted the complete Message to its local
  Inbox and sent a valid versioned ACK.
- `failed`: delivery is unrecoverable or all four delivery attempts are
  exhausted.

Retry is not a Message state. During retry wait the Message stays `pending`.

## 5. Agent Event Outbox

Outbox states:

```text
queued
inflight
acked
retry_wait
exhausted
```

Allowed transitions:

```text
queued -> inflight
inflight -> acked
inflight -> retry_wait
retry_wait -> inflight
inflight -> exhausted
retry_wait -> exhausted
```

Only a `message.pending` Event for the current Message may set
`can_transition_message=1`. Status, delivery-change, attempt-failure,
heartbeat, and recovery notifications must set it to `0`; ACKing those Events
only changes their own outbox row.

## 6. Fixed Delivery Retry Policy

Protocol constants:

```text
MAX_DELIVERY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = [60, 300, 600]
```

Attempt semantics:

```text
attempt 1: immediately after Message creation
attempt 2: 1 minute after attempt 1 fails
attempt 3: 5 minutes after attempt 2 fails
attempt 4: 10 minutes after attempt 3 fails
attempt 4 failure: exhausted
```

`max_delivery_attempts=4` is Relay-written and immutable in v0.5. It counts
the initial delivery plus three retries. The protocol does not persist
`delivery_expires_at`; scheduler state uses `outbox_attempts`,
`next_retry_at`, `inflight_until`, and `last_error`.

An attempt fails when WebSocket write fails, the connection closes before ACK,
the lease expires without ACK, or the Listener returns a retryable error. An
explicit non-retryable Listener persistence error exhausts immediately.

## 7. Authoritative Storage

### Task Snapshot

```text
task_id
root_task_id
protocol_version
requester_agent_id
target_agent_id
done_criteria
status
turn_sequence
current_message_id
from_agent_id
to_agent_id
task_version
max_turns
task_expires_at
reason
terminal_by_agent_id
completed_against_message_id
created_at
updated_at
```

`task_version` is the single aggregate optimistic-concurrency version. It
increments for a new Message, a valid current-Message delivery ACK, and a Task
terminal transition. v0.5 does not add `delivery_version`.

### Message Snapshot

```text
message_id
task_id
turn_sequence
from_agent_id
to_agent_id
parts_json
idempotency_key
delivery_status
max_delivery_attempts
delivered_at
failed_at
delivery_reason
created_at
updated_at
```

Attempt counters and scheduling timestamps belong to the transitionable Agent
Event, not the Message row. Visibility may expose them next to the Message.

### Agent Event Snapshot

```text
event_id
agent_id
event_type
task_id
message_id
payload_json
idempotency_key
outbox_status
outbox_attempts
inflight_until
next_retry_at
acked_at
exhausted_at
last_error
can_transition_message
created_at
updated_at
```

## 8. Create And New-Message Transactions

Task create atomically persists:

```text
Task(status=open, task_version=1, current_message_id=initial_message)
Message(delivery_status=pending, max_delivery_attempts=4)
AgentEvent(type=message.pending, outbox_status=queued,
           outbox_attempts=0, can_transition_message=1)
Task and Message audit Events
idempotency result
```

A new response or requester follow-up requires:

```text
Task.status = open
current Message.delivery_status = delivered
actor = Task.to_agent_id
actor != previous Message.from_agent_id
expected current Message, turn, and task_version match
```

Relay atomically inserts the next Message and pending Event, updates direction
and current Message, increments the requester turn only for requester
follow-ups, and increments `task_version`.

## 9. Versioned Message ACK

ACK input:

```text
task_id
message_id
turn_sequence
expected_task_version
idempotency_key
```

Relay verifies Task protocol, open status, current Message, turn, aggregate
version, Message ownership/direction, pending delivery, target Listener, and a
non-exhausted transitionable pending Event.

A successful ACK atomically:

```text
Message.pending -> delivered
Message.delivered_at = now
AgentEvent -> acked
AgentEvent.acked_at = now
Task.task_version += 1
Task.updated_at = now
append message.delivery_changed
enqueue informational participant notifications
record idempotency result
```

Duplicate identical mutations return the original result. Reusing a key for a
different request is a conflict. Old Message, turn, version, terminal-Task, and
exhausted-Event ACKs are rejected.

## 10. Delivery Exhaustion Transaction

Attempt four failure or an immediate non-retryable Listener persistence error
atomically performs:

```text
AgentEvent -> exhausted
Message.pending -> failed
Task.open -> failed
```

It records matching stable reason, terminal, attempt, and notification Events.
No committed state may contain an exhausted current pending Event with an open
Task or pending Message.

Delivery reasons:

```text
delivery_retry_exhausted
listener_persistence_failed
```

## 11. Completion, Failure, Expiry, Turns, And Follow-Up

Completion requires requester authority, Task `open`, current target-to-
requester direction, current Message `delivered`, current evidence id, turn,
and version. It sets `completed/goal_met` atomically.

Business failure reasons retained from v0.4 include:

```text
agent_reported_failure
max_turns_exhausted
relay_persistence_failed
internal_consistency_error
```

Task expiry wins through a conditional transaction. If expiry occurs while the
current Message is pending, Relay also marks the Message failed with
`task_expired_before_delivery` and exhausts its outbox Event with
`task_expired`.

One turn still begins with requester request/follow-up and ends when requester
Listener ACKs the target response. Target response keeps the turn; requester
follow-up increments it. At `max_turns`, requester must complete or fail. Task
IDs remain opaque, roots self-reference, and follow-ups inherit `root_task_id`.

## 12. Events And Non-Recursive Notifications

Protocol Events include:

```text
task.status_changed
message.pending
message.delivery_changed
message.delivery_attempt_failed
task.followup_created
```

Task lifecycle and Message delivery Events are separate. Informational Event
ACKs never mutate Task or Message. WebSocket pushes contain safe metadata and
payload references, never full Message parts.

## 13. Visibility And Diagnosis

All consumers use single and batch visibility APIs. The response contains Task,
current Message, current transitionable outbox Event, and deterministic
diagnosis.

Diagnosis values include:

```text
message_queued
message_inflight
message_pending_retry
waiting_target_response
waiting_requester_decision
task_completed
task_expired
task_failed_delivery
task_failed
```

Dashboard, dispatcher, MCP, Listener, workspace, and Inbox UI must not
reimplement these rules or query legacy delivery fields directly.

## 14. MCP, Listener, Workspace, And Inbox UI Contract

v0.5 provides create, send Message, complete, fail, follow-up, lineage,
visibility, and protocol-sync tools. Generic tools switch to v0.5 after the
maintenance cutover. v0.4 mutations return `410 protocol_retired`; historical
GET, timeline, and lineage remain read-only.

Listener handling order is fetch full payload, lock workspace, durably persist
Task/Message/Event, verify the local write, then send the versioned Message ACK.
Local write failure means no ACK. Stale responses trigger visibility resync.

Workspace v2 stores Task lifecycle and per-Message delivery separately. Legacy
v0.4 workspaces remain read-only. Inbox UI shows separate Task and delivery
badges, filters, attempt details, and action guards based on visibility.

## 15. Dashboard And Dispatcher Contract

Dashboard shows separate Task, Message delivery, and outbox statistics and
timelines. Dispatcher consumes batch visibility and reports Completed, Failed,
Expired, Delivery pending, Waiting for target response, and Waiting for
requester decision. It must never report a delivered current Message awaiting
a response as a failed Task.

## 16. Maintenance-Window Cutover

This project currently has few users, so v0.5 uses a direct maintenance-window
cutover instead of active-Task continuation.

Before maintenance, Task 0 updates and publishes Server, Client, and public
plans. v0.4 remains a completed historical baseline; v0.5 is recorded as the
implementation target. No implementation begins before those plan updates are
merged and published.

During maintenance:

1. Stop dispatcher, Listeners, WebSocket, and mutation APIs.
2. Drain current SQLite transactions and create a verified backup.
3. Preserve original legacy Task snapshots and audit history.
4. Mark active v0.4 Tasks `failed/protocol_upgrade_required`.
5. Mark their undelivered current Messages
   `failed/task_terminated_for_protocol_upgrade`.
6. Rebuild canonical v0.5 Task, Message, Event, index, and trigger schema.
7. Keep terminal v0.4 history readable; reject all v0.4 mutations with 410.
8. Deploy Server, MCP/Listener, UI, dashboard, and dispatcher v0.5.
9. Verify capability, health, manifest, visibility, migration invariants, and
   real two-Agent E2E before opening writes.

Old-data migration is intentionally simple. It preserves identity and audit,
but does not attempt to continue active v0.4 Tasks.

## 17. Rollback Boundary

Before v0.5 writes open, rollback restores the verified database backup and old
Server/Listener/dispatcher versions. After v0.5 writes open, rollback to the old
database would lose new Tasks and is forbidden without a new maintenance
window, explicit v0.5 export, and human approval. Forward-fix is the default.

## 18. Hard Delete And Security

v0.5 continues to forbid Task hard deletion at API, CLI, Store, foreign-key,
and raw-SQL trigger layers. Maintenance does not delete v0.4 Tasks, Messages,
Events, artifacts, or lineage. Local workspaces are preserved. Auth, WebSocket
payload secrecy, and guardrail/user-authority boundaries remain unchanged.

## 19. Conformance Acceptance

At minimum, conformance must prove:

1. Plan preserves v0.4 completed artifacts and introduces v0.5 separately.
2. Task and first Message/outbox Event create atomically.
3. Task lifecycle and Message delivery states have independent truth.
4. Only current transitionable Message ACK changes delivery.
5. Listener persistence precedes ACK.
6. Duplicate Message and ACK mutations are idempotent.
7. Old Message, turn, and task-version ACKs are rejected.
8. Informational Event ACK cannot mutate Task or Message.
9. Attempts occur at immediate, +1m, +5m, and +10m boundaries.
10. Scheduler does not claim before `next_retry_at`.
11. Lease expiry and ACK races have one atomic winner.
12. Attempt four failure atomically exhausts Event and fails Message and Task.
13. Non-retryable Listener persistence failure exhausts immediately.
14. Server restart preserves retry schedule.
15. Strict two-Agent alternation remains enforced.
16. Target response keeps turn; requester follow-up increments it.
17. Requester completion requires current delivered target evidence.
18. Max turns require requester completion or failure.
19. Task expiry wins races and closes pending delivery consistently.
20. Follow-up lineage and opaque IDs remain correct.
21. v0.4 history is readable and v0.4 mutations return 410.
22. Legacy active Tasks terminate with auditable upgrade reasons.
23. Hard delete remains impossible.
24. Dashboard and dispatcher use visibility diagnosis correctly.
25. Production two-Agent create/ACK/response/ACK/complete/follow-up E2E passes.

## 20. Implementation Ownership And Order

Keep Server and Client changes in separate commits and PRs:

```text
Task 0 Server/Client/public plan updates
-> Server v0.5 protocol, schema, Store, scheduler, API, migration, dashboard
-> MCP/Listener v0.5 tools, ACK, workspace, Inbox UI
-> dispatcher integration
-> cross-repository conformance and maintenance rehearsal
-> maintenance-window production cutover
```

The public roadmap must report actual state at every stage and may mark v0.5
complete only after merged code, maintenance deployment, invariant checks, and
production E2E.
