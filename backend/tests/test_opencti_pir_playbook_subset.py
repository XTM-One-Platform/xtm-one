"""A PIR playbook (or trigger) provisioned on a subset of the profile PIRs (#1801, gap pir-playbook-pir-subset)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import opencti_trigger_provisioning as prov


def _spec(**provision: Any) -> dict[str, Any]:
    config = {"provider": "opencti", "provision": {"fleet": "Threat", "purpose": "PIR flags", **provision}}
    spec = prov.read_provision_spec(config)
    assert spec is not None
    return spec


def test_pir_subset_implies_the_pir_ids_context_field() -> None:
    spec = _spec(mode="pir_playbook", pir_ids=["pir-b", "pir-a", "pir-a"])
    assert spec["organization_context"] == ["pir_ids"]
    assert spec["pir_subset"] == ["pir-a", "pir-b"]


def test_without_a_subset_every_profile_pir_is_kept() -> None:
    spec = _spec(mode="pir_playbook", organization_context=["pir_ids"])
    assert spec["pir_subset"] == []
    derived, missing = prov.derive_from_organization_context(spec, SimpleNamespace(pir_ids=["pir-c", "pir-a", "pir-b"]))
    assert missing == []
    assert derived["pir_ids"] == ["pir-a", "pir-b", "pir-c"]


def test_the_subset_narrows_the_profile_pirs() -> None:
    spec = _spec(mode="pir_playbook", pir_ids=["pir-b"])
    derived, missing = prov.derive_from_organization_context(spec, SimpleNamespace(pir_ids=["pir-a", "pir-b", "pir-c"]))
    assert missing == []
    assert derived["pir_ids"] == ["pir-b"]
    nodes = prov.pir_playbook_nodes(derived, "notifier", "member", "Member", {"pir-b": "Ransomware"})
    assert "pir-a" not in str(nodes) and "pir-b" in str(nodes)


def test_a_subset_id_outside_the_profile_is_named_and_nothing_is_derived() -> None:
    spec = _spec(mode="pir_playbook", pir_ids=["pir-b", "pir-z"])
    derived, missing = prov.derive_from_organization_context(spec, SimpleNamespace(pir_ids=["pir-a", "pir-b"]))
    assert missing == ["pir_ids ['pir-z'] (absent from the profile PIRs)"]
    assert derived["pir_ids"] == []


def test_pir_ids_must_be_a_list_of_strings() -> None:
    with pytest.raises(prov.ProvisionSpecError):
        _spec(mode="pir_playbook", pir_ids=[1, 2])
