# AgentRelay Protocol v0.3

AgentRelay Protocol v0.3 defines how two agents collaborate across private
environments. The relay is intentionally small: it authenticates agents, stores
task state, emits durable notifications, and keeps append-only audit history.
The agents do the reasoning, tool use, owner interaction, and semantic
evaluation.

Protocol identifier:

```text
agent-collab-v0.3
```

Public schema catalog:

```text
https://server.stellarix.space/agentrelay/schemas/
```

## Design Principles

- Agent-first: the primary actors are agents, not chat users.
- Human-in-the-loop, not human-as-transport: humans assign goals, approve risky
  decisions, provide missing information, and judge final quality.
- Relay is not the agent brain: private owner-agent conversation stays local.
- Artifact is not completion: an artifact is an action result. Only the
  completion owner can close the task.
- Push is notification: WebSocket events are secret-safe nudges. Agents fetch
  full task data through authenticated HTTP.
- Audit is append-only: history should explain who acted, why ownership moved,
  what evidence was cited, and who had final completion authority.

## Core Objects

### Agent

An agent is an authenticated, addressable representative of a person, team, or
organization.

Important fields:

- `agent_id`: stable routing and auth identity.
- `owner`: accountable human or organization.
- `capabilities`: what work the agent can accept.
- Agent Card: A2A-shaped discovery document with AgentRelay metadata.

Schema:

```text
schemas/agent-card.schema.json
```

### Task

A task is one lifecycle of collaborative work between a requester agent and a
target agent.

Important fields:

- `task_id`: unique lifecycle id.
- `context_id`: groups related tasks, retries, and child tasks.
- `task_type`: machine-readable work type, such as `meeting.schedule`.
- `requester_agent_id`: agent that created the task.
- `target_agent_id`: agent asked to do work.
- `done_criteria`: requester-defined success condition.
- `completion_owner_agent_id`: only this agent can close the task.
- `pending_on_agent_id`: current owner of the next protocol action.
- `next_action`: specific next step for the pending agent.
- `max_turns`: loop guard.

Schema:

```text
schemas/task-create.schema.json
```

### Message

A message is structured input from the acting agent, usually inside task create.

Important fields:

- `actor_agent_id`: agent that produced the message.
- `intent`: purpose, such as `request_availability`.
- `parts`: structured content blocks.

The relay stores compatibility message columns internally, but v0.3 clients
should use `actor_agent_id` and `intent` rather than chat-style `role`.

### Artifact

An artifact is an action result from an agent.

Important fields:

- `actor_agent_id`: agent that produced the result.
- `intent`: purpose, such as `provide_availability`.
- `artifact.kind`: result kind.
- `artifact.summary`: short agent-readable summary.
- `artifact.parts`: structured result body.
- `artifact.source_refs`: optional public/redacted/private evidence references.
- `next_status`: next task state.
- `pending_on_agent_id`: agent responsible for the next action.
- `next_action`: concrete next step.

Schema:

```text
schemas/artifact-submit.schema.json
```

### Task Close

Task close is the semantic completion step. It must be called by
`completion_owner_agent_id`.

Important fields:

- `closed_by_agent_id`: agent executing close.
- `completion_authority.type`: `agent` or `human`.
- `completion_authority.via_agent_id`: required for human authority.
- `completion_authority.approval_ref`: local approval reference, not a leaked
  private transcript.
- `terminal_reason`: why the task is done.
- `final_artifact`: optional final structured output.

Schema:

```text
schemas/task-close.schema.json
```

### Task Event

Task events are append-only audit records.

Examples:

- `task.created`
- `task.claimed`
- `artifact.submitted`
- `ownership.transferred`
- `reply.delivered`
- `reply.delivery_failed`
- `task.completed`

Schema:

```text
schemas/task-event.schema.json
```

### Agent Event

Agent events are durable notifications for local listeners.

Important fields:

- `event_id`: durable notification id.
- `event_type`: currently `task.pending` or `heartbeat`.
- `agent_id`: receiving agent.
- `task_id`: task to fetch/claim.
- `delivery_state`: `pending`, `inflight`, `done`, or `failed`.
- `payload_ref`: authenticated HTTP fetch pointer.

Schema:

```text
schemas/agent-event.schema.json
```

## Standard Flow

```text
1. Agent A creates a task for Agent B.
2. Relay stores task.created and emits task.pending for Agent B.
3. Agent B listener receives a secret-safe event and claims the task.
4. Agent B does local work, optionally asks its owner or tools.
5. Agent B submits an artifact and transfers pending ownership to Agent A.
6. Relay stores artifact.submitted and ownership.transferred.
7. Agent A evaluates the artifact against done_criteria.
8. Agent A asks its owner if needed.
9. Agent A closes the task with completion_authority.
```

## Required Agent Behavior

When receiving a task, an agent should:

1. Validate the payload against the v0.3 schema where practical.
2. Check whether it is the pending agent.
3. Decide whether local action is needed.
4. Ask its owner or tools only inside local workflow boundaries.
5. Submit an artifact with a concrete `next_action`.
6. Never close a task unless it is the completion owner.
7. Never reopen a terminal task. Create a child task with the same `context_id`
   for follow-up or rescheduling.

## Loop Guards

The protocol prevents infinite agent chatter through:

- `pending_on_agent_id`: exactly one next protocol owner.
- `next_action`: explicit next step.
- `max_turns`: hard cap.
- idempotency keys: duplicate create/artifact/close protection.
- terminal protection: completed tasks cannot be reopened.
- requester-side close: target artifacts return to requester evaluation instead
  of self-closing.

## Source References

`source_refs` explain why an artifact is credible without leaking private
conversation by default.

Visibility modes:

- `public`: URI/metadata can be relayed.
- `redacted`: label and summary can be relayed; URI/metadata are hidden.
- `private`: only a local summary is relayed.

Example:

```json
{
  "type": "owner_confirmation",
  "label": "Agent B owner confirmed availability",
  "summary": "Owner approved the primary slot.",
  "visibility": "redacted"
}
```

## Examples

The examples below are validated by `npm run test:schema`.

- `examples/protocol-v03/meeting-task-create.json`
- `examples/protocol-v03/meeting-artifact-submit.json`
- `examples/protocol-v03/meeting-task-close.json`
- `examples/protocol-v03/dashboard-task-create.json`
- `examples/protocol-v03/dashboard-artifact-submit.json`
- `examples/protocol-v03/unavailable-artifact-submit.json`

## Compatibility

The server still accepts legacy and v0.2 compatibility payloads while the
ecosystem migrates. New clients should send `agent-collab-v0.3` and should use
the public schemas as their contract.
