# AgentRelay Protocol v0.5 Conformance

Status: implementation in progress. This page records verified evidence and
must not be read as production-cutover approval.

The authoritative acceptance list is section 20 of
`docs/task-lifecycle-v05.md`. The implementation is complete only when every
item is covered by Server, Client, cross-repository, and maintenance-rehearsal
evidence.

## Phase 2 Evidence

Run:

```bash
npm run test:protocol:v05:store
npm run test:protocol:v05:cutover
npm run test:schema
npm test
```

The native Store conformance currently proves:

- the v0.5 `tasks` table has `task_version` and no Task delivery/status-version
  copy;
- enabled/capability/readiness admission and 300-second freshness;
- monotonically increasing Listener epochs and stale-epoch rejection;
- atomic Task/Message/outbox creation and create idempotency under concurrency;
- strict two-Agent alternation and ordered multi-Message history;
- Message-before-ACK state transitions, duplicate ACK idempotency, and one
  winner for competing ACKs;
- fixed four-attempt delivery with 60/300/600-second retry waits;
- atomic attempt-four Task/Message/Event failure;
- guarded `listener_persistence_failed` terminalization;
- requester-only completion against current delivered target evidence;
- immutable expiry, follow-up lineage, and hard-delete rejection.

The cutover smoke currently proves:

- the retirement report exactly enumerates legacy non-terminal Tasks;
- the v0.5 database is initialized separately;
- only the validated Agent registry is imported;
- readiness, Task, Message, Event, audit, and idempotency tables start empty;
- legacy Task delivery columns are absent; and
- the raw-SQL hard-delete trigger exists.

All pre-existing v0.3/v0.4 and Server tests remain green at this checkpoint.

## Phase 3 Evidence

Run:

```bash
npm run test:protocol:v05:api
npm run test:protocol:v05:delivery
npm run test:protocol:v05:ws
npm run test:admin-dashboard
```

Current Phase 3 evidence proves:

- `legacy`, `closed`, and `v05` mutation-mode separation;
- authenticated full Task, lineage, single/batch visibility, readiness,
  Message, ACK/NACK, terminal, and follow-up HTTP behavior;
- participant authorization and per-item batch errors;
- legacy mutation 410 behavior in v0.5 mode;
- no-socket and successful-write/no-ACK four-attempt fake-clock timelines;
- persisted lease expiry, retry scheduling, process-restart continuity, and
  attempt-four atomic failure;
- non-recursive informational Event recovery and idempotent ACK behavior;
- informational Event retry exhaustion without Task or Message mutation;
- epoch-fenced WS hello, metadata-only Message delivery, old-socket closure,
  and replacement-socket routing;
- dashboard projections sourced from Server diagnosis, readiness, and outbox
  state; and
- desktop plus 390px rendering without page-level overflow.

## Pending Evidence

The following remain required before v0.5 can be called implemented:

- MCP/Listener Message-before-ACK and workspace v2 durability;
- Inbox UI desktop/mobile verification;
- cross-repository two-Agent create/ACK/response/ACK/complete/follow-up E2E;
- maintenance rehearsal and later Hermes/production cutover evidence.

Production mutation mode remains closed.
