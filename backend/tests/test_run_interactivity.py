"""Unit tests for run interactivity detection.

``is_interactive_run`` decides whether a paused run notifies the assignment
owner. It must NOT influence the approval gate: a user's standing approval
applies to their scheduled runs exactly as it does to interactive ones.
"""

from app.models.assignment import AssignmentRun
from app.services.assignment_helpers import is_interactive_run


def _run(**kwargs):
    """Build a REAL AssignmentRun, not a stand-in.

    An earlier version of this suite used ``SimpleNamespace``, which happily
    accepted a ``triggered_by_user_id`` the model did not actually have. Every
    test passed while ``is_interactive_run`` returned False for every real run,
    because the attribute simply did not exist. Constructing the mapped class is
    what makes a missing column fail here instead of in production.
    """
    return AssignmentRun(**kwargs)


def test_manually_triggered_run_is_interactive():
    """A person clicked something, so somebody may be watching."""
    assert is_interactive_run(_run(triggered_by_user_id="user-1")) is True


def test_scheduled_run_is_autonomous():
    assert is_interactive_run(_run(triggered_by_user_id=None)) is False


def test_webhook_run_is_autonomous():
    """Webhooks never carry a triggering user (see assignment_engine:4716)."""
    assert is_interactive_run(_run(triggered_by_user_id=None)) is False


def test_integration_event_run_is_autonomous():
    assert is_interactive_run(_run(triggered_by_user_id=None)) is False


def test_unset_trigger_is_treated_as_autonomous():
    """Fail toward notifying.

    An unrecognised future trigger shape should send a notification nobody
    needed rather than leave a stalled run silent.
    """
    assert is_interactive_run(_run()) is False


def test_the_column_actually_exists_on_the_model():
    """Guards the bug this suite previously missed.

    ``is_interactive_run`` reads an attribute; if the column is dropped or
    renamed, ``getattr(..., None)`` silently reports every run as autonomous
    rather than raising, so nothing else here would fail.
    """
    from app.core.database import Base

    assert "triggered_by_user_id" in Base.metadata.tables["assignment_runs"].c


def test_owner_is_not_confused_with_trigger():
    """``user_id`` is the run's identity, not evidence a human started it.

    A scheduled run still resolves a ``user_id`` (from ``run_as_user_id`` or the
    assignment owner), so keying interactivity off it would classify every
    scheduled run as interactive and suppress the notification that makes an
    unattended pause visible.
    """
    assert is_interactive_run(_run(user_id="owner-1", triggered_by_user_id=None)) is False


def test_a_scheduled_fanout_is_autonomous_despite_carrying_a_user():
    """The case the identity check alone got wrong.

    A per-user fan-out passes the target user as ``triggered_by_user_id`` to
    execute as them — an execution identity, not an initiator. Reading that
    field alone called an unattended scheduled run interactive, which is the
    direction that leaves a stalled run silent.
    """
    run = _run(
        triggered_by_user_id="user-1",
        trigger_context={"type": "schedule_fanout", "cron": "0 * * * *"},
        trigger_payload={"trigger_type": "schedule_fanout"},
    )
    assert is_interactive_run(run) is False


def test_the_trigger_is_read_from_the_payload_when_no_context_says():
    """Both are stamped by the same call, but only the payload is guaranteed."""
    run = _run(
        triggered_by_user_id="user-1", trigger_payload={"trigger_type": "schedule"}
    )
    assert is_interactive_run(run) is False


def test_a_manual_trigger_keeps_its_identity():
    """The fix must not sweep in the runs a person really did start — an
    unrecognised trigger kind falls back to the identity field."""
    run = _run(
        triggered_by_user_id="user-1",
        trigger_context={"type": "manual"},
        trigger_payload={"trigger_type": "manual"},
    )
    assert is_interactive_run(run) is True


def test_an_agent_delegation_still_counts_as_its_user():
    """``trigger_assignment`` from a tool attributes the run to the user it acts
    for, and that person is reachable — the delegation is not in the unattended
    set."""
    run = _run(
        triggered_by_user_id="user-1",
        trigger_payload={"trigger_type": "delegation", "task": "x"},
    )
    assert is_interactive_run(run) is True

