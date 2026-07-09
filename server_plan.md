# AgentRelay Server Plan

Audience: Codex and maintainers working in `/home/ubuntu/projects/agentrelay/agentRelay`.

Status date: 2026-07-09.

Latest update: The user-facing plan now keeps the active roadmap ahead of historical context by moving the Completed sections directly above References.

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

## Active Next Steps

- Support the MCP Service Worker Kit with enough server/dashboard visibility to debug worker runs end to end.
- Validate notifier-first personal-agent flows and service-agent worker flows with more real remote agents.
- Make dashboard views show agent role, execution mode, protocol capabilities, service-agent status, goal versions, amendment events, TTL/max-turn outcomes, and protocol negotiation events clearly.
- Add production-grade observability for event backlog, retry health, protocol negotiation frequency, install-loopback failures, and live service-agent traffic.
- Define child-task/context continuation semantics for post-completion follow-up and revision workflows.
- Plan a v0.2 deprecation window after enough clients advertise v0.3 capability.

## Validation Notes

- Docs-only changes: inspect changed text and run `git diff --check`.
- Schema/protocol doc/example changes: run `npm run test:schema`, plus the relevant protocol smoke/conformance test if semantics changed.
- Server behavior/auth/task lifecycle/event delivery/dashboard/deployment changes: run `npm test` unless a targeted subset is clearly sufficient.
- Docker/runtime changes: rebuild/restart with `sudo docker compose up -d --build`, verify health, and verify a task-specific live marker.
- Public plan changes: update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`, sync it to `/var/www/html/agentrelay/plan.html` when public publication is expected, and verify the public URL.
