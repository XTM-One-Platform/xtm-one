# SPDX-License-Identifier: Apache-2.0

"""CTEM-28 (#1161): evidence decay and blind spots from OpenCRQ evidence health, as signals."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services import ctem_evidence_health as health
from app.services import organization_context as oc

NOW = datetime(2026, 11, 22, 11, 0, tzinfo=UTC)
SCENARIO = {"id": "s-1", "name": "Data theft extortion"}


def _asset(asset_id: str, state: str, last: str | None = "2026-09-01T00:00:00.000Z", **kw: Any) -> dict[str, Any]:
    return {
        "supportingAssetId": asset_id,
        "supportingAssetName": kw.pop("name", f"host-{asset_id}"),
        "supportingAssetDerivedName": None,
        "markingRefs": kw.pop("markings", []),
        "impact": 0.4,
        "state": state,
        "lastEvidenceAt": last,
        "sources": [{"connectorId": None, "origin": "connector", "lastSeenAt": last}],
        **kw,
    }


def _health(
    *assets: dict[str, Any], sources: list[dict[str, Any]] | None = None, markings: Any = None
) -> dict[str, Any]:
    return {
        "assets": list(assets),
        "counts": {},
        "markingRefs": [] if markings is None else markings,
        "sources": sources or [],
        "total": len(assets),
    }


# ── profile ─────────────────────────────────────────────────────────────


def test_the_pass_is_off_by_default_and_stays_out_of_the_always_on_block() -> None:
    profile = oc.parse_profile({})
    assert profile.evidence_health.enabled is None
    assert profile.evidence_health.max_age_days == 30
    assert profile.evidence_health.cadence_hours == 24
    assert "evidence_health" in oc.EXPOSURE_FIELDS
    assert "evidence_health" not in dict(oc.CONTRACT_FIELDS)


def test_a_re_test_delay_out_of_bounds_is_refused() -> None:
    with pytest.raises(ValueError):
        oc.parse_profile({"evidence_health": {"max_age_days": 0}})


# ── the mapping, a pure function ────────────────────────────────────────


def test_expired_attested_and_never_measured_are_path_9() -> None:
    gaps = health.gaps_of(
        SCENARIO,
        _health(_asset("a1", "expired"), _asset("a2", "attested_only"), _asset("a3", "never_measured", last=None)),
    )
    assert [(g.trigger_path, g.state) for g in gaps] == [
        ("control_decay", "expired"),
        ("control_decay", "attested_only"),
        ("control_decay", "never_measured"),
    ]
    assert {g.event for g in gaps} == {health.EVIDENCE_EXPIRED}
    assert gaps[2].dedupe_key.endswith(":never_measured:never")


def test_no_source_and_unknown_version_are_path_12() -> None:
    gaps = health.gaps_of(SCENARIO, _health(_asset("a1", "no_source", last=None), _asset("a2", "version_unknown")))
    assert [(g.trigger_path, g.event) for g in gaps] == [
        ("blind_spot", health.COVERAGE_GAP),
        ("blind_spot", health.COVERAGE_GAP),
    ]


def test_a_current_asset_raises_nothing() -> None:
    assert health.gaps_of(SCENARIO, _health(_asset("a1", "current"))) == []


def test_an_emptied_source_is_one_path_12_gap_and_a_healthy_one_none() -> None:
    sources = [
        {
            "kind": "openaev-findings-sync",
            "lastRunAt": "2026-11-22T06:00:00Z",
            "latestTotal": 0,
            "previousTotal": 840,
            "emptied": True,
        },
        {
            "kind": "drata-sync",
            "lastRunAt": "2026-11-22T05:00:00Z",
            "latestTotal": 90,
            "previousTotal": 92,
            "emptied": False,
        },
    ]
    gaps = health.gaps_of(SCENARIO, _health(sources=sources))
    assert len(gaps) == 1
    assert gaps[0].trigger_path == "blind_spot" and gaps[0].state == "source_emptied"
    assert gaps[0].subject_ref == "opencrq:openaev-findings-sync"
    assert "not closed" in gaps[0].reason


def test_the_evidence_names_the_scenario_and_keeps_no_finding_content() -> None:
    gap = health.gaps_of(SCENARIO, _health(_asset("a1", "expired")))[0]
    assert gap.evidence["risk_scenario_id"] == "s-1"
    assert gap.evidence["opencrq_asset_ids"] == ["a1"]
    assert set(gap.evidence) == {
        "event",
        "state",
        "last_evidence_at",
        "opencrq_asset_ids",
        "risk_scenario_id",
        "risk_scenario",
        "impact",
        "sources",
    }


TLP_RED = "marking-definition--5e57c739-391a-4eb3-b6be-7d15ca92d5ed"


def test_a_marked_asset_is_never_named_in_a_signal() -> None:
    gaps = health.gaps_of(
        SCENARIO, _health(_asset("a1", "expired", name="srv-red", markings=[TLP_RED]), _asset("a2", "expired"))
    )
    assert [g.evidence["opencrq_asset_ids"] for g in gaps] == [["a2"]]
    assert all("srv-red" not in g.subject_label for g in gaps)


def test_an_asset_whose_markings_are_not_stated_fails_closed() -> None:
    asset = _asset("a1", "expired")
    del asset["markingRefs"]
    assert health.gaps_of(SCENARIO, _health(asset)) == []


def test_a_marked_or_unstated_scenario_names_no_asset_but_keeps_an_emptied_source() -> None:
    emptied = [
        {"kind": "openaev-findings-sync", "lastRunAt": None, "latestTotal": 0, "previousTotal": 9, "emptied": True}
    ]
    marked = health.gaps_of(SCENARIO, _health(_asset("a1", "expired"), sources=emptied, markings=[TLP_RED]))
    assert [g.state for g in marked] == ["source_emptied"]
    unstated = _health(_asset("a1", "expired"))
    del unstated["markingRefs"]
    assert health.gaps_of(SCENARIO, unstated) == []


def test_the_key_moves_only_when_the_state_or_the_last_evidence_moves() -> None:
    first = health.gaps_of(SCENARIO, _health(_asset("a1", "expired")))[0].dedupe_key
    same = health.gaps_of(SCENARIO, _health(_asset("a1", "expired")))[0].dedupe_key
    later = health.gaps_of(SCENARIO, _health(_asset("a1", "expired", last="2026-10-15T00:00:00.000Z")))[0].dedupe_key
    other = health.gaps_of(SCENARIO, _health(_asset("a1", "version_unknown")))[0].dedupe_key
    assert first == same
    assert len({first, later, other}) == 3


# ── the pass ────────────────────────────────────────────────────────────


class _Reader:
    def __init__(self, by_scenario: dict[str, dict[str, Any]]):
        self.by_scenario = by_scenario
        self.calls: list[tuple[str, int]] = []

    async def scenarios(self) -> list[dict[str, Any]]:
        return [{"id": sid, "name": sid} for sid in self.by_scenario]

    async def evidence_health(self, scenario_id: str, max_age_days: int) -> dict[str, Any]:
        self.calls.append((scenario_id, max_age_days))
        return self.by_scenario[scenario_id]


def _patch_record(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from app.services import ctem_scope_gate_store, ctem_signals

    recorded: list[dict[str, Any]] = []
    seen: set[str] = set()

    async def _record(db, **kw):
        key = kw["signal"].dedupe_key
        created = key not in seen
        seen.add(key)
        recorded.append({**kw, "created": created})
        return object(), created

    async def _evaluate(db, row, *, created):
        return None

    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    monkeypatch.setattr(ctem_scope_gate_store, "evaluate_after_record", _evaluate)
    return recorded


@pytest.mark.asyncio
async def test_pass_signals_are_recorded_by_system_on_the_inside_lens(monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass signal is recorded by ``system``: only an administrator reads it (ctem_signals visibility)."""
    from app.services import ctem_signals

    recorded = _patch_record(monkeypatch)
    reader = _Reader({"s-1": _health(_asset("a1", "expired"), _asset("a2", "no_source", last=None))})
    result = await health.run_pass(object(), organization_id=uuid.uuid4(), reader=reader, max_age_days=45, now=NOW)
    assert reader.calls == [("s-1", 45)]
    assert result["counts"]["new"] == 2
    assert result["counts"][health.EVIDENCE_EXPIRED] == 1 and result["counts"][health.COVERAGE_GAP] == 1
    assert {r["recorded_by"] for r in recorded} == {ctem_signals.SYSTEM_ACTOR}
    assert {r["signal"].lens for r in recorded} == {"inside"}
    assert {r["signal"].trigger_path for r in recorded} == {"control_decay", "blind_spot"}


@pytest.mark.asyncio
async def test_an_asset_gap_links_to_its_opencrq_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The case row's "Open in OpenCRQ" icon (#1357) reads the asset page the pass writes."""
    recorded = _patch_record(monkeypatch)
    reader = _Reader({"s-1": _health(_asset("a1", "expired"))})
    reader.asset_link = lambda asset_id: f"https://crq.example/t1/assets/{asset_id}/overview"  # type: ignore[attr-defined]
    db = cast(AsyncSession, object())  # record_signal is patched: the pass never reads the session
    await health.run_pass(db, organization_id=uuid.uuid4(), reader=reader, max_age_days=45, now=NOW)
    assert recorded[0]["signal"].evidence["link"] == "https://crq.example/t1/assets/a1/overview"


def test_the_opencrq_reader_builds_the_asset_page_or_nothing() -> None:
    reader = health.OpenCrqEvidenceReader("https://crq.example/", {}, "t1")
    assert reader.asset_link("a1") == "https://crq.example/t1/assets/a1/overview"
    assert reader.asset_link("../a1") is None


@pytest.mark.asyncio
async def test_two_identical_passes_give_no_new_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_record(monkeypatch)
    reader = _Reader({"s-1": _health(_asset("a1", "expired"))})
    first = await health.run_pass(object(), organization_id=uuid.uuid4(), reader=reader, max_age_days=30, now=NOW)
    second = await health.run_pass(object(), organization_id=uuid.uuid4(), reader=reader, max_age_days=30, now=NOW)
    assert first["counts"]["new"] == 1
    assert second["counts"]["new"] == 0


@pytest.mark.asyncio
async def test_an_asset_or_source_in_two_scenarios_is_recorded_once(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _patch_record(monkeypatch)
    emptied = [
        {
            "kind": "drata-sync",
            "lastRunAt": "2026-11-22T05:00:00Z",
            "latestTotal": 0,
            "previousTotal": 9,
            "emptied": True,
        }
    ]
    reader = _Reader(
        {
            "s-1": _health(_asset("a1", "expired"), sources=emptied),
            "s-2": _health(_asset("a1", "expired"), sources=emptied),
        }
    )
    await health.run_pass(object(), organization_id=uuid.uuid4(), reader=reader, max_age_days=30, now=NOW)
    assert len(recorded) == 2


@pytest.mark.asyncio
async def test_a_pass_checkpoints_every_batch_of_new_signals(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cut pass keeps what it recorded: every COMMIT_BATCH new signals reach the checkpoint (#2055)."""
    _patch_record(monkeypatch)
    total = health.COMMIT_BATCH * 2 + 3
    reader = _Reader({"s-1": _health(*(_asset(f"a{i}", "expired") for i in range(total)))})
    checkpoints: list[object] = []

    async def _checkpoint(pending: object) -> None:
        checkpoints.append(pending)

    result = await health.run_pass(
        object(),  # type: ignore[arg-type]
        organization_id=uuid.uuid4(),
        reader=reader,
        max_age_days=30,
        now=NOW,
        checkpoint=_checkpoint,
    )
    assert result["counts"]["new"] == total
    assert len(checkpoints) == 2
    # The pending events left after the last batch are the caller's to publish; each batch gets a fresh holder.
    assert result["pending"] is not checkpoints[-1] and checkpoints[0] is not checkpoints[1]


# ── the schedule ────────────────────────────────────────────────────────


class _TickDb:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def _patch_tick(monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True) -> dict[str, int]:
    from app.services import organizations

    slot = {"released": 0}

    async def _primary(db):
        return type("Org", (), {"id": uuid.uuid4()})()

    async def _version(db, organization_id):
        return type(
            "Row", (), {"profile": {"evidence_health": {"enabled": enabled, "integration": str(uuid.uuid4())}}}
        )()

    async def _due(hours):
        return True

    async def _release():
        slot["released"] += 1

    monkeypatch.setattr(organizations, "get_primary", _primary)
    monkeypatch.setattr(oc, "current_version", _version)
    monkeypatch.setattr(health, "pass_due", _due)
    monkeypatch.setattr(health, "release_pass_slot", _release)
    return slot


@pytest.mark.asyncio
async def test_the_pass_does_nothing_when_off(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_tick(monkeypatch, enabled=False)
    assert await health.scheduled_pass(_TickDb()) is None


@pytest.mark.asyncio
async def test_a_failed_opencrq_read_records_nothing_and_gives_the_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    slot = _patch_tick(monkeypatch)

    async def _reader(db, profile):
        return object()

    async def _fail(db, **kw):
        raise health.EvidenceReadError("OpenCRQ answered 503")

    monkeypatch.setattr(health, "reader_for", _reader)
    monkeypatch.setattr(health, "run_pass", _fail)
    db = _TickDb()
    assert await health.scheduled_pass(db) == {"skipped": "OpenCRQ answered 503"}
    assert db.rollbacks == 1 and db.commits == 0 and slot["released"] == 1


@pytest.mark.asyncio
async def test_a_skipped_pass_gives_the_slot_back(monkeypatch: pytest.MonkeyPatch) -> None:
    slot = _patch_tick(monkeypatch)

    async def _no_reader(db, profile):
        return "no OpenCRQ credential could be resolved for the evidence health integration"

    monkeypatch.setattr(health, "reader_for", _no_reader)
    result = await health.scheduled_pass(_TickDb())
    assert result is not None and str(result["skipped"]).startswith("no OpenCRQ credential")
    assert slot["released"] == 1


@pytest.mark.asyncio
async def test_a_committed_pass_keeps_the_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_case_loop

    slot = _patch_tick(monkeypatch)

    async def _reader(db, profile):
        return object()

    async def _ok(db, **kw):
        return {"pending": ctem_case_loop.PendingEvents(), "counts": {"new": 0}}

    async def _publish(pending):
        return None

    monkeypatch.setattr(health, "reader_for", _reader)
    monkeypatch.setattr(health, "run_pass", _ok)
    monkeypatch.setattr(ctem_case_loop, "publish_pending", _publish)
    db = _TickDb()
    assert await health.scheduled_pass(db) == {"new": 0}
    assert db.commits == 1 and slot["released"] == 0


@pytest.mark.asyncio
async def test_a_non_opencrq_integration_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    integration = type("I", (), {"provider": "shodan", "user_id": None})()

    class _Db:
        async def get(self, model, key):
            return integration

    profile = oc.parse_profile({"evidence_health": {"enabled": True, "integration": str(uuid.uuid4())}})
    assert await health.reader_for(_Db(), profile) == "shodan is not an OpenCRQ integration"


# ── the run-as user of the pass (#2739) ─────────────────────────────────


class _IntegrationDb:
    """Answers one OpenCRQ integration owned by *owner*."""

    def __init__(self, integration_id: uuid.UUID, owner: uuid.UUID) -> None:
        self.integration = type("I", (), {"id": integration_id, "provider": "opencrq", "user_id": owner})()

    async def get(self, model, key):
        return self.integration


def _run_as_profile(integration_id: uuid.UUID, run_as: uuid.UUID | None) -> Any:
    return oc.parse_profile(
        {
            "evidence_health": {
                "enabled": True,
                "integration": str(integration_id),
                **({"run_as_user_id": str(run_as)} if run_as else {}),
            }
        }
    )


def _capture_token(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from app.services import integration_tools

    seen: dict[str, Any] = {}

    async def _token(db, integration_id, provider="", *, user_id=None, integ=None, **_kw):
        seen["user_id"] = user_id
        return None  # no credential: reader_for stops right after the token read

    monkeypatch.setattr(integration_tools, "_get_token", _token)
    return seen


@pytest.mark.asyncio
async def test_unset_run_as_reads_as_the_integration_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    integration_id, owner = uuid.uuid4(), uuid.uuid4()
    seen = _capture_token(monkeypatch)

    async def _never(*_a, **_kw):
        raise AssertionError("no run-as check without a run-as")

    monkeypatch.setattr(health, "run_as_refusal", _never)
    reason = await health.reader_for(_IntegrationDb(integration_id, owner), _run_as_profile(integration_id, None))
    assert reason == "no OpenCRQ credential could be resolved for the evidence health integration"
    assert seen["user_id"] == str(owner)


@pytest.mark.asyncio
async def test_a_picked_run_as_reads_as_that_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import module_run_as

    integration_id, owner, run_as = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen = _capture_token(monkeypatch)
    checked: list[tuple[str, str]] = []

    async def _ok(db, raw, integration):
        checked.append((str(raw), str(integration)))
        return None

    monkeypatch.setattr(module_run_as, "integration_refusal", _ok)
    await health.reader_for(_IntegrationDb(integration_id, owner), _run_as_profile(integration_id, run_as))
    assert checked == [(str(run_as), str(integration_id))]
    assert seen["user_id"] == str(run_as)


@pytest.mark.asyncio
async def test_a_refused_run_as_is_the_reason_never_another_user(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import module_run_as

    integration_id, owner, run_as = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    seen = _capture_token(monkeypatch)
    refusal = "The run-as user has no connected OpenCRQ integration: a fleet automation runs as that user."

    async def _refused(db, raw, integration):
        return refusal

    monkeypatch.setattr(module_run_as, "integration_refusal", _refused)
    reason = await health.reader_for(_IntegrationDb(integration_id, owner), _run_as_profile(integration_id, run_as))
    assert reason == refusal
    assert "user_id" not in seen  # no token read: never the owner's credential instead


def test_the_run_as_is_an_optional_profile_field() -> None:
    assert oc.parse_profile({}).evidence_health.run_as_user_id is None
    run_as = str(uuid.uuid4())
    parsed = oc.parse_profile({"evidence_health": {"run_as_user_id": run_as}})
    assert parsed.evidence_health.run_as_user_id == run_as


def test_a_refused_run_as_reads_as_a_configuration_reason() -> None:
    from app.services.ctem_platform_sources import reason_code

    assert reason_code("The run-as user has no connected OpenCRQ integration: x")[0] == "run_as_no_integration"
    assert reason_code("the run-as user is deactivated or no longer exists")[0] == "run_as_inactive"
