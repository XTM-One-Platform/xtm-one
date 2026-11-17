"""The Scoping stage page's mixed views (#11998): the observables in scope by type and
the vulnerabilities by CVSS severity, carried in the viz payload only when OpenCTI
answered them, beside the OpenAEV attack domains ring.

No DB / network.
"""

from __future__ import annotations

from app.api.v1 import ctem_metrics
from app.services import ctem_metrics_collector as collector


def test_named_rows_sorts_largest_first_drops_zeros_and_caps() -> None:
    rows = [("IPv4-Addr", 5), ("Domain-Name", 9), ("", 3), ("Url", 0), ("StixFile", 2)]
    assert collector._named_rows(rows) == [
        {"name": "Domain-Name", "value": 9},
        {"name": "IPv4-Addr", "value": 5},
        {"name": "StixFile", "value": 2},
    ]
    assert collector._named_rows(rows, limit=1) == [{"name": "Domain-Name", "value": 9}]


def test_scoping_viz_carries_only_answered_lists() -> None:
    facts = {
        "observables_by_type": [{"name": "IPv4-Addr", "value": 12}],
        "vulnerabilities_by_severity": [],
        "observables": 12,
    }
    assert ctem_metrics._stage_viz("scoping", facts, "cyber") == {
        "observables_by_type": [{"name": "IPv4-Addr", "value": 12}],
    }


def test_scoping_viz_keeps_the_ring_beside_the_opencti_lists() -> None:
    domains = [{"name": "Endpoint", "covered": True, "contracts": 3}]
    facts = {
        "attack_domains": domains,
        "vulnerabilities_by_severity": [{"label": "HIGH", "value": 4}],
    }
    assert ctem_metrics._stage_viz("scoping", facts, "cyber") == {
        "attack_domains": domains,
        "vulnerabilities_by_severity": [{"label": "HIGH", "value": 4}],
    }


def test_scoping_viz_of_another_vertical_has_no_opencti_lists() -> None:
    facts = {"observables_by_type": [{"name": "IPv4-Addr", "value": 12}]}
    assert ctem_metrics._stage_viz("scoping", facts, "fimi") == {}
