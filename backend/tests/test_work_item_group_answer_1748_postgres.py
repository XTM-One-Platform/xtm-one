# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""A decision addressed to a group, answered twice through ``answer_work_item`` (#1748, slice 2b).

The first member's answer clears ``needs_human``; a second member of the
group, who does not otherwise write the item, reads who decided first and
when (the 409 of the router), not "not found". Someone outside the group
still reads "not found".
"""

from __future__ import annotations

import uuid

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import person_writer
from tests.test_work_item_waiting_for_person_postgres import _row
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_ASK = {
    "question": "Accept the remediation?",
    "options": [{"key": "A", "label": "Accept"}, {"key": "B", "label": "Reject"}],
    "recommendation": "A",
}


@pytest.mark.asyncio
async def test_a_second_group_member_reads_who_decided_first() -> None:
    from app.models.group import Group, GroupMember

    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            filer = await _make_user(session, email="filer@example.com")
            first = await _make_user(session, email="first@example.com")
            second = await _make_user(session, email="second@example.com")
            outsider = await _make_user(session, email="outsider@example.com")
            agent = await _make_agent(session)
            group = Group(name=f"ctem-deciders-{uuid.uuid4().hex[:6]}")
            session.add(group)
            await session.flush()
            session.add_all(
                [
                    GroupMember(group_id=group.id, user_id=first.id),
                    GroupMember(group_id=group.id, user_id=second.id),
                ]
            )
            asked = _row(
                filer,
                title="gate",
                owner_agent_id=agent.id,
                status="blocked",
                needs_human=True,
                blocked_reason="accept the remediation?",
                ask=_ASK,
                waits_for_group_ids=[str(group.id)],
            )
            session.add(asked)
            await session.commit()
            asked_id = str(asked.id)

        async with _pointed_at_db(sessions):
            answered = await wi_service.answer_work_item(
                asked_id, user_id=str(first.id), option="A", answered_by="First", writer=writer
            )
            with pytest.raises(wi_service.WorkItemNotWaitingError, match="already decided by First at "):
                await wi_service.answer_work_item(asked_id, user_id=str(second.id), option="B", writer=writer)
            stranger = await wi_service.answer_work_item(asked_id, user_id=str(outsider.id), option="B", writer=writer)

    assert answered is not None and answered["payload"]["answer"]["option"] == "A"
    assert stranger is None
