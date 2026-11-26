# SPDX-License-Identifier: Apache-2.0
"""#2435: the Intelligence policy, OpenCTI PIRs stored in XTM One with a minimum score each."""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.models.organization_pir import OrganizationPir
from app.services import ctem_trigger_rules as rules
from app.services import opencti_trigger_provisioning as prov
from app.services import organization_pirs as pirs
from app.services.organization_context import OrganizationProfile

NOW = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)
OCTI = uuid.uuid4()


def _row(pir_id: str, *, enabled: bool = True, min_score: int = 75, integration_id=OCTI, **kw) -> OrganizationPir:
    return OrganizationPir(
        id=uuid.uuid4(),
        integration_id=integration_id,
        pir_id=pir_id,
        name=kw.get("name", f"PIR {pir_id}"),
        question=kw.get("question", ""),
        owner=kw.get("owner", ""),
        review_cadence=kw.get("review_cadence", ""),
        primary_assets=kw.get("primary_assets", []),
        min_score=min_score,
        enabled=enabled,
    )


# --- Acceptance 1: PIRs listed with no sync configured -------------------------------------------


def test_a_read_fills_the_table_with_new_pirs_disabled():
    nodes = [
        {
            "id": "pir-1",
            "name": "Ransomware on energy",
            "pir_type": "THREAT_LANDSCAPE",
            "pir_criteria": [{"weight": 2}],
        },
        {"id": "pir-2", "name": "", "description": "Retail", "pir_criteria": [{"weight": 1}, {"weight": 3}]},
    ]
    created = pirs.apply_read([], OCTI, nodes, NOW)
    assert [(r.pir_id, r.name, r.enabled, r.min_score) for r in created] == [
        ("pir-1", "Ransomware on energy", False, pirs.DEFAULT_MIN_SCORE),
        ("pir-2", "pir-2", False, pirs.DEFAULT_MIN_SCORE),
    ]
    assert created[0].criteria_summary == "1 criterion, weight 2"
    assert created[1].criteria_summary == "2 criteria, weights 1-3"
    assert all(r.integration_id == OCTI and r.last_seen_at == NOW for r in created)


def test_a_pir_gone_from_opencti_is_marked_gone_never_removed():
    kept, gone = _row("pir-1"), _row("pir-2")
    created = pirs.apply_read([kept, gone], OCTI, [{"id": "pir-1", "name": "Renamed"}], NOW)
    assert created == []
    assert kept.name == "Renamed" and kept.gone_at is None
    assert gone.gone_at == NOW and gone.enabled is True


def test_a_carried_pir_without_source_is_adopted_by_the_first_read():
    carried = _row("pir-1", integration_id=None, min_score=80, owner="SOC")
    created = pirs.apply_read([carried], OCTI, [{"id": "pir-1", "name": "Ransomware"}], NOW)
    assert created == [] and carried.integration_id == OCTI
    assert (carried.min_score, carried.owner, carried.enabled) == (80, "SOC", True)


def test_another_opencti_never_marks_these_pirs_gone():
    other = uuid.uuid4()
    row = _row("pir-1")
    pirs.apply_read([row], other, [], NOW)
    assert row.gone_at is None


# --- Acceptance 2: per-PIR minimum 80 vs 40 filters the playbook per PIR -------------------------


def _spec(thresholds: dict[str, int]) -> dict:
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {"organization_context": ["pir_ids"], "mode": "pir_playbook", "fleet": "F", "purpose": "P"},
        }
    )
    profile = OrganizationProfile.model_validate({"pir_ids": sorted(thresholds), "pir_min_scores": thresholds})
    derived, missing = prov.derive_from_organization_context(spec, profile)
    assert missing == []
    return derived


def test_two_minimums_provision_one_score_filter_each():
    spec = _spec({"pir-a": 80, "pir-b": 40})
    filters = prov.pir_score_filters(spec)
    assert [(f["values"][0]["values"], f["values"][1]["values"]) for f in filters] == [
        (["40"], ["pir-b"]),
        (["80"], ["pir-a"]),
    ]
    entry, _notify = prov.pir_playbook_nodes(spec, "notifier", "member", "Member", {})
    playbook_filters = json.loads(entry["filters"])
    nested = playbook_filters["filterGroups"][0]["filters"]
    assert [(f["values"][0]["values"][0], f["values"][1]["values"]) for f in nested] == [
        ("40", ["pir-b"]),
        ("80", ["pir-a"]),
    ]
    assert sorted(c["value"] for c in entry["inPirFilters"]) == ["pir-a", "pir-b"]


def test_a_flag_under_its_own_minimum_records_no_signal():
    own = {"pir-a": 80, "pir-b": 40}
    assert rules.pir_drop_reason("threat", 75, {"pir-a": 79}, own) is not None
    assert rules.pir_drop_reason("threat", 75, {"pir-b": 41}, own) is None
    assert rules.pir_floor_met([{"pir_id": "pir-a", "score_at_delivery": 79}], 75, own) is False
    assert rules.pir_floor_met([{"pir_id": "pir-b", "score_at_delivery": 45}], 75, own) is True


def test_one_minimum_keeps_the_single_filter_as_before():
    spec = _spec({"pir-a": 70, "pir-b": 70})
    assert len(prov.pir_score_filters(spec)) == 1


# --- Acceptance 3: a disabled PIR leaves the playbook at the next pass ---------------------------


def test_a_disabled_pir_leaves_the_profile_pir_ids():
    profile = OrganizationProfile.model_validate({"pir_ids": ["pir-a", "pir-b"]})
    rows = [_row("pir-a", min_score=80), _row("pir-b", enabled=False, min_score=40)]
    overlaid = pirs.overlay(profile, rows)
    assert overlaid.pir_ids == ["pir-a"]
    assert overlaid.pir_min_scores == {"pir-a": 80}
    spec = prov.read_provision_spec(
        {
            "provider": "opencti",
            "provision": {"organization_context": ["pir_ids"], "mode": "pir_playbook", "fleet": "F", "purpose": "P"},
        }
    )
    derived, _ = prov.derive_from_organization_context(spec, overlaid)
    assert derived["pir_ids"] == ["pir-a"]


def test_no_stored_pir_answers_the_profile_as_saved():
    profile = OrganizationProfile.model_validate({"pir_ids": ["pir-a"]})
    assert pirs.overlay(profile, []) is profile


def test_policy_edit_validates_and_writes():
    row = _row("pir-a")
    pirs.apply_edit(row, {"min_score": 80, "enabled": False, "review_cadence": "monthly", "owner": "SOC"}, NOW)
    assert (row.min_score, row.enabled, row.review_cadence, row.owner) == (80, False, "monthly", "SOC")
    for bad in ({"min_score": 101}, {"min_score": True}, {"review_cadence": "daily"}, {"enabled": "yes"}):
        with pytest.raises(pirs.PirPolicyError):
            pirs.apply_edit(row, bad, NOW)


# --- Acceptance 4: a 34-PIR profile migrates enabled with details and thresholds -----------------


def _migration():
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "i2435pirpolicy01_add_organization_pirs.py"
    spec = importlib.util.spec_from_file_location("i2435pirpolicy01", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_a_34_pir_profile_migrates_with_details_and_thresholds():
    ids = [f"pir-{n:02d}" for n in range(34)]
    profile = {
        "pir_ids": ids,
        "pir_names": {"pir-00": "Ransomware on energy"},
        "pir_details": {
            "pir-00": {
                "question": "Who targets us?",
                "owner": "SOC",
                "review_cadence": "monthly",
                "primary_assets": ["Payments"],
            }
        },
        "prioritization_policy": {"pir_min_score_threat": 60},
    }
    rows = _migration().carried_rows(profile)
    assert [r["pir_id"] for r in rows] == ids
    assert all(r["min_score"] == 60 for r in rows)
    assert rows[0] == {
        "pir_id": "pir-00",
        "name": "Ransomware on energy",
        "question": "Who targets us?",
        "owner": "SOC",
        "review_cadence": "monthly",
        "primary_assets": ["Payments"],
        "min_score": 60,
    }


def test_a_profile_without_policy_migrates_at_the_former_default():
    rows = _migration().carried_rows({"pir_ids": ["a", "a", ""]})
    assert [(r["pir_id"], r["min_score"]) for r in rows] == [("a", 75)]


# --- #2459: the identity of a PIR read, and the refusal said -------------------------------------


def _user(email: str, *, active: bool = True):
    import types

    return types.SimpleNamespace(id=uuid.uuid4(), email=email, is_active=active)


def test_the_seed_account_is_never_the_identity_of_a_per_user_read(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", "admin@seed.example")
    seed, clicker, run_as, admin = (
        _user("admin@seed.example"),
        _user("ciso@example.com"),
        _user("soc@example.com"),
        _user("it@example.com"),
    )
    order = pirs.ordered_identities(clicker, [run_as, None], [seed, admin, clicker], seed)
    assert [u.email for u in order] == ["ciso@example.com", "soc@example.com", "it@example.com"]
    # The scheduler has no clicker: the signal source's run-as user goes first, never the seed owner.
    assert [u.email for u in pirs.ordered_identities(None, [run_as], [seed], seed)] == ["soc@example.com"]
    assert pirs.ordered_identities(None, [], [seed], seed) == []
    assert pirs.ordered_identities(_user("x@example.com", active=False), [], [], None) == []


def test_a_static_key_integration_keeps_the_seed_account_as_a_last_resort(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "admin_email", "admin@seed.example")
    seed = _user("admin@seed.example")
    assert pirs.ordered_identities(None, [], [seed], seed, per_user=False) == [seed]
    other = _user("it@example.com")
    assert pirs.ordered_identities(None, [], [seed, other], seed, per_user=False) == [other]


def test_a_refused_read_tries_the_next_identity_then_says_why(monkeypatch):
    import asyncio
    import types

    first, second = _user("a@example.com"), _user("b@example.com")
    integration = types.SimpleNamespace(id=OCTI, name="OpenCTI - CTEM", user_id=None)

    async def identities(_db, _integration, _acting):
        return [first, second]

    answers = {first.id: "PIRs unreadable: AUTH_REQUIRED", second.id: [{"id": "pir-1", "name": "Filigran"}]}

    async def read_as(_db, _integration, user_id):
        return answers[user_id]

    monkeypatch.setattr(pirs, "_identities", identities)
    monkeypatch.setattr(pirs, "_read_as", read_as)
    assert asyncio.run(pirs._read_integration(None, integration)) == [{"id": "pir-1", "name": "Filigran"}]

    answers[second.id] = "PIRs unreadable: AUTH_REQUIRED"
    reason = asyncio.run(pirs._read_integration(None, integration))
    assert reason == "a@example.com: PIRs unreadable: AUTH_REQUIRED; b@example.com: PIRs unreadable: AUTH_REQUIRED"

    async def nobody(_db, _integration, _acting):
        return []

    monkeypatch.setattr(pirs, "_identities", nobody)
    assert "no identity to read the PIRs with" in asyncio.run(pirs._read_integration(None, integration))


def test_the_manual_refresh_reads_as_the_clicker_and_answers_the_errors(monkeypatch):
    import asyncio
    import types

    from app.api.v1 import organization_context as route

    seen: dict = {}

    async def refresh_all(_db, *, acting_user_id=None):
        seen["acting"] = acting_user_id
        return {"read": 0, "added": 0, "errors": {str(OCTI): "refused"}, "names": {str(OCTI): "OpenCTI - CTEM"}}

    async def list_rows(_db):
        return []

    class _Db:
        async def commit(self):
            return None

    monkeypatch.setattr(pirs, "refresh_all", refresh_all)
    monkeypatch.setattr(pirs, "list_rows", list_rows)
    admin = types.SimpleNamespace(id=uuid.uuid4())
    response = asyncio.run(route.refresh_intelligence_policy(db=_Db(), admin=admin))
    assert seen["acting"] == admin.id
    assert [(e.integration, e.reason) for e in response.errors] == [("OpenCTI - CTEM", "refused")]
    assert response.total == 0
