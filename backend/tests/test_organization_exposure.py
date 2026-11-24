"""Assets and exposure, prioritization policy, enterprise knowledge in the organization profile (#11543).

Non-regression first: a profile saved before #11543 parses, dumps and renders
for an agent exactly as before; then the new parts, the CSV import (preview,
per-row errors, primary asset links, merge) and the knowledge search scoped to
the knowledge bases the caller can read.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_assets_import as imp
from app.services import organization_context as oc
from app.services import organization_enterprise_knowledge as ek

#: A profile as stored before #11543 (every field it had then).
LEGACY = {
    "name": "Acme Energy",
    "sector": "energy",
    "sub_sector": "",
    "regions": ["FR"],
    "size": "1001-5000",
    "primary_assets": [
        {"name": "SCADA", "business_service": "Grid", "owner": "OT", "criticality": "critical", "opencrq_asset_id": ""}
    ],
    "tech_stack": [{"product": "Windows Server 2019", "vendor": "Microsoft", "cpe": ""}],
    "pir_ids": ["pir-1"],
    "pir_names": {"pir-1": "Ransomware"},
    "risk_appetite": {"annual_loss_threshold": 2000000.0, "approval_threshold": None},
    "loaded_hourly_rate": None,
    "currency": "EUR",
    "ticketing": {"integration": "", "project": ""},
    "onboarding_skipped": [],
}


def _row(profile: dict, version: int = 4):
    return SimpleNamespace(
        organization_id=None,  # no asset records read: the session is a stub
        version=version,
        profile=profile,
        source="form",
        created_at=datetime(2026, 11, 13, 8, 0, tzinfo=UTC),
    )


# ── Non-regression ──────────────────────────────────────────────────────────


def test_a_legacy_profile_reads_back_identical_through_the_legacy_dump():
    profile = oc.parse_profile(LEGACY)
    assert oc.legacy_dump(profile) == LEGACY


def test_the_new_parts_default_to_nothing_on_a_legacy_profile():
    profile = oc.parse_profile(LEGACY)
    assert profile.assets_exposure == []
    assert profile.enterprise_knowledge == []
    assert profile.prioritization_policy == oc.PrioritizationPolicy()
    assert not oc.policy_is_set(profile.prioritization_policy)
    assert profile.asset_groups == []
    assert oc.exposure_counts(profile) == {
        "assets_exposure": 0,
        "asset_groups": 0,
        "prioritization_policy": 0,
        "enterprise_knowledge": 0,
    }


def test_the_missing_fields_are_unchanged_by_the_new_parts():
    profile = oc.parse_profile(LEGACY)
    filled = oc.parse_profile(
        {**LEGACY, "assets_exposure": [{"name": "web-1"}], "prioritization_policy": {"cvss_min": 7}}
    )
    assert oc.missing_fields(profile) == oc.missing_fields(filled)
    for field in oc.EXPOSURE_FIELDS:
        assert field not in dict(oc.CONTRACT_FIELDS)


def test_the_agent_block_of_a_legacy_profile_carries_no_new_text():
    text = oc.render_for_agent(_row(LEGACY))
    assert "assets_exposure" not in text
    assert "prioritization_policy" not in text
    assert "enterprise_knowledge" not in text
    assert "asset_names" not in text
    assert "Also filled" not in text


# ── Posture target (#11547 S2e) ─────────────────────────────────────────────


def test_a_legacy_profile_has_no_posture_target_and_dumps_as_before():
    profile = oc.parse_profile(LEGACY)
    assert profile.posture_target is None
    assert "posture_target" not in oc.legacy_dump(profile)


def test_the_posture_target_is_a_score_and_stays_out_of_the_agent_block():
    profile = oc.parse_profile({**LEGACY, "posture_target": 75})
    assert profile.posture_target == 75
    assert oc.legacy_dump(profile) == LEGACY
    assert "posture_target" not in oc.render_for_agent(_row({**LEGACY, "posture_target": 75}))
    assert "posture_target" not in dict(oc.CONTRACT_FIELDS)
    assert oc.missing_fields(profile) == oc.missing_fields(oc.parse_profile(LEGACY))
    for bad in (-1, 101):
        with pytest.raises(ValueError):
            oc.parse_profile({**LEGACY, "posture_target": bad})


def test_the_agent_block_announces_the_filled_parts_by_one_line_never_their_rows():
    assets = [{"name": f"host-{i}", "internet_facing": "yes"} for i in range(300)]
    filled = {**LEGACY, "assets_exposure": assets, "prioritization_policy": {"cvss_min": 7, "epss_min": 0.1}}
    legacy_text = oc.render_for_agent(_row(LEGACY))
    text = oc.render_for_agent(_row(filled))
    assert "host-1" not in text
    added = text[len(legacy_text) :]
    assert added.strip().startswith("Also filled, read by tool: assets_exposure (300), prioritization_policy (2)")
    assert len(added) < 200


# ── Assets and exposure ─────────────────────────────────────────────────────


def test_an_asset_validates_its_vocabularies():
    asset = oc.AssetEntry(name=" web-1 ", tech_stack=["nginx", "nginx", " "], tags=["dmz"])
    assert asset.name == "web-1"
    assert asset.internet_facing == "unknown"
    assert asset.tech_stack == ["nginx"]
    with pytest.raises(ValueError):
        oc.AssetEntry(name="x", internet_facing="maybe")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        oc.AssetEntry.model_validate({"name": "x", "type": "printer"})
    with pytest.raises(ValueError):
        oc.AssetEntry(name="x", category="printer")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="not a subcategory of host"):
        oc.AssetEntry(name="x", category="host", subcategory="database")
    with pytest.raises(ValueError):
        oc.AssetEntry(name="x", criticality="critical")  # type: ignore[arg-type]


# ── OpenAEV taxonomy (#11781) ───────────────────────────────────────────────


def test_the_taxonomy_is_openaevs_thirteen_categories_and_each_subcategory_has_one_category():
    assert len(oc.ASSET_CATEGORIES) == 13
    assert oc.ASSET_CATEGORIES[-1] == "generic_asset"
    assert sum(len(subs) for subs in oc.ASSET_TAXONOMY.values()) == len(oc.ASSET_SUBCATEGORIES) == 85
    assert oc.ASSET_SUBCATEGORIES["database"] == "cloud_resource"
    assert oc.ASSET_SUBCATEGORIES["edr"] == "security_platform"
    assert oc.ASSET_CRITICALITIES == ("unknown", "low", "medium", "high", "very_high")


def test_a_repository_is_a_source_code_asset():
    # xtm-one #1233: additive, the exact values of OpenAEV's AssetSubCategory naming.
    assert oc.ASSET_TAXONOMY["source_code"] == (
        "code_repository",
        "ci_cd_pipeline",
        "package_registry",
        "software_package",
        "code_other",
    )
    repo = oc.AssetEntry(name="xtm-one", category="source_code", subcategory="code_repository")
    assert (repo.category, repo.subcategory) == ("source_code", "code_repository")
    assert oc.AssetEntry.model_validate({"name": "a", "type": "ci_cd_pipeline"}).category == "source_code"
    with pytest.raises(ValueError, match="not a subcategory of source_code"):
        oc.AssetEntry(name="x", category="source_code", subcategory="container_image")


@pytest.mark.parametrize(
    ("legacy", "category", "subcategory"),
    [
        ("host", "host", ""),
        ("application", "web_application", ""),
        ("cloud_service", "cloud_resource", ""),
        ("database", "cloud_resource", "database"),
        ("network", "network_device", ""),
        ("endpoint", "host", "workstation"),
        ("identity", "identity", ""),
        ("other", "generic_asset", ""),
    ],
)
def test_every_legacy_type_reads_as_its_category(legacy: str, category: str, subcategory: str):
    asset = oc.AssetEntry.model_validate({"name": "a", "type": legacy, "owner": "Ops", "tags": ["t"]})
    assert (asset.category, asset.subcategory, asset.owner, asset.tags) == (category, subcategory, "Ops", ["t"])
    assert "type" not in asset.model_dump()


def test_an_empty_legacy_type_was_a_host_and_a_category_or_subcategory_word_is_read_as_such():
    assert oc.AssetEntry.model_validate({"name": "a"}).category == "generic_asset"
    assert oc.upgrade_legacy_type({"name": "a", "type": ""})["category"] == "host"
    assert oc.AssetEntry.model_validate({"name": "a", "type": "saas_application"}).category == "saas_application"
    firewall = oc.AssetEntry.model_validate({"name": "a", "type": "firewall"})
    assert (firewall.category, firewall.subcategory) == ("network_device", "firewall")


def test_a_legacy_profile_moves_its_asset_group_rows_to_asset_groups_losing_nothing():
    stored = {
        **LEGACY,
        "primary_assets": [{**LEGACY["primary_assets"][0], "asset_names": ["DMZ", "db-1"]}],
        "assets_exposure": [
            {
                "name": "DMZ",
                "type": "asset_group",
                "environment": "production",
                "internet_facing": "yes",
                "owner": "Network",
                "group": "Perimeter",
                "tech_stack": ["nginx"],
                "tags": ["edge"],
                "source": "csv",
                "external_id": "g-1",
            },
            {"name": "Perimeter", "type": "asset_group"},
            {"name": "db-1", "type": "database", "group": "DMZ", "internet_facing": "no"},
        ],
    }
    profile = oc.parse_profile(stored)
    assert [a.name for a in profile.assets_exposure] == ["db-1"]
    assert (profile.assets_exposure[0].category, profile.assets_exposure[0].subcategory) == (
        "cloud_resource",
        "database",
    )
    dmz = profile.asset_groups[0]
    assert (dmz.name, dmz.environment, dmz.internet_facing, dmz.owner, dmz.source, dmz.external_id) == (
        "DMZ",
        "production",
        "yes",
        "Network",
        "csv",
        "g-1",
    )
    assert dmz.tech_stack == ["nginx"] and dmz.tags == ["edge", "group:Perimeter"]
    assert [g.name for g in profile.asset_groups] == ["DMZ", "Perimeter"]
    assert imp.check_links(profile) == []
    again = oc.parse_profile(json.loads(json.dumps(profile.model_dump(mode="json"))))
    assert again == profile


def test_the_profile_round_trips_assets_links_policy_and_knowledge():
    data = {
        **LEGACY,
        "primary_assets": [{**LEGACY["primary_assets"][0], "asset_names": ["DMZ", "web-1", "web-1"]}],
        "assets_exposure": [
            {"name": "web-1", "group": "DMZ", "environment": "production", "tech_stack": ["nginx"]},
        ],
        "asset_groups": [{"name": "DMZ", "internet_facing": "yes"}],
        "prioritization_policy": {
            "cvss_min": 7.5,
            "epss_min": 0.2,
            "internet_exposure_required": True,
            "min_primary_asset_criticality": "high",
            "exclusions": [{"kind": "tag", "value": "lab", "reason": "test systems"}],
            "accepted_risks": [{"subject": "CVE-2024-0001", "owner": "CISO", "expires_on": "2027-01-31"}],
        },
        "enterprise_knowledge": [{"knowledge_base_id": "8a0b3c1e-0000-4000-8000-000000000001", "kind": "patch_policy"}],
    }
    profile = oc.parse_profile(data)
    assert profile.primary_assets[0].asset_names == ["DMZ", "web-1"]
    assert profile.prioritization_policy.accepted_risks[0].expires_on == date(2027, 1, 31)
    again = oc.parse_profile(json.loads(json.dumps(profile.model_dump(mode="json"))))
    assert again == profile
    assert imp.check_links(profile) == []
    assert oc.exposure_counts(profile) == {
        "assets_exposure": 1,
        "asset_groups": 1,
        "prioritization_policy": 6,
        "enterprise_knowledge": 1,
    }


def test_a_policy_refuses_out_of_range_scores_and_an_accepted_risk_without_owner_or_expiry():
    with pytest.raises(ValueError):
        oc.PrioritizationPolicy(cvss_min=11)
    with pytest.raises(ValueError):
        oc.PrioritizationPolicy(epss_min=1.5)
    with pytest.raises(ValueError):
        oc.AcceptedRisk.model_validate({"subject": "CVE-1", "expires_on": "2027-01-01"})
    with pytest.raises(ValueError):
        oc.AcceptedRisk.model_validate({"subject": "CVE-1", "owner": "CISO"})


def test_the_recommended_policy_is_a_valid_policy_that_is_never_the_default():
    policy = oc.PrioritizationPolicy.model_validate(oc.RECOMMENDED_POLICY)
    assert oc.policy_is_set(policy)


def test_links_to_unknown_assets_or_groups_are_named():
    profile = oc.parse_profile(
        {
            **LEGACY,
            "primary_assets": [{"name": "SCADA", "asset_names": ["ghost"]}],
            "assets_exposure": [{"name": "web-1", "group": "nowhere"}],
        }
    )
    problems = imp.check_links(profile)
    assert any("'ghost'" in p for p in problems)
    assert any("'nowhere'" in p for p in problems)


# ── CSV import ──────────────────────────────────────────────────────────────

CSV = (
    b"name,type,environment,internet_facing,owner,group,tech_stack,tags,primary_assets\n"
    b"DMZ,asset_group,production,yes,Network,,,,SCADA\n"
    b"web-1,host,production,y,Web team,DMZ,nginx;OpenSSL,dmz|web,SCADA;Billing\n"
    b"db-1,database,PRODUCTION,no,DBA,,PostgreSQL,,\n"
    b"bad-1,printer,,yes,,,,,\n"
    b"bad-2,host,,maybe,,,,,\n"
    b"web-1,host,,yes,,,,,\n"
    b",,,,,,,,\n"
)


def test_the_csv_preview_parses_rows_names_errors_and_links_without_writing():
    profile = oc.parse_profile(LEGACY)
    preview = imp.parse_assets_csv(CSV, profile)
    assert [a.name for a in preview.assets] == ["web-1", "db-1"]
    assert [(g.name, g.internet_facing, g.owner) for g in preview.groups] == [("DMZ", "yes", "Network")]
    assert (preview.assets[1].category, preview.assets[1].subcategory) == ("cloud_resource", "database")
    web = preview.assets[0]
    assert (web.internet_facing, web.group, web.tech_stack, web.tags, web.source) == (
        "yes",
        "DMZ",
        ["nginx", "OpenSSL"],
        ["dmz", "web"],
        "csv",
    )
    assert preview.assets[1].environment == "production"
    assert [(e.line, e.name) for e in preview.errors] == [(5, "bad-1"), (6, "bad-2"), (7, "web-1")]
    assert "internet_facing" in preview.errors[1].message
    assert preview.links == {"SCADA": ["DMZ", "web-1"]}
    assert preview.unknown_primary_assets == ["Billing"]
    assert profile.assets_exposure == []
    good = b"name,type,internet_facing\nDMZ,asset_group,yes\nweb-1,host,yes\n"
    merged = imp.merge_assets(profile, imp.parse_assets_csv(good, profile))
    assert [g.name for g in merged.asset_groups] == ["DMZ"]
    assert [a.name for a in merged.assets_exposure] == ["web-1"]


def test_the_csv_reads_category_subcategory_and_criticality_and_names_a_bad_pair():
    data = (
        b"name,category,subcategory,criticality,internet_facing\n"
        b"fw-1,network_device,firewall,Very High,yes\n"
        b"bot,ai_target,,high,no\n"
        b"bad,host,database,,\n"
        b"bad-crit,host,,critical,\n"
    )
    preview = imp.parse_assets_csv(data, oc.parse_profile(LEGACY))
    assert [(a.name, a.category, a.subcategory, a.criticality) for a in preview.assets] == [
        ("fw-1", "network_device", "firewall", "very_high"),
        ("bot", "ai_target", "", "high"),
    ]
    assert [(e.line, e.name) for e in preview.errors] == [(4, "bad"), (5, "bad-crit")]
    assert "not a subcategory of host" in preview.errors[0].message


def test_the_csv_merge_upserts_by_name_keeps_other_rows_and_adds_the_links():
    profile = oc.parse_profile(
        {
            **LEGACY,
            "assets_exposure": [
                {"name": "web-1", "owner": "old"},
                {"name": "laptop-7", "type": "endpoint", "source": "manual"},
            ],
            "asset_groups": [
                {"name": "DMZ"},
            ],
        }
    )
    good = b"name,internet_facing,primary_assets\nweb-1,yes,SCADA\ndb-1,no,\n"
    preview = imp.parse_assets_csv(good, profile)
    assert preview.errors == []
    assert preview.replaced == ["web-1"]
    merged = imp.merge_assets(profile, preview)
    assert [a.name for a in merged.assets_exposure] == ["laptop-7", "web-1", "db-1"]
    assert (merged.assets_exposure[0].category, merged.assets_exposure[0].subcategory) == ("host", "workstation")
    assert [g.name for g in merged.asset_groups] == ["DMZ"]
    assert merged.assets_exposure[1].owner == ""
    assert merged.primary_assets[0].asset_names == ["web-1"]
    assert imp.check_links(merged) == []
    assert oc.legacy_dump(merged)["tech_stack"] == LEGACY["tech_stack"]


@pytest.mark.parametrize(
    ("data", "needle"),
    [
        (b"host,owner\nweb,x\n", "no name column"),
        (b"name,colour\nweb,red\n", "unknown column"),
        (b"\xff\xfe\x00", "not UTF-8"),
    ],
)
def test_a_bad_csv_file_is_refused_with_its_reason(data, needle):
    with pytest.raises(ValueError, match=needle):
        imp.parse_assets_csv(data, oc.parse_profile(LEGACY))


# ── Enterprise knowledge ────────────────────────────────────────────────────


def _links(*readable: bool) -> list[ek.LinkedKnowledgeBase]:
    return [
        ek.LinkedKnowledgeBase(knowledge_base_id=f"kb-{i}", label=f"KB {i}", kind="sop", readable=r)
        for i, r in enumerate(readable)
    ]


def test_the_knowledge_search_reads_only_the_knowledge_bases_the_caller_can_read():
    seen: list[list[str]] = []

    async def search(query: str, limit: int, kb_ids: list[str]) -> str:
        seen.append(kb_ids)
        return "passage"

    text = asyncio.run(ek.search_passages(_links(True, False, True), "patch window", search=search))
    assert seen == [["kb-0", "kb-2"]]
    assert "1 linked knowledge base(s) are not readable by you" in text
    assert text.endswith("passage")


def test_the_knowledge_search_with_nothing_readable_searches_nothing():
    async def search(query: str, limit: int, kb_ids: list[str]) -> str:  # pragma: no cover - must not run
        raise AssertionError("searched")

    text = asyncio.run(ek.search_passages(_links(False), "patch window", search=search))
    assert "no linked knowledge base you can read" in text


def test_the_knowledge_category_without_a_query_names_each_link_and_its_state():
    text = ek.render_links(_links(True, False))
    assert "KB 0 (sop): searchable" in text
    assert "KB 1 (sop): not readable by you" in text
    assert ek.render_links([]).startswith("Category enterprise_knowledge: not filled")


# ── The three categories in the #11533 registry and tool (S2) ───────────────


FILLED = {
    **LEGACY,
    "assets_exposure": [
        {"name": f"web-{i}", "type": "host", "internet_facing": "yes" if i % 2 else "no"} for i in range(120)
    ],
    "prioritization_policy": {
        "cvss_min": 7,
        "kev_always_in": True,
        "exclusions": [{"kind": "product", "value": "Legacy CRM"}],
        "accepted_risks": [{"subject": "CVE-2024-0001", "owner": "CISO", "expires_on": "2027-01-31"}],
    },
    "enterprise_knowledge": [{"knowledge_base_id": "8a0b3c1e-0000-4000-8000-000000000001", "kind": "sop"}],
}


def test_the_three_categories_are_in_the_registry_with_their_counts() -> None:
    from app.services import organization_context_categories as cats
    from app.services import organization_context_injection as inj

    for name in ("assets_exposure", "prioritization_policy", "enterprise_knowledge"):
        assert cats.get(name) is not None, name
    counts = inj.category_counts(oc.parse_profile(FILLED))
    expected = oc.exposure_counts(oc.parse_profile(FILLED))
    assert (
        {name: counts[name] for name in expected}
        == expected
        == {
            "assets_exposure": 120,
            "asset_groups": 0,
            "prioritization_policy": 3,
            "enterprise_knowledge": 1,
        }
    )
    legacy = inj.category_counts(oc.parse_profile(LEGACY))
    assert legacy["assets_exposure"] == legacy["prioritization_policy"] == legacy["enterprise_knowledge"] == 0


def test_the_basic_block_indexes_the_new_categories_by_one_line_and_keeps_its_cap() -> None:
    from app.services import organization_context_injection as inj

    text = inj.render_summary(_row(FILLED), tracked_count=0)
    assert "- assets_exposure (assets and exposure): 120 rows;" in text
    assert "- prioritization_policy (prioritization policy): 3 rows;" in text
    assert "- enterprise_knowledge (procedures and reference documents, read by query): 1 row;" in text
    assert "web-7" not in text and "CVE-2024-0001" not in text, "rows never in the basic block"
    assert len(text) <= inj.SUMMARY_MAX_CHARS
    legacy = inj.render_summary(_row(LEGACY), tracked_count=0)
    assert "- assets_exposure (assets and exposure): not filled" in legacy


def test_the_policy_rows_are_its_explicit_rules_only() -> None:
    from app.services import organization_context_categories as cats

    rows = cats.policy_rows(oc.parse_profile(FILLED))
    assert rows[0] == {"rule": "cvss_min", "value": 7.0}
    assert [r["rule"] for r in rows] == ["cvss_min", "exclusion", "accepted_risk"]
    assert rows[2]["owner"] == "CISO" and rows[2]["expires_on"] == "2027-01-31"
    assert cats.policy_rows(oc.parse_profile(LEGACY)) == []


class _Session:
    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


_Call = Callable[..., Coroutine[Any, Any, str]]


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> Callable[..., _Call]:
    import app.core.database as database
    from app.services import organizations as orgs
    from app.services.builtin_tools import ORGANIZATION_CONTEXT_TOOL, build_organization_context_tool
    from app.tools.base import ToolCall

    async def _current(_db: object, organization_id: object = None) -> Any:
        return _row(FILLED)

    async def _list(_db: object, **_kw: object) -> list[Any]:
        return []

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(oc, "current_version", _current)
    monkeypatch.setattr(orgs, "list_organizations", _list)

    def build(user_id: str | None = None) -> _Call:
        _, executor = build_organization_context_tool(user_id=user_id)

        async def call(**arguments: Any) -> str:
            result = await executor.execute_tool(ToolCall(id="1", name=ORGANIZATION_CONTEXT_TOOL, arguments=arguments))
            return str(result.content)

        return call

    return build


def test_the_assets_category_pages_and_filters_by_query(tool: Callable[..., _Call]) -> None:
    call = tool()
    first = asyncio.run(call(category="assets_exposure", limit=50))
    assert "rows 1-50 of 120" in first and "offset=50" in first
    last = asyncio.run(call(category="assets_exposure", offset=100, limit=50))
    assert "rows 101-120 of 120" in last and "End of the category." in last
    found = asyncio.run(call(category="assets_exposure", query="web-11"))
    assert "of 11 matching 'web-11'" in found  # web-11 and web-110 to web-119


def test_the_policy_category_answers_its_rules_and_a_query_narrows_them(tool: Callable[..., _Call]) -> None:
    call = tool()
    page = asyncio.run(call(category="prioritization_policy"))
    assert "rows 1-3 of 3" in page and "cvss_min" in page and "Legacy CRM" in page
    narrowed = asyncio.run(call(category="prioritization_policy", query="ciso"))
    assert "of 1 matching 'ciso'" in narrowed and "CVE-2024-0001" in narrowed


def test_the_knowledge_category_searches_only_what_the_caller_can_read(
    tool: Callable[..., _Call], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import vector

    seen: dict[str, object] = {}

    async def _linked(
        _db: object, profile: object, *, user_id: object, is_admin: bool = False
    ) -> list[ek.LinkedKnowledgeBase]:
        seen["user_id"] = user_id
        return _links(user_id is not None, False)

    def _make(_db: object, user_id: str, kb_ids: list[str], **_kw: object) -> Callable[..., Awaitable[str]]:
        seen["bound"] = list(kb_ids)

        async def _search(query: str, limit: int = 5, *, scope_kb_ids: list[str] | None = None) -> str:
            seen["scope"] = list(scope_kb_ids or [])
            return "the patch window is Sunday 02:00"

        return _search

    monkeypatch.setattr(ek, "linked_knowledge_bases", _linked)
    monkeypatch.setattr(vector, "make_search_knowledge_fn", _make)
    caller = "5f0c2a8e-0000-4000-8000-000000000042"
    text = asyncio.run(tool(caller)(category="enterprise_knowledge", query="patch window"))
    assert str(seen["user_id"]) == caller
    assert seen["bound"] == seen["scope"] == ["kb-0"], "only the readable knowledge base is searched"
    assert "Sunday 02:00" in text and "1 linked knowledge base(s) are not readable by you" in text
    listing = asyncio.run(tool(caller)(category="enterprise_knowledge"))
    assert "KB 0 (sop): searchable" in listing
    seen.clear()
    nobody = asyncio.run(tool()(category="enterprise_knowledge", query="patch window"))
    assert seen.get("scope") is None and "no linked knowledge base you can read" in nobody
