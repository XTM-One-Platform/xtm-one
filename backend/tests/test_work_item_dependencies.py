"""Work-item dependencies without a database: the pure parts."""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.services import work_item as wi_service
from app.services.work_item_context import pickup_state

A, B = str(uuid.uuid4()), str(uuid.uuid4())
NOW = datetime(2026, 10, 31, 22, 0, tzinfo=UTC)


def test_normalize_blocked_on_keeps_uuids_in_order_once_and_drops_its_own_id():
    own = str(uuid.uuid4())
    assert wi_service.normalize_blocked_on([A, "nope", A.upper(), own, B], own_id=own) == [A, B]
    assert wi_service.normalize_blocked_on(f"{A}, {B}") == [A, B]
    assert wi_service.normalize_blocked_on(None) == []


def test_normalize_title_ignores_case_and_spacing():
    assert wi_service.normalize_title("  Split  the Remainder ") == wi_service.normalize_title("split the remainder")


def test_the_dependency_note_unblocks_only_when_every_blocker_is_done():
    assert wi_service._dependency_note([A, B], {A: "done", B: "in_progress"}, NOW) is None
    note = wi_service._dependency_note([A, B], {A: "done", B: "done"}, NOW)
    assert note["unblocked"] is True and note["text"] == f"unblocked: {A}, {B} done"
    # Cancelled or gone, nothing left running: surfaced to its owner with the reason (#762).
    note = wi_service._dependency_note([A, B], {A: "cancelled"}, NOW)
    assert note is not None
    assert note["unblocked"] is False and note["surfaced"] is True
    assert f"{A} cancelled" in note["text"] and f"{B} no longer exists" in note["text"]
    assert note["text"].startswith("surfaced: ")
    # One done, one cancelled: surfaced too, nothing would release the cancelled one.
    mixed = wi_service._dependency_note([A, B], {A: "done", B: "cancelled"}, NOW)
    assert mixed is not None and mixed["surfaced"] is True
    # One still running: wait, say nothing yet.
    assert wi_service._dependency_note([A, B], {A: "cancelled", B: "open"}, NOW) is None


def test_release_on_passed_reopens_only_when_every_blocker_passed():
    """#11214: a retest done FAIL or INCONCLUSIVE never releases the item waiting for a PASS."""
    note = wi_service._dependency_note([A], {A: "done"}, NOW, verdicts={A: "PASS"}, release_on="passed")
    assert note["unblocked"] is True and note["text"] == f"unblocked: {A} passed"
    for verdict, said in (("FAIL", "FAIL"), ("INCONCLUSIVE", "INCONCLUSIVE"), (None, "without a verdict")):
        note = wi_service._dependency_note([A], {A: "done"}, NOW, verdicts={A: verdict}, release_on="passed")
        assert note["unblocked"] is False
        assert note["text"].startswith(f"still blocked: {A} completed {said}")
    # One passed, one failed: still blocked, the failed one named.
    note = wi_service._dependency_note(
        [A, B], {A: "done", B: "done"}, NOW, verdicts={A: "PASS", B: "FAIL"}, release_on="passed"
    )
    assert note["unblocked"] is False and f"{B} completed FAIL" in note["text"] and A not in note["text"]
    # Still running: say nothing yet, as without the condition.
    assert wi_service._dependency_note([A], {A: "in_progress"}, NOW, verdicts={}, release_on="passed") is None


def test_without_release_on_a_verdict_changes_nothing():
    for release_on in (None, "done"):
        note = wi_service._dependency_note([A], {A: "done"}, NOW, verdicts={A: "FAIL"}, release_on=release_on)
        assert note["unblocked"] is True and note["text"] == f"unblocked: {A} done"


def test_verdict_and_release_on_are_normalized_and_checked():
    assert wi_service.normalize_verdict(" pass ") == "PASS"
    assert wi_service.normalize_verdict("") is None and wi_service.normalize_verdict(None) is None
    assert wi_service.normalize_release_on("PASSED") == "passed"
    assert wi_service.normalize_release_on(None) is None
    for bad, fn in (("OK", wi_service.normalize_verdict), ("merged", wi_service.normalize_release_on)):
        try:
            fn(bad)
        except ValueError as exc:
            assert repr(bad) in str(exc)
        else:  # pragma: no cover
            raise AssertionError(f"{bad!r} accepted")
    assert wi_service._verdict_of({"verdict": "FAIL"}) == "FAIL"
    assert wi_service._verdict_of({"verdict": "maybe"}) is None and wi_service._verdict_of(None) is None


def test_with_release_on_sets_and_drops_the_payload_key():
    payload = {"program_key": "program:x"}
    kept = wi_service.with_release_on(payload, "passed")
    assert kept == {"program_key": "program:x", "blocked_on_release_on": "passed"}
    assert payload == {"program_key": "program:x"}  # pure
    assert wi_service.with_release_on(kept, "done") == {"program_key": "program:x"}
    assert wi_service.with_release_on(None, None) == {}


def test_deployed_is_an_opt_in_release_and_the_only_one_that_carries_a_deployment():
    # #11919: blocked_on releases at the completion; only release_on deployed waits for the build.
    from app.services.work_item_pull_requests import waits_for_deployment

    assert wi_service.normalize_release_on(" Deployed ") == "deployed"
    kept = wi_service.with_release_on({"program_key": "program:x"}, "deployed")
    assert kept == {"program_key": "program:x", "blocked_on_release_on": "deployed"}
    assert waits_for_deployment(kept)
    for payload in (None, {}, {"program_key": "program:x"}, {"blocked_on_release_on": "passed"}):
        assert not waits_for_deployment(payload)
    # The sweep treats deployed as done: the carried reference, not the dependency, holds it.
    note = wi_service._dependency_note([A], {A: "done"}, datetime.now(UTC), release_on="deployed")
    assert note is not None and note["unblocked"] is True


def _item(**overrides):
    base = dict(
        status="blocked",
        blocked_on=[A],
        action_mode=None,
        due_at=None,
        last_activity_at=NOW - timedelta(days=5),
        created_at=NOW - timedelta(days=5),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_an_item_blocked_on_items_is_never_stale_a_plain_blocked_one_still_is():
    assert not wi_service.is_work_item_stale(_item(), NOW)
    assert not wi_service.is_work_item_stale(_item(due_at=NOW - timedelta(days=1)), NOW)
    assert wi_service.is_work_item_stale(_item(blocked_on=None), NOW)


def test_the_pickup_state_of_an_item_blocked_on_items():
    assert pickup_state({"status": "blocked", "blocked_on": [A]}, [], NOW) == "blocked_on_items"
    assert pickup_state({"status": "blocked", "blocked_on": []}, [], NOW) == "blocked"


OWNER = str(uuid.uuid4())
PR = {"repo": "o/r", "kind": "pull_request", "number": 7, "release_on": "merged_or_closed"}
ISSUE = {"repo": "o/r", "kind": "issue", "number": 8, "release_on": "closed"}
DEPLOY = {"repo": "o/r", "kind": "deployment", "number": 7, "release_on": "deployed"}
RUN = {"repo": "o/r", "kind": "workflow_run", "workflow": "deploy.yml", "after": "2026-11-21T00:00:00Z", "release_on": "succeeded"}


def _blocked(**fields) -> dict:
    return {"status": "blocked", "owner_agent_id": OWNER, "blocked_on": [], "blocked_on_refs": [], **fields}


def test_an_item_waiting_for_references_waits_and_is_never_not_picked_up():
    """#861: every reference kind the platform releases reads as a wait, not 'Blocked: not picked up'."""
    for ref in (PR, ISSUE, DEPLOY, RUN):
        assert pickup_state(_blocked(blocked_on_refs=[ref]), [], NOW) == "waiting_for_refs"
    # Items and references: the items name the state, the references are listed beside them.
    assert pickup_state(_blocked(blocked_on=[A], blocked_on_refs=[PR]), [], NOW) == "blocked_on_items"
    # Every reference already satisfied: nothing left to release it, it needs an action.
    merged = {**PR, "state": "merged"}
    deployed = {**DEPLOY, "state": "deployed"}
    assert pickup_state(_blocked(blocked_on_refs=[merged, deployed]), [], NOW) == "blocked"
    # No owner: nothing would resume it once released, it stays an action.
    assert pickup_state(_blocked(owner_agent_id=None, blocked_on_refs=[PR]), [], NOW) == "blocked"


def test_an_item_waiting_for_a_person_waits_for_the_answer():
    """#861: the decision card reopens it; alone or with items and references."""
    assert pickup_state(_blocked(needs_human=True), [], NOW) == "waiting_for_person"
    assert pickup_state(_blocked(needs_human=True, blocked_on=[A], blocked_on_refs=[PR]), [], NOW) == "waiting_for_person"


def test_a_legacy_reason_only_item_stays_blocked():
    """#861: the only blocked shape that needs someone keeps the red state."""
    assert pickup_state(_blocked(blocked_reason="waits for the partner"), [], NOW) == "blocked"
