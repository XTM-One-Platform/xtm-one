"""The primary assets category and profile key, no alias (#1591, #1844)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services import organization_context_categories as cats

_VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(name: str):
    path = next(_VERSIONS.glob(f"{name}_*.py"))
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_category_reads_the_profile_key_of_its_name() -> None:
    category = cats.get("primary_assets")
    assert category is not None and category.field == "primary_assets"


def test_the_old_name_answers_nothing() -> None:
    old = "crown" + "_jewels"
    assert cats.get(old) is None, "#1844: no alias (Sam, 24 Nov 2026)"
    assert cats.normalize_categories([old, "pirs", "primary_assets"]) == ["pirs", "primary_assets"]
    assert cats.get("tech_stack") is None, "#1495: no tech_stack alias (Sam, 24 Nov 2026)"


def test_the_1591_migration_renames_in_place_and_back() -> None:
    module = _load("p1591primcat01")
    old = "crown" + "_jewels"
    assert module.rename_list(["software", old, "pirs"], old, "primary_assets") == ["software", "primary_assets", "pirs"]
    assert module.rename_list([old, "primary_assets"], old, "primary_assets") == ["primary_assets"]


def test_the_1844_migration_renames_keys_lists_and_values() -> None:
    module = _load("s1844primasset01")
    old, jewel = "crown" + "_jewels", "crown" + "_jewel"
    profile = {
        old: [{"name": "Payments"}, {"name": "Mail"}],
        "primary_assets": [{"name": "mail", "owner": "kept"}],
        "prioritization_policy": {"min_" + jewel + "_criticality": "high"},
        "case_attention": {jewel: 20, "kev": 15},
        "onboarding_skipped": [old, "pirs"],
        "pir_details": {"p1": {old: ["Payments"]}},
    }
    out = module.rename(profile)
    assert old not in out
    assert out["primary_assets"] == [{"name": "mail", "owner": "kept"}, {"name": "Payments"}]
    assert out["prioritization_policy"] == {"min_primary_asset_criticality": "high"}
    assert out["case_attention"] == {"primary_asset": 20, "kev": 15}
    assert out["onboarding_skipped"] == ["primary_assets", "pirs"]
    assert out["pir_details"] == {"p1": {"primary_assets": ["Payments"]}}


def test_the_1844_migration_renames_a_signal_decision_and_back() -> None:
    module = _load("s1844primasset01")
    jewel = "crown" + "_jewel"
    decision = {
        "reason_code": jewel + "_below_policy",
        "sources": [{"from": jewel, "name": "Payroll"}],
        "checks": [{"rule": "min_" + jewel + "_criticality"}],
        "resolution": {jewel + "s_named": ["Payroll"]},
        "name": "Payroll",
    }
    out = module.rename(decision)
    assert out == {
        "reason_code": "primary_asset_below_policy",
        "sources": [{"from": "primary_asset", "name": "Payroll"}],
        "checks": [{"rule": "min_primary_asset_criticality"}],
        "resolution": {"primary_assets_named": ["Payroll"]},
        "name": "Payroll",
    }
    back = module.rename(out, {new: old for old, new in module.NAMES.items()})
    assert back == decision
