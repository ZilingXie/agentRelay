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

## Core Client And Cross-Repository Evidence

Run from the Client repository:

```bash
npm test
npm run test:protocol:v05:e2e
```

The Client suite proves workspace v2 write/read verification, complete
Message-before-ACK persistence, guarded NACK, stale Event rejection,
non-recursive informational ACK, epoch-bound recovery, startup ACK/NACK endpoint
probes, local retirement of v0.3/v0.4 mutation tools, and Server-owned Inbox UI
diagnosis. The local cross-repository runner proves
create -> target ACK -> response -> requester ACK -> complete -> follow-up.

## Acceptance Matrix

`core-pass` means deterministic local evidence exists. `window-pending` means
the code path exists but the production maintenance environment is the required
evidence source. `hermes-deferred` remains outside this core implementation.

| # | Status | Evidence |
| --- | --- | --- |
| 1 | core-pass | Separate v0.4/v0.5 plans, schemas, tests, and workspace roots |
| 2 | core-pass | Store conformance atomic create |
| 3 | core-pass | Store/schema plus visibility tests |
| 4 | core-pass | Store and HTTP ACK guards |
| 5 | core-pass | Client intake and workspace v2 tests |
| 6 | core-pass | Store concurrency/idempotency tests |
| 7 | core-pass | Store/API stale context tests |
| 8 | core-pass | Store/API informational Event ACK tests |
| 9 | core-pass | Delivery fake-clock suite |
| 10 | core-pass | Delivery scheduler suite |
| 11 | core-pass | Store/delivery ACK lease race tests |
| 12 | core-pass | Store/delivery attempt-four tests |
| 13 | core-pass | Store/API guarded NACK tests |
| 14 | core-pass | Delivery restart schedule test |
| 15 | core-pass | Store plus Client direction guards |
| 16 | core-pass | Store and cross-repository E2E |
| 17 | core-pass | Store/API completion authority tests |
| 18 | core-pass | Store max-turn terminal-choice tests |
| 19 | core-pass | Store/delivery expiry race tests |
| 20 | core-pass | Store/API lineage tests |
| 21 | core-pass | Archive reads plus HTTP retirement probes for every legacy mutation |
| 22 | core-pass | Cutover retirement-report smoke |
| 23 | core-pass | Store, trigger, foreign-key, and surface checks |
| 24 | partial | Dashboard passes; dispatcher is `hermes-deferred` |
| 25 | window-pending | Local E2E passes; production two-Agent E2E is not run |
| 26 | window-pending | Admission rules pass; production enabled-Agent preflight is not run |
| 27 | hermes-deferred | Actual Hermes Listener/dispatcher regression is not run |
| 28 | core-pass | Server guarded NACK and Client retryable/no-NACK tests |
| 29 | core-pass | Cutover validated-registry-only smoke |
| 30 | core-pass | Delivery offline four-attempt fake-clock test |
| 31 | core-pass | Store/delivery/readiness timing and epoch tests |
| 32 | core-pass | Stable last-error and audit-detail tests |
| 33 | core-pass | Store/delivery exhaustion-reason audit tests |
| 34 | core-pass | Full Task schema/API and Client persist/read-back tests |
| 35 | core-pass | Store, WS replacement race, recovery, ACK/NACK epoch tests |

Production mutation mode remains closed. Core implementation is not production
completion: items 24-27 retain their stated release gates.
