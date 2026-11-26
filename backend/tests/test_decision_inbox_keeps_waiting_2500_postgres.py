"""The decisions inbox agrees with "Waiting for me" on a keep-waiting answer (#2500).

A ``keeps_waiting`` answer leaves the item ``needs_human`` and its decision
row open (#1719), but the person owes nothing more: ``GET /decisions/waiting``
(``decision_inbox.waiting_for``) leaves the item out exactly as the work items
filter and its count do. A new ask from the agent drops the recorded answer,
so the item waits for the person again in both reads. An answer that decides
still answers the row, unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import app.services.work_item as wi_service
from app.core.config import settings
from app.models import decision_hooks
from app.services import decision_inbox
from app.services.company_managed_writes import person_writer
from tests.test_work_item_decision_finish_1719_postgres import _ASK, _asked
from tests.test_work_item_workspace_scope_postgres import (
    _make_agent,
    _make_user,
    _pointed_at_db,
    _temp_database,
)

pytestmark = pytest.mark.integration

_REASK = {
    **_ASK,
    "question": "Ship the patch now that the review is in?",
}


@pytest.fixture(autouse=True)
def _inbox_on(monkeypatch: pytest.MonkeyPatch) -> Any:
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()
    monkeypatch.setattr(settings, "decisions_read_enabled", True)
    yield
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()


async def _inbox_titles(sessions: Any, person: Any) -> list[str]:
    async with sessions() as session:
        answer = await decision_inbox.waiting_for(session, SimpleNamespace(id=person.id, is_admin=False))
    assert answer["enabled"] is True, answer
    return sorted(str(row["origin"]) for row in answer["items"] if row["source_type"] == "work_item")


@pytest.mark.asyncio
async def test_a_keep_waiting_answer_leaves_the_inbox_and_matches_the_count() -> None:
    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="inbox-keep@example.com")
            agent = await _make_agent(session)
            kept = _asked(person, agent, "kept")
            unanswered = _asked(person, agent, "unanswered")
            session.add_all([kept, unanswered])
            await session.commit()
            kept_id = str(kept.id)

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            before = await _inbox_titles(sessions, person)
            await wi_service.answer_work_item(kept_id, user_id=uid, option="wait", answered_by="Person", writer=writer)
            after = await _inbox_titles(sessions, person)
            listed = await wi_service.list_work_items(user_id=uid, waiting_for_me=True)
            counted = await wi_service.count_work_items(user_id=uid, waiting_for_me=True)

            # The agent asks again: a new question, the earlier answer no longer stands.
            reasked = await wi_service.update_work_item(kept_id, user_id=uid, ask=_REASK, writer=writer)
            again = await _inbox_titles(sessions, person)
            counted_again = await wi_service.count_work_items(user_id=uid, waiting_for_me=True)

    assert before == ["kept", "unanswered"]
    # The inbox and the "Waiting for me" filter read one source of truth.
    assert after == ["unanswered"]
    assert [row["title"] for row in listed] == ["unanswered"]
    assert counted == len(after) == 1
    assert reasked is not None and "answer" not in (reasked.get("payload") or {})
    assert reasked["needs_human"] is True
    assert again == ["kept", "unanswered"]
    assert counted_again == 2


@pytest.mark.asyncio
async def test_a_deciding_answer_still_answers_the_decision() -> None:
    writer = person_writer(is_admin=False)
    async with _temp_database() as sessions:
        async with sessions() as session:
            person = await _make_user(session, email="inbox-ship@example.com")
            agent = await _make_agent(session)
            item = _asked(person, agent, "shipped")
            session.add(item)
            await session.commit()
            item_id = str(item.id)

        async with _pointed_at_db(sessions):
            uid = str(person.id)
            shipped = await wi_service.answer_work_item(item_id, user_id=uid, option="ship", writer=writer)
            after = await _inbox_titles(sessions, person)

    assert shipped is not None and shipped["status"] == "open" and shipped["needs_human"] is False
    assert after == []
