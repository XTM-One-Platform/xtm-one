"""The sources of the organization profile, and the purge or release of what one wrote (#1276).

Pure: the profile is built in memory, so the rules hold without a database:
a source is listed with its kind and a readable name, a purge removes only the
rows of its source (one sync's when named) and never a person's, a release
hands the rows to people, and rows of other sources with the same name are
named and kept.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import organization_context as oc
from app.services import organization_context_sources as osources


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "assets_exposure": [
                {"name": "web-01", "type": "server", "source": "openaev", "external_id": "a1", "sync_id": "s1"},
                {"name": "web-02", "type": "server", "source": "openaev", "external_id": "a2", "sync_id": "s2"},
                {"name": "web-01", "type": "server", "source": "intune", "external_id": "i1"},
                {"name": "laptop-7", "type": "workstation", "source": "manual"},
                {"name": "db-01", "type": "server", "source": "ciso-office", "external_id": "c1"},
            ],
            "crown_jewels": [{"name": "Billing", "criticality": "critical", "source": "opencrq", "external_id": "j1"}],
        }
    )


def test_kinds_and_display_names_never_show_a_raw_platform_slug():
    assert osources.kind_of("openaev") == "platform"
    assert osources.kind_of("ciso-office") == "agent"
    assert osources.kind_of("manual") == "manual" and osources.kind_of("csv") == "csv"
    assert osources.display_name("opencrq") == "OpenCRQ"
    assert osources.display_name("intune") == "Microsoft Intune"
    # One name everywhere: the row, its menus and the sync edit dialog (IMPORT_SOURCE_NAMES).
    assert osources.display_name("defender") == "Microsoft Defender for Endpoint"
    assert osources.display_name("agent:CISO Office") == "CISO Office"
    assert osources.source_key("") == "manual"


def test_summarize_lists_each_source_once_with_rows_writer_and_syncs_platforms_first():
    sync = SimpleNamespace(id=uuid.uuid4(), source="openaev", section="assets")
    sources = osources.summarize(
        _profile(), [sync], scope="inventory", writers={"ciso-office": "CISO Office"}, connected=("opencti",)
    )
    keys = [s.key for s in sources]
    assert keys[:3] == ["openaev", "opencrq", "opencti"]
    assert keys[-1] == "manual"
    by_key = {s.key: s for s in sources}
    assert by_key["openaev"].rows == {"assets_exposure": 2} and by_key["openaev"].syncs == [str(sync.id)]
    assert by_key["opencti"].total == 0
    assert by_key["ciso-office"].written_by_agent == "CISO Office"
    # Primary assets are an Inventory source (Sam, 24 Nov 2026): OpenCRQ is listed with its rows there.
    assert by_key["opencrq"].rows == {"crown_jewels": 1}
    business = osources.summarize(_profile(), [sync], scope="profile")
    assert "opencrq" not in {s.key for s in business}


def test_purge_of_one_sync_removes_only_its_rows_and_names_the_same_name_elsewhere():
    plan = osources.plan_rows(_profile(), source="openaev", mode="purge", sync_id="s1")
    assert plan.touched == {"assets_exposure": ["web-01"]}
    assert plan.kept_elsewhere["assets_exposure"] == ["web-01"]
    names = [(r.name, r.source) for r in plan.profile.assets_exposure]
    assert ("web-01", "openaev") not in names
    assert ("web-01", "intune") in names and ("web-02", "openaev") in names and ("laptop-7", "manual") in names


def test_a_sync_purge_or_release_stays_in_the_field_of_its_section() -> None:
    # Review on #1278: unstamped rows of the same platform in another field are not the sync's.
    profile = _profile()
    profile.tech_stack = [oc.TechStackEntry(product="nginx", source="openaev")]
    assert osources.sync_fields("assets") == ("assets_exposure",)
    assert osources.sync_fields("pirs") == () and osources.sync_fields("all") == ()
    for mode in ("purge", "release"):
        plan = osources.plan_rows(
            profile, source="openaev", mode=mode, sync_id="s1", fields=osources.sync_fields("assets")
        )
        assert set(plan.touched) == {"assets_exposure"} and plan.profile is not None
        assert [(r.product, r.source) for r in plan.profile.tech_stack] == [("nginx", "openaev")]
    # A pirs sync writes no row field: its purge touches nothing.
    plan = osources.plan_rows(profile, source="opencti", mode="purge", sync_id="p1", fields=())
    assert plan.count == 0 and plan.profile is not None
    assert plan.profile.assets_exposure == profile.assets_exposure


def test_release_hands_the_rows_to_people_and_keeps_them():
    plan = osources.plan_rows(_profile(), source="ciso-office", mode="release")
    assert plan.count == 1
    row = next(r for r in plan.profile.assets_exposure if r.name == "db-01")
    assert row.source == "manual" and row.sync_id == ""
    assert "formerly synced from ciso-office" in osources.plan_basis(plan, who="admin@example.com")


def test_rows_entered_by_hand_are_never_purged():
    with pytest.raises(osources.SourceRefusedError):
        osources.plan_rows(_profile(), source="manual", mode="purge")
    with pytest.raises(osources.SourceRefusedError):
        osources.plan_rows(_profile(), source="openaev", mode="wipe")


def test_the_plan_answer_bounds_the_names_it_lists():
    plan = osources.plan_rows(_profile(), source="openaev", mode="purge")
    answer = plan.as_dict()
    assert answer["count"] == 2 and answer["counts"] == {"assets_exposure": 2}
    assert set(answer["rows"]["assets_exposure"]) == {"web-01", "web-02"}
