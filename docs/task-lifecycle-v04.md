# Protocol v0.4 Task Lifecycle Design

Status: design complete; server implementation verified, MCP/Listener implementation pending.

Status date: 2026-07-16.

Protocol v0.3 remains the active contract until the server and MCP/Listener
implementations pass the v0.4 conformance suite and advertise v0.4 support.

## 1. Goals And Boundaries

Protocol v0.4 models one durable two-Agent Task with multi-turn messages. Relay
owns validation, persistence, lifecycle transitions, delivery notification,
expiry, concurrency control, lineage, and audit. Local Agents own reasoning,
execution, and any human interaction or guardrails.

The lifecycle does not expose `claimed`, `working`, `replied`, human-waiting, or
local execution-progress states. The protocol supports exactly one requester
Agent and one target Agent. The same Agent cannot submit two consecutive
messages.

## 2. Lifecycle States

Active states:

```text
submitted
delivered
completed
expired
failed
```

`completed`, `expired`, and `failed` are terminal and immutable.

`cancelled` and `archived` are reserved vocabulary only. v0.4 schemas, APIs,
and storage transitions must reject them until a future protocol version
defines their authority and semantics.

State meanings:

- `submitted`: Relay validated and persisted the current message; the target
  Listener has not yet ACKed durable local Inbox persistence.
- `delivered`: the `to_agent_id` Listener durably persisted the current message
  and ACKed it to Relay.
- `completed`: the requester Agent confirmed that the delivered target response
  satisfies `done_criteria`.
- `expired`: Relay's authoritative clock reached `task_expires_at` while the
  Task was still non-terminal.
- `failed`: an allowed actor ended the Task for one of the enumerated,
  non-retryable failure reasons.

## 3. Task Snapshot

The Task row is a fixed-size current snapshot. Message and Event rows preserve
history; new turns do not add Task columns.

| Field | Contract |
| --- | --- |
| `task_id` | Relay-generated opaque globally unique id; immutable. |
| `root_task_id` | Root Task id for the lineage; root Tasks self-reference; immutable. |
| `protocol_version` | `agent-collab-v0.4`. |
| `requester_agent_id` | Creates the Task, starts each turn, and alone may complete it. |
| `target_agent_id` | Responds to requester messages. |
| `done_criteria` | Requester-defined completion test. |
| `status` | One active lifecycle state. |
| `turn_sequence` | Requester-to-response turn number, beginning at `1`. |
| `current_message_id` | Current message awaiting delivery or action. |
| `from_agent_id` | Current message sender. |
| `to_agent_id` | Current message receiver and next action owner after delivery. |
| `status_version` | Monotonic optimistic-concurrency version. |
| `max_turns` | Hard requester-to-response turn limit; default `12`. |
| `task_expires_at` | Absolute Task deadline; default creation time plus 24 hours; immutable. |
| `reason` | Nullable machine-readable terminal reason. |
| `terminal_by_agent_id` | Nullable Agent that requested the terminal transition. |
| `completed_against_message_id` | Nullable delivered target response used for completion. |
| `created_at` | Relay creation time. |
| `updated_at` | Last snapshot transition time; terminal time after closure. |

There is no `turn_expires_at`, `is_followup`, `parent_task_id`, or follow-up
sequence field. `task_id != root_task_id` is sufficient to derive that a Task
is a follow-up.

## 4. Message And Event History

Each Message records:

```text
message_id
task_id
turn_sequence
from_agent_id
to_agent_id
parts
idempotency_key
created_at
```

There is no lifecycle `message_type`. Direction and turn position distinguish
requester requests/follow-ups from target responses.

Each lifecycle Event records at least:

```text
event_id
task_id
event_type
status_version
message_id
turn_sequence
from_status
to_status
reason
actor_agent_id
created_at
```

Message and Event timestamps remain independent even though the Task snapshot
stores only `created_at` and `updated_at`.

## 5. Turn Definition

One turn begins when the requester submits a request or follow-up and ends when
the requester Listener ACKs the target response.

```text
Turn N:
requester -> target  submitted
target Listener ACK  delivered
target -> requester  submitted (same turn_sequence)
requester Listener ACK delivered (turn complete)
```

The requester then either completes the Task or starts Turn N+1. A target
response does not increment `turn_sequence`; a requester follow-up does.

Strict alternation requires every new message to satisfy:

```text
new.from_agent_id == previous.to_agent_id
new.to_agent_id == previous.from_agent_id
```

## 6. Transition Table

| From | Action | To | Authority | Required guards |
| --- | --- | --- | --- | --- |
| none | create Task | `submitted` | requester | initial requester-to-target Message persisted; turn `1` |
| `submitted` | current Message ACK | `delivered` | `to_agent_id` Listener | current message, turn, and expected version match |
| `delivered` | target response | `submitted` | target | current direction requester-to-target; same turn |
| `delivered` | requester follow-up | `submitted` | requester | current direction target-to-requester; increment turn; new turn allowed |
| `delivered` | confirm goal | `completed` | requester | current direction target-to-requester; current message matches completion evidence |
| `submitted` or `delivered` | deadline reached | `expired` | Relay | server time is at or after `task_expires_at` |
| `submitted` or `delivered` | terminal failure | `failed` | reason-specific actor | reason and source-state guards pass |

No transition leaves a terminal state. Duplicate idempotent requests return the
existing result without changing `status_version`.

### Create

Relay atomically persists the Task and initial Message:

```text
status = submitted
turn_sequence = 1
from_agent_id = requester_agent_id
to_agent_id = target_agent_id
status_version = 1
root_task_id = task_id
```

### ACK And Delivery

A current-message ACK must include:

```text
message_id
turn_sequence
expected_status_version
idempotency_key
```

The Listener ACKs only after durable local Inbox persistence. ACKing a status
notification, receipt, or heartbeat never changes the Task.

### Target Response

The target may respond only from a delivered requester-to-target snapshot.
Relay persists the response and atomically swaps `from_agent_id` and
`to_agent_id`, changes status to `submitted`, and increments `status_version`.
`turn_sequence` is unchanged.

### Requester Follow-up And Max Turns

The requester may start a new turn only from a delivered target-to-requester
snapshot. Relay increments `turn_sequence`, swaps direction, persists the new
Message, and changes status to `submitted` atomically.

When `turn_sequence >= max_turns`, Relay rejects another turn with
`409 max_turns_reached`; it does not close the Task automatically. The
requester must choose:

- `completed` if the current response meets the goal; or
- `failed` with `reason=max_turns_exhausted` if it does not.

### Completion

Completion requires:

```text
status = delivered
from_agent_id = target_agent_id
to_agent_id = requester_agent_id
completed_against_message_id = current_message_id
```

Relay sets `status=completed`, `reason=goal_met`,
`terminal_by_agent_id=requester_agent_id`, and `updated_at=now` atomically.

### Expiry

`task_expires_at` is set at Task creation and never resets across turns. At or
after the deadline, Relay wins races against ACK, Message, completion, or
failure mutations and atomically sets:

```text
status = expired
reason = task_timeout
terminal_by_agent_id = null
updated_at = now
```

Relay's clock is authoritative.

### Failure

Allowed `failed` reasons:

| Reason | Authority | Allowed source |
| --- | --- | --- |
| `delivery_retry_exhausted` | Relay | `submitted` |
| `listener_persistence_failed` | current `to_agent_id` Listener | `submitted` |
| `relay_persistence_failed` | Relay | any non-terminal state, only if the failure record can be durably committed |
| `agent_reported_failure` | current `to_agent_id` Agent | `delivered` |
| `max_turns_exhausted` | requester | `delivered`, with `turn_sequence >= max_turns` |
| `internal_consistency_error` | Relay | any non-terminal state, only if the failure record can be durably committed |

Invalid messages, transient network errors, and individual failed ACK attempts
remain retryable request/Event failures and do not change Task status.

## 7. Concurrency And Idempotency

Every ACK, Message, completion, and failure mutation carries:

```text
task_id
current_message_id
turn_sequence
expected_status_version
idempotency_key
```

Relay performs a compare-and-set against the current snapshot. A stale request
returns `409 stale_task_state` and the authenticated current snapshot. The
client must refresh rather than infer or overwrite state.

An identical actor, operation, and `idempotency_key` returns the original
result. It never creates another Message, increments a turn, or emits another
lifecycle transition.

## 8. Notifications

Every committed lifecycle transition emits one durable `task.status_changed`
Event. Entering `submitted` also creates one current-message pending Event for
`to_agent_id`; terminal transitions notify both participants.

The current-message pending Event is the only Event whose Listener ACK may
change Task status to `delivered`. Delivery receipts and status-change Events
are informational and cannot recursively mutate Task lifecycle state.

Relay may keep internal outbox delivery states and retry metadata, but they are
not Task lifecycle states.

## 9. Follow-up Lineage

Follow-ups are new Tasks with opaque Relay-generated ids. There is no readable
id suffix and no persisted `is_followup` flag.

Root Task:

```text
task_id = root_task_id
```

Follow-up Task:

```text
task_id != root_task_id
root_task_id = source.root_task_id
```

Only the requester may create a follow-up from `completed`, `expired`, or
`failed`. The new Task inherits requester and target, requires new
`done_criteria` and an initial Message, resets `turn_sequence=1`, and receives
new `max_turns` and `task_expires_at` values using request overrides or defaults.

The `task.followup_created` Event records `source_task_id`, `new_task_id`, and
`root_task_id`, preserving the direct source without another Task snapshot
field. Follow-up creation is idempotent.

## 10. Hard-delete Prohibition

Protocol v0.4 forbids hard deletion of every Task, including roots and
follow-ups, regardless of status.

- There is no public, admin, MCP, or internal Task DELETE operation.
- Storage exposes no Task deletion method.
- Task foreign keys use `ON DELETE RESTRICT`, never cascade.
- SQLite installs a `BEFORE DELETE ON tasks` trigger that aborts deletion.
- Migrations must update in place and may not recreate data by dropping Task
  rows without a separately reviewed recovery procedure.
- Future archive, retention, or privacy work may redact large/private payloads
  or hide Tasks, but must preserve Task identity, `root_task_id`, lifecycle
  state, timestamps, and the minimal audit/lineage records.

## 11. Public Operations

Protocol v0.4 requires operations equivalent to:

```text
POST /tasks
POST /tasks/{task_id}/messages
POST /workers/{agent_id}/messages/{message_id}/ack
POST /tasks/{task_id}/complete
POST /tasks/{task_id}/fail
POST /tasks/{source_task_id}/followups
```

There is deliberately no `DELETE /tasks/{task_id}`. Exact URL compatibility
aliases may be retained during implementation, but all operations must enforce
the transition, actor, version, and idempotency contracts above.

## 12. Compatibility And Rollout

This is a breaking lifecycle change and must ship as Protocol v0.4.

1. Server adds v0.4 storage, schemas, routes, bundle, dashboard visibility, and
   conformance support while continuing to serve v0.3.
2. Existing Tasks receive `root_task_id=task_id`; existing v0.3 lifecycle
   states are not rewritten into approximate v0.4 states.
3. Active v0.3 Tasks finish under v0.3 rules.
4. MCP/Listener adds v0.4 Message ACK, strict alternation, completion/failure,
   follow-up, and stale-state recovery.
5. New Tasks use v0.4 only when both participants advertise v0.4 capability;
   otherwise Relay negotiates v0.3.
6. Daily dispatch, dashboard, and diagnostics select semantics by
   `protocol_version` and stop treating reply `delivery_status` as target
   delivery evidence.
7. v0.3 deprecation is a later, separately approved rollout decision.

Server and client changes use separate PRs. Server protocol/schema support must
merge before the client enables v0.4 creation.

## 13. Conformance And Acceptance

The implementation is not complete until automated coverage proves:

1. One-turn completion and multi-turn completion.
2. Target response keeps the turn; requester follow-up increments it.
3. Same-Agent consecutive Messages are rejected.
4. Pending current-message Event leaves the Task `submitted`; durable Listener
   ACK changes it to `delivered`.
5. Old-message, old-turn, and old-version ACKs are rejected.
6. Duplicate ACKs, Messages, and follow-up creates are idempotent.
7. `max_turns` rejects a new turn and requires requester completion or
   `max_turns_exhausted` failure.
8. `task_expires_at` is immutable and expires both `submitted` and `delivered`
   Tasks; expiry wins boundary races.
9. Completion rejects stale or requester-authored evidence.
10. Every failed reason enforces its actor and source-state rules.
11. Informational Event ACKs never recurse into lifecycle transitions.
12. Terminal Tasks reject every lifecycle mutation.
13. Root and follow-up lineage queries work without parsing Task ids; direct
    source is present in `task.followup_created`.
14. Concurrent follow-up creation produces distinct opaque Task ids under the
    same root.
15. Every API/CLI/store Task deletion attempt is absent or rejected, raw SQL
    deletion is blocked, and no foreign-key cascade can delete a Task.
16. v0.3 and v0.4 Tasks coexist and render with version-correct semantics.

Implementation acceptance also requires server schema tests, protocol smoke and
conformance tests, client checks, and an end-to-end two-Agent run covering
create, ACK, response, ACK, requester completion, and follow-up creation.
