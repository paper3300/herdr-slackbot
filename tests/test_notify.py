import pytest

from herdr_slackbot.notify import (
    REASON_BLOCKED,
    REASON_BUSY,
    REASON_GONE,
    REASON_SESSION_CHANGED,
    REASON_UNKNOWN,
    Action,
    check_send_allowed,
    check_thread_target,
    decide_notification,
    decide_resume,
)

TASK = {"started_at": 1.0, "working_announced": False}
TASK_ANNOUNCED = {"started_at": 1.0, "working_announced": True}


@pytest.mark.parametrize("status,ok,reason", [
    ("idle", True, None), ("done", True, None),
    ("working", False, REASON_BUSY), ("blocked", False, REASON_BLOCKED),
    ("unknown", False, REASON_UNKNOWN), (None, False, REASON_UNKNOWN),
])
def test_check_send_allowed(status, ok, reason):
    check = check_send_allowed(status)
    assert (check.ok, check.reason) == (ok, reason)


def test_check_thread_target():
    live = {"agent_session": {"value": "s1"}, "agent_status": "idle"}
    assert check_thread_target("s1", live).ok
    assert check_thread_target("s1", None).reason == REASON_GONE
    assert check_thread_target("s0", live).reason == REASON_SESSION_CHANGED
    assert check_thread_target("s1", dict(live, agent_status="working")).reason == REASON_BUSY


# --- PC-originated agents (no pending Slack task) ----------------------------

def test_pc_working_to_done_notifies():
    assert decide_notification("working", "done", pending_task=None, muted=False).action == Action.COMPLETED


def test_pc_working_to_idle_is_silent():
    assert decide_notification("working", "idle", pending_task=None, muted=False).action == Action.NONE


def test_pc_blocked_then_done_notifies():
    assert decide_notification("blocked", "done", pending_task=None, muted=False).action == Action.COMPLETED


def test_pc_done_muted():
    assert decide_notification("working", "done", pending_task=None, muted=True).action == Action.NONE


@pytest.mark.parametrize("prev,status", [("idle", "working"), ("unknown", "idle"), (None, "idle"),
                                         ("done", "idle"), ("idle", "unknown"), ("done", "done")])
def test_pc_other_transitions_silent(prev, status):
    assert decide_notification(prev, status, pending_task=None, muted=False).action == Action.NONE


def test_blocked_notifies_for_all_agents():
    assert decide_notification("working", "blocked", pending_task=None, muted=False).action == Action.BLOCKED
    assert decide_notification("idle", "blocked", pending_task=TASK, muted=True).action == Action.BLOCKED


def test_blocked_muted_pc_agent_is_silent():
    assert decide_notification("working", "blocked", pending_task=None, muted=True).action == Action.NONE


# --- Slack-originated tasks ----------------------------------------------------

def test_slack_task_started_once():
    first = decide_notification("idle", "working", pending_task=TASK, muted=True)
    assert first.action == Action.STARTED and first.mark_working_announced
    again = decide_notification("blocked", "working", pending_task=TASK_ANNOUNCED, muted=False)
    assert again.action == Action.NONE


@pytest.mark.parametrize("final", ["idle", "done"])
def test_slack_task_completes_on_idle_or_done_even_when_muted(final):
    d = decide_notification("working", final, pending_task=TASK_ANNOUNCED, muted=True)
    assert d.action == Action.COMPLETED and d.clear_pending


def test_slack_task_idle_before_working_is_not_completion():
    d = decide_notification("unknown", "idle", pending_task=TASK, muted=False)
    assert d.action == Action.NONE and not d.clear_pending


def test_slack_task_agent_released_clears_pending():
    d = decide_notification("working", "unknown", pending_task=TASK_ANNOUNCED, muted=False)
    assert d.action == Action.NONE and d.clear_pending


def test_decide_resume():
    assert decide_resume("done", TASK).action == Action.COMPLETED
    assert decide_resume("idle", TASK).clear_pending
    assert decide_resume("working", TASK).action == Action.NONE
    assert not decide_resume("working", TASK).clear_pending
    assert decide_resume(None, TASK).clear_pending
    assert decide_resume("blocked", TASK).action == Action.BLOCKED
    assert decide_resume("done", None).action == Action.NONE
