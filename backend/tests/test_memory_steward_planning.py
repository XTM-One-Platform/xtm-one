"""Memory Steward planning rules (#8258), without a database.

What a pass decides for one lane: the model's answer kept to what is well
formed, merges (newest kept), contradictions (newest wins unless the older is
confirmed more often, then flagged), demotions past the perishable window,
promotions after three summary versions, and nothing the lane already
recorded (undone or flagged) planned again.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.services.concierge.memory_history import operation_fingerprint, text_digest
from app.services.concierge.memory_steward_plan import (
    MAX_MODEL_GROUP,
    Candidate,
    PlannedOp,
    Verdict,
    observe_summary,
    parse_verdict,
    plan_entry_operations,
    plan_promotions,
)

NOW = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)


def _candidate(content: str, *, days_old: float, embedding=None, space=("m", 3), confirmations=1) -> Candidate:
    return Candidate(
        id=uuid.uuid4(),
        content=content,
        created_at=NOW - timedelta(days=days_old),
        embedding=embedding,
        embedding_space=space,
        confirmations=confirmations,
    )


def _ops(planned: list[PlannedOp]) -> list[tuple[str, tuple, object]]:
    return [(op.operation, op.before, op.after) for op in planned]


# ── parse_verdict ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"duplicates": [[1, 2]], "contradictions": [[2, 3]], "time_bound": [3]}', Verdict(((0, 1),), ((1, 2),), (2,))),
        ('```json\n{"duplicates": [[1, 2, 3]]}\n```', Verdict(duplicates=((0, 1, 2),))),
        ('Here it is: {"time_bound": [2, 2, 1]}', Verdict(time_bound=(0, 1))),
        # Out of range, zero, negative, booleans, strings, floats: dropped.
        ('{"duplicates": [[1, 4], [0, 1], [-1, 2], [true, 2], ["1", "2"], [1.0, 2]]}', Verdict()),
        # A group naming one entry twice, or a single entry, is no group.
        ('{"duplicates": [[2, 2], [1]], "contradictions": [[1, 2, 3], [3, 3]]}', Verdict()),
        ('{"time_bound": [9, false, "2"]}', Verdict()),
        ("not json at all", Verdict()),
        ("[1, 2]", Verdict()),
        ('{"duplicates": "1,2", "contradictions": {"a": 1}, "time_bound": 2}', Verdict()),
        ("", Verdict()),
    ],
)
def test_parse_verdict_keeps_only_well_formed_numbers(raw: str, expected: Verdict) -> None:
    assert parse_verdict(raw, 3) == expected


# ── merges ───────────────────────────────────────────────────────────────────


def test_same_text_merges_into_the_newest() -> None:
    old = _candidate("Prefers  answers in French", days_old=9)
    new = _candidate("prefers answers in french", days_old=1)
    other = _candidate("Works on the SOC team", days_old=4)

    planned = plan_entry_operations([old, other, new], Verdict(), now=NOW)

    assert _ops(planned) == [("merge", (old.id,), new.id)]
    assert planned[0].reason == "duplicate"


def test_close_embeddings_merge_only_within_one_embedding_space() -> None:
    a = _candidate("Uses Splunk", days_old=5, embedding=(1.0, 0.0, 0.1))
    b = _candidate("Their SIEM is Splunk", days_old=2, embedding=(1.0, 0.0, 0.12))
    c = _candidate("SIEM: Splunk", days_old=1, embedding=(1.0, 0.0, 0.1), space=("other-model", 3))

    planned = plan_entry_operations([a, b, c], Verdict(), now=NOW)

    assert _ops(planned) == [("merge", (a.id,), b.id)]


def test_far_embeddings_do_not_merge() -> None:
    a = _candidate("Uses Splunk", days_old=5, embedding=(1.0, 0.0, 0.0))
    b = _candidate("Works nights", days_old=2, embedding=(0.0, 1.0, 0.0))

    assert plan_entry_operations([a, b], Verdict(), now=NOW) == []


def test_model_groups_join_the_deterministic_ones() -> None:
    a, b, c = (_candidate(text, days_old=d) for text, d in (("A one", 3), ("a one", 2), ("A first", 1)))

    planned = plan_entry_operations([a, b, c], Verdict(duplicates=((1, 2),)), now=NOW)

    assert len(planned) == 1
    assert planned[0].after == c.id
    assert set(planned[0].before) == {a.id, b.id}


def test_a_model_group_never_grows_past_the_cap() -> None:
    candidates = [_candidate(f"Fact {n} about the SOC", days_old=n) for n in range(12)]
    # Overlapping groups that would chain the whole window into one.
    chained = tuple(tuple(range(start, start + 4)) for start in range(0, 9, 3))

    planned = plan_entry_operations(candidates, Verdict(duplicates=chained), now=NOW)

    assert planned
    assert all(len(op.before) + 1 <= MAX_MODEL_GROUP for op in planned)


# ── contradictions ───────────────────────────────────────────────────────────


def test_contradiction_newest_wins() -> None:
    old = _candidate("Their EDR is Defender", days_old=20)
    new = _candidate("Their EDR is CrowdStrike", days_old=2)

    planned = plan_entry_operations([new, old], Verdict(contradictions=((0, 1),)), now=NOW)

    assert _ops(planned) == [("remove", (old.id,), new.id)]
    assert planned[0].reason == "contradiction"


def test_contradiction_with_an_older_entry_confirmed_more_often_is_flagged() -> None:
    old = _candidate("Their EDR is Defender", days_old=20, confirmations=3)
    new = _candidate("Their EDR is CrowdStrike", days_old=2)

    planned = plan_entry_operations([old, new], Verdict(contradictions=((0, 1),)), now=NOW)

    assert _ops(planned) == [("flag", (old.id, new.id), None)]


def test_equally_confirmed_contradiction_is_settled_for_the_newest() -> None:
    old = _candidate("On call on Mondays", days_old=20, confirmations=2)
    new = _candidate("On call on Fridays", days_old=2, confirmations=2)

    planned = plan_entry_operations([old, new], Verdict(contradictions=((0, 1),)), now=NOW)

    assert _ops(planned) == [("remove", (old.id,), new.id)]


def test_an_entry_takes_part_in_one_operation_per_pass() -> None:
    a = _candidate("Lives in Lyon", days_old=10)
    b = _candidate("lives in lyon", days_old=5)
    c = _candidate("Lives in Paris", days_old=1)

    planned = plan_entry_operations(
        [a, b, c], Verdict(contradictions=((1, 2),), time_bound=(0,)), now=NOW
    )

    assert _ops(planned) == [("merge", (a.id,), b.id)]


# ── demotions ────────────────────────────────────────────────────────────────


def test_time_bound_entry_is_demoted_only_past_the_perishable_window() -> None:
    fresh = _candidate("On holiday this week", days_old=1)
    stale = _candidate("Travelling to Tokyo next Monday", days_old=10)

    planned = plan_entry_operations([fresh, stale], Verdict(time_bound=(0, 1)), now=NOW)

    assert _ops(planned) == [("demote", (stale.id,), None)]
    assert planned[0].reason == "stale"


# ── nothing recorded is planned again ────────────────────────────────────────


def test_an_operation_the_lane_recorded_is_not_planned_again() -> None:
    old = _candidate("Prefers French", days_old=9)
    new = _candidate("prefers french", days_old=1)
    first = plan_entry_operations([old, new], Verdict(), now=NOW)
    assert len(first) == 1

    again = plan_entry_operations(
        [old, new], Verdict(), now=NOW, known_fingerprints=frozenset({first[0].fingerprint})
    )

    assert again == []


def test_fingerprints_do_not_depend_on_order() -> None:
    a = _candidate("x is y", days_old=3)
    b = _candidate("X is y", days_old=2)
    c = _candidate("x  is y", days_old=1)

    first = plan_entry_operations([a, b, c], Verdict(), now=NOW)
    second = plan_entry_operations([c, a, b], Verdict(), now=NOW)

    assert [op.fingerprint for op in first] == [op.fingerprint for op in second]


# ── promotions ───────────────────────────────────────────────────────────────


def test_an_observation_is_counted_once_per_summary_version() -> None:
    summary_v1 = "- Prefers short answers with a table\n- Asked about phishing today"
    observations, ready = observe_summary(summary_v1, {})
    assert ready == []
    assert sorted(entry["seen"] for entry in observations.values()) == [1, 1]

    # The same summary again counts nothing.
    observations, ready = observe_summary(summary_v1, observations)
    assert sorted(entry["seen"] for entry in observations.values()) == [1, 1]

    observations, ready = observe_summary("- Prefers short answers with a table\n- New topic: DLP", observations)
    assert ready == []
    observations, ready = observe_summary("* prefers  short answers with a table\n- Asked about EDR", observations)

    assert ready == [(text_digest("prefers short answers with a table"), "prefers short answers with a table")]
    # A bullet gone from the summary is forgotten: it did not recur.
    assert len(observations) == 2


def test_observations_hold_digests_never_text() -> None:
    observations, _ = observe_summary("- The customer's VPN password policy is strict", {})

    assert "VPN" not in repr(observations)


def test_promotion_skips_what_the_lane_holds_and_what_was_undone() -> None:
    ready = [("d1", "Prefers short answers"), ("d2", "Works on the SOC team"), ("d3", "Uses a Mac")]
    undone = frozenset({operation_fingerprint("promote", "d3")})

    planned = plan_promotions(ready, ["prefers  SHORT answers"], known_fingerprints=undone)

    assert [(op.operation, op.promote_text, op.before, op.after) for op in planned] == [
        ("promote", "Works on the SOC team", (), None)
    ]
