"""The CTEM case read is fast (#1986): one view build per viewer at a time, the products read at once.

- ``viewer_view`` builds a viewer's view once when the case page reads the case
  and the list together: the second request waits for the first build and
  reads its cached result (singleflight per user, vertical and generation).
- ``read_open_issues`` reads OpenCTI and OpenAEV concurrently, each in its own
  task, so the reasons and the read context of one never reach the other.
- The closed-member lookups of OpenAEV run concurrently, bounded.
- The case routes answer a ``Server-Timing`` header with the phases and the cache outcome.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.services import ctem_cases_source as source
from app.services import ctem_cases_view as view_of

NO_DB: Any = None
NO_CLIENT: Any = None


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None, nx: bool = False) -> bool:
        self.data[key] = str(value)
        return True

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)

    async def incr(self, key: str) -> int:
        self.data[key] = str(int(self.data.get(key) or 0) + 1)
        return int(self.data[key])


@pytest.fixture
def redis(monkeypatch: pytest.MonkeyPatch) -> FakeRedis:
    client = FakeRedis()

    async def _redis() -> FakeRedis:
        return client

    monkeypatch.setattr(view_of, "_redis", _redis)
    return client


@pytest.fixture
def slow_reads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def read_view(db: Any, user_id: str, vertical: str) -> view_of.ViewerView:
        calls.append(user_id)
        await asyncio.sleep(0.05)
        return view_of.ViewerView(open_ids={f"signal:{len(calls)}"})

    async def no_signals(db: Any, user_id: str) -> set[str]:
        return set()

    monkeypatch.setattr("app.services.ctem_cases_source.read_view", read_view)
    monkeypatch.setattr(view_of, "signal_members", no_signals)
    return calls


@pytest.mark.asyncio
async def test_concurrent_reads_of_one_viewer_share_one_build(redis: FakeRedis, slow_reads: list[str]) -> None:
    case, listing = await asyncio.gather(
        view_of.viewer_view(NO_DB, "u-1", "cyber"), view_of.viewer_view(NO_DB, "u-1", "cyber")
    )
    assert slow_reads == ["u-1"]
    assert case.open_ids == listing.open_ids
    # The lock of a finished build is dropped: a long-lived worker keeps no entry per viewer.
    assert view_of._build_locks == {} and view_of._build_lock_refs == {}


@pytest.mark.asyncio
async def test_two_viewers_build_their_own_view_at_once(redis: FakeRedis, slow_reads: list[str]) -> None:
    await asyncio.gather(view_of.viewer_view(NO_DB, "u-1", "cyber"), view_of.viewer_view(NO_DB, "u-2", "cyber"))
    assert sorted(slow_reads) == ["u-1", "u-2"]


@pytest.mark.asyncio
async def test_a_fresh_read_still_reads_the_source(redis: FakeRedis, slow_reads: list[str]) -> None:
    await view_of.viewer_view(NO_DB, "u-1", "cyber")
    await view_of.viewer_view(NO_DB, "u-1", "cyber", fresh=True)
    assert slow_reads == ["u-1", "u-1"]


@pytest.mark.asyncio
async def test_the_timings_name_the_phases_and_the_cache_outcome(redis: FakeRedis, slow_reads: list[str]) -> None:
    phases = view_of.start_timing()
    await view_of.viewer_view(NO_DB, "u-1", "cyber")
    assert {"cache-miss", "source", "view", "signals"} <= set(phases)
    phases = view_of.start_timing()
    await view_of.viewer_view(NO_DB, "u-1", "cyber")
    assert "cache-hit" in phases and "source" not in phases
    header = view_of.server_timing({"case": 12.345, "cache-hit": 0.0, "odd name": 1.0})
    assert header == "case;dur=12.3, cache-hit;dur=0.0, odd_name;dur=1.0"


@pytest.mark.asyncio
async def test_no_timing_started_records_nothing() -> None:
    view_of._timings.set(None)
    async with view_of.timed("anything"):
        pass
    view_of.mark("cache", "hit")
    assert view_of._timings.get() is None


STACK = [{"name": "Tomcat", "vendor": "Apache"}]


@pytest.mark.asyncio
async def test_the_products_are_read_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    both = asyncio.Event()

    async def stack(db: Any) -> list[dict[str, str]]:
        return STACK

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]]:
        return (f"https://{provider}", {})

    async def wait_for_the_other(name: str) -> None:
        started.append(name)
        if len(started) == 2:
            both.set()
        # Sequential reads would never see the other one start: the wait would time out.
        await asyncio.wait_for(both.wait(), timeout=2)

    async def read_opencti(client: Any, base_url: str, headers: Any, stack: Any) -> source.OpenCtiRead | None:
        await wait_for_the_other("opencti")
        source._failed("AUTH_REQUIRED 401")
        return None

    async def read_openaev(client: Any, base_url: str, headers: Any, now: Any) -> list[Any]:
        await wait_for_the_other("openaev")
        return []

    monkeypatch.setattr(source, "_organization_software", stack)
    monkeypatch.setattr(source, "_user_creds", creds)
    monkeypatch.setattr(source, "_read_opencti", read_opencti)
    monkeypatch.setattr(source, "_read_openaev", read_openaev)
    monkeypatch.setattr(source, "attach_probe", lambda headers, base_url: headers)
    read = await source.read_open_issues(NO_DB, "u-1", "cyber")
    assert sorted(started) == ["openaev", "opencti"]
    # The OpenCTI error stays with OpenCTI: OpenAEV answered.
    assert read.missing == ["opencti"] and read.reasons == {"opencti": "rejected"}


class _Response:
    def __init__(self, fid: str) -> None:
        self.status_code = 200
        self._fid = fid

    def json(self) -> dict[str, Any]:
        return {"finding_id": self._fid, "finding_value": f"CVE-2026-{self._fid}", "finding_type": "cve"}


@pytest.mark.asyncio
async def test_openaev_lookups_by_id_run_concurrently_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    in_flight = 0
    peak = 0
    asked: list[str] = []

    async def api(base_url: str, headers: Any, method: str, path: str, what: str, **kw: Any) -> tuple[Any, Any]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        fid = path.rsplit("/", 1)[-1]
        asked.append(fid)
        if fid == "gone":
            return None, "404 Not Found"
        return _Response(fid), None

    def names(found: list[dict[str, Any]]) -> list[Any]:
        return [f["finding_id"] for f in found]

    monkeypatch.setattr(source, "_api", api)
    monkeypatch.setattr(source, "openaev_issues", names)
    ids = [f"f{i}" for i in range(20)] + ["gone"]
    found: Any = await source._read_openaev_by_id(NO_CLIENT, "https://openaev", {}, ids)
    assert sorted(asked) == sorted(ids)
    assert 1 < peak <= source.AEV_LOOKUP_CONCURRENCY
    # A 404 is not visible, never an outage; the order of the answers is the order asked.
    assert found == [f"f{i}" for i in range(20)]


@pytest.mark.asyncio
async def test_an_openaev_outage_during_the_lookups_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def api(base_url: str, headers: Any, method: str, path: str, what: str, **kw: Any) -> tuple[Any, Any]:
        return None, "503 Service Unavailable"

    monkeypatch.setattr(source, "_api", api)
    assert await source._read_openaev_by_id(NO_CLIENT, "https://openaev", {}, ["f1", "f2"]) is None
