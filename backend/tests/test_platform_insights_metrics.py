"""``query_platform_metrics``: the backend's xtmone_* Prometheus series, read by an agent.

The reader runs on a private ``CollectorRegistry`` so the process registry's
counts never leak into the assertions; the gate and the tool's wiring run
through the suite with a scope double.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


@pytest.fixture
def registry() -> CollectorRegistry:
    reg = CollectorRegistry()
    resumes = Counter(
        "xtmone_run_resumes_total", "Resumes", ["entry", "cause", "graceful", "version"], registry=reg
    )
    resumes.labels(entry="assignment_run", cause="rollout", graceful="true", version="f474aac6").inc(3)
    resumes.labels(entry="background_task", cause="lease_lost", graceful="false", version="f474aac6").inc()
    orphaned = Counter("xtmone_tasks_orphaned_total", "Orphaned", ["version"], registry=reg)
    orphaned.labels(version="f474aac6").inc(2)
    orphaned.labels(version="be1aec4d").inc()
    create = Histogram(
        "xtmone_sandbox_create_seconds",
        "Create seconds",
        ["image_key", "outcome", "pooled"],
        buckets=(1, 2, 5, 10),
        registry=reg,
    )
    for seconds in (0.5, 1.5, 1.5, 3, 4, 8, 20):
        create.labels(image_key="python", outcome="ok", pooled="true").observe(seconds)
    Gauge("xtmone_assignment_queue_depth", "Depth", ["platform_id"], registry=reg).labels(platform_id="p").set(4)
    Counter("python_like_total", "Not served", registry=reg).inc()
    return reg


def test_counters_answer_one_row_per_label_set(registry):
    from app.services.platform_insights.metrics import read_metrics

    resumes = read_metrics("xtmone_run_resumes_total", registry=registry)
    assert resumes["type"] == "counter"
    # Rows are ordered by their sorted label items: cause first.
    assert [row["labels"]["cause"] for row in resumes["rows"]] == ["lease_lost", "rollout"]
    for row in resumes["rows"]:
        assert set(row["labels"]) == {"entry", "cause", "graceful", "version"}
        assert row["value"] >= 0
    assert {row["labels"]["entry"]: row["value"] for row in resumes["rows"]} == {
        "assignment_run": 3.0,
        "background_task": 1.0,
    }

    # The family name without _total answers the same series.
    orphaned = read_metrics("xtmone_tasks_orphaned", registry=registry)
    assert orphaned["series"] == "xtmone_tasks_orphaned_total"
    assert {row["labels"]["version"]: row["value"] for row in orphaned["rows"]} == {"be1aec4d": 1.0, "f474aac6": 2.0}


def test_labels_keep_only_the_matching_rows(registry):
    from app.services.platform_insights.metrics import read_metrics

    rows = read_metrics("xtmone_tasks_orphaned_total", {"version": "be1aec4d"}, registry=registry)["rows"]
    assert rows == [{"series": "xtmone_tasks_orphaned_total", "labels": {"version": "be1aec4d"}, "value": 1.0}]


def test_a_histogram_answers_count_sum_and_quantile_estimates(registry):
    from app.services.platform_insights.metrics import read_metrics

    result = read_metrics("xtmone_sandbox_create_seconds", registry=registry)
    (row,) = result["rows"]
    assert row["labels"] == {"image_key": "python", "outcome": "ok", "pooled": "true"}
    assert row["count"] == 7
    assert row["sum"] == pytest.approx(38.5)
    assert row["mean"] == pytest.approx(5.5)
    # Buckets (cumulative): le1=1, le2=3, le5=5, le10=6, +Inf=7.
    # p50: rank 3.5 falls in (2, 5]: 2 + 3 * 0.5 / 2 = 2.75.
    assert row["p50"] == pytest.approx(2.75)
    # p95: rank 6.65 falls in +Inf: the highest finite bound.
    assert row["p95"] == pytest.approx(10.0)


def test_quantile_estimate_edges():
    from app.services.platform_insights.metrics import estimate_quantile

    assert estimate_quantile(0.5, []) is None
    assert estimate_quantile(0.5, [(1.0, 0.0), (float("inf"), 0.0)]) is None
    # Every observation in the first bucket: interpolated from zero.
    assert estimate_quantile(0.5, [(4.0, 10.0), (float("inf"), 10.0)]) == pytest.approx(2.0)


def test_an_unknown_or_foreign_series_is_refused_by_name(registry):
    from app.services.platform_insights.metrics import read_metrics
    from app.services.platform_insights.scope import InsightRefusedError

    with pytest.raises(InsightRefusedError) as foreign:
        read_metrics("python_like_total", registry=registry)
    assert "'python_like_total' is not served" in foreign.value.message
    with pytest.raises(InsightRefusedError) as unknown:
        read_metrics("xtmone_nope_total", registry=registry)
    assert "unknown series 'xtmone_nope_total'" in unknown.value.message
    assert "xtmone_run_resumes_total" in unknown.value.message


def test_without_a_series_the_served_ones_are_listed(registry):
    from app.services.platform_insights.metrics import read_metrics

    listed = {item["series"]: item for item in read_metrics(None, registry=registry)["series"]}
    assert set(listed) == {
        "xtmone_assignment_queue_depth",
        "xtmone_run_resumes_total",
        "xtmone_sandbox_create_seconds",
        "xtmone_tasks_orphaned_total",
    }
    assert listed["xtmone_run_resumes_total"]["label_sets"] == 2


def test_every_result_names_the_process_local_view(registry):
    from app.services.platform_insights.metrics import read_metrics

    result = read_metrics("xtmone_assignment_queue_depth", registry=registry)
    assert result["view"]["kind"] == "process_local"
    assert {"host", "pid", "version"} <= set(result["view"])
    assert result["caveats"][0].startswith("Process-local view")
    assert result["rows"][0]["value"] == 4.0


def _scope(platform: bool):
    from app.services.platform_insights.scope import PLATFORM_SCOPE, SELF_SCOPE

    label = PLATFORM_SCOPE if platform else SELF_SCOPE
    return SimpleNamespace(platform=platform, header=lambda: {"scope": label, "scope_note": "note"})


def test_a_non_administrator_gets_their_scope_and_no_series():
    from app.services.platform_insights.metrics import query_platform_metrics
    from app.services.platform_insights.scope import SELF_SCOPE

    result = asyncio.run(query_platform_metrics(None, _scope(False), {"series": "xtmone_run_resumes_total"}))
    assert result["scope"] == SELF_SCOPE
    assert result["rows"] == []
    assert "view" not in result


def test_an_administrator_reads_the_process_registry():
    from app.services.platform_insights.metrics import query_platform_metrics
    from app.services.platform_insights.scope import PLATFORM_SCOPE
    from app.services.rollout_counters import RUN_RESUMES

    RUN_RESUMES.labels(entry="assignment_run", cause="test_7571", graceful="true", version="t").inc()
    result = asyncio.run(
        query_platform_metrics(
            None, _scope(True), {"series": "xtmone_run_resumes_total", "labels": {"cause": "test_7571"}}
        )
    )
    assert result["scope"] == PLATFORM_SCOPE
    assert [row["labels"]["cause"] for row in result["rows"]] == ["test_7571"]
    assert result["rows"][0]["value"] >= 1
    json.dumps(result)  # The suite serialises the result as is.


def test_labels_must_be_an_object():
    from app.services.platform_insights.metrics import query_platform_metrics
    from app.services.platform_insights.scope import InsightRefusedError

    with pytest.raises(InsightRefusedError):
        asyncio.run(query_platform_metrics(None, _scope(True), {"series": "xtmone_run_resumes_total", "labels": "x"}))


def test_the_tool_is_wired_and_says_process_local():
    from app.services.builtin_tools import build_platform_insights_tools

    defs, _executor = build_platform_insights_tools("")
    (definition,) = [d for d in defs if d.name == "query_platform_metrics"]
    assert "PROCESS-LOCAL" in definition.description
    assert "Administrators only" in definition.description
