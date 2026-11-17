"""The CTEM posture cache is keyed to its shape (#11662).

A posture cached before a deploy that adds a viz key (the prioritization funnel
of #11557) stayed "fresh" for up to 24 h without that key, and the stage page
drew its sample funnel on a tenant with OpenCTI connected. ``_POSTURE_SHAPE``
marks what a cached payload carries: another shape is stale whatever its age.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

from app.api.v1 import ctem_metrics
from app.services.ctem_scoring import STAGE_KEYS
from app.services.ctem_verticals import CTEM_VERTICALS

# The viz keys each shape draws, per stage. A change to what ``_stage_viz``
# returns is a change of shape: bump ``_POSTURE_SHAPE`` and add its line here,
# so the caches built by the previous release are rebuilt at the first read.
_SHAPE_VIZ_KEYS = {
    2: {
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel"},
        "scoping": {"attack_domains"},
        "validation": {"expectation_buckets", "outcome_matrix"},
    },
    # The Discovery mixed views (#11998).
    3: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel"},
        "scoping": {"attack_domains"},
        "validation": {"expectation_buckets", "outcome_matrix"},
    },
    # The Scoping mixed views (#11998).
    4: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel"},
        "scoping": {"attack_domains", "observables_by_type", "vulnerabilities_by_severity"},
        "validation": {"expectation_buckets", "outcome_matrix"},
    },
    # The Prioritization mixed views (#11998): OpenAEV findings split.
    5: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel", "findings"},
        "scoping": {"attack_domains", "observables_by_type", "vulnerabilities_by_severity"},
        "validation": {"expectation_buckets", "outcome_matrix"},
    },
    # The Validation mixed views (#11998): OpenCTI rule coverage and detection depth.
    6: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel", "findings"},
        "scoping": {"attack_domains", "observables_by_type", "vulnerabilities_by_severity"},
        "validation": {"expectation_buckets", "outcome_matrix", "rule_coverage_by_tactic", "detection_depth"},
    },
    # The Mobilization mixed views (#11998): OpenCTI response mix and progress, OpenAEV fix backlog.
    7: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel", "findings"},
        "scoping": {"attack_domains", "observables_by_type", "vulnerabilities_by_severity"},
        "validation": {"expectation_buckets", "outcome_matrix", "rule_coverage_by_tactic", "detection_depth"},
        "remediation": {"response_mix", "response_progress", "fix_backlog"},
    },
    # The Exposed layer of the funnel reads the profile assets by reachability (#12284).
    8: {
        "discovery": {"techniques_by_tactic", "top_threats", "target_sectors", "target_countries"},
        "prioritization": {"severity_distribution", "top_vulnerabilities", "funnel", "findings"},
        "scoping": {"attack_domains", "observables_by_type", "vulnerabilities_by_severity"},
        "validation": {"expectation_buckets", "outcome_matrix", "rule_coverage_by_tactic", "detection_depth"},
        "remediation": {"response_mix", "response_progress", "fix_backlog"},
    },
}


class _EveryFact:
    """Stage facts that answer every key, so ``_stage_viz`` returns all its keys."""

    def get(self, _key, _default=None):
        return [{"x": 1}]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        self.store[key] = value


def _viz_keys() -> dict[str, set[str]]:
    keys: dict[str, set[str]] = {}
    for vertical in CTEM_VERTICALS:
        for stage in STAGE_KEYS:
            viz = ctem_metrics._stage_viz(stage, _EveryFact(), vertical)
            if viz:
                keys.setdefault(stage, set()).update(viz)
    return keys


def test_a_viz_change_without_a_shape_bump_fails():
    assert ctem_metrics._POSTURE_SHAPE in _SHAPE_VIZ_KEYS, (
        "_POSTURE_SHAPE was bumped: add its viz keys to _SHAPE_VIZ_KEYS"
    )
    assert _viz_keys() == _SHAPE_VIZ_KEYS[ctem_metrics._POSTURE_SHAPE], (
        "the cached posture changed shape: bump _POSTURE_SHAPE in ctem_metrics.py "
        "so the caches of the previous release are rebuilt (#11662)"
    )


def test_the_findings_split_is_openaevs_or_none():
    # No OpenAEV: no findings fact, so no split (the page draws its gap filler).
    assert ctem_metrics._findings_split({}) is None
    assert ctem_metrics._findings_split({"findings_total": True}) is None
    assert ctem_metrics._findings_split({"findings_total": 89, "findings_cve": 61}) == {"total": 89, "with_cve": 61}
    # A missing or larger CVE count never draws past the total.
    assert ctem_metrics._findings_split({"findings_total": 5}) == {"total": 5, "with_cve": 0}
    assert ctem_metrics._findings_split({"findings_total": 5, "findings_cve": 9}) == {"total": 5, "with_cve": 5}


def test_the_facts_key_carries_the_shape():
    _, facts_key = ctem_metrics._posture_cache_keys("u", "cyber")
    assert f"v{ctem_metrics._POSTURE_SHAPE}" in facts_key


def test_only_a_young_payload_of_the_current_shape_is_fresh():
    now = datetime.now(UTC).isoformat()
    shape = ctem_metrics._POSTURE_SHAPE
    assert ctem_metrics._cached_is_fresh({"generated_at": now, "shape": shape}, profile_stamp=None)
    # Built by the release before the shape existed, or by an older shape.
    assert not ctem_metrics._cached_is_fresh({"generated_at": now}, profile_stamp=None)
    assert not ctem_metrics._cached_is_fresh({"generated_at": now, "shape": shape - 1}, profile_stamp=None)
    assert not ctem_metrics._cached_is_fresh({"generated_at": "not-a-date", "shape": shape}, profile_stamp=None)
    assert not ctem_metrics._cached_is_fresh(None, profile_stamp=None)


def test_a_posture_of_another_profile_version_is_stale():
    now = datetime.now(UTC).isoformat()
    payload = {"generated_at": now, "shape": ctem_metrics._POSTURE_SHAPE, "profile_version": "org:1"}
    assert ctem_metrics._cached_is_fresh(payload, profile_stamp="org:1")
    # A profile saved since (a new version, or the first one) makes it stale.
    assert not ctem_metrics._cached_is_fresh(payload, profile_stamp="org:2")
    assert not ctem_metrics._cached_is_fresh({**payload, "profile_version": None}, profile_stamp="org:1")


async def test_an_old_shape_cache_is_rebuilt_and_stamped(monkeypatch):
    calls = 0

    async def fake_build(db, user, vertical):
        nonlocal calls
        calls += 1
        facts = {"prioritization": {"funnel": {"layers": [1]}}}
        return {"generated_at": datetime.now(UTC).isoformat(), "build": calls}, facts

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()
    user = SimpleNamespace(id="u-old-shape")
    posture_key, _ = ctem_metrics._posture_cache_keys("u-old-shape", "cyber")
    # A posture cached minutes ago by the release before the funnel: no shape.
    client.store[posture_key] = json.dumps({"generated_at": datetime.now(UTC).isoformat(), "build": 0})

    payload, facts = await ctem_metrics._build_and_cache(None, user, "cyber", client)

    assert calls == 1, "a posture of an older shape must be rebuilt, not served as fresh"
    assert payload["shape"] == ctem_metrics._POSTURE_SHAPE
    assert json.loads(client.store[posture_key])["shape"] == ctem_metrics._POSTURE_SHAPE
    assert facts["prioritization"]["funnel"] == {"layers": [1]}

    # The rebuilt entry is fresh: the next read serves it without a build.
    await ctem_metrics._build_and_cache(None, user, "cyber", client)
    assert calls == 1


async def test_the_posture_route_schedules_a_rebuild_of_an_old_shape(monkeypatch):
    monkeypatch.setattr(ctem_metrics, "_require_xtm_one", lambda: None)
    client = _FakeRedis()

    async def _client():
        return client

    monkeypatch.setattr(ctem_metrics, "_redis_or_none", _client)
    scheduled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ctem_metrics, "_schedule_posture_refresh", lambda uid, vertical, _stamp: scheduled.append((uid, vertical))
    )

    async def _no_profile(_db):
        return None

    monkeypatch.setattr(ctem_metrics, "_profile_stamp", _no_profile)

    async def _context(_db, payload):
        return payload

    monkeypatch.setattr(ctem_metrics, "_with_context", _context)
    user = SimpleNamespace(id="u-route", ctem_vertical="cyber")
    posture_key, _ = ctem_metrics._posture_cache_keys("u-route", "cyber")
    client.store[posture_key] = json.dumps({"generated_at": datetime.now(UTC).isoformat()})

    served = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)

    assert served["generated_at"]  # the old copy is served this once
    assert scheduled == [("u-route", "cyber")]
    # The page is told the copy is being rebuilt, so it spins and polls (#12183).
    assert served["refreshing"] is True


async def test_a_profile_saved_after_the_posture_was_cached_rebuilds_it(monkeypatch):
    """Assets added to the organization profile after a posture was cached: the
    next read rebuilds it, and the Exposed layer leaves not_configured."""
    monkeypatch.setattr(ctem_metrics, "_require_xtm_one", lambda: None)
    ctem_metrics._build_locks.clear()
    ctem_metrics._build_lock_refs.clear()
    client = _FakeRedis()

    async def _client():
        return client

    monkeypatch.setattr(ctem_metrics, "_redis_or_none", _client)
    # The organization profile: its saved version and whether it holds assets.
    profile = {"version": "org:1", "assets": False}

    async def _stamp(_db):
        return profile["version"]

    monkeypatch.setattr(ctem_metrics, "_profile_stamp", _stamp)
    builds = 0

    async def fake_build(db, user, vertical):
        nonlocal builds
        builds += 1
        exposed = "available" if profile["assets"] else "not_configured"
        return {"generated_at": datetime.now(UTC).isoformat(), "exposed": exposed}, {}

    monkeypatch.setattr(ctem_metrics, "_build_posture_payload", fake_build)
    rebuilds: list = []
    monkeypatch.setattr(
        ctem_metrics, "_schedule_posture_refresh", lambda uid, vertical, stamp: rebuilds.append((uid, vertical, stamp))
    )

    async def _context(_db, payload):
        return payload

    monkeypatch.setattr(ctem_metrics, "_with_context", _context)
    user = SimpleNamespace(id="u-profile", ctem_vertical="cyber")

    first = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert first["exposed"] == "not_configured" and builds == 1
    # Served from the cache while the profile is unchanged.
    await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert builds == 1 and rebuilds == []

    # A new profile version saves the assets and exposure.
    profile.update(version="org:2", assets=True)
    served = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert served["refreshing"] is True, "a posture of the previous profile version must be rebuilt"
    assert rebuilds == [("u-profile", "cyber", "org:2")]

    # The rebuild (run in the background in production) lands the new posture.
    await ctem_metrics._build_and_cache(None, user, "cyber", client)
    assert builds == 2
    rebuilt = await ctem_metrics.ctem_posture(vertical=None, db=None, user=user)
    assert rebuilt["exposed"] == "available"
    assert rebuilt["profile_version"] == "org:2"
    assert rebuilt["refreshing"] is False
