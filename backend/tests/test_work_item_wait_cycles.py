"""Work items never wait on themselves through other items or gated pull requests (#606).

Unit level, no database: the wait graph (item -> item, item -> pull request,
pull request -> the review item gating it), the cycle it finds, the message a
refused write carries, the item a sweep releases, the stalled chain, and the
merge queue removal the polling fallback now reads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.integrations.providers.github.reference_state import dequeued_from_timeline
from app.services import platform_events
from app.services.fleet_health_signals import FINDING_ADVICE, FINDING_FIX
from app.services.work_item_blocked_refs import removal_after
from app.services.work_item_wait_cycles import (
    CYCLE_ADVICE,
    _youngest,
    cycle_id,
    cycle_through,
    find_cycles,
    node_of,
    person_wait_chains,
    refusal_message,
    stalled_chains,
    waits_on_person,
)

REPO = "owner/name"
NOW = datetime(2026, 11, 20, 6, 0, tzinfo=UTC)


def _node(item_id, *, status="blocked", on=(), refs=(), key=None, title=None, payload=None, at=None, held=False):
    return node_of(
        item_id,
        status=status,
        blocked_on=list(on),
        blocked_on_refs=list(refs),
        correlation_key=key,
        title=title,
        payload=payload,
        last_activity_at=at,
        held=held,
    )


def _pr(number, **extra):
    return {"repo": REPO, "kind": "pull_request", "number": number, "release_on": "merged_or_closed", **extra}


def _graph(*nodes):
    return {n.id: n for n in nodes}


# -- item to item --------------------------------------------------------------------


def test_two_items_waiting_on_each_other_are_a_cycle():
    nodes = _graph(_node("a", on=["b"]), _node("b", on=["a"]))
    assert cycle_through("a", nodes) == [("", "b"), ("", "a")]
    assert len(find_cycles(nodes)) == 1


def test_a_three_item_cycle_is_found_once():
    nodes = _graph(_node("a", on=["b"]), _node("b", on=["c"]), _node("c", on=["a"]))
    cycles = find_cycles(nodes)
    assert len(cycles) == 1
    assert {i for _, i in cycles[0]} == {"a", "b", "c"}


def test_a_chain_is_no_cycle():
    nodes = _graph(_node("a", on=["b"]), _node("b", on=["c"]), _node("c", status="open"))
    assert find_cycles(nodes) == []
    assert cycle_through("a", nodes) is None


def test_an_item_that_is_not_blocked_breaks_the_cycle():
    nodes = _graph(_node("a", on=["b"]), _node("b", status="in_progress", on=["a"]))
    assert find_cycles(nodes) == []


# -- item to pull request to the item gating it ---------------------------------------


def test_fix_waits_on_pr_gated_by_a_review_waiting_on_the_fix():
    fix = _node("fix", refs=[_pr(12)])
    review = _node("review", key="ux:name-12", title=f"UX review: {REPO}#12", on=["fix"])
    cycle = cycle_through("fix", _graph(fix, review))
    assert cycle == [(f"{REPO}#12", "review"), ("", "fix")]


def test_the_review_gate_reads_the_short_repository_name_of_its_key():
    fix = _node("fix", refs=[_pr(12)])
    review = _node("review", key="ux:name-12", on=["fix"])
    assert cycle_through("review", _graph(fix, review)) is not None
    other_repo = _node("fix", refs=[{**_pr(12), "repo": "owner/other"}])
    assert cycle_through("review", _graph(other_repo, review)) is None


def test_a_verdict_item_and_gates_pull_requests_gate_too():
    fix = _node("fix", refs=[_pr(12)])
    verdict = _node("verdict", key="dev:ux-verdict-12-comment", title=f"Post the verdict on {REPO}#12", on=["fix"])
    assert cycle_through("fix", _graph(fix, verdict)) is not None
    gate = _node(
        "gate", key="ops:approval", payload={"gates_pull_requests": [{"repo": REPO, "number": "#12"}]}, on=["fix"]
    )
    assert cycle_through("fix", _graph(fix, gate)) is not None


def test_the_recommended_model_is_no_cycle():
    """The review waits on the fix item; the merge waits on the review, never the fix on the merge."""
    fix = _node("fix", status="in_progress")
    review = _node("review", key="ux:name-12", on=["fix"])
    assert find_cycles(_graph(fix, review)) == []


def test_a_satisfied_or_merged_reference_is_no_edge():
    review = _node("review", key="ux:name-12", on=["fix"])
    merged = _node("fix", refs=[_pr(12, state="merged")])
    assert cycle_through("fix", _graph(merged, review)) is None
    deployed_wait = _node("fix", refs=[{"repo": REPO, "kind": "deployment", "number": 12, "merge_sha": "abc"}])
    assert cycle_through("fix", _graph(deployed_wait, review)) is None
    undeployed = _node("fix", refs=[{"repo": REPO, "kind": "deployment", "number": 12, "release_on": "deployed"}])
    assert cycle_through("fix", _graph(undeployed, review)) is not None


# -- what a refusal and a release say ------------------------------------------------


def test_the_refusal_spells_out_the_cycle_and_the_fix():
    fix = _node("fix", refs=[_pr(12)])
    review = _node("review", key="ux:name-12", title=f"{REPO}#12", on=["fix"])
    cycle = cycle_through("fix", _graph(fix, review))
    message = refusal_message("fix", cycle, {"fix": "this item"})
    assert message.startswith(
        f"This wait closes a cycle: this item waits for {REPO}#12, which waits for the item gating it, review, "
        "which waits for this item."
    )
    assert "Nothing would ever release them" in message and CYCLE_ADVICE in message


def test_the_sweep_releases_the_item_blocked_most_recently_and_the_cycle_id_is_stable():
    a = _node("a", on=["b"], at=NOW - timedelta(hours=5))
    b = _node("b", on=["a"], at=NOW - timedelta(hours=1))
    nodes = _graph(a, b)
    (cycle,) = find_cycles(nodes)
    assert _youngest(cycle, nodes) == "b"
    assert cycle_id(cycle) == cycle_id(list(reversed(cycle)))


# -- stalled chains -----------------------------------------------------------------


def test_a_quiet_chain_with_no_live_holder_is_stalled():
    old = NOW - timedelta(hours=3)
    nodes = _graph(_node("a", on=["b"], at=old), _node("b", on=["c"], at=old), _node("c", status="open"))
    assert stalled_chains(nodes, NOW) == [["a", "b"]]


def test_a_recent_or_held_item_keeps_the_chain_alive():
    old = NOW - timedelta(hours=3)
    recent = _graph(_node("a", on=["b"], at=old), _node("b", on=["c"], at=NOW - timedelta(minutes=10)))
    assert stalled_chains(recent, NOW) == []
    held = _graph(_node("a", on=["b"], at=old), _node("b", on=["c"], at=old, held=True))
    assert stalled_chains(held, NOW) == []


def test_items_of_a_cycle_are_not_a_stalled_chain_too():
    old = NOW - timedelta(hours=3)
    nodes = _graph(_node("a", on=["b"], at=old), _node("b", on=["a"], at=old))
    assert stalled_chains(nodes, NOW, in_cycles=["a", "b"]) == []


# -- a chain waiting on a person's open decision (#2326) -----------------------------

ASK = {
    "question": "Accept the residual risk on this asset?",
    "context": "The fix needs a maintenance window the asset owner has not given.",
    "options": [
        {"key": "A", "label": "Accept the risk for 30 days", "effect": "The case waits 30 days."},
        {"key": "B", "label": "Escalate to the asset owner", "effect": "The case goes to the owner."},
    ],
    "recommendation": "B",
}


def _gate(item_id="gate", *, due=None, ask=ASK, payload=None, at=None, recipient="person-1"):
    return node_of(
        item_id,
        status="blocked",
        blocked_on=[],
        blocked_on_refs=[],
        correlation_key=None,
        title=None,
        payload=payload,
        last_activity_at=at,
        needs_human=True,
        ask=ask,
        due_at=due,
        created_at=at,
        recipient=recipient,
    )


def test_a_chain_waiting_on_a_valid_unanswered_gate_is_not_stalled():
    old = NOW - timedelta(hours=3)
    nodes = _graph(_node("case", on=["gate"], at=old), _gate(due=NOW + timedelta(days=1), at=old))
    assert waits_on_person(nodes["gate"], NOW)
    assert stalled_chains(nodes, NOW) == []
    assert person_wait_chains(nodes, NOW) == [["case", "gate"]]


def test_a_gate_with_no_due_date_still_waits_on_its_person():
    old = NOW - timedelta(hours=30)
    nodes = _graph(_node("case", on=["gate"], at=old), _gate(at=old))
    assert stalled_chains(nodes, NOW) == []


def test_a_gate_past_its_due_date_stalls_the_chain():
    old = NOW - timedelta(hours=3)
    nodes = _graph(_node("case", on=["gate"], at=old), _gate(due=NOW - timedelta(minutes=1), at=old))
    assert stalled_chains(nodes, NOW) == [["case", "gate"]]
    assert person_wait_chains(nodes, NOW) == []


def test_an_answered_gate_that_never_released_its_chain_is_stalled():
    old = NOW - timedelta(hours=3)
    answered = _gate(due=NOW + timedelta(days=1), at=old, payload={"answer": {"option": "B", "answered_at": "x"}})
    nodes = _graph(_node("case", on=["gate"], at=old), answered)
    assert stalled_chains(nodes, NOW) == [["case", "gate"]]


def test_a_keep_waiting_answer_leaves_the_gate_waiting():
    old = NOW - timedelta(hours=3)
    ask = {**ASK, "options": [*ASK["options"], {"key": "W", "label": "Keep waiting", "keeps_waiting": True}]}
    gate = _gate(due=NOW + timedelta(days=1), ask=ask, at=old, payload={"answer": {"option": "W"}})
    assert stalled_chains(_graph(_node("case", on=["gate"], at=old), gate), NOW) == []


def test_a_gate_on_an_unreadable_card_or_a_dead_blocker_is_stalled():
    old = NOW - timedelta(hours=3)
    blank = _gate(due=NOW + timedelta(days=1), ask={"question": ""}, at=old)
    assert stalled_chains(_graph(_node("case", on=["gate"], at=old), blank), NOW) == [["case", "gate"]]
    dead = _graph(_node("case", on=["sink"], at=old), _node("sink", at=old))
    assert stalled_chains(dead, NOW) == [["case", "sink"]]


def test_hundreds_of_gated_chains_are_never_flagged_before_their_due_date_whatever_was_touched():
    nodes = {}
    for n in range(300):
        at = NOW - timedelta(hours=2 + n % 7, minutes=n)
        nodes[f"case-{n}"] = _node(f"case-{n}", on=[f"gate-{n}"], at=NOW - timedelta(minutes=n % 3) if n % 2 else at)
        nodes[f"gate-{n}"] = _gate(f"gate-{n}", due=NOW + timedelta(days=1 + n % 3), at=at)
    assert stalled_chains(nodes, NOW) == []
    assert len(person_wait_chains(nodes, NOW)) == 300


def test_the_report_lists_person_waits_apart_with_recipient_and_age():
    from app.services.fleet_health_signals import FleetHealthReport, format_report, person_wait_rows

    old = NOW - timedelta(hours=3)
    nodes = _graph(_node("case", on=["gate"], at=old), _gate(due=NOW + timedelta(days=1), at=old))
    rows = person_wait_rows(nodes, person_wait_chains(nodes, NOW), NOW)
    assert rows == [{"items": ["case", "gate"], "decisions": ["gate"], "recipients": ["person-1"], "age_hours": 3.0}]
    report = FleetHealthReport(
        fleet_id="f1", fleet_name="Ops", status="ok", members=[], findings=[], waiting_on_people=rows
    )
    assert report.healthy and report.structured()["waiting_on_people"] == rows
    assert "Waiting on a person's decision (1 chain(s), no finding)" in format_report(report)
    assert "cancel" not in FINDING_ADVICE["blocked_chain_stalled"]


def test_fleet_health_and_the_event_catalog_know_the_new_kinds():
    for kind in ("wait_cycle", "blocked_chain_stalled"):
        assert kind in FINDING_FIX and FINDING_ADVICE[kind]
    assert "work_item.wait_cycle" in platform_events.PLATFORM_EVENTS


# -- a merge queue removal the webhook never delivered --------------------------------


def _timeline(state="OPEN", queued=False, removals=({"createdAt": "2026-11-20T03:37:19Z", "reason": "FAILED_CHECKS"},)):
    return {
        "repository": {
            "pullRequest": {
                "state": state,
                "isInMergeQueue": queued,
                "url": "https://x/602",
                "headRefName": "fix/x",
                "headRefOid": "40b51827",
                "timelineItems": {"nodes": list(removals)},
            }
        }
    }


def test_an_open_pull_request_out_of_the_queue_answers_its_latest_removal():
    event = dequeued_from_timeline("Owner/Name", 602, _timeline())
    assert event is not None
    assert (event.kind, event.repo, event.pr_numbers) == ("dequeued", "owner/name", (602,))
    assert (event.conclusion, event.closed_at) == ("failed_checks", "2026-11-20T03:37:19Z")


def test_no_removal_back_in_the_queue_or_closed_answers_nothing():
    assert dequeued_from_timeline(REPO, 602, _timeline(removals=())) is None
    assert dequeued_from_timeline(REPO, 602, _timeline(queued=True)) is None
    assert dequeued_from_timeline(REPO, 602, _timeline(state="MERGED")) is None
    assert dequeued_from_timeline(REPO, 602, "not json") is None


def test_a_polled_removal_counts_only_after_the_items_last_activity():
    parked = datetime(2026, 11, 20, 3, 15, tzinfo=UTC)
    assert removal_after("2026-11-20T03:37:19Z", parked)
    assert not removal_after("2026-11-20T03:37:19Z", parked + timedelta(hours=1))
    assert not removal_after(None, parked)
    assert removal_after("2026-11-20T03:37:19Z", None)
