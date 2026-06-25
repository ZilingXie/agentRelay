# Codex App Bridge Flow

Date: 2026-06-25

## Purpose

This document turns the Phase 1 Codex App bridge proof into a reusable connector flow.

AgentRelay remains a lightweight relay. It does not directly call Codex App thread operations. A Codex App-side bridge performs thread creation, thread reuse, and origin-thread delivery.

## Flow A: Target-side claim

Used by Frank-side bridge.

```text
1. GET /agentrelay/workers/frank-agent/claim
2. If task is null, stop.
3. If task.target_thread_id is empty:
   - create Codex App thread using bridge/prompts/target-thread.md
   - set title: AgentRelay <task_id>
   - POST /agentrelay/workers/frank-agent/tasks/<task_id>/thread
4. If task.target_thread_id exists:
   - send follow-up to that thread using bridge/prompts/target-thread.md
5. Frank Codex asks Frank.
6. Frank Codex submits artifact:
   POST /agentrelay/tasks/<task_id>/artifacts
```

Expected relay result after artifact submission:

```text
status = delivery_pending
pending_on_agent_id = zac-agent
event = ownership.transferred
```

## Flow B: Origin-thread delivery

Used by Zac-side bridge.

```text
1. GET /agentrelay/workers/zac-agent/claim
2. If task is null, stop.
3. Read task.requester_thread_id.
4. Send latest artifact to requester_thread_id using bridge/prompts/origin-delivery.md.
5. POST /agentrelay/tasks/<task_id>/deliveries
```

Successful delivery body:

```json
{
  "deliveredByAgentId": "zac-agent",
  "threadId": "<requester_thread_id>",
  "deliveryStatus": "delivered",
  "pendingOnHumanId": "zac",
  "nextAction": "Ask Zac whether the proposed time works."
}
```

Expected relay result:

```text
status = waiting_human
delivery_status = delivered
delivered_to_thread_id = requester_thread_id
event = reply.delivered
```

## Flow C: Requester-side close

Used after Zac confirms the done criteria.

```text
1. Zac replies in original requester thread.
2. Zac agent checks done_criteria.
3. If done criteria is satisfied:
   POST /agentrelay/tasks/<task_id>/close
4. If not satisfied:
   create a child task or transfer ownership to the next agent.
```

Close body:

```json
{
  "closedByAgentId": "zac-agent",
  "terminalReason": "Requester confirmed the proposed meeting time."
}
```

## Bridge Job Envelope

Reusable bridge jobs should follow:

```json
{
  "jobType": "origin_delivery",
  "taskId": "task_123",
  "agentId": "zac-agent",
  "task": {},
  "artifact": {},
  "thread": {
    "requesterThreadId": "019...",
    "targetThreadId": null,
    "threadPolicy": "reuse"
  },
  "instructions": "Deliver the latest artifact back to requester thread."
}
```

See `bridge/contracts/bridge-job.schema.json`.

## Current Limitation

The bridge is packaged as a reusable flow and prompt contract, not yet a running daemon.

Reason: Codex App thread operations are app-provided capabilities. The standalone Python relay server cannot call them directly.

Next implementation option:

```text
AgentRelay MCP tools + Codex App bridge prompts
```

The MCP tools should wrap the relay HTTP API. Codex App remains responsible for `create_thread` and `send_message_to_thread`.

