# SPDX-License-Identifier: Apache-2.0

"""CTEM-26 (#1110): the outside lens through a connected provider, diffed in code."""

from __future__ import annotations

from dataclasses import replace
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


def _diff(
    observations: list[surface.Observation],
    baseline: dict[str, surface.Baseline],
    profile: oc.OrganizationProfile | None = None,
    **kw: Any,
) -> list[surface.Finding]:
    return surface.diff(profile or _profile(), observations, baseline, now=NOW, provider="shodan", **kw)


READ = "shodan|corp.example"
COMPLETE = {READ: "complete"}
PREVIOUS = surface.PreviousPass(at=NOW - timedelta(days=1), statuses=COMPLETE)


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


async def test_the_settings_picker_reads_the_supported_providers() -> None:
    """The Settings provider picker (#1140 slice 2) lists what the pass reads, so it grows with each reader."""
    from app.api.v1.organization_context import list_attack_surface_providers

    answer = await list_attack_surface_providers(_user=None)  # type: ignore[arg-type]
    assert answer.providers == list(surface.SUPPORTED_PROVIDERS)
    assert "shodan" in answer.providers


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
    """Two complete passes; the host is absent from pass 1 and unknown to the provider before it."""
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    found = _diff(
        [
            _obs("shop.corp.example", "203.0.113.10", 443, reads=[READ]),
            _obs("dev.corp.example", "203.0.113.20", 22, reads=[READ], known_since=NOW - timedelta(hours=2)),
        ],
        baseline,
        previous=PREVIOUS,
        statuses=COMPLETE,
    )
    assert [(f.trigger_path, f.host, f.detail) for f in found] == [("surface_drift", "dev.corp.example", "new_host")]
    assert found[0].evidence["previous_pass_at"] == PREVIOUS.at.isoformat()


def test_hosts_a_truncated_previous_read_missed_join_the_baseline_silently() -> None:
    """Pass 1 read 3 hosts on a truncated DNS page, pass 2 reads 41 completely: no new host."""
    known = [f"h{i}.corp.example" for i in range(3)]
    baseline = {h: _base(h, 443) for h in known}
    observed = [_obs(f"h{i}.corp.example", f"203.0.113.{i + 1}", 443, reads=[READ]) for i in range(41)]
    withheld: dict[str, str] = {}
    previous = surface.PreviousPass(at=NOW - timedelta(days=1), statuses={READ: "truncated"})
    found = _diff(observed, baseline, previous=previous, statuses=COMPLETE, withheld=withheld)
    assert [f for f in found if f.trigger_path == "surface_drift"] == []
    assert len(withheld) == 38 and set(withheld.values()) == {"previous_read_truncated"}


def test_no_new_host_without_a_previous_summary_or_on_an_incomplete_read() -> None:
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    host = [_obs("dev.corp.example", "203.0.113.20", 22, reads=[READ])]
    assert _diff(host, baseline) == []
    assert _diff(host, baseline, previous=PREVIOUS, statuses={READ: "truncated"}) == []
    assert _diff(host, baseline, previous=PREVIOUS, statuses={READ: "empty"}) == []
    missing = surface.PreviousPass(at=PREVIOUS.at, statuses={})
    assert _diff(host, baseline, previous=missing, statuses=COMPLETE) == []


def test_a_host_the_provider_knew_before_the_previous_pass_is_not_new() -> None:
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    old = _obs("dev.corp.example", "203.0.113.20", 22, reads=[READ], known_since=NOW - timedelta(days=30))
    withheld: dict[str, str] = {}
    assert _diff([old], baseline, previous=PREVIOUS, statuses=COMPLETE, withheld=withheld) == []
    assert withheld == {"dev.corp.example": "known_to_provider"}


def test_read_statuses_name_truncated_empty_and_complete_reads() -> None:
    coverage = [
        surface.ReadCoverage("shodan", "corp.example", "dns", 5, 500, truncated=True),
        surface.ReadCoverage("shodan", "corp.example", "search", 1, 3),
        surface.ReadCoverage("shodan", "203.0.113.0/24", "search", 1, 0),
        surface.ReadCoverage("shodan", "other.example", "dns", 1, 4),
    ]
    assert surface.read_statuses(coverage) == {
        "shodan|corp.example": "truncated",
        "shodan|203.0.113.0/24": "empty",
        "shodan|other.example": "complete",
    }


def test_a_provider_name_outside_the_owned_domains_is_context_never_a_new_host() -> None:
    """Owned domain only, no ranges: the reverse DNS of a shared hosting IP raises nothing."""
    profile = _profile(address_ranges=[])
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    observed = [
        _obs("www.corp.example", "198.51.100.7", 80, reads=[READ]),
        _obs("host-7.hosting.example", "198.51.100.7", 80, reads=[READ]),
    ]
    out_of_scope: dict[str, str] = {}
    found = _diff(observed, baseline, profile, previous=PREVIOUS, statuses=COMPLETE, out_of_scope=out_of_scope)
    assert [(f.trigger_path, f.host, f.detail) for f in found] == [("surface_drift", "www.corp.example", "new_host")]
    assert found[0].evidence["shared_ip_names"] == ["host-7.hosting.example"]
    assert out_of_scope == {"host-7.hosting.example": "198.51.100.7"}


def test_a_foreign_name_on_an_owned_address_is_in_scope() -> None:
    found = _diff(
        [_obs("edge.cdn.example", "203.0.113.30", 443, reads=[READ])],
        {"shop.corp.example": _base("shop.corp.example", 443)},
        previous=PREVIOUS,
        statuses=COMPLETE,
    )
    assert [(f.trigger_path, f.host) for f in found] == [("surface_drift", "edge.cdn.example")]


def test_an_out_of_scope_host_raises_no_path_6_either() -> None:
    profile = _profile(address_ranges=[])
    assert _diff([_obs("gone.hosting.example", "198.51.100.8", resolves=False)], {}, profile) == []
    stale = {"old.hosting.example": _base("old.hosting.example", 80, days_ago=30)}
    assert _diff([], stale, profile) == []


def test_a_host_in_scope_by_its_address_keeps_its_unseen_check() -> None:
    """A baseline host scoped only by its stored address still raises path 6 once gone."""
    gone = replace(_base("edge.cdn.example", 443, days_ago=30), ip="203.0.113.30")
    found = _diff([], {"edge.cdn.example": gone})
    assert [(f.trigger_path, f.host, f.detail) for f in found] == [("stale_asset", "edge.cdn.example", "unseen")]
    outside = replace(gone, ip="198.51.100.8")
    assert _diff([], {"edge.cdn.example": outside}) == []


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


@pytest.mark.asyncio
async def test_a_name_backed_only_by_mx_or_txt_records_is_not_dangling(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = {
        "/dns/domain/corp.example": {
            "subdomains": ["_dmarc", "mail", "old"],
            "data": [
                {"subdomain": "_dmarc", "type": "TXT", "value": "v=DMARC1; p=reject"},
                {"subdomain": "mail", "type": "MX", "value": "mx.provider.example"},
            ],
        },
    }

    async def _get(self: Any, path: str, action: str, params: Any = None) -> Any:
        return answers.get(path, {})

    monkeypatch.setattr(surface.ShodanSurfaceReader, "_get", _get)
    observed = await surface.ShodanSurfaceReader("k").observe(["corp.example"], [])
    dangling = sorted(o.host for o in observed if o.resolves is False)
    assert dangling == ["old.corp.example"]


@pytest.mark.asyncio
async def test_the_shodan_reader_pages_dns_and_search_up_to_the_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/dns/domain`` is paged while ``more`` is true, the search while matches remain; the bound truncates."""
    calls: list[tuple[str, int]] = []

    async def _get(self: Any, path: str, action: str, params: Any = None) -> Any:
        page = int((params or {}).get("page") or 1)
        calls.append((path, page))
        if path.startswith("/dns/domain/"):
            return {"more": True, "data": [{"subdomain": f"d{page}", "type": "A", "value": f"203.0.113.{page}"}]}
        matches = [
            {"ip_str": f"198.51.100.{page}", "port": 1000 + i, "hostnames": [f"s{page}.corp.example"]}
            for i in range(surface._SHODAN_SEARCH_PAGE_SIZE)
        ]
        return {"total": 10_000, "matches": matches}

    monkeypatch.setattr(surface.ShodanSurfaceReader, "_get", _get)
    reader = surface.ShodanSurfaceReader("k")
    observed = await reader.observe(["corp.example"], [])
    dns_pages = [p for path, p in calls if path.startswith("/dns/domain/")]
    search_pages = [p for path, p in calls if path == "/shodan/host/search"]
    assert dns_pages == list(range(1, surface._SHODAN_DNS_PAGES + 1))
    assert search_pages == list(range(1, surface._SHODAN_SEARCH_PAGES + 1))
    assert all(c.truncated for c in reader.coverage)
    assert surface.read_statuses(reader.coverage) == {"shodan|corp.example": "truncated"}
    assert {o.host for o in observed} >= {"d1.corp.example", f"d{surface._SHODAN_DNS_PAGES}.corp.example"}
    assert all(o.reads == ["shodan|corp.example"] for o in observed)


@pytest.mark.asyncio
async def test_the_shodan_reader_stops_when_the_read_is_complete(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    async def _get(self: Any, path: str, action: str, params: Any = None) -> Any:
        calls.append(path)
        if path.startswith("/dns/domain/"):
            return {
                "more": False,
                "data": [{"subdomain": "www", "type": "A", "value": "203.0.113.5", "last_seen": "2026-10-01T00:00:00"}],
            }
        return {"total": 1, "matches": [{"ip_str": "203.0.113.5", "port": 443, "hostnames": ["www.corp.example"]}]}

    monkeypatch.setattr(surface.ShodanSurfaceReader, "_get", _get)
    reader = surface.ShodanSurfaceReader("k")
    observed = await reader.observe(["corp.example"], [])
    assert len(calls) == 2
    assert surface.read_statuses(reader.coverage) == {"shodan|corp.example": "complete"}
    assert observed[0].known_since == datetime(2026, 10, 1, tzinfo=UTC)


# The shape of a Search ASM Entities hit, as Google documents it for its SecOps integration
# (docs.cloud.google.com/chronicle/docs/soar/marketplace-integrations/mandiant-asm), with the
# ports filled in.
_GTI_IP_ENTITY: dict[str, Any] = {
    "id": "1",
    "dynamic_id": "Intrigue::Entity::IpAddress#203.0.113.10",
    "name": "203.0.113.10",
    "type": "Intrigue::Entity::IpAddress",
    "first_seen": "2026-11-01T01:44:46Z",
    "last_seen": "2026-11-21T01:44:46Z",
    "summary": {
        "scoped": True,
        "ports": {"count": 2, "tcp": [443, {"port": 8443}], "udp": None},
        "resolutions": ["shop.corp.example", "cdn.other.example"],
        "network": {"name": "EXAMPLE-02", "asn": "16509.0"},
    },
}


def _gti_answers(*entities: dict[str, Any]) -> dict[str, Any]:
    return {
        "/projects": {"success": True, "result": [{"id": 42, "name": "corp"}]},
        "/search/entities/type:Intrigue::Entity::IpAddress": {"result": {"hits": list(entities)}},
    }


@pytest.mark.asyncio
async def test_the_gti_asm_reader_keeps_the_owned_names_of_in_scope_ip_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_projects: list[str] = []
    out_of_scope = {
        **_GTI_IP_ENTITY,
        "name": "198.51.100.9",
        "summary": {"scoped": False, "resolutions": ["x.corp.example"]},
    }
    in_range_unnamed = {**_GTI_IP_ENTITY, "name": "203.0.113.20", "summary": {"scoped": True, "ports": {"tcp": [22]}}}
    elsewhere = {**_GTI_IP_ENTITY, "name": "192.0.2.5", "summary": {"scoped": True, "resolutions": ["a.other.example"]}}
    answers = _gti_answers(_GTI_IP_ENTITY, out_of_scope, in_range_unnamed, elsewhere)

    async def _get(self: Any, path: str, action: str, params: Any = None, project: str = "") -> Any:
        if project:
            seen_projects.append(project)
        return answers.get(path, {})

    monkeypatch.setattr(surface.GtiAsmSurfaceReader, "_get", _get)
    observed = await surface.GtiAsmSurfaceReader("k").observe(["corp.example"], ["203.0.113.0/24"])
    by_host = {o.host: o for o in observed}
    assert sorted(by_host) == ["203.0.113.20", "shop.corp.example"]
    assert by_host["shop.corp.example"].ip == "203.0.113.10"
    assert [p.key for p in by_host["shop.corp.example"].ports] == ["443/tcp", "8443/tcp"]
    assert [p.key for p in by_host["203.0.113.20"].ports] == ["22/tcp"]
    assert seen_projects == ["42"]


@pytest.mark.asyncio
async def test_the_gti_asm_reader_follows_the_page_token(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = [
        {"result": {"hits": [_GTI_IP_ENTITY], "next_page_token": "p2"}},
        {
            "result": {
                "hits": [{**_GTI_IP_ENTITY, "name": "203.0.113.11", "summary": {"resolutions": ["api.corp.example"]}}]
            }
        },
    ]
    tokens: list[str] = []

    async def _get(self: Any, path: str, action: str, params: Any = None, project: str = "") -> Any:
        if path == "/projects":
            return {"result": [{"id": "42"}]}
        tokens.append((params or {}).get("page_token", ""))
        return pages[len(tokens) - 1]

    monkeypatch.setattr(surface.GtiAsmSurfaceReader, "_get", _get)
    observed = await surface.GtiAsmSurfaceReader("k").observe(["corp.example"], [])
    assert tokens == ["", "p2"]
    assert sorted(o.host for o in observed) == ["api.corp.example", "shop.corp.example"]


def test_observations_of_several_providers_merge_per_host() -> None:
    shodan = [
        _obs("shop.corp.example", "203.0.113.10", 443),
        surface.Observation(host="old.corp.example", resolves=False),
    ]
    gti = [_obs("SHOP.corp.example", "203.0.113.10", 443, 8443), _obs("old.corp.example", "203.0.113.12", 80)]
    merged = {o.host: o for o in surface.merge_observations([("shodan", shodan), ("gti", gti)])}
    assert [p.key for p in merged["shop.corp.example"].ports] == ["443/tcp", "8443/tcp"]
    assert merged["shop.corp.example"].providers == ["shodan", "gti"]
    assert merged["old.corp.example"].resolves is True
    baseline = {"shop.corp.example": _base("shop.corp.example", 443)}
    found = surface.diff(_profile(), list(merged.values()), baseline, now=NOW, provider="shodan+gti")
    new_port = [f for f in found if f.detail == "port:8443/tcp"]
    assert new_port and new_port[0].evidence["providers"] == ["shodan", "gti"]


@pytest.mark.asyncio
async def test_a_merged_reader_fails_the_pass_when_one_provider_fails() -> None:
    class _Ok:
        provider = "shodan"

        async def observe(self, domains: list[str], ranges: list[str]) -> list[surface.Observation]:
            return [_obs("shop.corp.example", "203.0.113.10", 443)]

    class _Down:
        provider = "gti"

        async def observe(self, domains: list[str], ranges: list[str]) -> list[surface.Observation]:
            raise surface.SurfaceReadError("Google Threat Intelligence answered 503")

    reader = surface.MergedSurfaceReader([_Ok(), _Down()])
    assert reader.provider == "shodan+gti"
    with pytest.raises(surface.SurfaceReadError):
        await reader.observe(["corp.example"], [])


def test_the_declared_integrations_are_read_once_each() -> None:
    profile = _profile(attack_surface={"enabled": True, "integration": "a", "integrations": ["b", "a", " "]})
    assert surface.declared_integrations(profile.attack_surface) == ["a", "b"]


@pytest.mark.asyncio
async def test_an_unreadable_integration_is_skipped_and_the_others_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Reader:
        provider = "gti"

    async def _reader_of(db: Any, integration_id: str) -> Any:
        return _Reader() if integration_id == "b" else "securityscorecard has no attack surface pass"

    monkeypatch.setattr(surface, "_reader_of", _reader_of)
    profile = _profile(attack_surface={"enabled": True, "integration": "a", "integrations": ["b"]})
    reader = await surface.reader_for(object(), profile)  # type: ignore[arg-type]
    assert getattr(reader, "provider", None) == "gti"
    only_bad = _profile(attack_surface={"enabled": True, "integration": "a"})
    assert await surface.reader_for(object(), only_bad) == "securityscorecard has no attack surface pass"  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_empty_pass_list_names_the_first_step_the_schedule_stops_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2331: why_no_pass reads the profile as scheduled_pass does."""
    profiles: list[Any] = []

    async def _current(_db: Any, _org: Any) -> Any:
        return None, profiles[-1]

    async def _reader_for(_db: Any, _profile: Any) -> Any:
        return "no attack surface provider is declared in the profile"

    monkeypatch.setattr(oc, "current_profile", _current)
    monkeypatch.setattr(surface, "reader_for", _reader_for)
    profiles.append(_profile())
    assert "turned off" in await surface.why_no_pass(object(), None)  # type: ignore[arg-type]
    profiles.append(_profile(attack_surface={"enabled": True}, owned_domains=[], address_ranges=[]))
    assert "no owned domains" in await surface.why_no_pass(object(), None)  # type: ignore[arg-type]
    profiles.append(_profile(attack_surface={"enabled": True}))
    assert await surface.why_no_pass(object(), None) == "no attack surface provider is declared in the profile"  # type: ignore[arg-type]

    async def _readable(_db: Any, _profile: Any) -> Any:
        return object()

    monkeypatch.setattr(surface, "reader_for", _readable)
    assert (await surface.why_no_pass(object(), None)).startswith("no pass has completed yet")  # type: ignore[arg-type]


def test_a_repaired_signal_carries_its_dates() -> None:
    """#2331: the dry run names each signal with the dates an operator checks it against."""
    seen = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    row = type(
        "Row",
        (),
        {
            "id": "s1",
            "subject_ref": "a.corp.example",
            "first_seen_at": seen,
            "observed_at": seen,
            "state": "to_validate",
        },
    )()
    assert surface._signal_dates(row) == {
        "id": "s1",
        "host": "a.corp.example",
        "first_seen_at": seen.isoformat(),
        "observed_at": seen.isoformat(),
        "state": "to_validate",
    }


def test_every_connected_provider_is_either_read_or_named_with_its_reason() -> None:
    assert set(surface.READERS) == set(surface.SUPPORTED_PROVIDERS)
    assert not set(surface.READERS) & set(surface.NOT_SUPPORTED)
    assert all(reason for reason in surface.NOT_SUPPORTED.values())


# ── the schedule ────────────────────────────────────────────────────────


class _SlotRedis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None) -> bool | None:
        if nx and key in self.keys:
            return None
        self.keys.add(key)
        return True

    async def delete(self, key: str) -> None:
        self.keys.discard(key)


class _TickDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _tick_db() -> Any:
    return _TickDb()


def _patch_tick(monkeypatch: pytest.MonkeyPatch, redis: _SlotRedis) -> None:
    import uuid
    from types import SimpleNamespace

    from app.core import redis as redis_module
    from app.services import ctem_case_loop, organizations

    profile = _profile(attack_surface={"enabled": True, "integration": str(uuid.uuid4())})

    async def _client() -> _SlotRedis:
        return redis

    async def _primary(db: Any) -> Any:
        return SimpleNamespace(id=uuid.uuid4())

    async def _current(db: Any, organization_id: Any) -> Any:
        return SimpleNamespace(profile=profile.model_dump())

    async def _hydrated(db: Any, row: Any) -> Any:
        # No asset records in this fake session (#1543): the stored profile is the truth.
        return oc.parse_profile(row.profile if row else {})

    async def _publish(pending: Any) -> None:
        return None

    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(oc, "hydrated_profile", _hydrated)
    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)


@pytest.mark.asyncio
async def test_a_skipped_pass_gives_the_cadence_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _SlotRedis()
    _patch_tick(monkeypatch, redis)

    async def _no_reader(db: Any, profile: Any) -> str:
        return "no credential could be resolved for the attack surface integration"

    monkeypatch.setattr(surface, "reader_for", _no_reader)
    result = await surface.scheduled_pass(_tick_db())
    assert result is not None and str(result["skipped"]).startswith("no credential")
    assert redis.keys == set()


@pytest.mark.asyncio
async def test_a_failed_provider_read_gives_the_cadence_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _SlotRedis()
    _patch_tick(monkeypatch, redis)

    async def _reader(db: Any, profile: Any) -> Any:
        return object()

    async def _fail(db: Any, **kw: Any) -> Any:
        raise surface.SurfaceReadError("Shodan answered 503")

    monkeypatch.setattr(surface, "reader_for", _reader)
    monkeypatch.setattr(surface, "run_pass", _fail)
    db = _tick_db()
    assert await surface.scheduled_pass(db) == {"skipped": "Shodan answered 503"}
    assert db.rollbacks == 1 and redis.keys == set()


@pytest.mark.asyncio
async def test_a_committed_pass_keeps_the_cadence_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    redis = _SlotRedis()
    _patch_tick(monkeypatch, redis)

    async def _reader(db: Any, profile: Any) -> Any:
        return object()

    async def _ok(db: Any, **kw: Any) -> dict[str, Any]:
        return {"pending": [], "counts": {"new_exposure": 0}}

    monkeypatch.setattr(surface, "reader_for", _reader)
    monkeypatch.setattr(surface, "run_pass", _ok)
    db = _tick_db()
    assert await surface.scheduled_pass(db) == {"new_exposure": 0}
    assert db.commits == 1 and redis.keys == {"ctem:attack-surface:pass"}
    assert await surface.scheduled_pass(db) is None


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

    async def _previous(db, organization_id):
        return None

    monkeypatch.setattr(surface, "_baseline", _baseline)
    monkeypatch.setattr(surface, "_previous_pass", _previous)
    import uuid

    result = await surface.run_pass(_Db(), organization_id=uuid.uuid4(), profile=_profile(), reader=_Reader(), now=NOW)
    assert result["counts"]["new_exposure"] == 1
    assert [r["recorded_by"] for r in recorded] == [ctem_signals.SYSTEM_ACTOR]
    assert recorded[0]["signal"].lens == "outside"
    assert recorded[0]["signal"].subject_kind == "asset"
