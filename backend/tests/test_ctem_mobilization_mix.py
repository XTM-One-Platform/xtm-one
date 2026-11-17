"""The Mobilization stage page's mixed views (#11998): OpenCTI's response mix and
response progress, OpenAEV's fix backlog, each read from counts the collectors already
answer, and the viz payload that carries each one only when its product answered it.

No DB / network.
"""

from __future__ import annotations

from app.api.v1 import ctem_metrics
from app.services import ctem_metrics_collector as collector


def test_named_counts_keeps_the_answered_counts_above_zero_in_order():
    rows = collector._named_counts(("a", 3), ("b", 0), ("c", None), ("d", True), ("e", 7))
    assert rows == [{"name": "a", "value": 3}, {"name": "e", "value": 7}]
    assert collector._named_counts(("a", 0)) == []


def test_response_progress_keeps_a_pair_only_on_a_non_empty_total():
    assert collector._response_progress(61, 83, 3, 5) == {
        "cases_closed": 61,
        "cases_total": 83,
        "playbooks_running": 3,
        "playbooks_total": 5,
    }
    # An empty case queue has no closure: only the playbooks are drawn.
    assert collector._response_progress(0, 0, 3, 5) == {"playbooks_running": 3, "playbooks_total": 5}
    # A count above its total is capped; nothing answered is None, never an empty dict.
    assert collector._response_progress(9, 4, None, None) == {"cases_closed": 4, "cases_total": 4}
    assert collector._response_progress(None, None, None, 0) is None


def test_mobilization_viz_carries_each_list_only_when_answered():
    facts = {
        "response_mix": [{"name": "case_incidents", "value": 41}],
        "response_progress": {"cases_closed": 61, "cases_total": 83},
        "fix_backlog": [{"name": "open_findings", "value": 89}],
        "case_closure_pct": 73.0,
    }
    viz = ctem_metrics._stage_viz("remediation", facts, "cyber")
    assert viz == {k: facts[k] for k in ("response_mix", "response_progress", "fix_backlog")}
    # An unanswered list is absent, never an empty frame.
    assert ctem_metrics._stage_viz("remediation", {"response_mix": [], "response_progress": None}, "cyber") == {}
    # Another vertical draws no mixed view on Mobilization.
    assert ctem_metrics._stage_viz("remediation", facts, "fraud") == {}
