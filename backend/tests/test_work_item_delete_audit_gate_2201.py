"""Deleting a work item that waits on a person settles its decision and leaves an audit row (#2201).

Before #2201 the single delete (``db.delete``) and the bulk delete (a core
``DELETE`` the decision hook never sees) removed an open ``needs_human`` item
with no audit row, and the bulk path left its decision ``open``: the gate
vanished and the case it held stayed waiting. Both paths now cancel the open
decision with the reason ``work item deleted`` and write one audit row per item.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.services.work_item_delete_audit import DELETED_REASON, audit_details, audit_summary
from tests.test_work_item_workspace_scope_postgres import (
    _PERSON,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

_ASK = {
    "question": "Cancel the run?",
    "options": [{"key": "A", "label": "Cancel"}, {"key": "B", "label": "Keep"}],
    "recommendation": "B",
}


def test_audit_summary_names_a_waiting_gate():
    item = SimpleNamespace(title="Gate", status="blocked", needs_human=True, correlation_key="k", owner_agent_id=None)
    assert "waited on a person's decision" in audit_summary(item, ["d1"])
    details = audit_details(item, ["d1"])
    assert details["needs_human"] is True and details["cancelled_decisions"] == ["d1"]


def test_audit_summary_of_a_plain_item():
    item = SimpleNamespace(title="Plain", status="done", needs_human=False, correlation_key=None, owner_agent_id=None)
    assert audit_summary(item, []) == "Deleted work item 'Plain' (done)"


async def _waiting_item(session, user):
    """An open needs_human item of *user* with an open decision (opened by the hook, else here)."""
    from app.models.decision import Decision
    from app.models.work_item import WorkItem

    item = WorkItem(
        id=uuid.uuid4(),
        user_id=user.id,
        title="Waiting on a person",
        status="blocked",
        needs_human=True,
        ask=_ASK,
    )
    session.add(item)
    await session.flush()
    opened = (
        (await session.execute(select(Decision).where(Decision.source_id == item.id, Decision.status == "open")))
        .scalars()
        .all()
    )
    if not opened:
        session.add(
            Decision(
                id=uuid.uuid4(),
                source_type="work_item",
                source_id=item.id,
                kind="work_item",
                ask=_ASK,
                recipient=str(user.id),
                status="open",
            )
        )
    await session.commit()
    return item.id


async def _assert_settled(sessions, item_id):
    from app.models.audit_log import AuditLog
    from app.models.decision import Decision, DecisionEvent
    from app.models.work_item import WorkItem

    async with sessions() as session:
        assert await session.get(WorkItem, item_id) is None
        decisions = (await session.execute(select(Decision).where(Decision.source_id == item_id))).scalars().all()
        assert decisions and all(d.status != "open" for d in decisions)
        reasons = (
            (
                await session.execute(
                    select(DecisionEvent.detail).where(
                        DecisionEvent.decision_id.in_([d.id for d in decisions]), DecisionEvent.event == "cancelled"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert {"reason": DELETED_REASON} in reasons
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(AuditLog.entity_type == "work_item", AuditLog.entity_id == item_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(audits) == 1
        assert audits[0].action == "delete"
        assert audits[0].details["needs_human"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_single_delete_of_a_waiting_item_cancels_its_decision_and_audits() -> None:
    from app.services.work_item import delete_work_item

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            item_id = await _waiting_item(session, alice)
            alice_id = str(alice.id)
        async with _pointed_at_db(sessions):
            assert await delete_work_item(str(item_id), user_id=alice_id, writer=_PERSON) is True
        await _assert_settled(sessions, item_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bulk_delete_of_a_waiting_item_cancels_its_decision_and_audits() -> None:
    from app.services.work_item import bulk_delete_work_items

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            item_id = await _waiting_item(session, alice)
            alice_id = str(alice.id)
        async with _pointed_at_db(sessions):
            assert await bulk_delete_work_items(user_id=alice_id, ids=[str(item_id)]) == 1
        await _assert_settled(sessions, item_id)
