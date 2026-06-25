# Local MCP Install for AgentRelay

Date: 2026-06-25

GitHub repository: https://github.com/ZilingXie/agentRelay

## Should this live in a public GitHub repo?

Yes. For Phase 1, keep the MCP install method in the existing public `agentRelay` repo instead of creating a second repo.

Why:

- The relay HTTP API, MCP wrapper, schemas, and install docs must stay versioned together.
- A new repo would add coordination overhead before the PoC proves the end-to-end meeting loop.
- Later, if the MCP wrapper becomes reusable by itself, split it into a dedicated `agentrelay-mcp` repo or publish it as an npm package.

## Mental model

AgentRelay has two local pieces:

1. **Relay HTTP server**: the Python server that stores tasks, artifacts, delivery state, and audit events.
2. **Codex MCP server**: the Node stdio process at `mcp/server.mjs`. Codex starts this process and calls its tools. The MCP process then talks to the relay HTTP server through `AGENTRELAY_BASE_URL`.

The MCP server does not start the Python relay automatically. Start the relay first, then start or restart Codex so Codex can launch the MCP server.

Codex MCP configuration belongs in `~/.codex/config.toml` for a user-wide setup, or `.codex/config.toml` for a trusted project-scoped setup. Codex supports stdio MCP servers, which is the mode AgentRelay uses for Phase 1.

## Option A: Codex and relay on the same machine

Use this when Codex App is running on the same VM or workstation as the Python relay.

### 1. Clone and install dependencies

```bash
git clone https://github.com/ZilingXie/agentRelay.git
cd agentRelay
npm install
```

If the repo already exists:

```bash
cd /home/ubuntu/agentRelay
git pull
npm install
```

### 2. Start the relay HTTP server

```bash
cd /home/ubuntu/agentRelay
mkdir -p data
AGENTRELAY_DB_PATH=./data/agentrelay.sqlite3 python3 -m server.app
```

Default URL:

```text
http://127.0.0.1:8787/agentrelay
```

Quick health check:

```bash
curl http://127.0.0.1:8787/agentrelay/health
```

Expected response:

```json
{
  "ok": true,
  "service": "agentrelay"
}
```

### 3. Add the MCP server to Codex config

Edit `~/.codex/config.toml` and add:

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

Then restart Codex App, or open a new Codex session/thread.

### 4. Verify inside Codex

Ask Codex to use the AgentRelay MCP server and run `agentrelay_health`.

In Codex CLI/TUI, `/mcp` can list active MCP servers. In Codex App, start a new thread after editing the config so the app can load the new MCP server.

## Option B: Codex on your local Mac, relay on this VM

Use this when Codex App runs on your laptop but the relay HTTP server runs on `server.stellarix.space`.

### Safer Phase 1 approach: SSH tunnel

Keep the Python relay bound to `127.0.0.1` on the VM and create a tunnel from your laptop:

```bash
ssh -L 8787:127.0.0.1:8787 ubuntu@server.stellarix.space
```

On your laptop, clone the repo and configure Codex with your laptop path:

```bash
git clone https://github.com/ZilingXie/agentRelay.git
cd agentRelay
npm install
```

Example `~/.codex/config.toml` on macOS:

```toml
[mcp_servers.agentrelay]
command = "node"
args = ["/Users/zac/agentRelay/mcp/server.mjs"]
cwd = "/Users/zac/agentRelay"
startup_timeout_sec = 10
tool_timeout_sec = 60

[mcp_servers.agentrelay.env]
AGENTRELAY_BASE_URL = "http://127.0.0.1:8787/agentrelay"
```

Why this works: Codex launches the MCP server locally on your laptop, and the MCP server calls `http://127.0.0.1:8787/agentrelay`, which is forwarded through SSH to the VM relay.

### Later approach: public HTTPS relay API

Eventually AgentRelay can expose an authenticated HTTPS API, for example:

```text
https://server.stellarix.space/agentrelay/api
```

Do not expose the current Python relay API publicly without authentication. Phase 1 has no auth, per-agent tokens, replay protection, or rate limits yet.

## Minimal tool test

After Codex loads the MCP server, these tools should be available:

```text
agentrelay_health
agentrelay_list_agents
agentrelay_get_agent_card
agentrelay_create_task
agentrelay_claim_task
agentrelay_set_target_thread
agentrelay_submit_artifact
agentrelay_mark_delivery
agentrelay_close_task
agentrelay_get_task
agentrelay_get_events
agentrelay_update_status
```

A minimal first prompt in Codex App:

```text
Use the AgentRelay MCP server. First call agentrelay_health. If it is healthy, list agents.
```

## Troubleshooting

- **MCP server does not appear**: restart Codex App or open a new session after editing `~/.codex/config.toml`.
- **`node` not found**: install Node.js 18+ or use an absolute path to `node` in `command`.
- **Health check fails**: confirm the Python relay is running and `AGENTRELAY_BASE_URL` points to `/agentrelay`.
- **Mac cannot reach VM relay**: use the SSH tunnel, or deploy a secured HTTPS API before using a public URL.
- **Wrong repo path**: `args` and `cwd` must match the actual path on the machine running Codex.

## Source

Codex MCP configuration is documented in the official Codex manual: https://developers.openai.com/codex/mcp
