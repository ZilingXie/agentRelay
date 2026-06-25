# AgentRelay Codex App Bridge

This folder packages the Phase 1 Codex App bridge flow.

The standalone AgentRelay server is intentionally lightweight. It stores tasks, ownership, artifacts, delivery status, and audit events, but it does not directly call Codex App thread tools. The Codex App bridge is the component that can use Codex App thread operations.

## Bridge Responsibilities

The bridge performs three reusable flows:

1. Target-side claim:
   - Poll `GET /agentrelay/workers/:agentId/claim`.
   - Create or reuse a target Codex App thread.
   - Record `target_thread_id` on the task.

2. Origin-thread delivery:
   - Claim returned tasks for the requester agent.
   - Deliver the latest artifact to `requester_thread_id`.
   - Record delivery with `POST /agentrelay/tasks/:taskId/deliveries`.

3. Requester-side close:
   - After the requester human confirms the done criteria, call `POST /agentrelay/tasks/:taskId/close`.

## Required Codex App Operations

The bridge needs access to these Codex App capabilities:

```text
create_thread(prompt, target)
send_message_to_thread(thread_id, prompt)
read_thread(thread_id)
set_thread_title(thread_id, title)
```

These operations are not provided by the standalone Python relay server. They must run in a Codex App environment, a Codex App-side MCP bridge, or another integration that can call thread APIs.

## Flow Files

- `contracts/bridge-job.schema.json`: reusable bridge job envelope.
- `prompts/target-thread.md`: prompt template for creating/reusing the target agent thread.
- `prompts/origin-delivery.md`: prompt template for delivering an artifact back to requester thread.
- `prompts/requester-close.md`: prompt template for closing a task after requester confirmation.

## Phase 1 Rule

Delivery is not completion.

```text
artifact submitted -> delivery_pending
origin delivery -> waiting_human
requester confirms -> completed
```

Only `completion_owner_agent_id` can close the task.

