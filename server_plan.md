# AgentRelay Server Plan

Audience: Codex and maintainers working in `/home/ubuntu/projects/agentrelay/agentRelay`.

Status date: 2026-07-19.

Latest update: Protocol v0.4 is a completed, production-verified historical baseline. The next implementation target is Protocol v0.5, which separates Task lifecycle, Message delivery, and Agent Event outbox truth and will replace active writes during a maintenance-window cutover. The v0.4 contract, schemas, examples, and verification record remain preserved.

## Purpose

This file is the server-side working plan for the AgentRelay relay project. It is for implementation planning, repository hygiene, validation notes, and server-specific next steps.

The user-facing overall project plan lives only at:

```text
/home/ubuntu/projects/stellarix-site/agentrelay/plan.html
```

Do not treat repo-local `plan.html`, `plan.md`, or `phase3-plan.md` as the canonical overall project plan. They are historical/project-local references unless explicitly refreshed for a task.

## Update Rule

After every completed change, and after any explicit planning pass that changes direction or priorities:

1. Update this `server_plan.md` with server-facing implementation status, next steps, and validation notes.
2. Update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` with the user-facing project status.
3. If the public page should reflect the change, sync the stellarix-site file to `/var/www/html/agentrelay/plan.html` and verify `https://server.stellarix.space/agentrelay/plan.html`.

## Current Server State

- AgentRelay is the server/cloud relay project: protocol authority, HTTP/WSS relay, SQLite state, auth, delivery reliability, audit/timeline, admin dashboard, Docker deployment, and public protocol assets.
- The client/agent-side MCP project remains separate: `/home/ubuntu/projects/agentrelay/agent-relay-mcp` and `https://github.com/ZilingXie/agent-relay-mcp`.
- The public canonical plan has a manual-style navigation shell. Static manual intro pages live under `/home/ubuntu/projects/stellarix-site/agentrelay/manual/` and are published under `/agentrelay/manual/`. The intro pages load shared assets from `/agentrelay/manual/assets/` for the persistent lightweight sidebar, compact typography, and smooth client-side document switching.
- Protocol v0.3 is the active contract. Public schemas, guide, examples, conformance docs, manifest, bundle, and validation endpoint are published.
- The relay remains intentionally small: route, persist, authorize, notify, audit, and enforce transport/state invariants. Local inbox and human workflow adapters belong outside the cloud relay.
- Agent roles are `personal_agent` and `service_agent`; permissions are expressed through `execution_mode`, `protocol_capabilities`, and `policy`.

## Completed Server Milestones

- Protocol v0.3 timeline, transitions, response envelopes, schemas, docs, examples, and conformance runner.
- Reliable agent event delivery with durable outbox, cursor reads, inflight/done states, WebSocket notification, and authenticated HTTP payload fetch.
- Source refs, approval summaries, requester-owned close semantics, human completion authority, and human-authorized goal amendment.
- Lightweight TTL expiry, max-turn loop protection, terminal cleanup, install loopback health checks, and close-flow reliability hardening.
- Third-party onboarding flow with disposable conformance identities.
- Read-only admin dashboard and local admin/debug CLI.
- Protocol negotiation and drift recovery, including server-owned protocol bundle metadata and structured stale-client repair instructions.
- Role-aware Agent Cards for personal/service agents.
- Public manual navigation for Protocol, guardrails, MCP install, MCP client, server, health checks, and roles, including lightweight outline styling and smooth manual-page switching.

## Phase 4 Server Plan

Phase 4 goal: support a usable real-agent worker product without making the relay heavy. MCP owns the Service Worker Kit runtime, while the server provides the durable state, visibility, lifecycle safety, and protocol maturity needed to operate those workers. Phase 4 server-side items are currently not started.

Server-side workstreams:

1. Dashboard observability
   - Show agent role, execution mode, protocol capabilities, and service-agent status.
   - Surface goal versions, task amendments, TTL/max-turn outcomes, protocol negotiation events, delivery states, and retry/backlog health.
   - Keep dashboard read-only until there is a clearly safe operator mutation model.

2. Service worker visibility
   - Ensure server APIs expose enough task/event state for MCP service workers to debug claim, lease, submit, ACK, retry, fallback, and terminal cleanup.
   - Add targeted live markers for worker-loop validation without leaking task payloads into WebSocket pushes.

3. Agent lifecycle operations
   - Improve onboarding, install health checks, token rotation, service-agent status inspection, and deactivation workflows.
   - Preserve secret hygiene: never print tokens, never commit runtime auth/data, and avoid public mutable admin APIs too early.

4. Protocol continuation semantics
   - Define child tasks and context continuation for follow-up, revision, and post-completion changes.
   - Keep terminal tasks terminal; related future work should create a new task under the same context.

5. Protocol compatibility maturity
   - Add/maintain conformance profiles for personal agents, service agents, and unavailable-agent paths.
   - Plan the v0.2 deprecation window once v0.3 capability reporting is common enough.

## Protocol v0.4 Task Lifecycle Plan (Completed Baseline)

The design, Relay, and MCP/Listener implementations are complete. The authoritative implementation contract is [`docs/task-lifecycle-v04.md`](docs/task-lifecycle-v04.md). A production two-Agent E2E passed; v0.3 remains the default only as a compatibility policy until participant capability advertisement supports automatic selection.

Current implemented status snapshot (verified 2026-07-18):

- `submitted`: implemented; Relay has validated and persisted the current Message and is waiting for the target Listener's durable Inbox ACK.
- `delivered`: implemented; only the versioned current-Message ACK can enter this state after local persistence.
- `completed`: implemented; only the requester may confirm the current delivered target response against `done_criteria`.
- `expired`: implemented; Relay applies the immutable Task deadline to any active Task.
- `failed`: implemented; Relay enforces the enumerated reason, actor, and source-state rules.
- `cancelled` and `archived`: not implemented as lifecycle states; they remain reserved vocabulary and are rejected by v0.4.
- Multi-turn `submitted`/`delivered` cycling, strict alternation, optimistic concurrency, idempotency, `max_turns`, follow-up lineage, lifecycle notifications, and the hard-delete prohibition are implemented.
- Rollout remains explicit: v0.4 is accepted but non-default; v0.3 remains the compatibility default until trustworthy participant capability advertisement exists.

Key decisions:

- Active states are `submitted`, `delivered`, `completed`, `expired`, and `failed`; `cancelled` and `archived` are reserved and unreachable in v0.4.
- One turn runs from requester Message through requester ACK of the target response. Target responses keep the turn number; requester follow-ups increment it.
- The Task stores a fixed current snapshot with `current_message_id`, independent `from_agent_id` / `to_agent_id`, `status_version`, `max_turns`, and one immutable `task_expires_at`.
- At `max_turns`, Relay rejects another turn and the requester explicitly chooses `completed` or `failed/max_turns_exhausted`.
- Follow-ups use opaque ids and only `root_task_id`; root Tasks self-reference and direct source is audited in `task.followup_created`.
- v0.4 forbids hard deletion of every Task at API, CLI, store, foreign-key, and database-trigger layers.
- v0.3 Tasks continue under v0.3; v0.4 activates only after server and both participants advertise support and pass conformance.

Implemented server behavior:

- Additive v0.4 Task/Message/Event persistence, optimistic concurrency, idempotent mutation records, immutable Task expiry, and strict two-Agent alternation.
- Current-message ACK semantics that alone transition `submitted` to `delivered`; informational status-event ACKs cannot recurse.
- Completion, reason-specific failure authority, max-turn rejection, follow-up lineage query, and accepted-but-non-default protocol negotiation.
- Store-level isolation rejects every legacy claim/status/thread/amend/artifact/delivery/close mutation against v0.4 Tasks; current-message delivery requires the versioned v0.4 Message ACK.
- No Task deletion surface plus `ON DELETE RESTRICT` references and a SQLite `BEFORE DELETE` trigger covering raw SQL attempts.
- Public v0.4 schemas/example/bundle and smoke/conformance runners; the full existing server suite remains green.

## Protocol v0.5 Two-Layer Lifecycle Plan

Status: Protocol v0.5 is active in production as of 2026-07-19. Server PR #51
and Client PR #44 delivered the core implementation; Server PRs #55-#57 fixed
post-write preflight, closed-mode informational ACK, and exhausted-event alert
semantics found during the maintenance window. Zac and Vivi are the enabled
production Agents, both publish fresh v0.5/workspace-v2 readiness, and the
production root/follow-up E2E completed successfully. The independent Project
Hermes Listener/worker upgrade is deployed through
[`heremes-deploy#1`](https://github.com/ZilingXie/heremes-deploy/pull/1) and
passed a production v0.5 two-Agent flow. Task
`task_c2265c1f935f43738d522b22bccc4b46` completed Zac -> Hermes ACK -> Zac ACK
-> requester complete with both Messages and every outbox Event acked. The daily dispatcher protocol migration is deployed
through
[`heremes-deploy#3`](https://github.com/ZilingXie/heremes-deploy/pull/3), using
native v0.5 create, Server batch visibility, and an atomic local Task-id journal.
Its offline exact-runtime E2E and production read-only visibility probe passed.
The 24-hour
observation window is still in progress. The authoritative design is
[`docs/task-lifecycle-v05.md`](docs/task-lifecycle-v05.md).

The v0.5 direction is a direct maintenance-window upgrade:

Detailed implementation and cutover gates are maintained in
[`docs/protocol-v05-rollout-plan.md`](docs/protocol-v05-rollout-plan.md).

- Task truth is only `tasks.status`: `open`, `completed`, `expired`, `failed`.
- Message truth is only `messages.delivery_status`: `pending`, `delivered`,
  `failed`.
- Agent Event truth is only `agent_events.outbox_status`: `queued`, `inflight`,
  `acked`, `retry_wait`, `exhausted`.
- Visibility diagnosis is computed and never persisted. Dashboard, dispatcher,
  MCP, Listener, workspace, and Inbox UI consume the same visibility API.
- One aggregate `task_version` protects current Message, turn, ACK, and terminal
  mutations. v0.5 does not add an independent delivery version.
- Each Message permits four total attempts: immediate delivery, then retries
  after 1, 5, and 10 minutes. Retry wait remains Message `pending`.
- Attempt-four failure atomically exhausts the outbox Event, fails the Message,
  and fails the Task. Non-retryable Listener persistence failure exhausts
  immediately.
- v0.5 removes Task-level delivery copies and renames Event delivery fields to
  outbox terminology to prevent multiple sources of truth.
- Maintenance stops old writers, backs up SQLite, exports an auditable
  retirement report, mounts the unmodified v0.3/v0.4 database read-only, and
  creates a clean v0.5 database. No legacy row is rewritten or copied into v0.5.
- v0.3/v0.4 history and workspaces remain read-only; every legacy mutation
  returns `410 protocol_retired`.
- Every enabled Agent must advertise v0.5 and fresh Listener readiness before
  writes open. Unsupported/stale Agents are disabled; create rejects incapable
  participants instead of silently downgrading.
- The Hermes repository/runtime ownership is identified. Its dirty production
  baseline remains preserved. The v0.5 Listener/worker and daily dispatcher are
  deployed from reviewed isolated worktrees without overwriting that baseline.
  The remaining WeCom send-journal work is operational hardening, not protocol
  compatibility.
- Task hard deletion remains forbidden.

Production maintenance evidence:

- the legacy database, deployment configuration, source bundles, and rollback
  image were backed up and hashed before the first v0.5 write;
- the exact 54-Task legacy retirement report matches the read-only archive;
- post-write preflight passes with two completed Tasks, four delivered Messages,
  zero invariant violations, restrictive foreign keys, and the hard-delete
  trigger intact;
- legacy mutation routes return `410 protocol_retired`;
- two historical exhausted informational Events remain as audit history but do
  not affect Task/Message truth and no longer raise a transitionable-event alert;
- after the first production v0.5 write, rollback to the legacy database is
  forbidden; incident response is `closed` plus forward-fix only.

Implementation order:

1. Merge and publish Server, Client, and public v0.5 planning updates while
   preserving v0.4 as completed.
2. Implement Server protocol/schema/Store/scheduler/API/archive/conformance.
3. Implement MCP/Listener tools, durable ACK, workspace v2, and Inbox UI.
4. Upgrade dashboard to the visibility contract and run core rehearsal with
   production mutations closed.
5. Preserve and upgrade Hermes in its later independent workstream.
6. Run the full cutover and rollback rehearsal including Hermes.
7. Execute the maintenance-window cutover and production E2E.

Project Hermes implementation workstream:

1. **Complete.** Preserve the existing dirty production baseline from
   `ZilingXie/heremes-deploy` on a reviewed task branch after secret scanning;
   reconcile its tracked worker unit with the verified
   `/home/ubuntu/projects/hermes/project-hermes-worker` runtime path.
2. **Complete in `heremes-deploy#1`.** Upgrade the Hermes Listener to v0.5
   capability/readiness, Message-before-ACK,
   guarded non-retryable NACK, workspace v2, and v0.5 response submission.
3. **Complete in `heremes-deploy#3`.** Replace dispatcher status inference
   and direct legacy-field reads with the Server batch visibility contract. Use
   native v0.5 create with Server-generated Task ids and stable idempotency.
4. **Complete in `heremes-deploy#3`.** Report Completed, Failed, Expired,
   Delivery pending, Waiting for target
   response, and Waiting for requester decision separately. Surface stable
   diagnosis/reason and safe retry details; classify API/partial-batch failures
   as report errors, never Task failures.
5. **Partially complete.** Dry-run output, full-content idempotency, atomic
   Task-id journal, and visibility report errors are implemented. The explicit
   WeCom `send_started/sent` journal and production metrics remain hardening work.
6. Gate cutover on Zac/Hermes, exhaustion, completed, expired, partial-batch, and
   duplicate-dispatch regressions against the exact deployed runtime, followed
   by one maintenance dry-run and the production two-Agent E2E.

## Protocol Automatic Upgrade

Status: implementation merged in Server PR
[`#61`](https://github.com/ZilingXie/agentRelay/pull/61) at `2a8c789` and Client
PR [`agent-relay-mcp#50`](https://github.com/ZilingXie/agent-relay-mcp/pull/50)
at `087bd2c`. Deployed to production on 2026-07-19; health, current manifest,
v0.5 bundle, authenticated negotiation, authority/origin, revision, and digest
were verified with mutation mode preserved at `v05`.

- Relay publishes version-specific schema and canonical bundle digests, stable
  authority/origin metadata, bundle revision, and non-programmable semantic
  operation adapters.
- `POST /agentrelay/api/protocols/negotiate` returns `up_to_date`, `hot_patch`,
  `client_release_required`, or `hot_rollback` from registry-owned compatibility
  and runtime capability data. Clients do not compare version strings.
- Hot adapters are restricted data mappings. Identity, approval, authorization,
  idempotency, endpoint allowlists, and local side effects remain in MCP core.
- Lifecycle, transport, persistence, approval, or local execution changes still
  require an MCP code release; bundle updates cover compatible wire changes.
- Server schema, negotiation smoke, Client runtime/MCP smoke, and a cross-repo
  HTTP negotiation check gate rollout before any bundle is required.
- Verified 2026-07-19 with the full Server and Client suites plus a real HTTP
  negotiation that activated the v0.5 bundle and assembled all five semantic
  operation payloads.
- Production verification returned bundle revision `1` and digest
  `sha256:f7467985fe7444a96f4699d155d0d4a2cd64f64c8f07f813deff2e9d2bf6eb9d`.

The detailed contract is [`docs/protocol-auto-upgrade.md`](docs/protocol-auto-upgrade.md).

## Guardrail Hardening

Status: Server PR [`#64`](https://github.com/ZilingXie/agentRelay/pull/64) and
Client PR
[`agent-relay-mcp#54`](https://github.com/ZilingXie/agent-relay-mcp/pull/54)
are merged. Relay and Zac are deployed on adapter v2 revision `2`. The full
Server suite, Client 204-test suite plus MCP smoke, and cross-repository HTTP
hot-patch/up-to-date E2E passed. Hermes policy enforcement merged in
[`heremes-deploy#4`](https://github.com/ZilingXie/heremes-deploy/pull/4), with
worker-process positive/negative coverage in
[`#5`](https://github.com/ZilingXie/heremes-deploy/pull/5) and
[`#6`](https://github.com/ZilingXie/heremes-deploy/pull/6). Relay, Zac, and
Hermes are deployed and verified. Automatic protocol upgrade is one Guardrail
subsystem.

- Server publishes adapter contract v2, `local_authorization_v1`, immutable
  revision `2`, schema/bundle digests, and a bounded publication/expiration
  window. Negotiation returns `client_release_required` when Server hot update
  is disabled or the client lacks any compiled capability.
- MCP Core, not Relay data, owns identity, Task context, authorization,
  idempotency, routes, lifecycle transitions, persistence, and local side
  effects. Adapter mappings must match the Core's exact operation and slot
  contract and cannot contain executable behavior.
- Human mutations require a Local Inbox approval record bound to the exact
  action, payload, Task context, expiry, and confirmation reference. Direct v0.5
  create is disabled by default and uses reviewed-draft Send.
- Hermes receives only two service-policy authorities: bounded reply to its
  current delivered Message and `agent_reported_failure`. Create, complete,
  follow-up, goal/participant changes, requester authority, other reasons, and
  local side effects are denied.
- Relay remains the trusted protocol publisher; independent signing/KMS is
  deferred. Local approval does not claim protection from a malicious process
  with same-user filesystem write access.
- Release order is Server then Client then the independently preserved Hermes
  deploy source. Production verification covers Zac and Hermes, not Vivi, and
  includes positive/negative authorization, hot patch, malicious-bundle reject,
  last-known-good, authorized rollback, and both emergency-disable switches.

## Guardrail Verification Record

- Hermes was deployed from an isolated clean worktree without changing the
  dirty canonical Agent overlay baseline. Runtime worker/policy hashes match
  merged source; the service is active with zero restarts and Relay reports it
  ready/fresh on client `0.5.0`, workspace `2`.
- Production Task `task_da25ff6e44ca41d981a7182afd4b0e06` completed at
  version `5` after delivered Zac request, exact Hermes
  `HERMES_GUARDRAIL_ACK_20260719`, and Zac completion through the one-time Local
  Inbox approval path. Worker-process E2E proves allowed reply/failure and zero
  Relay POSTs for close, legacy-complete replay, and actor-tampered replay.
- Production Relay publishes revision `2` with digest
  `sha256:ba842be162628c7cc137914220dca2582dd2259db28e9192e3dce8c0afcc7f36`;
  Zac is `up_to_date` and passed `doctor` after service restart.

## Structured Message Subject And Dynamic Agent Tools

Status: implemented on task branch; pending Server/Client PRs, staged deployment,
client upgrades, bundle activation, and production verification.

- A UI-only subject belongs to the first Message of a new Task, never to Task
  state. Follow-ups provide a new subject; ordinary replies contain only parts.
- v0.5 Message persistence adds nullable `subject` and `metadata_json`; existing
  rows migrate to null and remain readable without Task backfill.
- The compatible deployment publishes optional wire subject under adapter
  contract v1. After MCP upgrades, `AGENTRELAY_DYNAMIC_AGENT_TOOLS_ENABLED=1`
  publishes contract v2/revision 4 and requires
  `dynamic_agent_tool_schema_v1`.
- The signed bundle may update only the fixed create/reply/follow-up tool input
  Schemas and descriptions. Local runtime code retains identity, approval,
  operation/route allowlists, protected slots, LKG, and rollback authority.
- Create/follow-up pre-register a bounded, non-authoritative
  `/message/metadata` slot. A signed bundle may add optional public fields only
  inside that container; ordinary reply remains `taskId + parts`.
- Release gate: full Server and Client suites, malicious-bundle coverage,
  process-level MCP smoke, cross-repo hot patch, and create/reply/complete/
  follow-up E2E must pass before activation.

## Active Next Steps

- Complete the 24-hour production observation window and record readiness,
  delivery backlog, invariant, dashboard, and Inbox UI results.
- Keep `scripts/protocol_v05_preflight.py --allow-existing-collaboration` as the
  post-write production verification gate. Any incident must first switch
  mutations to `closed`, then be repaired forward.
- Observe the first scheduled Hermes v0.5 daily dispatch and add the remaining
  WeCom at-most-once send journal plus production visibility/readiness metrics.
- Support the MCP Service Worker Kit with enough server/dashboard visibility to debug worker runs end to end.
- Validate notifier-first personal-agent flows and service-agent worker flows with more real remote agents.
- Make dashboard views show agent role, execution mode, protocol capabilities, service-agent status, goal versions, amendment events, TTL/max-turn outcomes, and protocol negotiation events clearly.
- Add production-grade observability for event backlog, retry health, protocol negotiation frequency, install-loopback failures, and live service-agent traffic.
- Define child-task/context continuation semantics for post-completion follow-up and revision workflows.
- Plan a v0.2 deprecation window after enough clients advertise v0.3 capability.
- Add trustworthy participant capability advertisement and negotiated automatic protocol selection; treat any default-version change as a separate rollout decision.

## Protocol v0.4 Verification Record

- Server implementation PR: `#42`; production metadata fixes: `#43` and `#44`; deterministic conformance evidence: `#46`; legacy-mutation isolation and acceptance hardening: `#47`.
- Client implementation PR: `agent-relay-mcp#37`; follow-up workspace fix: `#38`; repeatable E2E runner: `#39` and `#40`.
- Production root Task `task_255b7b51f9364697a6e599c45ea2d496` reached `completed` after both Listener ACK boundaries.
- Follow-up Task `task_842009bd133a4a2bbe48ebde9af8e0e4` shared the root lineage and was explicitly terminated as `failed/agent_reported_failure` after verification.

## Validation Notes

- Docs-only changes: inspect changed text and run `git diff --check`.
- Schema/protocol doc/example changes: run `npm run test:schema`, plus the relevant protocol smoke/conformance test if semantics changed.
- Server behavior/auth/task lifecycle/event delivery/dashboard/deployment changes: run `npm test` unless a targeted subset is clearly sufficient.
- Docker/runtime changes: rebuild/restart with `sudo docker compose up -d --build`, verify health, and verify a task-specific live marker.
- Public plan changes: update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`, sync it to `/var/www/html/agentrelay/plan.html` when public publication is expected, and verify the public URL.
