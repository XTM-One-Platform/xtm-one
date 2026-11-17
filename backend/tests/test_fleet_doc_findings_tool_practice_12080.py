"""The fleet tool lists what the About page chip counts (#12080).

Pinned here, without a database: a fleet with one good-practice suggestion and no
stored finding shows "1 suggestion" on its About page; ``list_fleet_doc_findings``
lists the same id with its kind, member and applicable flag, the same count, and
both read 0 once the tool dismissed it.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import fleet_docs
from app.services import fleet_practice_suggestions as practice

PRINTS = {"": "fleet-1", "triage": "t-1"}


def _fleet() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Alert Desk",
        slug="alert-desk",
        is_builtin=False,
        documentation={"sections": {"purpose": "Triage alerts."}, "version": 1, "findings": []},
    )


@pytest.fixture
def one_practice(monkeypatch):
    triage = SimpleNamespace(id=uuid.uuid4(), name="Triage")
    item = practice._suggestion(
        "member_cannot_consult_orchestrator",
        triage,
        "Desk Lead",
        "Triage cannot consult Desk Lead for a route, scope or context question.",
        "Add a Consult handover from Triage to Desk Lead.",
    )

    async def _practice(_db, _fleet):
        return [dict(item)]

    async def _prints(_db, _fleet):
        return dict(PRINTS)

    async def _fp(_db, _fleet):
        return "f"

    monkeypatch.setattr(practice, "practice_suggestions", _practice)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _prints)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    return item


def _tool(monkeypatch, fleet, audits: list):
    from app.services import audit
    from app.services.builtin_tools import fleets as fleet_tools

    class _Db:
        async def commit(self) -> None: ...

        async def rollback(self) -> None: ...

    @asynccontextmanager
    async def _factory():
        yield _Db()

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Owner")

    async def _load(_db, _uid):
        return user

    async def _manager(*_a, **_kw):
        return fleet, None

    async def _log(_db, **kw):
        audits.append(kw)

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manager)
    monkeypatch.setattr(audit, "log_audit", _log)
    _defs, executor = fleet_tools.build_fleet_tools(str(user.id))
    return executor.handler("list_fleet_doc_findings")


async def test_the_tool_lists_the_practice_suggestion_the_chip_counts(monkeypatch, one_practice) -> None:
    fleet = _fleet()
    view = await fleet_docs.documentation_view(object(), fleet, can_edit=True)
    assert [f["id"] for f in view.findings] == [one_practice["id"]], "the chip counts 1"

    handler = _tool(monkeypatch, fleet, [])
    answer = await handler(fleet_id=str(fleet.id), action="list")
    assert f"Fleet **Alert Desk**: {len(view.findings)} suggestion(s), 0 dismissed." in answer, answer
    assert (
        f"`{one_practice['id']}` member_cannot_consult_orchestrator (Triage)"
        " [good practice, applicable on the About page]:" in answer
    ), answer
    assert "no configuration suggestion" not in answer


async def test_after_a_dismiss_both_read_zero_and_the_dismiss_is_audited(monkeypatch, one_practice) -> None:
    fleet = _fleet()
    audits: list = []
    handler = _tool(monkeypatch, fleet, audits)
    done = await handler(fleet_id=str(fleet.id), action="dismiss", reason="isolated on purpose")
    assert "1 suggestion(s) dismissed." in done, done
    assert [a["details"]["findings"] for a in audits] == [[one_practice["id"]]]

    view = await fleet_docs.documentation_view(object(), fleet, can_edit=True)
    assert view.findings == [] and [d["id"] for d in view.dismissed_findings] == [one_practice["id"]]
    answer = await handler(fleet_id=str(fleet.id), action="list")
    assert "0 suggestion(s), 1 dismissed." in answer, answer
    assert fleet.documentation["findings"] == [], "a practice suggestion is never stored"


async def test_a_fleet_with_nothing_to_suggest_reads_zero(monkeypatch, one_practice) -> None:
    async def _none(_db, _fleet):
        return []

    monkeypatch.setattr(practice, "practice_suggestions", _none)
    fleet = _fleet()
    answer = await _tool(monkeypatch, fleet, [])(fleet_id=str(fleet.id), action="list")
    assert answer == "Fleet **Alert Desk**: 0 suggestion(s), 0 dismissed.", answer
    assert await fleet_docs.manager_suggestions(object(), SimpleNamespace(documentation=None)) == ([], [], {})
