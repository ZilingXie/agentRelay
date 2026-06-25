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

## Local install summary

On the machine running Codex:

```bash
git clone https://github.com/ZilingXie/agent-relay-mcp.git
cd agent-relay-mcp
npm install
node scripts/install-codex-mcp.mjs --write --base-url http://127.0.0.1:8787/agentrelay
```

If Codex is local but the relay is on `server.stellarix.space`, keep an SSH tunnel open:

```bash
ssh -N -L 8787:127.0.0.1:8787 ubuntu@server.stellarix.space
```

Then keep `AGENTRELAY_BASE_URL` as:

```text
http://127.0.0.1:8787/agentrelay
```

Restart Codex App or open a new session after installing.

## Security note

Do not expose the current Phase 1 relay API publicly without authentication. The public MCP client supports optional `AGENTRELAY_TOKEN`, but the relay must implement token validation before public HTTPS is safe.

## Source

Codex MCP configuration is documented in the official Codex manual: https://developers.openai.com/codex/mcp
