# Protocol v0.6 Public Roadmap Pending Copy

This is reviewable source copy for the canonical public roadmap. It is not a
publication artifact and must not be presented as deployed. Insert an English
section after the current Protocol v0.5 production section and mirror the same
facts in the Chinese roadmap section.

## Status

Tag: `Implementation complete in open PRs - not merged or deployed`

Protocol v0.6 treats an offline Listener as a normal delivery condition. Task
creation no longer depends on transient Listener readiness. An unavailable
Listener leaves the Task open, the Message pending, and the Event parked for
authenticated, epoch-fenced recovery. Push retry exhaustion does not turn a
transport failure into a business failure. Task TTL still transitions the Task
to expired and creates a recoverable terminal notice.

## Component Work

- Server: [agentRelay PR #74](https://github.com/ZilingXie/agentRelay/pull/74)
  defines the v0.6 schemas and lifecycle, separates capability from readiness,
  implements bounded parked recovery, preserves v0.5 wire compatibility, and
  exposes `waiting_listener` through batch visibility.
- Client: [agent-relay-mcp PR #67](https://github.com/ZilingXie/agent-relay-mcp/pull/67)
  drains transitionable and terminal Events after startup or reconnection,
  persists and reads back local state before epoch-bound ACK, and exposes
  Listener health plus offline recovery summaries in the Local Inbox UI.
- Hermes: [heremes-deploy PR #7](https://github.com/ZilingXie/heremes-deploy/pull/7)
  consumes Server batch visibility, reports `Delivered / Waiting listener /
  Expired / Failed`, keeps partial API failures separate, and journals WeCom
  notification attempts before send.

## Verification

- Server v0.6 offline state-machine proof: 6/6.
- Server v0.6 HTTP conformance: 6/6; WebSocket smoke passed.
- Server v0.5 Store/API/Delivery baselines remain 21/21, 23/23, and 20/20.
- Client full suite: 228/228 with no failures or skips; MCP smoke passed.
- Client real Listener/recovery/intake focused suite: 10/10.
- Hermes full suite: 28/28 with no failures, skips, or TODOs.

## Rollout Gate

Merge and deploy in this order: Server PR #74, Client PR #67, then Hermes PR
#7. Keep defaults on Protocol v0.5 until the Server and target Listeners support
v0.6. Only then opt Hermes into `AGENTRELAY_PROTOCOL_VERSION=agent-collab-v0.6`.
No production deployment or real WeCom notification is part of these PRs.

Rollback is configuration-first: return Hermes and clients to v0.5 before
rolling back Server v0.6 capability. Do not delete parked Events or Task data.

## Publication Checklist

1. Confirm all three PRs are merged in dependency order and their deployment
   checks are green.
2. Replace the pending tag with the actual rollout status; do not claim
   production until a live offline-create/recovery probe passes.
3. Update both English and Chinese active-roadmap sections in
   `/home/ubuntu/projects/stellarix-site/agentrelay/plan.html`.
4. Publish through the site's authorized workflow and verify the public page.
