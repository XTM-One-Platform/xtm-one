"""The signal a source issue records (#1529): path, subject, evidence and an idempotent key."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import ctem_case_loop as loop
from app.services import ctem_cases as rules
from app.services.ctem_source_signals import signal_of


def _issue(source: str = "opencti", ext: str = "v1") -> rules.SourceIssue:
    return rules.SourceIssue(
        source=source,
        external_id=ext,
        name="CVE-2026-1",
        cve="CVE-2026-1",
        product="Tomcat",
        vendor="Apache",
        fixed_version="9.0.98",
        cvss=9.8,
        assets=("web-1", "web-2"),
    )


def test_an_opencti_vulnerability_is_a_new_vulnerability_signal_keyed_on_its_fix() -> None:
    issue = _issue()
    fix = rules.remediation_of(issue)
    assert fix is not None
    signal = signal_of(issue, fix)
    assert signal is not None
    assert (signal.trigger_path, signal.source_provider, signal.source_external_id) == (
        "new_vulnerability",
        "opencti",
        "v1",
    )
    assert (signal.subject_kind, signal.subject_ref) == ("vulnerability", "CVE-2026-1")
    assert signal.evidence["source_issue"] == "opencti:v1"
    assert signal.evidence["assets"] == ["web-1", "web-2"]
    assert signal.evidence["software"] == ["Apache Tomcat"]
    assert signal.evidence["case_fix"]["key"] == "upgrade:apache/tomcat@9.0.98"
    # The case the signal opens groups on the same fix key the recompute groups on.
    case_fix = loop.source_fix_of(signal.evidence)
    assert case_fix is not None and case_fix.key == fix.key and case_fix.kind == "upgrade"


class _Db:
    def __init__(self) -> None:
        self.info: dict[str, Any] = {}

    def begin_nested(self) -> Any:
        class _Tx:
            async def __aenter__(self) -> None:
                return None

            async def __aexit__(self, *exc: object) -> None:
                return None

        return _Tx()


def _run_reader(monkeypatch: pytest.MonkeyPatch, excluded: dict[str, set[tuple[str, ...]]]) -> list[Any]:
    """One source issue through the cases recompute's reader; the signals it handed to record_signal."""
    from app.services import ctem_cases_store as store
    from app.services import ctem_scope_gate_store as gate_store
    from app.services import ctem_signal_sources, ctem_signals

    recorded: list[Any] = []

    async def _excluded(db: Any, organization_id: Any) -> dict[str, set[tuple[str, ...]]]:
        return excluded

    async def _record(db: Any, *, signal: Any, **_: Any) -> tuple[Any, bool]:
        recorded.append(signal)
        return object(), False

    async def _evaluate(*_: Any, **__: Any) -> None:
        return None

    monkeypatch.setattr(ctem_signal_sources, "excluded_sources", _excluded)
    monkeypatch.setattr(ctem_signals, "record_signal", _record)
    monkeypatch.setattr(gate_store, "evaluate", _evaluate)
    issue = _issue()
    fix = rules.remediation_of(issue)
    assert fix is not None
    asyncio.run(store._source_signal(_Db(), uuid.uuid4(), issue, fix, datetime.now(UTC)))
    return recorded


def test_the_cases_reader_records_nothing_on_a_path_off_for_its_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2583 note of 27 Nov 2026: OpenCTI "Not used here" on path 1 stops the recompute's signals too."""
    off = {"new_vulnerability": {("opencti", "listener"), ("opencti", "pir_playbook")}}
    assert _run_reader(monkeypatch, off) == []
    assert _run_reader(monkeypatch, {"new_vulnerability": {("opencti", "cases_reader")}}) == []


def test_the_cases_reader_records_as_its_own_source_when_on(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import ctem_signals

    recorded = _run_reader(monkeypatch, {})
    assert len(recorded) == 1
    assert recorded[0].evidence[ctem_signals.SOURCE_KEY_EVIDENCE] == "opencti:cases_reader"
    # Only the listener off: the PIR playbook still feeds the path, so the reader still records.
    assert len(_run_reader(monkeypatch, {"new_vulnerability": {("opencti", "listener")}})) == 1


def test_an_openaev_finding_is_proven_exploitable() -> None:
    issue = _issue("openaev", "f1")
    fix = rules.remediation_of(issue)
    assert fix is not None
    signal = signal_of(issue, fix)
    assert signal is not None and signal.trigger_path == "proven_exploitable"


def test_the_key_is_stable_per_issue_and_fix() -> None:
    issue = _issue()
    fix = rules.remediation_of(issue)
    assert fix is not None
    one, two = signal_of(issue, fix), signal_of(issue, fix)
    assert one is not None and two is not None and one.dedupe_key == two.dedupe_key
    other = signal_of(_issue(ext="v2"), fix)
    assert other is not None and other.dedupe_key != one.dedupe_key


def test_an_unknown_source_records_nothing() -> None:
    issue = _issue("signal")
    fix = rules.remediation_of(issue)
    assert fix is not None and signal_of(issue, fix) is None


def test_a_malformed_case_fix_falls_back_to_the_path_rule() -> None:
    assert loop.source_fix_of({"case_fix": {"key": "k", "kind": "nope", "title": "t"}}) is None
    assert loop.source_fix_of({}) is None
