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

`task_expires_at` is a strict end-to-end deadline. The reply, requester-side
durable ACK, validation, and requester completion must all commit before it.
At `now >= task_expires_at`, v0.6 mutation transactions first persist expiry
and then reject the late ACK/NACK, Message, completion, or failure. Delivery
claim/recovery also runs expiry before returning transitionable Events.

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
- A mutation committed before the deadline remains idempotently readable after
  the deadline; an uncommitted mutation at the deadline cannot win over expiry.

## Bounded Coordinator Grants (bundle revision 13)

An operator-configured coordinator identity may create a root Task only with an
opaque grant sent in `X-AgentRelay-Coordinator-Grant`. Grant issuance is
authenticated at `POST /coordinator-grants` and binds the coordinator identity,
Investigation and Round ids, approved plan digest, human authority reference,
target set, exact Task count, common absolute `task_expires_at`, grant expiry,
and exactly `create`, `read`, `batch`, and `complete-own` operations.

The Server stores only a SHA-256 token hash. Reissuing the same issuance key
with identical claims returns the same `grant_id` and rotates `token_version`;
changed claims conflict. Task create validates and consumes quota in the same
`BEGIN IMMEDIATE` transaction that writes the Task, first Message, Event,
idempotency record, and grant-to-Task mapping. A create must use `max_turns=1`,
the exact common deadline, an allowed target, and matching
`investigation_id`, `round_id`, `work_item_id`, and `approved_plan_digest`
metadata. `POST /coordinator-grants/{grant_id}/tasks/resolve` recovers an
unknown create outcome by its original idempotency key; it never creates a new
Task.

The grant cannot authorize reply, follow-up, goal or participant mutation,
failure, completion of another Task, or another Round. Missing grants return
401; invalid, expired, revoked, wrong-identity, and forbidden-operation grants
return 403; claim mismatch, exhausted quota, and not-owned Task access return
409. `AGENTRELAY_COORDINATOR_AGENT_IDS` is the explicit identity allowlist.
`AGENTRELAY_COORDINATOR_DIRECT_CREATE_COMPATIBILITY` defaults off; when
explicitly enabled, each grantless coordinator create writes a
`coordinator.compatibility_create` Task audit event.

## One-Round Investigation Contract (bundle revision 13)

AgentRelay remains a pairwise transport and does not add Investigation, Round,
barrier, subagent, cancel, or batch-create objects. A Personal Investigation
Agent creates independent Tasks with the same absolute `task_expires_at` and
normally `max_turns=1`. The first Message may carry `investigation_id`,
`round_id`, and `work_item_id` in bounded metadata. Relay preserves these
values in Task reads and audit records without interpreting them; Event frames
remain pointer-only.

A target may return a `result-packet-v06.schema.json` part with status
`answered`, `blocked`, or `failed`. The requester validates the packet and may
complete its own Task for any accepted status. Relay Task `completed` means the
remote inquiry contract was accepted, not that the enclosing investigation
succeeded. Open Tasks expire normally. Human approval to start another round
belongs to the Personal Agent Prompt and is outside Relay state.

## Agent Discovery

The v0.6 Agent registry is authoritative for both `/agents` and Agent Cards.
`/agents` exposes only scheduling fields: identity, enabled/protocol support,
Card revision/reference, readiness freshness/observation, and active Task
count. Static governed profiles contain role, execution mode, declared skills,
accepted Task types, input/output modes, data and permission boundaries, and
policy. Dynamic readiness stays in Presence and observed success never expands
declared capability. Operators update profiles with
`scripts/upsert_agent_profile.py`; Agents cannot self-declare broader access.

## File Attachments (bundle revision 11)

v0.6 adds task-scoped file transfer as an additive capability. Message parts
remain open JSON objects, so a new `file` part kind rides the existing reply
path without touching Task transitions or delivery:

- **Upload**: `POST /agentrelay/tasks/{task_id}/files` with a raw body, an
  explicit `Content-Length`, `X-AgentRelay-File-Name` (UTF-8 percent-encoded),
  and optional `X-AgentRelay-File-Sha256`. Only a Task participant may upload,
  only while the Task is `open`, and the upload is capped at
  `AGENTRELAY_MAX_FILE_BYTES` (default 64 MiB). Only this upload route receives
  nginx's 65 MiB body allowance; ordinary JSON API routes remain capped at
  1 MiB by nginx and by the application. The Server streams bytes to
  `AGENTRELAY_BLOBS_DIR/<task_id>/<file_id>`, computes `sha256` itself, and
  de-duplicates only exact uploader/name/MIME/content matches within the same
  Task. Blob paths are derived
  exclusively from the server-generated `file_id`; the client-supplied name is
  display metadata only.
- **Reference**: a reply Message may include
  `{kind: "file", file_id, name, mime_type?, size_bytes, sha256}` parts (see
  `file-part-v06.schema.json`). The Server rejects file parts whose blob is
  unknown, belongs to another Task, was uploaded by a different agent than the
  Message actor, or whose name, MIME, size, or digest does not exactly match the
  stored blob. A Message may reference at most
  `AGENTRELAY_MAX_FILES_PER_MESSAGE` files (default 8) and
  `AGENTRELAY_MAX_TOTAL_FILE_BYTES` bytes in aggregate (default 64 MiB). A Task's
  initial Message (create or follow-up) cannot carry file parts because file
  uploads are Task-scoped; reply with the file part instead.
- **Download**: `GET /agentrelay/tasks/{task_id}/files/{file_id}` streams the
  bytes with `Content-Length`, `X-AgentRelay-File-Sha256`, and an RFC 5987
  `Content-Disposition`. The uploader can access its own unreferenced upload;
  the other participant receives 404 until a committed Message references that
  file. `GET /agentrelay/tasks/{task_id}/files` applies the same visibility rule.
- **Lifecycle**: bytes never ride JSON Messages or WebSocket frames; Events stay
  secret-safe pointers. Uploads never referenced by an accepted Message are
  deleted after `AGENTRELAY_FILE_ORPHAN_HOURS` (default 24). Files of a Task
  that stayed terminal are deleted after
  `AGENTRELAY_FILE_RETENTION_HOURS` (default 72); until then participants may
  still download referenced files. The WebSocket delivery coordinator runs GC
  immediately at startup and then no more than once per
  `AGENTRELAY_FILE_GC_INTERVAL_SECONDS` (default 3600); the admin summary reports
  file counts, bytes, and configured limits. GC also removes stale untracked
  blobs left by a process crash between blob rename and metadata commit.
- **Scope**: file transfer is v0.6-lane only. The v0.5 compatibility lane does
  not accept file parts and has no file endpoints. File limits are advertised
  in the v0.6 protocol manifest under `constants.files`. Using file parts from
  an agent requires an MCP client release with upload/download support, as
  documented in `protocol-auto-upgrade.md`.
