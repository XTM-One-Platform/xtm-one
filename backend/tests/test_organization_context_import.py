"""Import of one profile section from one connected platform (#11780).

The mapping, the filters and the merge are pure; the platform reads are
covered by stubbing the readers, so the rules hold without a network: the
source's count is reported, hand-typed rows are never overwritten, a re-run
updates the source's own rows only, an unreadable platform is reported.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services import organization_context_import as imp


def _profile(**kwargs) -> oc.OrganizationProfile:
    return oc.OrganizationProfile(**kwargs)


def _aev_asset(asset_id: str, name: str, **extra) -> dict:
    return {"asset_id": asset_id, "asset_name": name, **extra}


def test_every_section_has_a_field_sources_and_filters():
    assert set(imp.SECTION_SOURCES) == set(imp.SECTION_FIELD)
    for section, sources in imp.SECTION_SOURCES.items():
        for source in sources:
            assert (section, source) in imp.SOURCE_FILTERS
    # Assets need no OpenAEV: OpenCTI alone can feed them; Intune and Defender bring devices and software (#1251).
    assert imp.SECTION_SOURCES["assets"] == (
        "openaev",
        "opencti",
        "intune",
        "defender",
        "tanium",
        "crowdstrike",
        "servicenow",
        "github",
        "gitlab",
    )


def test_openaev_asset_maps_exposure_and_provenance():
    row = imp.openaev_asset(
        _aev_asset(
            "a1",
            "web-01",
            asset_internet_facing=True,
            asset_category="CLOUD_RESOURCE",
            asset_subcategory="DATABASE",
            asset_criticality="VERY_HIGH",
        )
    )
    assert (
        row.name,
        row.category,
        row.subcategory,
        row.criticality,
        row.internet_facing,
        row.source,
        row.external_id,
    ) == ("web-01", "cloud_resource", "database", "very_high", "yes", "openaev", "a1")
    # A subcategory of another category is dropped; an unknown category reads generic.
    assert imp.openaev_asset(_aev_asset("a5", "z", asset_category="HOST", asset_subcategory="ROUTER")).subcategory == ""
    assert imp.openaev_asset(_aev_asset("a6", "w", asset_category="WHATEVER")).category == "generic_asset"
    assert imp.openaev_asset(_aev_asset("a7", "v", endpoint_platform="Linux")).category == "host"
    assert imp.openaev_asset(_aev_asset("a2", "db", asset_internet_facing=False)).internet_facing == "no"
    assert imp.openaev_asset(_aev_asset("a3", "x")).internet_facing == "unknown"
    assert imp.openaev_asset(_aev_asset("", "nameless id")) is None
    # The batch's pick wins over the category; a flat type of #11543 is still read.
    picked = imp.openaev_asset(_aev_asset("a4", "y", asset_category="HOST"), "cloud_resource/database")
    assert (picked.category, picked.subcategory) == ("cloud_resource", "database")
    legacy = imp.openaev_asset(_aev_asset("a4", "y", asset_category="HOST"), "database")
    assert (legacy.category, legacy.subcategory) == ("cloud_resource", "database")


def test_openaev_endpoint_without_subcategory_takes_its_platform():
    """#12351: a Windows endpoint OpenAEV gives no subcategory is a workstation, so a user endpoint."""
    from app.services.asset_reachability import is_user_endpoint

    ws = imp.openaev_asset(_aev_asset("w1", "pc", asset_category="HOST", endpoint_platform="Windows"))
    assert (ws.category, ws.subcategory) == ("host", "workstation")
    assert is_user_endpoint(ws)
    mac = imp.openaev_asset(_aev_asset("w2", "mac", endpoint_platform="MacOS"))
    assert (mac.category, mac.subcategory) == ("host", "workstation")
    srv = imp.openaev_asset(_aev_asset("s1", "srv", asset_category="HOST", endpoint_platform="Linux"))
    assert (srv.category, srv.subcategory) == ("host", "server")
    assert not is_user_endpoint(srv)
    phone = imp.openaev_asset(_aev_asset("m1", "phone", endpoint_platform="Android"))
    assert (phone.category, phone.subcategory) == ("mobile_device", "smartphone")
    # OpenAEV's own subcategory wins over the platform.
    vm = imp.openaev_asset(
        _aev_asset("v1", "vm", asset_category="HOST", asset_subcategory="VIRTUAL_MACHINE", endpoint_platform="Windows")
    )
    assert vm.subcategory == "virtual_machine"
    # A platform that does not fit the endpoint's own category adds nothing.
    cloud = imp.openaev_asset(_aev_asset("c1", "c", asset_category="CLOUD_RESOURCE", endpoint_platform="Linux"))
    assert (cloud.category, cloud.subcategory) == ("cloud_resource", "")
    # OpenAEV's internet_facing is kept when it knows it.
    edge = imp.openaev_asset(_aev_asset("e1", "edge", endpoint_platform="Linux", asset_internet_facing=True))
    assert edge.internet_facing == "yes"
    unknown = imp.openaev_asset(_aev_asset("u1", "u", endpoint_platform="Unknown"))
    assert (unknown.category, unknown.subcategory) == ("host", "")


def _aev_row(asset: dict[str, object]) -> oc.AssetEntry:
    row = imp.openaev_asset(asset)
    assert row is not None
    return row


def test_openaev_endpoint_carries_its_os_family_and_addresses() -> None:
    """An OpenAEV Windows endpoint reads os_family Windows, the name Intune gives, with its MACs and IPs as keys."""
    row = _aev_row(
        _aev_asset(
            "w1",
            "lapt-0002",
            endpoint_platform="Windows",
            asset_mac_addresses=["40-D1-33-98-4F-63", "", None, "40-D1-33-98-4F-63"],
            asset_ips=["10.0.0.5"],
        )
    )
    assert (row.os_family, row.mac, row.ip) == ("Windows", ["40-D1-33-98-4F-63"], ["10.0.0.5"])
    assert _aev_row(_aev_asset("m1", "m", endpoint_platform="MacOS")).os_family == "macOS"
    assert _aev_row(_aev_asset("l1", "l", endpoint_platform="Linux")).os_family == "Linux"
    # A platform that names no operating system leaves the family empty.
    for platform in ("Unknown", "Generic", None):
        assert _aev_row(_aev_asset("u1", "u", endpoint_platform=platform)).os_family == ""


def test_batch_taxonomy_reads_a_category_a_pair_or_a_legacy_type():
    assert imp.batch_taxonomy("identity") == ("identity", "")
    assert imp.batch_taxonomy("host/server") == ("host", "server")
    assert imp.batch_taxonomy("other") == ("generic_asset", "")
    assert imp.batch_taxonomy("host/router") is None
    assert imp.batch_taxonomy("not-a-type") is None


async def test_openaev_inventory_adds_the_non_endpoint_assets_once(monkeypatch):
    calls: list[str] = []
    endpoint = {"asset_id": "e1", "asset_type": "endpoint", "asset_name": "srv-01", "asset_hostname": "srv-01"}
    rows = {
        "/api/endpoints/search": [endpoint],
        "/api/assets/search": [
            {**endpoint, "asset_hostname": None},
            {
                "asset_id": "w1",
                "asset_type": "asset",
                "asset_name": "app.example.io",
                "asset_category": "web_application",
            },
            {"asset_id": "t1", "asset_type": "ai_target", "asset_name": "llm"},
        ],
    }

    async def fake_search(base_url, headers, path, filter_group, text=""):
        calls.append(path)
        return imp.ReadResult(rows=list(rows[path]), total=len(rows[path]))

    monkeypatch.setattr(imp, "_openaev_search", fake_search)
    read = await imp._openaev_inventory("https://aev", {}, None)
    assert calls == ["/api/endpoints/search", "/api/assets/search"]
    assert [r["asset_id"] for r in read.rows] == ["e1", "w1"]
    assert read.rows[0]["asset_hostname"] == "srv-01"
    assert read.total == 2
    web = imp.openaev_asset(read.rows[1])
    assert web is not None and web.name == "app.example.io" and web.external_id == "w1"

    calls.clear()
    only = await imp._openaev_inventory("https://aev", {}, None, endpoints_only=True)
    assert calls == ["/api/endpoints/search"]
    assert [r["asset_id"] for r in only.rows] == ["e1"]


def test_openaev_asset_group_is_an_asset_groups_entry():
    group = imp.openaev_asset_group(
        {"asset_group_id": "g1", "asset_group_name": "dmz", "asset_group_description": "edge"}
    )
    assert isinstance(group, oc.AssetGroupEntry)
    assert (group.name, group.description, group.source, group.external_id) == ("dmz", "edge", "openaev", "g1")
    assert imp.SECTION_FIELD["asset_groups"] == "asset_groups"
    plan = imp.plan_import(_profile(), "asset_groups", "openaev", imp.ReadResult(rows=[group], total=1))
    assert plan.new == ["dmz"]
    assert [g.name for g in plan.profile.asset_groups] == ["dmz"]
    assert plan.profile.assets_exposure == []


def test_opencti_system_takes_the_batch_type_labels_and_author():
    node = {"id": "s1", "name": "SAP", "createdBy": {"name": "IT"}, "objectLabel": [{"value": "erp"}]}
    row = imp.opencti_system(node, "web_application/web_service")
    assert (row.category, row.subcategory, row.owner, row.tags, row.source, row.external_id) == (
        "web_application",
        "web_service",
        "IT",
        ["erp"],
        "opencti",
        "s1",
    )
    assert imp.opencti_system(node, "not-a-type").category == "generic_asset"


def test_a_source_code_asset_is_read_from_openaev_and_opencti_once_they_carry_it():
    # xtm-one #1233: an OpenAEV asset or OpenCTI System typed SOURCE_CODE reads as source_code.
    row = imp.openaev_asset(
        _aev_asset("r1", "xtm-one", asset_category="SOURCE_CODE", asset_subcategory="CODE_REPOSITORY")
    )
    assert (row.category, row.subcategory) == ("source_code", "code_repository")
    node = {"id": "s2", "name": "ci", "x_opencti_system_subcategory": "ci_cd_pipeline"}
    system = imp.opencti_system(node, "")
    assert (system.category, system.subcategory) == ("source_code", "ci_cd_pipeline")
    assert imp.batch_taxonomy("source_code/software_package") == ("source_code", "software_package")


def test_opencti_system_maps_the_asset_taxonomy_of_opencti_19943():
    node = {
        "id": "s2",
        "name": "web-01",
        "createdBy": {"name": "Connector"},
        "objectLabel": [{"value": "dmz"}],
        "objectMarking": [{"definition": "TLP:AMBER"}],
        "x_opencti_system_category": "HOST",
        "x_opencti_system_subcategory": "SERVER",
        "x_opencti_criticality": "VERY_HIGH",
        "x_opencti_internet_facing": "yes",
        "x_opencti_environment": "production",
        "x_opencti_owner": "Infra team",
        "x_opencti_platform": "Linux",
    }
    row = imp.opencti_system(node, "identity")
    assert (row.category, row.subcategory, row.criticality, row.internet_facing, row.environment, row.owner) == (
        "host",
        "server",
        "very_high",
        "yes",
        "production",
        "Infra team",
    )
    # The platform and the markings have no profile field: kept as tags.
    assert row.tags == ["dmz", "platform:Linux", "marking:TLP:AMBER"]
    # A subcategory of another category is dropped; a subcategory alone gives its category.
    assert imp.opencti_system({**node, "x_opencti_system_subcategory": "ROUTER"}, "").subcategory == ""
    alone = imp.opencti_system(
        {**node, "x_opencti_system_category": None, "x_opencti_system_subcategory": "ROUTER"}, ""
    )
    assert (alone.category, alone.subcategory) == ("network_device", "router")
    # No category: the batch type; no owner: the author; an unknown value: the default.
    bare = imp.opencti_system(
        {"id": "s3", "name": "x", "createdBy": {"name": "IT"}, "x_opencti_environment": "lab"}, "identity"
    )
    assert (bare.category, bare.owner, bare.environment, bare.criticality) == ("identity", "IT", "other", "unknown")
    assert imp.opencti_system_has_type(node) and not imp.opencti_system_has_type({"id": "s3"})


def test_opencti_filters_cover_the_system_taxonomy_and_the_creator():
    group = imp.opencti_filter_group(
        {"category": ["HOST"], "criticality": ["HIGH", "VERY_HIGH"], "creator": ["u1"], "internet_facing": "yes"}
    )
    keys = {tuple(f["key"]): f["values"] for f in group["filters"]}
    assert keys == {
        ("creator_id",): ["u1"],
        ("x_opencti_system_category",): ["HOST"],
        ("x_opencti_criticality",): ["HIGH", "VERY_HIGH"],
        ("x_opencti_internet_facing",): ["yes"],
    }
    assert "x_opencti_owner" in imp.opencti_systems_query(True)
    assert "x_opencti_owner" not in imp.opencti_systems_query(False)


def test_openaev_filter_group_reads_the_criticality():
    group = imp.openaev_filter_group({"criticality": ["HIGH"]})
    assert group["filters"] == [{"key": "asset_criticality", "mode": "or", "operator": "eq", "values": ["HIGH"]}]


def test_a_subcategory_option_names_its_category_from_the_vocabulary():
    rows = imp.opencti_vocabulary_options(
        "subcategory", [{"name": "SERVER", "description": "Physical or virtual server (category: HOST)"}, {"name": ""}]
    )
    assert rows == [
        {"value": "SERVER", "label": "SERVER", "description": "Physical or virtual server", "parent": "HOST"}
    ]
    options = imp.taxonomy_options(upper=True)
    assert {"value": "SERVER", "label": "SERVER", "description": "", "parent": "HOST"} in options["subcategory"]


def test_rows_that_left_the_source_are_reported_and_kept_unless_removed():
    profile = _profile(
        assets_exposure=[
            oc.AssetEntry(name="old", source="openaev", external_id="a9"),
            oc.AssetEntry(name="hand", source="manual"),
            oc.AssetEntry(name="other", source="opencti", external_id="s1"),
        ]
    )
    rows = [imp.openaev_asset(_aev_asset("a1", "web-01"))]
    kept = imp.plan_import(profile, "assets", "openaev", imp.ReadResult(rows=rows, total=1))
    assert kept.gone == ["old"] and kept.as_dict()["counts"]["gone"] == 1
    assert [a.name for a in kept.profile.assets_exposure] == ["old", "hand", "other", "web-01"]
    removed = imp.plan_import(profile, "assets", "openaev", imp.ReadResult(rows=rows, total=1), remove_gone=True)
    assert [a.name for a in removed.profile.assets_exposure] == ["hand", "other", "web-01"]
    # A truncated read never removes: the rows past the cut are not known to be gone.
    cut = imp.plan_import(
        profile, "assets", "openaev", imp.ReadResult(rows=rows, total=9, truncated=True), remove_gone=True
    )
    assert cut.gone == [] and len(cut.profile.assets_exposure) == 4


def test_opencrq_threshold_keeps_that_level_and_above():
    # #1591: OpenCRQ primary assets, rated by their highest security need.
    assets = [
        {"id": "1", "name": "A", "availability": "low"},
        {"id": "2", "name": "B", "availability": "low", "confidentiality": "medium"},
        {"id": "3", "name": "C", "traceability": "very_high"},
        {"id": "4", "name": "D"},
    ]
    # A primary asset with no need rated is always kept, as high.
    assert [j.name for j in (imp.opencrq_primary_asset(a, "medium") for a in assets) if j] == ["B", "C", "D"]
    assert [j.criticality for j in (imp.opencrq_primary_asset(a, "high") for a in assets) if j] == ["critical", "high"]
    assert [j.name for j in (imp.opencrq_primary_asset(a) for a in assets) if j] == ["A", "B", "C", "D"]
    # The alias kept for one release reads primary assets too.
    assert imp.opencrq_jewel(assets[2], "low") == imp.opencrq_primary_asset(assets[2], "low")


def test_a_row_of_the_superseded_supporting_asset_layer_is_removed_and_a_typed_row_kept() -> None:
    # #1591: rows an older build imported from OpenCRQ SUPPORTING assets go at the next import.
    profile = oc.OrganizationProfile(
        crown_jewels=[
            oc.CrownJewel(name="Mail server", source="opencrq", external_id="s1"),
            oc.CrownJewel(name="Mail", source="manual", opencrq_asset_id="s1"),
        ]
    )
    rows = [imp.opencrq_primary_asset({"id": "p1", "name": "Messaging", "availability": "high"})]
    read = imp.ReadResult(rows=[r for r in rows if r], total=1, superseded={"s1"})
    plan = imp.plan_import(profile, "crown_jewels", "opencrq", read)
    assert plan.superseded == ["Mail server"]
    assert plan.profile is not None
    assert [j.name for j in plan.profile.crown_jewels] == ["Mail", "Messaging"]
    # A failed read supersedes nothing.
    failed = imp.plan_import(profile, "crown_jewels", "opencrq", imp.ReadResult(error="x", superseded={"s1"}))
    assert failed.superseded == []


def test_openaev_filter_group_ands_every_filter():
    group = imp.openaev_filter_group({"tags": ["t1"], "category": "ENDPOINT", "platform": ["Linux"]}, asset_ids=["a"])
    assert group["mode"] == "and"
    assert [f["key"] for f in group["filters"]] == ["asset_id", "asset_tags", "asset_category", "endpoint_platform"]
    assert imp.openaev_filter_group({}) is None


def test_opencti_filter_group_nests_a_given_group():
    given = {"mode": "and", "filters": [{"key": ["x"], "values": ["y"]}], "filterGroups": []}
    group = imp.opencti_filter_group({"labels": ["erp"], "created_after": "2026-01-01", "filter_group": given})
    assert [f["key"] for f in group["filters"]] == [["objectLabel"], ["created"]]
    assert group["filterGroups"] == [given]
    assert imp.opencti_filter_group({}) is None


def test_assets_merge_counts_new_updated_unchanged_skipped_and_keeps_hand_rows():
    profile = _profile(
        assets_exposure=[
            oc.AssetEntry(name="crm", category="web_application", owner="Sales", source="manual"),
            oc.AssetEntry(name="web-01", category="host", internet_facing="no", source="openaev", external_id="a1"),
            oc.AssetEntry(name="db-01", category="generic_asset", source="openaev", external_id="a2", owner="DBA"),
        ]
    )
    rows = [
        imp.openaev_asset(_aev_asset("a1", "web-01", asset_internet_facing=True)),  # updated
        imp.openaev_asset(_aev_asset("a2", "db-01")),  # unchanged (owner kept)
        # New: a hand-typed row has the name, so this is a second record of it (#1178, S3),
        # added beside it for the asset store to merge; the hand-typed row is untouched.
        imp.openaev_asset(_aev_asset("a3", "CRM")),
        imp.openaev_asset(_aev_asset("a4", "mail-01")),  # new
    ]
    plan = imp.plan_import(profile, "assets", "openaev", imp.ReadResult(rows=rows, total=4))
    assert (plan.new, plan.updated, plan.unchanged, plan.skipped) == (["CRM", "mail-01"], ["web-01"], ["db-01"], [])
    saved = {a.name: a for a in plan.profile.assets_exposure}
    assert saved["crm"].owner == "Sales" and saved["crm"].source == "manual"
    assert saved["CRM"].source == "openaev" and saved["CRM"].external_id == "a3"
    assert saved["web-01"].internet_facing == "yes"
    assert saved["db-01"].owner == "DBA"
    assert plan.source_total == 4
    # The current profile is untouched.
    assert len(profile.assets_exposure) == 3


def test_a_rerun_from_another_source_never_touches_the_first_source_rows():
    profile = _profile(assets_exposure=[oc.AssetEntry(name="sap", source="openaev", external_id="a1")])
    row = imp.opencti_system({"id": "a1", "name": "SAP"}, "application")
    plan = imp.plan_import(profile, "assets", "opencti", imp.ReadResult(rows=[row], total=1))
    # Same external id, other source: not its row; a second record of the same thing, added
    # beside it (the asset store merges them, #1178 S3), the first source's row untouched.
    assert plan.new == ["SAP"] and not plan.skipped and not plan.updated
    assert plan.profile is not None
    assert [(a.source, a.name) for a in plan.profile.assets_exposure] == [("openaev", "sap"), ("opencti", "SAP")]


def test_an_asset_group_of_another_source_with_the_same_name_is_still_skipped() -> None:
    profile = _profile(asset_groups=[oc.AssetGroupEntry(name="web", source="manual")])
    row = oc.AssetGroupEntry(name="WEB", source="openaev", external_id="g1")
    plan = imp.plan_import(profile, "asset_groups", "openaev", imp.ReadResult(rows=[row], total=1))
    assert plan.skipped == ["WEB"] and not plan.new


def test_the_same_import_twice_is_unchanged_the_second_time():
    rows = [imp.openaev_asset(_aev_asset("a1", "web-01", asset_internet_facing=True))]
    first = imp.plan_import(_profile(), "assets", "openaev", imp.ReadResult(rows=rows, total=1))
    second = imp.plan_import(first.profile, "assets", "openaev", imp.ReadResult(rows=rows, total=1))
    assert first.new == ["web-01"] and second.unchanged == ["web-01"] and not second.new


def test_crown_jewels_merge_keeps_links_and_hand_jewels():
    profile = _profile(
        crown_jewels=[
            oc.CrownJewel(name="Billing", criticality="high", opencrq_asset_id="1", asset_names=["db-01"]),
            oc.CrownJewel(name="SCADA", criticality="critical"),
        ]
    )
    rows = [
        oc.CrownJewel(name="Billing", criticality="critical", opencrq_asset_id="1"),
        oc.CrownJewel(name="scada", criticality="critical", opencrq_asset_id="2"),
    ]
    plan = imp.plan_import(profile, "crown_jewels", "opencrq", imp.ReadResult(rows=rows, total=2))
    assert (plan.updated, plan.skipped) == (["Billing"], ["scada"])
    billing = plan.profile.crown_jewels[0]
    assert (billing.criticality, billing.asset_names) == ("critical", ["db-01"])


def test_pirs_merge_adds_and_names():
    profile = _profile(pir_ids=["p1"])
    rows = [{"id": "p1", "name": "Ransomware"}, {"id": "p2", "name": "Espionage"}]
    plan = imp.plan_import(profile, "pirs", "opencti", imp.ReadResult(rows=rows, total=2))
    assert (plan.new, plan.updated) == (["Espionage"], ["Ransomware"])
    assert plan.profile.pir_ids == ["p1", "p2"]
    assert plan.profile.pir_names == {"p1": "Ransomware", "p2": "Espionage"}


def test_software_merge_never_duplicates_a_product():
    profile = _profile(tech_stack=[oc.TechStackEntry(product="Splunk")])
    rows = [
        oc.TechStackEntry(product="splunk", source="openaev", external_id="splunk"),
        oc.TechStackEntry(product="CrowdStrike", source="openaev", external_id="crowdstrike"),
    ]
    plan = imp.plan_import(profile, "software", "openaev", imp.ReadResult(rows=rows, total=2))
    # The typed "Splunk" is a person's row: the import never takes it over (#1134).
    assert (plan.new, plan.skipped) == (["CrowdStrike"], ["splunk"])
    assert plan.profile is not None
    assert [(t.product, t.source) for t in plan.profile.tech_stack] == [
        ("Splunk", "manual"),
        ("CrowdStrike", "openaev"),
    ]


def test_provenance_names_the_source_and_filters():
    plan = imp.ImportPlan(section="assets", source="openaev", source_total=1, read=1, new=["a"])
    sources = imp.provenance(plan, {"tags": ["prod"], "category": ""})
    assert sources["assets_exposure"]["source"] == "openaev"
    assert "tags=['prod']" in sources["assets_exposure"]["basis"]
    assert "; read 20" in sources["assets_exposure"]["basis"]


@pytest.mark.asyncio
async def test_options_of_a_source_that_fails_name_the_reason(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(imp, "_opencti_options", boom)
    found = await imp.filter_options(None, user_id=uuid.uuid4(), section="assets", source="opencti")
    assert found.options == {} and found.unavailable["labels"] == "unreadable"
    assert (await imp.filter_options(None, user_id=uuid.uuid4(), section="pirs", source="openaev")).options == {}


@pytest.mark.asyncio
async def test_a_source_that_cannot_feed_the_section_is_refused_without_io():
    read = await imp.read_source(None, user_id=uuid.uuid4(), section="pirs", source="openaev", filters={})
    # #1774: the refusal names what the source feeds instead.
    assert read.error == "openaev cannot feed pirs: it feeds assets, asset_groups, software"


@pytest.mark.asyncio
async def test_an_unreadable_platform_is_reported_never_raised(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("down")

    monkeypatch.setattr(imp, "_read_openaev", boom)
    read = await imp.read_source(None, user_id=uuid.uuid4(), section="assets", source="openaev", filters={})
    assert read.error == "openaev: RuntimeError" and read.rows == []


@pytest.mark.asyncio
async def test_preview_counts_what_the_source_answered(monkeypatch):
    from app.services import organization_context as org_context

    async def current(_db):
        return SimpleNamespace(profile={}, version=3)

    async def fake_read(_db, _user, section, filters, batch_type):
        assert filters == {"tags": ["prod"]}
        return imp.ReadResult(rows=[imp.openaev_asset(_aev_asset("a1", "web-01"))], total=1)

    async def no_records(*_a: object, **_k: object) -> None:
        return None  # no organization records yet: the profile plan

    monkeypatch.setattr(org_context, "current_version", current)
    monkeypatch.setattr(imp, "_read_openaev", fake_read)
    monkeypatch.setattr(imp, "_plan_on_records", no_records)
    plan, version = await imp.preview_import(
        None, user_id=uuid.uuid4(), section="assets", source="openaev", filters={"tags": ["prod"]}
    )
    assert version == 3
    body = plan.as_dict()
    assert body["source_total"] == 1 and body["counts"]["new"] == 1 and body["sample"][0]["external_id"] == "a1"
