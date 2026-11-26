"""Agents read the notification bell (#2198) and the Platform Health page (#2199).

``list_my_notifications`` answers ``build_feed`` for the person the run acts for,
never another person's; ``get_platform_health`` answers the page's own reads for
the person, so its role scope is the page's.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.services import notification_feed_tool as bell
from app.services.builtin_tools.health_orchestrator import TOOL_NAMES, build_health_orchestrator_tools


class _Db:
    def __init__(self, users):
        self.users = users

    async def get(self, _model, key):
        return self.users.get(key)


def _session(db):
    @asynccontextmanager
    async def _open():
        yield db

    return _open


def test_the_bell_tool_is_a_coordination_function():
    from app.services.builtin_tools.work_items import build_work_item_tools

    definitions, executor = build_work_item_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))
    assert bell.TOOL_NAME in {d.name for d in definitions}
    assert bell.TOOL_NAME in executor.tool_names


@pytest.mark.asyncio
async def test_the_bell_answers_the_acting_person_feed_and_nobody_else(monkeypatch):
    me = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    other = SimpleNamespace(id=uuid.uuid4(), is_active=True)
    seen: list = []

    async def _feed(_db, user, *, limit, offset=0, decisions_only=False):
        seen.append((user.id, limit, offset, decisions_only))
        return {
            "items": [{"id": "work_item:x", "kind": "decision", "title": "Answer it", "unread": True}],
            "unread_count": 1,
            "waiting_decision_total": 551,
            "unread_total": 552,
            "next_offset": 50,
        }

    import app.core.database as database
    import app.services.notification_feed as feed

    monkeypatch.setattr(database, "async_session", _session(_Db({me.id: me, other.id: other})))
    monkeypatch.setattr(feed, "build_feed", _feed)
    answer = await bell.build_list_my_notifications(str(me.id))(limit=500)
    assert seen == [(me.id, bell.MAX_LIMIT, 0, False)]
    # #2453: the totals are the whole bell's, the next page is named.
    assert "Answer it" in answer and "551 decision(s) waiting in all" in answer
    assert "552 unread in all" in answer and "offset=50" in answer
    await bell.build_list_my_notifications(str(me.id))(limit=50, offset=50, decisions_only=True)
    assert seen[-1] == (me.id, 50, 50, True)


@pytest.mark.asyncio
async def test_the_bell_of_nobody_is_not_read(monkeypatch):
    import app.services.notification_feed as feed

    async def _feed(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("read for nobody")

    monkeypatch.setattr(feed, "build_feed", _feed)
    assert "acts for nobody" in await bell.build_list_my_notifications(None)()


def test_the_health_group_offers_the_page_read_in_assignment_runs_too():
    assert "get_platform_health" in TOOL_NAMES
    definitions, executor = build_health_orchestrator_tools("", allow_requests=False)
    assert "get_platform_health" in {d.name for d in definitions}
    assert "get_platform_health" in executor.tool_names


@pytest.mark.asyncio
@pytest.mark.parametrize("is_admin", [True, False])
async def test_the_page_read_is_the_page_for_that_person(monkeypatch, is_admin):
    user = SimpleNamespace(id=uuid.uuid4(), is_active=True, is_admin=is_admin)
    calls: list = []

    class _Model(SimpleNamespace):
        def model_dump(self, mode="json"):
            return dict(vars(self))

    async def _overview(*, user, db):
        calls.append(("overview", user.id))
        return SimpleNamespace(
            scope="all" if user.is_admin else "managed", overall=90, modules=[_Model(module="xtm_one", score=90)]
        )

    async def _findings(**kwargs):
        calls.append(("findings", kwargs["user"].id, kwargs["module"]))
        return SimpleNamespace(total=1, items=[_Model(id="f1", module="xtm_one", severity="warning")])

    async def _settings(_db):
        return SimpleNamespace(
            alert_emails=SimpleNamespace(
                enabled=True,
                recipients=["a@example.com"],
                min_severity="warning",
                immediate=True,
                digest=True,
                alert_after_minutes=15,
            )
        )

    import app.api.v1.platform_health_findings as page
    import app.core.database as database
    import app.services.health.settings as settings

    monkeypatch.setattr(database, "async_session", _session(_Db({user.id: user})))
    monkeypatch.setattr(page, "health_overview", _overview)
    monkeypatch.setattr(page, "list_health_findings", _findings)
    monkeypatch.setattr(settings, "load_health_settings", _settings)
    _defs, executor = build_health_orchestrator_tools(str(user.id))
    answer = await executor._handlers["get_platform_health"](module="xtm_one")
    assert calls == [("overview", user.id), ("findings", user.id, ["xtm_one"])]
    assert ('"scope": "all"' in answer) is is_admin
    assert ("alert_emails" in answer) is is_admin
    assert "a@example.com" not in answer
