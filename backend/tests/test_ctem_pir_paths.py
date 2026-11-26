"""PIRs per trigger path (#2436): one PIR playbook per path over its own selection, each PIR at its minimum."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_platform_sources as sources
from app.services import opencti_trigger_provisioning as prov
from app.services import organization_context as org_context
from app.services import organization_pirs
from app.services.ctem_signal_sources import PIR_PATHS, pir_selection

NOW = datetime(2026, 11, 26, 7, 0, tzinfo=UTC)
ENABLED = {"v1": "VULNERABILITY", "v2": "VULNERABILITY", "t1": "THREAT_LANDSCAPE", "t2": "THREAT_ORIGIN"}


def test_the_default_selection_is_the_pirs_of_the_path_type() -> None:
    assert pir_selection("new_vulnerability", ENABLED, {})["pir_ids"] == ["v1", "v2"]
    assert pir_selection("relevant_threat", ENABLED, {})["pir_ids"] == ["t1", "t2"]


def test_a_path_with_no_pir_of_its_type_says_none_is_available_and_keeps_listening() -> None:
    threats_only = {"t1": "THREAT_LANDSCAPE"}
    out = pir_selection("new_vulnerability", threats_only, None)
    assert out["available"] == [] and out["default"] is True
    # Before #2436 the single playbook listened to every PIR: the upgrade never silences a path.
    assert out["pir_ids"] == ["t1"]


def test_a_chosen_selection_wins_and_drops_a_pir_no_longer_enabled() -> None:
    out = pir_selection("new_vulnerability", ENABLED, {"new_vulnerability": ["v2", "gone"]})
    assert out == {"pir_ids": ["v2"], "available": ["v1", "v2"], "default": False}


def test_an_empty_selection_switches_the_path_off() -> None:
    assert pir_selection("relevant_threat", ENABLED, {"relevant_threat": []})["pir_ids"] == []


def test_the_profile_refuses_a_pir_selection_on_a_path_no_playbook_feeds() -> None:
    with pytest.raises(ValueError):
        org_context.SignalSourcesSettings(pirs={"credential_leak": ["p"]})
    assert org_context.SignalSourcesSettings(pirs={"relevant_threat": ["b", "a", "a"]}).pirs == {
        "relevant_threat": ["a", "b"]
    }


@pytest.fixture
def profile(monkeypatch: pytest.MonkeyPatch) -> Any:
    saved = org_context.parse_profile({})
    rows = [
        SimpleNamespace(
            pir_id=pid,
            name=pid,
            enabled=True,
            min_score=score,
            pir_type=ENABLED[pid],
            question="",
            owner="",
            review_cadence="",
            primary_assets=[],
        )
        for pid, score in (("v1", 40), ("v2", 80), ("t1", 60), ("t2", 60))
    ]

    async def current_version(_db: Any, _org: Any = None) -> Any:
        return SimpleNamespace(profile={})

    async def overlay(_db: Any, p: Any) -> Any:
        return organization_pirs.overlay(p, rows)  # type: ignore[arg-type]  # rows stand in for OrganizationPir

    monkeypatch.setattr(org_context, "current_version", current_version)
    monkeypatch.setattr(org_context, "parse_profile", lambda _raw: saved)
    monkeypatch.setattr(organization_pirs, "overlay_profile", overlay)
    return saved


async def test_two_vulnerability_pirs_each_filter_at_their_own_minimum(profile: Any) -> None:
    spec = await sources._spec(None, "opencti", "pir_playbook", path="new_vulnerability", pir_ids=["v1", "v2"])
    assert spec["pir_ids"] == ["v1", "v2"]
    assert spec["entity_types"] == list(PIR_PATHS["new_vulnerability"])
    filters = prov.pir_score_filters(spec)
    assert [(f["values"][0]["values"], f["values"][1]["values"]) for f in filters] == [
        (["40"], ["v1"]),
        (["80"], ["v2"]),
    ]
    threat = await sources._spec(None, "opencti", "pir_playbook", path="relevant_threat", pir_ids=["t1"])
    assert threat["pir_ids"] == ["t1"] and "Vulnerability" not in threat["entity_types"]
    assert prov.object_name(spec) != prov.object_name(threat)


class FakeModule:
    def __init__(self) -> None:
        self.removed: list[dict[str, Any]] = []
        self.provisioned: dict[str, list[str]] = {}

    async def reconcile(
        self, _client: Any, spec: dict[str, Any], state: Any, *, url: str, secret: str, now: datetime
    ) -> dict[str, Any]:
        self.provisioned[spec["purpose"]] = list(spec["pir_ids"])
        return {
            "status": "provisioned",
            "playbook_id": f"pb-{spec['purpose']}",
            "notifier_id": "n",
            "fingerprint": "f",
            "checked_at": now.isoformat(),
        }

    async def remove(self, _client: Any, state: dict[str, Any]) -> None:
        self.removed.append(dict(state))
        return None


async def test_each_path_gets_its_own_playbook(monkeypatch: pytest.MonkeyPatch, profile: Any) -> None:
    module = FakeModule()

    async def selections(_db: Any) -> dict[str, list[str]]:
        return {"new_vulnerability": ["v1", "v2"], "relevant_threat": ["t1"]}

    monkeypatch.setattr(sources, "_module", lambda *_a: module)
    monkeypatch.setattr(sources, "pir_path_selections", selections)
    row = SimpleNamespace(
        id=uuid.uuid4(),
        provider="opencti",
        kind="pir_playbook",
        state={"status": "provisioned", "playbook_id": "old", "notifier_id": "old-n"},
    )
    state = await sources._reconcile_pir_paths(None, row, (object(), "integ", "s"), NOW)
    assert state["status"] == "provisioned" and sources.is_connected(state)
    assert module.provisioned == {
        "OpenCTI PIR playbook - new_vulnerability": ["v1", "v2"],
        "OpenCTI PIR playbook - relevant_threat": ["t1"],
    }
    assert len(state["playbook_ids"]) == 2
    assert module.removed == [{"notifier_id": "old-n", "playbook_id": "old"}]


async def test_an_empty_selection_removes_its_playbook(monkeypatch: pytest.MonkeyPatch, profile: Any) -> None:
    module = FakeModule()

    async def selections(_db: Any) -> dict[str, list[str]]:
        return {"new_vulnerability": [], "relevant_threat": ["t1"]}

    monkeypatch.setattr(sources, "_module", lambda *_a: module)
    monkeypatch.setattr(sources, "pir_path_selections", selections)
    held = {"status": "provisioned", "playbook_id": "pb-v", "notifier_id": "n-v"}
    row = SimpleNamespace(
        id=uuid.uuid4(),
        provider="opencti",
        kind="pir_playbook",
        state={"status": "provisioned", "paths": {"new_vulnerability": held}},
    )
    state = await sources._reconcile_pir_paths(None, row, (object(), "integ", "s"), NOW)
    assert set(state["paths"]) == {"relevant_threat"}
    assert module.removed == [held]
