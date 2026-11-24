"""#1844 S1: the stored ai_systems rows become ai_target assets once, then the key is gone.

The data migration ``r1844noaisys01`` carries what the profile model used to do on every
read (#1501): its pure helpers are pinned here, the SQL around them is a loop over rows.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services import organization_context as oc

_MIGRATION = (
    Path(__file__).resolve().parents[1] / "alembic" / "versions" / "r1844noaisys01_profile_ai_systems_into_assets.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("r1844noaisys01", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_stored_ai_system_moves_to_the_assets_with_nothing_lost() -> None:
    m = _migration()
    out = m.migrate_profile(
        {
            "name": "Acme",
            "assets_exposure": [{"name": "Bot", "category": "ai_target", "source": "openaev", "external_id": "t-1"}],
            "ai_systems": [
                {"name": "Bot", "kind": "model_api", "openaev_asset_id": "t-1"},
                {"name": "Helpdesk", "kind": "assistant", "owner": "IT", "exposure": "public"},
                {"name": "Planner", "kind": "agent", "exposure": "partner"},
                {"name": "  "},
            ],
            "onboarding_skipped": ["ai_systems", "ticketing"],
        }
    )
    assert out is not None and "ai_systems" not in out
    assert out["onboarding_skipped"] == ["ticketing"]
    profile = oc.parse_profile(out)
    rows = {a.name: a for a in profile.assets_exposure}
    assert list(rows) == ["Bot", "Helpdesk", "Planner"]
    helpdesk, planner = rows["Helpdesk"], rows["Planner"]
    assert (helpdesk.category, helpdesk.subcategory, helpdesk.internet_facing, helpdesk.owner) == (
        "ai_target",
        "",
        "yes",
        "IT",
    )
    assert helpdesk.tags == ["ai_kind:assistant", "exposure:public"]
    assert (planner.subcategory, planner.internet_facing, planner.tags) == ("ai_agent", "unknown", ["exposure:partner"])


def test_an_openaev_id_becomes_the_source_and_external_id() -> None:
    row = _migration().asset_row({"name": "Bot", "openaev_asset_id": "e-9"})
    assert row is not None and (row["source"], row["external_id"], row["category"]) == ("openaev", "e-9", "ai_target")


def test_a_profile_without_the_key_is_left_alone() -> None:
    m = _migration()
    assert m.migrate_profile({"name": "Acme", "assets_exposure": []}) is None
    assert m.migrate_profile(None) is None
    # Re-run: the migrated profile has nothing left to move.
    once = m.migrate_profile({"name": "Acme", "ai_systems": [{"name": "Bot"}]})
    assert once is not None and m.migrate_profile(once) is None


def test_an_ai_systems_sync_becomes_an_assets_sync_on_the_ai_target_category() -> None:
    m = _migration()
    assert m.migrate_sync("ai_systems", {"tags": ["prod"]}) == ("assets", {"tags": ["prod"], "category": ["ai_target"]})
    assert m.migrate_sync("assets", {}) is None


def test_a_pending_ai_systems_proposal_is_dropped() -> None:
    m = _migration()
    kept = m.migrate_proposals([{"field": "ai_systems", "value": []}, {"field": "sector", "value": "Finance"}])
    assert kept == [{"field": "sector", "value": "Finance"}]
    assert m.migrate_proposals([{"field": "sector"}]) is None


def test_the_profile_refuses_the_retired_key() -> None:
    try:
        oc.parse_profile({"ai_systems": [{"name": "Bot"}]})
    except ValueError as exc:
        assert "ai_systems" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("ai_systems is no field of the profile any more")
