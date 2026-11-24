"""A refused write on an item the person reads answers 403 with why (#1845).

The read tiers (``_visible_clause``) admit the stored decision recipient
(#901) and every readable workspace; the write tiers (``_writable_clause``)
do not. A person who opened such an item and pressed Cancel got
"Work item not found" on the item the dialog was showing. The write now
answers 403 with the reason, the detail read carries ``can_write`` and
``read_only_reason``, and an item the person cannot read stays a 404.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.services.work_item as wi_service
from app.api.v1.work_items import _write_missed
from app.models.work_item import WorkItem
from app.services.company_managed_writes import person_writer
from app.services.workspaces import create_workspace
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration


async def _items(sessions: async_sessionmaker[AsyncSession]) -> dict[str, str]:
    """A filed item waiting for another person, and an unfiled one waiting for them."""
    async with sessions() as session:
        filer = await _make_user(session, email="filer@example.com")
        recipient = await _make_user(session, email="recipient@example.com")
        stranger = await _make_user(session, email="stranger@example.com")
        agent = await _make_agent(session, name="Other fleet assistant")
        agent.user_id = filer.id
        ws = await create_workspace(session, user_id=filer.id, name="Other fleet")
        await session.flush()
        filed = WorkItem(
            id=uuid.uuid4(),
            user_id=filer.id,
            title="Filed decision",
            workspace_id=ws.id,
            owner_agent_id=agent.id,
            created_by_agent_id=agent.id,
            status="open",
            waits_for_user_id=recipient.id,
        )
        unfiled = WorkItem(
            id=uuid.uuid4(),
            user_id=filer.id,
            title="Unfiled decision",
            owner_agent_id=agent.id,
            created_by_agent_id=agent.id,
            status="open",
            waits_for_user_id=recipient.id,
        )
        session.add_all([filed, unfiled])
        await session.commit()
        return {
            "filed": str(filed.id),
            "unfiled": str(unfiled.id),
            "filer": str(filer.id),
            "recipient": str(recipient.id),
            "stranger": str(stranger.id),
        }


@pytest.mark.asyncio
async def test_the_recipient_reads_a_reason_and_the_filer_may_write() -> None:
    async with _temp_database() as sessions:
        ids = await _items(sessions)
        async with _pointed_at_db(sessions):
            recipient_filed = await wi_service.person_write_access(ids["filed"], user_id=ids["recipient"])
            recipient_unfiled = await wi_service.person_write_access(ids["unfiled"], user_id=ids["recipient"])
            filer = await wi_service.person_write_access(ids["filed"], user_id=ids["filer"])
            stranger = await wi_service.person_write_access(ids["filed"], user_id=ids["stranger"])

    assert recipient_filed == (False, wi_service.READ_ONLY_RECIPIENT)
    # The recipient reason wins over "another person's chat" (#1910): it says what they may do.
    assert recipient_unfiled == (False, wi_service.READ_ONLY_RECIPIENT)
    assert filer == (True, None)
    assert stranger is None


@pytest.mark.asyncio
async def test_a_refused_cancel_answers_403_with_the_reason_never_not_found() -> None:
    async with _temp_database() as sessions:
        ids = await _items(sessions)
        async with _pointed_at_db(sessions):
            cancelled = await wi_service.update_work_item(
                ids["filed"],
                user_id=ids["recipient"],
                status="cancelled",
                writer=person_writer(is_admin=False),
            )
            refused = await _write_missed(ids["filed"], SimpleNamespace(id=ids["recipient"]))
            hidden = await _write_missed(ids["filed"], SimpleNamespace(id=ids["stranger"]))
            by_filer = await wi_service.update_work_item(
                ids["filed"],
                user_id=ids["filer"],
                status="cancelled",
                writer=person_writer(is_admin=False),
            )

    assert cancelled is None, "the recipient tier (#1910) cancels through complete, never a generic update"
    assert refused.status_code == 403
    assert refused.detail == wi_service.READ_ONLY_RECIPIENT
    assert hidden.status_code == 404
    assert by_filer is not None and by_filer["status"] == "cancelled"


@pytest.mark.asyncio
async def test_the_recipient_may_cancel_and_answer_and_nothing_else() -> None:
    """#1910: the decision recipient's tier is cancel and answer only."""
    from sqlalchemy import select

    from app.models.audit_log import AuditLog

    async with _temp_database() as sessions:
        ids = await _items(sessions)
        async with sessions() as session:
            item = await session.get(WorkItem, uuid.UUID(ids["filed"]))
            item.needs_human = True
            item.status = "blocked"
            item.ask = {"question": "Ship it?", "options": [{"key": "A", "label": "Yes"}, {"key": "B", "label": "No"}]}
            await session.commit()
        writer = person_writer(is_admin=False)
        async with _pointed_at_db(sessions):
            may_cancel = await wi_service.person_may_cancel(ids["filed"], user_id=ids["recipient"])
            stranger_may_cancel = await wi_service.person_may_cancel(ids["filed"], user_id=ids["stranger"])
            stranger_cancel = await wi_service.complete_work_item(
                ids["filed"], user_id=ids["stranger"], status="cancelled", writer=writer
            )
            done = await wi_service.complete_work_item(
                ids["filed"], user_id=ids["recipient"], status="done", writer=writer
            )
            edited = await wi_service.update_work_item(
                ids["filed"], user_id=ids["recipient"], title="Renamed", writer=writer
            )
            answered = await wi_service.answer_work_item(
                ids["filed"], user_id=ids["recipient"], option="A", writer=writer
            )
            cancelled = await wi_service.complete_work_item(
                ids["unfiled"], user_id=ids["recipient"], status="cancelled", writer=writer
            )
        async with sessions() as session:
            audited = (
                await session.execute(
                    select(AuditLog.action, AuditLog.entity_id).where(
                        AuditLog.user_id == uuid.UUID(ids["recipient"]), AuditLog.entity_type == "work_item"
                    )
                )
            ).all()

    assert may_cancel is True
    assert stranger_may_cancel is False
    assert stranger_cancel is None, "a reader who is not the recipient gets no tier"
    assert done is None, "mark done is not in the recipient tier"
    assert edited is None, "an edit is not in the recipient tier"
    assert answered is not None and answered["payload"]["answer"]["option"] == "A"
    assert cancelled is not None and cancelled["status"] == "cancelled"
    actions = {(a, str(e)) for a, e in audited}
    assert ("answer", ids["filed"]) in actions
    assert ("cancel", ids["unfiled"]) in actions
