"""Draft of the organization profile from the connected platforms (#8987, slice 2b).

The mapping and the merge are pure; the platform reads are covered by
stubbing the three readers, so the rules hold without a network: only an
empty field is filled, each one names its platform, an unreadable platform
is reported and never fails the draft, nothing is saved.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import organization_context as oc
from app.services import organization_context_draft as d


def _facts() -> d.PlatformFacts:
    facts = d.PlatformFacts()
    d.opencti_facts(
        {"settings": {"platform_organization": {"name": "Acme Energy", "sectors": {"edges": [
            {"node": {"name": "Energy"}}, {"node": {"name": "Utilities"}}]}}}},
        {"pirs": {"edges": [{"node": {"id": "pir-1"}}, {"node": {"id": "pir-1"}}, {"node": {"id": "pir-2"}}]}},
        facts,
    )
    d.openaev_facts(
        [{"asset_name": "CrowdStrike Falcon"}, {"asset_name": "crowdstrike falcon"}, {"asset_name": "Splunk"}, {}],
        facts,
    )
    d.opencrq_facts(
        [
            # OpenCRQ primary assets, rated by their security needs (#1591).
            {"id": "a1", "name": "Billing", "integrity": "high"},
            {"id": "a2", "name": "SCADA", "availability": "very_high"},
            {"id": "a3", "name": "Wiki", "availability": "low"},
            {"id": "", "name": "Orphan", "availability": "very_high"},
        ],
        facts,
    )
    return facts


def test_each_platform_maps_to_its_fields_with_its_source():
    facts = _facts()
    assert (facts.name, facts.sector) == ("Acme Energy", "Energy")
    assert facts.pir_ids == ["pir-1", "pir-2"]
    assert [t.product for t in facts.tech_stack] == ["CrowdStrike Falcon", "Splunk"]
    assert [(j.name, j.criticality, j.opencrq_asset_id) for j in facts.crown_jewels] == [
        ("SCADA", "critical", "a2"),
        ("Billing", "high", "a1"),
        ("Wiki", "low", "a3"),
    ]
    assert facts.sources == {
        "name": "opencti",
        "sector": "opencti",
        "pir_ids": "opencti",
        "tech_stack": "openaev",
        "crown_jewels": "opencrq",
    }


def test_the_draft_fills_only_the_empty_fields_and_keeps_the_input_untouched():
    current = oc.parse_profile({"name": "Acme (typed)", "crown_jewels": [{"name": "ERP"}]})
    draft = d.merge_draft(current, _facts())
    assert draft.profile.name == "Acme (typed)"
    assert [j.name for j in draft.profile.crown_jewels] == ["ERP"]
    assert draft.profile.sector == "Energy"
    assert draft.profile.pir_ids == ["pir-1", "pir-2"]
    assert [f["field"] for f in draft.filled] == ["sector", "pir_ids", "tech_stack"]
    assert {f["field"]: f["source"] for f in draft.filled}["tech_stack"] == "openaev"
    assert current.sector == "" and current.pir_ids == []


def test_nothing_from_the_platforms_fills_nothing_and_guesses_nothing():
    draft = d.merge_draft(oc.OrganizationProfile(), d.PlatformFacts(unavailable=["OpenCTI: down"]))
    assert draft.filled == []
    assert draft.unavailable == ["OpenCTI: down"]
    assert oc.missing_fields(draft.profile) == [path for path, _ in oc.CONTRACT_FIELDS]


def test_an_opencti_without_organization_or_pirs_leaves_those_fields_out():
    facts = d.PlatformFacts()
    d.opencti_facts({"settings": {"platform_organization": None}}, {}, facts)
    assert (facts.name, facts.sector, facts.pir_ids, facts.sources) == ("", "", [], {})


async def test_a_failing_platform_is_reported_and_the_others_still_draft(monkeypatch):
    async def opencrq(db, user_id, tenant_id, facts):
        raise RuntimeError("boom")

    async def opencti(db, user_id, facts):
        facts.unavailable.append("OpenCTI: not connected, or no token for this administrator")

    async def openaev(db, user_id, facts):
        d.openaev_facts([{"asset_name": "Splunk"}], facts)

    monkeypatch.setattr(d, "_read_opencrq", opencrq)
    monkeypatch.setattr(d, "_read_opencti", opencti)
    monkeypatch.setattr(d, "_read_openaev", openaev)
    facts = await d.collect_facts(SimpleNamespace(), user_id=uuid.uuid4())
    assert facts.unavailable == [
        "OpenCRQ: RuntimeError",
        "OpenCTI: not connected, or no token for this administrator",
    ]
    assert [t.product for t in facts.tech_stack] == ["Splunk"]


async def test_the_draft_starts_from_the_current_version_and_saves_nothing(monkeypatch):
    row = SimpleNamespace(version=3, profile={"name": "Acme"})

    async def current_version(db: Any, organization_id: Any = None) -> Any:
        return row

    async def hydrated_profile(db: Any, current: Any) -> Any:  # no asset records in this fake session (#1543)
        return oc.parse_profile(current.profile)

    async def collect_facts(db, *, user_id, tenant_id=""):
        facts = d.PlatformFacts(sector="Energy")
        facts.sources["sector"] = "opencti"
        return facts

    async def no_save(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("the draft saved a version")

    monkeypatch.setattr(oc, "current_version", current_version)
    monkeypatch.setattr(oc, "hydrated_profile", hydrated_profile)
    monkeypatch.setattr(oc, "save_version", no_save)
    monkeypatch.setattr(d, "collect_facts", collect_facts)
    draft, version = await d.draft_profile(SimpleNamespace(), user_id=uuid.uuid4())
    assert version == 3
    assert (draft.profile.name, draft.profile.sector) == ("Acme", "Energy")
    assert draft.filled == [{"field": "sector", "source": "opencti", "detail": "Energy"}]


@pytest.mark.parametrize(("need", "expected"), [("medium", "medium"), ("low", "low"), ("", "high"), (None, "high")])
def test_every_primary_asset_is_a_row_rated_by_its_highest_need(need: str | None, expected: str) -> None:
    # #1591: no threshold on the draft; a primary asset with no need rated reads as high.
    facts = d.PlatformFacts()
    d.opencrq_facts([{"id": "a1", "name": "X", "confidentiality": need}], facts)
    assert [(j.name, j.criticality) for j in facts.crown_jewels] == [("X", expected)]
    assert facts.sources["crown_jewels"] == "opencrq"


def test_the_pir_picker_lists_each_pir_once_by_name():
    rows = d.opencti_pirs(
        {"pirs": {"edges": [
            {"node": {"id": "pir-1", "name": "Ransomware on energy"}},
            {"node": {"id": "pir-1", "name": "dup"}},
            {"node": {"id": "pir-2", "name": ""}},
            {"node": {"id": ""}},
        ]}}
    )
    assert rows == [{"id": "pir-1", "name": "Ransomware on energy"}, {"id": "pir-2", "name": "pir-2"}]
    assert d.opencti_pirs({}) == []


async def test_the_pir_list_says_why_when_opencti_is_not_connected(monkeypatch):
    from app.services import ctem_metrics_collector

    async def no_creds(db, user_id, provider):
        return None

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", no_creds)
    assert await d.list_pirs(SimpleNamespace(), user_id=uuid.uuid4()) == ([], "not_connected", "")


async def test_the_pir_list_gives_the_opencti_url_even_when_unreadable(monkeypatch):
    from app.integrations.providers.opencti import _common
    from app.services import ctem_metrics_collector

    async def creds(db, user_id, provider):
        return "https://opencti.example/", {"Authorization": "Bearer x"}

    async def no_pirs(client, base_url, headers, query, variables=None):
        return {}, "PIRs are Enterprise Edition"

    monkeypatch.setattr(ctem_metrics_collector, "_user_creds", creds)
    monkeypatch.setattr(_common, "_graphql", no_pirs)
    assert await d.list_pirs(SimpleNamespace(), user_id=uuid.uuid4()) == ([], "unreadable", "https://opencti.example")


async def test_the_pir_read_follows_every_page_past_the_first_hundred(monkeypatch):
    """#11589: 250 PIRs are read in 3 pages, none left unnamed."""
    from app.integrations.providers.opencti import _common

    total = 250
    calls: list[dict] = []

    async def paged(client, base_url, headers, query, variables=None):
        calls.append(dict(variables or {}))
        start = int(variables.get("after") or 0)
        end = min(start + variables["first"], total)
        edges = [{"node": {"id": f"pir-{i}", "name": f"PIR {i}"}} for i in range(start, end)]
        return {"pirs": {"edges": edges, "pageInfo": {"hasNextPage": end < total, "endCursor": str(end)}}}, None

    monkeypatch.setattr(_common, "_graphql", paged)
    data, err = await d.read_all_pirs(None, "https://opencti.example", {})
    assert err is None
    rows = d.opencti_pirs(data)
    assert len(rows) == total and rows[-1] == {"id": "pir-249", "name": "PIR 249"}
    assert [c.get("after") for c in calls] == [None, "100", "200"]


async def test_a_failing_page_answers_the_error_never_a_partial_list(monkeypatch):
    from app.integrations.providers.opencti import _common

    async def second_fails(client, base_url, headers, query, variables=None):
        if variables.get("after"):
            return {}, "timeout"
        page = {"edges": [{"node": {"id": "pir-1", "name": "A"}}], "pageInfo": {"hasNextPage": True, "endCursor": "c1"}}
        return {"pirs": page}, None

    monkeypatch.setattr(_common, "_graphql", second_fails)
    assert await d.read_all_pirs(None, "u", {}) == ({}, "timeout")


def test_the_platform_draft_carries_the_name_of_each_pir():
    facts = d.PlatformFacts()
    d.opencti_facts({}, {"pirs": {"edges": [{"node": {"id": "pir-1", "name": "Ransomware"}}, {"node": {"id": "pir-2"}}]}}, facts)
    draft = d.merge_draft(oc.OrganizationProfile(), facts)
    assert draft.profile.pir_ids == ["pir-1", "pir-2"]
    assert draft.profile.pir_names == {"pir-1": "Ransomware"}


def test_missing_names_fill_only_the_gaps_and_never_overwrite():
    rows = [{"id": "pir-1", "name": "Live one"}, {"id": "pir-2", "name": "Live two"}, {"id": "pir-3", "name": "pir-3"}]
    found = d.missing_pir_names(["pir-1", "pir-2", "pir-3", "pir-gone"], {"pir-1": "Saved name"}, rows)
    # pir-1 keeps its saved name, pir-3 has no real name, pir-gone keeps nothing new.
    assert found == {"pir-2": "Live two"}


async def test_every_save_names_the_pirs_it_stores(monkeypatch):
    async def live(db, *, user_id):
        return [{"id": "pir-1", "name": "Ransomware"}, {"id": "pir-2", "name": "Phishing"}], None, ""

    monkeypatch.setattr(d, "list_pirs", live)
    profile = oc.parse_profile({"pir_ids": ["pir-1", "pir-2", "pir-1"], "pir_names": {"pir-2": "Kept"}})
    named = await d.name_saved_pirs(SimpleNamespace(), user_id=uuid.uuid4(), profile=profile)
    assert named.pir_ids == ["pir-1", "pir-2"]
    assert named.pir_names == {"pir-1": "Ransomware", "pir-2": "Kept"}


async def test_a_failed_name_lookup_never_refuses_the_save(monkeypatch):
    async def down(db, *, user_id):
        raise RuntimeError("down")

    monkeypatch.setattr(d, "list_pirs", down)
    profile = oc.parse_profile({"pir_ids": ["pir-1"]})
    assert await d.name_saved_pirs(SimpleNamespace(), user_id=uuid.uuid4(), profile=profile) is profile


async def test_a_fully_named_profile_reads_nothing(monkeypatch):
    async def never(db, *, user_id):  # pragma: no cover - must never run
        raise AssertionError("OpenCTI read for nothing")

    monkeypatch.setattr(d, "list_pirs", never)
    profile = oc.parse_profile({"pir_ids": ["pir-1"], "pir_names": {"pir-1": "A"}})
    assert await d.name_saved_pirs(SimpleNamespace(), user_id=uuid.uuid4(), profile=profile) is profile


async def test_the_backfill_stores_missing_names_and_keeps_the_ids(monkeypatch):
    row = SimpleNamespace(profile={"name": "Acme", "pir_ids": ["pir-1", "pir-2"], "pir_names": {"pir-2": "Old"}})
    commits: list[bool] = []

    async def current_version(db):
        return row

    async def commit():
        commits.append(True)

    monkeypatch.setattr(oc, "current_version", current_version)
    db = SimpleNamespace(commit=commit)
    stored = await d.backfill_pir_names(db, [{"id": "pir-1", "name": "New"}, {"id": "pir-2", "name": "Renamed"}])
    assert stored == 1 and commits == [True]
    assert row.profile == {"name": "Acme", "pir_ids": ["pir-1", "pir-2"], "pir_names": {"pir-2": "Old", "pir-1": "New"}}
    # Nothing missing: nothing written.
    assert await d.backfill_pir_names(db, [{"id": "pir-1", "name": "New"}]) == 0 and commits == [True]
