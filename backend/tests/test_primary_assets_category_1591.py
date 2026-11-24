"""The primary assets category, ``crown_jewels`` its one-release alias (#1591, slice 2b)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from app.services import organization_context_categories as cats

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "p1591primcat01_crown_jewels_category_to_primary_assets.py"
)


def _migration():
    spec = importlib.util.spec_from_file_location("p1591primcat01", _MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_category_reads_the_stored_profile_key() -> None:
    category = cats.get("primary_assets")
    assert category is not None and category.field == "crown_jewels"


def test_the_old_name_answers_the_same_category() -> None:
    assert cats.get("crown_jewels") is cats.get("primary_assets")
    assert cats.get("Crown_Jewels") is cats.get("primary_assets")
    assert cats.get("software") is None, "#1495: no software alias (Sam, 24 Nov 2026)"


def test_a_stored_selection_naming_both_keeps_one() -> None:
    assert cats.normalize_categories(["crown_jewels", "pirs", "primary_assets"]) == ["primary_assets", "pirs"]


def test_the_migration_renames_in_place_and_back() -> None:
    module = _migration()
    assert module.rename_list(["software", "crown_jewels", "pirs"], "crown_jewels", "primary_assets") == [
        "software",
        "primary_assets",
        "pirs",
    ]
    assert module.rename_list(["crown_jewels", "primary_assets"], "crown_jewels", "primary_assets") == [
        "primary_assets"
    ]
    assert module.rename_list(["primary_assets"], "primary_assets", "crown_jewels") == ["crown_jewels"]
