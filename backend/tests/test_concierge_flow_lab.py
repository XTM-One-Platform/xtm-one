"""Flow Lab pure diagnostics (#8260): paths, payload mapping, trigger take-over, mining, node stats."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.concierge.flow_lab import (
    MIN_PATH_RUNS,
    excerpt,
    flow_trigger_for_assignment_run,
    modal_path,
    node_stats,
    path_verdict,
    repeated_ngrams,
    switch_over_refusal,
    tool_sequence,
    trigger_takeover_refusal,
)


def _assignment(trigger_type: str = "schedule") -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), name="Daily triage", trigger_type=trigger_type)



# ── Agent-to-flow path ─────────────────────────────────────────────────────


def test_tool_sequence_reads_names_only_and_ignores_malformed_entries():
    actions = [
        {"tool": "search_alerts", "arguments": {"q": "secret"}},
        {"tool": "  "},
        "not a dict",
        {"arguments": {}},
        {"tool": "create_ticket"},
    ]
    assert tool_sequence(actions) == ("search_alerts", "create_ticket")
    assert tool_sequence(None) == ()
    assert tool_sequence({"tool": "x"}) == ()


def test_modal_path_breaks_ties_on_the_shorter_sequence():
    path, share = modal_path([("a", "b", "c"), ("a", "b"), ("a", "b", "c"), ("a", "b")])
    assert path == ("a", "b")
    assert share == 0.5
    assert modal_path([]) == ((), None)


def test_a_stable_scheduled_assignment_is_switchable():
    verdict = path_verdict(_assignment("schedule"), [("search", "summarize")] * 9 + [("search",)])
    assert verdict.runs == 10
    assert verdict.path == ("search", "summarize")
    assert verdict.share == 0.9
    assert verdict.stable is True
    assert verdict.switchable is True
    assert verdict.reasons == ()


def test_an_unstable_path_is_neither_stable_nor_switchable():
    verdict = path_verdict(_assignment(), [("a", "b")] * 7 + [("c",)] * 3)
    assert verdict.stable is False
    assert verdict.switchable is False
    assert verdict.reasons == ("the most frequent path covers 70 % of the runs, 80 % needed",)


def test_too_few_runs_give_no_path():
    verdict = path_verdict(_assignment(), [("a", "b")] * (MIN_PATH_RUNS - 1))
    assert verdict.stable is False
    assert verdict.reasons == (f"only {MIN_PATH_RUNS - 1} completed runs, {MIN_PATH_RUNS} needed",)


def test_runs_without_tool_calls_have_no_path_to_reproduce():
    verdict = path_verdict(_assignment(), [()] * 10)
    assert verdict.stable is False
    assert verdict.reasons == ("the runs call no tool, so there is no path to reproduce",)


def test_a_stable_webhook_assignment_is_stable_but_not_switchable():
    verdict = path_verdict(_assignment("webhook"), [("a", "b")] * 10)
    assert verdict.stable is True
    assert verdict.switchable is False
    assert verdict.reasons == (switch_over_refusal("webhook"),)
    assert "new delivery URL" in verdict.reasons[0]


@pytest.mark.parametrize(
    ("trigger_type", "switchable"),
    [("schedule", True), ("integration_event", True), ("work_item", True), ("webhook", False), ("manual", False)],
)
def test_switch_over_refusal_per_trigger_type(trigger_type, switchable):
    assert (switch_over_refusal(trigger_type) is None) is switchable


# ── Trigger payload mapping ────────────────────────────────────────────────


def test_schedule_payload_maps_to_the_flow_schedule_trigger():
    assert flow_trigger_for_assignment_run(
        "schedule", {"trigger_type": "schedule", "scheduled_at": "2026-11-03T08:00:00+00:00"}, {"type": "schedule"}
    ) == ("schedule", {"fired_at": "2026-11-03T08:00:00+00:00"})


def test_integration_event_payload_keeps_the_event_and_its_type():
    event = {"subject": "Invoice", "from": "a@example.com"}
    assert flow_trigger_for_assignment_run(
        "integration_event", event, {"type": "integration_event", "event": "new_email"}
    ) == ("integration_event", {"payload": event, "event_type": "new_email"})


def test_work_item_payload_is_shaped_as_the_flow_work_item_trigger():
    item = {"id": "w1", "title": "Patch host", "description": "CVE"}
    assert flow_trigger_for_assignment_run(
        "work_item", {"trigger_type": "work_item", "work_item_id": "w1", "work_item": item}, {}
    ) == ("work_item", {"payload": item, "work_item_id": "w1", "title": "Patch host", "description": "CVE"})


def test_webhook_payload_drops_the_delivery_headers():
    trigger_type, payload = flow_trigger_for_assignment_run(
        "webhook", {"alert": 1}, {"type": "webhook", "headers": {"authorization": "Bearer x"}}
    )
    assert trigger_type == "webhook"
    assert payload == {"payload": {"alert": 1}, "headers": {}}


@pytest.mark.parametrize("payload", [None, "text", ["list"]])
def test_hostile_payload_shapes_never_raise(payload):
    assert flow_trigger_for_assignment_run("integration_event", payload, None) == (
        "integration_event",
        {"payload": {}, "event_type": ""},
    )


def test_manual_has_no_flow_counterpart():
    assert flow_trigger_for_assignment_run("manual", {}, {}) is None


def _graph(*nodes: tuple[str, dict]) -> dict:
    return {"nodes": [{"id": f"n{i}", "kind": kind, "config": config} for i, (kind, config) in enumerate(nodes)], "edges": []}


def test_a_flow_takes_a_schedule_over_only_on_the_same_cron():
    assignment = SimpleNamespace(trigger_type="schedule", trigger_config={"cron": "0 8 * * 1-5"})
    agent = SimpleNamespace(integration_bindings={})
    same = _graph(("trigger.schedule", {"cron": "0  8 * * 1-5"}), ("trigger.manual", {}))
    assert trigger_takeover_refusal(assignment, agent, same) is None
    assert trigger_takeover_refusal(assignment, agent, _graph(("trigger.schedule", {"cron": "0 9 * * 1-5"}))) == (
        "the flow's schedule differs from the assignment's"
    )


@pytest.mark.parametrize(
    "graph",
    [
        _graph(),
        _graph(("trigger.webhook", {})),
        _graph(("trigger.schedule", {"cron": "0 8 * * *"}), ("trigger.schedule", {"cron": "0 9 * * *"})),
    ],
    ids=["no-trigger", "other-kind", "two-automatic-triggers"],
)
def test_a_flow_needs_exactly_the_assignments_one_automatic_trigger(graph):
    assignment = SimpleNamespace(trigger_type="schedule", trigger_config={"cron": "0 8 * * *"})
    assert trigger_takeover_refusal(assignment, SimpleNamespace(), graph) == (
        "the flow needs exactly one automatic trigger, a trigger.schedule node"
    )


def test_a_schedule_take_over_needs_the_same_timezone():
    assignment = SimpleNamespace(trigger_type="schedule", trigger_config={"cron": "0 7 * * *", "timezone": "Europe/Paris"})
    refusal = "the flow's schedule timezone differs from the assignment's"

    def flow(**config):
        return _graph(("trigger.schedule", {"cron": "0 7 * * *", **config}))

    assert trigger_takeover_refusal(assignment, None, flow(timezone=" Europe/Paris ")) is None
    assert trigger_takeover_refusal(assignment, None, flow()) == refusal
    assert trigger_takeover_refusal(assignment, None, flow(timezone="America/New_York")) == refusal
    utc = SimpleNamespace(trigger_type="schedule", trigger_config={"cron": "0 7 * * *"})
    assert trigger_takeover_refusal(utc, None, flow(timezone="UTC")) is None
    # The assignment reads an unknown zone as UTC, the flow node never fires on it.
    assert trigger_takeover_refusal(utc, None, flow(timezone="Mars/Olympus")) == refusal


def test_a_work_item_take_over_needs_the_same_queue_prefix_and_priority():
    agent_id, other_agent = uuid.uuid4(), uuid.uuid4()
    assignment = SimpleNamespace(
        trigger_type="work_item", agent_id=agent_id, trigger_config={"correlation_prefix": "inc-", "priority_min": "high"}
    )
    other_queue = "the flow drains another agent's work items than the assignment"

    def flow(**config):
        base = {"owner_agent_id": str(agent_id), "correlation_prefix": " inc- ", "priority_min": "high"}
        return _graph(("trigger.work_item", {**base, **config}))

    assert trigger_takeover_refusal(assignment, None, flow()) is None
    assert trigger_takeover_refusal(assignment, None, flow(owner_agent_id=str(other_agent))) == other_queue
    assert trigger_takeover_refusal(assignment, None, flow(owner_agent_id="")) == other_queue
    assert trigger_takeover_refusal(assignment, None, flow(correlation_prefix="")) == (
        "the flow takes work items of another correlation prefix than the assignment"
    )
    assert trigger_takeover_refusal(assignment, None, flow(priority_min="low")) == (
        "the flow takes work items of another minimum priority than the assignment"
    )
    # A handover assignment drains the queue its config names, not its agent's.
    handover = SimpleNamespace(trigger_type="work_item", agent_id=agent_id, trigger_config={"owner_agent_id": str(other_agent)})
    unfiltered = {"correlation_prefix": "", "priority_min": ""}
    assert trigger_takeover_refusal(handover, None, flow(owner_agent_id=str(other_agent), **unfiltered)) is None
    assert trigger_takeover_refusal(handover, None, flow(**unfiltered)) == other_queue


def test_an_integration_event_take_over_matches_integration_event_and_no_filter():
    integration_id = str(uuid.uuid4())
    assignment = SimpleNamespace(trigger_type="integration_event", trigger_config={"provider": "microsoft", "event": "new_email"})
    filtered = SimpleNamespace(
        trigger_type="integration_event",
        trigger_config={"provider": "microsoft", "event": "new_email", "filter": {"to_me": True}},
    )
    agent = SimpleNamespace(integration_bindings={"microsoft": [integration_id]})

    def flow(**config):
        return _graph(("trigger.integration_event", {"integration_id": integration_id, "event_type": "new_email", **config}))

    refusal = "the flow or the assignment filters events: their filter languages differ"
    assert trigger_takeover_refusal(assignment, agent, flow()) is None
    # Even an identical predicate: the two filter languages read it differently.
    assert trigger_takeover_refusal(filtered, agent, flow(filter={"to_me": True})) == refusal
    assert trigger_takeover_refusal(filtered, agent, flow()) == refusal
    assert trigger_takeover_refusal(assignment, agent, flow(filter={"to_me": True})) == refusal
    assert trigger_takeover_refusal(assignment, agent, flow(event_type="calendar")) == (
        "the flow listens to another event than the assignment"
    )
    assert trigger_takeover_refusal(
        assignment, agent, _graph(("trigger.integration_event", {"integration_id": str(uuid.uuid4())}))
    ) == ("the flow listens to another integration than the assignment")


def test_a_per_user_assignment_is_never_taken_over():
    assignment = SimpleNamespace(trigger_type="schedule", trigger_config={"cron": "0 8 * * *"}, run_as_all_users=True)
    refusal = "a per-user assignment (one run for each user) cannot be replaced by a single flow"
    assert trigger_takeover_refusal(assignment, SimpleNamespace(), _graph(("trigger.schedule", {"cron": "0 8 * * *"}))) == refusal
    fan_out = SimpleNamespace(id=uuid.uuid4(), name="Per-user digest", trigger_type="schedule", run_as_all_users=True)
    verdict = path_verdict(fan_out, [("a", "b")] * 10)
    assert (verdict.stable, verdict.switchable, verdict.reasons) == (True, False, (refusal,))


def test_a_webhook_assignment_is_never_taken_over():
    assignment = SimpleNamespace(trigger_type="webhook", trigger_config={})
    assert trigger_takeover_refusal(assignment, SimpleNamespace(), _graph(("trigger.webhook", {}))) == (
        switch_over_refusal("webhook")
    )


def test_an_excerpt_is_bounded_and_serializes_any_value():
    assert excerpt("short", 10) == "short"
    assert excerpt("x" * 20, 10) == "x" * 9 + "…"
    assert excerpt({"a": [1, "b"]}, 100) == '{"a": [1, "b"]}'


# ── New-flow mining ────────────────────────────────────────────────────────


def test_repeated_ngrams_needs_several_users_and_keeps_the_longest_sequence():
    shared = ("search_cve", "get_asset", "create_ticket")
    sequences = [(f"u{i % 3}", ("greet", *shared)) for i in range(6)]
    candidates = repeated_ngrams(sequences, min_occurrences=5, min_users=2)
    assert [c.sequence for c in candidates] == [("greet", "search_cve", "get_asset", "create_ticket")]
    assert candidates[0].conversations == 6
    assert candidates[0].users == 3


def test_one_users_habit_is_not_a_candidate():
    assert repeated_ngrams([("u1", ("a", "b"))] * 10, min_occurrences=5, min_users=2) == []


def test_retry_loops_and_once_per_conversation_counting():
    sequences = [(f"u{i}", ("poll", "poll", "poll", "a", "b", "a", "b")) for i in range(5)]
    candidates = repeated_ngrams(sequences, min_occurrences=5, min_users=2)
    sequences_found = {c.sequence for c in candidates}
    assert ("poll", "poll") not in sequences_found
    assert all(c.conversations == 5 for c in candidates)


# ── Repair diagnosis ───────────────────────────────────────────────────────


def test_node_stats_counts_failures_and_bounds_errors():
    start = datetime(2026, 11, 3, tzinfo=UTC)

    def step(node, status, error=None, ms=100):
        return SimpleNamespace(
            node_id=node,
            node_kind="action.http_request",
            status=status,
            error=error,
            started_at=start,
            completed_at=start + timedelta(milliseconds=ms),
        )

    stats = node_stats(
        [step("http", "failed", "x" * 1000), step("http", "failed", "timeout"), step("http", "completed", ms=300)]
    )
    assert len(stats) == 1
    node = stats[0]
    assert node.executions == 3
    assert node.failures == 2
    assert len(node.errors) == 2
    assert max(len(e) for e in node.errors) == 300
    assert node.duration_p50_ms == 100.0
    assert node.duration_p95_ms == 300.0
