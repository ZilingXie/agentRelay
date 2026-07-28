# AgentRelay Protocol v0.6 Offline Delivery

Protocol v0.6 treats Listener absence as a delivery condition, not a Task
failure. Task, Message, and Event remain separate sources of truth.

| Object | Field | States relevant to offline delivery |
| --- | --- | --- |
| Task | `tasks.status` | `open`, `completed`, `expired`, `failed` |
| Message | `messages.delivery_status` | `pending`, `delivered`, `failed` |
| Event | `agent_events.outbox_status` | `queued`, `inflight`, `retry_wait`, `parked`, `acked`, `exhausted` |

`waiting_listener` is a computed visibility diagnosis for
`open + pending + parked`. It is never persisted as Task or Message truth.

## Authoritative Transition Matrix

| Trigger | Task | Message | Event | Retry eligibility |
| --- | --- | --- | --- | --- |
| Create/reply, Listener fresh | `open` | `pending` | `queued` | Real-time push |
| Create/reply, Listener absent/stale | `open` | `pending` | `parked` | HTTP recovery only |
| Real-time claim | unchanged | unchanged | `queued/retry_wait -> inflight` via `push` | Lease-bound |
| Push/write/ACK-lease failure, attempts 1-3 | unchanged | unchanged | `inflight -> retry_wait` | Timed push retry |
| Fourth push failure | unchanged | unchanged | `inflight -> parked` | HTTP recovery only |
| HTTP recovery claim | unchanged | unchanged | `parked/queued/retry_wait -> inflight` via `recovery` | Lease-bound |
| Recovery lease failure or persistence NACK | unchanged | `pending` | `inflight -> parked` | HTTP recovery only |
| Durable current-epoch ACK | unchanged | `pending -> delivered` | `inflight/retry_wait/parked -> acked` | None |
| Task TTL | `open -> expired` | pending current Message becomes `failed` | obsolete transitionable Event becomes `exhausted`; terminal notice is created | Notice remains recoverable |
| Authorized business failure | `open -> failed` | pending current Message becomes `failed` | obsolete transitionable Event becomes `exhausted`; terminal notice is created | Notice remains recoverable |

Transport conditions never take the authorized business-failure row.

## Admission

Create and reply require known, enabled, authorized participants advertising
`agent-collab-v0.6`. Transient Listener readiness is not an admission gate. A
Message for a fresh ready Listener starts `queued`; otherwise it starts
`parked`. A per-Agent admission limit of 1,000 recoverable Events
(`queued`, `inflight`, `retry_wait`, or `parked`) bounds new Message backlog;
admission fails with `agent_backlog_full` before another Message Event can be
added. `acked` and terminally `exhausted` Events do not consume this quota.
Terminal informational notices are loss-prevention writes and are not allowed
to roll back Task expiry or an authorized business terminal transition.

## Delivery

`outbox_attempts` counts real-time push claims only and never exceeds four.
Push failure moves `inflight` to `retry_wait`; the fourth failure moves it to
`parked` without changing the open Task or pending Message. A confirmed local
persistence NACK also parks the Event.

Authenticated HTTP recovery is fenced by `listener_instance_id` and
`readiness_epoch`. It may claim `parked` as `inflight` without incrementing
`outbox_attempts`; `recovery_attempts` records those claims and `inflight_via`
records `push` or `recovery`. An expired recovery lease returns to `parked`, so
recovery cannot re-enable periodic push. A durable ACK changes the Event to
`acked` and the current Message to `delivered` exactly once.

The recovery HTTP endpoint returns at most one Event per request. A Listener
must call it until the returned `events` array is empty, durably write and read
back each Event before ACK, then continue the loop. Recovery prioritizes
transitionable Messages before informational notices.

## Expiry And Notices

Task TTL remains authoritative. Expiry atomically changes the Task to
`expired`, fails an undelivered current Message with `task_expired`, exhausts
that obsolete transitionable Event, and creates informational terminal Events
for participants. Informational Events also park after bounded push attempts
and remain recoverable until ACK, including after the Task is terminal.

## Required Invariants

- Transport errors never set `tasks.status = failed`.
- `parked` Events have no `next_retry_at` or active lease.
- Only a current Listener epoch may recover or ACK an Event.
- A late ACK may win over `retry_wait` or `parked` for the same current Message.
- Old epochs, stale Messages, stale turns, and stale Task versions remain
  rejected.
- Real-time retries are bounded; HTTP recovery is explicit and observable.
