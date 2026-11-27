# SPDX-License-Identifier: Apache-2.0

"""#2542: a failed OpenCTI software read (threat #2523, CVE #2171) leaves an incomplete scope gate
decision, never cached for the pass, retried by a scheduled pass with backoff and an attempt cap;
every decision says how each read went ("not needed", "not applicable"), never None."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import ctem_cve_software, ctem_threat_software
from app.services import ctem_scope_gate as gate
from app.services import ctem_scope_gate_store as store
from app.services import organization_context as oc

TODAY = date(2026, 11, 27)
NOW = datetime(2026, 11, 27, 3, 0, tzinfo=UTC)
THREAT_ID = "6e1f2b0a-0000-4000-8000-000000000042"
FAILED = ctem_cve_software.LOOKUP_FAILED


def _profile() -> oc.OrganizationProfile:
    return oc.parse_profile(
        {
            "software": [{"product": "Ivanti Connect Secure"}, {"product": "Metabase"}],
            "assets_exposure": [
                {"name": "vpn.example.com", "internet_facing": "yes", "products": ["Ivanti Connect Secure"]},
                {"name": "metabase.example.com", "internet_facing": "yes", "products": ["Metabase"]},
            ],
        }
    )


def _threat_facts(**kw: Any) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="APT-X",
        subject_label="APT-X",
        source_provider="opencti",
        source_external_id=THREAT_ID,
        evidence={"event": "create", "stix_type": "intrusion-set", "stix_id": "intrusion-set--1"},
        **kw,
    )


def _cve_facts(**kw: Any) -> gate.SignalFacts:
    return gate.SignalFacts(
        trigger_path="new_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="report--1",
        subject_label="Metabase pre-auth RCE CVE-2023-38646",
        source_provider="opencti",
        evidence={"stix_type": "report"},
        **kw,
    )


def _row(facts: gate.SignalFacts, *, decision: dict[str, Any] | None = None) -> Any:
    return SimpleNamespace(
        id=str(uuid.uuid4()),
        organization_id="org",
        state="new",
        trigger_path=facts.trigger_path,
        lens=facts.lens,
        subject_kind=facts.subject_kind,
        subject_ref=facts.subject_ref,
        source_provider=facts.source_provider,
        source_external_id=facts.source_external_id,
        subject_label=facts.subject_label,
        evidence=dict(facts.evidence),
        decision=decision,
        updated_at=None,
        recorded_by=str(uuid.uuid4()),
    )


class _Nested:
    async def __aenter__(self) -> _Nested:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _db() -> Any:
    return SimpleNamespace(info={}, begin_nested=lambda: _Nested())


@pytest.fixture
def fake_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """The profile above, no related signal, no OpenCRQ read, no fallback reader."""

    async def _no_related(_db: Any, _row: Any) -> list[Any]:
        return []

    async def _profile_of(_db: Any, _org: Any) -> oc.OrganizationProfile:
        return _profile()

    async def _links(_db: Any, _org: Any) -> None:
        return None

    async def _scenarios(_db: Any, *, user_id: Any) -> gate.ScenarioIndex:
        return gate.ScenarioIndex()

    async def _no_reader(_db: Any) -> None:
        return None

    monkeypatch.setattr(store, "related_signals", _no_related)
    monkeypatch.setattr(store, "_profile", _profile_of)
    monkeypatch.setattr(store, "_primary_assets", _links)
    monkeypatch.setattr(store, "read_scenarios", _scenarios)
    monkeypatch.setattr(store, "default_scenario_reader", _no_reader)


def _answers(*replies: tuple[tuple[str, ...], str]) -> tuple[list[Any], Any]:
    """A fake OpenCTI read answering *replies* in turn, and the list of its calls."""
    calls: list[Any] = []
    queue = list(replies)

    async def read(*args: Any, **kw: Any) -> tuple[tuple[str, ...], str]:
        calls.append(args)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return calls, read


async def _decide(row: Any, at: datetime) -> None:
    """One retry pass on *row*, as :func:`store.recheck_lookup_failed` runs it after its selection."""
    counts = await store._decide_again(_db(), [row], now=at, isolate=True)
    assert "failed" not in counts


# ── the pure gate: explicit lookups, incomplete decisions ─────────────────


@pytest.mark.parametrize(
    ("facts", "key"),
    [(_threat_facts(threat_lookup=FAILED), "threat_lookup"), (_cve_facts(cve_lookup=FAILED), "cve_lookup")],
)
def test_a_failed_read_leaves_an_incomplete_decision(facts: gate.SignalFacts, key: str) -> None:
    decision = gate.decide(_profile(), facts, today=TODAY)
    assert decision.outcome == "to_validate" and decision.record["reason_code"] == "subject_unresolved"
    assert decision.record["incomplete"] is True
    assert decision.record["reconcile"][key] == FAILED


def test_a_threat_resolved_on_evidence_reads_not_needed() -> None:
    base = _threat_facts()
    facts = replace(base, evidence={**base.evidence, "targets": ["Ivanti Connect Secure"]})
    decision = gate.decide(_profile(), facts, today=TODAY)
    assert decision.record["reason_code"] != "subject_unresolved"
    assert decision.record["reconcile"]["threat_lookup"] == gate.LOOKUP_NOT_NEEDED
    assert decision.record["reconcile"]["cve_lookup"] == gate.LOOKUP_NOT_APPLICABLE
    assert decision.record["incomplete"] is False


def test_a_cve_resolved_on_evidence_reads_not_needed() -> None:
    base = _cve_facts()
    facts = replace(base, evidence={**base.evidence, "software": ["Metabase"]})
    decision = gate.decide(_profile(), facts, today=TODAY)
    assert decision.record["reason_code"] != "subject_unresolved"
    assert decision.record["reconcile"]["cve_lookup"] == gate.LOOKUP_NOT_NEEDED
    assert decision.record["incomplete"] is False


def test_a_threat_of_another_source_reads_not_applicable() -> None:
    facts = gate.SignalFacts(
        trigger_path="relevant_threat",
        lens="world",
        subject_kind="threat",
        subject_ref="x",
        source_provider="recorded_future",
        source_external_id="x",
        evidence={"stix_type": "malware"},
    )
    decision = gate.decide(_profile(), facts, today=TODAY)
    reconcile = decision.record["reconcile"]
    assert reconcile["threat_lookup"] == gate.LOOKUP_NOT_APPLICABLE
    assert reconcile["cve_lookup"] == gate.LOOKUP_NOT_APPLICABLE
    # Unmatched, but nothing to read: a final decision, never retried.
    assert decision.record["reason_code"] == "subject_unresolved" and decision.record["incomplete"] is False


def test_every_decision_carries_both_lookups_never_none() -> None:
    for facts in (_threat_facts(), _cve_facts(), _threat_facts(threat_lookup="read")):
        reconcile = gate.decide(_profile(), facts, today=TODAY).record["reconcile"]
        assert isinstance(reconcile["threat_lookup"], str) and reconcile["threat_lookup"]
        assert isinstance(reconcile["cve_lookup"], str) and reconcile["cve_lookup"]


# ── the session cache keeps only reads that answered ──────────────────────


@pytest.mark.asyncio
async def test_a_failed_threat_read_is_not_reused_in_the_same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, read = _answers(((), FAILED), (("Ivanti Connect Secure",), "read"))
    monkeypatch.setattr(ctem_threat_software, "threat_software", read)
    monkeypatch.setattr(store, "default_scenario_reader", lambda db: _none())
    db = _db()
    row = _row(_threat_facts())
    first = await store._with_threat_software(db, row, _threat_facts(), _profile(), None)
    assert first.threat_lookup == FAILED
    second = await store._with_threat_software(db, row, _threat_facts(), _profile(), None)
    assert second.threat_lookup == "read" and second.threat_software == ("Ivanti Connect Secure",)
    assert len(calls) == 2
    # The read that answered is cached for the pass.
    await store._with_threat_software(db, row, _threat_facts(), _profile(), None)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_failed_cve_read_is_not_reused_in_the_same_session(monkeypatch: pytest.MonkeyPatch) -> None:
    calls, read = _answers(((), FAILED), (("Metabase",), "read"))
    monkeypatch.setattr(ctem_cve_software, "cve_software", read)
    monkeypatch.setattr(store, "default_scenario_reader", lambda db: _none())
    db = _db()
    row = _row(_cve_facts())
    first = await store._with_cve_software(db, row, _cve_facts(), _profile(), None)
    assert first.cve_lookup == FAILED
    second = await store._with_cve_software(db, row, _cve_facts(), _profile(), None)
    assert second.cve_lookup == "read" and second.cve_software == ("Metabase",)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_a_bulk_pass_decides_from_the_failure_it_met_not_from_not_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefetch failed: a ``remote=False`` decision of that pass names why, never "not read"."""
    _calls, read = _answers(((), FAILED))
    monkeypatch.setattr(ctem_threat_software, "threat_software", read)
    monkeypatch.setattr(store, "default_scenario_reader", lambda db: _none())
    db = _db()
    row = _row(_threat_facts())
    await store._with_threat_software(db, row, _threat_facts(), _profile(), None, release=True)
    facts = await store._with_threat_software(db, row, _threat_facts(), _profile(), None, remote=False)
    assert facts.threat_lookup == FAILED


@pytest.mark.asyncio
async def test_a_bulk_prefetch_meets_a_failure_once_per_key_and_reads_it_again_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2548 review: N rows sharing a threat id cost one OpenCTI timeout per prefetch, not N."""
    calls, read = _answers(((), FAILED))
    monkeypatch.setattr(ctem_threat_software, "threat_software", read)
    monkeypatch.setattr(store, "default_scenario_reader", lambda db: _none())
    monkeypatch.setattr(store, "facts_of", lambda row: _threat_facts())
    db = _db()
    recorder = str(uuid.uuid4())
    rows = [_row(_threat_facts()) for _ in range(5)]
    for row in rows:
        row.recorded_by = recorder  # one reader: one OpenCTI key for the five rows
    profiles: dict[Any, Any] = {"org": _profile()}
    links: dict[Any, Any] = {"org": None}
    await store.prepare_cve_reads(db, rows, profiles=profiles, links=links)
    assert len(calls) == 1
    facts = await store._with_threat_software(db, rows[-1], _threat_facts(), _profile(), None, remote=False)
    assert facts.threat_lookup == FAILED
    # The next pass reads the failed key again.
    await store.prepare_cve_reads(db, rows, profiles=profiles, links=links)
    assert len(calls) == 2


async def _none() -> None:
    return None


# ── the retry pass: fail then read, attempts counted, cap ─────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["threat", "cve"])
async def test_a_failed_read_is_resolved_by_the_retry_with_attempts_counted(
    fake_io: None, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    software = "Ivanti Connect Secure" if kind == "threat" else "Metabase"
    calls, read = _answers(((), FAILED), ((software,), "read"))
    if kind == "threat":
        monkeypatch.setattr(ctem_threat_software, "threat_software", read)
        row = _row(_threat_facts())
        key = "threat_lookup"
    else:
        monkeypatch.setattr(ctem_cve_software, "cve_software", read)
        row = _row(_cve_facts())
        key = "cve_lookup"

    await store.evaluate(_db(), row, now=NOW)
    assert row.state == "to_validate"
    assert row.decision["incomplete"] is True and row.decision["reconcile"][key] == FAILED
    assert row.decision["lookup_attempts"] == 1 and row.decision["lookup_retry"] == "scheduled"
    assert store._lookup_failed(row.decision)

    await _decide(row, NOW + store.lookup_backoff(1))
    # Resolved to the inventory software: no longer Unmatched (a case candidate).
    assert row.decision["reason_code"] != "subject_unresolved", row.decision["reason"]
    assert row.state != "to_validate"
    assert row.decision["incomplete"] is False and row.decision["reconcile"][key] == "read"
    assert software in row.decision["reconcile"]["products"]
    assert row.decision["lookup_attempts"] == 2 and "lookup_retry" not in row.decision
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_retries_stop_at_the_cap_and_the_failure_stays_visible(
    fake_io: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _calls, read = _answers(((), FAILED))
    monkeypatch.setattr(ctem_threat_software, "threat_software", read)
    row = _row(_threat_facts())
    at = NOW
    await store.evaluate(_db(), row, now=at)
    for _ in range(store.LOOKUP_ATTEMPTS_MAX - 1):
        at += store.lookup_backoff(row.decision["lookup_attempts"])
        await _decide(row, at)
    assert row.decision["lookup_attempts"] == store.LOOKUP_ATTEMPTS_MAX
    assert row.decision["lookup_retry"] == "exhausted"
    assert row.decision["incomplete"] is True and row.decision["reconcile"]["threat_lookup"] == FAILED


def test_a_decision_of_the_2523_window_counts_as_failed_once() -> None:
    """A decision written before #2542 (no ``incomplete``) with a failed lookup is picked up by the retry."""
    old = {"reason_code": "subject_unresolved", "reconcile": {"threat_lookup": FAILED, "cve_lookup": None}}
    assert store._lookup_failed(old)
    assert not store._lookup_failed({"reason_code": "subject_unresolved", "reconcile": {"threat_lookup": None}})
    assert not store._lookup_failed({**old, "incomplete": False})
    record = {"incomplete": True}
    store._count_lookup_attempts(record, old)
    assert record["lookup_attempts"] == 2 and record["lookup_retry"] == "scheduled"


def test_the_backoff_doubles() -> None:
    assert [store.lookup_backoff(n) for n in (0, 1, 2, 3)] == [
        timedelta(minutes=5),
        timedelta(minutes=5),
        timedelta(minutes=10),
        timedelta(minutes=20),
    ]


@pytest.mark.asyncio
async def test_the_retry_pass_selects_failed_unmatched_decisions_due_by_backoff(fake_io: None) -> None:
    statements: list[str] = []

    class _Result:
        def scalars(self) -> Any:
            return iter([])

    class _Db:
        info: dict[str, Any] = {}

        async def execute(self, statement: Any) -> _Result:
            from sqlalchemy.dialects import postgresql

            compiled = statement.compile(dialect=postgresql.dialect())
            statements.append(f"{compiled} {sorted(map(str, compiled.params.values()))}")
            return _Result()

    assert await store.recheck_lookup_failed(_Db(), now=NOW, batch=50) == {}
    sql = statements[0]
    assert "ctem_signals.state" in sql and "reason_code" in sql and "subject_unresolved" in sql
    for key in ("incomplete", "lookup_attempts", "threat_lookup", "cve_lookup", FAILED):
        assert key in sql, key
    assert "LIMIT" in sql
