from __future__ import annotations

from typing import Any


def build_timeline_entry(event: dict[str, Any], sequence: int) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = event.get("event_type", "")
    category = category_for_event(event_type)
    actor_agent_id = first_present(
        payload,
        "actor_agent_id",
        "agentId",
        "closed_by_agent_id",
        "closedByAgentId",
        "deliveredByAgentId",
    )
    pending_on_agent_id = first_present(payload, "pending_on_agent_id", "pendingOnAgentId")
    title = title_for_event(event_type)

    return {
        "timeline_id": event.get("event_id"),
        "sequence": sequence,
        "task_id": event.get("task_id"),
        "event_type": event_type,
        "category": category,
        "title": title,
        "summary": summarize_event(event_type, payload),
        "actor_agent_id": actor_agent_id,
        "intent": payload.get("intent"),
        "artifact_id": payload.get("artifactId") or payload.get("artifact_id"),
        "status": payload.get("status") or payload.get("nextStatus") or payload.get("next_status"),
        "pending_on_agent_id": pending_on_agent_id,
        "source_refs": payload.get("source_refs") or [],
        "completion_authority": payload.get("completion_authority"),
        "delivery": delivery_payload(event_type, payload),
        "created_at": event.get("created_at"),
        "payload": payload,
    }


def category_for_event(event_type: str) -> str:
    if event_type.startswith("artifact."):
        return "artifact"
    if event_type.startswith("ownership."):
        return "ownership"
    if event_type.startswith("reply.") or event_type.startswith("delivery."):
        return "delivery"
    if event_type.startswith("thread."):
        return "local_workflow"
    if event_type in {"task.completed", "task.closed", "task.expired", "task.rejected"}:
        return "completion"
    if event_type.startswith("task."):
        return "lifecycle"
    return "event"


def title_for_event(event_type: str) -> str:
    titles = {
        "task.created": "Task created",
        "task.claimed": "Task claimed",
        "task.amended": "Task goal amended",
        "task.status_updated": "Task status updated",
        "artifact.submitted": "Artifact submitted",
        "ownership.transferred": "Ownership transferred",
        "reply.delivered": "Reply delivered",
        "reply.delivery_failed": "Reply delivery failed",
        "thread.created": "Local thread created",
        "thread.reused": "Local thread reused",
        "task.completed": "Task completed",
        "task.expired": "Task expired",
    }
    return titles.get(event_type, event_type.replace(".", " ").title())


def summarize_event(event_type: str, payload: dict[str, Any]) -> str:
    if event_type == "task.created":
        requester = payload.get("requester_agent_id") or "requester agent"
        target = payload.get("target_agent_id") or "target agent"
        intent = payload.get("intent")
        if intent:
            return f"{requester} created a {intent} task for {target}."
        return f"{requester} created a task for {target}."
    if event_type == "task.claimed":
        agent = payload.get("agentId") or payload.get("actor_agent_id") or "agent"
        return f"{agent} claimed the task."
    if event_type == "task.amended":
        actor = payload.get("actor_agent_id") or "requester agent"
        version = payload.get("goal_version") or "new"
        disposition = payload.get("previous_goal_disposition") or "clarified"
        return f"{actor} amended the task goal to version {version}; previous goal was {disposition}."
    if event_type == "artifact.submitted":
        actor = payload.get("actor_agent_id") or "agent"
        summary = payload.get("summary")
        if summary:
            return f"{actor} submitted an artifact: {summary}"
        intent = payload.get("intent") or "work result"
        return f"{actor} submitted {intent}."
    if event_type == "ownership.transferred":
        pending = payload.get("pending_on_agent_id") or payload.get("pendingOnAgentId") or "next agent"
        return f"Task responsibility moved to {pending}."
    if event_type == "reply.delivered":
        thread_id = payload.get("threadId") or "requester thread"
        return f"Reply delivered to {thread_id}."
    if event_type == "reply.delivery_failed":
        error = payload.get("error") or "unknown error"
        return f"Reply delivery failed: {error}"
    if event_type == "task.status_updated":
        status = payload.get("status") or "unknown"
        return f"Task status changed to {status}."
    if event_type == "task.completed":
        reason = payload.get("terminal_reason") or payload.get("terminalReason") or "no reason provided"
        return f"Task completed: {reason}"
    if event_type == "task.expired":
        reason = payload.get("terminal_reason") or payload.get("terminalReason") or "TTL expired"
        return f"Task expired: {reason}"
    if event_type.startswith("thread."):
        agent = payload.get("agentId") or "agent"
        thread = payload.get("threadId") or "local thread"
        return f"{agent} mapped the task to {thread}."
    return event_type


def delivery_payload(event_type: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if not (event_type.startswith("reply.") or event_type.startswith("delivery.")):
        return None
    status = "failed" if event_type.endswith("failed") else "delivered"
    return {
        "status": status,
        "thread_id": payload.get("threadId"),
        "delivered_by_agent_id": payload.get("deliveredByAgentId"),
        "error": payload.get("error"),
    }


def first_present(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None:
            return value
    return None
