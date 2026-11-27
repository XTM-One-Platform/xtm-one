"""#2583 point 3: a PIR score filter on a Vulnerability or a Report can never match."""

from __future__ import annotations

from app.services import opencti_trigger_provisioning as prov


def test_vulnerability_and_report_are_refused_with_the_reason() -> None:
    reason = prov.pir_source_refusal(["Report", "Intrusion-Set", "Vulnerability"])
    assert reason is not None
    assert "['Report', 'Vulnerability']" in reason
    assert "source of a relationship" in reason


def test_threat_types_can_be_pir_sources() -> None:
    assert prov.pir_source_refusal(["Intrusion-Set", "Campaign", "Malware"]) is None
    assert prov.pir_source_refusal([]) is None
    assert prov.pir_source_refusal(None) is None
