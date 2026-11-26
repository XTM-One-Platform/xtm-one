"""One provenance shape on every list section of the organization profile (#1134).

Primary assets, software and AI systems carry ``source`` / ``external_id`` /
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


def _pa_row(asset_id: str, name: str, level: str = "high") -> oc.PrimaryAsset:
    # An OpenCRQ primary asset rated by its integrity need (#1591).
    pa_row = imp.opencrq_primary_asset({"id": asset_id, "name": name, "integrity": level}, "low")
    assert pa_row is not None
    return pa_row


def _result(plan: imp.ImportPlan) -> oc.OrganizationProfile:
    assert plan.profile is not None
    return plan.profile


def test_a_primary_asset_saved_with_its_opencrq_id_reads_as_an_opencrq_row() -> None:
    profile = oc.parse_profile({"primary_assets": [{"name": "Payments", "opencrq_asset_id": "a-1"}]})
    pa_row = profile.primary_assets[0]
    assert (pa_row.source, pa_row.external_id, pa_row.opencrq_asset_id) == ("opencrq", "a-1", "a-1")


def test_a_typed_row_stays_manual_and_its_provenance_stays_out_of_the_legacy_dump() -> None:
    profile = oc.parse_profile({"primary_assets": [{"name": "Payments"}], "software": [{"product": "Splunk"}]})
    assert (profile.primary_assets[0].source, profile.software[0].source) == ("manual", "manual")
    dumped = oc.legacy_dump(profile)
    assert "source" not in dumped["primary_assets"][0] and "sync_id" not in dumped["software"][0]


def test_platform_sources_are_neither_hand_nor_agent_sources() -> None:
    assert oc.is_platform_source("opencrq") and oc.is_platform_source("intune")
    assert not oc.is_platform_source("manual") and not oc.is_platform_source("csv")
    assert not oc.is_platform_source("agent:1234") and not oc.is_platform_source("")


def test_an_empty_profile_takes_every_opencrq_primary_asset_at_or_above_the_threshold() -> None:
    """Sam's scenario: every primary asset deleted by hand, the import brings the high ones back (not only very_high)."""
    rows = [_pa_row("a-1", "Payments", "very_high"), _pa_row("a-2", "Billing", "high")]
    plan = imp.plan_import(oc.OrganizationProfile(), "primary_assets", "opencrq", imp.ReadResult(rows=rows, total=2))
    assert plan.new == ["Payments", "Billing"]
    pa_rows = _result(plan).primary_assets
    assert [(j.source, j.external_id) for j in pa_rows] == [("opencrq", "a-1"), ("opencrq", "a-2")]


def test_a_primary_asset_rerun_keeps_its_local_enrichment() -> None:
    stored = _pa_row("a-1", "Payments").model_copy(
        update={"business_service": "Cards", "asset_names": ["web-1"], "owner": "CISO"}
    )
    plan = imp.plan_import(
        oc.OrganizationProfile(primary_assets=[stored]),
        "primary_assets",
        "opencrq",
        imp.ReadResult(rows=[_pa_row("a-1", "Payments", "very_high")], total=1),
    )
    pa_row = _result(plan).primary_assets[0]
    assert plan.updated == ["Payments"]
    assert (pa_row.criticality, pa_row.business_service, pa_row.asset_names, pa_row.owner) == (
        "critical",
        "Cards",
        ["web-1"],
        "CISO",
    )


def test_a_deletion_upstream_removes_the_row_in_every_keyed_section() -> None:
    profile = oc.OrganizationProfile(
        primary_assets=[_pa_row("a-1", "Payments"), _pa_row("a-2", "Billing"), oc.PrimaryAsset(name="Typed")],
    )
    read = imp.ReadResult(rows=[_pa_row("a-1", "Payments")], total=1)
    kept = imp.plan_import(profile, "primary_assets", "opencrq", read)
    assert kept.gone == ["Billing"] and len(_result(kept).primary_assets) == 3
    removed = imp.plan_import(profile, "primary_assets", "opencrq", read, remove_gone=True)
    assert [j.name for j in _result(removed).primary_assets] == ["Payments", "Typed"]


def test_a_truncated_read_removes_nothing() -> None:
    profile = oc.OrganizationProfile(primary_assets=[_pa_row("a-1", "Payments"), _pa_row("a-2", "Billing")])
    read = imp.ReadResult(rows=[_pa_row("a-1", "Payments")], total=2000, truncated=True)
    plan = imp.plan_import(profile, "primary_assets", "opencrq", read, remove_gone=True)
    assert plan.gone == [] and len(_result(plan).primary_assets) == 2


def test_an_excluded_id_is_never_imported_and_its_stored_row_goes() -> None:
    profile = oc.OrganizationProfile(primary_assets=[_pa_row("a-2", "Billing")])
    read = imp.ReadResult(rows=[_pa_row("a-1", "Payments"), _pa_row("a-2", "Billing")], total=2)
    plan = imp.plan_import(profile, "primary_assets", "opencrq", read, excluded_ids=["a-2"], sync_id="s-1")
    assert plan.excluded == ["Billing"] and plan.excluded_removed == ["Billing"]
    assert [(j.name, j.sync_id) for j in _result(plan).primary_assets] == [("Payments", "s-1")]
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
    osync.define(sync, owner_id=uuid.uuid4(), section="primary_assets", source="opencrq")
    assert sync.remove_gone is True and sync.excluded_external_ids == []
    osync.define(
        sync,
        owner_id=uuid.uuid4(),
        section="primary_assets",
        source="opencrq",
        excluded_external_ids=[" a-1 ", "a-1", ""],
    )
    assert sync.excluded_external_ids == ["a-1"]
    osync.define(sync, owner_id=uuid.uuid4(), section="all")
    assert sync.remove_gone is False
