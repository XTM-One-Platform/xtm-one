"""#1495 part 4b: the per-asset and per-group list ``tech_stack`` is named ``products`` (migration s1495products01)."""

import importlib.util
from pathlib import Path

from app.services import organization_context as oc

_PATH = next((Path(__file__).resolve().parents[1] / "alembic/versions").glob("s1495products01_*.py"))
_spec = importlib.util.spec_from_file_location("s1495products01", _PATH)
assert _spec and _spec.loader
mig = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mig)


def test_rows_of_assets_and_groups_are_renamed_and_nothing_else() -> None:
    profile = {
        "software": [{"product": "nginx", "tech_stack": "kept: not a row list"}],
        "assets_exposure": [{"name": "web-1", "tech_stack": ["nginx"]}, {"name": "db-1"}],
        "asset_groups": [{"name": "DMZ", "tech_stack": ["nginx"]}],
    }
    out = mig.rename_profile(profile)
    assert out["assets_exposure"] == [{"name": "web-1", "products": ["nginx"]}, {"name": "db-1"}]
    assert out["asset_groups"] == [{"name": "DMZ", "products": ["nginx"]}]
    assert out["software"] == profile["software"]
    assert mig.rename_profile(out, "products", "tech_stack") == profile, "the downgrade names it back"


def test_a_row_holding_both_keys_keeps_products() -> None:
    assert mig.rename_key({"tech_stack": ["old"], "products": ["new"]}) == {"products": ["new"]}


def test_provenance_and_proposal_paths() -> None:
    sources = {
        "assets_exposure[0].tech_stack": {"source": "user"},
        "asset_groups[2].tech_stack[1]": {"source": "csv"},
        "software[0].tech_stack": {"source": "x"},
    }
    assert mig.rename_sources(sources) == {
        "assets_exposure[0].products": {"source": "user"},
        "asset_groups[2].products[1]": {"source": "csv"},
        "software[0].tech_stack": {"source": "x"},
    }
    proposals = [{"field": "assets_exposure", "value": [{"name": "a", "tech_stack": ["x"]}]}, {"field": "sector"}]
    assert mig.rename_proposals(proposals)[0]["value"] == [{"name": "a", "products": ["x"]}]


def test_the_gate_decision_reconcile_key() -> None:
    decision = {"version": 1, "reconcile": {"assets": [], "tech_stack": ["nginx"]}}
    assert mig.rename_decision(decision)["reconcile"] == {"assets": [], "products": ["nginx"]}
    assert mig.rename_decision({"version": 1}) == {"version": 1}


def test_the_models_have_no_tech_stack_field() -> None:
    assert "tech_stack" not in oc.AssetEntry.model_fields
    assert "tech_stack" not in oc.AssetGroupEntry.model_fields
    assert oc.AssetEntry(name="web-1", products=["nginx", "nginx"]).products == ["nginx"]
