# SPDX-License-Identifier: Apache-2.0

"""CTEM-26 (#1110): the outside lens through a connected provider, diffed in code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import ctem_attack_surface as surface
from app.services import organization_context as oc

NOW = datetime(2026, 11, 22, 7, 0, tzinfo=UTC)


def _profile(**extra: Any) -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "owned_domains": ["corp.example"],
            "address_ranges": ["203.0.113.0/24"],
            "assets_exposure": [
                {"name": "shop.corp.example", "internet_facing": "yes", "criticality": "high"},
                {"name": "admin.corp.example", "internet_facing": "no", "criticality": "very_high"},
            ],
            **extra,
        }
    )


def _obs(host: str, ip: str = "203.0.113.10", *ports: int, **kw: Any) -> surface.Observation:
    return surface.Observation(host=host, ip=ip, ports=[surface.Port(p) for p in ports], **kw)


def _base(host: str, *ports: int, days_ago: int = 1, signalled: tuple[str, ...] = ()) -> surface.Baseline:
    return surface.Baseline(
        host=host,
        ports=tuple(f"{p}/tcp" for p in ports),
        last_seen_at=NOW - timedelta(days=days_ago),
        signalled=signalled,
    )


def _diff(observations, baseline, profile=None):
    return surface.diff(profile or _profile(), observations, baseline, now=NOW, provider="shodan")


# ── profile fields ──────────────────────────────────────────────────────


def test_address_ranges_are_normalised_cidr_blocks() -> None:
    profile = oc.parse_profile({"address_ranges": ["203.0.113.7/24", "198.51.100.1", "203.0.113.0/24"]})
    assert profile.address_ranges == ["203.0.113.0/24", "198.51.100.1/32"]


def test_an_address_range_that_is_not_one_is_refused() -> None:
    with pytest.raises(ValueError):
        oc.parse_profile({"address_ranges": ["not-a-range"]})


def test_the_pass_is_off_by_default_and_stays_out_of_the_always_on_block() -> None:
    profile = _profile()
    assert profile.attack_surface.enabled is False
    assert profile.attack_surface.cadence_hours == 24
    dumped = oc.legacy_dump(profile)
    assert "address_ranges" not in dumped and "attack_surface" not in dumped
    assert "address_ranges" not in dict(oc.CONTRACT_FIELDS)


# ── path 5: new exposure ────────────────────────────────────────────────


def test_a_new_open_port_on_a_declared_asset_gives_one_path_5_signal() -> None:
    found = _diff(
        [_obs("shop.corp.example", "203.0.113.10", 443, 8443)], {"shop.corp.example": _base("shop.corp.example", 443)}
    )
    assert [(f.trigger_path, f.detail) for f in found] == [("new_exposure", "port:8443/tcp")]
    assert found[0].evidence["new_port"] == "8443/tcp"
    assert found[0].evidence["hostnames"] == ["shop.corp.example"]


def test_a_declared_asset_that_should_not_answer_gives_a_path_5_signal_on_the_first_pass() -> None:
    found = _diff([_obs("admin.corp.example", "203.0.113.11", 443)], {})
    assert [(f.trigger_path, f.detail) for f in found] == [("new_exposure", "answers")]


# ── path 6: stale or orphaned asset ─────────────────────────────────────


def test_a_name_whose_target_disappeared_gives_one_path_6_signal() -> None:
    found = _diff(
        [surface.Observation(host="old.corp.example", resolves=False)],
        {"old.corp.example": _base("old.corp.example", 443)},
    )
    assert [(f.trigger_path, f.detail) for f in found] == [("stale_asset", "dangling")]


def test_an_owned_name_pointing_outside_the_owned_ranges_is_path_6() -> None:
    found = _diff(
        [_obs("promo.corp.example", "192.0.2.50", 443)], {"promo.corp.example": _base("promo.corp.example", 443)}
    )
    assert [(f.trigger_path, f.detail) for f in found] == [("stale_asset", "unowned:192.0.2.50")]


def test_a_host_unseen_for_the_stale_delay_is_path_6() -> None:
    found = _diff([], {"legacy.corp.example": _base("legacy.corp.example", 80, days_ago=20)})
    assert [(f.trigger_path, f.detail) for f in found] == [("stale_asset", "unseen")]
    assert _diff([], {"legacy.corp.example": _base("legacy.corp.example", 80, days_ago=3)}) == []


# ── path 7: surface drift ───────────────────────────────────────────────


def test_an_unknown_host_gives_one_path_7_signal() -> None:
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    found = _diff(
        [_obs("shop.corp.example", "203.0.113.10", 443), _obs("dev.corp.example", "203.0.113.20", 22)], baseline
    )
    assert [(f.trigger_path, f.host) for f in found] == [("surface_drift", "dev.corp.example")]


def test_the_first_pass_sets_the_baseline_and_raises_no_drift() -> None:
    assert _diff([_obs("dev.corp.example", "203.0.113.20", 22)], {}) == []


# ── idempotence ─────────────────────────────────────────────────────────


def test_two_identical_passes_give_no_new_signal() -> None:
    first = _diff(
        [_obs("shop.corp.example", "203.0.113.10", 443, 8443)], {"shop.corp.example": _base("shop.corp.example", 443)}
    )
    keys = tuple(f.dedupe_key for f in first)
    again = _diff(
        [_obs("shop.corp.example", "203.0.113.10", 443, 8443)],
        {"shop.corp.example": _base("shop.corp.example", 443, 8443, signalled=keys)},
    )
    assert again == []


def test_a_port_that_closed_may_signal_again_when_it_reopens() -> None:
    key = "new_exposure:attack_surface:shop.corp.example:port:8443/tcp"
    closed = surface._still_holding([key], _obs("shop.corp.example", "203.0.113.10", 443))
    assert closed == []
    still = surface._still_holding([key], _obs("shop.corp.example", "203.0.113.10", 443, 8443))
    assert still == [key]


# ── the provider reader ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_shodan_reader_builds_one_observation_per_host(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "/dns/domain/corp.example": {
            "subdomains": ["shop", "old"],
            "data": [{"subdomain": "shop", "type": "A", "value": "203.0.113.10"}],
        },
        "/shodan/host/search": {
            "matches": [{"ip_str": "203.0.113.10", "port": 443, "transport": "tcp", "hostnames": ["shop.corp.example"]}]
        },
    }

    async def _get(self, path, action, params=None):
        return answers.get(path, {})

    monkeypatch.setattr(surface.ShodanSurfaceReader, "_get", _get)
    observed = await surface.ShodanSurfaceReader("k").observe(["corp.example"], ["203.0.113.0/24"])
    by_host = {o.host: o for o in observed}
    assert by_host["shop.corp.example"].ip == "203.0.113.10"
    assert [p.key for p in by_host["shop.corp.example"].ports] == ["443/tcp"]
    assert by_host["old.corp.example"].resolves is False


# ── segregation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pass_signals_are_recorded_by_system(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass signal is recorded by ``system``: only an administrator reads it (ctem_signals visibility)."""
    from app.services import ctem_scope_gate_store, ctem_signals

    recorded: list[dict[str, Any]] = []

    async def _record(db, **kw):
        recorded.append(kw)
        return object(), True

    async def _evaluate(db, row, *, created):
        return None

    async def _baseline(db, organization_id):
        return {}

    class _Reader:
        provider = "shodan"

        async def observe(self, domains, ranges):
            return [_obs("admin.corp.example", "203.0.113.11", 443)]

    class _Db:
        def add(self, row):
            pass

        async def flush(self):
            pass

    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _evaluate)
    monkeypatch.setattr(surface, "_baseline", _baseline)
    import uuid

    result = await surface.run_pass(_Db(), organization_id=uuid.uuid4(), profile=_profile(), reader=_Reader(), now=NOW)
    assert result["counts"]["new_exposure"] == 1
    assert [r["recorded_by"] for r in recorded] == [ctem_signals.SYSTEM_ACTOR]
    assert recorded[0]["signal"].lens == "outside"
    assert recorded[0]["signal"].subject_kind == "asset"
