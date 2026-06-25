# Origin Thread Delivery Proof

Date: 2026-06-25

## Goal

Prove that AgentRelay can deliver a remote agent artifact back to the original requester Codex App thread.

This is the second half of the Phase 1 Codex App bridge:

- Frank-side proof: create and reuse Frank task thread.
- Zac-side proof: send Frank's reply back to Zac's origin requester thread.

## Result

Status: passed.

Created Zac origin proof thread:

```text
thread_id: 019efd73-c459-7cf1-baa5-33afa244648e
title: AgentRelay Zac origin delivery proof
host: remote-ssh-codex-managed:tencent
cwd: /home/ubuntu
task_id: task_delivery_proof_001
```

The thread acknowledged it could serve as the requester thread:

```text
Acknowledged. This thread can serve as the `requester_thread_id` for `task_delivery_proof_001`.
```

Then a simulated Frank artifact was delivered to the same thread:

```text
Frank is available Tuesday 10:00-11:00 or Thursday 15:00-16:00 China time.
```

The Zac origin thread replied:

```text
Delivered back to the original Zac requester thread; Zac should confirm whether Tuesday 10:00-11:00 or Thursday 15:00-16:00 China time works.
```

## Relay State Support

The relay now supports delivery tracking:

```text
POST /agentrelay/tasks/:taskId/deliveries
```

Successful delivery records:

```text
delivery_status = delivered
delivered_to_thread_id = requester_thread_id
status = waiting_human
pending_on_human_id = zac
event = reply.delivered
```

Failed delivery records:

```text
delivery_status = failed
status = delivery_pending
pending_on_agent_id = completion_owner_agent_id
event = reply.delivery_failed
```

## What This Proves

- `requester_thread_id` can be reused as the callback thread.
- A reply artifact from Frank can be delivered back into Zac's original Codex App thread.
- Delivery is separate from task completion.
- After delivery, the task can wait for Zac human confirmation.

## Remaining Gap

The proof used Codex App tools directly from this session. A standalone production bridge still needs an implementation path, such as:

- a Codex App-side MCP bridge,
- a small connector process with access to Codex App thread operations,
- or a future native automation/wakeup integration.

