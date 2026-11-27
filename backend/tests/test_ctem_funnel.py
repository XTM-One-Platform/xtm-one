"""The CTEM prioritization funnel (#11546): one test per layer computation,
the honesty rule (an unconfigured layer has no count, never 0, and never counts
as filtered), totals that never increase, and the collector wiring that keeps
the stage score and the history snapshot unchanged.

No DB / network: the collectors and ``_graphql`` are monkeypatched.
"""

from __future__ import annotations

import pytest

from app.api.v1.ctem_metrics import _numeric_metrics, _stage_viz
from app.services import ctem_metrics_collector as collector
from app.services.asset_reachability import exposed_count, reachability_split
from app.services.ctem_funnel import (
    EPSS_THRESHOLD,
    EXPOSED_NOT_LINKED,
    LAYERS,
    LIVE,
    NOT_AVAILABLE,
    NOT_CONFIGURED,
    build_funnel,
)
from app.services.ctem_scoring import compute_posture
from app.services.organization_context import AssetEntry


def _layer(funnel, key):
    return next(layer for layer in funnel["layers"] if layer["key"] == key)


def test_every_layer_is_drawn_in_order():
    funnel = build_funnel(known=100, exploitable_known=10)
    assert [layer["key"] for layer in funnel["layers"]] == [key for key, _, _ in LAYERS]


def test_known_layer_counts_every_vulnerability_and_carries_openaev_findings():
    funnel = build_funnel(known=4214, exploitable_known=300, asset_findings=89)
    known = _layer(funnel, "known")
    assert known["count"] == 4214 and known["state"] == LIVE and known["drop"] is None
    assert known["asset_findings"] == 89
    assert _layer(build_funnel(known=10), "known")["asset_findings"] is None


def test_no_known_count_means_no_funnel():
    assert build_funnel(known=None) is None
    assert build_funnel(known=True) is None


def test_relevant_layer_matches_the_tech_stack():
    funnel = build_funnel(known=4214, relevant=512, exploitable_known=300, exploitable_relevant=21)
    relevant = _layer(funnel, "relevant")
    assert relevant == {**relevant, "count": 512, "drop": 3702, "state": LIVE, "category": None}


def test_without_a_tech_stack_relevant_is_not_configured_and_never_zero():
    funnel = build_funnel(known=4214, exploitable_known=300)
    relevant = _layer(funnel, "relevant")
    assert relevant["state"] == NOT_CONFIGURED
    assert relevant["count"] is None and relevant["drop"] is None
    assert relevant["category"] == "software"
    # Not counted as filtered: exploitable reads the known layer.
    exploitable = _layer(funnel, "exploitable")
    assert exploitable["basis"] == "known"
    assert exploitable["count"] == 300 and exploitable["drop"] == 4214 - 300


def test_exploitable_layer_is_kev_or_epss_on_the_relevant_layer():
    funnel = build_funnel(known=4214, relevant=512, exploitable_known=300, exploitable_relevant=21)
    exploitable = _layer(funnel, "exploitable")
    assert exploitable["basis"] == "relevant"
    assert exploitable["count"] == 21 and exploitable["drop"] == 491
    assert funnel["epss_threshold"] == EPSS_THRESHOLD == 0.5
    assert funnel["needs_action"] == 21


def test_exploitable_without_its_count_is_not_available():
    funnel = build_funnel(known=100)
    assert _layer(funnel, "exploitable")["state"] == NOT_AVAILABLE
    assert _layer(funnel, "exploitable")["count"] is None
    assert funnel["needs_action"] == 100


@pytest.mark.parametrize(
    "key,category", [("exposed", "assets"), ("business_impact", "primary_assets"), ("policy", "prioritization_policy")]
)
def test_organization_layers_are_not_configured_until_their_fields_exist(key, category):
    layer = _layer(build_funnel(known=100, relevant=50, exploitable_known=9, exploitable_relevant=5), key)
    assert layer["state"] == NOT_CONFIGURED and layer["count"] is None and layer["drop"] is None
    assert layer["category"] == category


_SPLIT = {"total": 211, "reachable": 150, "unreachable": 40, "unknown": 21, "endpoints": 120}


def test_exposed_without_assets_is_not_configured_with_its_call_to_action():
    for reachability in (None, {**_SPLIT, "total": 0}):
        layer = _layer(build_funnel(known=100, exploitable_known=9, reachability=reachability), "exposed")
        assert layer["state"] == NOT_CONFIGURED and layer["category"] == "assets" and layer["count"] is None
        assert "reachability" not in layer


def test_exposed_with_assets_but_no_join_is_not_available_and_shows_the_split():
    funnel = build_funnel(known=100, exploitable_known=9, reachability=_SPLIT)
    layer = _layer(funnel, "exposed")
    assert layer["state"] == NOT_AVAILABLE and layer["category"] is None
    assert layer["count"] is None and layer["drop"] is None
    assert layer["reason"] == EXPOSED_NOT_LINKED
    assert layer["reachability"] == _SPLIT
    assert funnel["needs_action"] == 9


def test_exposed_on_laptops_only_drops_nothing():
    laptops = [AssetEntry(name=f"l{i}", category="host", subcategory="laptop", internet_facing="no") for i in range(9)]
    split = reachability_split(laptops)
    exposed = exposed_count([[{"name": f"l{i}"}] for i in range(9)], laptops)
    layer = _layer(build_funnel(known=100, exploitable_known=9, reachability=split, exposed=exposed), "exposed")
    assert layer["state"] == LIVE and layer["count"] == 9 and layer["drop"] == 0
    assert layer["reachability"]["endpoints"] == 9


def test_exposed_drops_only_the_proven_unreachable_and_keeps_unknown():
    assets = [
        AssetEntry(name="srv-no", category="host", subcategory="server", internet_facing="no"),
        AssetEntry(name="srv-unknown", category="host", subcategory="server", internet_facing="unknown"),
    ]
    findings = [[{"name": "srv-no"}]] * 4 + [[{"name": "srv-unknown"}]] * 5
    exposed = exposed_count(findings, assets)
    funnel = build_funnel(known=100, exploitable_known=9, reachability=reachability_split(assets), exposed=exposed)
    layer = _layer(funnel, "exposed")
    assert layer["state"] == LIVE and layer["count"] == 5 and layer["drop"] == 4
    assert funnel["needs_action"] == 5


def test_exposed_never_increases_the_funnel():
    funnel = build_funnel(known=100, exploitable_known=9, reachability=_SPLIT, exposed=12)
    assert _layer(funnel, "exposed")["count"] == 9 and funnel["capped"] is True


# ── Business impact and policy on the live configuration (#1980) ───────────


def _reacher(name, products, *links):
    from app.services.ctem_primary_assets import Link, Reacher

    return Reacher(
        name=name,
        products={p.lower(): p for p in products},
        links=[(Link(primary, "opencrq"), kind, through) for primary, kind, through in links],
    )


def _index(*reachers, truncated=False):
    from app.services.ctem_primary_assets import PrimaryAssetIndex

    return PrimaryAssetIndex(loaded=True, reachers=list(reachers), truncated=truncated)


_STACK = [
    {"product": "chrome", "label": "Google Chrome", "cpe": "", "vendor": "google"},
    {"product": "openssl", "label": "OpenSSL", "cpe": "", "vendor": "openssl"},
    {"product": "nginx", "label": "nginx", "cpe": "", "vendor": "f5"},
]
_TODAY = __import__("datetime").date(2026, 11, 24)


def _profile(**policy):
    from app.services.organization_context import OrganizationProfile

    return OrganizationProfile.model_validate(
        {
            "primary_assets": [
                {"name": "Payments", "criticality": "critical"},
                {"name": "Wiki", "criticality": "low"},
            ],
            "prioritization_policy": policy,
        }
    )


def test_scope_without_links_or_policy_is_not_configured():
    from app.services.ctem_funnel import funnel_scope

    scope = funnel_scope(profile=_profile(), index=_index(), stack=_STACK, today=_TODAY)
    assert scope == {"business": {"configured": False}, "policy": {"configured": False}}


def test_business_scope_keeps_the_software_run_by_supporting_assets():
    from app.services.ctem_funnel import funnel_scope

    index = _index(
        _reacher("laptop-1", ["Chrome"], ("payments", "asset_group", "Laptops")),
        _reacher("db-1", ["OpenSSL"], ("payments", "asset", "db-1"), ("wiki", "asset", "db-1")),
    )
    business = funnel_scope(profile=_profile(), index=index, stack=_STACK, today=_TODAY)["business"]
    assert business["configured"] is True
    assert business["labels"] == ["Google Chrome", "OpenSSL"]
    assert business["primary_assets"] == 2 and business["supporting_assets"] == 2
    assert "reason" not in business


def test_business_scope_without_software_inventory_says_why():
    from app.services.ctem_funnel import SCOPE_NO_SOFTWARE, funnel_scope

    index = _index(_reacher("laptop-1", ["Chrome"], ("payments", "asset", "laptop-1")))
    business = funnel_scope(profile=_profile(), index=index, stack=[], today=_TODAY)["business"]
    assert business["configured"] is True and business["labels"] is None
    assert business["reason"] == SCOPE_NO_SOFTWARE


def test_scope_emptied_over_a_software_inventory_is_a_live_zero():
    from app.services.ctem_funnel import funnel_scope

    # The supporting asset runs nothing of the stack; the floor removes every primary asset.
    index = _index(_reacher("wiki-1", ["Confluence"], ("wiki", "asset", "wiki-1")))
    scope = funnel_scope(
        profile=_profile(min_primary_asset_criticality="high"), index=index, stack=_STACK, today=_TODAY
    )
    business, policy = scope["business"], scope["policy"]
    assert business["empty"] is True and "reason" not in business and business["labels"] is None
    assert policy["empty"] is True and "reason" not in policy and policy["labels"] is None
    funnel = build_funnel(known=100, exploitable_known=9, business_scope=business, policy_scope=policy)
    for key in ("business_impact", "policy"):
        layer = _layer(funnel, key)
        assert layer["state"] == LIVE and layer["count"] == 0 and "empty" not in layer
    assert _layer(funnel, "business_impact")["drop"] == 9
    assert funnel["needs_action"] == 0


def test_policy_scope_applies_exclusions_criticality_and_scores():
    from app.services.ctem_funnel import funnel_scope

    index = _index(
        _reacher("laptop-1", ["Chrome"], ("payments", "asset_group", "Laptops")),
        _reacher("db-1", ["OpenSSL"], ("payments", "asset", "db-1")),
        _reacher("wiki-1", ["nginx"], ("wiki", "asset", "wiki-1")),
    )
    profile = _profile(
        cvss_min=7.0,
        min_primary_asset_criticality="high",
        exclusions=[{"kind": "asset_group", "value": "laptops"}],
        accepted_risks=[
            {"subject": "CVE-2024-3094", "owner": "CISO", "expires_on": "2027-01-01"},
            {"subject": "CVE-2020-0001", "owner": "CISO", "expires_on": "2020-01-01"},
            {"subject": "CVE-2023-1111", "scope": "db-1", "owner": "CISO", "expires_on": "2027-01-01"},
        ],
    )
    policy = funnel_scope(profile=profile, index=index, stack=_STACK, today=_TODAY)["policy"]
    assert policy["configured"] is True and policy["basis"] == "business_impact"
    # Laptops excluded, the Wiki (low) under the floor: only OpenSSL on db-1 stays.
    assert policy["labels"] == ["OpenSSL"]
    assert policy["rules"] == ["cvss_min", "asset_group_exclusions", "accepted_risks", "min_primary_asset_criticality"]
    cvss, accepted = policy["filter_groups"]
    assert cvss["mode"] == "or" and {"key": ["x_opencti_cisa_kev"], "values": ["true"]} in cvss["filters"]
    # missing_score defaults to hold: the gate holds an unscored vulnerability, so the funnel drops it too.
    assert {"key": ["x_opencti_cvss_base_score"], "values": [], "operator": "nil"} not in cvss["filters"]
    # Only the unscoped, unexpired CVE is left out by name.
    assert accepted["filters"][0] == {"key": ["name"], "values": ["CVE-2024-3094"], "operator": "not_eq"}


def test_policy_scores_keep_unscored_vulnerabilities_only_when_missing_score_is_pass():
    from app.services.ctem_funnel import funnel_scope

    index = _index(_reacher("db-1", ["OpenSSL"], ("payments", "asset", "db-1")))
    nil_cvss = {"key": ["x_opencti_cvss_base_score"], "values": [], "operator": "nil"}
    nil_epss = {"key": ["x_opencti_epss_score"], "values": [], "operator": "nil"}
    held = funnel_scope(profile=_profile(cvss_min=7.0, epss_min=0.1), index=index, stack=_STACK, today=_TODAY)["policy"]
    cvss, epss = held["filter_groups"][:2]
    assert nil_cvss not in cvss["filters"] and nil_epss not in epss["filters"]
    passed = funnel_scope(
        profile=_profile(cvss_min=7.0, epss_min=0.1, missing_score="pass"), index=index, stack=_STACK, today=_TODAY
    )["policy"]
    cvss, epss = passed["filter_groups"][:2]
    assert nil_cvss in cvss["filters"] and nil_epss in epss["filters"]


def test_policy_without_primary_assets_narrows_the_exploitable_layer():
    from app.services.ctem_funnel import funnel_scope

    profile = _profile(exclusions=[{"kind": "product", "value": "nginx"}])
    policy = funnel_scope(profile=profile, index=_index(), stack=_STACK, today=_TODAY)["policy"]
    assert policy["basis"] == "exploitable" and policy["labels"] == ["Google Chrome", "OpenSSL"]
    assert policy["filter_groups"] == []


def test_live_scope_layers_count_and_never_contradict_the_layer_above():
    business_scope = {
        "configured": True,
        "labels": ["OpenSSL"],
        "primary_assets": 2,
        "supporting_assets": 263,
        "basis": "exploitable",
    }
    policy_scope = {
        "configured": True,
        "labels": ["OpenSSL"],
        "rules": ["cvss_min"],
        "basis": "business_impact",
        "filter_groups": [],
    }
    funnel = build_funnel(
        known=4214,
        relevant=512,
        exploitable_known=300,
        exploitable_relevant=21,
        business=12,
        business_scope=business_scope,
        policy=30,
        policy_scope=policy_scope,
    )
    business, policy = _layer(funnel, "business_impact"), _layer(funnel, "policy")
    assert business == {
        **business,
        "state": LIVE,
        "count": 12,
        "drop": 9,
        "primary_assets": 2,
        "supporting_assets": 263,
    }
    assert "labels" not in business and "filter_groups" not in policy
    # A larger policy count than the layer above is capped, never increasing.
    assert policy["count"] == 12 and funnel["capped"] is True and policy["rules"] == ["cvss_min"]
    assert funnel["needs_action"] == 12


def test_configured_scope_without_a_count_is_not_available_never_zero():
    from app.services.ctem_funnel import SCOPE_NO_SOFTWARE, SCOPE_UNREAD

    funnel = build_funnel(
        known=100,
        exploitable_known=9,
        business_scope={"configured": True, "labels": None, "reason": SCOPE_NO_SOFTWARE, "primary_assets": 1},
        policy_scope={"configured": True, "labels": ["x"], "rules": ["epss_min"]},
    )
    business, policy = _layer(funnel, "business_impact"), _layer(funnel, "policy")
    assert business["state"] == NOT_AVAILABLE and business["count"] is None and business["reason"] == SCOPE_NO_SOFTWARE
    assert business["primary_assets"] == 1 and business["category"] is None
    assert policy["state"] == NOT_AVAILABLE and policy["reason"] == SCOPE_UNREAD
    assert funnel["needs_action"] == 9


async def test_scope_counts_send_one_call_with_the_policy_filters(monkeypatch):
    from app.services.ctem_funnel import funnel_scope

    index = _index(_reacher("db-1", ["OpenSSL"], ("payments", "asset", "db-1")))
    scope = funnel_scope(profile=_profile(cvss_min=7.0), index=index, stack=_STACK, today=_TODAY)

    async def fake_read(client, base_url, headers, query, stack, **kw):
        return [{"id": "sw-1", "name": "openssl", "vendor": "openssl", "cpe": ""}], True, None

    sent = []

    async def fake_counts(client, base_url, headers, spec, user_id):
        sent.extend(spec)
        return {name: n for (name, _t, _f), n in zip(spec, (14, 6), strict=False)}

    monkeypatch.setattr(collector, "_read_stack_software", fake_read)
    monkeypatch.setattr(collector, "_octi_multi_counts", fake_counts)
    counts = await collector._octi_scope_counts(None, "http://x", {}, _STACK, scope, "u1")
    assert counts == {"business": 14, "policy": 6}
    business_filter, policy_filter = sent[0][2], sent[1][2]
    assert business_filter["filters"][0]["values"][1] == {"key": "id", "values": ["sw-1"]}
    assert len(business_filter["filterGroups"]) == 1 and len(policy_filter["filterGroups"]) == 2


async def test_scope_counts_skip_layers_without_software(monkeypatch):
    scope = {"business": {"configured": True, "labels": None}, "policy": {"configured": False}}
    assert await collector._octi_scope_counts(None, "http://x", {}, _STACK, scope, "u1") == {}


def test_open_issues_wait_for_ctem_cases():
    layer = _layer(build_funnel(known=100), "open_issues")
    assert layer["state"] == NOT_AVAILABLE and layer["count"] is None and layer["category"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"known": 100, "relevant": 50, "exploitable_known": 30, "exploitable_relevant": 5},
        {"known": 100, "relevant": 150, "exploitable_known": 30, "exploitable_relevant": 160},
        {"known": 100, "exploitable_known": 130},
        {"known": 0, "relevant": 0, "exploitable_known": 0, "exploitable_relevant": 0},
    ],
)
def test_totals_never_increase_down_the_funnel(kwargs):
    funnel = build_funnel(**kwargs)
    counts = [layer["count"] for layer in funnel["layers"] if layer["count"] is not None]
    assert counts == sorted(counts, reverse=True)
    for layer in funnel["layers"]:
        assert layer["drop"] is None or layer["drop"] >= 0
    assert funnel["capped"] is any(
        kwargs.get(k, 0) > kwargs["known"] for k in ("relevant", "exploitable_known", "exploitable_relevant")
    )


def test_drops_add_up_to_the_volume_removed():
    funnel = build_funnel(known=4214, relevant=512, exploitable_known=300, exploitable_relevant=21)
    drops = sum(layer["drop"] or 0 for layer in funnel["layers"])
    assert drops == 4214 - funnel["needs_action"]


# ── Collector wiring ─────────────────────────────────────────────────────────


def _wire(monkeypatch, *, stack, tailored, openaev, reachability=None, rows=None, scope=None, scope_counts=None):
    async def fake_stack(db):
        return stack

    async def fake_rows(db):
        return rows

    def fake_reachability(rows_):
        return reachability

    async def fake_creds(db, user_id, provider):
        return ("http://fake", {"Authorization": "x"})

    async def fake_octi(base_url, headers, user_id, client):
        return {
            "prioritization": {
                "vulnerabilities_total": 4214,
                "kev_count": 47,
                "critical_or_kev_count": 230,
                "noncritical_share_pct": 94.5,
                "funnel_inputs": {"known": 4214, "exploitable": 300},
            }
        }

    async def fake_aev(base_url, headers, user_id, client):
        return {"prioritization": {"findings_total": 120, "findings_cve": 89}}

    async def fake_tailored(client, base_url, headers, stack_, user_id):
        return tailored

    collectors = [("opencti", fake_octi)] + ([("openaev", fake_aev)] if openaev else [])
    monkeypatch.setattr(collector, "_organization_software", fake_stack)
    monkeypatch.setattr(collector, "_profile_asset_rows", fake_rows)
    monkeypatch.setattr(collector, "_profile_reachability", fake_reachability)
    monkeypatch.setattr(collector, "_user_creds", fake_creds)
    monkeypatch.setattr(collector, "_octi_kev_tailored", fake_tailored)
    monkeypatch.setattr(collector, "_VERTICAL_COLLECTORS", {"cyber": collectors})

    async def fake_scope(db, stack_):
        return scope

    async def fake_scope_counts(client, base_url, headers, stack_, scope_, user_id):
        return scope_counts or {}

    monkeypatch.setattr(collector, "_funnel_scope", fake_scope)
    monkeypatch.setattr(collector, "_octi_scope_counts", fake_scope_counts)


@pytest.mark.parametrize("openaev", [False, True])
async def test_build_posture_attaches_the_funnel_with_openaev_absent_or_present(monkeypatch, openaev):
    _wire(monkeypatch, stack=[], tailored=None, openaev=openaev)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    prio = facts["prioritization"]
    assert "funnel_inputs" not in prio and "funnel_tailored" not in prio
    funnel = prio["funnel"]
    assert _layer(funnel, "known")["asset_findings"] == (89 if openaev else None)
    assert _layer(funnel, "relevant")["state"] == NOT_CONFIGURED
    assert _layer(funnel, "exploitable")["count"] == 300


async def test_build_posture_reads_the_profile_assets_into_the_exposed_layer(monkeypatch):
    _wire(monkeypatch, stack=[], tailored=None, openaev=True, reachability=_SPLIT)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["state"] == NOT_AVAILABLE and exposed["reachability"] == _SPLIT
    assert exposed["reason"] == EXPOSED_NOT_LINKED


async def test_build_posture_feeds_the_business_and_policy_layers(monkeypatch):
    scope = {
        "business": {"configured": True, "labels": ["OpenSSL"], "primary_assets": 3, "supporting_assets": 263},
        "policy": {"configured": True, "labels": ["OpenSSL"], "rules": ["cvss_min"], "filter_groups": []},
    }
    _wire(
        monkeypatch,
        stack=_STACK,
        tailored=None,
        openaev=False,
        scope=scope,
        scope_counts={"business": 40, "policy": 11},
    )
    funnel = (await collector.build_posture_for_user(None, "u1", "cyber"))[0]["prioritization"]["funnel"]
    assert (
        _layer(funnel, "business_impact")["count"] == 40
        and _layer(funnel, "business_impact")["supporting_assets"] == 263
    )
    assert _layer(funnel, "policy")["count"] == 11 and funnel["needs_action"] == 11


async def test_funnel_scope_is_none_without_a_session():
    assert await collector._funnel_scope(None, _STACK) is None


async def test_profile_reachability_is_none_without_a_session():
    assert await collector._profile_asset_rows(None) is None
    assert collector._profile_reachability(None) is None


# ── The Exposed join (#12351) ────────────────────────────────────────────────


class _Resp:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _finding(cve, *asset_ids):
    return {
        "finding_id": f"{cve}@{'+'.join(asset_ids) or 'unknown'}",
        "finding_type": "cve",
        "finding_value": cve,
        "finding_assets": [{"asset_id": a, "asset_name": f"host-{a}"} for a in asset_ids],
    }


_JOIN_ASSETS = [
    {
        "name": "db",
        "category": "host",
        "subcategory": "server",
        "internet_facing": "no",
        "source": "openaev",
        "external_id": "srv",
    },
    {
        "name": "pc",
        "category": "host",
        "subcategory": "workstation",
        "internet_facing": "no",
        "source": "openaev",
        "external_id": "ws",
    },
]


def _wire_join(monkeypatch, findings, *, named_total=1, total=None):
    sent: list[dict] = []

    async def fake_api(base_url, headers, method, path, action, *, json=None, params=None, client=None, **kw):
        if path == "/api/findings/search" and (json or {}).get("size") == collector._EXPOSED_PAGE:
            return _Resp({"content": findings, "totalElements": total or len(findings), "totalPages": 1}), None
        return _Resp({"content": [], "totalElements": 89}), None

    async def fake_counts(client, base_url, headers, specs, user_id):
        sent.extend(f for _k, _t, f in specs)
        return {key: named_total for key, _t, _f in specs}

    _wire(monkeypatch, stack=[], tailored=None, openaev=True, reachability=_SPLIT, rows=(_JOIN_ASSETS, []))
    monkeypatch.setattr(collector, "_api", fake_api)
    monkeypatch.setattr(collector, "_octi_multi_counts", fake_counts)
    return sent


async def test_exposed_layer_is_live_when_the_findings_join_the_profile_assets(monkeypatch):
    # CVE-1 sits only on a proven-unreachable server: it drops. CVE-2 sits on a workstation
    # (always reachable) and the server: it stays. CVE-3 sits on an asset the profile does not hold.
    findings = [_finding("CVE-1", "srv"), _finding("CVE-2", "srv", "ws"), _finding("CVE-3", "other")]
    sent = _wire_join(monkeypatch, findings, named_total=1)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    # The fake answers 1 per count: CVE-2 and CVE-3 are exposed, all three observed.
    assert exposed["state"] == LIVE and exposed["count"] == 1
    assert exposed["reachability"] == _SPLIT
    # One count of the exposed names, one of the observed names, under the exploitable layer's filter.
    assert sent == [
        {"mode": "and", "filters": [{"key": ["name"], "values": names}], "filterGroups": [collector._F_EXPLOITABLE]}
        for names in (["CVE-2", "CVE-3"], ["CVE-1", "CVE-2", "CVE-3"])
    ]


async def test_exposed_layer_counts_only_vulnerabilities_found_on_an_asset(monkeypatch):
    # #2268 (partner gap abb45061): a vulnerability of the layer found on no asset is never exposed.
    sent = _wire_join(monkeypatch, [_finding("CVE-2", "ws")])
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["state"] == LIVE and exposed["count"] == 1 and exposed["observed"] == 1
    assert exposed["drop"] == 299
    named = {"key": ["name"], "values": ["CVE-2"]}
    assert sent == [{"mode": "and", "filters": [named], "filterGroups": [collector._F_EXPLOITABLE]}] * 2


async def test_exposed_layer_tells_observed_apart_from_exposed(monkeypatch):
    # CVE-1 only on the unreachable server: observed, not exposed. CVE-2 on the workstation: both.
    sent = _wire_join(monkeypatch, [_finding("CVE-1", "srv"), _finding("CVE-2", "ws")], named_total=1)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["count"] == 1
    assert [f["filters"][0]["values"] for f in sent] == [["CVE-2"], ["CVE-1", "CVE-2"]]


async def test_exposed_layer_is_zero_when_no_vulnerability_is_found_on_an_asset(monkeypatch):
    sent = _wire_join(monkeypatch, [_finding("CVE-9")])
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["state"] == LIVE and exposed["count"] == 0 and exposed["observed"] == 0
    assert sent == []


async def test_an_unknown_location_finding_never_counts_in_the_exposed_layer(monkeypatch):
    # OpenAEV #9233: a CVE found on no asset (a user or unknown location) is no exposure (gap abb45061).
    # CVE-1 on the unreachable server is observed, not exposed; the same CVE with no asset does not keep it in.
    findings = [_finding("CVE-1", "srv"), _finding("CVE-1"), _finding("CVE-9")]
    sent = _wire_join(monkeypatch, findings, named_total=1)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["state"] == LIVE and exposed["count"] == 0 and exposed["observed"] == 1
    assert sent == [
        {"mode": "and", "filters": [{"key": ["name"], "values": ["CVE-1"]}], "filterGroups": [collector._F_EXPLOITABLE]}
    ]


async def test_the_exposed_join_reads_open_cve_findings_only(monkeypatch):
    bodies: list[dict] = []

    async def fake_api(base_url, headers, method, path, action, *, json=None, **kw):
        bodies.append(json or {})
        return _Resp({"content": [], "totalElements": 0, "totalPages": 0}), None

    monkeypatch.setattr(collector, "_api", fake_api)
    assert await collector._aev_cve_assets("https://aev", {}, None) == {}
    keys = {f["key"]: f["values"] for f in bodies[0]["filterGroup"]["filters"]}
    assert keys == {"finding_type": ["cve"], "finding_status": ["OPEN"]}


async def test_exposed_layer_stays_not_available_past_the_finding_cap(monkeypatch):
    _wire_join(monkeypatch, [_finding("CVE-1", "srv")], total=collector._EXPOSED_FINDING_CAP + 1)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    exposed = _layer(facts["prioritization"]["funnel"], "exposed")
    assert exposed["state"] == NOT_AVAILABLE and exposed["reason"] == EXPOSED_NOT_LINKED


def test_exposed_basis_follows_the_last_live_layer():
    assert collector._exposed_basis({"known": 10, "exploitable": 4}, {}) == (4, collector._F_EXPLOITABLE)
    assert collector._exposed_basis({"known": 10}, {}) == (10, None)
    tailored = {"relevant": 7, "exploitable": 2, "filters": {"relevant": {"r": 1}, "exploitable": {"e": 1}}}
    assert collector._exposed_basis({"known": 10, "exploitable": 4}, tailored) == (2, {"e": 1})
    # A tailored layer too wide to carry a filter cannot be joined.
    assert collector._exposed_basis({"known": 10}, {"relevant": 7, "exploitable": 2, "filters": {}}) is None


async def test_build_posture_tailors_the_funnel_to_the_stack(monkeypatch):
    tailored = {
        "kev_tailored_count": 3,
        "kev_tailored_products": ["Fortinet FortiOS"],
        "funnel_tailored": {"relevant": 512, "exploitable": 21},
    }
    _wire(monkeypatch, stack=[{"label": "Fortinet FortiOS"}], tailored=tailored, openaev=False)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    prio = facts["prioritization"]
    assert prio["kev_tailored_count"] == 3
    assert [layer["count"] for layer in prio["funnel"]["layers"][:3]] == [4214, 512, 21]


async def test_without_a_tailored_count_the_exploitable_link_is_the_generic_filter(monkeypatch):
    """#11954: a not configured ``relevant`` never lends its tailored filter to a generic count."""
    tailored = {"funnel_tailored": {"relevant": None, "exploitable": None, "filters": {"relevant": {"x": 1}, "exploitable": {"x": 2}}}}
    _wire(monkeypatch, stack=[{"label": "Fortinet FortiOS"}], tailored=tailored, openaev=False)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    funnel = facts["prioritization"]["funnel"]
    assert _layer(funnel, "relevant")["state"] == NOT_CONFIGURED
    assert _layer(funnel, "relevant")["filters"] is None
    exploitable = _layer(funnel, "exploitable")
    assert exploitable["basis"] == "known"
    assert exploitable["filters"] == collector._F_EXPLOITABLE
    assert exploitable["filters"]["mode"] == "or" and len(exploitable["filters"]["filters"]) == 2


async def test_a_tailored_count_never_links_with_the_generic_filter(monkeypatch):
    """#11954: a stack too wide for a link leaves the tailored layers without a link, never the generic list."""
    tailored = {"funnel_tailored": {"relevant": 512, "exploitable": 21, "filters": {"relevant": None, "exploitable": None}}}
    _wire(monkeypatch, stack=[{"label": "Fortinet FortiOS"}], tailored=tailored, openaev=False)
    facts, _ = await collector.build_posture_for_user(None, "u1", "cyber")
    funnel = facts["prioritization"]["funnel"]
    assert _layer(funnel, "exploitable")["basis"] == "relevant"
    assert _layer(funnel, "exploitable")["filters"] is None
    assert _layer(funnel, "relevant")["filters"] is None


def test_the_tailored_filters_ride_with_the_tailored_counts():
    links = {"relevant": {"mode": "and"}, "exploitable": {"mode": "and", "filterGroups": [collector._F_EXPLOITABLE]}}
    assert collector._funnel_link_filters({"relevant": 512, "exploitable": 21, "filters": links}) == links
    assert collector._funnel_link_filters({}) == {"exploitable": collector._F_EXPLOITABLE}


async def test_the_funnel_changes_no_score_and_no_snapshot_metric(monkeypatch):
    """Additive: the stage scores and the history metrics read exactly what they read before."""
    _wire(monkeypatch, stack=[], tailored=None, openaev=True)
    facts, present = await collector.build_posture_for_user(None, "u1", "cyber")
    without = {stage: {k: v for k, v in f.items() if k != "funnel"} for stage, f in facts.items()}
    assert compute_posture(facts, present, "cyber") == compute_posture(without, present, "cyber")
    assert _numeric_metrics(facts["prioritization"]) == _numeric_metrics(without["prioritization"])


def test_the_prioritization_viz_carries_the_funnel_and_keeps_the_severity_bars():
    viz = _stage_viz(
        "prioritization", {"severity_distribution": [{"label": "high", "value": 3}], "funnel": {"layers": []}}, "cyber"
    )
    assert viz["severity_distribution"] == [{"label": "high", "value": 3}]
    assert viz["funnel"] == {"layers": []}
    assert _stage_viz("prioritization", {}, "cyber")["funnel"] is None


def test_the_exploitable_count_is_last_so_older_counts_keep_their_place():
    assert collector._CYBER_COUNT_SPECS[-1][0] == "exploitable"
    keys = [key for key, _, _ in collector._CYBER_COUNT_SPECS]
    assert keys.index("critical_or_kev") < keys.index("exploitable")
