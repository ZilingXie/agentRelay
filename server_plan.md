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

Status: core design confirmed; specification review in progress; implementation
not started. No v0.5 code work starts until the Server, Client, and public
planning updates are merged and published. The authoritative design is
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
- The Hermes repository/runtime ownership is now identified. Task 0 must
  preserve and reconcile its dirty production baseline; visibility-API
  regression against that exact runtime remains a cutover blocker.
- Task hard deletion remains forbidden.

Implementation order:

1. Merge and publish Server, Client, and public v0.5 planning updates while
   preserving v0.4 as completed.
2. Locate and record the deployed Hermes dispatcher runtime and owner.
3. Implement Server protocol/schema/Store/scheduler/API/archive/conformance.
4. Implement MCP/Listener tools, durable ACK, workspace v2, and Inbox UI.
5. Upgrade dashboard and dispatcher to the visibility contract.
6. Run full suites, cutover rehearsal, cross-repository conformance, and
   maintenance rollback rehearsal.
7. Execute the maintenance-window cutover and production E2E.

Project Hermes implementation workstream:

1. During Task 0, preserve the existing dirty production baseline from
   `ZilingXie/heremes-deploy` on a reviewed task branch after secret scanning;
   reconcile its tracked worker unit with the verified
   `/home/ubuntu/projects/hermes/project-hermes-worker` runtime path.
2. Upgrade the Hermes Listener to v0.5 capability/readiness, Message-before-ACK,
   guarded non-retryable NACK, workspace v2, and v0.5 response submission.
3. Replace dispatcher status inference and direct legacy-field reads with the
   Server batch visibility contract. Keep the dispatcher read-only with respect
   to Task lifecycle.
4. Report Completed, Failed, Expired, Delivery pending, Waiting for target
   response, and Waiting for requester decision separately. Surface stable
   diagnosis/reason and safe retry details; classify API/partial-batch failures
   as report errors, never Task failures.
5. Add dry-run output, per-window dispatch idempotency, duplicate-send
   prevention, and metrics/alerts for visibility, WeCom send, and stale Listener
   readiness failures.
6. Gate cutover on Zac/Vivi, exhaustion, completed, expired, partial-batch, and
   duplicate-dispatch regressions against the exact deployed runtime, followed
   by one maintenance dry-run and the production two-Agent E2E.

## Active Next Steps

- Complete and publish Task 0 planning updates for Protocol v0.5 without
  replacing any v0.4 contract or evidence.
- Break the approved v0.5 design into separate Server, Client, UI, dispatcher,
  and maintenance implementation plans and PRs.
- Locate the deployed Hermes Listener and dispatcher ownership/runtime details,
  then execute the dedicated Hermes v0.5 workstream before cutover.
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
