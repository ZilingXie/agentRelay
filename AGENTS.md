# AgentRelay Development Rules

These rules apply to `/home/ubuntu/projects/agentrelay/agentRelay`.

## Project Map

- This repo is the AgentRelay server/cloud relay: protocol authority, HTTP/WSS relay, SQLite state, auth, delivery reliability, audit/timeline, admin dashboard, Docker deployment, and roadmap docs.
- The client/agent-side MCP project is separate: `/home/ubuntu/projects/agentrelay/agent-relay-mcp` and <https://github.com/ZilingXie/agent-relay-mcp>.
- Server-side working plan: `server_plan.md`.
- Canonical overall project plan: `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.
- Public roadmap URL: <https://server.stellarix.space/agentrelay/plan.html#intro>. Publish by syncing the canonical plan to `/var/www/html/agentrelay/plan.html`.
- Protocol docs: `docs/protocol-v03.md`; public schemas: `schemas/`; examples: `examples/protocol-v03/`.
- Repo-local `plan.md`, `phase3-plan.md`, and `plan.html` are historical/project-local references unless the user explicitly asks to refresh them.

## Required Workflow

1. Before starting new functional work, keep `main` clean and synchronized with `origin/main`.
2. If local `main` has uncommitted changes, stop and confirm with the user before doing anything else.
3. Do not make feature, protocol, server behavior, deployment, dashboard, or MCP-facing API changes directly on `main`.
4. For any code or non-trivial docs change, create a task branch/worktree under `/home/ubuntu/projects/agentrelay/`:

   ```bash
   git worktree add -b <branch-name> ../agentRelay-<short-slug> main
   ```

5. Work only in that task worktree. Keep changes scoped and preserve user/runtime state.
6. Run targeted verification, then commit task-owned files only.
7. Push the branch, open a PR to `main`, and merge after verification.
8. After opening or updating the PR, refresh CodeGraph from the task worktree:

   ```bash
   npm run codegraph:status
   ```

   If pending sync is reported, run `npm run codegraph:sync`, then rerun `npm run codegraph:status`.

9. Fast-forward `/home/ubuntu/projects/agentrelay/agentRelay` to `origin/main`.
10. Remove only the task-owned worktree/local branch after the PR is merged.

## Safety Boundaries

- Never print or commit secrets/runtime state: `.env`, `data/`, SQLite databases, auth files, logs, tokens, or generated runtime artifacts.
- Keep AgentRelay small: route, persist, authorize, notify, audit, and enforce transport/state invariants. Do not turn the relay into a local agent brain or hardcoded UX adapter.
- Local inbox-to-user-workflow adapters belong in the MCP/client repo or user-owned integrations, not the cloud relay.
- Preserve compatibility unless the user explicitly approves a breaking migration. Prefer additive Protocol v0.3+ behavior.

## Protocol Boundaries

- Agent roles are `personal_agent` and `service_agent`.
- Role is descriptive; permissions are expressed through `execution_mode`, `protocol_capabilities`, and `policy`.
- `personal_agent` defaults to notifier-first behavior and may amend/close requester-owned work only with human authority.
- `service_agent` may claim assigned work and submit artifacts, but must not change requester-owned goals.
- Artifact submission does not automatically complete a task.
- Close is controlled by `completion_owner_agent_id`; human completion authority is recorded through the authorized agent.
- WebSocket push must remain secret-safe; full task payloads are fetched through authenticated HTTP.

## Validation

- Docs-only: inspect the changed text and run `git diff --check`.
- Schema/protocol docs/examples: run `npm run test:schema`; run the relevant protocol smoke test when behavior semantics change.
- Server behavior, auth, task lifecycle, event delivery, dashboard, or deployment code: run `npm test` unless a narrow targeted subset is clearly sufficient.
- Docker/runtime: rebuild/restart with `sudo docker compose up -d --build`, verify `https://server.stellarix.space/agentrelay/api/health`, and verify one task-specific live marker.
- Public roadmap: update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`, mark completed features with PR links, copy it to `/var/www/html/agentrelay/plan.html`, and verify `https://server.stellarix.space/agentrelay/plan.html`.

## Roadmap Updates

- After completed server changes, or any planning pass that changes direction/priorities, update both `server_plan.md` and `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.
- Do not leave completed public-roadmap work marked as pending.

## Deployment

Production runs from this repo with Docker Compose:

```text
agentrelay-api -> 127.0.0.1:8787
agentrelay-ws  -> 127.0.0.1:8788
host nginx     -> https://server.stellarix.space/agentrelay/...
```

Runtime state is bind-mounted under `data/`. Do not bake credentials or SQLite state into images.

## Final Report

Include:

- What changed and why.
- PR/commit links when applicable.
- Verification commands and results.
- Deployment/public-page verification when applicable.
- CodeGraph status after PR work, when a PR was opened or updated.
- Whether `server_plan.md` and `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` were updated.
- Any residual risk or skipped checks.
