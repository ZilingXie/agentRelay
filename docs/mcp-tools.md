# AgentRelay MCP Tools

Date: 2026-06-25

## Purpose

The AgentRelay MCP server lets Codex App agents call the AgentRelay HTTP API through tools instead of hand-written HTTP calls.

The MCP server is a stdio process:

```text
mcp/server.mjs
```

It wraps:

```text
http://127.0.0.1:8787/agentrelay
```

or another base URL configured by `AGENTRELAY_BASE_URL`.

## Install

```bash
npm install
```

For full local Codex setup instructions, use the public installer repo: `https://github.com/ZilingXie/agent-relay-mcp`.

## Run Relay

```bash
AGENTRELAY_DB_PATH=./data/agentrelay.sqlite3 python3 -m server.app
```

## Run MCP Server

```bash
AGENTRELAY_BASE_URL=http://127.0.0.1:8787/agentrelay node mcp/server.mjs
```

## Codex App MCP Config

Codex reads MCP server config from `~/.codex/config.toml`, or from a project-scoped `.codex/config.toml` in a trusted project.

Use this stdio MCP server config:

```toml
[mcp_servers.agentrelay]
command = "node"
args = ["/home/ubuntu/agentRelay/mcp/server.mjs"]
cwd = "/home/ubuntu/agentRelay"
startup_timeout_sec = 10
tool_timeout_sec = 60

[mcp_servers.agentrelay.env]
AGENTRELAY_BASE_URL = "http://127.0.0.1:8787/agentrelay"
```

If the relay is deployed behind HTTPS, use:

```text
https://server.stellarix.space/agentrelay/api
```

only after the live API is exposed by nginx or another reverse proxy. The static plan page alone does not expose the Python API.

## Tools

### `agentrelay_health`

Checks relay reachability.

### `agentrelay_list_agents`

Lists registered agents.

### `agentrelay_get_agent_card`

Gets the A2A-shaped card for one agent.

### `agentrelay_create_task`

Creates a task and records:

```text
requester_thread_id
done_criteria
completion_owner_agent_id
pending_on_agent_id
```

### `agentrelay_claim_task`

Claims the next task pending on an agent.

### `agentrelay_set_target_thread`

Records the target Codex App thread after creating or reusing it.

### `agentrelay_submit_artifact`

Submits an artifact. This does not complete the task by default. It transfers ownership back to the requester-side completion owner.

### `agentrelay_mark_delivery`

Records delivery back to `requester_thread_id`.

### `agentrelay_close_task`

Closes a task. Only `completion_owner_agent_id` should call this.

### `agentrelay_get_task`

Fetches task state, messages, and artifacts.

### `agentrelay_get_events`

Fetches task audit events.

### `agentrelay_update_status`

Updates relay transport status and pending ownership fields.

## Smoke Test

```bash
npm test
```

The MCP smoke test starts a temporary relay server, launches the MCP server over stdio, and runs:

```text
create task
frank-agent claim
record Frank thread
submit Frank artifact
zac-agent claim
mark delivery to requester thread
close by completion owner
```
