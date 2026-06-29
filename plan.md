# AgentRelay PoC Plan

GitHub repository: https://github.com/ZilingXie/agentRelay

## 1. 原始想法整理

你想探索的问题不是“一个 agent 怎么调用工具”，而是“本地 agent 如何和远端另一个人的 agent 协作”。你的直觉是把复杂的实时协议先降级成类似 email 的异步机制：

- 有一个根目录 `agentRelay/`。
- 根目录里有 `agentlist.md`，类似 `skills.md`，描述可联系的 agents、能力、收件地址和权限边界。
- 每个 agent 有自己的 inbox。
- 有一个 archive 目录保存处理完的消息。
- 人类可以只对自己的 agent 说目标，比如“我想和 Frank 开会，但不知道他的时间”。
- Zac 的 agent 写一封结构化 message，放进 Frank agent 的 inbox。
- Frank agent 周期性检查 inbox，读取新消息，判断是否需要行动。
- 如果不需要下一步，归档。
- 如果需要下一步，先执行可自动完成的部分；涉及隐私、授权、承诺或不确定信息时向 Frank 确认。
- Frank agent 完成后写 reply，投递到 Zac agent 的 inbox。
- 两边持续往返，直到任务完成，最后归档。

这个模型的核心价值是：让人类从“复制粘贴中转站”变成只处理授权、偏好和例外的监督者。

## 2. 当前目录结构

```text
agentRelay/
  agentlist.md
  plan.md
  phase1-plan.md
  inboxes/
    zac-agent/
    frank-agent/
  outbox/
  drafts/
  messages/
    example-meeting-request.md
  archive/
```

说明：

- `inboxes/<agent-id>/` 是每个 agent 的收件箱。
- `outbox/` 可用于记录本地 agent 已发送但未确认投递的消息。
- `drafts/` 可用于需要人类确认后再发送的消息。
- `messages/` 存放消息模板和示例，不参与投递。
- `archive/` 存放完成、拒绝、过期或重复的消息。
- `phase1-plan.md` 记录第一阶段 Codex App thread 闭环计划。

## 2.1 第一阶段计划更新

Progress:

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
- [x] Implement AgentRelay MCP tools that wrap the relay HTTP API.
- [x] Publish standalone local Codex MCP installer in `ZilingXie/agent-relay-mcp`.
- [x] Add Phase 1 username/token auth support for public MCP clients.
- [x] Deploy AgentRelay behind Docker Compose and nginx HTTPS reverse proxy.
- [x] Configure Codex App to use AgentRelay MCP and run the full Phase 1 meeting scenario.

第一阶段不以 CLI 作为用户体验入口，而以 Codex App thread 作为入口。

核心要求：

- Zac 在 Codex App thread `abc` 中发起请求。
- Zac Codex 通过 AgentRelay skill/MCP 创建 task，并记录 `requester_thread_id=abc`。
- 本地安装方式放在 public repo `https://github.com/ZilingXie/agent-relay-mcp`；private `agentRelay` repo 只保留 server 和计划。
- Frank connector 定期 `GET /workers/frank-agent/claim`。
- 有新 claim 时，Frank connector 在 Frank 的 Codex App 中创建 thread；如果同一 task 已经有 thread，则复用。
- Frank 在自己的 Codex thread 中给出答复。
- Frank Codex 通过 AgentRelay skill/MCP 提交 artifact/result。
- AgentRelay 必须把结果投递回 Zac 的原始 thread `abc`，而不是新建 Zac thread。

详细计划见 `phase1-plan.md`。

Task completion policy:

- 区分 `agent action complete` 和 `workflow task complete`。
- `done_criteria` 由 requester side 在创建 task 时提出。
- requester agent 是 semantic completion owner。
- Relay 保存 `done_criteria` 作为 metadata，维护 transport state、ownership transfer、TTL 和审计。
- Agent 只能提交 action result、artifact、next_action 建议，不能单方面替代 requester 关闭整个 workflow。
- Frank agent 回复 10:00 后，Frank action complete，但 task 应转为 pending on Zac confirmation。
- Zac 长时间不回复时进入 `waiting_human`，走 reminder/TTL/expired。
- 已完成 task 不重新打开；Zac 之后变更时间时创建同 `context_id` 的 child task。

详细规则见 `docs/task-completion-policy.md`。


## 2.2 第二阶段计划：WebSocket Notify Push

Phase 2 的目标是在不删除现有手动 fetch / HTTP claim 方式的前提下，新增 cloud push 能力。

核心决策：

- 保留现有 REST API、MCP tools、手动 `claim` / `get_task` 流程。
- 新增 WebSocket notify，避免本地用 cron 轮询。
- 本地 listener 和 Codex App thread 创建/复用属于本地逻辑，不进入 cloud relay 语义判断。
- Cloud 只负责 durable task state、auth、pending event outbox、WebSocket 推送、precise claim 和 thread binding metadata。

Phase 2 新增 cloud endpoints：

```text
GET  /agentrelay/api/workers/:agentId/pending
POST /agentrelay/api/workers/:agentId/tasks/:taskId/claim
GET  /agentrelay/api/workers/:agentId/events/ws
POST /agentrelay/api/workers/:agentId/events/:eventId/ack
```

Phase 2 新增数据模型：

```text
agent_events           # durable WebSocket event outbox
task_thread_bindings   # per-agent local thread mapping
```

Progress:

- [x] Define Phase 2 WebSocket notify push plan.
- [x] Add durable `agent_events` schema/helper foundation.
- [x] Add per-agent `task_thread_bindings` schema/helper foundation.
- [x] Add Phase 2 store smoke test to `npm test`.
- [x] Add pending summaries and precise task claim API.
- [x] Add event ack API with thread binding writeback.
- [x] Add Phase 2 API smoke test to `npm test`.
- [x] Emit pending events from task state transitions.
- [x] Add automatic pending event smoke coverage.
- [x] Add WebSocket sidecar and WSS deployment.
- [x] Add WebSocket smoke test to `npm test`.
- [x] Update public `agent-relay-mcp` repo with listener install/verification flow.
- [x] Define user-owned local inbox hook/adapter contract in public `agent-relay-mcp` README.
- [x] Zac and Frank reinstall public MCP repo and verify doctor/MCP/WSS.
- [x] Run two-agent test using local listeners.

部署方式：

```text
docker compose agentrelay-api -> REST server on 127.0.0.1:8787
docker compose agentrelay-ws  -> WebSocket sidecar on 127.0.0.1:8788
host nginx                    -> /agentrelay/api/... REST + WSS routes
```

详细实施计划见 `phase2-plan.md`。

## 2.3 Docker runtime migration

目标：把当前云端 AgentRelay runtime 从 systemd 直接运行 Python 进程，迁移为 Docker Compose 管理，同时保持 host nginx、HTTPS 域名、REST API、WebSocket endpoint 和 auth/data 文件路径语义不变。

迁移原则：

- 不把 `data/`、sqlite、auth token、`.env` 或日志打进镜像。
- host nginx 继续负责 TLS 和 `/agentrelay/api` reverse proxy。
- Docker 只承载两个 Python 进程：REST API 和 WebSocket sidecar。
- 两个容器共享同一个 host bind mount：`./data:/app/data`。
- 默认只绑定 `127.0.0.1:8787` 和 `127.0.0.1:8788`，不直接暴露公网端口。
- systemd 旧服务先保留，作为快速 rollback 路径。

Progress:

- [x] Add Dockerfile for the Python server runtime.
- [x] Add docker-compose.yml with `agentrelay-api` and `agentrelay-ws` services.
- [x] Keep runtime data outside the image through bind-mounted `data/`.
- [x] Add `.dockerignore` to exclude state, credentials, dependencies, and large references.
- [x] Add Docker deployment/cutover/rollback docs.
- [x] Verify Docker stack on temporary ports `18787` and `18788`.
- [x] Cut production from systemd services to Docker Compose.
- [x] Verify public HTTPS REST and WSS after cutover.
- [x] Disable old `agentrelay` and `agentrelay-ws` systemd services after stable operation.

详细说明见 `docs/docker-deployment.md`。

## 2.4 Phase 3: Agent Collaboration Protocol

下一阶段目标不是继续让 relay 变 heavy，而是把已经跑通的 agents 协作方式产品化/协议化。

核心判断：

```text
AgentRelay = public relay, durable state, auth, notification, audit
Agent Collaboration Protocol = agents 如何协作解决问题
Local adapters = Codex App, Codex CLI, WeChat, Slack, or custom workflow
```

Phase 1/2 已经定义出的协议事实：

- 每个 human owner 有自己的 local agent。
- requester agent 创建 task，并定义 `done_criteria`。
- `completion_owner_agent_id` 通常是 requester agent，负责语义完成判断。
- target agent 可以完成自己的 action，但不能单方面关闭整个 workflow。
- artifact 表示 action result，不等同于 task complete。
- `pending_on_agent_id` / `pending_on_human_id` 定义下一步责任归属。
- WebSocket `task.pending` 只是通知；HTTP task state 才是 source of truth。
- 本地 inbox 到 Codex App/CLI/WeChat/Slack 的投递由用户自己的 adapter/hook 决定。
- terminal task 不重新打开；后续变更创建同 `context_id` 的 child task。

Phase 3 Progress:

- [x] Create `phase3-plan.md`.
- [x] Document current Phase 1/2 communication semantics in `docs/agent-collaboration-protocol-v0.md`.
- [ ] Add `protocol_version` to task/message/artifact/event payloads.
- [ ] Create JSON schemas for task creation, artifact submission, status transition, close, and agent events.
- [ ] Implement a task state transition validator.
- [ ] Add negative tests for invalid transitions and unauthorized completion.
- [ ] Add idempotency keys for create/artifact/ack/close operations.
- [ ] Add first-class human approval records.
- [ ] Expand agent cards with capabilities, scopes, and accepted task types.
- [ ] Add A2A mapping document and minimal compatibility endpoint plan.

详细计划见 `phase3-plan.md`。

## 3. 建议的消息格式

先用 Markdown 加 YAML front matter，方便人和 agent 都能读：

```markdown
---
protocol: agenthub-mail-v0
message_id: msg_20260625_001
thread_id: meet_frank_20260625
from: zac-agent
to: frank-agent
subject: Request Frank availability for meeting
created_at: 2026-06-25T10:00:00+08:00
reply_to: msg_20260625_000
status: new
priority: normal
requires_human: maybe
confidentiality: private
ttl: 2026-06-30T23:59:59+08:00
---

## Request

Zac wants to schedule a meeting with Frank.

## Context

- Desired duration: 30 minutes
- Timezone: Asia/Shanghai unless otherwise specified
- Flexible dates: next week

## Requested Next Step

Please check Frank's available slots and reply with 2-3 candidate times.

## Human Confirmation Boundary

Ask Frank before exposing calendar details or committing to a meeting.
```

最小必备字段：

- `message_id`: 去重和审计。
- `thread_id`: 多轮对话归属。
- `from` / `to`: 路由。
- `subject`: 人类可扫读。
- `created_at`: 排序和超时。
- `status`: `new | processing | waiting-human | replied | done | rejected | expired | error`。
- `requires_human`: `no | maybe | yes`。
- `confidentiality`: `public | private | sensitive`。

## 4. 处理生命周期

1. Sender agent 生成 message 到 `drafts/`。
2. 如果 `requires_human: yes`，先让 owner 审批。
3. 审批通过后，sender 将 message 投递到 receiver inbox。
4. Receiver watcher 发现新 message，并用原子 rename 或 lock 标记为 `processing`。
5. Receiver agent 检查：
   - 是否是发给自己的。
   - sender 是否在信任列表。
   - message 是否重复、过期或格式非法。
   - 是否需要人类确认。
6. Receiver agent 执行可自动完成的步骤。
7. 如需人类确认，写入 `waiting-human` 状态并通知 owner。
8. 完成后生成 reply，投递回 sender inbox。
9. 原始 message 和中间产物进入 `archive/<thread_id>/`。
10. 当 thread 达到 `done | rejected | expired`，两边停止自动循环。

## 5. 远端 agent 通信方式调研

### A2A: 最接近目标的正式协议

Agent2Agent Protocol, A2A, 是目前最贴近“本地 agent 和远端 agent 作为 peers 协作”的协议。官方描述它用于不同框架、不同厂商、不同组织下的 agent 互操作。核心概念包括：

- Agent Card: 远端 agent 发布自己的身份、能力、endpoint、认证方式。
- Message: agent 之间交换上下文、请求和回复。
- Task: 任务级生命周期，适合长任务和多轮协作。
- Artifact: 任务输出，比如文档、结构化数据、文件。
- Streaming / push notifications: 支持长任务的增量更新和异步通知。
- HTTPS + auth: 生产部署依赖标准 Web 安全机制。

它和你的 mailbox 想法高度相似：`agentlist.md` 对应简化版 Agent Card registry；`thread_id` 对应 task/context；`archive` 对应 task history/artifacts；watcher 轮询 inbox 对应简化版 push notification 或 task polling。

差异是：A2A 是网络协议，强调 HTTP/JSON-RPC/gRPC/REST binding、能力发现、认证、错误码、版本协商；你的 PoC 是文件系统协议，强调易实现、易观察、易调试。

### MCP: 适合做 mailbox 工具层，不是主要 agent-to-agent 协议

Model Context Protocol, MCP, 主要解决 agent 连接外部系统的问题，例如文件、数据库、搜索、业务 API 和工作流。它不是原生的 agent-to-agent 协议，但非常适合把 AgentHub 暴露成工具：

- `list_agents`
- `send_message`
- `read_inbox`
- `claim_message`
- `archive_message`
- `get_thread`

也就是说，AgentHub 可以先是一个文件夹，下一步变成一个 MCP server。这样 Codex、Claude、Hermes 或其他支持 MCP 的本地 agent 都能用同一组工具读写 mailbox。

### ACP: 名字容易混淆

现在至少有两个常见 ACP：

- Agent Communication Protocol: IBM/BeeAI 方向的 agent interoperability 协议，RESTful、异步优先、支持多模态和 agent run lifecycle。公开资料显示它后来并入 A2A/LF AI & Data 生态，因此如果从零开始，优先参考 A2A 更稳。
- Agent Client Protocol: Zed/JetBrains 等推动的 editor/IDE 与 coding agent 的协议，目标是让编辑器和 coding agent 解耦。它可以覆盖本地或远端 agent，但它的主要关系是 client-to-coding-agent，不是任意 agent-to-agent 协作。

### ANP: 更偏开放网络和去中心化身份

Agent Network Protocol, ANP, 目标更像“Agentic Web”：用 DID、JSON-LD、agent description、agent discovery、协议协商来让开放网络上的 agents 发现和连接彼此。它的愿景很适合大规模 agent 网络，但对当前 PoC 来说偏重。

如果你未来关心跨组织、跨域名、去中心化身份、公开 agent marketplace，ANP 值得看；如果只是让两个已知 owner 的 Codex-like agents 互相协作，A2A 思路加 mailbox PoC 更快。

## 6. 对当前计划的批判性评估

### 优点

- 简单：文件夹和 Markdown 很容易实现，几乎没有基础设施成本。
- 可观察：人类可以直接打开每封 message，看见 agent 到底说了什么。
- 异步：不要求两个 agent 同时在线。
- 容错：crash 后 inbox/archive 仍在，容易恢复。
- 权限边界清楚：可以把“需要问人”的状态写进消息。
- 很适合 PoC：先验证协作语义，再决定是否上 A2A、MCP server、webhook 或 queue。

### 不足和风险

- 远端投递问题没解决：一个 VM 文件夹只解决同一台机器或共享挂载，真正远端 agent 需要 transport，例如 HTTPS、SSH、对象存储同步、Git、S3、Tailscale/ZeroTier、队列或 A2A endpoint。
- 身份认证不足：`from: frank-agent` 只是文本，任何进程都能伪造。至少需要 signed messages 或受控投递 API。
- 并发和锁问题：cron 轮询 inbox 时可能两个 worker 同时处理同一封 message。需要 claim/lock/atomic rename。
- 消息状态可能漂移：如果 agent 修改 Markdown front matter，状态和正文可能不一致。更可靠的做法是把 envelope metadata 和 body 分离，或用 JSONL event log。
- 审计链不完整：归档文件能保存结果，但不能天然证明谁在何时读过、改过、批准过。
- prompt injection 风险：远端 agent 发来的 message 本质是不可信输入，不能让它直接覆盖本地 agent 的系统规则或读取敏感文件。
- 隐私边界需要显式化：例如“查看 Frank 日历”不等于“把 Frank 的全部空闲时间发给 Zac”。应只回复满足请求所需的最小信息。
- 无限循环风险：两个 agent 互相追问可能陷入循环。需要 `max_turns`、`ttl`、`done criteria`、`requires_human` 升级规则。
- 缺少失败语义：需要定义 reject、timeout、cannot-complete、need-more-info、partial-complete。
- agentlist.md 作为 registry 太弱：适合 PoC，但以后应升级到 signed Agent Card 或至少 JSON schema。
- cron 不够实时也不够精细：PoC 可用 cron；正式化后应改为 filesystem watcher、queue consumer、webhook 或 A2A push notification。

## 7. 推荐 PoC 路线

### Phase 0: 文件夹 PoC

目标：验证“不要问我，问我的 agent”是否真的减少人类中转。

要做：

- 固定目录结构。
- 固定 message schema。
- 写一个 watcher 脚本，轮询 `inboxes/<agent-id>/`。
- 实现 claim/processing/archive。
- 实现 reply 投递。
- 对 `requires_human` 先只生成 draft，不自动发送。

成功标准：

- Zac agent 能给 Frank agent 发起会议请求。
- Frank agent 能识别需要 human confirmation。
- Frank 确认后，Frank agent 回复候选时间。
- Zac agent 能把候选时间整理给 Zac。
- 整个 thread 最终归档。

### Phase 1: MCP 化

目标：让不同本地 agent 都能通过工具访问 mailbox，而不是直接读写文件。

要做：

- 建一个 AgentRelay MCP server。
- 暴露 `send_message/read_inbox/claim_message/archive_message/list_agents/get_thread`。
- MCP server 负责锁、schema 校验和审计。
- agent 不再直接写文件，只调用工具。

### Phase 2: 远端 transport

目标：让本地 agent 能投递给远端 owner 的 agent。

可选 transport：

- HTTPS webhook: 简单直接，适合 A2A-like 演进。
- SSH dropbox: 适合你已有 VM/SSH 环境，部署快。
- Object storage sync: 适合离线和异步，但状态一致性较麻烦。
- Message queue: 稳定但基础设施更重。
- A2A endpoint: 最标准，适合走向跨组织互操作。

建议：先用 SSH 或 HTTPS webhook，不要一开始上完整实时 messaging。

### Historical Phase 3 idea: A2A 对齐

早期目标是让 AgentRelay 从“自定义 mailbox”升级为“A2A-compatible gateway”。现在 Phase 3 的判断更精确：先定义 Agent Collaboration Protocol，再把它映射到 A2A。A2A 是互操作目标，不应该让 relay 变成决定任务语义的大脑。

映射关系：

- `agentlist.md` -> Agent Card registry。
- `message_id/thread_id` -> A2A message/task/context ids。
- `archive/<thread_id>/` -> task history/artifacts。
- `requires_human` -> task state `input-required` 或 `auth-required` 的本地语义。
- watcher/reply -> task update, polling, streaming 或 push notification。

## 8. 我建议保留和修改的地方

保留：

- mailbox 隐喻。
- Markdown 可读消息。
- 每个 agent 独立 inbox。
- archive。
- human confirmation boundary。
- 异步优先。

修改：

- 不要让 cron 直接“执行邮件中的指令”；先经过 schema validation、trust check、policy check。
- 不要只靠文件名判断是否处理过；使用 `message_id` 去重。
- 不要把所有完成消息扔进一个 archive；按 `thread_id` 分目录。
- 不要让 agentlist.md 只是自然语言；至少加固定字段，后续可迁移成 JSON Agent Card。
- 不要默认自动回复所有消息；涉及隐私、承诺、身份、金钱、外部副作用时必须 human approval。

## 9. 关键开放问题

- Agent 身份怎么证明？共享目录 PoC 可以先靠文件权限，远端版本需要签名或认证。
- “远端”具体怎么连？SSH、HTTPS、对象存储、A2A server，还是先只在同一 VM 模拟？
- 每个 agent 的 owner approval UI 是什么？命令行、Codex thread、Slack/Telegram、email，还是只写 draft 文件？
- 消息能携带附件吗？如果可以，附件路径、hash、权限和过期策略要定义。
- thread 什么时候算完成？由发起方决定，接收方决定，还是双方都写 `done`？
- agent 能否代表人类做承诺？如果能，哪些承诺需要审批？

## 10. 当前判断

你的计划作为 PoC 是对的，尤其是“email-like async mailbox”这个选择。它能快速暴露真正难点：身份、权限、状态、审批、投递、归档和失败处理。

但它不应该停留在“共享文件夹 + cron 执行 Markdown”。更稳的路线是：

```text
Folder PoC -> MCP/listener relay -> Agent Collaboration Protocol -> A2A-compatible gateway
```

这样既不被完整 A2A 的复杂度拖住，又不会走进一个只能本机玩具化运行的死胡同。

## 11. 参考资料

- A2A Protocol latest specification: https://a2a-protocol.org/latest/specification/
- A2A and MCP comparison: https://a2a-protocol.org/latest/topics/a2a-and-mcp/
- A2A What is A2A: https://a2a-protocol.org/latest/topics/what-is-a2a/
- MCP introduction: https://modelcontextprotocol.io/docs/getting-started/intro
- MCP Streamable HTTP transport: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports
- MCP authorization: https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization
- Agent Communication Protocol docs: https://agentcommunicationprotocol.dev/introduction/welcome
- IBM Research ACP note: https://research.ibm.com/projects/agent-communication-protocol
- Agent Client Protocol introduction: https://agentclientprotocol.com/get-started/introduction
- Agent Network Protocol: https://agent-network-protocol.com/

## Auth update

Phase 1 auth is documented in `docs/relay-auth.md`. The cloud relay issues `username + agent_id + token`; the public MCP client stores these in `.env` and sends bearer-token headers.

Deployment update: `docs/relay-deployment.md` and `docs/docker-deployment.md` record the Docker Compose runtime and nginx reverse proxy for `https://server.stellarix.space/agentrelay/api`.

Public MCP installer update: `ZilingXie/agent-relay-mcp` now explicitly requires the local agent to write `.env`, report the `.env` path without printing the token, run `npm run doctor`, then verify MCP with `agentrelay_health` and `agentrelay_list_agents` after Codex restart/new thread. See `docs/local-agent-verification.md` in the public MCP repo.

Public MCP installer correction: install docs now use a two-phase flow. Phase A configures Codex and writes a `.env` template, then stops and asks the user to fill `.env` and restart/open a new Codex session. Phase B starts only after the user says that is done: the agent runs `npm run doctor`, then verifies MCP with `agentrelay_health` and `agentrelay_list_agents`.

Local adapter boundary update: the public `ZilingXie/agent-relay-mcp` README defines receive modes and the local hook contract. Manual mode uses HTTP/MCP pending checks. Automatic mode uses the WebSocket listener, writes incoming work to `.agentrelay/inbox/`, and lets the user choose a local `AGENTRELAY_LISTENER_HOOK` adapter for Codex App, Codex CLI, WeChat, Slack, or another workflow. This keeps cloud AgentRelay as a relay instead of hard-binding it to one human interface.
