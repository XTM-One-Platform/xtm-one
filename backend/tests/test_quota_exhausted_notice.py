"""One notification a day when an agent crosses its agentic quota.

``notify_quota_exhausted_once`` is called on every quota refusal (a delivery
dropped before any run, a queued run skipped at dequeue); a Redis ``SET NX`` per
agent and UTC day lets the first one through and sends one bell entry to the
agent's owner and to every active administrator, naming the assignment.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core import database
from app.core import redis as redis_module
from app.services import notify
from app.services import quota_exhausted_notice as notice

pytestmark = pytest.mark.asyncio


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, tuple[str, int | None]] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = (value, ex)
        return True


class _FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def world(monkeypatch):
    fake_redis = _FakeRedis()
    session = _FakeSession()
    state: dict = {"redis": fake_redis, "session": session, "sent": [], "admins": []}

    async def _client():
        if state.get("redis_down"):
            raise ConnectionError("redis down")
        return fake_redis

    @asynccontextmanager
    async def _session():
        yield session

    async def _admins(_db):
        return list(state["admins"])

    async def _send(db, user_id, *, title, summary=None, source_label=None, extra_data=None, is_company=False):
        state["sent"].append({"user_id": user_id, "title": title, "summary": summary, "extra": extra_data})

    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(database, "async_session", _session)
    monkeypatch.setattr(notice, "_admin_ids", _admins)
    monkeypatch.setattr(notify, "notify_web_user", _send)
    return state


def _pair():
    owner = uuid.uuid4()
    agent = SimpleNamespace(id=uuid.uuid4(), name="Triage", user_id=owner)
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Inbound webhooks")
    return assignment, agent, owner


NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)


async def test_the_first_refusal_of_the_day_notifies_the_owner_and_the_admins(world):
    assignment, agent, owner = _pair()
    admin = str(uuid.uuid4())
    world["admins"] = [admin, str(owner)]

    sent = await notice.notify_quota_exhausted_once(assignment, agent, "Agentic quota exceeded", where="delivery", now=NOW)

    assert sent is True
    # The owner first, the administrators after, each once (the owner is also an admin here).
    assert [item["user_id"] for item in world["sent"]] == [str(owner), admin]
    first = world["sent"][0]
    assert "Inbound webhooks" in first["title"] and "Triage" in first["title"]
    assert first["extra"]["notification_source"] == notice.NOTIFICATION_SOURCE
    assert first["extra"]["assignment_id"] == str(assignment.id)
    assert first["extra"]["where"] == "delivery"
    assert world["session"].commits == 1
    key = notice.notice_key(agent.id, NOW)
    assert key.endswith(":2026-11-03")
    assert world["redis"].keys[key][1] == notice.NOTICE_TTL_SECONDS


async def test_later_refusals_the_same_day_send_nothing(world):
    assignment, agent, _owner = _pair()
    other_assignment = SimpleNamespace(id=uuid.uuid4(), name="Integration events")

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="delivery", now=NOW) is True
    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="dequeue", now=NOW) is False
    assert await notice.notify_quota_exhausted_once(other_assignment, agent, "spent", where="delivery", now=NOW) is False

    assert len(world["sent"]) == 1


async def test_the_next_day_notifies_again(world):
    assignment, agent, _owner = _pair()
    tomorrow = datetime(2026, 11, 4, 0, 5, tzinfo=UTC)

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="dequeue", now=NOW) is True
    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="dequeue", now=tomorrow) is True

    assert len(world["sent"]) == 2


async def test_each_agent_has_its_own_notice(world):
    assignment, agent, _owner = _pair()
    _other_assignment, other_agent, _other_owner = _pair()

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="delivery", now=NOW) is True
    assert await notice.notify_quota_exhausted_once(assignment, other_agent, "spent", where="delivery", now=NOW) is True

    assert len(world["sent"]) == 2


async def test_a_redis_that_cannot_answer_sends_nothing(world):
    """A refusal storm must never become a notification storm."""
    world["redis_down"] = True
    assignment, agent, _owner = _pair()

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="delivery", now=NOW) is False
    assert world["sent"] == []


async def test_a_failing_delivery_never_raises(world, monkeypatch):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(notice, "_admin_ids", _boom)
    assignment, agent, _owner = _pair()

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="dequeue", now=NOW) is False


async def test_an_agent_without_an_owner_notifies_the_admins_only(world):
    assignment, agent, _owner = _pair()
    agent.user_id = None
    admin = str(uuid.uuid4())
    world["admins"] = [admin]

    assert await notice.notify_quota_exhausted_once(assignment, agent, "spent", where="delivery", now=NOW) is True
    assert [item["user_id"] for item in world["sent"]] == [admin]
