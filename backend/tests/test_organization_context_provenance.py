"""One provenance shape on every list section of the organization profile (#1134).

Crown jewels, software and AI systems carry ``source`` / ``external_id`` /
``sync_id`` like the assets; the ids saved before #1134 (``opencrq_asset_id``,
``openaev_asset_id``) read as a platform row; every section merges on
``source`` + ``external_id``, removes what the source no longer returns when
asked, and honours the sync's exclusion list.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

from app.services import organization_context as oc
from app.services import organization_context_import as imp
from app.services import organization_context_sync as osync


def _jewel(asset_id: str, name: str, level: str = "high") -> oc.CrownJewel:
    # An OpenCRQ primary asset rated by its integrity need (#1591).
    jewel = imp.opencrq_primary_asset({"id": asset_id, "name": name, "integrity": level}, "low")
    assert jewel is not None
    return jewel


def _result(plan: imp.ImportPlan) -> oc.OrganizationProfile:
    assert plan.profile is not None
    return plan.profile


def test_a_crown_jewel_saved_with_its_opencrq_id_reads_as_an_opencrq_row() -> None:
    profile = oc.parse_profile({"crown_jewels": [{"name": "Payments", "opencrq_asset_id": "a-1"}]})
    jewel = profile.crown_jewels[0]
    assert (jewel.source, jewel.external_id, jewel.opencrq_asset_id) == ("opencrq", "a-1", "a-1")


def test_a_typed_row_stays_manual_and_its_provenance_stays_out_of_the_legacy_dump() -> None:
    profile = oc.parse_profile({"crown_jewels": [{"name": "Payments"}], "tech_stack": [{"product": "Splunk"}]})
    assert (profile.crown_jewels[0].source, profile.tech_stack[0].source) == ("manual", "manual")
    dumped = oc.legacy_dump(profile)
    assert "source" not in dumped["crown_jewels"][0] and "sync_id" not in dumped["tech_stack"][0]


def test_platform_sources_are_neither_hand_nor_agent_sources() -> None:
    assert oc.is_platform_source("opencrq") and oc.is_platform_source("intune")
    assert not oc.is_platform_source("manual") and not oc.is_platform_source("csv")
    assert not oc.is_platform_source("agent:1234") and not oc.is_platform_source("")


def test_an_empty_profile_takes_every_opencrq_jewel_at_or_above_the_threshold() -> None:
    """Sam's scenario: every jewel deleted by hand, the import brings the high ones back (not only very_high)."""
    rows = [_jewel("a-1", "Payments", "very_high"), _jewel("a-2", "Billing", "high")]
    plan = imp.plan_import(oc.OrganizationProfile(), "crown_jewels", "opencrq", imp.ReadResult(rows=rows, total=2))
    assert plan.new == ["Payments", "Billing"]
    jewels = _result(plan).crown_jewels
    assert [(j.source, j.external_id) for j in jewels] == [("opencrq", "a-1"), ("opencrq", "a-2")]


def test_a_crown_jewel_rerun_keeps_its_local_enrichment() -> None:
    stored = _jewel("a-1", "Payments").model_copy(
        update={"business_service": "Cards", "asset_names": ["web-1"], "owner": "CISO"}
    )
    plan = imp.plan_import(
        oc.OrganizationProfile(crown_jewels=[stored]),
        "crown_jewels",
        "opencrq",
        imp.ReadResult(rows=[_jewel("a-1", "Payments", "very_high")], total=1),
    )
    jewel = _result(plan).crown_jewels[0]
    assert plan.updated == ["Payments"]
    assert (jewel.criticality, jewel.business_service, jewel.asset_names, jewel.owner) == (
        "critical",
        "Cards",
        ["web-1"],
        "CISO",
    )


def test_a_deletion_upstream_removes_the_row_in_every_keyed_section() -> None:
    profile = oc.OrganizationProfile(
        crown_jewels=[_jewel("a-1", "Payments"), _jewel("a-2", "Billing"), oc.CrownJewel(name="Typed")],
    )
    read = imp.ReadResult(rows=[_jewel("a-1", "Payments")], total=1)
    kept = imp.plan_import(profile, "crown_jewels", "opencrq", read)
    assert kept.gone == ["Billing"] and len(_result(kept).crown_jewels) == 3
    removed = imp.plan_import(profile, "crown_jewels", "opencrq", read, remove_gone=True)
    assert [j.name for j in _result(removed).crown_jewels] == ["Payments", "Typed"]


def test_a_truncated_read_removes_nothing() -> None:
    profile = oc.OrganizationProfile(crown_jewels=[_jewel("a-1", "Payments"), _jewel("a-2", "Billing")])
    read = imp.ReadResult(rows=[_jewel("a-1", "Payments")], total=2000, truncated=True)
    plan = imp.plan_import(profile, "crown_jewels", "opencrq", read, remove_gone=True)
    assert plan.gone == [] and len(_result(plan).crown_jewels) == 2


def test_an_excluded_id_is_never_imported_and_its_stored_row_goes() -> None:
    profile = oc.OrganizationProfile(crown_jewels=[_jewel("a-2", "Billing")])
    read = imp.ReadResult(rows=[_jewel("a-1", "Payments"), _jewel("a-2", "Billing")], total=2)
    plan = imp.plan_import(profile, "crown_jewels", "opencrq", read, excluded_ids=["a-2"], sync_id="s-1")
    assert plan.excluded == ["Billing"] and plan.excluded_removed == ["Billing"]
    assert [(j.name, j.sync_id) for j in _result(plan).crown_jewels] == [("Payments", "s-1")]
    assert plan.as_dict()["counts"]["excluded"] == 1


def test_a_sync_stamps_its_id_on_the_rows_it_writes() -> None:
    read = imp.ReadResult(rows=[imp.openaev_ai_target({"asset_id": "e-1", "asset_name": "Bot"})], total=1)
    plan = imp.plan_import(oc.OrganizationProfile(), "assets", "openaev", read, sync_id="s-9")
    systems = oc.ai_target_assets(_result(plan))
    assert [(s.source, s.external_id, s.sync_id) for s in systems] == [("openaev", "e-1", "s-9")]


def test_an_openaev_ai_target_keeps_its_subcategory_and_fields_never_its_secrets() -> None:
    """#1501: no forced kind; provider, endpoint, model and modality kept; token and prompt never read."""
    row = imp.openaev_ai_target(
        {
            "asset_id": "t-1",
            "asset_name": "Support agent",
            "asset_category": "AI_TARGET",
            "asset_subcategory": "AI_AGENT",
            "asset_internet_facing": True,
            "ai_target_provider": "ANTHROPIC",
            "ai_target_endpoint": "https://api.example.test/v1",
            "ai_target_model": "claude",
            "ai_target_modality": "TEXT",
            "ai_target_token": "secret",
            "ai_target_system_prompt": "secret prompt",
        }
    )
    assert row is not None
    assert (row.category, row.subcategory, row.internet_facing) == ("ai_target", "ai_agent", "yes")
    assert (row.ai_target_provider, row.ai_target_model, row.ai_target_modality) == ("anthropic", "claude", "text")
    assert "secret" not in row.model_dump_json()


def test_a_new_sync_removes_what_its_source_deleted_by_default() -> None:
    sync: Any = SimpleNamespace(excluded_external_ids=None)
    osync.define(sync, owner_id=uuid.uuid4(), section="crown_jewels", source="opencrq")
    assert sync.remove_gone is True and sync.excluded_external_ids == []
    osync.define(
        sync,
        owner_id=uuid.uuid4(),
        section="crown_jewels",
        source="opencrq",
        excluded_external_ids=[" a-1 ", "a-1", ""],
    )
    assert sync.excluded_external_ids == ["a-1"]
    osync.define(sync, owner_id=uuid.uuid4(), section="all")
    assert sync.remove_gone is False
