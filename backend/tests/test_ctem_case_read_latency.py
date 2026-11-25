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

from app.services import ctem_cases as rules
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
    # A 404 is gone (OpenAEV #9233 re-keyed the findings), never an outage; the order is the order asked.
    assert found == ([f"f{i}" for i in range(20)], ["gone"])


@pytest.mark.asyncio
async def test_an_openaev_outage_during_the_lookups_answers_none(monkeypatch: pytest.MonkeyPatch) -> None:
    async def api(base_url: str, headers: Any, method: str, path: str, what: str, **kw: Any) -> tuple[Any, Any]:
        return None, "503 Service Unavailable"

    monkeypatch.setattr(source, "_api", api)
    assert await source._read_openaev_by_id(NO_CLIENT, "https://openaev", {}, ["f1", "f2"]) is None


def _octi_issue(vid: str) -> Any:
    return rules.SourceIssue(source="opencti", external_id=vid, name=vid)


@pytest.fixture
def case_source(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """The source of a case read: OpenCTI holds open v1, closed v2 readable; OpenAEV reads f1 only."""
    asked: dict[str, Any] = {"scoped": [], "by_id": {}, "full": 0}

    async def stack(db: Any) -> list[dict[str, str]]:
        return STACK

    async def creds(db: Any, user_id: str, provider: str) -> tuple[str, dict[str, str]] | None:
        return (f"https://{provider}", {}) if provider in ("opencti", "openaev") else None

    async def scoped(client: Any, base_url: str, headers: Any, stack: Any, vuln_ids: list[str]) -> Any:
        asked["scoped"].append(vuln_ids)
        return source.OpenCtiRead([_octi_issue(v) for v in vuln_ids if v == "v1"])

    async def full(*a: Any, **kw: Any) -> Any:
        asked["full"] += 1
        raise AssertionError("a case read never reads the whole stack")

    async def lookup(src: str, cred: Any, ids: list[str]) -> list[Any]:
        asked["by_id"][src] = sorted(ids)
        if src == "opencti":
            return [_octi_issue(i) for i in ids if i == "v2"]
        return [rules.SourceIssue(source="openaev", external_id=i, name=i) for i in ids if i == "f1"]

    async def no_access(db: Any, user_id: str) -> bool:
        return False

    monkeypatch.setattr(source, "_organization_software", stack)
    monkeypatch.setattr(source, "_user_creds", creds)
    monkeypatch.setattr(source, "_read_opencti_scoped", scoped)
    monkeypatch.setattr(source, "_read_opencti", full)
    monkeypatch.setattr(source, "_read_openaev", full)
    monkeypatch.setattr(source, "_lookup_by_id", lookup)
    monkeypatch.setattr(source, "_reads_opencrq", no_access)
    monkeypatch.setattr(source, "attach_probe", lambda headers, base_url: headers)
    return asked


@pytest.mark.asyncio
async def test_a_case_view_reads_only_the_cases_members(case_source: dict[str, Any]) -> None:
    members = ["opencti:v1", "opencti:v2", "opencti:v3", "openaev:f1", "openaev:f9"]
    view = await source.read_case_view(NO_DB, "u-1", "cyber", members)
    assert case_source["full"] == 0 and case_source["scoped"] == [["v1", "v2", "v3"]]
    # What is not open is looked up by id: v2 closed and readable, v3 not readable, f9 not readable.
    assert case_source["by_id"] == {"opencti": ["v2", "v3"], "openaev": ["f1", "f9"]}
    assert view.open_ids == {"opencti:v1"}
    assert set(view.issues) == {"opencti:v1", "opencti:v2", "openaev:f1"}
    assert not view.sees("opencti:v3") and not view.sees("openaev:f9") and view.missing == []


@pytest.mark.asyncio
async def test_a_case_view_names_a_product_that_fails(
    case_source: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def down(client: Any, base_url: str, headers: Any, stack: Any, vuln_ids: list[str]) -> Any:
        source._failed("ReadTimeout: timed out")
        return None

    monkeypatch.setattr(source, "_read_opencti_scoped", down)
    view = await source.read_case_view(NO_DB, "u-1", "cyber", ["opencti:v1", "openaev:f1"])
    assert view.missing == ["opencti"] and view.reasons == {"opencti": "timeout"}
    assert set(view.issues) == {"openaev:f1"}  # the other product still answers


@pytest.mark.asyncio
async def test_a_case_view_of_another_vertical_reads_no_product(case_source: dict[str, Any]) -> None:
    view = await source.read_case_view(NO_DB, "u-1", "fimi", ["opencti:v1"])
    assert case_source["scoped"] == [] and case_source["by_id"] == {} and view.issues == {}


@pytest.mark.asyncio
async def test_the_scoped_opencti_read_filters_the_relations_by_the_cases_vulnerabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, dict[str, Any]]] = []

    async def graphql(client: Any, base_url: str, headers: Any, query: str, variables: dict[str, Any]) -> Any:
        sent.append((query, variables))
        if "CtemCaseStackSoftware" in query:
            node = {"id": "sw1", "name": "Tomcat", "vendor": "Apache", "version": None, "cpe": None}
            return {"stixCyberObservables": {"edges": [{"node": node}], "pageInfo": {}}}, None
        if "CtemCaseScopedVulns" in query:
            rel = {"from": {"id": "sw1"}, "to": {"id": "v1", "name": "CVE-2026-1"}}
            return {"stixCoreRelationships": {"edges": [{"node": rel}], "pageInfo": {}}}, None
        return {"stixCoreRelationships": {"edges": []}}, None

    async def software(client: Any, base_url: str, headers: Any, query: str, stack: Any, **kw: Any) -> Any:
        return [{"id": "sw1", "name": "Tomcat", "vendor": "Apache"}], True, None

    monkeypatch.setattr(source, "_graphql", graphql)
    monkeypatch.setattr(source, "_read_stack_software", software)
    monkeypatch.setattr(source, "_stack_matches", lambda node, stack: "Tomcat")
    monkeypatch.setattr(
        source, "opencti_issues", lambda sw, st, rels, rem, fix: [_octi_issue(r["to"]["id"]) for r in rels]
    )
    found = await source._read_opencti_scoped(NO_CLIENT, "https://opencti", {}, STACK, ["v1", "v2"])
    assert found is not None and [i.external_id for i in found.issues] == ["v1"]
    scoped = [v for q, v in sent if "CtemCaseScopedVulns" in q]
    assert scoped and scoped[0]["to"] == ["v1", "v2"] and scoped[0]["from"] == ["sw1"]
    assert not any("CtemCaseStackVulns" in q for q, _ in sent)  # never the whole stack's vulnerabilities
