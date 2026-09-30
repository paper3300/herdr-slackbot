"""Send gating (D6/D11) and notification decisions (D8/D9) as pure functions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

SENDABLE = frozenset({"idle", "done"})
ACTIVE = frozenset({"working", "blocked"})
SETTLED = frozenset({"idle", "done"})

# SendCheck.reason codes
REASON_BUSY = "busy"
REASON_BLOCKED = "blocked"
REASON_UNKNOWN = "unknown"
REASON_GONE = "agent_gone"
REASON_SESSION_CHANGED = "session_changed"


@dataclass(frozen=True)
class SendCheck:
    ok: bool
    reason: str | None = None


def check_send_allowed(status: str | None) -> SendCheck:
    """D6: only idle/done agents accept a prompt."""
    if status in SENDABLE:
        return SendCheck(True)
    if status == "working":
        return SendCheck(False, REASON_BUSY)
    if status == "blocked":
        return SendCheck(False, REASON_BLOCKED)
    return SendCheck(False, REASON_UNKNOWN)


def check_thread_target(bound_session: str, live_agent: Mapping | None) -> SendCheck:
    """D11: a thread reply goes only to the agent session the thread is bound to."""
    if live_agent is None:
        return SendCheck(False, REASON_GONE)
    live_session = (live_agent.get("agent_session") or {}).get("value")
    if live_session != bound_session:
        return SendCheck(False, REASON_SESSION_CHANGED)
    return check_send_allowed(live_agent.get("agent_status"))


class Action(str, Enum):
    NONE = "none"
    STARTED = "started"  # "⏳ started (working)" reply for a Slack task
    COMPLETED = "completed"  # result post
    BLOCKED = "blocked"  # "⚠️ confirm on PC"


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str = ""
    clear_pending: bool = False  # the Slack task is finished (or can no longer finish)
    mark_working_announced: bool = False


def decide_notification(prev: str | None, status: str, *, pending_task: Mapping | None,
                        muted: bool) -> Decision:
    """What to post for one status transition of an agent.

    pending_task: the Slack-originated task in flight for this agent session, if any.
    Slack tasks always get their replies (mute does not apply, D9/D10).
    PC-originated work: `working -> done` only (unseen tab); `working -> idle` means the
    user watched it finish. `blocked` notifies for every agent.
    """
    if prev == status:
        return Decision(Action.NONE, "no change")

    if status == "blocked":
        if muted and not pending_task:
            return Decision(Action.NONE, "muted")
        return Decision(Action.BLOCKED, "needs confirmation")

    if pending_task:
        if status == "working":
            if pending_task.get("working_announced"):
                return Decision(Action.NONE, "already announced")
            return Decision(Action.STARTED, "slack task started", mark_working_announced=True)
        if status in SETTLED and prev in ACTIVE:
            return Decision(Action.COMPLETED, "slack task finished", clear_pending=True)
        if status == "unknown":
            return Decision(Action.NONE, "agent released/unclassified", clear_pending=prev in ACTIVE)
        return Decision(Action.NONE, "not a completion")

    if status == "done" and prev in ACTIVE:
        if muted:
            return Decision(Action.NONE, "muted")
        return Decision(Action.COMPLETED, "finished unseen")
    return Decision(Action.NONE, "not notified")


def decide_resume(status: str | None, pending_task: Mapping | None) -> Decision:
    """After a bridge restart: settle Slack tasks that finished while the bridge was down."""
    if not pending_task:
        return Decision(Action.NONE, "no pending task")
    if status in SETTLED:
        return Decision(Action.COMPLETED, "finished while bridge was down", clear_pending=True)
    if status is None or status == "unknown":
        return Decision(Action.NONE, "agent gone", clear_pending=True)
    if status == "blocked":
        return Decision(Action.BLOCKED, "needs confirmation")
    return Decision(Action.NONE, "still working")
