"""The signal a source issue records (#1529): path, subject, evidence and an idempotent key."""

from __future__ import annotations

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
