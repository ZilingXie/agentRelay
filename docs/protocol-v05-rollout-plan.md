# Protocol v0.5 Cross-Component Rollout Plan

Status: core implementation and production cutover complete; independent
specification review approved. Project Hermes Listener/worker and daily
dispatcher protocol migration are deployed; the WeCom at-most-once send journal remains operational hardening.

Status date: 2026-07-19.

This plan implements the contract in `task-lifecycle-v05.md`. Protocol v0.4 is
a completed immutable baseline. Its docs, schemas, examples, tests, production
evidence, database archive, and local workspaces remain intact.

## 1. Confirmed Constants And Boundaries

```text
Task states = open / completed / expired / failed
Message states = pending / delivered / failed
Outbox states = queued / inflight / acked / retry_wait / exhausted
MAX_DELIVERY_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = [60, 300, 600]
DELIVERY_ACK_LEASE_SECONDS = 60
LISTENER_READINESS_PUBLISH_INTERVAL_SECONDS = 60
LISTENER_READINESS_MAX_AGE_SECONDS = 300
MAX_VISIBILITY_BATCH_SIZE = 100
```

- Exactly two Agents, strict alternation, and one current Message are supported.
- Task execution progress stays local. No claimed, working, or human-waiting
  state is added to Relay.
- Task, Message, and outbox fields are authoritative only on their own objects.
  Diagnosis is Server-computed and read-only.
- `cancelled` and `archived` remain reserved. Task hard deletion remains
  forbidden.
- The maintenance window replaces every v0.3/v0.4 mutation path with v0.5.
  There is no silent downgrade and no active legacy Task continuation.

## 2. Repositories And Runtime Ownership

| Area | Source or runtime | Ownership |
| --- | --- | --- |
| Relay, dashboard, protocol | `ZilingXie/agentRelay` | Server authority |
| MCP, Listener, workspace, Inbox UI | `ZilingXie/agent-relay-mcp` | Client authority |
| Hermes deploy source | `ZilingXie/heremes-deploy` | Deploy-layer source; non-secret Git backup |
| Hermes runtime copy | `/home/ubuntu/projects/hermes/project-hermes-worker` | `ubuntu` user systemd runtime |
| Hermes Listener | `project-hermes-worker.service` | active, restart-always |
| Hermes dispatcher | `project-hermes-daily-dispatch.service/.timer` | weekday 10:00 Asia/Shanghai |
| Public plan | `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` | canonical public roadmap |

The Hermes deploy repository currently has production-used uncommitted changes.
The deployed Listener and dispatcher hashes match that dirty source, while the
tracked worker unit contains an obsolete runtime path. These files are
pre-existing production state, not disposable changes.

## 3. Task 0: Core Implementation Gate

Core v0.5 implementation starts after the planning publication gate. Hermes
baseline preservation remains mandatory before production cutover, but does not
block Server/Client implementation or rehearsal with production mutations
closed.

1. Merge Server planning changes while preserving all v0.4 files.
2. Merge Client planning changes while preserving v0.4 tools/tests/workspaces.
3. Add the v0.5 section to the canonical public plan and publish it; mark v0.4
   as completed baseline and v0.5 as planned, not implemented.
4. Keep production mutation mode closed throughout core implementation and
   rehearsal; do not deploy or modify Hermes in this workstream.

Exit evidence: Server, Client, and public plan updates are published; Server and
Client implementation branches start clean; public pages return 200 and show
v0.4 completed plus v0.5 planned.

## 4. Server Protocol And Public Schemas

Primary files:

```text
server/protocol_v05.py                 new protocol constants and validators
server/protocol_registry.py            v0.5 manifest/default cutover support
schemas/task-create-v05.schema.json
schemas/task-message-v05.schema.json
schemas/message-ack-v05.schema.json
schemas/message-delivery-fail-v05.schema.json
schemas/task-terminal-v05.schema.json
schemas/task-detail-v05.schema.json
schemas/task-visibility-v05.schema.json
schemas/task-visibility-batch-v05.schema.json
examples/protocol-v05/
docs/protocol-v05-conformance.md
```

Contracts must close enums, reject unknown fields where appropriate, define
stable errors, and use one response envelope. Mutation inputs carry
`idempotency_key`; current-state mutations carry Message id, turn, and
`expected_task_version`. The protocol bundle records all fixed constants.

Exit evidence: schema tests cover valid/invalid examples and the manifest names
v0.5 as accepted but not writable until the maintenance switch.

## 5. Server Storage And Cutover Tooling

Primary files:

```text
server/store_v05.py
scripts/export_protocol_retirement.py
scripts/init_v05_database.py
scripts/verify_v05_cutover.py
```

Create native v0.5 Task, Message, Agent Event, idempotency, readiness, audit,
follow-up, and registry schema. Required indexes cover current Message lookup,
due outbox work, per-Agent recovery, expiry, visibility batches, and lineage.
All Task references use `ON DELETE RESTRICT`; a raw-SQL Task delete trigger is
mandatory.

The v0.5 Agent registry adds explicit `enabled` and protocol-capability data.
`enabled` is Relay admission policy, not proof that a credential is valid or a
Listener is online. Persistent timestamps use Server-authored UTC Unix seconds.

Add `agent_listener_readiness` keyed by Agent with protocol/client/workspace
versions, current instance id, monotonically increasing readiness epoch,
transport, ready flag, observed time, and updated time. Startup registration
replaces the instance and increments the epoch; stale-epoch heartbeats and
shutdown writes are rejected. Readiness is operational admission data, never
Task or Message state.

Cutover tooling must:

1. open the old SQLite database read-only after backup;
2. export and hash a retirement report for every non-terminal legacy Task;
3. initialize a separate v0.5 database;
4. seed only validated Agent identity/access/enabled/capability records and
   initialize readiness empty;
5. prove no legacy collaboration/outbox row crossed databases; and
6. expose explicit read-only legacy Task/timeline/lineage endpoints.

Exit evidence: fixture migrations, archive reads, 410 mutation rejection,
registry-only import, hard-delete rejection, and backup/restore rehearsal pass.

## 6. Delivery Coordinator And State Machine

Primary files:

```text
server/store_v05.py
server/ws_app.py
server/delivery_coordinator.py
server/app.py
```

The WS service runs the due-Event coordinator. For each due Event it atomically
claims `queued/retry_wait -> inflight`, increments attempts, and either sends to
the authenticated target socket or records `listener_unavailable`. A successful
write starts the 60-second ACK lease. Lease expiry records the failure and
schedules +60/+300/+600 seconds, or exhausts attempt four.

Fake-clock acceptance uses two explicit timelines:

```text
no socket or immediate write failure:
claim/fail attempts at t=0, 60, 360, 960 seconds

socket write succeeds but every ACK lease expires:
claim attempts at t=0, 120, 480, 1140 seconds
lease failures at t=60, 180, 540, 1200 seconds
```

These are consequences of `next_retry_at = failed_at + backoff`; retry offsets
are never interpreted as offsets from Message creation.

HTTP recovery calls the same Store claim. It may return the target's existing
inflight Event without incrementing attempts, or win a due claim; it cannot
create a parallel lease. Late ACK, retry claim, expiry, terminal mutation, and
NACK races use conditional transactions with one winner. Server restart derives
all work from persisted `outbox_status`, `next_retry_at`, and `inflight_until`.

Informational Event exhaustion changes only that Event. Current transitionable
`message.pending` exhaustion atomically fails Event, Message, and Task.
The same loop conditionally expires open Tasks at `task_expires_at`; expiry and
delivery exhaustion race through one Store transaction winner.

Terminal expiry or Relay failure may cancel a never-claimed Event through
`queued -> exhausted`. It sets stable `exhaustion_reason`, leaves
`last_error=null`, and appends terminal audit rather than a delivery-attempt
failure.

Attempt failures store only the contract's stable `last_error` value. Sanitized
exception detail is appended to the audit Event and cannot drive diagnosis or
state transitions.

Exit evidence: fake-clock tests cover no socket, socket write failure, no ACK,
late ACK, reconnect recovery, HTTP/WS claim race, restart, attempt boundaries,
informational exhaustion, Task expiry, and attempt-four atomic failure.

## 7. Server APIs, Visibility, And Readiness

Primary files:

```text
server/app.py
server/timeline.py
server/protocol_v05.py
```

Add authenticated v0.5 operations for create, send Message, Message ACK,
non-retryable delivery NACK, complete, fail, follow-up, lineage, single
visibility, batch visibility, and Listener readiness publication. Generic
mutation routes switch once during maintenance. Explicit v0.3/v0.4 mutations
return `410 protocol_retired` afterward.

HTTP surface:

| Method and path | Purpose |
| --- | --- |
| `POST /agentrelay/api/tasks` | create native v0.5 Task and first Message |
| `GET /agentrelay/api/tasks/{task_id}` | full active Task plus complete ordered Message parts |
| `POST /agentrelay/api/tasks/{task_id}/messages` | submit the next alternating Message |
| `POST /agentrelay/api/workers/{agent_id}/messages/{message_id}/ack` | durable current-Message ACK |
| `POST /agentrelay/api/workers/{agent_id}/messages/{message_id}/delivery-fail` | guarded non-retryable NACK |
| `POST /agentrelay/api/tasks/{task_id}/complete` | requester completion |
| `POST /agentrelay/api/tasks/{task_id}/fail` | reason-authorized failure |
| `POST /agentrelay/api/tasks/{task_id}/followups` | create a root-linked Task |
| `GET /agentrelay/api/tasks/{task_id}/lineage` | active v0.5 lineage |
| `GET /agentrelay/api/tasks/{task_id}/visibility` | single diagnosis contract |
| `POST /agentrelay/api/task-visibility/batch` | batch diagnosis contract |
| `POST /agentrelay/api/workers/{agent_id}/readiness/register` | replace process instance and receive epoch |
| `POST /agentrelay/api/workers/{agent_id}/readiness` | epoch-guarded readiness heartbeat |
| `GET /agentrelay/api/legacy/tasks/{task_id}` | read-only original legacy snapshot |
| `GET /agentrelay/api/legacy/tasks/{task_id}/timeline` | read-only legacy timeline |
| `GET /agentrelay/api/legacy/tasks/{task_id}/lineage` | read-only legacy lineage |

Readiness endpoints require self Agent authentication. Visibility requires a
Task participant, while the admin dashboard uses its existing admin authority.
Stable conflicts include `protocol_v05_required`, `listener_not_ready`,
`stale_task_version`, `stale_message`, `stale_turn`,
`stale_readiness_epoch`, `protocol_retired`, and `invariant_violation`.

The full Task response conforms to `task-detail-v05.schema.json`. It orders by
turn, then requester-to-target before target-to-requester. Visibility remains a
small diagnosis response and cannot substitute for Message content fetch.

Readiness publishes every 60 seconds and expires at 300 seconds. Create requires
both enabled participants to advertise v0.5 and have fresh ready Listener
instances. Startup registers a new epoch, performs bundle, workspace, recovery,
and ACK/NACK self-checks, then reports ready. A prior process cannot write the
new epoch. Later unavailability uses delivery retry.

WS routing keys are `(agent_id, listener_instance_id, readiness_epoch)`. A stale
WS hello is rejected. Committing a new readiness registration immediately makes
the prior epoch ineligible for coordinator sends; the WS service observes the
registry change and closes the old socket. The coordinator rechecks the current
epoch from Store before every send, so cross-process notification delay cannot
route a Message to the replaced socket.

Visibility returns Task, current Message, transitionable outbox Event, stable
diagnosis, generated time, and diagnosis version. Batch responses preserve the
same item shape and report unknown/unauthorized items in `errors`; consumers may
not infer a replacement state. Batch input is an ordered de-duplicated list of
at most 100 Task ids; larger requests fail as a whole, while valid-size requests
return per-item authorization/not-found errors without hiding successful items.

Exit evidence: authority, concurrency, idempotency, error-code, batch-partial,
readiness freshness, disabled-Agent, and no-downgrade tests pass.

## 8. MCP Tools And Listener

Primary files:

```text
mcp/server.mjs
scripts/agentrelay-v05.mjs
scripts/protocol-sync.mjs
scripts/agentrelay-listener-core.mjs
scripts/listener.mjs
scripts/agentrelay-inbox-intake.mjs
scripts/install-listener-service.mjs
scripts/doctor.mjs
```

Add explicit v0.5 tool builders first. At cutover, generic create/send/complete/
fail/follow-up/visibility tools use v0.5; legacy mutation tools return a clear
retirement response. Keep legacy GET/timeline/lineage read-only.

Explicit tools are `agentrelay_create_task_v05`,
`agentrelay_send_message_v05`, `agentrelay_complete_task_v05`,
`agentrelay_fail_task_v05`, `agentrelay_create_followup_v05`,
`agentrelay_get_task_v05`, `agentrelay_get_task_lineage_v05`, and
`agentrelay_get_task_visibility_v05`.
Message ACK, NACK, readiness, and recovery remain Listener-internal operations,
not Agent reasoning tools.

For transitionable `message.pending`, Listener order is fetch complete Task and
Message, acquire workspace lock, persist workspace v2 atomically, read back and
verify, then send versioned ACK. Informational Events may use Event-only ACK.
Retryable or uncertain local errors send neither ACK nor NACK. Guarded NACK is
allowed only for positively non-retryable persistence failure.

At startup register the process instance and retain its readiness epoch. Publish
every 60 seconds with both values. `doctor` checks capability, readiness age,
workspace v2 write/read, recovery, and endpoint compatibility without creating
a business Task.

WS hello, HTTP recovery, Message ACK, and NACK carry the registered instance and
epoch. Stale processes cannot claim or mutate delivery. Listener fetches the
full Task response, persists all ordered Message parts, verifies the current
Message id/version, and only then ACKs.

Exit evidence: focused tests cover routing, durable-before-ACK, NACK guard,
duplicate/out-of-order Events, stale version, restart/recovery, readiness, and
retired protocol behavior; full `npm test` passes.

## 9. Workspace v2 And Inbox UI

Primary files:

```text
scripts/agentrelay-task-workspace.mjs
scripts/agentrelay-task-context-sync.mjs
scripts/agentrelay-mcp-task-actions.mjs
scripts/rebuild-task-index.mjs
scripts/agentrelay-inbox-ui.mjs
```

Use a distinct v0.5 namespace. Each Task directory stores the latest Task
snapshot, immutable per-Message records, visibility snapshot, sync metadata,
and local-only workflow/actions. Task columns do not grow per turn; Message
history grows as records. Never rewrite v0.3/v0.4 workspace roots.

Inbox UI renders separate Task and Message badges plus Server diagnosis. It
shows attempts, next retry, safe last error, direction, turn, version, deadline,
lineage, and stale sync. Filters use lifecycle and diagnosis. Actions are
enabled only from Server-provided guards/current evidence. Local archive remains
presentation-only and cannot delete or mutate Relay Task state.

Exit evidence: desktop/mobile UI tests cover all diagnosis values, long ids and
errors, no overlap, stale/offline state, legacy read-only display, action guards,
and workspace/index restart recovery.

## 10. Relay Dashboard And Operations

Primary files:

```text
dashboard/
server/app.py
scripts/agentrelay_admin.py
scripts/admin_dashboard_smoke_test.py
```

Dashboard and admin APIs consume the Server diagnosis function. Show Task,
Message, and outbox dimensions separately; include retry backlog, exhausted
Events, invariant violations, readiness age, protocol versions, and dispatcher
health. No dashboard code may reconstruct diagnosis from raw legacy fields.

Alerts cover invariant violations, due-work lag, exhausted transitionable
Events, repeated Listener unavailability, stale enabled Agents, archive access
failure, and dispatcher/report failure. WebSocket pushes remain metadata-only.

Exit evidence: API/UI smoke tests and authorization/secret-redaction checks pass.

## 11. Project Hermes Listener And Dispatcher

Source: `ZilingXie/heremes-deploy`. Runtime:
`/home/ubuntu/projects/hermes/project-hermes-worker`.

Before changes, preserve the production-used dirty baseline through the Task 0
gate. Never commit `.env`, jobs, logs, state, artifacts, sessions, or credentials.

Listener work:

1. replace legacy post-worker Event ACK with v0.5 Message-before-ACK intake;
2. publish capability/readiness every 60 seconds;
3. persist workspace v2 before triggering Hermes execution;
4. submit replies as v0.5 Messages with aggregate version and idempotency;
5. keep execution/dead-letter progress local; and
6. add service startup, restart, stale readiness, and rollback checks.

Listener status: complete on 2026-07-19 through
[`heremes-deploy#1`](https://github.com/ZilingXie/heremes-deploy/pull/1).
Production Task `task_c2265c1f935f43738d522b22bccc4b46` proved native v0.5
Message-before-ACK intake, workspace-v2 persistence, Hermes execution, v0.5
reply Message, requester Listener ACK, requester completion, and fully acked
outbox Events. Source/runtime hashes matched after deployment. The existing
dirty deploy baseline was not overwritten.

Dispatcher work:

1. replace hard-coded v0.3 payloads with v0.5 create and Relay-generated opaque
   Task ids; retain stable per-dispatch idempotency keys;
2. replace Task/timeline/Event delivery inference with batch visibility;
3. preserve the existing 45-minute observation window, 60-second polling,
   dry-run, sanitized diagnostics, and weekday timer;
4. report Completed, Failed, Expired, Delivery pending, Waiting for target, and
   Waiting for requester separately;
5. treat batch/API errors as report errors, never Task failures; and
6. add a durable per-schedule send journal. Record `send_started` before WeCom
   I/O and `sent` after a confirmed response. An uncertain result is not retried
   automatically; it requires operator reconciliation. This is intentional
   at-most-once behavior because the webhook has no idempotency contract.

Dispatcher protocol status: items 1-5 complete on 2026-07-19 through
[`heremes-deploy#3`](https://github.com/ZilingXie/heremes-deploy/pull/3).
The deployed dispatcher uses strict v0.5 create payloads, Server-generated Task
ids, full-content idempotency keys, an atomic local Task-id journal, and batch
visibility as its only status source. Thirteen Node tests include a fully offline
fake-Relay create/visibility/journal-replay E2E; a production read-only batch
probe returned `task_completed / delivered / acked`. A real private-workspace
Hermes dry-run was intentionally not executed without separate authorization.
Item 6 remains operational hardening.

Required regression fixtures: Zac delivered-but-waiting, Vivi not-delivered,
queued/inflight/retry wait, exhaustion, completed, expired, business failure,
partial batch, stale readiness, duplicate process start, uncertain WeCom result,
and dry-run with zero external sends.

Exit evidence: secret scan, diff check, source/runtime hash match, Node tests,
systemd unit verification, dry-run snapshot, and exact-runtime regressions pass.

## 12. Cross-Repository Conformance And Release Order

Merge and deploy in this order:

1. Task 0 Server/Client/public plan updates.
2. Server schemas/storage/state machine/readiness/visibility behind closed writes.
3. Server dashboard and cutover tooling.
4. Client MCP/Listener/workspace v2/Inbox UI.
5. Core cross-repository conformance and rehearsal with production mutations closed.
6. Preserve the user-approved Hermes baseline and upgrade its Listener/dispatcher.
7. Full maintenance rehearsal including Hermes.
8. Production maintenance cutover and E2E.

Server and Client use separate PRs. The Hermes Listener/worker is merged through
its independent repository PR after preserving the dirty baseline. The Hermes dispatcher protocol migration is merged through
its independent repository PR. The remaining WeCom send journal is a later hardening PR. Each
PR records its dependency and does not enable production writes by itself.

The full acceptance set is the lifecycle contract's conformance list plus:

- legacy suites remain green before retirement;
- no consumer reads legacy delivery fields for v0.5 diagnosis;
- a fully offline Listener reaches attempt four;
- readiness publishes at 60 seconds and expires at 300;
- every enabled Agent passes preflight;
- workspace v2 and legacy roots remain isolated;
- actual Hermes systemd runtime passes dry-run and regression fixtures; and
- WeCom duplicate/uncertain-send behavior matches the send journal contract.

## 13. Maintenance Window Runbook

Preparation:

1. announce maintenance and freeze Task creation;
2. stop dispatcher timer, Hermes/other Listeners, WS, and mutation APIs;
3. drain requests and record service/container/unit versions;
4. back up and hash the old database and auth/Agent registry source;
5. export/hash the non-terminal retirement report;
6. verify restore into an isolated location;
7. mount the old database read-only and initialize the v0.5 database;
8. deploy Server, Client, dashboard, Hermes, and dispatcher artifacts with
   production mutations closed;
9. start upgraded Listeners so they can register readiness while mutations stay
   closed; and
10. run readiness preflight, disabling unsupported/stale Agents.

The executable read-only gate is `scripts/protocol_v05_preflight.py`. It
combines runtime mode/current-manifest checks, admin readiness, the retirement
report, empty native collaboration tables, hard-delete and foreign-key
invariants. Run it in `closed` mode before writes open, then in `v05` mode with
`--allow-existing-collaboration` for later checks after real Tasks exist.

Validation before writes:

- health, protocol manifest, schemas, archive reads, and 410 legacy mutations;
- no legacy collaboration rows in v0.5;
- hard-delete trigger and foreign keys;
- a separate rehearsal database exercises delivery through all five outbox
  states and the Zac/Hermes create, ACK, response, ACK, requester complete, and
  follow-up flow;
- Inbox UI/dashboard diagnosis rendering; and
- Hermes dispatcher dry-run against rehearsal fixtures with no WeCom send.

After all checks pass, start the WS coordinator, switch production mutation mode
from `closed` to `v05`, run one controlled production two-Agent E2E, compare
visibility, and finally enable the dispatcher timer and one controlled real
report. Mutation mode is deployment configuration, not a mutable dashboard
control.

Rollback is allowed while production mutation mode is `closed` by restoring old
binaries/config and the verified old database; rehearsal data is not production
state. If mode is `v05` but no production collaboration mutation has committed,
operators may return it to `closed`, prove collaboration tables empty, and roll
back. The first successfully committed production Task, Message, ACK/NACK,
terminal, or follow-up mutation crosses the boundary; readiness/registry writes
do not. Old-database rollback is then forbidden. Set mutations to `closed` and
forward-fix unless a separately approved export/recovery window is executed.

## 14. Post-Cutover Observation And Completion

For the first 24 hours, review due-work lag, attempts by reason, exhausted
Events, invariant violations, readiness age, visibility errors, archive errors,
Hermes executions, and WeCom journal state. Keep the old database mounted
read-only and retain backup/report hashes according to operations policy.

Mark v0.5 implemented only after production E2E, the first scheduled Hermes
report, 24-hour observation, no unresolved invariant violation, merged PR links,
updated Server/Client/public plans, clean synchronized repositories, and a final
CodeGraph sync from the workspace root.
