# SPDX-License-Identifier: Apache-2.0

"""#2523: the scope gate reads the software an OpenCTI threat targets or uses before it parks
the threat Unmatched, with the recorder's own access, never inside the bulk passes' loop."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_cve_software, ctem_threat_software
from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

TODAY = date(2026, 11, 26)
_NO_CLIENT: Any = None
THREAT_ID = "6e1f2b0a-0000-4000-8000-000000000001"


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "software": [{"product": "Ivanti Connect Secure"}],
            "assets_exposure": [
                {"name": "vpn.example.com", "internet_facing": "yes", "products": ["Ivanti Connect Secure"]},
            ],
        }
    )


def _malware(**kw: Any) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="ShadowPad",
        subject_label="ShadowPad",
        source_provider="opencti",
        source_external_id=THREAT_ID,
        evidence={"event": "create", "stix_type": "malware", "stix_id": "malware--1"},
        **kw,
    )


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _db() -> Any:
    return SimpleNamespace(info={}, begin_nested=lambda: _Nested())


async def _no_fallback(db: Any) -> None:
    return None


def test_a_listener_threat_without_software_stays_unmatched() -> None:
    decision = gate.decide(_profile(), _malware(), today=TODAY)
    assert decision.record["reason_code"] == "subject_unresolved"
    # #2542: needed and not taken by this decision: said so, and the decision is incomplete.
    assert decision.record["reconcile"]["threat_lookup"] == gate.LOOKUP_NOT_READ
    assert decision.record["reconcile"]["cve_lookup"] == gate.LOOKUP_NOT_APPLICABLE
    assert decision.record["incomplete"] is True


def test_the_software_it_targets_resolves_it_to_the_inventory() -> None:
    facts = _malware(threat_software=("Ivanti Connect Secure",), threat_lookup="read")
    decision = gate.decide(_profile(), facts, today=TODAY)
    reconcile = decision.record["reconcile"]
    assert decision.record["reason_code"] != "subject_unresolved"
    assert reconcile["products"] == ["Ivanti Connect Secure"]
    assert "vpn.example.com" in reconcile["assets"]
    assert reconcile["threat_software"] == ["Ivanti Connect Secure"]
    assert reconcile["threat_lookup"] == "read"


def test_software_out_of_the_stack_is_tried_and_named() -> None:
    facts = _malware(threat_software=("Fortinet FortiOS",), threat_lookup="read")
    decision = gate.decide(_profile(), facts, today=TODAY)
    assert decision.record["reason_code"] == "subject_unresolved"
    assert "Fortinet FortiOS" in decision.record["reconcile"]["software_refs"]


def test_the_threat_id_is_read_for_opencti_threats_only() -> None:
    assert store.threat_opencti_id(_malware()) == THREAT_ID
    no_internal = gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="APT1",
        source_provider="opencti",
        evidence={"stix_type": "intrusion-set", "stix_id": "intrusion-set--1"},
    )
    assert store.threat_opencti_id(no_internal) == "intrusion-set--1"
    report = gate.SignalFacts(
        trigger_path="new_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="report--1",
        source_provider="opencti",
        evidence={"stix_type": "report"},
    )
    assert store.threat_opencti_id(report) == ""
    other = gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="x",
        source_provider="recorded_future",
        source_external_id="x",
        evidence={"stix_type": "malware"},
    )
    assert store.threat_opencti_id(other) == ""


@pytest.mark.asyncio
async def test_read_threat_software_follows_targets_uses_and_targeted_vulnerabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_graphql(client: Any, base_url: Any, headers: Any, query: Any, variables: Any = None) -> Any:
        calls.append(variables)
        if "fromId" in query:
            assert variables["from"] == [THREAT_ID]
            return {
                "stixCoreRelationships": {
                    "edges": [
                        {"node": {"to": {"name": "Ivanti Connect Secure", "cpe": "cpe:2.3:a:ivanti:connect_secure"}}},
                        {"node": {"to": {"vulnerability_id": "v1"}}},
                        {"node": {"to": {}}},
                    ]
                }
            }, None
        assert variables["to"] == ["v1"]
        return {"stixCoreRelationships": {"edges": [{"node": {"from": {"name": "Ivanti Policy Secure"}}}]}}, None

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", fake_graphql)
    found = await ctem_threat_software.read_threat_software(_NO_CLIENT, "https://octi", {}, THREAT_ID)
    assert found == ["Ivanti Connect Secure", "cpe:2.3:a:ivanti:connect_secure", "Ivanti Policy Secure"]
    assert len(calls) == 2


_SIBLING_FRAGMENTS = re.compile(r"(?:\.\.\. on \w+ \{[^{}]*\}\s*){2,}")
_FRAGMENT = re.compile(r"\.\.\. on (\w+) \{([^{}]*)\}")
_FIELD = re.compile(r"(\w+)\s*(:\s*\w+)?")


def _response_keys(selection: str) -> set[str]:
    """The response keys of a flat selection: the alias when one is given, else the field name."""
    return {match.group(1) for match in _FIELD.finditer(selection)}


@pytest.mark.parametrize(
    "query",
    [
        ctem_threat_software._OCTI_THREAT_TARGETS,
        ctem_cve_software._OCTI_CVE_SOFTWARE,
        ctem_cve_software._OCTI_VULNS,
    ],
)
def test_sibling_inline_fragments_share_no_response_key(query: str) -> None:
    """OpenCTI refuses sibling fragments whose shared key differs in type (``String`` vs ``String!``).

    The read tests mock ``_graphql``, so only this check stops a query OpenCTI rejects at validation.
    """
    for group in _SIBLING_FRAGMENTS.finditer(query):
        seen: dict[str, str] = {}
        for fragment in _FRAGMENT.finditer(group.group(0)):
            for key in _response_keys(fragment.group(2)):
                assert key not in seen, f"{key!r} is read on both {seen[key]} and {fragment.group(1)}"
                seen[key] = fragment.group(1)


def test_the_fragment_check_catches_the_shipped_conflict() -> None:
    shipped = "to { ... on Software { name cpe } ... on Vulnerability { id name } }"
    with pytest.raises(AssertionError, match="'name'"):
        test_sibling_inline_fragments_share_no_response_key(shipped)


@pytest.mark.asyncio
async def test_read_threat_software_without_relations_answers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_graphql(client: Any, base_url: Any, headers: Any, query: Any, variables: Any = None) -> Any:
        return {"stixCoreRelationships": {"edges": []}}, None

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", fake_graphql)
    assert await ctem_threat_software.read_threat_software(_NO_CLIENT, "https://octi", {}, THREAT_ID) == []


@pytest.mark.asyncio
async def test_read_threat_software_answers_none_when_opencti_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_graphql(client: Any, base_url: Any, headers: Any, query: Any, variables: Any = None) -> Any:
        return {}, "boom"

    monkeypatch.setattr("app.integrations.providers.opencti._common._graphql", fake_graphql)
    assert await ctem_threat_software.read_threat_software(_NO_CLIENT, "https://octi", {}, THREAT_ID) is None


@pytest.mark.asyncio
async def test_threat_software_without_a_reader_reads_nothing() -> None:
    assert await ctem_threat_software.threat_software(_db(), None, THREAT_ID) == ((), "no reader")


@pytest.mark.asyncio
async def test_threat_software_without_opencti_access_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_metrics_collector

    seen: dict[str, Any] = {}

    async def _creds(db: Any, user_id: Any, provider: Any, *, release: Any = True) -> Any:
        seen["release"] = release
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _creds)
    found = await ctem_threat_software.threat_software(_db(), uuid.uuid4(), THREAT_ID)
    assert found == ((), ctem_cve_software.LOOKUP_NO_ACCESS)
    # The record-time gate decides inside a savepoint: the credential read never releases by default.
    assert seen["release"] is False


@pytest.mark.asyncio
async def test_threat_software_times_out_and_records_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.opencti import capabilities
    from app.services import ctem_metrics_collector

    async def _creds(db: Any, user_id: Any, provider: Any, *, release: Any = True) -> Any:
        return "https://octi", {}

    async def _slow(*_args: Any, **_kw: Any) -> Any:
        await asyncio.sleep(1)
        return ["never"]

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", _creds)
    monkeypatch.setattr(capabilities, "attach_probe", lambda headers, _url: headers)
    monkeypatch.setattr(ctem_threat_software, "read_threat_software", _slow)
    monkeypatch.setattr(ctem_threat_software, "LOOKUP_TIMEOUT_SECONDS", 0.05)
    found = await ctem_threat_software.threat_software(_db(), uuid.uuid4(), THREAT_ID)
    assert found == ((), ctem_cve_software.LOOKUP_FAILED)


@pytest.mark.asyncio
async def test_the_store_reads_only_an_unresolved_threat_once_per_session(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []

    async def fake_threat_software(db: Any, reader: Any, threat_id: Any, *, release: Any = False) -> Any:
        seen.append((reader, threat_id, release))
        return ("Ivanti Connect Secure",), "read"

    monkeypatch.setattr(ctem_threat_software, "threat_software", fake_threat_software)
    monkeypatch.setattr(store, "default_scenario_reader", _no_fallback)
    db = _db()
    recorder = uuid.uuid4()
    row: Any = SimpleNamespace(id="s1", recorded_by=str(recorder))
    facts = await store._with_threat_software(db, row, _malware(), _profile(), None)
    assert facts.threat_software == ("Ivanti Connect Secure",) and facts.threat_lookup == "read"
    assert seen == [(recorder, THREAT_ID, False)]
    # Cached per session, reader and threat id.
    await store._with_threat_software(db, row, _malware(), _profile(), None)
    assert len(seen) == 1
    # Resolved on its evidence alone: no read.
    base = _malware()
    resolved = replace(base, evidence={**base.evidence, "targets": ["Ivanti Connect Secure"]})
    assert await store._with_threat_software(_db(), row, resolved, _profile(), None) is resolved
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_an_opencti_failure_is_recorded_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(db: Any, reader: Any, threat_id: Any, *, release: Any = False) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(ctem_threat_software, "threat_software", broken)
    monkeypatch.setattr(store, "default_scenario_reader", _no_fallback)
    row: Any = SimpleNamespace(id="s1", recorded_by=str(uuid.uuid4()))
    facts = await store._with_threat_software(_db(), row, _malware(), _profile(), None)
    assert facts.threat_software == () and facts.threat_lookup == "OpenCTI did not answer"


@pytest.mark.asyncio
async def test_the_hourly_recheck_reads_up_front_with_the_connection_released(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2516: the bulk prefetch has no savepoint open and releases; the pass then decides from the cache."""
    seen: list[tuple[Any, ...]] = []

    async def fake_threat_software(db: Any, reader: Any, threat_id: Any, *, release: Any = False) -> Any:
        seen.append((reader, threat_id, release))
        return ("Ivanti Connect Secure",), "read"

    monkeypatch.setattr(ctem_threat_software, "threat_software", fake_threat_software)
    monkeypatch.setattr(store, "default_scenario_reader", _no_fallback)
    threat = _malware()
    monkeypatch.setattr(store, "facts_of", lambda row: threat)
    db = _db()
    recorder = uuid.uuid4()
    org = uuid.uuid4()
    row: Any = SimpleNamespace(id="s1", recorded_by=str(recorder), state="new", organization_id=org)
    # Not prepared: the loop's read is skipped, the facts stay as they are.
    assert await store._with_threat_software(db, row, threat, _profile(), None, remote=False) is threat
    assert seen == []
    await store.prepare_cve_reads(db, [row, row], profiles={org: _profile()}, links={org: None})
    assert seen == [(recorder, THREAT_ID, True)]
    facts = await store._with_remote_software(db, row, threat, _profile(), None, remote=False)
    assert facts.threat_software == ("Ivanti Connect Secure",) and facts.threat_lookup == "read"
    assert len(seen) == 1
