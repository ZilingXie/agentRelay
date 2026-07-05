# AgentRelay Phase 3 Plan: Agent Collaboration Protocol

GitHub repository: https://github.com/ZilingXie/agentRelay

## 0. Decision

Phase 3 moves AgentRelay from "a working relay PoC" toward "a productizable agent collaboration protocol."

The important distinction:

```text
AgentRelay is the relay.
Agent Collaboration Protocol is how agents coordinate work.
```

AgentRelay connects local agents that do not have public IP addresses. It should provide durable transport, auth, state, notification, and audit. It should not become the semantic brain that decides what the humans want or whether the real-world task is complete.

## 0.1 Protocol v0.2 Implementation Status

Protocol v0.2 is implemented as an additive compatibility rollout.

- Server task creation accepts canonical `requester_agent_id`, `target_agent_id`, `done_criteria`, `completion_owner_agent_id`, and `pending_on_agent_id`.
- Server artifact submission accepts canonical `actor_agent_id`, artifact `intent`, `kind`, `parts`, and `pending_on_agent_id`.
- Legacy `from`, `to`, `message.role`, and `pendingOnHumanId` remain accepted for older clients.
- Auth checks use requester identity for task creation and actor identity for artifact submission.
- Audit events include `protocol_version`, `actor_agent_id`, `intent`, and pending ownership fields for v0.2 events.
- MCP clients prefer v0.2 payloads while keeping legacy input aliases.
- `scripts/protocol_v02_smoke_test.py` verifies v0.2 create/claim/artifact/close flow, auth rejection, and legacy create compatibility.

The SQLite schema is intentionally unchanged in this rollout. Existing columns remain the storage compatibility layer while v0.2 semantics are expressed through normalized payloads and audit event payloads.

## 0.2 v0.3 Planning Decisions

AgentRelay should borrow useful engineering patterns from Octo without adopting Octo's IM/workplace-centered philosophy.

Confirmed decisions:

- Keep AgentRelay agent-centered: humans are owners, authorities, and local decision sources behind agents.
- Keep private human-agent conversations outside relay audit by default.
- Artifact submission never completes a task automatically.
- Use completion authority option A: the configured completion owner agent executes the close call, while the close payload may record that a human made the final decision through that agent.
- Add optional `source_refs` for important artifacts, but do not require citation fields for simple tasks.
- Treat WebSocket delivery reliability as relay-to-local-agent event delivery, not as task semantics.
- Keep secrets out of push payloads; local agents fetch full payloads through authenticated HTTP.
- Merge JSON schema work with agent-first API/MCP/CLI output design so agents get predictable success, error, and next-action envelopes.

## 1. What Phase 1 And Phase 2 Already Define

Phase 1 defined the basic collaboration loop:

```text
requester human -> requester local agent -> relay task -> target local agent -> target human
target local agent -> artifact/result -> relay -> requester local agent -> requester human
```

Phase 2 defined the no-public-IP transport loop:

```text
local listener -> outbound WSS -> AgentRelay Cloud
AgentRelay Cloud -> durable task.pending event -> local listener
local listener -> precise HTTP claim/fetch/ack -> user-owned local workflow adapter
```

The implemented protocol now has these concepts:

- Agent identity and Agent Card.
- Task lifecycle with `task_id`, `context_id`, `status`, and `pending_on_agent_id`.
- Requester-defined `done_criteria`.
- Requester-side `completion_owner_agent_id`.
- Structured messages and artifacts with `actor_agent_id` and `intent`.
- Durable task events and agent events.
- Per-agent thread bindings for local workflow reuse.
- Manual HTTP recovery plus WebSocket notification.
- User-owned local adapter hooks for Codex App, Codex CLI, WeChat, Slack, or custom workflows.

The current protocol is documented in `docs/agent-collaboration-protocol-v0.md`.

## 2. Phase 3 Goal

Define and implement the first stable Agent Collaboration Protocol contract:

```text
ACP-v0 over AgentRelay transport
```

This contract should answer:

- How does one agent ask another agent to help solve a problem?
- How does the target agent accept, reject, consult its local owner, or return partial work?
- How does ownership transfer between agents?
- How does an agent say "my action is complete" without incorrectly closing the whole workflow?
- Who is allowed to close a task?
- How can human completion authority be recorded without making humans first-class relay participants?
- How do agents avoid infinite loops?
- How does this map to A2A without forcing the relay to be the agent brain?

## 3. Product Principle

AgentRelay should stay small and durable:

- route
- persist
- notify
- authorize
- audit
- enforce transport/state invariants

Local agents should remain responsible for:

- reasoning
- tool use
- asking humans
- evaluating `done_criteria`
- choosing local UX/workflow surfaces
- deciding whether to continue, close, reject, or create child tasks

This keeps the relay useful across Codex App, Codex CLI, WeChat, Slack, and future agent runtimes.

## 4. Protocol Objects To Stabilize

### Agent

Add explicit capability and policy metadata:

```text
agent_id
owner
display_name
description
capabilities
accepted_task_types
human_approval_policy
scopes
public_card_version
```

### Task

Stabilize task envelope:

```text
protocol_version
task_id
context_id
parent_task_id
task_type
subject
requester_agent_id
target_agent_id
status
done_criteria
completion_owner_agent_id
completion_authority
pending_on_agent_id
next_action
terminal_reason
ttl
max_turns
turn_count
idempotency_key
created_at
updated_at
```

### Message

Define message as agent communication input:

```text
message_id
task_id
context_id
actor_agent_id
intent
parts
confidentiality
created_at
```

### Artifact

Define artifact as agent action output:

```text
artifact_id
task_id
actor_agent_id
intent
kind
parts
source_refs
summary
next_status
pending_on_agent_id
next_action
created_at
```

### Completion Authority

Represent final authority without requiring the relay to manage human login or private human-agent conversation:

```text
task_id
closed_by_agent_id
completion_authority.type       # agent | human
completion_authority.owner_id
completion_authority.via_agent_id
completion_authority.approval_ref
terminal_reason
closed_at
```

The close API is still executed by `completion_owner_agent_id`. If the final decision came from a human, the agent records a redacted approval reference or summary in the close payload.

### Source Reference

Source references are optional evidence pointers for artifacts:

```text
type               # owner_confirmation | calendar_lookup | file | message | tool_result | external_url | other
label
summary
visibility         # public | redacted | private
uri
metadata
```

They explain where important artifact claims came from without forcing private local conversations into relay audit.

### Event

Define event names and required payload fields:

```text
task.created
task.claimed
task.status_updated
message.added
artifact.submitted
ownership.transferred
delivery.scheduled
delivery.completed
delivery.failed
task.closed
task.expired
task.rejected
child_task.created
```

## 5. State Machine To Implement

Phase 3 should make this table explicit and enforce it in code.

Candidate states:

```text
submitted
claimed
working
waiting_remote
delivery_pending
input_required
auth_required
completed
rejected
failed
expired
cancelled
```

Terminal states:

```text
completed
rejected
failed
expired
cancelled
```

Core transition rules:

- `submitted -> claimed` only by `pending_on_agent_id`.
- `claimed -> working | input_required | auth_required`.
- `artifact.submitted` may transfer ownership to another agent.
- A non-terminal transition must set `pending_on_agent_id`.
- A non-terminal transition must set `next_action`.
- A terminal transition must set `terminal_reason`.
- Only `completion_owner_agent_id` may execute close as `completed`.
- Close may include `completion_authority.type = human` when the final decision came from the owner through the completion owner agent.
- Any terminal task rejects normal follow-up messages; create child tasks instead.
- `turn_count` must increment on cross-agent handoff.
- `max_turns` and `ttl` must be enforced.

## 6. A2A Relationship

Phase 3 should align with A2A but not block on full A2A coverage.

Mapping target:

```text
AgentRelay Agent Card -> A2A Agent Card subset
AgentRelay Task       -> A2A Task-like lifecycle
AgentRelay Message    -> A2A Message parts
AgentRelay Artifact   -> A2A Artifact-like output
AgentRelay Events     -> A2A task status/history/push notification mapping
```

Implementation stance:

- Keep current MCP/local listener as the practical client surface.
- Add protocol fields and validation first.
- Add A2A-compatible endpoint shapes after internal semantics are stable.
- Treat A2A as interoperability mapping, not as a reason to make the relay decide semantic completion.

## 7. Optimized Phase 3 Implementation Roadmap

1. [x] Create `phase3-plan.md`.
2. [x] Document the current Phase 1/2 communication protocol in `docs/agent-collaboration-protocol-v0.md`.
3. [x] Implement Protocol v0.2 compatibility layer and audit payload normalization.
4. [x] Schema and agent-first envelope: create JSON schemas for task create, artifact submit, close, and agent events; standardize MCP/API/CLI responses with `ok`, `data`, `error`, `hint`, and `next_action`.
5. [x] Timeline and audit model: refactor task events into a clearer dashboard-ready task timeline/activity log.
6. [x] Transition validator and completion authority: enforce legal state changes, terminal rules, max turns, TTL, close permissions, and human completion authority via agent.
7. [x] Reliable event delivery: add idempotency keys, event cursor support, local delivery states for `dedup`, `inflight`, `done`, and ack semantics; keep secrets out of push payloads.
8. [x] Source refs and approval summaries: add optional `source_refs` and redacted approval summaries for important artifacts and closes.
9. [x] Expand Agent Cards and A2A mapping: add capabilities, accepted task types, scopes, approval policy, and a minimal A2A compatibility map.
10. [x] Add admin/debug views or CLI for agents, tasks, timelines, events, and pending work.
11. [x] Add a Protocol v0.3 conformance-gated onboarding flow for third-party agents.
12. [x] Run a real two-agent Protocol v0.3 flow through AgentRelay and verify the full close/cleanup loop.
13. [x] Polish requester-side MCP completion decisions and human completion authority.
14. [x] Close flow reliability polish: stable task event ordering, idempotent install loopback checks, healthcheck TTL cleanup, local inbox workflow binding, and latest-artifact close evidence refs.
15. [ ] Continue validating the new local inbox workbench end-to-end with more real remote agents.

## 8. First Implementation Slice

Status: completed for server-side Protocol v0.3 opt-in behavior.

Implemented outputs:

- `schemas/task-create.schema.json`
- `schemas/artifact-submit.schema.json`
- `schemas/task-close.schema.json`
- `schemas/task-event.schema.json`
- `schemas/agent-event.schema.json`
- `schemas/task-timeline.schema.json`
- `schemas/agent-card.schema.json`
- `schemas/response-envelope.schema.json`
- `schemas/part.schema.json`
- `schemas/source-ref.schema.json`
- `schemas/README.md`
- `server/protocol_v03.py`
- Protocol v0.3 request validation for create/artifact/close.
- Agent-first response envelopes for v0.3 requests and `X-AgentRelay-Envelope: v0.3` clients.
- Structured error envelopes with `type`, `code`, `message`, `hint`, and `detail.field`.
- `scripts/protocol_v03_smoke_test.py`.
- `scripts/schema_conformance_test.mjs`.
- `docs/protocol-v03.md`.
- `docs/protocol-v03-conformance.md`.
- `examples/protocol-v03/*.json`.
- `scripts/protocol_v03_conformance_runner.py`.
- `npm run test:protocol:v03`.
- `npm run test:protocol:v03:conformance`.
- `npm run test:schema`.

Compatibility note: legacy/v0.2 clients still receive the existing response shape unless they explicitly request the v0.3 envelope.

## 8.0.1 Protocol Guide And Examples

Status: completed.

Implemented outputs:

- Published `docs/protocol-v03.md` as the agent-facing v0.3 guide.
- Added validated examples for:
  - meeting task create
  - meeting artifact submit
  - meeting task close
  - dashboard/work artifact flow
  - unavailable-agent fallback
- Extended schema conformance testing so example payloads are validated against
  the public schemas.
- Published docs and examples through the relay under:
  - `/agentrelay/docs/protocol-v03.md`
  - `/agentrelay/docs/protocol-v03-conformance.md`
  - `/agentrelay/examples/protocol-v03/*.json`

## 8.0.2 Protocol Conformance Runner

Status: completed.

Implemented outputs:

- Added `scripts/protocol_v03_conformance_runner.py`.
- Runner supports a temporary local relay for CI/development.
- Runner supports real relay verification with two disposable agent identities.
- Checks health, create, target event, precise claim, artifact, requester event,
  requester claim, close, task events, timeline, redacted source refs, and
  secret-safe event payloads.
- Added `npm run test:protocol:v03:conformance`.

## 8.1 Timeline And Audit Slice

Status: completed as a derived timeline view over existing `task_events`.

Implemented outputs:

- `server/timeline.py`
- `Store.get_timeline(task_id)`
- `GET /agentrelay/api/tasks/:taskId/timeline`
- MCP tool `agentrelay_get_timeline`
- `schemas/task-timeline.schema.json`
- Timeline entries with stable fields for dashboard use:
  - `timeline_id`
  - `sequence`
  - `event_type`
  - `category`
  - `title`
  - `summary`
  - `actor_agent_id`
  - `intent`
  - `artifact_id`
  - `pending_on_agent_id`
  - `source_refs`
  - `completion_authority`
  - `delivery`
  - raw `payload`
- v0.3 smoke coverage confirms source refs and human completion authority appear in timeline.
- MCP smoke coverage confirms agents can fetch the normalized timeline.

Compatibility note: this slice does not migrate or replace `task_events`. The timeline is derived from the existing append-only audit events, so existing data remains readable and existing clients can still use `agentrelay_get_events`.

## 8.2 Transition Validator And Completion Authority Slice

Status: completed as a centralized validator over the existing SQLite task model.

Implemented outputs:

- `server/transitions.py`
- Centralized task state constants for terminal, claimable, non-terminal, and known states.
- Claim validation for pending owner, claimable status, TTL, and already-claimed protection.
- Status update validation for explicit terminal reasons and required non-terminal pending owner plus `next_action`.
- Artifact validation that keeps artifacts non-terminal and requires ownership handoff context.
- `turn_count` / `max_turns` enforcement when `pending_on_agent_id` changes.
- Delivery validation for requester-thread delivery by the completion owner agent.
- Close validation that only allows `completion_owner_agent_id` to close the task.
- Human completion authority support through the closing agent, without making humans first-class relay participants.
- Terminal task protection for claim, artifact, delivery, and close operations.
- `scripts/transition_smoke_test.py`.
- `npm run test:transitions`.

Compatibility note: this slice keeps the existing database schema stable and keeps legacy/v0.2 clients working. Precise claim conflicts still return HTTP 409 for existing Phase 2 clients, while validator failures remain structured validation errors in newer protocol paths.

## 8.3 Reliable Event Delivery Slice

Status: completed as an additive agent event delivery layer.

Implemented outputs:

- Agent event delivery states: `pending`, `inflight`, `done`, and `failed`.
- Additive SQLite columns for `idempotency_key`, `delivery_attempts`, `inflight_until`, `done_at`, `failed_at`, and `last_error`.
- Durable cursor format over `(created_at, event_id)`.
- `GET /agentrelay/api/workers/:agentId/events` for cursor reads, state filters, and optional `claim=true` lease behavior.
- Backward-compatible ack endpoint that treats legacy ack statuses as `done`.
- Explicit `done` and `failed` ack semantics; failed events remain claimable.
- WebSocket delivery now claims events as `inflight` and sends secret-safe notification metadata plus `payloadRef`.
- MCP tools:
  - `agentrelay_list_agent_events`
  - `agentrelay_claim_agent_events`
  - `agentrelay_ack_agent_event`
- `scripts/reliable_event_smoke_test.py`.
- Reliable event checks included in `npm test`.

Compatibility note: this slice preserves existing `/pending`, `/claim`, and old event ack behavior. Full task content is still available through authenticated task fetch; push notifications avoid task body fields so future adapters can safely route notifications through local or third-party channels.

## 8.4 Source Refs And Approval Summaries Slice

Status: completed as additive normalization for artifact and close audit payloads.

Implemented outputs:

- Normalized `source_refs` for artifact submission and final close artifacts.
- Source ref visibility contract:
  - `public`: keep safe `uri` and `metadata`.
  - `redacted`: keep label/summary, hide `uri` and `metadata`, mark `redacted`.
  - `private`: keep only a safe label/summary marker, hide local details, mark `redacted`.
- Normalized `completion_authority` with optional redacted approval summary and `source_refs`.
- Protocol v0.3 validation for completion authority visibility and source ref object shape.
- MCP support for source refs and approval/final artifact JSON arguments.
- Smoke tests confirming redaction behavior in task events and timeline entries.

Compatibility note: this slice does not require new database columns. It sanitizes audit payloads at write time so relay history remains explainable without making private human-agent conversations part of the relay audit.

## 8.5 Agent Cards And A2A Mapping Slice

Status: completed as A2A-shaped discovery and compatibility mapping.

Implemented outputs:

- Expanded per-agent cards at `GET /agentrelay/api/agents/:agentId/card`.
- Agent card list endpoint at `GET /agentrelay/api/agents/cards`.
- Minimal A2A mapping endpoint at `GET /agentrelay/api/agents/:agentId/a2a-map`.
- Agent Cards now include:
  - A2A protocol version marker.
  - Supported interfaces for A2A-shaped HTTP and AgentRelay HTTP.
  - Capabilities including push notifications and state transition history.
  - Bearer auth scheme metadata.
  - Default input/output modes.
  - Skills with accepted task types, intents, examples, and approval requirements.
  - AgentRelay extension metadata for scopes, endpoints, and human approval policy.
- `schemas/agent-card.schema.json`.
- `scripts/agent_card_smoke_test.py`.
- MCP smoke coverage for expanded Agent Cards.

Compatibility note: this slice does not implement a full A2A JSON-RPC runtime. It intentionally documents the current HTTP relay mapping and discovery surface so future A2A gateway work can be added without overstating compatibility.

## 8.6 Admin Debug CLI Slice

Status: completed as a read-only local SQLite inspection CLI.

Implemented outputs:

- `scripts/agentrelay_admin.py`
- `docs/admin-cli.md`
- Commands:
  - `summary`: high-level counts for agents, tasks, pending owners, and events.
  - `agents`: list known agent registry rows.
  - `tasks`: list recent tasks with optional agent/status filters.
  - `task`: show one full task with messages, artifacts, and thread bindings.
  - `timeline`: show normalized task timeline.
  - `events`: list durable agent events with agent/state filters.
  - `pending`: list task work currently pending on one agent.
- Table output for quick SSH debugging.
- JSON output for scripts and future dashboard tooling.
- `scripts/admin_cli_smoke_test.py`.

Compatibility note: this is intentionally read-only and local to trusted machines with SQLite DB access. It does not add a public admin API or dashboard surface yet.

## 8.7 Third-Party Agent Onboarding Slice

Status: completed as a server-side admin workflow around the Protocol v0.3 conformance runner.

Implemented outputs:

- `scripts/onboard_agent.py`
- `docs/third-party-agent-onboarding.md`
- `scripts/onboarding_smoke_test.py`
- `npm run test:onboarding`
- Prepare step for two disposable conformance identities.
- Conformance step that runs the v0.3 protocol loop against a real relay without printing tokens.
- Promote step that creates the real agent identity only after conformance when `--require-conformance` is used.
- Private env file output under `data/local-env/`.
- Token-free onboarding manifest under `data/onboarding/`.

Compatibility note: this slice does not replace `scripts/create_agent_identity.sh`. The existing helper remains available for trusted existing users and token rotation; third-party integrations should use the conformance-gated onboarding flow.

## 8.8 Real Two-Agent Protocol v0.3 Flow

Status: completed with a real Zac Agent -> Project Hermes Agent task.

Validation evidence:

- Task: `task_aef35264c1824fd7b396f98ecd3f40ec`
- Requester agent: `zac-agent`
- Target agent: `project-hermes`
- Protocol version: `agent-collab-v0.3`
- Flow: create -> target pending event -> target claim -> artifact submit -> ownership transfer to requester -> requester close -> terminal event cleanup.
- Final task state: `completed`
- Final delivery state: `delivered`
- Related agent events: `done`

This validates the real cross-agent process rather than only the conformance
runner. The specific task was a dashboard title update, not a meeting request,
but it exercised the same protocol responsibilities: requester-defined
`done_criteria`, target-side work artifact, requester-side completion ownership,
and terminal cleanup.

Next focus: improve local MCP/listener/client adapters based on real-flow gaps,
especially how local agents decide whether to close, how human confirmation is
recorded as `completion_authority`, and how pending work is surfaced to users.

## 8.9 Requester-Side Completion Decision Polish

Status: completed in the public MCP/client repo.

Implemented outputs in `ZilingXie/agent-relay-mcp`:

- PR: `agent-relay-mcp#3`
- MCP tool: `agentrelay_prepare_completion_decision`
- `agentrelay_close_task` now supports structured human completion authority fields.
- New doc: `docs/completion-decision-workflow.md`
- Tool reference updated for requester-side close, human authority, and revision request workflow.
- Codex App inbox example rules now require the decision helper before requester-owned close.

This keeps the relay small. The relay still records state and enforces close
permissions; the local requester-side agent decides whether `done_criteria` is
satisfied, asks its human owner when needed, and records human approval as
redacted `completion_authority`.

Next focus: validate the newer local inbox workbench with a real remote agent so
incoming tasks, processor decisions, human approvals, revision requests, and
close actions are visible and ergonomic in the default local client experience.

The first implementation slice should be intentionally small:

```text
protocol docs
JSON schemas + agent-first response envelope
state transition validator
negative tests
```

Do not start with a dashboard or full A2A compatibility. The highest leverage next step is preventing ambiguous or invalid agent collaboration states.

Recommended files:

```text
docs/agent-collaboration-protocol-v0.md
schemas/task-create.schema.json
schemas/artifact-submit.schema.json
schemas/task-close.schema.json
schemas/agent-event.schema.json
schemas/response-envelope.schema.json
server/transitions.py
scripts/phase3_transition_smoke_test.py
```

## 9. Octo Comparison

Octo is a useful reference, but it is not a replacement for AgentRelay.

AgentRelay:

- AI agents are at the center.
- Humans are owners, authorities, and local decision sources behind agents.
- The main abstractions are task, pending owner, artifact, completion owner, timeline, agent event, and thread binding.
- The best use case is cross-environment personal agents coordinating work while both sides stay behind NAT.

Octo:

- Workspace and IM collaboration are at the center.
- Humans, teams, channels, and agents collaborate inside a shared workplace surface.
- The main abstractions are space, channel, thread, message, bot, Matter, timeline, and runtime daemon.
- The best use case is enterprise IM where people and bots collaborate visibly.

What AgentRelay borrows from Octo:

- Timeline discipline for clearer activity logs and future dashboards.
- Status separation: action output and task completion remain separate decisions.
- Event delivery reliability: replay, cursor, dedup, inflight, done, and ack.
- Secret hygiene: push notifications should not carry credentials or sensitive full payloads.
- Agent-first tooling: structured JSON envelopes and actionable error hints.
- Evidence pointers: optional `source_refs` for important artifact claims.

## 10. Open Design Questions

- Should `done_criteria` stay free text, or become structured by task type?
- Should an agent be allowed to reject a task without asking its human?
- Which artifact types should require `source_refs`, if any?
- How much of a human completion authority record should be visible to the remote requester?
- Should `turn_count` increment on every status update or only on cross-agent ownership transfer?
- How should task capabilities/scopes be represented in agent cards?
- How strict should A2A compatibility be for v0?
- Should child tasks inherit auth/scopes from parent tasks?
