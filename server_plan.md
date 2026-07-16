# AgentRelay Server Plan

Audience: Codex and maintainers working in `/home/ubuntu/projects/agentrelay/agentRelay`.

Status date: 2026-07-16.

Latest update: The Protocol v0.4 server implementation is complete on its task branch and passes the full server suite plus all 16 v0.4 conformance checks. Protocol v0.3 remains the default until the MCP/Listener implementation and live two-Agent E2E pass.

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

## Protocol v0.4 Task Lifecycle Plan

The design and server implementation are complete. The authoritative implementation contract is [`docs/task-lifecycle-v04.md`](docs/task-lifecycle-v04.md). MCP/Listener implementation and live two-Agent E2E remain pending, so v0.3 is still the default.

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
- No Task deletion surface plus `ON DELETE RESTRICT` references and a SQLite `BEFORE DELETE` trigger covering raw SQL attempts.
- Public v0.4 schemas/example/bundle and smoke/conformance runners; the full existing server suite remains green.

## Active Next Steps

- Support the MCP Service Worker Kit with enough server/dashboard visibility to debug worker runs end to end.
- Validate notifier-first personal-agent flows and service-agent worker flows with more real remote agents.
- Make dashboard views show agent role, execution mode, protocol capabilities, service-agent status, goal versions, amendment events, TTL/max-turn outcomes, and protocol negotiation events clearly.
- Add production-grade observability for event backlog, retry health, protocol negotiation frequency, install-loopback failures, and live service-agent traffic.
- Define child-task/context continuation semantics for post-completion follow-up and revision workflows.
- Plan a v0.2 deprecation window after enough clients advertise v0.3 capability.
- Merge and deploy the verified Protocol v0.4 server compatibility implementation, then implement the MCP/Listener side; do not make v0.4 the default until end-to-end conformance passes.

## Validation Notes

- Docs-only changes: inspect changed text and run `git diff --check`.
- Schema/protocol doc/example changes: run `npm run test:schema`, plus the relevant protocol smoke/conformance test if semantics changed.
- Server behavior/auth/task lifecycle/event delivery/dashboard/deployment changes: run `npm test` unless a targeted subset is clearly sufficient.
- Docker/runtime changes: rebuild/restart with `sudo docker compose up -d --build`, verify health, and verify a task-specific live marker.
- Public plan changes: update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`, sync it to `/var/www/html/agentrelay/plan.html` when public publication is expected, and verify the public URL.
