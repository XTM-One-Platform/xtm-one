"""Regression tests for the CTEM collector parallelization (issue: prod pool
exhaustion + slow dashboard after the CTEM rollout).

A live posture build used to run ~13 OpenCTI + ~12 OpenAEV round-trips strictly
one-after-another, and the two providers sequentially - the dominant cost of the
~20-30s build. These cover the concurrency machinery that fixed that:

* ``_gather_bounded`` - fan independent queries out concurrently, capped, in order.
* ``build_posture_for_user`` - OpenCTI and OpenAEV collection run CONCURRENTLY,
  with creds resolved up front (DB-bound) and facts merged across providers.

No DB / network: ``_user_creds`` and the per-provider collectors are monkeypatched.
"""

from __future__ import annotations

import asyncio

from app.services import ctem_metrics_collector as collector


async def test_gather_bounded_caps_concurrency_and_preserves_order():
    """``_gather_bounded`` never runs more than *limit* coroutines at once and
    returns results in the original argument order (not completion order)."""
    active = 0
    peak = 0

    async def task(i: int) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        # Later items sleep less, so completion order != argument order unless
        # ordering is preserved explicitly.
        await asyncio.sleep(0.02 * (1 + (9 - i) / 10))
        active -= 1
        return i

    results = await collector._gather_bounded(3, *[task(i) for i in range(9)])

    assert results == list(range(9)), "results must preserve input order"
    assert peak <= 3, "must not exceed the concurrency limit"
    assert peak > 1, "should actually have run concurrently"


async def test_build_posture_runs_providers_concurrently(monkeypatch):
    """OpenCTI and OpenAEV collection overlap (run concurrently) and their
    per-stage facts merge into one posture; creds are resolved before collection."""
    creds_calls: list[str] = []

    async def fake_creds(db, user_id, provider):
        creds_calls.append(provider)
        return ("http://fake", {"Authorization": "x"})

    monkeypatch.setattr(collector, "_user_creds", fake_creds)

    active = 0
    peak = 0

    async def fake_octi(base_url, headers, user_id, client):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # simulate the slow OpenCTI collection
        active -= 1
        return {"scoping": {"octi": 1}, "discovery": {"d": 1}}

    async def fake_aev(base_url, headers, user_id, client):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.05)  # simulate the slow OpenAEV collection
        active -= 1
        return {"scoping": {"aev": 2}, "validation": {"v": 3}}

    monkeypatch.setattr(
        collector,
        "_VERTICAL_COLLECTORS",
        {"cyber": [("opencti", fake_octi), ("openaev", fake_aev)]},
    )

    stage_facts, present = await collector.build_posture_for_user(None, "u1", "cyber")

    assert peak == 2, "the two providers must run concurrently, not sequentially"
    assert present == {"opencti", "openaev"}
    assert set(creds_calls) == {"opencti", "openaev"}
    # Facts from both providers merge per stage (OpenAEV's scoping facts update
    # OpenCTI's), and provider-unique stages are preserved.
    assert stage_facts["scoping"] == {"octi": 1, "aev": 2}
    assert stage_facts["discovery"] == {"d": 1}
    assert stage_facts["validation"] == {"v": 3}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


async def test_aev_domain_coverage_resolves_domain_ids_to_names(monkeypatch):
    """``domain-counts`` groups by the Domain *id* (a random per-instance UUID),
    so coverage must resolve id -> name via ``/api/domains`` and count the core
    domains BY NAME. The previous code matched the UUID directly against the
    display-name set, so it always returned 0 (the regression this guards)."""
    domains_payload = [
        {"domain_id": "uuid-endpoint", "domain_name": "Endpoint"},
        {"domain_id": "uuid-network", "domain_name": "Network"},
        {"domain_id": "uuid-cloud", "domain_name": "Cloud"},
        {"domain_id": "uuid-tabletop", "domain_name": "Tabletop"},
    ]
    counts_payload = [
        {"domain": "uuid-endpoint", "count": 120},
        {"domain": "uuid-network", "count": 8},
        {"domain": "uuid-cloud", "count": 0},  # present but empty -> not covered
        {"domain": "uuid-tabletop", "count": 50},  # non-core domain -> excluded
    ]

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/domains":
            return _FakeResp(domains_payload), None
        if path == "/api/injector_contracts/domain-counts":
            return _FakeResp(counts_payload), None
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(collector, "_api", fake_api)

    covered, total, _breakdown = await collector._aev_domain_coverage("http://fake", {}, None)
    assert total == 7
    # Endpoint + Network have content; Cloud is 0 and Tabletop is non-core.
    assert covered == 2


async def test_aev_domain_coverage_unknown_when_ids_unresolvable(monkeypatch):
    """If ``/api/domains`` is unavailable and the counts are unresolvable UUIDs,
    coverage degrades to ``None`` (sample placeholder) rather than a misleading 0."""
    counts_payload = [{"domain": "uuid-endpoint", "count": 120}]

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/domains":
            return None, "domains unavailable"
        if path == "/api/injector_contracts/domain-counts":
            return _FakeResp(counts_payload), None
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(collector, "_api", fake_api)

    covered, total, _breakdown = await collector._aev_domain_coverage("http://fake", {}, None)
    assert total == 7
    assert covered is None


async def test_aev_domain_coverage_tolerates_name_keyed_rows(monkeypatch):
    """Backward compatible: a build whose ``domain-counts`` returns display names
    directly (no id map) is still counted by name."""

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/domains":
            return None, "no domains endpoint"
        if path == "/api/injector_contracts/domain-counts":
            return _FakeResp([
                {"domain": "Endpoint", "count": 10},
                {"domain": "Web App", "count": 3},
            ]), None
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(collector, "_api", fake_api)

    covered, total, _breakdown = await collector._aev_domain_coverage("http://fake", {}, None)
    assert total == 7
    assert covered == 2


async def test_finished_exercises_windowed_falls_back_on_filter_error(monkeypatch):
    """A windowed finished-exercises query that ERRORS (e.g. an older OpenAEV
    rejecting the date filter) retries WITHOUT the window so validation still gets
    data rather than nothing."""
    seen_date_filter: list[bool] = []

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        fg = (json or {}).get("filterGroup") or {}
        has_date = any(f.get("key") == "exercise_start_date" for f in fg.get("filters", []))
        seen_date_filter.append(has_date)
        if has_date:
            return None, "date filter unsupported"
        return _FakeResp({"content": [{"exercise_id": "e1"}], "totalElements": 1}), None

    monkeypatch.setattr(collector, "_api", fake_api)

    sims, total = await collector._aev_finished_exercises(
        "http://fake", {}, None, since_iso="2026-03-01T00:00:00.000Z"
    )
    assert seen_date_filter == [True, False]  # windowed first, then unwindowed retry
    assert total == 1
    assert sims == [{"exercise_id": "e1"}]


async def test_finished_exercises_windowed_empty_is_kept(monkeypatch):
    """A successful but EMPTY windowed query is a real 'no sims in the window' and
    must NOT trigger the unwindowed fallback."""
    calls: list[bool] = []

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        fg = (json or {}).get("filterGroup") or {}
        calls.append(any(f.get("key") == "exercise_start_date" for f in fg.get("filters", [])))
        return _FakeResp({"content": [], "totalElements": 0}), None

    monkeypatch.setattr(collector, "_api", fake_api)

    sims, total = await collector._aev_finished_exercises(
        "http://fake", {}, None, since_iso="2026-03-01T00:00:00.000Z"
    )
    assert calls == [True]  # windowed only - no fallback retry
    assert sims == []
    assert total == 0


async def test_collect_openaev_drift_uses_sim_ids_not_the_result_tuple(monkeypatch):
    """The expectations-drift call must iterate the unpacked
    ``sims`` list, NOT the ``(sims, sims_total)`` tuple ``_aev_finished_exercises``
    returns. Iterating the tuple called ``.get`` on the list itself and the
    resulting ``AttributeError`` aborted the ENTIRE OpenAEV collection - every
    OpenAEV-backed fact (endpoints, findings, validation rates) stayed empty."""
    drift_paths: list[str] = []

    sims_payload = {
        "content": [
            {
                "exercise_id": "sim-1",
                "exercise_global_score": [
                    {
                        "type": "PREVENTION",
                        "distribution": [
                            {"id": "SUCCESS", "value": 3},
                            {"id": "FAILED", "value": 1},
                        ],
                    },
                ],
            },
            {"exercise_id": "sim-2", "exercise_global_score": []},
            # No exercise_id: must be skipped, not turned into the string "None".
            {"exercise_global_score": []},
        ],
        "totalElements": 3,
    }

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None):
        if path == "/api/exercises/search":
            return _FakeResp(sims_payload), None
        if path.startswith("/api/exercises/") and path.endswith("/expectations-drift"):
            drift_paths.append(path)
            return _FakeResp({"drift_detected": "sim-1" in path}), None
        if path == "/api/domains":
            return _FakeResp([{"domain_id": "u1", "domain_name": "Endpoint"}]), None
        if path == "/api/injector_contracts/domain-counts":
            return _FakeResp([{"domain": "u1", "count": 5}]), None
        if path == "/api/collectors?include_next=true":
            return _FakeResp([]), None
        if path == "/api/attack_patterns/coverage":
            return _FakeResp([]), None
        # Every remaining call is a paginated count search.
        return _FakeResp({"content": [], "totalElements": 4}), None

    monkeypatch.setattr(collector, "_api", fake_api)

    stages = await collector._collect_openaev("http://fake", {}, "u1", None)

    assert sorted(drift_paths) == [
        "/api/exercises/sim-1/expectations-drift",
        "/api/exercises/sim-2/expectations-drift",
    ]
    assert stages["validation"]["drifted_simulations"] == 1
    assert stages["validation"]["finished_simulations"] == 3
    # The collection completed: the surrounding facts are populated, not lost
    # to the tuple-iteration crash.
    assert stages["scoping"]["endpoints_total"] == 4
    assert stages["prioritization"]["findings_total"] == 4


async def test_build_posture_one_provider_failure_does_not_block_other(monkeypatch):
    """A provider raising (or having no creds) never blocks the other product -
    the posture still returns the healthy provider's facts."""

    async def fake_creds(db, user_id, provider):
        # OpenAEV not registered for this user -> no creds.
        return ("http://fake", {"Authorization": "x"}) if provider == "opencti" else None

    monkeypatch.setattr(collector, "_user_creds", fake_creds)

    async def fake_octi(base_url, headers, user_id, client):
        return {"scoping": {"octi": 1}}

    async def fake_aev(base_url, headers, user_id, client):  # pragma: no cover - never called
        raise AssertionError("OpenAEV collector must not run without creds")

    monkeypatch.setattr(
        collector,
        "_VERTICAL_COLLECTORS",
        {"cyber": [("opencti", fake_octi), ("openaev", fake_aev)]},
    )

    stage_facts, present = await collector.build_posture_for_user(None, "u1", "cyber")

    assert present == {"opencti"}
    assert stage_facts["scoping"] == {"octi": 1}
