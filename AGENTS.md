# AgentRelay Development Rules

These instructions apply to `/home/ubuntu/projects/agentrelay/agentRelay`.

## Source Of Truth

1. `AGENTS.md` is the hot-path instruction file for this repository. Keep it concise enough to load frequently.
2. This repo is the AgentRelay server/cloud relay project: protocol authority, HTTP/WSS relay, SQLite state, auth, delivery reliability, audit/timeline, admin dashboard, Docker deployment, and roadmap docs.
3. The client/agent-side MCP project is separate: `/home/ubuntu/projects/agentrelay/agent-relay-mcp` and <https://github.com/ZilingXie/agent-relay-mcp>.
4. Server-side working plan for Codex/maintainers lives in `server_plan.md`.
5. The only canonical overall project plan for the user lives at `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.
6. The public roadmap URL is <https://server.stellarix.space/agentrelay/plan.html#intro>; publish it by syncing `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` to `/var/www/html/agentrelay/plan.html`.
7. Protocol docs live in `docs/protocol-v03.md`, public schemas in `schemas/`, and examples in `examples/protocol-v03/`.
8. Repo-local `plan.md`, `phase3-plan.md`, and `plan.html` are historical/project-local references, not the canonical overall plan, unless the user explicitly asks to refresh them.
9. After every completed change, and after any explicit planning pass that changes direction or priorities, update both `server_plan.md` and `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.

## Non-Negotiables

1. Keep `main` clean and synchronized with `origin/main`.
2. Do not make feature, protocol, server behavior, deployment, or MCP-facing API changes directly on `main`.
3. Create a task branch/worktree for non-trivial work, keep changes scoped, then PR back to `main`.
4. Preserve secrets and runtime state. Do not print tokens or commit `.env`, `data/`, SQLite databases, auth files, logs, or other runtime artifacts.
5. AgentRelay should stay small: route, persist, authorize, notify, audit, and enforce transport/state invariants. Do not turn the relay into the local agent brain or a hardcoded UX adapter.
6. Local inbox-to-user-workflow adapters belong in the MCP/client repo or user-owned integrations, not in the cloud relay.
7. Preserve compatibility unless the user explicitly approves a breaking migration. Prefer additive Protocol v0.3+ behavior.

## Branch And PR Workflow

For any new feature, protocol change, server behavior change, deployment change, dashboard change, MCP-facing API change, or other non-trivial logic change:

1. Start from clean `main`.
2. Create a worktree under `/home/ubuntu/projects/agentrelay/`:

```bash
git worktree add -b <branch-name> ../agentRelay-<short-slug> main
```

3. Work only in that task worktree.
4. Run targeted verification that proves the change.
5. Commit task-owned files only.
6. Push the branch, open a PR to `main`, and merge after verification.
7. After opening or updating the PR, refresh CodeGraph state from the task worktree:

```bash
npm run codegraph:status
```

If it reports pending sync, run `npm run codegraph:sync` and then rerun `npm run codegraph:status`. Include the final CodeGraph status in the PR/final report.
8. Fast-forward `/home/ubuntu/projects/agentrelay/agentRelay` to `origin/main`.
9. Remove only the task-owned worktree/local branch after the PR is merged.

Small documentation-only corrections may be made directly on `main` when the user explicitly asks for a quick edit, but roadmap/protocol documentation accompanying code should be included in the feature PR.

## Validation Matrix

1. Docs-only changes:
   - Inspect the changed text.
   - Run `git diff --check`.
2. Schema/protocol docs/examples:
   - Run `npm run test:schema`.
   - Run the relevant protocol smoke test when behavior semantics change.
3. Server behavior, auth, task lifecycle, event delivery, dashboard, or deployment code:
   - Run `npm test` unless the change is very narrow and a targeted subset is clearly sufficient.
4. Docker/runtime changes:
   - Rebuild/restart with `sudo docker compose up -d --build`.
   - Verify `https://server.stellarix.space/agentrelay/api/health`.
   - Verify one task-specific live marker.
5. Public roadmap changes:
   - Update `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.
   - Copy `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` to `/var/www/html/agentrelay/plan.html`.
   - Verify `https://server.stellarix.space/agentrelay/plan.html`.

## Protocol Boundaries

1. Agent roles are `personal_agent` and `service_agent`.
2. Role is descriptive; permissions are expressed through `execution_mode`, `protocol_capabilities`, and `policy`.
3. `personal_agent` defaults to notifier-first behavior and may amend/close requester-owned work only with human authority.
4. `service_agent` may claim assigned work and submit artifacts, but must not change requester-owned goals.
5. Artifact submission does not automatically complete a task.
6. Close is controlled by `completion_owner_agent_id`; human completion authority is recorded through the authorized agent, not by making human-agent private chat part of relay audit.
7. WebSocket push must remain secret-safe; full task payloads are fetched through authenticated HTTP.

## Deployment Notes

Production runs from this repo with Docker Compose:

```text
agentrelay-api -> 127.0.0.1:8787
agentrelay-ws  -> 127.0.0.1:8788
host nginx     -> https://server.stellarix.space/agentrelay/...
```

Runtime state is bind-mounted under `data/`. Do not bake credentials or SQLite state into images.

## Final Report Checklist

Include:

1. What changed and why.
2. PR/commit links when applicable.
3. Verification commands and results.
4. Deployment/public-page verification when applicable.
5. CodeGraph status after PR work, when a PR was opened or updated.
6. Whether `server_plan.md` and `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html` were updated.
7. Any residual risk or skipped checks.
