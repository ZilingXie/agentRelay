# AgentRelay Protocol v0.6 Offline Delivery

Protocol v0.6 treats Listener absence as a delivery condition, not a Task
failure. Task, Message, and Event remain separate sources of truth.

Protocol activation applies only to newly created Tasks. An already open Task
keeps its creation-time `protocol_version`; compatible older Tasks are drained
through their own Store, readiness epoch, recovery feed, and WebSocket lane.
Bundle hot update may change a compatible wire mapping, but it never migrates
Task lifecycle state or rewrites Task protocol ownership.

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
| Any push/write/ACK-lease failure | unchanged | unchanged | `inflight -> parked` | HTTP recovery only |
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

Relay applies one persisted per-Agent inflight limit across every active
delivery lane, including v0.5 compatibility drain and v0.6. The default is
`max_inflight=1`; operators may configure values from 1 through 100. An Event
may move into `inflight` only while the Agent's combined inflight count is below
that limit. The next Event therefore waits until an ACK/NACK releases the
current lease or the ACK lease expires. This flow-control limit is independent
from the 1,000-Event admission backlog quota above.

`outbox_attempts` counts real-time push claims only. The current v0.6
coordinator parks an Event after the first push/write/ACK-lease failure, without
changing the open Task or pending Message. It does not schedule another
real-time push for that Event. A confirmed local persistence NACK also parks the
Event.

Authenticated HTTP recovery is fenced by `listener_instance_id` and
`readiness_epoch`. It may claim `parked` as `inflight` without incrementing
`outbox_attempts`; `recovery_attempts` records those claims and `inflight_via`
records `push` or `recovery`. An expired recovery lease returns to `parked`, so
recovery cannot re-enable periodic push. A durable ACK changes the Event to
`acked` and the current Message to `delivered` exactly once.

The recovery HTTP endpoint returns at most one Event per request. A Listener
must durably write and read back the Event, ACK it, and only then call recovery
again until the returned `events` array is empty. Recovery prioritizes
transitionable Messages before informational notices and obeys the same
per-Agent inflight limit as WebSocket push.

ACK, NACK, Listener registration/readiness, HTTP recovery, WebSocket socket
registration, and ACK-lease expiry all wake the delivery coordinator. The
one-second coordinator poll remains a fallback if an internal wake is lost or
the API-to-WebSocket wake request is unavailable.

The admin delivery summary reports per-Agent `queued`, `inflight`, and `parked`
counts across active lanes. ACK latency is measured from the most recent claim
to durable ACK. Recovery latency is measured from the first transition to
`parked` through the durable ACK that follows a recovery claim. Historical rows
without reconstructable timestamps are excluded from latency samples.

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
- A real-time delivery failure parks the Event immediately; HTTP recovery is
  explicit and observable.
- Combined inflight Events across all active protocol lanes never exceed the
  Agent's persisted `max_inflight`.

## File Attachments (bundle revision 10)

v0.6 adds task-scoped file transfer as an additive capability. Message parts
remain open JSON objects, so a new `file` part kind rides the existing reply
path without touching Task transitions or delivery:

- **Upload**: `POST /agentrelay/tasks/{task_id}/files` with a raw body, an
  explicit `Content-Length`, `X-AgentRelay-File-Name` (UTF-8 percent-encoded),
  and optional `X-AgentRelay-File-Sha256`. Only a Task participant may upload,
  only while the Task is `open`, and the upload is capped at
  `AGENTRELAY_MAX_FILE_BYTES` (default 64 MiB; nginx `client_max_body_size`
  stays 1 MiB above the cap). The Server streams bytes to
  `AGENTRELAY_BLOBS_DIR/<task_id>/<file_id>`, computes `sha256` itself, and
  de-duplicates identical content within the same Task. Blob paths are derived
  exclusively from the server-generated `file_id`; the client-supplied name is
  display metadata only.
- **Reference**: a reply Message may include
  `{kind: "file", file_id, name, mime_type?, size_bytes, sha256}` parts (see
  `file-part-v06.schema.json`). The Server rejects file parts whose blob is
  unknown, belongs to another Task, was uploaded by a different agent than the
  Message actor, or whose metadata does not match the stored blob. A Task's
  initial Message (create or follow-up) cannot carry file parts because file
  uploads are Task-scoped; reply with the file part instead.
- **Download**: `GET /agentrelay/tasks/{task_id}/files/{file_id}` streams the
  bytes to a Task participant with `Content-Length`, `X-AgentRelay-File-Sha256`,
  and an RFC 5987 `Content-Disposition`. `GET /agentrelay/tasks/{task_id}/files`
  lists file metadata for a Task.
- **Lifecycle**: bytes never ride JSON Messages or WebSocket frames; Events stay
  secret-safe pointers. Uploads never referenced by an accepted Message are
  deleted after `AGENTRELAY_FILE_ORPHAN_HOURS` (default 24). Files of a Task
  that stayed terminal are deleted after
  `AGENTRELAY_FILE_RETENTION_HOURS` (default 72); until then participants may
  still download them. The WebSocket delivery coordinator runs this GC on its
  tick, and the admin summary reports file counts and bytes.
- **Scope**: file transfer is v0.6-lane only. The v0.5 compatibility lane does
  not accept file parts and has no file endpoints. File limits are advertised
  in the v0.6 protocol manifest under `constants.files`. Using file parts from
  an agent requires an MCP client release with upload/download support, as
  documented in `protocol-auto-upgrade.md`.
