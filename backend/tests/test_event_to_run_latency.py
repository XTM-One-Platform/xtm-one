"""Event-to-run latency of an event-triggered run."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from prometheus_client import REGISTRY

from app.services.assignment_engine import _run_event_latency
from app.services.sub_agent import _with_watch_event_latency
from app.services.tracing.event_latency import event_latency, observe, task_event_latency

T0 = datetime(2026, 11, 2, 1, 0, 0, tzinfo=UTC)


def _iso(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def test_a_platform_event_run_splits_its_latency():
    figures = event_latency(
        {"type": "platform_event", "occurred_at": _iso(0), "triggered_at": _iso(0.5)},
        created_at=T0 + timedelta(seconds=0.7),
        started_at=T0 + timedelta(seconds=3.2),
    )
    assert figures == {
        "kind": "platform_event",
        "event_to_trigger_ms": 500.0,
        "trigger_to_queue_ms": 200.0,
        "queue_ms": 2500.0,
        "event_to_run_ms": 3200.0,
        "event_origin": "occurred_at",
    }


def test_get_run_names_the_event_age_of_an_event_started_run():
    from app.services.builtin_tools.agent_builder import _event_latency_line

    run = SimpleNamespace(
        trigger_context={"type": "platform_event", "occurred_at": _iso(0), "triggered_at": _iso(0.1)},
        created_at=T0 + timedelta(seconds=0.2),
        started_at=T0 + timedelta(seconds=5940),
    )
    assert _event_latency_line(run) == "Event to start: 5940.0s (platform_event, 5939.8s queued)"
    manual = SimpleNamespace(trigger_context={"type": "manual"}, created_at=T0, started_at=T0)
    assert _event_latency_line(manual) is None
    not_started = SimpleNamespace(trigger_context=run.trigger_context, created_at=T0, started_at=None)
    assert _event_latency_line(not_started) is None


def test_an_integration_event_run_counts_from_its_hand_over():
    figures = event_latency(
        {"type": "integration_event", "provider": "microsoft", "triggered_at": _iso(0).replace("+00:00", "Z")},
        created_at=T0 + timedelta(seconds=1),
        started_at=T0 + timedelta(seconds=4),
    )
    assert figures["event_to_run_ms"] == 4000.0
    assert figures["event_to_trigger_ms"] is None
    assert figures["provider"] == "microsoft"
    assert figures["event_origin"] == "receipt"


def _github_webhook_context() -> dict:
    """The context the provider webhook receiver writes (webhooks.py): ``type`` is ``webhook``."""
    return {
        "type": "webhook",
        "scope": "provider",
        "provider": "github",
        "event_key": "workflow_run.completed",
        "triggered_at": _iso(0),
        "body": {"action": "completed"},
    }


def test_a_webhook_delivered_integration_event_run_is_measured():
    """The webhook path writes ``type: webhook``; the assignment's trigger names the kind."""
    figures = event_latency(
        _github_webhook_context(),
        created_at=T0 + timedelta(seconds=0.2),
        started_at=T0 + timedelta(seconds=1.5),
        trigger_type="integration_event",
    )
    assert figures == {
        "kind": "integration_event",
        "provider": "github",
        "event_to_trigger_ms": None,
        "trigger_to_queue_ms": 200.0,
        "queue_ms": 1300.0,
        "event_to_run_ms": 1500.0,
        "event_origin": "receipt",
    }


def test_a_legacy_webhook_assignment_run_is_not_measured():
    context = _github_webhook_context()
    assert event_latency(context, created_at=T0, started_at=T0) is None
    assert event_latency(context, created_at=T0, started_at=T0, trigger_type="webhook") is None


def test_a_run_without_an_event_origin_or_with_bad_timestamps():
    assert event_latency({"type": "schedule"}, created_at=T0, started_at=T0) is None
    assert event_latency(None, created_at=T0, started_at=T0) is None
    figures = event_latency({"type": "platform_event", "occurred_at": "not a date"}, created_at=None, started_at=T0)
    assert figures["event_to_run_ms"] is None and figures["queue_ms"] is None
    observe(figures)  # nothing to observe, no error


def test_a_naive_timestamp_is_read_as_utc():
    figures = event_latency(
        {"type": "platform_event", "occurred_at": "2026-11-02T01:00:00"}, created_at=None, started_at=T0 + timedelta(seconds=1)
    )
    assert figures["event_to_run_ms"] == 1000.0


def _count(kind: str) -> float:
    return REGISTRY.get_sample_value("xtmone_event_to_run_seconds_count", {"kind": kind}) or 0.0


def test_the_engine_hook_measures_a_first_start_only():
    context = {"type": "integration_event", "triggered_at": _iso(0)}
    run = SimpleNamespace(trigger_context=context, created_at=T0, started_at=T0 + timedelta(seconds=2))
    before = _count("integration_event")
    assert _run_event_latency(run, "pending")["event_to_run_ms"] == 2000.0
    assert _count("integration_event") == before + 1
    # A HITL resume and a resumed attempt start long after the event: not measured.
    assert _run_event_latency(run, "awaiting_human") is None
    resumed = SimpleNamespace(
        trigger_context={**context, "resumes": [{"cause": "rollout"}]}, created_at=T0, started_at=T0
    )
    assert _run_event_latency(resumed, "pending") is None
    assert _count("integration_event") == before + 1


def test_the_engine_hook_measures_a_webhook_delivered_integration_event_run():
    run = SimpleNamespace(
        trigger_context=_github_webhook_context(), created_at=T0, started_at=T0 + timedelta(seconds=3)
    )
    before = _count("integration_event")
    figures = _run_event_latency(run, "pending", trigger_type="integration_event")
    assert figures["kind"] == "integration_event" and figures["provider"] == "github"
    assert figures["event_to_run_ms"] == 3000.0
    assert _count("integration_event") == before + 1
    assert _run_event_latency(run, "pending", trigger_type="webhook") is None


def test_the_engine_hook_never_raises():
    assert _run_event_latency(SimpleNamespace(), "pending") is None


def _watch_metadata() -> dict:
    return {
        "origin": "platform_event_watch",
        "event": "work_item.completed",
        "occurred_at": _iso(0),
        "triggered_at": _iso(0.4),
    }


def test_a_watch_started_task_splits_its_latency():
    figures = task_event_latency(_watch_metadata(), started_at=T0 + timedelta(seconds=2.4))
    assert figures == {
        "kind": "platform_event_watch",
        "event_to_trigger_ms": 400.0,
        "queue_ms": 2000.0,
        "event_to_run_ms": 2400.0,
        "event_origin": "occurred_at",
    }


def test_any_other_task_is_not_a_watch_task():
    assert task_event_latency({"origin": "spawn"}, started_at=T0) is None
    assert task_event_latency(None, started_at=T0) is None


def test_the_task_start_hook_measures_a_watch_task_once():
    before = _count("platform_event_watch")
    metadata = _with_watch_event_latency(_watch_metadata(), T0 + timedelta(seconds=1))
    assert metadata["event_latency"]["event_to_run_ms"] == 1000.0
    assert _count("platform_event_watch") == before + 1
    # Started again (a held task released): measured once, not twice.
    assert _with_watch_event_latency(metadata, T0 + timedelta(seconds=90)) is metadata
    assert _count("platform_event_watch") == before + 1
    plain = {"origin": "spawn"}
    assert _with_watch_event_latency(plain, T0) is plain
    assert _with_watch_event_latency(None, T0) is None


def test_the_measure_costs_microseconds():
    """The program's gate: once per run start, against seconds of run."""
    context = {"type": "platform_event", "occurred_at": _iso(0), "triggered_at": _iso(0.5)}
    started = time.perf_counter()
    for _ in range(1000):
        observe(event_latency(context, created_at=T0, started_at=T0 + timedelta(seconds=3)))
    per_run_ms = (time.perf_counter() - started) * 1000.0 / 1000
    assert per_run_ms < 0.5, per_run_ms
