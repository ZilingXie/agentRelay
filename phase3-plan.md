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

The current protocol already has these concepts:

- Agent identity and Agent Card.
- Task lifecycle with `task_id`, `context_id`, `status`, `pending_on_agent_id`, and `pending_on_human_id`.
- Requester-defined `done_criteria`.
- Requester-side `completion_owner_agent_id`.
- Structured messages and artifacts.
- Durable task events and agent events.
- Per-agent thread bindings for local workflow reuse.
- Manual HTTP recovery plus WebSocket notification.
- User-owned local adapter hooks for Codex App, Codex CLI, WeChat, Slack, or custom workflows.

The current protocol is documented in `docs/agent-collaboration-protocol-v0.md`.

## 2. Phase 3 Goal

Define and implement the first stable "Agent Collaboration Protocol" contract:

```text
ACP-v0 over AgentRelay transport
```

This contract should answer:

- How does one agent ask another agent to help solve a problem?
- How does the target agent accept, reject, ask a human, or return partial work?
- How does ownership transfer between agents?
- How does an agent say "my action is complete" without incorrectly closing the whole workflow?
- Who is allowed to close a task?
- How do agents avoid infinite loops?
- How are human approvals represented?
- How does this map to A2A without forcing the relay to be the agent brain?

## 3. Product Principle

AgentRelay should stay small and boring:

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
pending_on_agent_id
pending_on_human_id
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
from_agent_id
to_agent_id
role
parts
requires_human
confidentiality
created_at
```

### Artifact

Define artifact as agent action output:

```text
artifact_id
task_id
from_agent_id
to_agent_id
kind
parts
summary
next_status
pending_on_agent_id
pending_on_human_id
next_action
created_at
```

### Human Approval

Add a first-class approval object instead of hiding it in status text:

```text
approval_id
task_id
agent_id
human_id
approval_type
prompt
decision
decision_summary
expires_at
created_at
decided_at
```

### Event

Define event names and required payload fields:

```text
task.created
task.claimed
task.status_updated
message.added
artifact.submitted
approval.requested
approval.decided
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
waiting_human
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
- `claimed -> working | waiting_human | input_required | auth_required`.
- `artifact.submitted` may transfer ownership to another agent.
- A non-terminal transition must set `pending_on_agent_id` or `pending_on_human_id`.
- A non-terminal transition must set `next_action`.
- A terminal transition must set `terminal_reason`.
- Only `completion_owner_agent_id` may close as `completed`.
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

## 7. Phase 3 Implementation Steps

1. [x] Create `phase3-plan.md`.
2. [x] Document the current Phase 1/2 communication protocol in `docs/agent-collaboration-protocol-v0.md`.
3. [ ] Add `protocol_version` to task/message/artifact/event payloads.
4. [ ] Create JSON schemas for task create, artifact submit, status transition, close, and agent events.
5. [ ] Implement a transition validator in `server/store.py`.
6. [ ] Add negative tests for invalid transitions and unauthorized completion.
7. [ ] Add idempotency keys to task creation, artifact submission, event ack, and close.
8. [ ] Add first-class human approval records.
9. [ ] Expand agent cards with capabilities, scopes, and accepted task types.
10. [ ] Add admin/debug views or CLI for agents, tasks, events, and pending work.
11. [ ] Add A2A mapping document and minimal compatibility endpoint plan.
12. [ ] Run the meeting flow again under the validated protocol.

## 8. First Implementation Slice

The first implementation slice should be intentionally small:

```text
protocol docs
JSON schemas
state transition validator
negative tests
```

Do not start with a dashboard or full A2A compatibility. The highest leverage next step is preventing ambiguous or invalid agent collaboration states.

Recommended files:

```text
docs/agent-collaboration-protocol-v0.md
schemas/task-create.schema.json
schemas/artifact-submit.schema.json
schemas/status-transition.schema.json
schemas/task-close.schema.json
schemas/agent-event.schema.json
server/transitions.py
scripts/phase3_transition_smoke_test.py
```

## 9. Open Design Questions

- Should `done_criteria` stay free text, or become structured by task type?
- Should an agent be allowed to reject a task without asking its human?
- Should approval records be private to the target agent, or visible to the requester as redacted summaries?
- Should `turn_count` increment on every status update or only on cross-agent ownership transfer?
- How should task capabilities/scopes be represented in agent cards?
- How strict should A2A compatibility be for v0?
- Should child tasks inherit auth/scopes from parent tasks?
