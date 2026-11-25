"""The per-viewer CTEM case view cache (#915): a case opened for everyone drops every viewer's cached view.

``forget_view`` drops the caller's own view after their recompute;
``forget_all_views`` retires every viewer's view of a vertical (a case opened
from a signal, a signal joining a case), so the case is on the board and its
page at once instead of after ``VIEW_TTL_SECONDS``.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import ctem_cases as rules
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


@pytest.fixture
def revalidations(monkeypatch):
    started: list[tuple[str, str, str]] = []
    monkeypatch.setattr(view_of, "_revalidate", lambda u, v, g: started.append((u, v, g)))
    return started


@pytest.fixture
def judged(monkeypatch):
    """A source whose view judged ``opencti:a`` (readable) and ``openaev:b`` (product down)."""
    calls: list[str] = []

    async def read_view(db, user_id, vertical):
        calls.append(user_id)
        issue = rules.SourceIssue(source="opencti", external_id="a", name="CVE-1")
        return view_of.ViewerView(issues={"opencti:a": issue}, unavailable={"openaev:b"})

    async def signal_members(db, user_id):
        return set()

    async def source_signal_members(db, seen):
        return set()

    async def read_case_view(db: Any, user_id: str, vertical: str, members: list[str]) -> view_of.ViewerView:
        calls.append(f"case:{user_id}")
        issues = {m: rules.SourceIssue(source="opencti", external_id=m.split(":", 1)[1], name=m) for m in members}
        return view_of.ViewerView(issues={m: i for m, i in issues.items() if m.startswith("opencti:")})

    monkeypatch.setattr("app.services.ctem_cases_source.read_view", read_view)
    monkeypatch.setattr("app.services.ctem_cases_source.read_case_view", read_case_view)
    monkeypatch.setattr(view_of, "signal_members", signal_members)
    monkeypatch.setattr(view_of, "source_signal_members", source_signal_members)
    return calls


@pytest.mark.asyncio
async def test_a_case_read_after_a_bump_answers_from_the_older_view_and_rebuilds_in_background(
    redis, judged, revalidations
):
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_all_views("cyber")
    phases = view_of.start_timing()
    view = await view_of.viewer_view(None, "u-1", "cyber", members=["opencti:a", "openaev:b", "signal:s"])
    assert judged == ["u-1"]  # no cold source read on the request
    assert view.sees("opencti:a") and view.sees("openaev:b")
    assert "cache-stale" in phases
    assert revalidations == [("u-1", "cyber", "1")]


@pytest.mark.asyncio
async def test_a_member_the_older_view_never_judged_is_read_for_this_case_only(
    redis: Any, judged: list[str], revalidations: list[Any]
) -> None:
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_all_views("cyber")
    phases = view_of.start_timing()
    view = await view_of.viewer_view(None, "u-1", "cyber", members=["opencti:a", "opencti:new"])
    # The case's own members are read with the viewer's credentials; the whole view builds in the background.
    assert judged == ["u-1", "case:u-1"] and revalidations == [("u-1", "cyber", "1")]
    assert view.sees("opencti:new") and "cache-scoped" in phases


@pytest.mark.asyncio
async def test_a_read_without_members_never_takes_the_older_view(redis, judged, revalidations):
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_all_views("cyber")
    await view_of.viewer_view(None, "u-1", "cyber")  # a change, the list: the current generation
    assert judged == ["u-1", "u-1"] and revalidations == []


@pytest.mark.asyncio
async def test_another_viewers_older_view_is_never_served(redis, judged, revalidations):
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_all_views("cyber")
    await view_of.viewer_view(None, "u-2", "cyber", members=["opencti:a"])
    # u-2 has no view of its own: its own read of the case, never u-1's view.
    assert judged == ["u-1", "case:u-2"] and revalidations == [("u-2", "cyber", "1")]


@pytest.mark.asyncio
async def test_a_case_read_with_no_view_reads_the_case_and_caches_nothing(
    redis: Any, judged: list[str], revalidations: list[Any]
) -> None:
    view = await view_of.viewer_view(None, "u-1", "cyber", members=["opencti:a", "openaev:hidden"])
    assert judged == ["case:u-1"] and revalidations == [("u-1", "cyber", "0")]
    # What the viewer's own read did not return stays Restricted.
    assert view.sees("opencti:a") and not view.sees("openaev:hidden")
    assert not any(k.startswith("ctem:cases:view:") for k in redis.data)


@pytest.mark.asyncio
async def test_a_scoped_read_keeps_the_seen_ids_of_a_product_that_does_not_answer(
    redis: Any, monkeypatch: pytest.MonkeyPatch, revalidations: list[Any]
) -> None:
    async def read_case_view(db: Any, user_id: str, vertical: str, members: list[str]) -> view_of.ViewerView:
        return view_of.ViewerView(missing=["openaev"], reasons={"openaev": "timeout"})

    async def none(db: Any, *a: Any) -> set[str]:
        return set()

    monkeypatch.setattr("app.services.ctem_cases_source.read_case_view", read_case_view)
    monkeypatch.setattr(view_of, "signal_members", none)
    monkeypatch.setattr(view_of, "source_signal_members", none)
    redis.data[view_of._seen_key("u-1", "cyber")] = '["openaev:b", "openaev:other-case"]'
    view = await view_of.viewer_view(None, "u-1", "cyber", members=["openaev:b"])
    assert view.unavailable == {"openaev:b"} and view.missing == ["openaev"]


@pytest.mark.asyncio
async def test_a_case_read_waits_for_a_build_in_flight_instead_of_a_scoped_read(
    redis: Any, judged: list[str], revalidations: list[Any]
) -> None:
    key = view_of._view_key("u-1", "cyber", "0")
    async with view_of._singleflight(key):
        task = asyncio.create_task(view_of.viewer_view(None, "u-1", "cyber", members=["opencti:a"]))
        await asyncio.sleep(0)
        assert not task.done()
    await task
    assert judged == ["u-1"] and revalidations == []


@pytest.mark.asyncio
async def test_a_case_read_never_waits_for_a_background_build(
    redis: Any, judged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key = view_of._view_key("u-1", "cyber", "0")
    monkeypatch.setitem(view_of._revalidations, key, None)
    async with view_of._singleflight(key):
        view = await view_of.viewer_view(None, "u-1", "cyber", members=["opencti:a"])
    assert judged == ["case:u-1"] and view.sees("opencti:a")


@pytest.mark.asyncio
async def test_a_list_read_after_a_bump_answers_the_older_view_and_rebuilds_in_background(
    redis: Any, judged: list[str], revalidations: list[Any]
) -> None:
    """#2092: a list read never builds the whole view on the request after a drain bumped the generation."""
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.forget_all_views("cyber")
    phases = view_of.start_timing()
    view = await view_of.viewer_view(None, "u-1", "cyber", stale_ok=True)
    assert judged == ["u-1"] and view.sees("opencti:a")
    assert "cache-stale" in phases and revalidations == [("u-1", "cyber", "1")]


@pytest.mark.asyncio
async def test_a_list_read_with_no_view_waits_briefly_then_says_it_is_rebuilding(
    redis: Any, judged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = asyncio.Event()

    async def slow() -> None:
        await gate.wait()

    tasks: list[asyncio.Task[None]] = []

    def revalidate(user_id: str, vertical: str, generation: str) -> asyncio.Task[None]:
        tasks.append(asyncio.create_task(slow()))
        return tasks[-1]

    monkeypatch.setattr(view_of, "_revalidate", revalidate)
    monkeypatch.setattr(view_of, "LIST_BUILD_WAIT_SECONDS", 0.01)
    with pytest.raises(view_of.ViewRebuildingError) as raised:
        await view_of.viewer_view(None, "u-1", "cyber", stale_ok=True)
    assert judged == [] and raised.value.retry_after == view_of.LIST_RETRY_AFTER_SECONDS
    # The background build is never cancelled by the request that gave up waiting.
    assert not tasks[0].cancelled()
    gate.set()
    await tasks[0]


@pytest.mark.asyncio
async def test_a_list_read_with_no_view_answers_the_build_that_finished_in_the_wait(
    redis: Any, judged: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    def revalidate(user_id: str, vertical: str, generation: str) -> asyncio.Task[Any]:
        client = redis
        return asyncio.create_task(view_of._build_view(None, client, user_id, vertical, generation))

    monkeypatch.setattr(view_of, "_revalidate", revalidate)
    phases = view_of.start_timing()
    view = await view_of.viewer_view(None, "u-1", "cyber", stale_ok=True)
    assert judged == ["u-1"] and view.sees("opencti:a") and "cache-built" in phases


@pytest.mark.asyncio
async def test_no_redis_reads_the_source_every_time(monkeypatch, reads):
    async def _none():
        return None

    monkeypatch.setattr(view_of, "_redis", _none)
    await view_of.forget_all_views("cyber")  # nothing to drop, no error
    await view_of.viewer_view(None, "u-1", "cyber")
    await view_of.viewer_view(None, "u-1", "cyber")
    assert reads == ["u-1", "u-1"]
