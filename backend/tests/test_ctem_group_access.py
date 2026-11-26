# SPDX-License-Identifier: Apache-2.0

"""CTEM rights granted to a group (#1748): view reads the organization, decide also acts.

Since #2441 a person with no right has no CTEM at all: 403 on the API, no own
signals, no CTEM tool or event.

Covers ``services/ctem_access.py`` (the one reader), the signal API (list, read,
triage), the signal tool reach and the event delivery rule. The group
membership query is replaced by a dict of rights per user.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request

from app.api.deps import get_current_user
from app.api.v1 import ctem_signals as api_module
from app.core.config import settings
from app.core.database import get_db
from app.models.ctem_signal import CtemSignal
from app.services import ctem_access
from app.services import ctem_signals as signals
from app.services.builtin_tools import build_ctem_signal_tools
from app.tools.base import ToolCall

ORG = uuid.uuid4()
ADMIN = str(uuid.uuid4())
VIEWER = str(uuid.uuid4())
DECIDER = str(uuid.uuid4())
NOBODY = str(uuid.uuid4())

RIGHTS: dict[str, str] = {VIEWER: "view", DECIDER: "decide"}


def _signal(recorded_by: str, state: str = "to_validate") -> CtemSignal:
    return CtemSignal(
        id=uuid.uuid4(),
        organization_id=ORG,
        trigger_path="new_vulnerability",
        lens="vulnerability",
        source_provider="opencti",
        subject_kind="asset",
        subject_ref="host-1",
        state=state,
        recorded_via="listener",
        recorded_by=recorded_by,
    )


class _Store:
    def __init__(self) -> None:
        self.rows = [_signal("system"), _signal("system"), _signal(NOBODY)]
        self.triaged: list[tuple[list[Any], str]] = []

    def _mine(self, recorded_by: str | None) -> list[CtemSignal]:
        return [r for r in self.rows if recorded_by is None or r.recorded_by == recorded_by]

    async def list(self, _db: Any, *, organization_id: Any, recorded_by: Any, **_kw: Any) -> Any:
        rows = self._mine(recorded_by)
        return rows, len(rows), {}

    async def get(self, _db: Any, signal_id: Any, *, organization_id: Any, recorded_by: Any) -> Any:
        return next((r for r in self._mine(recorded_by) if r.id == signal_id), None)

    async def select(self, _db: Any, *, organization_id: Any, recorded_by: Any, ids: Any = None, **_kw: Any) -> Any:
        return [r for r in self._mine(recorded_by) if not ids or r.id in ids]


class _Db:
    async def commit(self) -> None:
        return None

    async def get(self, _model: Any, ident: Any) -> Any:
        return SimpleNamespace(id=uuid.UUID(str(ident)), is_admin=str(ident) == ADMIN)


@pytest.fixture(autouse=True)
def rights(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    async def _right(_db: Any, user_id: Any) -> str | None:
        return RIGHTS.get(str(user_id))

    monkeypatch.setattr(ctem_access, "group_right", _right)
    return RIGHTS


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> _Store:
    fake = _Store()
    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    monkeypatch.setattr(signals, "list_signals", fake.list)
    monkeypatch.setattr(signals, "get_signal", fake.get)
    monkeypatch.setattr(signals, "select_signals", fake.select)

    async def _triage(_db: Any, rows: list[Any], *, action: str, actor: str, evaluator_id: Any) -> dict[str, int]:
        fake.triaged.append((rows, action))
        return {"changed": len(rows)}

    from app.services import ctem_scope_gate_store

    monkeypatch.setattr(ctem_scope_gate_store, "triage", _triage)
    return fake


@pytest.fixture
def client(store: _Store, monkeypatch: pytest.MonkeyPatch) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(api_module.router, prefix="/api/v1/ctem-signals")

    async def _primary(_db: Any) -> Any:
        return SimpleNamespace(id=ORG)

    monkeypatch.setattr(api_module, "get_primary", _primary)

    async def _db() -> Any:
        yield _Db()

    async def _user(request: Request) -> Any:
        uid = request.headers["X-User"]
        return SimpleNamespace(id=uuid.UUID(uid), is_admin=uid == ADMIN, name=uid, email=None)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = _user
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# --- the one reader -------------------------------------------------------------------------


def test_a_right_is_view_decide_or_none() -> None:
    assert ctem_access.normalize_right("VIEW") == "view"
    assert ctem_access.normalize_right(" decide ") == "decide"
    assert ctem_access.normalize_right("none") is None
    assert ctem_access.normalize_right(None) is None
    with pytest.raises(ValueError):
        ctem_access.normalize_right("admin")


async def test_levels_by_right() -> None:
    db = _Db()
    assert await ctem_access.level(db, ADMIN) == "admin"
    assert await ctem_access.level(db, DECIDER) == "decide"
    assert await ctem_access.level(db, VIEWER) == "view"
    assert await ctem_access.level(db, NOBODY) is None
    assert await ctem_access.reads_organization(db, VIEWER) is True
    assert await ctem_access.may_decide(db, VIEWER) is False
    assert await ctem_access.may_decide(db, DECIDER) is True
    with pytest.raises(ctem_access.NoCtemAccess):
        await ctem_access.signal_reader(db, NOBODY)
    with pytest.raises(PermissionError):
        await ctem_access.signal_writer(db, VIEWER)
    with pytest.raises(ctem_access.NoCtemAccess):
        await ctem_access.signal_writer(db, NOBODY)
    assert await ctem_access.signal_writer(db, DECIDER) is None
    assert await ctem_access.refusal(db, NOBODY, write=False) == ctem_access.NO_ACCESS_MESSAGE
    assert await ctem_access.refusal(db, VIEWER, write=False) is None
    assert await ctem_access.refusal(db, VIEWER, write=True) == ctem_access.VIEW_ONLY_MESSAGE
    assert await ctem_access.refusal(db, DECIDER, write=True) is None
    assert await ctem_access.refusal(db, ADMIN, write=True) is None


async def test_the_strongest_group_right_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()  # the real reader, over a result holding both rights

    class _Rows:
        def scalars(self) -> Any:
            return SimpleNamespace(all=lambda: ["view", "decide"])

    class _Exec(_Db):
        async def execute(self, _statement: Any) -> Any:
            return _Rows()

    assert await ctem_access.group_right(_Exec(), DECIDER) == "decide"


async def test_an_unreadable_membership_is_no_right(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()

    class _Broken(_Db):
        async def execute(self, _statement: Any) -> Any:
            raise RuntimeError("down")

    assert await ctem_access.group_right(_Broken(), VIEWER) is None


# --- the signal API -------------------------------------------------------------------------


# Stable ids: the users are random per process, and pytest-xdist workers must collect the same names.
@pytest.mark.parametrize(
    ("user", "seen"),
    [(ADMIN, 3), (VIEWER, 3), (DECIDER, 3)],
    ids=["admin", "view", "decide"],
)
async def test_view_and_decide_read_the_system_signals(client: httpx.AsyncClient, user: str, seen: int) -> None:
    response = await client.get("/api/v1/ctem-signals", headers={"X-User": user})
    assert response.status_code == 200
    assert response.json()["total"] == seen


async def test_without_a_right_every_signal_route_is_a_403(client: httpx.AsyncClient, store: _Store) -> None:
    own = store.rows[2]  # recorded by NOBODY: no own-signals fallback any more (#2441)
    for path in ("/api/v1/ctem-signals", f"/api/v1/ctem-signals/{own.id}"):
        refused = await client.get(path, headers={"X-User": NOBODY})
        assert refused.status_code == 403
        assert refused.json()["detail"] == ctem_access.NO_ACCESS_MESSAGE
    read = await client.get(f"/api/v1/ctem-signals/{store.rows[0].id}", headers={"X-User": VIEWER})
    assert read.status_code == 200


async def test_view_cannot_triage(client: httpx.AsyncClient, store: _Store) -> None:
    body = {"action": "dismiss", "ids": [str(store.rows[0].id)]}
    response = await client.post("/api/v1/ctem-signals/triage", json=body, headers={"X-User": VIEWER})
    assert response.status_code == 403
    assert store.triaged == []


async def test_decide_triages_the_system_signals(client: httpx.AsyncClient, store: _Store) -> None:
    body = {"action": "dismiss", "filter": {"state": "to_validate"}}
    response = await client.post("/api/v1/ctem-signals/triage", json=body, headers={"X-User": DECIDER})
    assert response.status_code == 200
    assert response.json()["matched"] == 3
    assert store.triaged and store.triaged[0][1] == "dismiss"


async def test_without_a_right_triage_is_a_403(client: httpx.AsyncClient, store: _Store) -> None:
    body = {"action": "dismiss", "filter": {"state": "to_validate"}}
    response = await client.post("/api/v1/ctem-signals/triage", json=body, headers={"X-User": NOBODY})
    assert response.status_code == 403
    assert store.triaged == []


# --- agent tool reach and event delivery ----------------------------------------------------


async def test_an_agent_of_a_view_member_lists_the_system_signals(
    store: _Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session() -> Any:
        yield _Db()

    async def _primary(_db: Any) -> Any:
        return SimpleNamespace(id=ORG)

    monkeypatch.setattr("app.core.database.async_session", _session)
    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    monkeypatch.setattr("app.api.v1.helpers.agent_tool_admin_bypass", lambda user: bool(user and user.is_admin))

    for user, total, applied in ((VIEWER, 3, "organization"),):
        _, executor = build_ctem_signal_tools(user_id=user)
        result = await executor.execute_tool(ToolCall(id="c", name="list_ctem_signals", arguments={}))
        body = json.loads(result.content)
        assert body["total"] == total
        assert body["scope"]["applied"] == applied
    _, executor = build_ctem_signal_tools(user_id=VIEWER)
    body = json.loads((await executor.execute_tool(ToolCall(id="c", name="list_ctem_signals", arguments={}))).content)
    assert body["scope"]["ctem_right"] == "view"


async def test_a_system_signal_event_reaches_a_member_with_a_right_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Scalar:
        def scalar_one_or_none(self) -> str:
            return "system"

    class _EventDb(_Db):
        async def execute(self, _statement: Any) -> Any:
            return _Scalar()

    sid = uuid.uuid4()
    db: Any = _EventDb()
    assert await signals.user_reads_signal(db, VIEWER, sid) is True
    assert await signals.user_reads_signal(db, DECIDER, sid) is True
    assert await signals.user_reads_signal(db, ADMIN, sid) is True
    assert await signals.user_reads_signal(db, NOBODY, sid) is False


async def test_an_own_signal_event_no_longer_reaches_a_person_without_a_right() -> None:
    class _Scalar:
        def scalar_one_or_none(self) -> str:
            return NOBODY

    class _EventDb(_Db):
        async def execute(self, _statement: Any) -> Any:
            return _Scalar()

    db: Any = _EventDb()
    assert await signals.user_reads_signal(db, NOBODY, uuid.uuid4()) is False


# --- tools and triggers offered only with a right (#2441) -------------------------------------


async def test_ctem_tools_are_wired_only_with_a_right(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session() -> Any:
        yield _Db()

    monkeypatch.setattr(type(settings), "is_copilot_like", property(lambda _s: False), raising=False)
    monkeypatch.setattr("app.core.database.async_session", _session)
    assert await ctem_access.wires_ctem(VIEWER) is True
    assert await ctem_access.wires_ctem(DECIDER) is True
    assert await ctem_access.wires_ctem(ADMIN) is True
    assert await ctem_access.wires_ctem(NOBODY) is False
    assert await ctem_access.wires_ctem(None) is False


async def test_ctem_events_are_offered_and_saved_only_with_a_right(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import platform_events

    names = {e["event"] for e in platform_events.catalog(ctem=False)}
    assert "ctem_signal.recorded" not in names and "ctem_case.opened" not in names
    assert "timer.due" in names
    assert "ctem_signal.recorded" in {e["event"] for e in platform_events.catalog()}

    async def _wires(user_id: Any) -> bool:
        return str(user_id) != NOBODY

    monkeypatch.setattr(ctem_access, "wires_ctem", _wires)
    config = {"event": "ctem_signal.recorded"}
    assert await platform_events.ctem_event_error("platform_event", config, NOBODY) is not None
    assert await platform_events.ctem_event_error("platform_event", config, VIEWER) is None
    assert await platform_events.ctem_event_error("platform_event", {"event": "timer.due"}, NOBODY) is None
    assert await platform_events.ctem_event_error("schedule", config, NOBODY) is None
