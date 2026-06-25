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
- `plan.md`: overall AgentRelay plan
- `phase1-plan.md`: Phase 1 Codex App thread loop
- `plan.html`: public planning page deployed to `https://server.stellarix.space/agentrelay/plan.html`
- `agentlist.md`: draft agent registry

## Phase 1 Progress

- [x] Create GitHub repository and push planning docs.
- [ ] Scaffold relay server project.
- [ ] Implement SQLite data model.
- [ ] Implement A2A-shaped task and worker APIs.
- [ ] Verify with a local smoke test.
- [ ] Add Codex App thread bridge proof.

## First Implementation Milestone

Build the smallest vertical slice:

1. Relay data model for agents, tasks, messages, artifacts, events, and thread mapping.
2. A2A-shaped task creation and task lookup endpoints.
3. Worker claim endpoint for `frank-agent`.
4. Codex App bridge proof for creating/reusing Frank threads and sending replies back to Zac's origin thread.
