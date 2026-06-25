# Codex App Thread Bridge Proof

Date: 2026-06-25

## Goal

Prove that Phase 1 can use Codex App threads as the human-facing interface instead of a CLI.

The bridge needs two capabilities:

- Create a Codex App thread for Frank when `frank-agent` claims a task.
- Reuse an existing Codex App thread for follow-up messages.

## Result

Status: passed for thread creation and thread reuse.

Created Frank-side proof thread:

```text
thread_id: 019efceb-420e-7223-8c29-7330c476a469
title: AgentRelay Frank bridge proof
host: remote-ssh-codex-managed:tencent
cwd: /home/ubuntu
task_id: task_bridge_proof_001
```

The created thread acknowledged:

```text
Acknowledged. This thread can represent Frank's side of `task_bridge_proof_001`.

For the bridge proof, I would ask Frank for his meeting availability first before returning any availability to Zac.
```

Then a follow-up message was sent to the same thread. The thread replied:

```text
Confirmed: this same thread is being reused for `task_bridge_proof_001`.
```

## What This Proves

- AgentRelay can create a Frank-side Codex App thread after a worker claim.
- AgentRelay can store the returned thread id as `target_thread_id`.
- Follow-up messages for the same task can reuse `target_thread_id`.
- The same Codex App `send_message_to_thread` capability can be used later to deliver Frank's artifact back to Zac's stored `requester_thread_id`.

## Current Caveat

The proof did not self-send a message back into the currently running Zac thread, because doing so during an active turn could confuse the live conversation. The next implementation step should wire this into a controlled bridge action that sends to `requester_thread_id` after the relay observes `task.completed`.

## Required Bridge Contract

The AgentRelay bridge needs these operations:

```text
create_target_thread(task) -> target_thread_id
send_to_thread(thread_id, prompt) -> delivery_result
read_thread(thread_id) -> thread_status
set_thread_title(thread_id, title) -> delivery_result
```

For Phase 1, these operations can be implemented by a Codex App-side bridge rather than the standalone relay process, because the standalone Python server does not have access to Codex App thread tools.

