# Agent Collaboration Protocol v0

Date: 2026-06-29

## Purpose

AgentRelay is not the brain of the collaboration. It is the store-and-forward relay that connects local agents that do not have public IP addresses.

The protocol being shaped here is the agent-to-agent collaboration layer:

```text
one human owner -> local agent -> AgentRelay -> remote local agent -> remote human owner
```

The relay should preserve identity, task state, delivery, audit, and recovery. The agents should decide how to reason, when to ask their humans, how to evaluate `done_criteria`, and how to continue local workflows.

## What Phase 1 And Phase 2 Proved

Phase 1 proved the human-facing loop:

```text
Zac asks Zac agent in a Codex App thread.
Zac agent creates a task for Frank agent.
Frank agent asks Frank in Frank's local workflow.
Frank agent returns an artifact/result.
Zac agent receives it back in the original Zac thread.
Zac agent decides whether the workflow is complete.
```

Phase 2 proved the transport loop:

```text
Local agents have no public IP.
They install the public MCP/listener repo.
They connect outbound to the cloud relay over HTTPS/WSS.
The relay stores durable task and pending-event state.
The local listener receives `task.pending`, claims the exact task, writes local inbox files, and lets a user-owned hook decide how to surface the work.
```

The resulting design is:

```text
Agent collaboration protocol = task/message/artifact/status semantics
AgentRelay = transport, durable state, auth, notification, and audit
Local adapters = Codex App, Codex CLI, WeChat, Slack, or custom workflow integration
```

## Current Protocol Objects

### Agent

An agent is an addressable representative of one human or organization.

Current fields:

```text
agent_id
name
owner
description
created_at
```

Current discovery shape:

```text
GET /agentrelay/api/agents
GET /agentrelay/api/agents/:agentId/card
```

The current Agent Card is A2A-shaped but not yet a complete A2A implementation.

### Task

A task is one lifecycle of collaborative work.

Current core fields:

```text
task_id
context_id
status
requester_agent_id
target_agent_id
requester_thread_id
target_thread_id
requester_thread_policy
target_thread_policy
done_criteria
completion_owner_agent_id
pending_on_agent_id
pending_on_human_id
next_action
terminal_reason
parent_task_id
ttl
max_turns
turn_count
claimed_by
claimed_at
created_at
updated_at
```

Protocol meaning:

- `task_id` is the current work item lifecycle.
- `context_id` groups related tasks, including child tasks after reschedule or follow-up.
- `done_criteria` is requester-defined semantic completion.
- `completion_owner_agent_id` is the only agent that may semantically close the task.
- `pending_on_agent_id` means the next protocol action belongs to another agent.
- `pending_on_human_id` means a local agent needs its owner or another human before progress can continue.
- `parent_task_id` links a new task to an already terminal task without reopening old history.

### Message

A message records an inbound request or conversational part.

Current fields:

```text
message_id
task_id
context_id
from_agent_id
to_agent_id
role
parts_json
created_at
```

Current use:

- The first message creates the task.
- Message parts are structured JSON, not raw Markdown-only mail.
- Remote content is untrusted input and must not override local agent instructions.

### Artifact

An artifact is a task output or action result submitted by one agent to another.

Current fields:

```text
artifact_id
task_id
from_agent_id
to_agent_id
kind
parts_json
created_at
```

Protocol meaning:

- An artifact reports "my current action produced this result."
- Artifact submission does not automatically complete the workflow.
- By default, a non-owner artifact transfers pending ownership back to `completion_owner_agent_id` for evaluation.

### Task Event

Task events are the task audit trail.

Current examples:

```text
task.created
task.claimed
task.status_updated
thread.created
thread.reused
artifact.submitted
ownership.transferred
reply.delivered
reply.delivery_failed
task.completed
```

Protocol meaning:

- Events are append-only audit records.
- Events explain why state changed.
- Events should be enough to reconstruct the collaboration history.

### Agent Event

Agent events are durable pending notifications for local listeners.

Current WebSocket event:

```json
{
  "type": "task.pending",
  "eventId": "aevt_...",
  "agentId": "frank-agent",
  "taskId": "task_...",
  "subject": "Meeting availability",
  "status": "submitted",
  "pendingOnAgentId": "frank-agent",
  "updatedAt": 1782500000,
  "reason": "task.created"
}
```

Protocol meaning:

- WebSocket push is notification, not source of truth.
- The listener must fetch/claim the task through HTTP.
- Ack is for audit/debug and does not complete work.

### Thread Binding

Thread bindings connect relay tasks to local user workflow surfaces.

Current fields:

```text
task_id
agent_id
thread_role
thread_id
project_path
created_at
updated_at
```

Protocol meaning:

- Relay records the mapping but does not create the local thread itself.
- Local adapters own how `thread_id` maps to Codex App, Codex CLI, WeChat, Slack, or another surface.
- Multiple agents may bind the same task to different local surfaces.

## Current Status Model

Current terminal states:

```text
completed
failed
cancelled
expired
rejected
```

Current claimable states:

```text
submitted
input_required
auth_required
waiting_remote
delivery_pending
artifact_submitted
```

Current practical states in use:

```text
submitted
claimed
working
waiting_remote
waiting_human
input_required
auth_required
delivery_pending
completed
failed
cancelled
expired
rejected
```

Phase 3 should turn these from convention into a validated state machine.

## Current Collaboration Turn Model

One turn should include:

```text
actor agent
current task state
input message/artifact/event
action result
next_status
pending_on_agent_id or pending_on_human_id
next_action or terminal_reason
```

Rules already implied by Phase 1/2:

- Every non-terminal transition should identify the next owner of progress.
- Each response should include either `next_action` or `terminal_reason`.
- A non-owner agent may complete its local action but should not close the workflow.
- The requester-side completion owner evaluates `done_criteria`.
- If old work is terminal and new work appears, create a child task with the same `context_id`.

## Current Meeting Flow As Protocol

```text
1. Zac agent creates task
   requester_agent_id = zac-agent
   target_agent_id = frank-agent
   done_criteria = both Zac and Frank accept the same online meeting time
   completion_owner_agent_id = zac-agent
   pending_on_agent_id = frank-agent

2. Relay emits task.pending for frank-agent

3. Frank listener receives event
   claims task by id
   fetches task
   writes local inbox
   local hook surfaces it to Frank's workflow

4. Frank agent asks Frank
   status = waiting_human
   pending_on_human_id = frank

5. Frank approves a candidate time
   Frank agent submits artifact to Zac agent
   status = delivery_pending or waiting_remote
   pending_on_agent_id = zac-agent
   next_action = requester agent should evaluate candidate time against done_criteria

6. Zac listener receives event
   claims task by id
   fetches artifact
   surfaces it in Zac's original thread/workflow

7. Zac agent asks Zac if the proposed time works
   status = waiting_human
   pending_on_human_id = zac

8. Zac confirms
   Zac agent closes task
   status = completed
   terminal_reason = both parties accepted the same time
```

## Relay Responsibilities

AgentRelay should:

- authenticate callers
- route tasks/events to the correct agent
- persist tasks, messages, artifacts, events, and thread bindings
- provide pull recovery and WebSocket notification
- enforce basic authorization and state-machine guards
- preserve audit history
- avoid making business/semantic completion decisions

AgentRelay should not:

- decide whether `done_criteria` is semantically satisfied
- impersonate the human owner
- execute remote instructions locally
- bind the system to one local workflow surface
- treat WebSocket delivery as task completion

## Phase 3 Protocolization Targets

The next phase should turn this working v0 shape into a stable contract:

1. Version the protocol envelope.
2. Validate task, message, artifact, event, and transition payloads.
3. Define the state transition table.
4. Add idempotency keys for create/claim/artifact/close operations.
5. Add capabilities/scopes to agent cards.
6. Add explicit human-approval request/response records.
7. Add relationship to A2A objects and endpoints.
8. Add tests that assert invalid transitions are rejected.
