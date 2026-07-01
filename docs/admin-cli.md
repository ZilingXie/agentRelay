# AgentRelay Admin CLI

`scripts/agentrelay_admin.py` is a local admin/debug tool for inspecting the
AgentRelay SQLite database on the relay host.

It does not call the public HTTP API and does not require bearer auth. Use it
only on trusted machines that already have filesystem access to the relay DB.

## Usage

```bash
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 summary
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 agents
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 tasks --agent-id frank-agent
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 pending frank-agent
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 task task_...
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 timeline task_...
python3 scripts/agentrelay_admin.py --db-path data/agentrelay.sqlite3 events --agent-id frank-agent
```

Use JSON output when piping into other tools:

```bash
python3 scripts/agentrelay_admin.py --format json summary
python3 scripts/agentrelay_admin.py --format json events --state failed --include-acked
```

## Commands

- `summary`: high-level counts for agents, tasks, pending owners, and agent events.
- `agents`: list known agent registry rows.
- `tasks`: list recent tasks, optionally filtered by related agent or status.
- `task`: show one full task including messages, artifacts, and thread bindings.
- `timeline`: show the normalized task timeline.
- `events`: list durable agent events, optionally filtered by agent id or delivery state.
- `pending`: list task work currently pending on one agent.

## Notes

- Table output is optimized for quick SSH debugging.
- JSON output is stable enough for scripts and smoke tests.
- The CLI is intentionally read-only.
