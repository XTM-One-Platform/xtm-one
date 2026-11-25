"""An inbox run's end completes its item only when nothing is left pending.

The run finalizer used to mark every dispatched item ``done`` on a successful
run. An item whose run opened a pull request (still open, CI pending) or left
it ``blocked`` was closed before its deliverable landed, so the CI completion
and its blockers had nothing to wake. ``settle_inbox_work_item``
keeps what the run decided and parks an item that still waits on a pull
request.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services.company_managed_writes import agent_writer

AGENT = agent_writer(is_company_managed=False)


def _item(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        workspace_id=None,
        owner_agent_id=uuid.uuid4(),
        created_by_agent_id=None,
        title="Fix the thing",
        description="brief",
        payload={},
        attachment_file_ids=None,
        status="claimed",
        priority="high",
        correlation_key="dev:thing",
        due_at=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=uuid.uuid4(),
        claim_notes=None,
        claimed_at=now - timedelta(minutes=5),
        source="agent",
        result=None,
        action_mode=None,
        blocked_on=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Result:
    def __init__(self, one):
        self._one = one

    def scalar_one_or_none(self):
        return self._one


class _Session:
    def __init__(self, item):
        self._item = item
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Result(self._item)

    async def commit(self):
        self.committed = True


@pytest.fixture
def settle(monkeypatch):
    published: list = []

    async def _tiers(db, user_id):
        return [], []

    async def _gate(db, writer, item, action):
        return object()

    async def _publish(events):
        published.extend(events)

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_gate_write", _gate)
    monkeypatch.setattr(wi_service, "_publish", _publish)
    monkeypatch.setattr(wi_service, "update_events", lambda item, **kw: [("status", item.status)])

    async def _no_live_ci(bindings, user_id):
        return {}

    # No code host by default: a recorded red stands (#10751 reads it live).
    monkeypatch.setattr("app.services.work_item_pull_requests.read_live_ci", _no_live_ci)

    async def _run(item, **kwargs):
        session = _Session(item)
        monkeypatch.setattr("app.core.database.async_session", lambda: session)
        out = await wi_service.settle_inbox_work_item(
            str(item.id), user_id=str(item.user_id), result="Run summary.", writer=AGENT, **kwargs
        )
        return out, session

    _run.published = published
    return _run


def _pr(state="open", **extra):
    return {"repo": "owner/name", "number": 12, "state": state, **extra}


@pytest.mark.asyncio
async def test_nothing_pending_completes_with_the_run_summary(settle):
    item = _item(payload={"pull_requests": [_pr("merged", merge_sha="abc")]})
    out, session = await settle(item)
    assert out == "completed"
    assert item.status == "done" and item.result == "Run summary."
    assert session.committed


@pytest.mark.asyncio
async def test_an_open_pull_request_parks_the_item_blocked_not_done(settle):
    item = _item(status="in_progress", payload={"pull_requests": [_pr(), _pr("merged") | {"number": 9}]})
    out, session = await settle(item)
    assert out == "parked"
    assert item.status == "blocked"
    assert item.result is None
    # The claim is released: the CI completion dispatches it again.
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None and item.claimed_at is None
    assert "owner/name#12 still open" in item.claim_notes[-1]["text"]
    assert "#9" not in item.claim_notes[-1]["text"]
    assert session.committed


@pytest.mark.asyncio
async def test_an_open_red_pull_request_puts_the_item_back_in_the_queue_never_parked(settle):
    red = _pr(head_sha="abc", ci={"head_sha": "abc", "runs": {"ci": {"status": "completed", "conclusion": "failure"}}})
    item = _item(status="in_progress", blocked_on_refs=None, payload={"pull_requests": [red]})
    out, session = await settle(item)
    # Parked on the merge of a red head, nothing would ever wake it (#9418).
    assert out == "released"
    assert item.status == "open" and item.blocked_on_refs is None
    assert item.payload["_dispatch_attempts"] == 1
    assert "owner/name#12 red on its head" in item.claim_notes[-1]["text"]
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None
    assert session.committed


def _red(head="abc"):
    return _pr(head_sha=head, ci={"head_sha": head, "runs": {"ci": {"status": "completed", "conclusion": "failure"}}})


def _live_ci(monkeypatch, state):
    asked: list = []

    async def _read(bindings, user_id):
        asked.append([b["number"] for b in bindings])
        return {("owner/name", 12): state}

    monkeypatch.setattr("app.services.work_item_pull_requests.read_live_ci", _read)
    return asked


@pytest.mark.asyncio
async def test_a_second_release_on_the_same_red_defers_the_item_instead_of_looping(settle):
    item = _item(status="in_progress", blocked_on_refs=None, not_before=None, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    # The first release on a red goes back to the queue after the red hand-off: the
    # head's red event fixes it first, the next inbox run only if nobody did.
    assert out == "released" and item.status == "open"
    assert timedelta(0) < item.not_before - datetime.now(UTC) <= wi_service.RED_HANDOFF_DEFER
    # The next run starts once the hand-off is over.
    item.status, item.not_before = "in_progress", None
    out, _ = await settle(item)
    # Same head, same check runs: a run at once would see the same thing (a held draft
    # at its rerun cap was dispatched five times in four minutes).
    assert out == "released" and item.status == "open"
    assert item.not_before is not None and item.not_before > datetime.now(UTC) + timedelta(minutes=50)
    assert item.payload[wi_service.RED_DEFERRED_UNTIL_KEY] == item.not_before.isoformat()
    assert "same head and same check runs as the previous release" in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_a_release_on_a_new_head_or_a_new_conclusion_is_not_deferred(settle):
    item = _item(status="in_progress", blocked_on_refs=None, not_before=None, payload={"pull_requests": [_red()]})
    await settle(item)
    item.status, item.not_before = "in_progress", None
    item.payload = {**item.payload, "pull_requests": [_red("def")]}
    out, _ = await settle(item)
    assert out == "released" and item.not_before - datetime.now(UTC) <= wi_service.RED_HANDOFF_DEFER
    item.status, item.not_before = "in_progress", None
    rerun = _red("def")
    rerun["ci"]["runs"]["ci"]["run_id"] = 2
    item.payload = {**item.payload, "pull_requests": [rerun]}
    out, _ = await settle(item)
    assert out == "released" and item.not_before - datetime.now(UTC) <= wi_service.RED_HANDOFF_DEFER
    assert wi_service.RED_DEFERRED_UNTIL_KEY not in item.payload


@pytest.mark.asyncio
async def test_a_first_red_release_is_held_back_for_the_heads_red_event(settle):
    # The red-check event and an inbox run both took one red head: the inbox waits
    # for the red event's hand-off; a not_before already past is replaced.
    past = datetime.now(UTC) - timedelta(minutes=5)
    item = _item(status="in_progress", blocked_on_refs=None, not_before=past, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    assert out == "released" and item.status == "open"
    assert timedelta(0) < item.not_before - datetime.now(UTC) <= wi_service.RED_HANDOFF_DEFER
    assert item.claimed_by_run_id is None
    assert "for the head's red event" in item.claim_notes[-1]["text"]


def test_the_ci_completion_clears_only_the_deferral_of_an_unchanged_red():
    at = datetime.now(UTC) + timedelta(hours=1)
    item = _item(not_before=at, payload={wi_service.RED_DEFERRED_UNTIL_KEY: at.isoformat()})
    assert wi_service.clear_red_deferral(item) is True
    assert item.not_before is None and wi_service.RED_DEFERRED_UNTIL_KEY not in item.payload
    # A not_before set since by a run or a person is kept.
    other = at + timedelta(hours=2)
    item = _item(not_before=other, payload={wi_service.RED_DEFERRED_UNTIL_KEY: at.isoformat()})
    assert wi_service.clear_red_deferral(item) is False and item.not_before == other
    item = _item(not_before=other, payload={})
    assert wi_service.clear_red_deferral(item) is False and item.not_before == other


@pytest.mark.asyncio
async def test_a_red_of_a_head_the_pull_request_moved_past_parks_on_its_ci(settle, monkeypatch):
    asked = _live_ci(monkeypatch, {"head_sha": "def", "conclusion": None})
    item = _item(status="in_progress", blocked_on_refs=None, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    # The red was abc's; the head is def with CI pending: never released on it (#10751).
    assert asked == [[12]]
    assert out == "parked"
    assert item.status == "blocked"
    assert "_dispatch_attempts" not in item.payload
    binding = item.payload["pull_requests"][0]
    assert binding["head_sha"] == "def" and binding["ci"] == {"head_sha": "def"}
    texts = [n["text"] for n in item.claim_notes]
    assert any("recorded red is void" in t and "moved from abc to def" in t for t in texts)


@pytest.mark.asyncio
async def test_a_red_whose_failing_check_runs_again_parks_on_its_ci(settle, monkeypatch):
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": None})
    item = _item(status="in_progress", blocked_on_refs=None, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    assert out == "parked"
    assert item.status == "blocked"
    run = item.payload["pull_requests"][0]["ci"]["runs"]["ci"]
    assert run["status"] == "in_progress" and run["conclusion"] is None
    assert any("running again" in n["text"] for n in item.claim_notes)


@pytest.mark.asyncio
async def test_a_red_the_live_read_confirms_is_still_released(settle, monkeypatch):
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": "failure"})
    item = _item(status="in_progress", blocked_on_refs=None, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    assert out == "released"
    assert item.status == "open"
    assert "owner/name#12 red on its head" in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_a_moved_head_already_red_live_is_released_on_the_new_head(settle, monkeypatch):
    _live_ci(monkeypatch, {"head_sha": "def", "conclusion": "failure", "run_url": "u"})
    item = _item(status="in_progress", blocked_on_refs=None, payload={"pull_requests": [_red()]})
    out, _ = await settle(item)
    assert out == "released"
    assert item.payload["pull_requests"][0]["ci"]["head_sha"] == "def"


@pytest.mark.asyncio
async def test_an_open_pull_request_is_read_live_and_parks_while_its_ci_runs(settle, monkeypatch):
    # #11730: every open pull request is read live at the run's end, not only a red one.
    asked = _live_ci(monkeypatch, {"head_sha": "def", "conclusion": None, "draft": False})
    item = _item(status="in_progress", payload={"pull_requests": [_pr()]})
    out, _ = await settle(item)
    assert out == "parked" and asked == [[12]]
    assert item.payload["pull_requests"][0]["head_sha"] == "def"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]


@pytest.mark.asyncio
async def test_a_ready_pull_request_whose_ci_already_completed_is_not_parked_on_it(settle, monkeypatch):
    # #11730 criterion 2: CI finished without a delivery the item saw: no wake is left to come.
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": "success", "draft": False})
    item = _item(status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [_pr()]})
    out, _ = await settle(item)
    assert out == "deferred"
    assert item.status == "open" and item.blocked_on_refs is None
    # Held back for the head's green event, which merges: no second run on that head.
    assert item.not_before is not None
    assert timedelta(0) < item.not_before - datetime.now(UTC) <= wi_service.GREEN_HANDOFF_DEFER
    assert item.claimed_by_run_id is None
    ci = item.payload["pull_requests"][0]["ci"]
    assert ci["head_sha"] == "abc" and any(k.startswith("abc:success:") for k in ci["dispatched"])
    assert "green on its head and no CI completion left to come" in item.claim_notes[-1]["text"]
    assert "after the head's green event" in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_a_green_ci_already_recorded_and_dispatched_parks_as_before(settle, monkeypatch):
    # The dispatch was spent on an earlier run: nothing new, the merge decides.
    green = _pr(
        draft=False,
        head_sha="abc",
        ci={"head_sha": "abc", "runs": {"ci": {"status": "completed", "conclusion": "success"}}},
    )
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": "success", "draft": False})
    item = _item(status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [green]})
    out, _ = await settle(item)
    assert out == "parked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]


@pytest.mark.asyncio
async def test_a_ci_completion_told_to_the_ending_run_is_not_waited_for_again(settle):
    # #11730: the completion was told to the holding run, never dispatched: parking on
    # it would wait for a wake already spent.
    told = _pr(
        draft=False,
        head_sha="abc",
        ci={
            "head_sha": "abc",
            "told_holder": "success",
            "runs": {"ci": {"status": "completed", "conclusion": "success"}},
        },
    )
    item = _item(status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [told]})
    out, _ = await settle(item)
    assert out == "deferred" and item.status == "open"
    assert "told_holder" not in item.payload["pull_requests"][0]["ci"]
    # Acted on once: the next run end on the same head (taken past the green hand-off)
    # parks as before.
    item.status = "in_progress"
    item.not_before = None
    out, _ = await settle(item)
    assert out == "parked"


@pytest.mark.asyncio
async def test_a_pull_request_marked_ready_mid_run_is_seen_at_the_run_end(settle, monkeypatch):
    # #11730 criterion 3: the binding still says draft, GitHub says ready.
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": None, "draft": False})
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=True, head_sha="abc")]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert item.payload["pull_requests"][0]["draft"] is False
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]
    assert item.blocked_reason is None
    assert any("ready for review" in n["text"] for n in item.claim_notes)


@pytest.mark.asyncio
async def test_a_pull_request_turned_back_to_draft_mid_run_is_never_parked_on_its_merge(settle, monkeypatch):
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": None, "draft": True})
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=False, head_sha="abc")]},
    )
    out, _ = await settle(item)
    assert out == "parked" and item.blocked_on_refs is None
    assert "owner/name#12 (draft)" in item.blocked_reason


@pytest.mark.asyncio
async def test_an_open_ux_round_wins_over_a_finished_ci(settle, monkeypatch):
    # #11730 criterion 1: blocked on the UX item, not deferred on the green CI.
    gate = str(uuid.uuid4())

    async def _gates(db, item, bindings):
        return {("owner/name", 12): gate}

    monkeypatch.setattr(wi_service, "_open_ux_gates", _gates)
    _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": "success", "draft": True})
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=True, head_sha="abc")]},
    )
    out, _ = await settle(item)
    assert out == "parked" and item.status == "blocked"
    assert item.blocked_on == [gate] and item.blocked_on_refs is None
    assert "UX round of owner/name#12" in item.blocked_reason


@pytest.mark.asyncio
async def test_a_draft_with_an_open_ux_round_is_blocked_on_the_round(settle, monkeypatch):
    gate = str(uuid.uuid4())
    seen: list = []

    async def _gates(db, item, bindings):
        seen.extend(b["number"] for b in bindings)
        return {("owner/name", 12): gate}

    monkeypatch.setattr(wi_service, "_open_ux_gates", _gates)
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=True)]},
    )
    out, _ = await settle(item)
    assert seen == [12]
    assert out == "parked" and item.blocked_on == [gate]
    assert "UX round of owner/name#12" in item.blocked_reason


@pytest.mark.asyncio
async def test_the_green_check_block_and_the_run_end_block_make_one_wait(settle, monkeypatch):
    # #11730 criterion 4: the green-check run already blocked the item on the round;
    # the run end adds no second entry.
    gate = str(uuid.uuid4())

    async def _gates(db, item, bindings):
        return {("owner/name", 12): gate}

    monkeypatch.setattr(wi_service, "_open_ux_gates", _gates)
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on=[gate],
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=False)]},
    )
    out, _ = await settle(item)
    assert out == "parked" and item.blocked_on == [gate]


@pytest.mark.asyncio
async def test_a_blocked_item_is_never_overridden_by_a_live_read(settle, monkeypatch):
    # #11730 criterion 4: a completion never clears a blocked state.
    asked = _live_ci(monkeypatch, {"head_sha": "abc", "conclusion": "success", "draft": False})
    blocker = str(uuid.uuid4())
    item = _item(status="blocked", blocked_on=[blocker], payload={"pull_requests": [_pr(head_sha="abc")]})
    out, _ = await settle(item)
    assert out == "blocked" and item.status == "blocked" and item.blocked_on == [blocker]
    assert asked == [] and item.result is None


def test_refresh_open_from_live_is_pure_and_skips_closed_or_unread():
    from app.services.work_item_pull_requests import refresh_open_from_live

    payload = {"pull_requests": [_pr(draft=True, head_sha="abc"), _pr("merged") | {"number": 9}]}
    reads = {
        ("owner/name", 12): {"head_sha": "def", "conclusion": "failure", "draft": False},
        ("owner/name", 9): {"head_sha": "zzz", "conclusion": "success", "draft": False},
    }
    out, changed, decided = refresh_open_from_live(payload, reads)
    assert payload["pull_requests"][0]["head_sha"] == "abc"  # never in place
    first, merged = out["pull_requests"]
    assert first["draft"] is False and first["head_sha"] == "def" and first["ci"]["failed_at"]
    assert decided == {("owner/name", 12)}
    assert "head_sha" not in merged or merged["head_sha"] != "zzz"
    assert len(changed) == 3
    assert refresh_open_from_live(payload, {})[1:] == ([], set())


def test_void_stale_reds_is_pure_and_leaves_an_unread_red():
    from app.services.work_item_pull_requests import void_stale_reds

    payload = {"pull_requests": [_red()]}
    out, voided = void_stale_reds(payload, {})
    assert voided == [] and out["pull_requests"][0]["ci"]["runs"]["ci"]["conclusion"] == "failure"
    out, voided = void_stale_reds(payload, {("owner/name", 12): {"head_sha": "def", "conclusion": None}})
    assert len(voided) == 1
    # The caller's payload is never changed in place.
    assert payload["pull_requests"][0]["head_sha"] == "abc"


@pytest.mark.asyncio
async def test_an_item_whose_merge_is_not_the_deliverable_is_released_at_once_not_blocked(settle):
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        payload={"pr_merge_not_required": True, "pull_requests": [_pr(head_sha="abc")]},
    )
    out, session = await settle(item, run_id=item.claimed_by_run_id)
    assert out == "released"
    assert item.status == "open"
    assert item.result is None
    # Never blocked on the merge, never deferred on a clock: counted as an attempt.
    assert item.blocked_on_refs is None
    assert item.not_before is None
    assert item.payload["_dispatch_attempts"] == 1
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None and item.claimed_at is None
    assert "owner/name#12 still open" in item.claim_notes[-1]["text"]
    assert "pr_merge_not_required" in item.claim_notes[-1]["text"]
    assert session.committed


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [False, "true", None])
async def test_without_the_opt_out_the_item_is_parked_on_its_pull_requests(settle, flag):
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        payload={"pr_merge_not_required": flag, "pull_requests": [_pr()]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert item.status == "blocked" and item.not_before is None
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]


def _green_draft(head="abc"):
    return _pr(
        draft=True,
        head_sha=head,
        ci={"head_sha": head, "runs": {"ci": {"status": "completed", "conclusion": "success"}}},
    )


@pytest.mark.asyncio
async def test_a_draft_whose_ci_completed_is_released_at_once_never_parked(settle):
    # #10324: no merge to wait for and no CI completion left to come. The work left is
    # the item's own: back in the queue at once, no clock, counted as an attempt.
    item = _item(
        status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [_green_draft()]}
    )
    out, _ = await settle(item, run_id=item.claimed_by_run_id)
    assert out == "released"
    assert item.status == "open" and item.blocked_on_refs is None
    assert item.not_before is None
    assert item.payload["_dispatch_attempts"] == 1
    assert item.claimed_by_run_id is None
    assert "draft owner/name#12" in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_a_draft_left_the_same_way_on_an_unchanged_head_ends_blocked_at_the_cap(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 3)
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_green_draft()]},
    )
    runs = []
    for n in range(3):
        item.status = "in_progress"
        item.claimed_by_run_id = uuid.uuid4()
        runs.append(str(item.claimed_by_run_id))
        out, _ = await settle(item, run_id=item.claimed_by_run_id)
        assert out == "released", n
    assert item.status == "blocked"
    for rid in runs:
        assert rid in item.blocked_reason
    assert "work_item.release_exhausted" in [n for n, _ in settle.published]


@pytest.mark.asyncio
async def test_a_new_head_on_the_draft_restarts_the_release_count(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item(
        status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [_green_draft()]}
    )
    out, _ = await settle(item, run_id=item.claimed_by_run_id)
    assert out == "released" and item.payload["_dispatch_attempts"] == 1
    # The next run pushed a new head: the count starts again, the item stays open.
    item.status = "in_progress"
    item.claimed_by_run_id = uuid.uuid4()
    item.payload = {**item.payload, "pull_requests": [_green_draft("def")]}
    out, _ = await settle(item, run_id=item.claimed_by_run_id)
    assert out == "released" and item.status == "open"
    assert item.payload["_dispatch_attempts"] == 1


@pytest.mark.asyncio
async def test_a_ready_pull_request_with_an_open_ux_round_is_blocked_on_the_round_not_its_merge(settle, monkeypatch):
    # #10324: a non-draft UI pull request whose UX round is open waits for the verdict;
    # a changes-requested keeps it open, so a merge ref would never release the item.
    gate = str(uuid.uuid4())
    seen: list = []

    async def _gates(db, item, bindings):
        seen.extend((b["repo"], b["number"]) for b in bindings)
        return {("owner/name", 12): gate}

    monkeypatch.setattr(wi_service, "_open_ux_gates", _gates)
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=False), _pr(draft=False) | {"number": 13}]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert seen == [("owner/name", 12), ("owner/name", 13)]
    assert item.blocked_on == [gate]
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(13, "merged_or_closed")]
    assert "UX round of owner/name#12" in item.blocked_reason and gate in item.blocked_reason


def test_the_ux_round_key_is_the_repository_name_and_the_number():
    assert wi_service.ux_gate_correlation_key("XTM-One-Platform/xtm-one", 10292) == "ux:xtm-one-10292"


@pytest.mark.asyncio
async def test_an_open_draft_pull_request_blocks_with_a_reason_never_on_its_merge(settle):
    # A UI pull request waits for its UX round as a draft: a "changes requested"
    # keeps it open, so a merged_or_closed ref would never release the item (#10321).
    stale = {"repo": "owner/name", "kind": "pull_request", "number": 12, "release_on": "merged_or_closed"}
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=[stale],
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=True)]},
    )
    out, session = await settle(item)
    assert out == "parked"
    assert item.status == "blocked" and item.result is None
    assert item.blocked_on_refs is None
    assert "owner/name#12 (draft)" in item.blocked_reason
    assert (
        "is a draft: blocked until its CI completion or closing releases it by itself" in item.claim_notes[-1]["text"]
    )
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None
    assert session.committed


@pytest.mark.asyncio
async def test_a_ready_pull_request_next_to_a_draft_is_still_parked_on_its_merge(settle):
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=True), _pr(draft=False) | {"number": 13}]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(13, "merged_or_closed")]
    assert "owner/name#12 (draft)" in item.blocked_reason
    note = item.claim_notes[-1]["text"]
    assert "waiting on the merge of owner/name#13" in note and "owner/name#12 is a draft" in note


@pytest.mark.asyncio
async def test_a_ready_pull_request_parks_as_before_with_no_reason_written(settle):
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        blocked_reason=None,
        payload={"pull_requests": [_pr(draft=False)]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]
    assert item.blocked_reason is None
    assert item.claim_notes[-1]["text"].endswith("waiting on the pull request, not done")


@pytest.mark.asyncio
async def test_the_opt_out_with_nothing_open_completes_as_before(settle):
    item = _item(payload={"pr_merge_not_required": True, "pull_requests": [_pr("merged")]})
    out, _ = await settle(item)
    assert out == "completed" and item.status == "done"


@pytest.mark.asyncio
async def test_a_blocked_item_stays_blocked_with_its_claim_released(settle):
    blocker = str(uuid.uuid4())
    item = _item(status="blocked", blocked_on=[blocker])
    out, _ = await settle(item)
    assert out == "blocked"
    assert item.status == "blocked" and item.blocked_on == [blocker]
    assert item.claimed_by_agent_id is None and item.result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_an_item_the_run_settled_itself_is_kept(settle, status):
    item = _item(status=status, result="My own result.", payload={"pull_requests": [_pr()]})
    out, session = await settle(item)
    assert out == "kept"
    assert item.status == status and item.result == "My own result."
    assert not session.committed
    assert settle.published == []


@pytest.mark.asyncio
async def test_a_run_that_may_not_complete_releases_the_item_open(settle):
    item = _item(status="in_progress", claim_notes=[{"at": "t", "text": "started"}])
    # The ending run is the holder itself.
    run_id = item.claimed_by_run_id
    out, session = await settle(
        item, run_id=run_id, may_complete=False, release_reason="the repeated tool failure brake withdrew its tools"
    )
    assert out == "released"
    assert item.status == "open" and item.result is None
    assert item.claimed_by_agent_id is None and item.claimed_by_run_id is None and item.claimed_at is None
    # The progress trail is kept and the release is named.
    assert item.claim_notes[0]["text"] == "started"
    note = item.claim_notes[-1]["text"]
    assert note.startswith(f"released: run {run_id} ended without completing the item")
    assert "repeated tool failure brake" in note
    assert item.payload["_dispatch_attempts"] == 1
    assert item.payload["_released_by_runs"] == [str(run_id)]
    assert session.committed


@pytest.mark.asyncio
async def test_releases_stop_at_the_dispatch_cap_blocked_with_the_runs_named(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 3)
    earlier = [str(uuid.uuid4()), str(uuid.uuid4())]
    item = _item(
        status="in_progress",
        payload={"_dispatch_attempts": 2, "_released_by_runs": earlier},
        blocked_reason=None,
    )
    run_id = item.claimed_by_run_id
    out, _ = await settle(item, run_id=run_id, may_complete=False)
    assert out == "released"
    assert item.status == "blocked"
    assert item.claimed_by_run_id is None
    for rid in [*earlier, str(run_id)]:
        assert rid in item.blocked_reason
    assert "did not call complete_work_item" in item.blocked_reason


@pytest.mark.asyncio
async def test_the_release_cap_with_no_condition_raises_release_exhausted(settle, monkeypatch):
    # Blocked at the cap with nothing that releases it: the owner's fleet hears it (#9540).
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item(status="in_progress", payload={"_dispatch_attempts": 1}, blocked_on_refs=None, blocked_reason=None)
    run_id = item.claimed_by_run_id
    out, _ = await settle(item, run_id=run_id, may_complete=False)
    assert out == "released" and item.status == "blocked"
    assert "No release condition" in item.blocked_reason
    exhausted = [data for name, data in settle.published if name == "work_item.release_exhausted"]
    assert len(exhausted) == 1
    data = exhausted[0]
    assert data["work_item_id"] == str(item.id) and data["status"] == "blocked"
    assert data["dispatch_attempts"] == 2 and data["release_conditions"] == []
    assert data["released_by_runs"] == [str(run_id)]
    assert data["blocked_reason"] == item.blocked_reason


@pytest.mark.asyncio
async def test_the_release_cap_keeps_and_names_the_item_s_own_wait(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 1)
    blocker = str(uuid.uuid4())
    ref = {"repo": "owner/name", "kind": "pull_request", "number": 9443, "release_on": "merged_or_closed"}
    item = _item(status="in_progress", blocked_on=[blocker], blocked_on_refs=[ref], blocked_reason=None)
    out, _ = await settle(item, may_complete=False)
    assert out == "released" and item.status == "blocked"
    # Never stripped: whatever it waited for still releases it.
    assert item.blocked_on == [blocker] and item.blocked_on_refs == [ref]
    assert "pull_request owner/name#9443" in item.blocked_reason
    assert f"work item {blocker[:8]}" in item.blocked_reason
    (data,) = [d for n, d in settle.published if n == "work_item.release_exhausted"]
    assert data["release_conditions"] == ["pull_request owner/name#9443", f"work item {blocker[:8]}"]


@pytest.mark.asyncio
async def test_a_release_under_the_cap_raises_no_release_exhausted(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item(status="in_progress", payload={})
    out, _ = await settle(item, may_complete=False)
    assert out == "released" and item.status == "open"
    assert "work_item.release_exhausted" not in [n for n, _ in settle.published]


@pytest.mark.asyncio
async def test_a_run_that_deferred_the_item_itself_is_not_counted(settle, monkeypatch):
    # #10161: a future not_before set by the run is a deferral, not a failed release.
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    later = datetime.now(UTC) + timedelta(hours=1)
    item = _item(status="in_progress", payload={"_dispatch_attempts": 1}, not_before=later, blocked_reason=None)
    out, _ = await settle(item, run_id=item.claimed_by_run_id, may_complete=False)
    assert out == "deferred"
    assert item.status == "open" and item.not_before == later
    assert item.payload["_dispatch_attempts"] == 1
    assert "not counted toward the release cap" in item.claim_notes[-1]["text"]
    assert "work_item.release_exhausted" not in [n for n, _ in settle.published]


@pytest.mark.asyncio
async def test_a_past_not_before_is_no_deferral_and_still_counts(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item(status="in_progress", payload={}, not_before=datetime.now(UTC) - timedelta(minutes=1))
    out, _ = await settle(item, may_complete=False)
    assert out == "released" and item.payload["_dispatch_attempts"] == 1


@pytest.mark.asyncio
async def test_a_run_that_merged_a_slice_restarts_the_release_count(settle, monkeypatch):
    # #10161: the run merged a pull request bound to the item during its claim: progress.
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item(status="in_progress", payload={"_dispatch_attempts": 1}, blocked_reason=None)
    merged_at = (item.claimed_at + timedelta(minutes=2)).isoformat()
    item.payload["pull_requests"] = [_pr(state="merged", closed_at=merged_at)]
    out, _ = await settle(item, run_id=item.claimed_by_run_id, may_complete=False)
    assert out == "released" and item.status == "open"
    assert "_dispatch_attempts" not in item.payload
    assert "owner/name#12" in item.claim_notes[-1]["text"]
    assert "work_item.release_exhausted" not in [n for n, _ in settle.published]


@pytest.mark.asyncio
async def test_a_merge_before_the_claim_is_no_progress_of_this_run(settle, monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 5)
    item = _item(status="in_progress", payload={"_dispatch_attempts": 1})
    merged_at = (item.claimed_at - timedelta(hours=1)).isoformat()
    item.payload["pull_requests"] = [_pr(state="merged", closed_at=merged_at)]
    out, _ = await settle(item, may_complete=False)
    assert out == "released" and item.payload["_dispatch_attempts"] == 2


@pytest.mark.asyncio
async def test_a_program_item_survives_eight_slice_runs(settle, monkeypatch):
    # Acceptance of #10161: one slice merged per run, never auto-blocked at the cap.
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 3)
    item = _item(status="in_progress", payload={}, blocked_reason=None)
    for n in range(8):
        now = datetime.now(UTC)
        item.status = "in_progress"
        item.claimed_by_run_id = uuid.uuid4()
        item.claimed_at = now - timedelta(minutes=5)
        slices = [b for b in item.payload.get("pull_requests", [])]
        slices.append({"repo": "owner/name", "number": 100 + n, "state": "merged", "closed_at": now.isoformat()})
        item.payload = {**item.payload, "pull_requests": slices}
        out, _ = await settle(item, run_id=item.claimed_by_run_id, may_complete=False)
        assert out == "released" and item.status == "open", n
    assert "work_item.release_exhausted" not in [n for n, _ in settle.published]


def test_release_exhausted_is_a_catalogued_platform_event():
    from app.services import platform_events as pe

    assert "work_item.release_exhausted" in pe.PLATFORM_EVENTS
    assert "blocked_reason" in pe.PLATFORM_EVENTS["work_item.release_exhausted"]["filter_keys"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["done", "cancelled"])
async def test_an_item_the_run_completed_is_kept_even_when_it_may_not_complete(settle, status):
    item = _item(status=status, result="My own result.")
    out, session = await settle(item, may_complete=False)
    assert out == "kept"
    assert item.status == status and item.result == "My own result."
    assert not session.committed


@pytest.mark.asyncio
async def test_an_open_pull_request_still_parks_when_it_may_not_complete(settle):
    item = _item(status="in_progress", payload={"pull_requests": [_pr()]})
    out, _ = await settle(item, may_complete=False)
    assert out == "parked" and item.status == "blocked"


# -- which inbox run ends may complete their item ---------------------------------


def _tool(name):
    return SimpleNamespace(name=name)


def _msg(role, content, wind_down=None):
    from app.llm.base import ChatMessage

    return ChatMessage(role=role, content=content, wind_down=wind_down)


def test_a_clean_run_without_the_completion_tool_may_complete():
    from app.services.assignment_engine import _inbox_release_reason

    end = SimpleNamespace(stop_reason="end_turn")
    assert _inbox_release_reason(end, [_msg("assistant", "Done.")], [_tool("web_fetch")]) is None
    assert _inbox_release_reason(end, [], None) is None


def test_a_run_offered_complete_work_item_that_did_not_call_it_is_released():
    from app.services.assignment_engine import _inbox_release_reason

    end = SimpleNamespace(stop_reason="end_turn")
    reason = _inbox_release_reason(end, [], [_tool("update_work_item"), _tool("complete_work_item")])
    assert reason == "it did not call complete_work_item"


@pytest.mark.parametrize(
    "messages, stop, expected",
    [
        ([_msg("system", "brake", "tool_failure_brake")], "end_turn", "tool_failure_brake"),
        ([_msg("system", "brake", "rejected_work_brake")], "end_turn", "rejected_work_brake"),
        ([_msg("system", "last call", "iteration_backstop")], "end_turn", "iteration_backstop"),
        ([_msg("system", "budget", "budget_stop")], "end_turn", "budget_stop"),
        ([], "budget_exhausted", "budget_stop"),
        ([], "timeout", "timeout"),
        ([], "waiting_on_tasks", "waiting_on_tasks"),
    ],
    ids=["failure-brake", "rejected-brake", "iteration-backstop", "budget-notice", "budget-stop", "timeout", "waiting"],
)
def test_a_wound_down_run_never_completes_its_item(messages, stop, expected):
    from app.services.assignment_engine import _inbox_release_reason

    # Even an agent with no completion tool: the wind-down wins.
    reason = _inbox_release_reason(SimpleNamespace(stop_reason=stop), messages, [])
    assert reason == f"the run was wound down: {expected}"


def test_only_the_structured_marker_counts_never_the_text():
    from app.tools.agent_loop import _TOOL_FAILURE_BRAKE_NUDGE, turn_wound_down

    end = SimpleNamespace(stop_reason="end_turn")
    # The brake's text without the marker (quoted by a user, or by the model) is no wind-down.
    quoted = _TOOL_FAILURE_BRAKE_NUDGE.format(tool_name="x", count=5)
    assert turn_wound_down(end, [_msg("user", quoted), _msg("system", quoted)]) is None
    # A marker on a non-system message is ignored.
    assert turn_wound_down(end, [_msg("user", "x", "tool_failure_brake")]) is None


# -- the pull requests are read live before a park (#9507) ------------------------


def _closing(number, merged=True, sha="m3rge"):
    from app.integrations.pull_request_events import PullRequestEvent

    return PullRequestEvent(
        kind="closed", repo="owner/name", pr_numbers=(number,), merged=merged, merge_sha=sha if merged else None
    )


@pytest.fixture
def live(monkeypatch):
    """A stub code host: the closings it answers, and the pull requests it was asked for."""
    asked: list[list[int]] = []
    answers: list = []

    async def _read(bindings, user_id):
        asked.append([b["number"] for b in bindings])
        wanted = {b["number"] for b in bindings}
        return [e for e in answers if e.pr_numbers[0] in wanted]

    monkeypatch.setattr("app.services.work_item_pull_requests.read_pull_request_closings", _read)
    return SimpleNamespace(asked=asked, answers=answers)


@pytest.mark.asyncio
async def test_a_pull_request_merged_since_it_was_bound_is_never_parked_on(settle, live):
    live.answers.append(_closing(9))
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=None,
        payload={"pull_requests": [_pr(), _pr() | {"number": 9}]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    # Both open bindings were read, once each.
    assert live.asked == [[12, 9]]
    assert [r["number"] for r in item.blocked_on_refs] == [12]
    merged = next(b for b in item.payload["pull_requests"] if b["number"] == 9)
    assert merged["state"] == "merged" and merged["merge_sha"] == "m3rge"
    assert any("owner/name#9 no longer open" in n["text"] for n in item.claim_notes)
    assert "#9" not in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_every_pull_request_merged_completes_instead_of_parking(settle, live):
    live.answers.append(_closing(12))
    item = _item(status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [_pr()]})
    out, _ = await settle(item)
    assert out == "completed" and item.status == "done"
    assert item.payload["pull_requests"][0]["state"] == "merged"


@pytest.mark.asyncio
async def test_a_park_drops_the_refs_already_satisfied_or_known_merged(settle, live):
    live.answers.append(_closing(9))
    stale = [
        {"repo": "owner/name", "kind": "pull_request", "number": 9, "release_on": "merged_or_closed"},
        {
            "repo": "owner/name",
            "kind": "pull_request",
            "number": 7,
            "release_on": "merged_or_closed",
            "state": "merged",
        },
    ]
    item = _item(
        status="in_progress",
        not_before=None,
        blocked_on_refs=stale,
        payload={"pull_requests": [_pr(), _pr() | {"number": 9}]},
    )
    out, _ = await settle(item)
    assert out == "parked"
    assert [r["number"] for r in item.blocked_on_refs] == [12]


@pytest.mark.asyncio
async def test_an_item_the_run_blocked_on_its_ux_item_keeps_that_wait_only(settle, live):
    item = _item(
        status="blocked",
        blocked_on=[str(uuid.uuid4())],
        blocked_on_refs=None,
        payload={"pull_requests": [_pr()]},
    )
    out, _ = await settle(item)
    assert out == "blocked"
    assert item.blocked_on_refs is None


@pytest.mark.asyncio
async def test_an_item_the_run_deferred_itself_keeps_its_not_before(settle, live):
    later = datetime.now(UTC) + timedelta(minutes=30)
    item = _item(status="in_progress", not_before=later, blocked_on_refs=None, payload={"pull_requests": [_pr()]})
    out, _ = await settle(item)
    assert out == "deferred"
    assert item.status == "open" and item.not_before == later
    assert item.blocked_on_refs is None
    assert item.claimed_by_run_id is None
    assert "deferred the item itself" in item.claim_notes[-1]["text"]


@pytest.mark.asyncio
async def test_an_unreadable_code_host_parks_as_before(settle, monkeypatch):
    async def _boom(bindings, user_id):
        raise RuntimeError("no access")

    monkeypatch.setattr("app.services.work_item_pull_requests.read_pull_request_closings", _boom)
    item = _item(status="in_progress", not_before=None, blocked_on_refs=None, payload={"pull_requests": [_pr()]})
    out, _ = await settle(item)
    assert out == "parked" and [r["number"] for r in item.blocked_on_refs] == [12]


@pytest.mark.asyncio
async def test_read_pull_request_closings_reads_each_pull_request_once(monkeypatch):
    from app.services import work_item_pull_requests as wipr

    calls: list = []

    async def _reader(repo, kind, number, token):
        calls.append((repo, kind, number, token))
        return _closing(number) if number == 9 else None

    async def _token(db, provider, user_id):
        return "tok"

    class _Db:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr("app.integrations.reference_state.reference_state_readers", lambda: {"github": _reader})
    monkeypatch.setattr("app.services.work_item_blocked_refs.reference_token", _token)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Db())
    bindings = [_pr(), _pr() | {"number": 9}, _pr()]
    out = await wipr.read_pull_request_closings(bindings, uuid.uuid4())
    assert [e.pr_numbers for e in out] == [(9,)]
    assert calls == [("owner/name", "pull_request", 12, "tok"), ("owner/name", "pull_request", 9, "tok")]


def test_open_pull_requests_reads_only_open_bindings():
    item = _item(
        payload={
            "pull_requests": [
                _pr(),
                {"repo": "owner/name", "number": 13, "state": "closed"},
                {"repo": "owner/name", "number": 14},  # no state yet: open
                {"repo": "not-a-repo", "number": 15},  # not a binding
            ]
        }
    )
    assert [b["number"] for b in wi_service.open_pull_requests(item)] == [12, 14]
