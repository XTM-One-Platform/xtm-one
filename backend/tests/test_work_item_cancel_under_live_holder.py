"""A cancel under a live holder run reaches that run (#11554).

An item held by a live run could be cancelled by another writer with nothing
telling the holder: it kept coding, pushed and opened a pull request named on
the cancelled item 13 minutes later. Now the cancel (or a completion by a
person) records a ``finished_under_holder`` claim note, offers a stop steer to
the holder's note session, and the holder's next work-item read-back and
``create_github_pr(work_item_id=...)`` say it; the pull request is refused
before any write. An item nobody cancelled is unaffected.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

import app.services.work_item as wi_service
from app.integrations.providers.github import build_tools
from app.services.company_managed_writes import agent_writer, person_writer
from app.services.work_item_preflight import finished_under_holder_text, work_item_write_refusal

CANCELLER = uuid.uuid4()
AGENT = agent_writer(is_company_managed=False, agent_id=CANCELLER)
PERSON = person_writer(is_admin=False)


def _item(**overrides):
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        is_company_managed=False,
        workspace_id=None,
        owner_agent_id=uuid.uuid4(),
        created_by_agent_id=None,
        title="Remove the workarounds",
        description="brief",
        payload={},
        attachment_file_ids=None,
        status="in_progress",
        priority="medium",
        correlation_key="dev:thing",
        due_at=None,
        not_before=None,
        claimed_by_agent_id=uuid.uuid4(),
        claimed_by_run_id=uuid.uuid4(),
        claim_notes=None,
        claimed_at=now - timedelta(minutes=5),
        source="agent",
        result=None,
        action_mode=None,
        blocked_on=None,
        blocked_on_refs=None,
        blocked_reason=None,
        needs_human=None,
        ask=None,
        fleet_impact=None,
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
def world(monkeypatch):
    """The store around *item*, its holder reported *holder_status*; the offers it makes are kept."""

    def _make(item, holder_status):
        session = _Session(item)
        offers: list[dict] = []
        monkeypatch.setattr("app.core.database.async_session", lambda: session)

        async def _tiers(db, user_id):
            return [], []

        async def _gate(db, writer, it, action):
            return object()

        async def _holders(db, run_ids):
            return {rid: wi_service.HoldingRun(holder_status, None, "run") for rid in run_ids}

        async def _none(*_a, **_kw):
            return None

        async def _passthrough(work_item_id, user_id=None, **_kw):
            return {
                "id": str(item.id),
                "title": item.title,
                "status": item.status,
                "claim_notes": list(item.claim_notes or []),
            }

        async def _offer(item_id, holder_run_id, note, *, title=None, writer_run_ids=None, any_item=False):
            offers.append({"item": str(item_id), "holder": str(holder_run_id), "note": note, "title": title})
            return True

        monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
        monkeypatch.setattr(wi_service, "_gate_write", _gate)
        monkeypatch.setattr(wi_service, "holding_runs", _holders)
        monkeypatch.setattr(wi_service, "_publish", _none)
        monkeypatch.setattr(wi_service, "get_work_item", _passthrough)
        monkeypatch.setattr(wi_service, "update_events", lambda it, **kw: [])
        monkeypatch.setattr(wi_service, "_record_roi_on_done", _none)
        monkeypatch.setattr("app.services.work_item_fleet_impact.note_impact_routed", _none)
        monkeypatch.setattr("app.services.work_item_note_steering.offer", _offer)
        # A blocked write names a real blocker (#762); its wait-cycle read is not under test here.
        monkeypatch.setattr("app.services.work_item_wait_cycles.refuse_wait_cycle", _none)
        return session, offers

    return _make


# -- The cancel is recorded and offered to the live holder -------------------------


@pytest.mark.asyncio
async def test_a_person_cancelling_a_held_item_tells_the_live_holder(world):
    item = _item()
    holder = item.claimed_by_run_id
    _session, offers = world(item, "running")

    await wi_service.complete_work_item(str(item.id), user_id=str(item.user_id), status="cancelled", writer=PERSON)

    assert item.status == "cancelled"
    note = item.claim_notes[-1]
    assert note["kind"] == wi_service.FINISHED_UNDER_HOLDER
    assert note["status"] == "cancelled" and note["by"] == "a person"
    assert note["holder_run_id"] == str(holder)
    assert len(offers) == 1 and offers[0]["holder"] == str(holder) and offers[0]["item"] == str(item.id)
    assert offers[0]["note"].startswith("STOP: item cancelled by a person at ")
    assert "no push, no pull request" in offers[0]["note"]


@pytest.mark.asyncio
async def test_another_agent_cancelling_through_update_names_itself(world):
    item = _item()
    holder = item.claimed_by_run_id
    _session, offers = world(item, "running")

    await wi_service.update_work_item(
        str(item.id), user_id=str(item.user_id), status="cancelled", writer=AGENT, note="STOP: Sam's order"
    )

    note = item.claim_notes[-1]
    assert note["by"] == f"agent {CANCELLER}" and note["holder_run_id"] == str(holder)
    stops = [o for o in offers if o["note"].startswith("STOP: item cancelled by agent")]
    assert len(stops) == 1 and stops[0]["holder"] == str(holder)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder_status", "status", "own"),
    [("completed", "cancelled", False), ("running", "cancelled", True), ("running", "in_progress", False)],
    ids=["holder-ended", "holder-cancels-itself", "not-finished"],
)
async def test_nothing_is_recorded_without_a_live_foreign_holder(world, holder_status, status, own):
    item = _item()
    _session, offers = world(item, holder_status)
    runs = (item.claimed_by_run_id,) if own else None

    if status in ("done", "cancelled"):
        await wi_service.complete_work_item(
            str(item.id), user_id=str(item.user_id), status=status, writer=PERSON, writer_run_ids=runs
        )
    else:
        await wi_service.update_work_item(
            str(item.id), user_id=str(item.user_id), status=status, writer=PERSON, writer_run_ids=runs
        )

    assert not item.claim_notes
    assert offers == []


# -- A reset under a live holder is told too (#11711) -------------------------------


@pytest.mark.asyncio
async def test_a_person_putting_a_held_item_back_to_open_tells_the_live_holder(world):
    item = _item()
    holder = item.claimed_by_run_id
    _session, offers = world(item, "running")

    await wi_service.update_work_item(str(item.id), user_id=str(item.user_id), status="open", writer=PERSON)

    assert item.status == "open" and item.claimed_by_run_id is None
    note = item.claim_notes[-1]
    assert note["kind"] == wi_service.FINISHED_UNDER_HOLDER
    assert note["status"] == "reset to open" and note["holder_run_id"] == str(holder)
    assert len(offers) == 1 and offers[0]["holder"] == str(holder)
    assert offers[0]["note"].startswith("STOP: item reset to open by a person at ")


@pytest.mark.parametrize(
    ("status", "current", "blockers", "refs", "expected"),
    [
        ("open", "in_progress", None, None, "open"),
        ("open", "open", None, None, None),
        (None, "in_progress", ["x"], None, "blocked"),
        (None, "claimed", None, [{"repo": "a/b"}], "blocked"),
        ("in_progress", "claimed", None, None, None),
        ("done", "in_progress", None, None, None),
        (None, "in_progress", [], [], None),
        ("blocked", "in_progress", None, None, "blocked"),
        ("blocked", "blocked", None, None, None),
        (None, "blocked", ["x"], None, None),
    ],
)
def test_claim_reset_by(status, current, blockers, refs, expected):
    assert wi_service.claim_reset_by(status, current, blockers=blockers, refs=refs) == expected


# -- Another run is refused, unless it takes the item over (#11740) -----------------

RETEST_RUN = uuid.uuid4()


def _write(item, status, *, take_over=False, runs=(RETEST_RUN,)):
    if status in ("done", "cancelled"):
        return wi_service.complete_work_item(
            str(item.id),
            user_id=str(item.user_id),
            status=status,
            writer=AGENT,
            writer_run_ids=runs,
            take_over=take_over,
        )
    return wi_service.update_work_item(
        str(item.id),
        user_id=str(item.user_id),
        status=status,
        writer=AGENT,
        writer_run_ids=runs,
        blocked_on_refs=[{"repo": "o/r", "kind": "pull_request", "number": 7}] if status == "blocked" else None,
        take_over=take_over,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "open", "done", "cancelled"])
async def test_another_run_cannot_take_an_item_a_live_run_holds(world, status):
    """The #11729 retest: run B parks, requeues or finishes the item run H holds."""
    item = _item(status="claimed")
    holder = item.claimed_by_run_id
    session, offers = world(item, "running")

    with pytest.raises(ValueError, match=f"held by run {holder}, still running: moving it to {status}"):
        await _write(item, status)

    assert item.status == "claimed" and item.claimed_by_run_id == holder
    assert not item.claim_notes and offers == [] and not session.committed


@pytest.mark.asyncio
async def test_the_refusal_says_how_to_take_it_over(world):
    item = _item()
    world(item, "running")

    with pytest.raises(ValueError) as refused:
        await _write(item, "open")
    assert "Add a note instead, or pass take_over=true" in str(refused.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "open", "done", "cancelled"])
async def test_taking_over_is_recorded_clears_the_hold_and_tells_the_holder(world, status):
    item = _item(status="claimed")
    holder = item.claimed_by_run_id
    _session, offers = world(item, "running")

    await _write(item, status, take_over=True)

    assert item.status == status
    if status in ("open", "blocked"):
        assert item.claimed_by_run_id is None and item.claimed_by_agent_id is None
    note = item.claim_notes[-1]
    landed = status if status in ("done", "cancelled") else f"reset to {status}"
    assert note["kind"] == wi_service.FINISHED_UNDER_HOLDER
    assert note["status"] == landed and note["holder_run_id"] == str(holder)
    assert note["by"] == f"agent {CANCELLER}"
    stops = [o for o in offers if o["note"].startswith(f"STOP: item {landed} by agent {CANCELLER}")]
    assert len(stops) == 1 and stops[0]["holder"] == str(holder)


@pytest.mark.asyncio
async def test_a_blocked_item_its_live_run_parked_is_still_held(world):
    """The retest's second step: H parked it, B's requeue no longer slips through."""
    item = _item(status="blocked", blocked_reason="waits for CI")
    holder = item.claimed_by_run_id
    world(item, "running")

    with pytest.raises(ValueError, match=f"held by run {holder}"):
        await _write(item, "open")
    assert item.status == "blocked" and item.claimed_by_run_id == holder


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder_status", "runs"),
    [("running", "own"), ("completed", "other"), ("running", None)],
    ids=["its-own-run", "holder-ended", "a-person"],
)
async def test_the_holder_an_ended_holder_or_a_person_needs_no_take_over(world, holder_status, runs):
    item = _item(status="in_progress")
    world(item, holder_status)
    writer_runs = {"own": (item.claimed_by_run_id,), "other": (RETEST_RUN,), None: None}[runs]

    await _write(item, "blocked", runs=writer_runs)

    assert item.status == "blocked"


@pytest.mark.asyncio
async def test_a_note_or_a_priority_from_another_run_still_goes_through(world):
    item = _item()
    holder = item.claimed_by_run_id
    world(item, "running")

    await wi_service.update_work_item(
        str(item.id),
        user_id=str(item.user_id),
        priority="high",
        note="retest observed",
        writer=AGENT,
        writer_run_ids=(RETEST_RUN,),
    )
    assert item.status == "in_progress" and item.claimed_by_run_id == holder and item.priority == "high"


@pytest.mark.parametrize(
    ("status", "current", "blockers", "expected"),
    [
        ("done", "in_progress", None, "done"),
        ("cancelled", "blocked", None, "cancelled"),
        ("open", "blocked", None, "open"),
        ("blocked", "claimed", None, "blocked"),
        (None, "in_progress", ["x"], "blocked"),
        ("in_progress", "claimed", None, None),
        (None, "in_progress", None, None),
        ("open", "done", None, None),
    ],
)
def test_holder_dropping_write(status, current, blockers, expected):
    assert wi_service.holder_dropping_write(status, current, blockers=blockers) == expected


# -- The holder's next tool results say stop ---------------------------------------


def _cancelled_view(holder: str) -> dict:
    note = wi_service.finished_under_holder_note(
        "cancelled", AGENT, uuid.UUID(holder), datetime(2026, 11, 12, 5, 21, 25, tzinfo=UTC)
    )
    return {
        "id": str(uuid.uuid4()),
        "status": "cancelled",
        "completed_at": "2026-11-12T05:21:25Z",
        "claim_notes": [note],
    }


def test_the_holder_reads_the_stop_and_another_run_does_not():
    holder = str(uuid.uuid4())
    view = _cancelled_view(holder)

    stop = finished_under_holder_text(view, [holder])
    assert stop == (
        f"STOP: item cancelled by agent {CANCELLER} at 2026-11-12T05:21:25Z while run {holder} held it. "
        "Write nothing more for it (no push, no pull request) and end the run."
    )
    assert finished_under_holder_text(view, [str(uuid.uuid4())]) is None
    assert finished_under_holder_text({"status": "cancelled", "claim_notes": [{"text": "released"}]}) is None


def test_the_get_work_item_answer_of_the_holder_says_stop():
    from app.services.builtin_tools.work_items import _full_item_text

    holder = str(uuid.uuid4())
    view = {
        **_cancelled_view(holder),
        "title": "Remove the workarounds",
        "priority": "high",
        "description": "brief",
        "updated_at": "2026-11-12T05:35:00Z",
    }
    text = _full_item_text(view, [], 0, False, own_run_ids=(holder,))
    assert "ALREADY CANCELLED" in text
    assert f"STOP: item cancelled by agent {CANCELLER}" in text
    other = _full_item_text(view, [], 0, False, own_run_ids=(str(uuid.uuid4()),))
    assert "STOP:" not in other


def test_only_a_cancelled_item_refuses_a_write():
    assert work_item_write_refusal(None) is None
    assert work_item_write_refusal({"id": "x", "status": "in_progress"}) is None
    assert work_item_write_refusal({"id": "x", "status": "done"}) is None
    plain = work_item_write_refusal({"id": "x", "status": "cancelled", "completed_at": "T"})
    assert plain == "work item `x` is cancelled (at T). Write nothing more for it and end the run."
    holder = str(uuid.uuid4())
    assert "STOP: item cancelled by agent" in work_item_write_refusal(_cancelled_view(holder))


# -- create_github_pr refuses on a cancelled item ----------------------------------


class _GitHub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append((method, url))
        if url.endswith("/contents/.xtm-one/checks.toml"):
            return None, "Error: GitHub API error (404): Not Found"
        if method == "post" and url.endswith("/pulls"):
            pr = {"number": 12, "html_url": "https://github.com/acme/app/pull/12", "head": {"ref": "f", "sha": "abc"}}
            return httpx.Response(201, json=pr), None
        return None, "Error: GitHub API error (404): Not Found"


def _tools(status: str):
    recorded: list[str] = []

    async def _record(work_item_id, repo, number, head_branch=None, head_sha=None, url=None, draft=None):
        recorded.append(work_item_id)
        return f"Named on work item {work_item_id}"

    async def _check(work_item_id):
        return work_item_write_refusal({"id": work_item_id, "status": status, "completed_at": "T"})

    executor = MagicMock()
    executor.register = MagicMock()
    build_tools("fake-token", executor, record_pull_request=_record, check_work_item=_check)
    return {c[0][0]: c[0][1] for c in executor.register.call_args_list}, recorded


@pytest.mark.asyncio
async def test_create_github_pr_on_a_cancelled_item_is_refused_before_any_write():
    fake = _GitHub()
    tools, recorded = _tools("cancelled")
    with patch("app.integrations.providers.github._common.safe_request", new=fake):
        result = await tools["create_github_pr"](repo="acme/app", title="t", head="f", base="main", work_item_id="w1")
    assert result.startswith("Error: no pull request opened, nothing was written: work item `w1` is cancelled")
    assert fake.calls == [] and recorded == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["in_progress", "done"])
async def test_create_github_pr_on_an_uncancelled_item_goes_ahead(status):
    fake = _GitHub()
    tools, recorded = _tools(status)
    with patch("app.integrations.providers.github._common.safe_request", new=fake):
        result = await tools["create_github_pr"](repo="acme/app", title="t", head="f", base="main", work_item_id="w1")
    assert result.startswith("PR #12 created: https://github.com/acme/app/pull/12")
    assert recorded == ["w1"]
