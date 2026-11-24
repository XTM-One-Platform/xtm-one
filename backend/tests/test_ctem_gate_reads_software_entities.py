# SPDX-License-Identifier: Apache-2.0

"""#1495: the CTEM scope gate and trigger rule read the Software entities, not the profile list alone.

``organization_context.software_profile`` answers the current profile with its
software read from the asset store's Software entities (synced or typed by an
administrator) plus the typed profile rows no entity carries, the reading the
CVE matcher already had. Before the store holds any software, the profile rows
are answered as they are.
"""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import ctem_trigger_rules as rules
from app.services import organization_context as oc
from app.services import organization_software_store as software_store

ORG = uuid.uuid4()
TODAY = date(2026, 11, 24)


def _typed_profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {"tech_stack": [{"product": "Confluence", "vendor": "Atlassian", "cpe": "", "version": "8.5"}]}
    )


@pytest.fixture
def stored(monkeypatch):
    """The current profile holds one typed row; the store holds whatever ``software`` lists."""
    software: list[dict[str, Any]] = []
    row = SimpleNamespace(organization_id=ORG, profile={})

    async def _current_profile(_db, _org=None):
        return row, _typed_profile()

    async def _matcher_software(_db, organization_id):
        assert organization_id == ORG
        return list(software)

    monkeypatch.setattr(oc, "current_profile", _current_profile)
    monkeypatch.setattr(software_store, "matcher_software", _matcher_software)
    return software


async def test_the_profile_rows_are_answered_as_they_are_before_the_store_holds_software(stored):
    _row, profile = await oc.software_profile(None, ORG)
    assert [(e.vendor, e.product, e.version) for e in profile.tech_stack] == [("Atlassian", "Confluence", "8.5")]


async def test_a_synced_software_joins_the_typed_rows_and_keeps_its_installed_version(stored):
    stored.append({"vendor": "Microsoft", "product": "Edge", "cpe": "", "versions": ["131.0"]})
    stored.append(
        {"vendor": "Atlassian", "product": "Confluence", "cpe": "cpe:2.3:a:atlassian:confluence", "versions": []}
    )
    _row, profile = await oc.software_profile(None, ORG)
    by_product = {e.product: e for e in profile.tech_stack}
    assert set(by_product) == {"Edge", "Confluence"}
    assert by_product["Edge"].version == "131.0" and by_product["Edge"].source == "software"
    # The entity wins; its CPE is kept, nothing typed is listed twice.
    assert by_product["Confluence"].cpe == "cpe:2.3:a:atlassian:confluence"


async def test_more_software_than_a_typed_list_holds_is_still_answered(stored):
    stored.extend({"vendor": "V", "product": f"P{i}", "cpe": "", "versions": []} for i in range(600))
    _row, profile = await oc.software_profile(None, ORG)
    assert len(profile.tech_stack) == 601


async def test_a_vulnerability_on_synced_software_resolves_in_the_gate(stored):
    stored.append({"vendor": "Microsoft", "product": "Edge", "cpe": "", "versions": ["131.0"]})
    profile = await store._profile(None, ORG)
    facts = gate.SignalFacts(
        trigger_path="vulnerability",
        lens="inside",
        subject_kind="software",
        subject_ref="Edge",
        source_provider="opencti",
        evidence={},
    )
    assert gate.resolve(profile, facts).products == ["Edge"]
    assert gate.runs_in_profile(profile, "Edge") == ["Edge"]


async def test_the_trigger_rule_reads_the_software_entities(stored, monkeypatch):
    stored.append({"vendor": "Microsoft", "product": "Edge", "cpe": "", "versions": []})

    async def _primary(_db):
        return SimpleNamespace(id=ORG)

    monkeypatch.setattr("app.services.organizations.get_primary", _primary)
    profile = await rules._primary_profile(None)
    assert rules.drop_reason(profile, {"data": {"source_value": "Edge"}}) is None
    assert rules.drop_reason(profile, {"data": {"source_value": "Notepad++"}}) is not None
