# AgentRelay Development Rules

These instructions apply to the `/home/ubuntu/agentRelay` repository.

## Repository

This is the server-side AgentRelay project.

GitHub repository:

```text
https://github.com/ZilingXie/agentRelay
```

The client/agent-side MCP project is separate:

```text
https://github.com/ZilingXie/agent-relay-mcp
```

## Worktree And PR Policy

For any new feature, protocol change, server behavior change, MCP-facing API change,
or other non-trivial logic change:

1. Create a new branch/worktree before editing.
2. Keep the change scoped to that branch/worktree.
3. Run the relevant tests before finishing.
4. Commit the change.
5. Push the branch.
6. Open a pull request.
7. Merge the pull request after review/approval.

Do not continue making feature or protocol changes directly on `main`.

Small documentation-only corrections may be made directly when the user explicitly
asks for a quick edit, but roadmap/protocol documentation that accompanies a code
change should be included in that feature branch and PR.

## Current Project Direction

AgentRelay is an agent-first collaboration protocol and relay server. The relay
connects agents that do not have public IP addresses, but it should stay small:
identity, auth, state, notification, delivery reliability, and audit history.

The relay should not become the agent brain, a human-centered IM product, or a
hardcoded local workflow adapter. Local inbox-to-user-workflow adapters remain
user-owned and client-specific.

## Compatibility

Preserve existing Phase 1/2 and Protocol v0.2 compatibility unless the user
explicitly approves a breaking migration.

When adding Protocol v0.3+ behavior, prefer opt-in or additive behavior first.

## Validation

Before opening a PR, run at least:

```bash
npm test
git diff --check
```

If the change is narrow and full tests are temporarily impractical, run the most
relevant targeted tests and clearly state what was not run.
