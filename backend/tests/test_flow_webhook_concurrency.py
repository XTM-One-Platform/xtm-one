"""A Webhook trigger's ``max_concurrent_runs`` - parsing, catalog, validator.

The admission itself (lock, count, 429) is walked through the real route in
``test_flow_webhook_concurrency_postgres.py``.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.flow_catalog import (
    FIELD_NUMBER,
    MAX_WEBHOOK_CONCURRENT_RUNS,
    NODE_KINDS_BY_ID,
)
from app.services.flow_graph import normalize_graph, validate_graph
from app.services.flow_triggers import webhook_concurrency_limit


def _node(**config):
    return {"id": "w1", "kind": "trigger.webhook", "config": {"path": "alerts", **config}}


@pytest.mark.parametrize(
    ("value", "limit"),
    [
        (None, 1),
        (1, 1),
        (3, 3),
        ("4", 4),
        (" 5 ", 5),
        (6.0, 6),
        (MAX_WEBHOOK_CONCURRENT_RUNS, MAX_WEBHOOK_CONCURRENT_RUNS),
        # Out of range: clamped.
        (0, 1),
        (-3, 1),
        (999, MAX_WEBHOOK_CONCURRENT_RUNS),
        # Unreadable: one delivery at a time, never more than asked.
        (2.5, 1),
        (True, 1),
        ("many", 1),
        ("", 1),
        ([3], 1),
        ({"n": 3}, 1),
        (float("inf"), 1),
        (float("nan"), 1),
        # Digits `str.isdigit()` accepts but `int()` refuses.
        ("\u00b2", 1),  # superscript two
        ("\u2460", 1),  # circled digit one
        ("\u0663", 1),  # Arabic-Indic three: `int()` reads it, the field takes ASCII only
        # Past CPython's int-from-string digit limit.
        pytest.param("9" * 5000, 1, id="5000-digits"),
    ],
)
def test_the_limit_is_read_clamped_and_falls_back_to_one(value, limit):
    node = _node() if value is None else _node(max_concurrent_runs=value)
    assert webhook_concurrency_limit(node) == limit


def test_a_node_without_config_is_one_delivery_at_a_time():
    assert webhook_concurrency_limit({"id": "w1", "kind": "trigger.webhook"}) == 1


def test_the_catalog_offers_the_field_with_its_bounds():
    field = next(
        f for f in NODE_KINDS_BY_ID["trigger.webhook"].config_fields
        if f.name == "max_concurrent_runs"
    )
    assert (field.type, field.default, field.min, field.max, field.step) == (
        FIELD_NUMBER,
        1,
        1,
        MAX_WEBHOOK_CONCURRENT_RUNS,
        1,
    )
    assert field.required is False


def _issues(**config):
    graph = normalize_graph(
        {
            "nodes": [
                _node(**config),
                {"id": "s1", "kind": "util.stop", "config": {"result": "ok"}},
            ],
            "edges": [
                {"id": "e1", "source": "w1", "source_handle": "out", "target": "s1",
                 "target_handle": "in", "kind": "data"},
            ],
        }
    )
    return [(i.code, i.field) for i in validate_graph(graph).issues]


@pytest.mark.parametrize("value", [None, "", 1, 7, "12", MAX_WEBHOOK_CONCURRENT_RUNS])
def test_a_readable_limit_in_range_raises_nothing(value):
    config = {} if value is None else {"max_concurrent_runs": value}
    assert _issues(**config) == []


@pytest.mark.parametrize(
    "value",
    [
        0,
        -1,
        MAX_WEBHOOK_CONCURRENT_RUNS + 1,
        2.5,
        "lots",
        True,
        "\u00b2",
        "\u2460",
        "\u0663",
        pytest.param("9" * 5000, id="5000-digits"),
    ],
)
def test_a_limit_the_trigger_would_change_is_flagged(value):
    assert _issues(max_concurrent_runs=value) == [
        ("webhook.concurrent_runs_out_of_range", "max_concurrent_runs")
    ]


@pytest.mark.asyncio
async def test_the_delivery_is_admitted_against_the_node_s_own_limit(monkeypatch):
    from app.services import flow_triggers

    monkeypatch.setattr(flow_triggers, "hold_back_if_ai_required", AsyncMock(return_value=False))
    admitted = AsyncMock(return_value=False)
    monkeypatch.setattr(flow_triggers, "webhook_run_admitted", admitted)
    created = AsyncMock()
    monkeypatch.setattr(flow_triggers, "create_flow_run", created)
    flow = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)
    db = SimpleNamespace(commit=AsyncMock())

    outcome = await flow_triggers.trigger_flow_from_webhook_outcome(
        db, flow, _node(max_concurrent_runs=3), payload={}, headers={}
    )

    assert outcome.skipped == flow_triggers.WEBHOOK_SKIP_ACTIVE_RUN
    admitted.assert_awaited_once_with(db, flow.id, 3)
    created.assert_not_awaited()
    # The refusal ends its transaction itself: the admission lock goes with it.
    db.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_the_ai_gate_is_asked_before_the_admission_lock(monkeypatch):
    """The gate reads the licence: never inside the lock deliveries wait on."""
    from app.services import flow_triggers

    monkeypatch.setattr(flow_triggers, "hold_back_if_ai_required", AsyncMock(return_value=True))
    admitted = AsyncMock(side_effect=AssertionError("no admission while AI is held back"))
    monkeypatch.setattr(flow_triggers, "webhook_run_admitted", admitted)
    flow = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)

    outcome = await flow_triggers.trigger_flow_from_webhook_outcome(
        SimpleNamespace(commit=AsyncMock()), flow, _node(), payload={}, headers={}
    )

    assert outcome.skipped == flow_triggers.WEBHOOK_SKIP_AI_UNAVAILABLE
    admitted.assert_not_awaited()
