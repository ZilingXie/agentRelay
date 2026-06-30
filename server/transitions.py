from __future__ import annotations

import time
from typing import Any


TERMINAL_STATES = {"completed", "failed", "cancelled", "expired", "rejected"}
CLAIMABLE_STATES = {
    "submitted",
    "input_required",
    "auth_required",
    "waiting_remote",
    "delivery_pending",
    "artifact_submitted",
}
NON_TERMINAL_STATES = {
    "submitted",
    "claimed",
    "working",
    "waiting_remote",
    "waiting_human",
    "input_required",
    "auth_required",
    "delivery_pending",
    "artifact_submitted",
}
KNOWN_STATES = TERMINAL_STATES | NON_TERMINAL_STATES


class TransitionError(ValueError):
    """Raised when a task state transition violates the AgentRelay state machine."""


def assert_known_status(status: str) -> None:
    if status not in KNOWN_STATES:
        raise TransitionError(f"unknown task status: {status}")


def assert_not_terminal(task: dict[str, Any], action: str) -> None:
    status = task.get("status")
    if status in TERMINAL_STATES:
        raise TransitionError(f"cannot {action}; task is terminal: {status}")


def assert_not_expired(task: dict[str, Any]) -> None:
    ttl = task.get("ttl")
    if ttl is None:
        return
    try:
        ttl_value = int(ttl)
    except (TypeError, ValueError):
        return
    # Existing tasks store ttl inconsistently; enforce only epoch-second values.
    if ttl_value > 1_000_000_000 and ttl_value < int(time.time()):
        raise TransitionError("task ttl has expired")


def assert_claim_allowed(task: dict[str, Any], agent_id: str) -> None:
    assert_not_terminal(task, "claim task")
    assert_not_expired(task)
    pending_on = task.get("pending_on_agent_id")
    if pending_on != agent_id:
        raise TransitionError(f"task is pending on {pending_on or 'none'}, not {agent_id}")
    claimed_by = task.get("claimed_by")
    if claimed_by and claimed_by != agent_id:
        raise TransitionError(f"task is already claimed by {claimed_by}")
    status = task.get("status")
    if status != "claimed" and status not in CLAIMABLE_STATES:
        raise TransitionError(f"task status is not claimable: {status}")


def assert_update_status_allowed(task: dict[str, Any], status: str, payload: dict[str, Any]) -> None:
    assert_known_status(status)
    if task.get("status") in TERMINAL_STATES:
        raise TransitionError(f"cannot update terminal task: {task.get('status')}")
    if status in TERMINAL_STATES:
        if not (payload.get("terminalReason") or payload.get("terminal_reason")):
            raise TransitionError("terminal transition requires terminalReason")
        return
    next_action = payload.get("nextAction") or payload.get("next_action")
    pending_on_agent_id = payload.get("pendingOnAgentId") or payload.get("pending_on_agent_id")
    pending_on_human_id = payload.get("pendingOnHumanId") or payload.get("pending_on_human_id")
    if status not in {"claimed", "working"}:
        if not pending_on_agent_id and not pending_on_human_id:
            raise TransitionError("non-terminal transition requires pending owner")
        if not next_action:
            raise TransitionError("non-terminal transition requires nextAction")


def assert_artifact_allowed(
    task: dict[str, Any],
    actor_agent_id: str,
    next_status: str,
    pending_on_agent_id: str | None,
    next_action: str | None,
) -> None:
    assert_not_terminal(task, "submit artifact")
    assert_not_expired(task)
    assert_known_status(next_status)
    if actor_agent_id not in {
        task.get("requester_agent_id"),
        task.get("target_agent_id"),
        task.get("completion_owner_agent_id"),
        task.get("pending_on_agent_id"),
        task.get("claimed_by"),
    }:
        raise TransitionError("actor agent is not associated with the task")
    if next_status in TERMINAL_STATES:
        raise TransitionError("artifact submission cannot move task to a terminal state")
    if not pending_on_agent_id:
        raise TransitionError("artifact submission requires pending_on_agent_id")
    if not next_action:
        raise TransitionError("artifact submission requires next_action")


def next_turn_count(task: dict[str, Any], pending_on_agent_id: str | None) -> int:
    current = int(task.get("turn_count") or 0)
    previous_pending = task.get("pending_on_agent_id")
    if pending_on_agent_id and pending_on_agent_id != previous_pending:
        return current + 1
    return current


def assert_max_turns(task: dict[str, Any], turn_count: int) -> None:
    max_turns = int(task.get("max_turns") or 12)
    if turn_count > max_turns:
        raise TransitionError("task exceeded max_turns")


def assert_delivery_allowed(task: dict[str, Any], delivered_by_agent_id: str, thread_id: str) -> None:
    assert_not_terminal(task, "mark delivery")
    if delivered_by_agent_id != task.get("completion_owner_agent_id"):
        raise TransitionError("only completion_owner_agent_id can mark requester delivery")
    if thread_id != task.get("requester_thread_id"):
        raise TransitionError("delivery threadId must match requester_thread_id")


def assert_close_allowed(task: dict[str, Any], payload: dict[str, Any]) -> None:
    assert_not_terminal(task, "close task")
    closed_by_agent_id = payload.get("closedByAgentId") or payload.get("closed_by_agent_id")
    if closed_by_agent_id != task.get("completion_owner_agent_id"):
        raise TransitionError("only completion_owner_agent_id can close the task")
    terminal_reason = payload.get("terminalReason") or payload.get("terminal_reason")
    if not terminal_reason:
        raise TransitionError("close requires terminalReason")
    authority = payload.get("completion_authority")
    if authority is None:
        return
    if not isinstance(authority, dict):
        raise TransitionError("completion_authority must be an object")
    authority_type = authority.get("type")
    if authority_type not in {"agent", "human"}:
        raise TransitionError("completion_authority.type must be agent or human")
    if authority_type == "human":
        if authority.get("via_agent_id") != closed_by_agent_id:
            raise TransitionError("human completion authority must be recorded via the closing agent")
        for key in ("owner_id", "approval_ref"):
            if not authority.get(key):
                raise TransitionError(f"human completion authority requires {key}")
