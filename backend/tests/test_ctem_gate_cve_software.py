# SPDX-License-Identifier: Apache-2.0

"""#2171: the scope gate maps a signal's CVEs to the software OpenCTI relates to them,
and record_ctem_signal says when the evidence holds no key the gate reads."""

from __future__ import annotations

import uuid
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_cve_software
from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc
from app.services.builtin_tools import ctem_signals as tools

TODAY = date(2026, 11, 25)


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "software": [{"product": "Metabase"}],
            "assets_exposure": [
                {"name": "metabase.data.example.com", "internet_facing": "yes", "products": ["Metabase"]},
            ],
        }
    )


def _threat(**kw) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="report--8ec23395",
        subject_label="Metabase pre-auth RCE CVE-2023-38646",
        source_provider="opencti",
        evidence={"stack_fit": "Metabase in tech_stack"},
        **kw,
    )


def test_a_cve_only_threat_stays_unresolved_without_the_cve_software():
    decision = gate.decide(_profile(), _threat(), today=TODAY)
    assert decision.record["reason_code"] == "subject_unresolved"
    assert decision.record["inputs"]["cves"] == ["CVE-2023-38646"]
    assert decision.record["reconcile"]["cve_lookup"] is None


def test_the_software_of_its_cve_resolves_it_to_the_inventory_and_its_assets():
    facts = _threat(cve_software=("Metabase", "cpe:2.3:a:metabase:metabase:0.46.6:*:*:*:*:*:*:*"), cve_lookup="read")
    decision = gate.decide(_profile(), facts, today=TODAY)
    reconcile = decision.record["reconcile"]
    assert decision.record["reason_code"] != "subject_unresolved"
    assert reconcile["products"] == ["Metabase"]
    assert "metabase.data.example.com" in reconcile["assets"]
    assert "Metabase" in reconcile["software_refs"]
    assert reconcile["cve_lookup"] == "read"


def test_a_failed_lookup_is_recorded_never_guessed():
    decision = gate.decide(_profile(), _threat(cve_lookup=ctem_cve_software.LOOKUP_FAILED), today=TODAY)
    assert decision.record["reason_code"] == "subject_unresolved"
    assert decision.record["reconcile"]["cve_lookup"] == "OpenCTI did not answer"
    assert decision.record["reconcile"]["cve_software"] == []


def test_software_names_keeps_each_name_and_cpe_once():
    relations = [
        {"from": {"name": "Metabase", "cpe": "cpe:2.3:a:metabase:metabase:0.46.6:*:*:*:*:*:*:*"}},
        {"from": {"name": "Metabase", "cpe": None}},
        {"from": {}},
        {},
    ]
    assert ctem_cve_software.software_names(relations) == [
        "Metabase",
        "cpe:2.3:a:metabase:metabase:0.46.6:*:*:*:*:*:*:*",
    ]


@pytest.mark.asyncio
async def test_read_cve_software_follows_the_vulnerability_to_its_software(monkeypatch):
    calls: list[str] = []

    async def fake_graphql(client, base_url, headers, query, variables=None):
        calls.append(query)
        if "vulnerabilities" in query:
            assert variables["filters"]["filters"][0]["values"] == ["CVE-2023-38646"]
            return {"vulnerabilities": {"edges": [{"node": {"id": "v1", "name": "CVE-2023-38646"}}]}}, None
        assert variables["to"] == ["v1"]
        return {"stixCoreRelationships": {"edges": [{"node": {"from": {"name": "Metabase", "cpe": ""}}}]}}, None

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", fake_graphql)
    found = await ctem_cve_software.read_cve_software(None, "https://octi", {}, ["CVE-2023-38646"])
    assert found == ["Metabase"] and len(calls) == 2


@pytest.mark.asyncio
async def test_read_cve_software_answers_none_when_opencti_fails(monkeypatch):
    async def fake_graphql(client, base_url, headers, query, variables=None):
        return {}, "boom"

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", fake_graphql)
    assert await ctem_cve_software.read_cve_software(None, "https://octi", {}, ["CVE-2023-38646"]) is None


@pytest.mark.asyncio
async def test_cve_software_without_a_reader_reads_nothing():
    assert await ctem_cve_software.cve_software(SimpleNamespace(), None, ["CVE-2023-38646"]) == ((), "no reader")


@pytest.mark.asyncio
async def test_the_store_reads_the_cve_software_only_for_an_unresolved_cve_signal(monkeypatch):
    seen: list[tuple] = []

    async def fake_cve_software(db, reader, cves):
        seen.append((reader, tuple(cves)))
        return ("Metabase",), "read"

    async def no_fallback(db):
        return None

    monkeypatch.setattr(ctem_cve_software, "cve_software", fake_cve_software)
    monkeypatch.setattr(store, "default_scenario_reader", no_fallback)

    class _Nested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(info={}, begin_nested=lambda: _Nested())
    recorder = uuid.uuid4()
    row = SimpleNamespace(id="s1", recorded_by=str(recorder))
    facts = await store._with_cve_software(db, row, _threat(), _profile(), None)
    assert facts.cve_software == ("Metabase",) and facts.cve_lookup == "read"
    assert seen == [(recorder, ("CVE-2023-38646",))]
    # Resolved without it (a software subject in the inventory): no read.
    software = gate.SignalFacts(
        trigger_path="new_vulnerability",
        lens="world",
        subject_kind="software",
        subject_ref="Metabase",
        subject_label="CVE-2023-38646",
    )
    assert await store._with_cve_software(db, row, software, _profile(), None) is software
    # No CVE: no read.
    plain = gate.SignalFacts(trigger_path="new_threat", lens="world", subject_kind="threat", subject_ref="report--1")
    assert await store._with_cve_software(db, row, plain, _profile(), None) is plain
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_bulk_pass_reads_the_cve_software_up_front_and_never_in_its_loop(monkeypatch: Any) -> None:
    """#2171 review: the bulk re-decide paths read OpenCTI before any row is written, never inside the loop."""
    seen: list[tuple[Any, ...]] = []

    async def fake_cve_software(db: Any, reader: Any, cves: list[str]) -> tuple[tuple[str, ...], str]:
        seen.append((reader, tuple(cves)))
        return ("Metabase",), "read"

    async def no_fallback(db: Any) -> None:
        return None

    monkeypatch.setattr(ctem_cve_software, "cve_software", fake_cve_software)
    monkeypatch.setattr(store, "default_scenario_reader", no_fallback)
    threat = _threat()
    monkeypatch.setattr(store, "facts_of", lambda row: threat)

    class _Nested:
        async def __aenter__(self) -> _Nested:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

    db: Any = SimpleNamespace(info={}, begin_nested=lambda: _Nested())
    recorder = uuid.uuid4()
    org = uuid.uuid4()
    row: Any = SimpleNamespace(id="s1", recorded_by=str(recorder), state="to_validate", organization_id=org)
    # Not prepared: the loop's read is skipped, the facts stay as they are.
    assert await store._with_cve_software(db, row, threat, _profile(), None, remote=False) is threat
    assert seen == []
    await store.prepare_cve_reads(db, [row, row], profiles={org: _profile()}, links={org: None})
    assert seen == [(recorder, ("CVE-2023-38646",))]
    facts = await store._with_cve_software(db, row, threat, _profile(), None, remote=False)
    assert facts.cve_software == ("Metabase",) and facts.cve_lookup == "read"
    assert len(seen) == 1


def test_the_hint_names_the_read_keys_only_when_the_gate_left_the_signal_unresolved():
    unresolved = {"reason_code": "subject_unresolved"}
    hint = tools.evidence_hint({"stack_fit": "Metabase on metabase.data.example.com"}, unresolved)
    assert hint and "hostnames" in hint and "products" in hint
    assert tools.evidence_hint({"hostnames": ["metabase.data.example.com"]}, unresolved) is None
    assert tools.evidence_hint({"stack_fit": "free text"}, {"reason_code": "in_scope"}) is None
    assert tools.evidence_hint(None, None) is None


def test_the_evidence_description_names_every_key_the_gate_reads():
    for key in (*gate.ASSET_EVIDENCE_KEYS, *gate.SOFTWARE_EVIDENCE_KEYS, "cve"):
        if key == "asset":
            continue
        assert key in tools.EVIDENCE_DESCRIPTION
    assert gate.evidence_read_keys({"products": ["Metabase"], "stack_fit": "x", "cpe": ""}) == ["products"]
