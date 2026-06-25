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
- `docs/thread-bridge-proof.md`: Codex App thread creation and reuse proof
- `docs/task-completion-policy.md`: task completion, ownership transfer, timeout, and follow-up policy
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
- [ ] Implement controlled delivery back to Zac's origin thread.
- [ ] Encode requester-side completion ownership in task metadata and API payloads.

## Run Locally

```bash
AGENTRELAY_DB_PATH=./data/agentrelay.sqlite3 python3 -m server.app
```

Smoke test:

```bash
python3 scripts/smoke_test.py http://127.0.0.1:8787
```

## First Implementation Milestone

Build the smallest vertical slice:

1. Relay data model for agents, tasks, messages, artifacts, events, and thread mapping.
2. A2A-shaped task creation and task lookup endpoints.
3. Worker claim endpoint for `frank-agent`.
4. Codex App bridge proof for creating/reusing Frank threads and sending replies back to Zac's origin thread.
