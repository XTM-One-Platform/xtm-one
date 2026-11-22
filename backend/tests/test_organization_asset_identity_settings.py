"""The organization's asset identity settings (#1178): key order and per-field precedence from the profile."""

from __future__ import annotations

from typing import Any

import pytest

from app.services import organization_context as oc
from app.services.organization_asset_store import canonical_rows, identity_settings


def _row(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"name": "laptop-01", "category": "host", "source": "manual", "external_id": ""}
    base.update(kw)
    return base


def test_an_empty_profile_keeps_the_engine_defaults() -> None:
    assert identity_settings(None) == ((), {})
    assert identity_settings({}) == ((), {})
    assert oc.OrganizationProfile().asset_identity == oc.AssetIdentity()


def test_the_settings_are_a_profile_part_never_dumped_into_the_always_on_block() -> None:
    assert "asset_identity" in oc.EXPOSURE_FIELDS
    profile = oc.parse_profile({"name": "Acme", "asset_identity": {"key_order": ["hostname"]}})
    assert profile.asset_identity.key_order == ["hostname"]
    assert "asset_identity" not in oc.legacy_dump(profile)


def test_an_unknown_key_is_refused_and_duplicates_and_case_are_cleaned() -> None:
    with pytest.raises(ValueError):
        oc.AssetIdentity.model_validate({"key_order": ["ip"]})
    settings = oc.AssetIdentity.model_validate(
        {"key_order": ["serial", "serial", "mac"], "precedence": {"Criticality": [" Hand ", "opencrq", "hand"]}}
    )
    assert settings.key_order == ["serial", "mac"]
    assert settings.precedence == {"criticality": ["hand", "opencrq"]}


def test_a_stored_value_that_no_longer_parses_keeps_the_defaults() -> None:
    assert identity_settings({"asset_identity": {"key_order": ["nonsense"]}}) == ((), {})


def test_a_custom_precedence_changes_the_value_shown() -> None:
    rows = [
        _row(source="opencrq", external_id="c-1", serial="SN-1", criticality="high"),
        _row(source="manual", external_id="m-1", serial="SN-1", criticality="low"),
    ]
    [default] = canonical_rows(rows)
    assert default["fields"]["criticality"]["value"] == "high"
    _keys, precedence = identity_settings({"asset_identity": {"precedence": {"criticality": ["hand", "opencrq"]}}})
    [custom] = canonical_rows(rows, precedence=precedence)
    assert custom["fields"]["criticality"]["value"] == "low"


def test_a_key_left_out_of_the_order_no_longer_merges() -> None:
    rows = [
        _row(source="intune", external_id="d-1", name="a", hostname="ws-1"),
        _row(source="openaev", external_id="a-1", name="b", hostname="ws-1"),
    ]
    assert len(canonical_rows(rows)) == 1
    key_order, _prec = identity_settings({"asset_identity": {"key_order": ["serial", "mac"]}})
    assert len(canonical_rows(rows, key_order=key_order)) == 2
