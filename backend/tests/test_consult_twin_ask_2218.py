"""A caller that asks again in other words lands on its open durable ask (#2218).

24 Nov 2026: one partner question became two ``consult:`` asks (different
wording, so different digests) beside a third item: three runs on one
question. The durable ask of a cut consult now reuses the newest open ask of
the same caller to the same target for the same person within
``TWIN_ASK_WINDOW``: the new wording goes on it as a note, nothing is filed.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.services import consult_watchdog as cw

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _peer_has_an_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    async def has_inbox(**_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(cw, "_peer_has_inbox", has_inbox)


def _capture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[dict[str, object]], list[tuple[str, dict[str, object]]]]:
    from app.services import work_item

    created: list[dict[str, object]] = []
    notes: list[tuple[str, dict[str, object]]] = []

    async def fake_create(**kwargs: object) -> dict[str, object]:
        created.append(dict(kwargs))
        return {"id": f"item-{len(created)}"}

    async def fake_update(item_id: str, **kwargs: object) -> dict[str, object]:
        notes.append((item_id, kwargs))
        return {"id": item_id}

    monkeypatch.setattr(work_item, "create_work_item", fake_create)
    monkeypatch.setattr(work_item, "update_work_item", fake_update)
    return created, notes


async def _cut(question: str, *, held: str | None = "held-1") -> str | None:
    return await cw.durable_consult_after_cut(
        user_id="u-1",
        caller_agent_id="caller-agent",
        caller_name="Caller",
        caller_is_company_managed=False,
        target_id="peer-agent-0001",
        target_name="Peer",
        question=question,
        cuts=1,
        source_work_item_id=held,
        parent_run_id="run-2",
    )


async def test_a_reworded_ask_becomes_a_note_on_the_open_one(monkeypatch: pytest.MonkeyPatch) -> None:
    created, notes = _capture(monkeypatch)
    first_key = cw.durable_consult_key("peer-agent-0001", "What is the status of X?")

    async def twin(**kwargs: object) -> tuple[str, str]:
        assert kwargs == {"user_id": "u-1", "caller_agent_id": "caller-agent", "target_id": "peer-agent-0001"}
        return ("open-ask-1", first_key)

    monkeypatch.setattr(cw, "open_twin_ask", twin)
    text = await _cut("Where does X stand now, please?")

    # No second ask: only the caller's follow-up, blocked on the open ask.
    assert [c["correlation_key"] for c in created] == [f"{first_key}-answer"]
    assert created[0]["blocked_on"] == ["open-ask-1"]
    assert len(notes) == 1
    item_id, kwargs = notes[0]
    assert item_id == "open-ask-1"
    assert "Where does X stand now, please?" in str(kwargs["note"])
    assert kwargs["stamp_note"] is True
    assert text is not None and "open-ask-1" in text


async def test_the_same_wording_keeps_the_key_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same question, same key: the existing create dedupe answers, no note."""
    created, notes = _capture(monkeypatch)
    question = "What is the status of X?"

    async def twin(**_kwargs: object) -> tuple[str, str]:
        return ("open-ask-1", cw.durable_consult_key("peer-agent-0001", question))

    monkeypatch.setattr(cw, "open_twin_ask", twin)
    await _cut(question, held=None)
    assert notes == []
    assert [c["correlation_key"] for c in created] == [cw.durable_consult_key("peer-agent-0001", question)]


async def test_no_open_ask_files_one_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    created, notes = _capture(monkeypatch)

    async def none(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(cw, "open_twin_ask", none)
    await _cut("q", held=None)
    assert notes == []
    assert len(created) == 1 and created[0]["owner_agent_id"] == "peer-agent-0001"


async def test_the_twin_read_is_scoped_to_caller_target_person_and_open_asks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The query names the caller, the target, the person, open statuses and skips -answer items."""
    from app.core import database

    seen: list[str] = []

    class _Rows:
        def first(self) -> None:
            return None

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def execute(self, query: Any) -> _Rows:
            from sqlalchemy.dialects import postgresql

            dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
            seen.append(str(query.compile(dialect=dialect, compile_kwargs={"literal_binds": True})))
            return _Rows()

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    caller, target, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert await cw.open_twin_ask(user_id=str(user), caller_agent_id=str(caller), target_id=str(target)) is None
    (sql,) = seen
    assert str(caller).replace("-", "") in sql.replace("-", "")
    assert str(target).replace("-", "") in sql.replace("-", "")
    assert str(user).replace("-", "") in sql.replace("-", "")
    assert f"consult:{str(target)[:8]}-%" in sql
    assert "-answer" in sql
    assert "'done'" in sql and "'cancelled'" in sql


async def test_an_unreadable_twin_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await cw.open_twin_ask(user_id="u-1", caller_agent_id="not-a-uuid", target_id="x") is None
