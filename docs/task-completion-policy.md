# Task Completion Policy

Date: 2026-06-25

## 1. Core distinction

AgentRelay must distinguish between:

```text
agent action complete
workflow task complete
```

Example:

```text
Zac asks Frank when Frank is available for an online meeting.
Frank agent asks Frank.
Frank says 10:00.
Frank agent replies to Zac agent.
```

At this point, Frank agent's current action is complete, but the workflow task is not necessarily complete. Zac may still need to confirm whether 10:00 works for him.

## 2. Who defines task completion?

Task completion is defined at task creation time by the requester side.

For Phase 1:

- The requester agent proposes `done_criteria`.
- The relay stores `done_criteria` as part of the task.
- The relay owns the canonical task state.
- Agents may report action results, artifacts, and recommendations.
- Agents may not unilaterally archive the whole workflow task unless their reported result satisfies the stored `done_criteria` and relay validation passes.

In the meeting example, the critical difference is:

```text
Goal A: Ask Frank when he is available.
Done when: Frank provides approved candidate times and Zac is notified.

Goal B: Schedule an online meeting with Frank.
Done when: both Zac and Frank have accepted the same time, or the request expires/rejects.
```

If Zac says "I want to have a meeting with Frank," the correct `done_criteria` should usually be Goal B, not Goal A.

## 3. Required task fields

The task record should include:

```text
done_criteria
pending_on_agent_id
pending_on_human_id
next_action
terminal_reason
turn_count
max_turns
ttl
parent_task_id
context_id
```

`context_id` groups related tasks. `task_id` represents one lifecycle and should not be reused after terminal completion.

## 4. Recommended status model

```text
submitted
claimed
working
waiting_remote
waiting_human
input_required
auth_required
delivery_pending
completed
rejected
failed
expired
cancelled
archived
```

Terminal states:

```text
completed
rejected
failed
expired
cancelled
archived
```

`archived` should only happen after a terminal state. Archive is not how an agent says "nothing for me to do right now."

## 5. Ownership transfer example

Meeting scheduling flow:

```text
1. Zac creates task:
   done_criteria = both Zac and Frank accept the same online meeting time
   pending_on_agent_id = frank-agent

2. Frank agent asks Frank.
   status = waiting_human
   pending_on_human_id = frank

3. Frank says 10:00.
   Frank action complete.
   status = waiting_remote
   pending_on_agent_id = zac-agent
   next_action = Ask Zac whether 10:00 works.

4. Zac agent asks Zac.
   status = waiting_human
   pending_on_human_id = zac

5. Zac says OK.
   If Frank's 10:00 was already an approved slot:
     status = completed
     terminal_reason = Both sides accepted 10:00.
   If Frank's response was only tentative:
     pending_on_agent_id = frank-agent
     next_action = Confirm Zac accepted 10:00 with Frank.
```

## 6. Human silence

If Zac does not answer:

```text
status = waiting_human
pending_on_human_id = zac
reminder_count += 1
```

Recommended Phase 1 policy:

```text
T+30 minutes: first reminder
T+24 hours: second reminder
T+48 hours: expire task
```

On expiry:

```text
status = expired
terminal_reason = Zac did not confirm before TTL.
```

The relay may optionally notify Frank agent that the request expired.

## 7. Post-completion changes

If a terminal task is already completed and Zac later says:

```text
I cannot do 10:00 anymore. Can we change to 11:00?
```

Do not reopen or mutate the completed task.

Create a child task:

```json
{
  "type": "reschedule_meeting",
  "context_id": "ctx_meeting_zac_frank",
  "parent_task_id": "task_schedule_001",
  "reason": "Zac is no longer available at 10:00",
  "done_criteria": "Both Zac and Frank accept a replacement meeting time"
}
```

This preserves audit history and avoids confusing old terminal states with new work.

## 8. Anti-loop rules

Phase 1 should enforce:

```text
max_turns default: 8
ttl default: 48 hours
terminal tasks reject new normal messages
each non-terminal transition must set pending_on_agent_id or pending_on_human_id
each reply must include next_action or terminal_reason
same agent cannot claim the same task twice in a row without changing status, artifact, or pending ownership
two consecutive clarification requests escalate to human
```

## 9. Practical rule

Agents can say:

```text
My action is complete.
I recommend the task be completed.
I recommend waiting for Zac.
I recommend creating a child task.
```

But the relay records the canonical task state and decides whether the task has reached terminal completion according to `done_criteria`.

