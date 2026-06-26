#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "Usage: $0 <username> [agent-id]" >&2
  echo "Example: $0 frank" >&2
  echo "Example: $0 'Frank Xie' frank-agent" >&2
  exit 2
fi

username="$1"
shift

args=("$username")
if [[ $# -eq 1 ]]; then
  args+=(--agent-id "$1")
fi

cd "$(dirname "$0")/.."
python3 scripts/upsert_agent_identity.py "${args[@]}"
sudo systemctl restart agentrelay
sudo systemctl is-active --quiet agentrelay

echo ""
echo "AgentRelay restarted successfully."
echo "Copy the generated local env file into the user's local agent-relay-mcp/.env."
