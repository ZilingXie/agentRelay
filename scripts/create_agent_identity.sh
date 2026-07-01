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

restarted=false
docker_compose=()
if command -v docker >/dev/null 2>&1 && docker compose ps --services --status running 2>/dev/null | grep -Eq '^agentrelay-(api|ws)$'; then
  docker_compose=(docker compose)
elif command -v sudo >/dev/null 2>&1 && command -v docker >/dev/null 2>&1 && sudo docker compose ps --services --status running 2>/dev/null | grep -Eq '^agentrelay-(api|ws)$'; then
  docker_compose=(sudo docker compose)
fi

if [[ ${#docker_compose[@]} -gt 0 ]]; then
  "${docker_compose[@]}" restart agentrelay-api agentrelay-ws
  "${docker_compose[@]}" ps agentrelay-api agentrelay-ws
  restarted=true
elif command -v sudo >/dev/null 2>&1 && systemctl list-unit-files agentrelay.service >/dev/null 2>&1; then
  sudo systemctl restart agentrelay
  if systemctl list-unit-files agentrelay-ws.service >/dev/null 2>&1; then
    sudo systemctl restart agentrelay-ws
  fi
  sudo systemctl is-active --quiet agentrelay
  restarted=true
else
  echo "Could not auto-restart AgentRelay. Restart Docker Compose or systemd manually." >&2
fi

echo ""
if [[ "${restarted}" == "true" ]]; then
  echo "AgentRelay restarted successfully."
fi
echo "Copy the generated local env file into the user's local agent-relay-mcp/.env."
