"""The per-viewer CTEM case view cache (#915): a case opened for everyone drops every viewer's cached view.

``forget_view`` drops the caller's own view after their recompute;
``forget_all_views`` retires every viewer's view of a vertical (a case opened
from a signal, a signal joining a case), so the case is on the board and its
page at once instead of after ``VIEW_TTL_SECONDS``.
"""

from __future__ import annotations

import pytest

from app.services import ctem_cases_view as view_of


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return False
        self.data[key] = str(value)
        return True

    async def delete(self, key):
        self.data.pop(key, None)

    async def incr(self, key):
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        return int(self.data[key])


@pytest.fixture
def redis(monkeypatch):
    client = FakeRedis()

    async def _redis():
        return client

    monkeypatch.setattr(view_of, "_redis", _redis)
    return client


@pytest.fixture
def reads(monkeypatch):
    calls: list[str] = []

    async def read_view(db, user_id, vertical):
        calls.append(user_id)
        return view_of.ViewerView(open_ids={f"signal:{len(calls)}"})

    async def signal_members(db, user_id):
        return set()

    monkeypatch.setattr("app.services.ctem_cases_source.read_view", read_view)
    # The signal members are read live from the database on every call, cached or not:
    # out of this test's scope, which is the cached view.
    monkeypatch.setattr(view_of, "signal_members", signal_members)
    return calls


@pytest.mark.asyncio
async def test_the_view_is_cached_per_viewer(redis, reads):
    first = await view_of.viewer_view(None, "u-1", "cyber")
    again = await view_of.viewer_view(None, "u-1", "cyber")
    assert reads == ["u-1"] and again.open_ids == first.open_ids


@pytest.mark.asyncio
async def test_forget_all_views_makes_every_viewer_read_again(redis, reads):
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.viewer_view(None, "u-2", "cyber")
    await view_of.viewer_view(None, "u-3", "exposure")
    await view_of.forget_all_views("cyber")
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.viewer_view(None, "u-2", "cyber")
    await view_of.viewer_view(None, "u-3", "exposure")  # another vertical keeps its view
    assert reads == ["u-1", "u-2", "u-3", "u-1", "u-2"]


@pytest.mark.asyncio
async def test_forget_view_still_drops_the_callers_view_after_a_bump(redis, reads):
    await view_of.forget_all_views("cyber")
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_view("u-1", "cyber")
    await view_of.viewer_view(None, "u-1", "cyber")
    assert reads == ["u-1", "u-1"]


@pytest.mark.asyncio
async def test_no_redis_reads_the_source_every_time(monkeypatch, reads):
    async def _none():
        return None

    monkeypatch.setattr(view_of, "_redis", _none)
    await view_of.forget_all_views("cyber")  # nothing to drop, no error
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.viewer_view(None, "u-1", "cyber")
    assert reads == ["u-1", "u-1"]
