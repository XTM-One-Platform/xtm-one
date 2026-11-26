"""An ask rewritten while the item keeps waiting reaches its open decision (#2264).

Before #2264 the decision hook acted only on a ``needs_human`` transition: an
``update_work_item(ask=...)`` that kept ``needs_human`` true left the open
decision on the old ask, so a card sent after the rewrite showed the old
question. The send also refused an integration named by its 8+ hex id prefix.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.models import decision_hooks as hooks
from app.services import work_item_notify as notify_mod
from tests.test_work_item_workspace_scope_postgres import _make_user, _temp_database

_ASK_A = {"question": "Ship A?", "options": [{"key": "A", "label": "Yes"}, {"key": "B", "label": "No"}]}
_ASK_B = {"question": "Ship B?", "options": [{"key": "A", "label": "Yes"}, {"key": "B", "label": "No"}]}


def test_a_rewritten_ask_while_waiting_is_a_reask() -> None:
    assert hooks.work_item_transition(True, True, answer=None, ask_changed=True) == ("reask",)


def test_an_unchanged_ask_while_waiting_does_nothing() -> None:
    assert hooks.work_item_transition(True, True, answer=None, ask_changed=False) is None


def test_an_ask_written_on_an_item_not_waiting_does_nothing() -> None:
    assert hooks.work_item_transition(False, False, answer=None, ask_changed=True) is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_open_decision_follows_the_rewritten_ask() -> None:
    from app.models.decision import Decision
    from app.models.work_item import WorkItem

    async with _temp_database() as sessions:
        async with sessions() as session:
            alice = await _make_user(session, email="alice@example.com")
            item = WorkItem(
                id=uuid.uuid4(), user_id=alice.id, title="Gate", status="blocked", needs_human=True, ask=_ASK_A
            )
            session.add(item)
            await session.commit()
            item_id = item.id
        async with sessions() as session:
            item = await session.get(WorkItem, item_id)
            item.ask = _ASK_B
            await session.commit()
        async with sessions() as session:
            decisions = (await session.execute(select(Decision).where(Decision.source_id == item_id))).scalars().all()
            opened = [d for d in decisions if d.status == "open"]
            assert len(opened) == 1
            assert opened[0].ask["question"] == "Ship B?"
            assert len(decisions) == 1


class _Result:
    def __init__(self, rows: list[uuid.UUID]) -> None:
        self._rows = rows

    def scalars(self) -> _Result:
        return self

    def first(self) -> uuid.UUID | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[uuid.UUID]:
        return list(self._rows)

    def scalar_one_or_none(self) -> uuid.UUID | None:
        return self._rows[0] if self._rows else None


class _Db:
    """Answers the name lookup first, then the prefix lookup."""

    def __init__(self, by_name: list[uuid.UUID], by_prefix: list[uuid.UUID]) -> None:
        self._answers = [by_name, by_prefix]

    async def execute(self, _stmt: Any) -> _Result:
        return _Result(self._answers.pop(0) if self._answers else [])


@pytest.fixture
def _visible(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    async def _filter(_db: Any, _user_id: Any) -> bool:
        return True

    import app.api.v1.helpers as helpers

    monkeypatch.setattr(helpers, "integration_visible_filter", _filter)
    yield


@pytest.mark.asyncio
async def test_an_integration_named_by_its_id_prefix_is_resolved(_visible: None) -> None:
    found = uuid.UUID("0123abcd-0000-0000-0000-000000000001")
    item = SimpleNamespace(user_id=uuid.uuid4())
    assert await notify_mod._resolve_integration(_Db([], [found]), item, "0123abcd") == str(found)


@pytest.mark.asyncio
async def test_an_ambiguous_integration_prefix_is_refused(_visible: None) -> None:
    item = SimpleNamespace(user_id=uuid.uuid4())
    two = [uuid.UUID("0123abcd-0000-0000-0000-000000000001"), uuid.UUID("0123abcd-0000-0000-0000-000000000002")]
    with pytest.raises(notify_mod.NotifyError, match="several integrations"):
        await notify_mod._resolve_integration(_Db([], two), item, "0123abcd")
