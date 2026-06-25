# AgentRelay Phase 1 Plan: Codex App Thread Loop

GitHub repository: https://github.com/ZilingXie/agentRelay

## 0. Progress

- [x] Create GitHub repository and push planning docs.
- [x] Add official A2A repository as upstream reference.
- [x] Scaffold relay server project.
- [x] Implement SQLite data model.
- [x] Implement A2A-shaped task and worker APIs.
- [x] Verify with a local smoke test.
- [x] Add Codex App thread bridge proof.
- [x] Encode requester-side completion ownership in task metadata and API payloads.
- [x] Implement controlled delivery back to Zac's origin thread.
- [x] Package Codex App bridge into a reusable connector/MCP flow.
- [ ] Implement AgentRelay MCP tools that wrap the relay HTTP API.

## 1. 第一阶段目标

跑通一个真实的 Codex App 体验闭环：

```text
Zac 在 Codex App thread abc 里发起请求
  -> Zac Codex 使用 AgentRelay skill/MCP 创建 A2A-shaped task
  -> AgentRelay 将 task 放入 frank-agent queue
  -> Frank connector 定期 GET /workers/frank-agent/claim
  -> 有新 claim 时，Frank connector 在 Frank 的 Codex App 中创建或复用 thread
  -> Frank Codex 在该 thread 里询问 Frank
  -> Frank 给出可用时间
  -> Frank Codex 使用 AgentRelay skill/MCP 提交回复
  -> AgentRelay 将回复投递回 Zac 原始 thread abc
  -> Zac Codex 在 thread abc 里继续处理并告诉 Zac
```

成功标准：

- Zac 不需要复制粘贴给 Frank。
- Frank 不需要打开 AgentRelay 后台，只在自己的 Codex App thread 中确认或回复。
- AgentRelay 能保存 task、message、artifact、thread mapping 和 audit log。
- Zac 的最终回复必须回到原始发起 thread，而不是新建一个 Zac thread。

## 2. 和前一个 CLI 方案的关键变化

第一阶段不使用 CLI 作为用户体验入口。CLI 可以保留为调试工具，但不是 PoC 成功标准。

新的用户体验入口是 Codex App:

- Zac 在 Codex App 里新建 thread，例如 `abc`。
- Zac 对 Codex 说：用 AgentRelay skill/MCP 问问 Frank 什么时候有空约线上会议。
- Zac Codex 通过 AgentRelay skill/MCP 调用 relay。
- Frank 侧 connector 轮询 relay。
- Frank 侧 connector 收到 claim 后，在 Frank 的 Codex App 里创建 thread，或者如果同一个 task 已经有 thread，则复用它。
- Frank 在 Codex App thread 中回复。
- Frank Codex 通过 AgentRelay skill/MCP 把结果发回 relay。
- Zac 侧 connector 或 relay bridge 将结果发送回 Zac 的原始 thread `abc`。

## 3. 必须支持的 thread reuse

Thread reuse 是 Phase 1 的核心要求。

### Zac 侧

Zac 的发起 thread 是 task 的 callback thread。

创建 task 时必须记录：

```json
{
  "requester_agent_id": "zac-agent",
  "requester_thread_id": "abc",
  "requester_thread_policy": "reuse-origin-thread"
}
```

当 Frank agent 回复后，系统必须把消息发回 `abc`，而不是创建新的 Zac thread。

### Frank 侧

Frank 侧收到新 task 时：

- 如果 task 没有 `target_thread_id`，Frank connector 创建一个新的 Codex App thread。
- 创建后把 `target_thread_id` 写回 AgentRelay。
- 如果之后同一个 task 有 follow-up，Frank connector 必须复用这个 `target_thread_id`。

记录示例：

```json
{
  "target_agent_id": "frank-agent",
  "target_thread_id": "frank-thread-123",
  "target_thread_policy": "reuse-task-thread"
}
```

## 3.1 Task completion policy

AgentRelay must distinguish between `agent action complete` and `workflow task complete`.

For example, when Frank agent asks Frank and returns "10:00" to Zac agent, Frank agent's action is complete. The overall task may still be pending because Zac needs to confirm whether 10:00 works.

Task completion is defined at task creation time by the requester side through `done_criteria`.

For Phase 1:

- The requester agent proposes `done_criteria`.
- The requester agent is the semantic owner of completion.
- The relay stores `done_criteria` on the task as metadata.
- The relay maintains transport state, ownership transfer, TTL, and audit.
- Agents submit artifacts, action results, and recommendations.
- Agents do not unilaterally archive the whole workflow unless the requester-side completion logic explicitly closes it.

Meeting task default:

```text
done_criteria = both Zac and Frank accept the same online meeting time
```

If Zac does not reply to a proposed time, the task stays `waiting_human` with `pending_on_human_id=zac`, then reminder/expiry policy applies.

If Zac accepts and later changes his mind after completion, AgentRelay should create a child task with the same `context_id` and `parent_task_id`, instead of reopening the completed task.

See `docs/task-completion-policy.md`.

## 4. AgentRelay 需要保存的数据

Phase 1 的 task 记录至少需要：

```text
task_id
context_id
status
requester_agent_id
target_agent_id
requester_thread_id
target_thread_id
done_criteria
completion_owner_agent_id
pending_on_agent_id
pending_on_human_id
next_action
terminal_reason
parent_task_id
created_at
updated_at
ttl
max_turns
```

message 记录至少需要：

```text
message_id
task_id
context_id
from_agent_id
to_agent_id
role
parts
created_at
```

artifact 记录至少需要：

```text
artifact_id
task_id
from_agent_id
parts
created_at
```

event log 至少记录：

```text
task.created
task.claimed
thread.created
thread.reused
human.input_required
artifact.submitted
task.completed
reply.delivered
reply.delivery_failed
```

## 5. 最小 API

外部 A2A-shaped API:

```text
GET  /agentrelay/agents/:agentId/card
POST /agentrelay/tasks
GET  /agentrelay/tasks/:taskId
GET  /agentrelay/tasks/:taskId/events
POST /agentrelay/tasks/:taskId/messages
POST /agentrelay/tasks/:taskId/artifacts
POST /agentrelay/tasks/:taskId/status
POST /agentrelay/tasks/:taskId/close
POST /agentrelay/tasks/:taskId/deliveries
```

Worker API:

```text
GET  /agentrelay/workers/:agentId/claim
POST /agentrelay/workers/:agentId/tasks/:taskId/thread
POST /agentrelay/workers/:agentId/tasks/:taskId/heartbeat
POST /agentrelay/workers/:agentId/tasks/:taskId/complete
```

Codex App bridge capability:

```text
create_thread(owner, prompt, metadata)
send_message_to_thread(thread_id, prompt)
read_thread(thread_id)
set_thread_title(thread_id, title)
```

The connector can use the Codex App thread tools where available. If the tools are not available to a standalone daemon, Phase 1 needs a small bridge process or MCP server running in the Codex App environment.

Bridge package:

- `bridge/contracts/bridge-job.schema.json`
- `bridge/prompts/target-thread.md`
- `bridge/prompts/origin-delivery.md`
- `bridge/prompts/requester-close.md`
- `docs/codex-app-bridge-flow.md`

## 6. Phase 1 task lifecycle

1. Zac opens a Codex App thread `abc`.
2. Zac asks: "Use AgentRelay to ask Frank when he is available for an online meeting."
3. Zac Codex calls AgentRelay MCP/skill `create_task`.
4. AgentRelay creates a task with `requester_thread_id=abc`.
5. Frank connector polls `GET /workers/frank-agent/claim`.
6. AgentRelay returns the pending task.
7. Frank connector checks whether the task already has `target_thread_id`.
8. If not, it creates a Frank Codex App thread with the task context.
9. Frank Codex asks Frank for availability in that thread.
10. Frank replies with candidate times.
11. Frank Codex calls AgentRelay MCP/skill `submit_artifact`.
12. AgentRelay emits `artifact.submitted` and transfers ownership back to `zac-agent`.
13. Zac delivery bridge sends the result to `requester_thread_id=abc`.
14. AgentRelay records `reply.delivered` and moves the task to `waiting_human`.
15. Zac Codex continues inside thread `abc`, summarizes Frank's availability, and asks Zac for confirmation.
16. If Zac confirms the done criteria, Zac Codex calls `/close` and AgentRelay records `task.completed`.

## 7. Meeting request payload

Example task creation payload:

```json
{
  "contextId": "ctx_meeting_frank_20260625",
  "from": "zac-agent",
  "to": "frank-agent",
  "requesterThreadId": "abc",
  "message": {
    "role": "user",
    "parts": [
      {
        "kind": "text",
        "text": "Zac wants to schedule a 30-minute online meeting with Frank. Please ask Frank when he is available."
      }
    ]
  },
  "humanBoundary": {
    "requiresHuman": true,
    "reason": "Frank must approve sharing availability or committing to meeting times."
  }
}
```

Example artifact from Frank:

```json
{
  "taskId": "task_123",
  "from": "frank-agent",
  "to": "zac-agent",
  "artifact": {
    "kind": "meeting_availability",
    "parts": [
      {
        "kind": "text",
        "text": "Frank is available Tuesday 10:00-11:00 or Thursday 15:00-16:00 China time."
      }
    ]
  }
}
```

## 8. Open implementation question

The key technical question is how a background connector can create and continue Codex App threads.

Possible approaches:

- Codex App native thread tools: best if callable from the connector's runtime.
- AgentRelay MCP server inside Codex App: Codex itself calls MCP tools to create/submit/poll tasks, while a small bridge handles thread delivery.
- Periodic Codex thread wakeup/automation: the relay wakes or messages the original thread when a reply arrives.

The implementation should start by proving that a connector can call `create_thread` for Frank and `send_message_to_thread` for Zac's original thread.
