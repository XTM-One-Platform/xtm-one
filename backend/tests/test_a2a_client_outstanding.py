"""A delegation is a row while its partner task is still running.

Spec 011 foundational phase. Until now the row was written only at conclusion,
so a partner task in flight had no record at all — which is why two live
delegations were invisible in ``a2a_delegations`` during manual verification.
Three things need it earlier: a collect, for its ownership check; the cancel
cascade, to know what to cancel; and a partner's callback, to find the run to
resume.

Outstanding is the ABSENCE of a conclusion — no outcome, no ``terminal_at`` —
not a state column. The migrated schema has none.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.a2a.client import delegation
from app.a2a.client.delegation import CallingRun, DelegationResult
from app.models.a2a import MECHANISM_ASKED, OUTCOME_COMPLETED
from tests.a2a_client_stubs import RecordingDb, integration_row, settings_row

pytestmark = pytest.mark.asyncio


def _calling():
    return CallingRun(agent_id=uuid.uuid4(), user_id=uuid.uuid4(), run_ref=uuid.uuid4())


def _result(**kw):
    fields = {
        "outcome": OUTCOME_COMPLETED,
        "partner_task_id": "partner-task-1",
        "protocol_version": "0.3",
        "signature_state": "unsigned",
    }
    fields.update(kw)
    return DelegationResult(**fields)


class TestTheOutstandingRow:
    async def test_it_carries_no_conclusion(self):
        """The absence of an outcome is what "still running" means, so writing
        one here would report the exchange finished."""
        db, integration = RecordingDb(), integration_row()
        await delegation._record_outstanding(
            db,
            integration,
            settings_row(),
            _calling(),
            _result(outcome=""),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        assert len(db.added) == 1
        row = db.added[0]
        assert row.outcome is None
        assert row.terminal_at is None

    async def test_it_carries_what_a_collect_and_a_callback_need(self):
        """The three lookups that now depend on this row: ownership matches
        integration, target and user; the cancel cascade needs the task id;
        resumption needs the run."""
        db, integration, calling = RecordingDb(), integration_row(), _calling()
        # An explicit id: the shared stub leaves it off, and the target is what
        # scopes ownership alongside the integration and the user.
        target = settings_row(id=uuid.uuid4())
        await delegation._record_outstanding(
            db,
            integration,
            target,
            calling,
            _result(),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        row = db.added[0]
        assert row.integration_id == integration.id
        assert row.target_id == target.id
        assert row.user_id == calling.user_id
        assert row.run_ref == calling.run_ref
        assert row.partner_task_id == "partner-task-1"

    async def test_it_records_how_the_delegation_waited(self):
        db = RecordingDb()
        await delegation._record_outstanding(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        assert db.added[0].mechanism == MECHANISM_ASKED

    async def test_a_task_with_no_identity_is_not_recorded(self):
        """Nothing could collect, cancel or resume it, so a row would only be
        a row nobody can act on."""
        db = RecordingDb()
        await delegation._record_outstanding(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(partner_task_id=None),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        assert db.added == []

    async def test_it_is_written_once_for_one_exchange(self):
        """A collect that finds the task still running must not add a second
        row for the same task."""
        existing = SimpleNamespace(partner_task_id="partner-task-1", outcome=None)
        db = RecordingDb(outstanding=existing)
        await delegation._record_outstanding(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        assert db.added == []

    async def test_bookkeeping_failure_costs_the_caller_nothing(self):
        """Same contract as the terminal recorder: a savepoint confines it, and
        a delegation that worked is not reported as a platform failure."""
        db = RecordingDb(fail=True)
        await delegation._record_outstanding(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(),
            submitted_at=datetime.now(UTC),
            mechanism=MECHANISM_ASKED,
        )
        assert db.added == []


class TestConcludingIt:
    async def test_the_same_row_concludes(self):
        """One exchange is one row. A second would make one delegation look
        like two to every count an operator reads."""
        existing = SimpleNamespace(
            partner_task_id="partner-task-1",
            outcome=None,
            failure_kind=None,
            partner_reason=None,
            continuation_count=0,
            terminal_at=None,
        )
        db = RecordingDb(outstanding=existing)
        await delegation._record(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(),
            submitted_at=datetime.now(UTC),
        )
        assert db.added == []
        assert existing.outcome == OUTCOME_COMPLETED
        assert existing.terminal_at is not None

    async def test_an_exchange_never_handed_back_still_writes_one_row(self):
        """The ordinary short delegation: no hand-back, so nothing to update."""
        db = RecordingDb()
        await delegation._record(
            db,
            integration_row(),
            settings_row(),
            _calling(),
            _result(),
            submitted_at=datetime.now(UTC),
        )
        assert len(db.added) == 1
        assert db.added[0].outcome == OUTCOME_COMPLETED
        assert db.added[0].terminal_at is not None
