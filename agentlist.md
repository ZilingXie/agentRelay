# AgentRelay Agent List

This is the draft registry for the AgentRelay PoC.

## Purpose

Each entry should tell another agent:

- who the agent represents
- what the agent is allowed to do
- how to deliver messages
- when human confirmation is required
- which message formats and protocol versions it supports

## Draft Agents

### zac-agent

- Owner: Zac
- Inbox: `inboxes/zac-agent/`
- Role: Personal coordinator agent.
- Can handle: drafting requests, routing tasks, reading replies, asking Zac only when approval or missing context is required.
- Requires human confirmation for: commitments, calendar changes, sending sensitive data, spending money.

### frank-agent

- Owner: Frank
- Inbox: `inboxes/frank-agent/`
- Role: Remote collaborator agent placeholder.
- Can handle: schedule lookup and meeting coordination, assuming Frank grants access.
- Requires human confirmation for: exposing private calendar details, accepting meetings, sharing sensitive information.
