"""Tests that event_poller logs exceptions with full repr and exc_info.

"Event poll error" was logged with %s formatting which
produces an empty message for exceptions whose __str__ returns empty string
(e.g. bare ``ValueError()``, network exceptions with no args, etc.).  The fix
switches to %r + exc_info=True so the exception class name is always present.
"""

import inspect
import logging
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# Static source checks — ensure the format string uses %r and exc_info=True
# ---------------------------------------------------------------------------


def test_event_poller_warning_uses_repr_not_str():
    """The exception handler must use %r (not %s) so an empty-message exception
    still produces a non-empty log record."""
    from app.services.event_poller import poll_integration_events

    source = inspect.getsource(poll_integration_events)
    assert "Event poll error for assignment %s: %r" in source, (
        "poll_integration_events exception handler must log the exception with "
        "%r format so empty-__str__ exceptions are still identifiable"
    )


def test_event_poller_warning_uses_exc_info():
    """The exception handler must pass exc_info=True so the traceback is
    captured in the log record for diagnostics."""
    from app.services.event_poller import poll_integration_events

    source = inspect.getsource(poll_integration_events)
    assert "exc_info=True" in source, (
        "poll_integration_events exception handler must pass exc_info=True to "
        "include the full traceback in the warning log"
    )


# ---------------------------------------------------------------------------
# Runtime check — empty-message exception produces non-empty log message
# ---------------------------------------------------------------------------


async def test_event_poll_error_log_includes_exception_type(caplog):
    """When the per-assignment poll loop raises an exception with an empty
    __str__, the logged warning must still contain the exception class name
    (via repr) and carry the traceback (exc_info)."""
    from app.services import event_poller

    empty_exc = ValueError()  # str(empty_exc) == ""
    assert str(empty_exc) == "", "pre-condition: ValueError() stringifies to empty"

    assignment_id = "5d62d33c-cb27-45e8-80bd-57da15a30090"
    fake_assignment = MagicMock()
    fake_assignment.id = assignment_id
    fake_assignment.agent_id = None
    fake_assignment.trigger_config = {"provider": "microsoft", "event": "new_email"}

    # The assignments query (before the loop) must succeed and yield one
    # assignment; the failure is then raised INSIDE the per-assignment try
    # block (first redis.get call) so it flows through the except handler
    # under test instead of escaping poll_integration_events entirely.
    assignments_result = MagicMock()
    assignments_result.scalars.return_value.all.return_value = [fake_assignment]
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(return_value=assignments_result)

    fake_redis = AsyncMock()
    fake_redis.get.side_effect = empty_exc

    from tests.agentic_gate_stub import GATE, agentic_ai_available

    with (
        caplog.at_level(logging.WARNING, logger="app.services.event_poller"),
        patch.object(event_poller, "redis_client", new=AsyncMock(return_value=fake_redis)),
        # Each assignment asks the background gate first; AI may run.
        patch(GATE, agentic_ai_available),
    ):
        triggered = await event_poller.poll_integration_events(fake_db)

    assert triggered == 0

    matching = [
        record
        for record in caplog.records
        if assignment_id in record.getMessage() and record.levelname == "WARNING"
    ]
    assert matching, (
        "expected a 'Event poll error' warning for the failing assignment, "
        f"got records: {[r.getMessage() for r in caplog.records]!r}"
    )
    for record in matching:
        assert "ValueError" in record.getMessage(), (
            f"Warning log message must include exception class name via repr, "
            f"got: {record.getMessage()!r}"
        )
        assert record.exc_info, "warning must carry exc_info for the traceback"
