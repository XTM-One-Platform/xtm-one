"""blocked_on admits what the writing agent's own read admits (#1593).

An agent reads a company-managed gate filed for it in its fleet workspace
(the owner-agent tier of #11900) but blocking its loop item on that gate was
refused: ``_check_blockers`` built the visibility without the actor. These
tests pin the actor's tiers and the creator's narrowing on the check, and a
person's check left unchanged.

No database: SQL is compiled and read, sessions are fakes.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.services.company_managed_writes import CompanyManagedWriter, agent_writer, person_writer

AGENT = uuid.uuid4()
CM_AGENT = agent_writer(is_company_managed=True, agent_id=AGENT)


class _Result:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_Result":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _Db:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self._rows)


def _sql(statement: Any) -> str:
    return str(statement.compile(dialect=postgresql.dialect())).lower()  # type: ignore[no-untyped-call]


def _item(status: str = "open", *, company: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status=status,
        is_company_managed=company,
        created_by_agent_id=uuid.uuid4(),
        owner_agent_id=AGENT,
        claimed_by_agent_id=None,
    )


async def _no_policies(_db: Any, _actor: Any, _items: Any) -> dict[Any, Any]:
    return {}


@pytest.fixture(autouse=True)
def _no_workspaces(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _tiers(_db: Any, _uid: Any) -> tuple[list[Any], list[Any]]:
        return [], []

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_policies_for", _no_policies)


@pytest.mark.asyncio
async def test_the_agent_check_selects_through_its_owner_agent_tier() -> None:
    gate = _item()
    db = _Db([gate])
    await wi_service._check_blockers(db, None, [str(gate.id)], CM_AGENT)
    assert "fleets.is_archived is false" in _sql(db.statements[0])


@pytest.mark.asyncio
async def test_the_creator_narrowing_still_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _item()
    monkeypatch.setattr(wi_service, "_admits", lambda *_a, **_k: False)
    with pytest.raises(ValueError, match="cannot see"):
        await wi_service._check_blockers(_Db([gate]), None, [str(gate.id)], CM_AGENT)


@pytest.mark.asyncio
async def test_a_cancelled_gate_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _item("cancelled")
    monkeypatch.setattr(wi_service, "_admits", lambda *_a, **_k: True)
    with pytest.raises(ValueError, match="cancelled"):
        await wi_service._check_blockers(_Db([gate]), None, [str(gate.id)], CM_AGENT)


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", [None, person_writer(is_admin=True)])
async def test_a_person_s_check_is_unchanged(reader: CompanyManagedWriter | None) -> None:
    gate = _item(company=False)
    db = _Db([gate])
    await wi_service._check_blockers(db, str(uuid.uuid4()), [str(gate.id)], reader)
    assert "is_archived" not in _sql(db.statements[0])
