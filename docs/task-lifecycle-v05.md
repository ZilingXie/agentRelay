# Protocol v0.5 Two-Layer Task And Message Delivery Design

Status: core design confirmed; specification review in progress; implementation
not started.

Status date: 2026-07-19.

Protocol v0.4 is a completed, immutable historical baseline. Protocol v0.5 is
the next implementation target and replaces every v0.3/v0.4 write path during
a maintenance-window cutover. Existing v0.3/v0.4 docs, schemas, examples, and
evidence remain published and must not be overwritten.

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
retry_wait -> acked
inflight -> exhausted
retry_wait -> exhausted
```

Only a `message.pending` Event for the current Message may set
`can_transition_message=1`. Status, delivery-change, attempt-failure,
heartbeat, and recovery notifications must set it to `0`; ACKing those Events
only changes their own outbox row.

Exhaustion is also scoped by this flag. Exhausting an informational Event with
`can_transition_message=0` updates only that Event and observability metrics; it
must never fail a Message or Task. Only exhaustion of the current
`message.pending` Event with `can_transition_message=1` executes the atomic
Event/Message/Task failure transaction.

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

The same four-attempt outbox policy applies to informational Events, using the
Relay constant because they have no Message-owned attempt limit. Their
exhaustion remains Event-only as defined above.

Every Event delivery surface must claim the Event before returning or sending
it. Claim atomically changes `queued/retry_wait -> inflight`, increments
`outbox_attempts`, sets `inflight_until`, and clears `next_retry_at`. Attempt 1
is the first claim. HTTP polling may not expose an unclaimed queued Event as if
it had been delivered.

An attempt fails when WebSocket write fails, the connection closes before ACK,
the lease expires without ACK, or the Listener returns a retryable error. The
failure transaction uses the post-claim attempt count:

```text
attempt 1 failure -> retry_wait; next_retry_at = failed_at + 60
attempt 2 failure -> retry_wait; next_retry_at = failed_at + 300
attempt 3 failure -> retry_wait; next_retry_at = failed_at + 600
attempt 4 failure -> exhausted; next_retry_at = null
```

Only the scheduler may reclaim `retry_wait`, and only at or after
`next_retry_at`. A late valid ACK may transition `inflight` or `retry_wait` to
`acked` while the Message remains current and pending. An explicit
non-retryable Listener persistence error exhausts immediately regardless of
attempt count.

A Listener reports only a confirmed non-retryable local persistence failure by
calling the Message delivery-failure operation with:

```text
task_id
message_id
event_id
turn_sequence
expected_task_version
reason = listener_persistence_failed
idempotency_key
```

Relay applies the same current-Message, target-Agent, version, ownership, and
Event guards as ACK before exhausting. A retryable local error is not NACKed;
the Listener sends no ACK and Relay recovers through lease expiry. This keeps
retry policy and attempt accounting exclusively on Relay.

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

For the current transitionable `message.pending` Event, attempt four failure or
an immediate non-retryable Listener persistence error atomically performs:

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

Task terminal reasons are a closed protocol enum:

| Task status | Task `reason` | `terminal_by_agent_id` | Message effect |
| --- | --- | --- | --- |
| `completed` | `goal_met` | requester | current Message remains delivered |
| `expired` | `task_expired` | null | pending Message fails with `task_expired_before_delivery`; delivered remains delivered |
| `failed` | `delivery_retry_exhausted` | null | pending Message fails with the same reason |
| `failed` | `listener_persistence_failed` | target Listener Agent | pending Message fails with the same reason |
| `failed` | `agent_reported_failure` | authorized current `to_agent_id` | delivered remains delivered |
| `failed` | `max_turns_exhausted` | requester | delivered remains delivered |
| `failed` | `relay_persistence_failed` | null | pending Message fails with the same reason; delivered remains delivered |
| `failed` | `internal_consistency_error` | null | pending Message fails with the same reason; delivered remains delivered |

`reason` is null while open and required in every terminal state.
`completed_against_message_id` is required only for `completed` and null for
all other states. Relay-driven terminal transitions keep
`terminal_by_agent_id=null`; `updated_at` remains the terminal timestamp.

Business failure authority retained from v0.4 is explicit:

| Reason | Actor | Required source state and direction |
| --- | --- | --- |
| `agent_reported_failure` | current `to_agent_id` Agent | Task open; current Message delivered |
| `max_turns_exhausted` | requester | Task open; current delivered target-to-requester Message; turn at max |
| `relay_persistence_failed` | Relay | Task open; durable failure record can still commit |
| `internal_consistency_error` | Relay | Task open; durable failure record can still commit |

Every Task terminal transition must leave no retryable transitionable Event.
If the current Message is pending when any Task failure commits, the same
transaction marks that Message failed and its transitionable Event exhausted.
For Relay/internal failure, the Message uses the matching reason. If the
current Message is already delivered, its delivery state remains delivered and
its pending Event must already be acked. This invariant also applies to expiry
and maintenance termination.

Task expiry wins through a conditional transaction. If expiry occurs while the
current Message is pending, Relay also marks the Message failed with
`task_expired_before_delivery` and exhausts its outbox Event with
`task_expired`.

One turn still begins with requester request/follow-up and ends when requester
Listener ACKs the target response. Target response keeps the turn; requester
follow-up increments it. At `max_turns`, requester must complete or fail. Task
IDs remain opaque, roots self-reference, and follow-ups inherit `root_task_id`.

### Consolidated Transition Table

Unchanged cells retain their current value. Every row is one Store transaction.

| Operation | Actor | Required source and guards | Task result | Message result | Transitionable outbox result |
| --- | --- | --- | --- | --- | --- |
| create | requester | both participants pass capability admission; idempotency key new or identical | `none -> open`, version 1 | first Message `none -> pending` | `none -> queued`, attempts 0 |
| claim delivery attempt | Relay delivery surface | Task open; current Message pending; Event queued, or retry due; lease claim wins | unchanged | unchanged | `queued/retry_wait -> inflight`; attempts +1 |
| ACK current Message | target Listener | current Event/Message/turn/version and direction match; local persistence complete | open; version +1 | `pending -> delivered` | `inflight/retry_wait -> acked` |
| retryable attempt failure | Relay | current transitionable Event inflight; attempts less than 4 | unchanged | remains pending | `inflight -> retry_wait`; set policy retry time |
| delivery exhaustion | Relay or target Listener NACK | attempt 4 fails, or guarded non-retryable persistence failure | `open -> failed`; delivery reason | `pending -> failed`; same reason | `inflight/retry_wait -> exhausted` |
| send next Message | current `to_agent_id` | Task open; current Message delivered/Event acked; strict alternation; current Message/turn/version match | open; current snapshot changes; version +1 | new Message `none -> pending` | new Event `none -> queued`, attempts 0 |
| complete | requester | open; delivered target-to-requester current Message; evidence/turn/version match | `open -> completed`; `goal_met`; version +1 | unchanged delivered | remains acked |
| business fail | reason-authorized Agent or Relay | guard in failure authority table; current Message/turn/version match | `open -> failed`; stable reason; version +1 | delivered stays delivered; pending becomes failed | acked stays acked; pending Event becomes exhausted |
| expire | Relay clock | open; immutable deadline reached; conditional update wins | `open -> expired`; version +1 | delivered stays delivered; pending becomes failed | acked stays acked; pending Event becomes exhausted |

Follow-up creation is not a transition of the source Task. It requires an
immutable terminal source, creates a distinct root-linked Task through the
create transaction, and leaves the source unchanged.

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

All consumers use single and batch visibility APIs. Every response includes:

```text
protocol_version = agent-collab-v0.5
diagnosis_version = 1
generated_at
task
current_message
outbox
diagnosis
```

The batch response contains `items` with the identical per-Task shape and an
`errors` entry for each unknown or unauthorized Task. A batch item may not omit
current Message or outbox state for an open Task.

Diagnosis uses this strict priority and exhaustive matrix:

| Task | Message | Outbox | Direction | Diagnosis |
| --- | --- | --- | --- | --- |
| `completed` | any valid terminal snapshot | any non-retryable snapshot | any | `task_completed` |
| `expired` | any valid terminal snapshot | any non-retryable snapshot | any | `task_expired` |
| `failed` | failed with delivery reason | exhausted transitionable Event | any | `task_failed_delivery` |
| `failed` | any other valid terminal snapshot | no retryable transitionable Event | any | `task_failed` |
| `open` | `pending` | `queued` | either | `message_queued` |
| `open` | `pending` | `inflight` | either | `message_inflight` |
| `open` | `pending` | `retry_wait` | either | `message_pending_retry` |
| `open` | `delivered` | `acked` | requester to target | `waiting_target_response` |
| `open` | `delivered` | `acked` | target to requester | `waiting_requester_decision` |

Any other combination returns `invariant_violation` plus stable invariant
codes and triggers an operational alert. It must not be coerced to a normal
diagnosis. Terminal Task diagnosis has priority, but terminal invariants still
require no retryable transitionable Event.

Dashboard, dispatcher, MCP, Listener, workspace, and Inbox UI must not
reimplement this matrix or query legacy delivery fields directly.

## 14. MCP, Listener, Workspace, And Inbox UI Contract

v0.5 provides create, send Message, complete, fail, follow-up, lineage,
visibility, and protocol-sync tools. Generic tools switch to v0.5 after the
maintenance cutover. v0.3/v0.4 mutations return `410 protocol_retired`;
historical GET, timeline, and lineage remain read-only.

Listener handling order is fetch full payload, lock workspace, durably persist
Task/Message/Event, verify the local write, then send the versioned Message ACK.
Local write failure means no ACK. Stale responses trigger visibility resync.

This ordering is protocol-specific. A v0.5 transitionable `message.pending`
Event must never use the legacy ACK-then-sync intake path. That path may remain
only for read-only legacy handling and v0.5 informational Events whose ACK
cannot transition Message or Task state.

Workspace v2 stores Task lifecycle and per-Message delivery separately. Legacy
v0.3/v0.4 workspaces remain read-only. Inbox UI shows separate Task and delivery
badges, filters, attempt details, and action guards based on visibility.

## 15. Dashboard And Dispatcher Contract

Dashboard shows separate Task, Message delivery, and outbox statistics and
timelines. Dispatcher consumes batch visibility and reports Completed, Failed,
Expired, Delivery pending, Waiting for target response, and Waiting for
requester decision. It must never report a delivered current Message awaiting
a response as a failed Task.

### Project Hermes Upgrade

Project Hermes has two separate v0.5 responsibilities:

- The Hermes Listener participates in Message delivery. It advertises v0.5,
  reports readiness, persists the complete Message before ACK, uses the guarded
  NACK only for confirmed non-retryable persistence failures, stores workspace
  v2, and submits its reply through the normal v0.5 Message transaction.
- The Hermes daily dispatcher is a read-only reporting integration. It consumes
  Server batch visibility and diagnosis; it never queries legacy Task delivery
  fields, reads raw `agent_events` to infer product state, or maintains a fourth
  status model.

Task 0 must identify and record the deployed dispatcher's executable
repository, deployment path, owner, schedule, configuration source, and
rollback command. Its v0.5 workstream then:

1. replaces local status inference with batch visibility;
2. reports separate counts for Completed, Failed, Expired, Delivery pending,
   Waiting for target response, and Waiting for requester decision;
3. includes stable diagnosis/reason plus attempt, next-retry, and last-error
   details where relevant, without exposing Message content;
4. makes a partial batch/API failure explicit instead of classifying the
   affected Task as Failed;
5. adds a dry-run mode that produces the report without sending WeCom;
6. preserves one idempotent dispatch record per schedule window so retries do
   not send duplicate reports; and
7. adds metrics and alerts for visibility failures, report-send failures, and
   stale Hermes Listener readiness.

Opening v0.5 writes is blocked until the exact deployed Listener and dispatcher
runtime pass the Zac delivered-but-waiting, Vivi not-delivered, delivery
exhaustion, completed, expired, partial-batch, and duplicate-dispatch regression
cases. A maintenance rehearsal must run the dispatcher in dry-run before the
first real v0.5 WeCom report.

## 16. Listener Capability And Readiness Gate

The cutover has no silent downgrade. Relay uses the enabled Agent registry as
the complete admission set. Every enabled requester and target Agent must have:

```text
protocol_capabilities contains agent-collab-v0.5
listener_readiness.protocol_version = agent-collab-v0.5
listener_readiness.ready = true
listener_readiness.observed_at no older than 300 seconds
```

`LISTENER_READINESS_MAX_AGE_SECONDS=300` is a deployment admission constant,
not a delivery guarantee or Task state. Before writes open, operators must
disable every unsupported, unupgraded, or stale Agent. Relay rejects creation
when either participant is not v0.5-capable with `409 protocol_v05_required`,
or when either participant's Listener is not fresh and ready with
`409 listener_not_ready`. Once a Task is admitted, later Listener unavailability
uses the normal four-attempt delivery policy rather than changing capability.

This gate prevents Relay from accepting a new v0.5 Task for a participant that
is still running an incompatible Listener. The 300-second check is only a
cutover/create admission snapshot; it does not mean a Message was delivered,
does not keep a Task alive, and does not replace delivery retry. The deployment
must verify that 300 seconds covers at least three configured readiness
publication intervals. Otherwise the Listener interval or admission constant
must be corrected before cutover rather than weakening the gate at runtime.

## 17. Maintenance-Window Cutover

This project currently has few users, so v0.5 uses a direct maintenance-window
cutover instead of active-Task continuation.

Before maintenance, Task 0 updates and publishes Server, Client, and public
plans. v0.4 remains a completed historical baseline; v0.5 is recorded as the
implementation target. No implementation begins before those plan updates are
merged and published.

During maintenance:

1. Stop dispatcher, Listeners, WebSocket, and mutation APIs.
2. Drain current SQLite transactions and create a verified backup.
3. Export a retirement report for every non-terminal v0.3/v0.4 Task, including
   Task id, protocol, original status, current participant ids, current Message
   id when present, and `protocol_upgrade_required` as the operational reason.
4. Verify the backup and retirement report, then mount the entire legacy
   database as a read-only archive. Do not rewrite legacy lifecycle fields.
5. Create a new v0.5 database from the canonical v0.5 schema and seed only the
   validated Agent identity, authorization, capability, and readiness registry
   required to authenticate upgraded participants. Do not copy legacy Task,
   Message, Event, artifact, lineage, idempotency, or scheduler rows into it.
6. Deploy Server, MCP/Listener, workspace, Inbox UI, dashboard, and the located
   dispatcher runtime at v0.5.
7. Validate every enabled Agent against the capability/readiness gate; disable
   unsupported or stale Agents before opening writes.
8. Verify health, manifest, visibility, no-legacy-task invariants, legacy
   archive reads, 410 retirement behavior, and real two-Agent E2E.
9. Open v0.5 writes only after every gate passes.

Legacy reads return the original v0.3/v0.4 snapshot plus archive metadata and
may join the retirement report; they do not synthesize a v0.5 status. Every
v0.3/v0.4 mutation route returns `410 protocol_retired`. Old local workspaces
are read-only. This intentionally avoids mixed-protocol rows and does not
attempt to continue old active Tasks.

The separate archive/new-database boundary prevents legacy fields from becoming
a second interpretation of v0.5 truth. It trades active legacy continuation for
a simpler invariant: every writable collaboration row is natively v0.5. The
retirement report preserves the operational outcome for unfinished legacy work,
while the original database remains the audit source. Only identity and access
records cross the boundary because the upgraded participants must still
authenticate; those records are validated before import and do not carry Task
state.

## 18. Rollback Boundary

Before v0.5 writes open, rollback restores the verified database backup and old
Server/Listener/dispatcher versions. After v0.5 writes open, rollback to the old
database would lose new Tasks and is forbidden without a new maintenance
window, explicit v0.5 export, and human approval. Forward-fix is the default.

## 19. Hard Delete And Security

v0.5 continues to forbid Task hard deletion at API, CLI, Store, foreign-key,
and raw-SQL trigger layers. The legacy database and retirement report are
retained as immutable archive material; maintenance does not delete legacy
Tasks, Messages, Events, artifacts, or lineage. Local workspaces are preserved.
Auth, WebSocket payload secrecy, and guardrail/user-authority boundaries remain
unchanged.

## 20. Conformance Acceptance

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
21. v0.3/v0.4 history is readable from the unmodified archive and all legacy
    mutations return 410.
22. Every legacy non-terminal Task appears in the verified retirement report;
    no legacy row is copied into or reinterpreted by the v0.5 database.
23. Hard delete remains impossible.
24. Dashboard and dispatcher use visibility diagnosis correctly.
25. Production two-Agent create/ACK/response/ACK/complete/follow-up E2E passes.
26. Every enabled Agent passes the v0.5 capability/readiness gate before writes
    open; unsupported and stale participants receive the specified 409 errors.
27. The actual deployed Hermes Listener and dispatcher pass delivered-but-
    waiting, not-delivered, exhaustion, completed, expired, partial-batch, stale
    readiness, and duplicate-dispatch regression cases; dry-run sends no WeCom
    message and a retried schedule window sends at most one real report.
28. A guarded non-retryable Listener NACK is idempotent and exhausts exactly
    once; retryable local errors produce no NACK and remain Relay-scheduled.
29. Only the validated Agent registry crosses into the v0.5 database; no legacy
    collaboration or outbox row is present after seeding.

## 21. Implementation Ownership And Order

Keep Server and Client changes in separate commits and PRs:

```text
Task 0 Server/Client/public plan updates
-> locate deployed dispatcher and record repository/path/owner
-> Server v0.5 protocol, schema, Store, scheduler, API, archive, dashboard
-> MCP/Listener v0.5 tools, ACK, workspace, Inbox UI
-> dispatcher integration
-> cross-repository conformance and maintenance rehearsal
-> maintenance-window production cutover
```

The public roadmap must report actual state at every stage and may mark v0.5
complete only after merged code, maintenance deployment, invariant checks, and
production E2E.
