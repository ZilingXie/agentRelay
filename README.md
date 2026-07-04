# AgentRelay

AgentRelay is a PoC for A2A-shaped communication between local Codex-style agents that do not have public IP addresses.

The relay server runs on a public VM. Local agents connect outward, claim tasks, create or reuse Codex App threads, ask their human owners when needed, and send results back through the relay.

## Phase 1 Goal

Run the meeting-scheduling loop end to end:

```text
Zac Codex App thread
  -> AgentRelay
  -> Frank Codex App thread
  -> Frank approval/reply
  -> AgentRelay
  -> original Zac Codex App thread
```

The important Phase 1 requirement is thread reuse:

- Zac's original thread must be stored as `requester_thread_id`.
- Frank's claimed task thread must be stored as `target_thread_id`.
- Replies must return to Zac's original thread, not a new thread.

## Current Docs

- GitHub: https://github.com/ZilingXie/agentRelay
- A2A upstream reference: `references/a2a`
- `plan.md`: overall AgentRelay plan
- `phase1-plan.md`: Phase 1 Codex App thread loop
- `phase3-plan.md`: Phase 3 agent collaboration protocol roadmap
- `docs/agent-collaboration-protocol-v0.md`: current Phase 1/2 collaboration protocol shape
- `docs/thread-bridge-proof.md`: Codex App thread creation and reuse proof
- `docs/origin-thread-delivery-proof.md`: Zac origin thread delivery proof
- `docs/task-completion-policy.md`: task completion, ownership transfer, timeout, and follow-up policy
- `docs/codex-app-bridge-flow.md`: reusable Codex App bridge flow
- `docs/mcp-tools.md`: AgentRelay MCP tool server usage
- `docs/relay-auth.md`: Phase 1 relay username/token auth
- `docs/relay-deployment.md`: relay deployment notes
- `docs/docker-deployment.md`: Docker Compose deployment and rollback notes
- `docs/admin-cli.md`: local admin/debug CLI for inspecting agents, tasks, timelines, events, and pending work
- Public MCP installer repo: https://github.com/ZilingXie/agent-relay-mcp
- `docs/local-mcp-install.md`: pointer to the public MCP install repo
- `bridge/`: bridge contracts and prompt templates
- `plan.html`: public planning page deployed to `https://server.stellarix.space/agentrelay/plan.html`
- `agentlist.md`: draft agent registry

## Phase 1 Progress

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

## Phase 2 Progress

- [x] Add WebSocket notify push without removing manual fetch.
- [x] Add durable pending events, precise claim, event ack, and per-agent thread bindings.
- [x] Publish public MCP/listener install and verification flow.
- [x] Document user-owned local inbox hook/adapter contract in `ZilingXie/agent-relay-mcp`.
- [x] Migrate production runtime to Docker Compose.
- [x] Verify Zac/Frank two-agent local-listener message and reply loop.

## Phase 3 Direction

Phase 3 turns the working PoC into a productizable agent collaboration protocol. The relay remains transport and durable state; the agents own reasoning, human approval, and semantic completion.

Next implementation slice:

- [x] Create Phase 3 plan.
- [x] Document the current Phase 1/2 agent communication protocol.
- [x] Add public JSON schemas, schema conformance tests, and agent-first response envelopes.
- [x] Publish Protocol v0.3 guide and machine-validated example payloads.
- [x] Add Protocol v0.3 conformance runner for local and real relay checks.
- [x] Implement and test an explicit task state transition validator.
- [x] Add reliable event delivery, source refs, approval summaries, Agent Cards, and A2A mapping.
- [x] Add admin/debug CLI for agents, tasks, timelines, events, and pending work.

See `phase3-plan.md` and the public `plan.html` for the current roadmap.

## Add A User / Agent

On the relay server, run:

```bash
cd /home/ubuntu/agentRelay
scripts/create_agent_identity.sh <username>
```

Examples:

```bash
scripts/create_agent_identity.sh frank
scripts/create_agent_identity.sh "Frank Xie" frank-agent
```

The command creates or replaces the user's cloud auth token, creates/updates the matching agent registry row, writes a local env copy under `data/local-env/`, and restarts the running relay when Docker Compose or legacy systemd services are detected.

Afterward, send the generated `.env` values to the user's local `agent-relay-mcp/.env` privately. Do not paste tokens into chats, commits, or logs.

Verify from a configured local MCP client with:

```text
agentrelay_health
agentrelay_list_agents
```

See `docs/relay-auth.md` for details and token rotation notes.

## Run Locally

```bash
AGENTRELAY_DB_PATH=./data/agentrelay.sqlite3 python3 -m server.app
```

Smoke test:

```bash
python3 scripts/smoke_test.py http://127.0.0.1:8787
```

MCP smoke test:

```bash
npm test
```

Admin/debug CLI:

```bash
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 summary
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 pending frank-agent
python3 scripts/agentrelay_admin.py --format json --db-path data/agentrelay.sqlite3 events --state failed
```

The smoke test verifies:

- `done_criteria` and `completion_owner_agent_id` are stored.
- Frank artifact submission does not complete the task.
- Artifact submission transfers ownership back to `zac-agent`.
- Delivery to `requester_thread_id` records `reply.delivered`.
- Non-owner close is rejected.
- Requester-side owner close completes the task.

## MCP Server

```bash
AGENTRELAY_BASE_URL=http://127.0.0.1:8787/agentrelay node mcp/server.mjs
```

See `docs/mcp-tools.md` and the public installer repo `https://github.com/ZilingXie/agent-relay-mcp`.

## First Implementation Milestone

Build the smallest vertical slice:

1. Relay data model for agents, tasks, messages, artifacts, events, and thread mapping.
2. A2A-shaped task creation and task lookup endpoints.
3. Worker claim endpoint for `frank-agent`.
4. Codex App bridge proof for creating/reusing Frank threads and sending replies back to Zac's origin thread.
