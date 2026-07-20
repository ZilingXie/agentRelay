# Local MCP Install for AgentRelay

Date: 2026-06-25

Private AgentRelay server repository: https://github.com/ZilingXie/agentRelay
Public MCP installer repository: https://github.com/ZilingXie/agent-relay-mcp

## Current decision

The AgentRelay server repo is private, so local Codex MCP installation instructions and the installable MCP client must live in the public `agent-relay-mcp` repo.

Use this public repo when asking a local Codex agent to install AgentRelay MCP:

```text
https://github.com/ZilingXie/agent-relay-mcp.git
```

This private repo can keep server-side implementation, relay API docs, and planning docs. The public MCP repo contains:

- the standalone stdio MCP client at `mcp/server.mjs`
- `package.json` and `package-lock.json`
- `scripts/install-codex-mcp.mjs`
- `INSTALL_FOR_CODEX.md`
- human-facing install docs
- smoke tests against a fake relay
- Phase 2 WebSocket listener and listener install docs

## Local install summary

On the machine running Codex:

```bash
git clone https://github.com/ZilingXie/agent-relay-mcp.git
cd agent-relay-mcp
npm install
node scripts/install-codex-mcp.mjs --write --base-url http://127.0.0.1:8787/agentrelay
```

For the current public cloud relay, use:

```bash
node scripts/install-codex-mcp.mjs --write \
  --base-url https://server.stellarix.space/agentrelay/api \
  --ws-url wss://server.stellarix.space/agentrelay/api \
  --agent-id zac-agent \
  --username zac
```

After the user fills `.env` with the token and restarts Codex, the local agent must run:

```bash
npm run doctor
npm run health:install
```

`doctor` checks HTTP health, authenticated `/agents`, and WebSocket `hello`.
`health:install` calls the server-hosted install loopback endpoint, waits for
the synthetic `agentrelay-healthcheck` ACK to arrive in the local inbox, and
closes the health check task.

Then keep the Phase 2 listener running:

```bash
npm run listener
```

or install it as a background listener:

```bash
npm run install:listener
```

Incoming `task.pending` notifications are written to:

```text
.agentrelay/inbox/
```

Restart Codex App or open a new session after installing.

## Server-hosted install loopback

The relay exposes:

```text
POST /agentrelay/api/healthchecks/install
```

In v0.5 mode this authenticated endpoint creates a synthetic Task from the
authenticated requester to the built-in `agentrelay-healthcheck` actor, records
the synthetic request as delivered, creates an ACK Message, and queues that
Message for the requester. The installer waits for its normal Listener ACK,
then completes the Task with the latest v0.5 Message/turn/version context. It
validates MCP auth, server reachability, v0.5 persistence, WebSocket/local
Listener delivery, Local Inbox state, and completion permissions. It does not
call Project Hermes or any remote agent adapter.

The endpoint supports client idempotency keys. Retrying the same install check
for the same requester returns the same synthetic task instead of creating a
second pending notification. Non-terminal install healthcheck tasks also have a
short TTL; stale checks are marked `expired` and their pending delivery state is
cleaned up by later healthcheck calls.

Real `project-hermes` tasks remain optional E2E collaboration tests. If a real
Hermes task fails after `health:install` passes, debug Hermes or its adapter
rather than the MCP install.

## Security note

The current public relay uses username/token authentication. The token belongs only in the local `.env`, not in Codex config or chat logs. WebSocket uses the same auth headers and can only subscribe to the token's own agent id.

## Source

Codex MCP configuration is documented in the official Codex manual: https://developers.openai.com/codex/mcp
