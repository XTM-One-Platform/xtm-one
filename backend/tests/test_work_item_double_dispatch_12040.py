"""One run per item, never two, while another run holds it (#12040).

Three paths dispatched a second run on an item another run was finishing:

1. a per-head green claim ``dev:green-claim:<owner/name>@<head sha>`` held by a
   live merging run was not read as a lease on the pull request, so the inbox
   pass and the CI-completion dispatch started a run that stood down;
2. the inbox run's end put a ready, green item back in the queue while the
   ``[merge] full_ci_workflows`` run the merge waits for was still running;
3. two review deliveries 1 s apart on one head started two runs: the review
   listener keys its deliveries per head with ``trigger_config.dedup``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.services import event_run_dedup
from app.services import shared_state_pull_requests as leases_mod
from app.services.company_managed_writes import agent_writer

REPO = "XTM-One-Platform/xtm-one"
SHA = "64e5994ca07d830521e2e7e76279c0e2856d732b"
CLAIM = f"dev:green-claim:{REPO}@{SHA}"

# -- 1. a live green claim is a lease on its pull request ------------------------------


def test_a_green_claim_key_names_its_repository() -> None:
    assert leases_mod.repository_in_key(CLAIM) == REPO
    assert leases_mod.repository_in_key(f"dev:green-claim:{REPO}@{SHA[:12]}") == REPO
    # Not a sha after the @: no repository read from it.
    assert leases_mod.repository_in_key(f"dev:green-claim:{REPO}@main") is None
    # The scoped branch lease is read as before.
    assert leases_mod.repository_in_key(f"dev:branch-lock:{REPO}:fix/12-x") == REPO


def test_a_green_claim_names_its_pull_request_and_no_other() -> None:
    kw = {"number": 12005, "head_branch": "feat/x"}
    assert leases_mod.entry_names_pull_request(CLAIM, {"pr": 12005, "holder": "run"}, repo=REPO, **kw)
    # Same number in another repository: never.
    assert not leases_mod.entry_names_pull_request(CLAIM, {"pr": 12005}, repo="OpenAEV-Platform/openaev", **kw)
    # Another pull request of the same repository: never.
    assert not leases_mod.entry_names_pull_request(CLAIM, {"pr": 12005}, repo=REPO, number=12006, head_branch="x")


@pytest.mark.asyncio
async def test_no_dispatch_while_a_live_run_holds_the_green_claim(monkeypatch) -> None:
    holder = uuid.uuid4()
    state = {"alive": {holder}}

    async def held_rows(user_id):
        return [(CLAIM, {"pr": 12005, "holder": str(holder), "purpose": "merge at green"}, holder)]

    async def alive(run_ids):
        return {r for r in run_ids if r in state["alive"]}

    monkeypatch.setattr(leases_mod, "_held_rows", held_rows)
    monkeypatch.setattr(leases_mod, "_alive", alive)
    payload = {"pull_requests": [{"repo": REPO, "number": 12005, "head_branch": "feat/x", "state": "open"}]}
    lease = await leases_mod.live_pull_request_lease(payload, user_id=None)
    assert lease is not None and lease["key"] == CLAIM and lease["run_id"] == str(holder)
    assert lease["repo"].lower() == REPO.lower() and lease["number"] == 12005
    # The holder ended: the next pass dispatches.
    state["alive"] = set()
    assert await leases_mod.live_pull_request_lease(payload, user_id=None) is None


# -- 2. a running full CI run is a wake still to come -----------------------------------


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
        payload={"pull_requests": [{"repo": "owner/name", "number": 12, "state": "open"}]},
        attachment_file_ids=None,
        status="in_progress",
        priority="high",
        correlation_key="dev:thing",
        due_at=None,
        not_before=None,
        blocked_on_refs=None,
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

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, _statement):
        return _Result(self._item)

    async def commit(self):
        return None


@pytest.fixture
def settle(monkeypatch):
    async def _tiers(db, user_id):
        return [], []

    async def _gate(db, writer, item, action):
        return object()

    async def _publish(events):
        return None

    monkeypatch.setattr(wi_service, "_workspace_tiers", _tiers)
    monkeypatch.setattr(wi_service, "_gate_write", _gate)
    monkeypatch.setattr(wi_service, "_publish", _publish)
    monkeypatch.setattr(wi_service, "update_events", lambda item, **kw: [("status", item.status)])

    def _live(state):
        async def _read(bindings, user_id):
            return {("owner/name", 12): state}

        monkeypatch.setattr("app.services.work_item_pull_requests.read_live_ci", _read)

    async def _run(item):
        monkeypatch.setattr("app.core.database.async_session", lambda: _Session(item))
        out = await wi_service.settle_inbox_work_item(
            str(item.id),
            user_id=str(item.user_id),
            result="Run summary.",
            writer=agent_writer(is_company_managed=False),
        )
        return out

    _run.live = _live
    return _run


@pytest.mark.asyncio
async def test_a_green_head_whose_full_ci_run_is_running_is_parked_not_requeued(settle) -> None:
    settle.live({"head_sha": "abc", "conclusion": "success", "draft": False, "full_ci": "running"})
    item = _item()
    out = await settle(item)
    assert out == "parked"
    assert item.status == "blocked"
    assert [(r["number"], r["release_on"]) for r in item.blocked_on_refs] == [(12, "merged_or_closed")]
    assert not any("back in the queue" in n["text"] for n in item.claim_notes)


@pytest.mark.asyncio
async def test_a_green_head_whose_full_ci_run_is_absent_or_green_goes_back_to_the_queue(settle) -> None:
    for full_ci in ("absent", "green", None):
        state = {"head_sha": "abc", "conclusion": "success", "draft": False}
        if full_ci:
            state["full_ci"] = full_ci
        settle.live(state)
        item = _item()
        out = await settle(item)
        # No full run will come by itself (absent: the next merge dispatches it): the next step is the item's.
        assert out == "deferred", full_ci
        assert item.status == "open"


def test_full_ci_running_reads_only_a_running_state() -> None:
    assert wi_service._full_ci_running({"full_ci": "running"})
    for read in ({"full_ci": "green"}, {"full_ci": "absent"}, {}, None, "running"):
        assert not wi_service._full_ci_running(read)


# -- 3. twin review deliveries on one head start one run --------------------------------

REVIEW_DEDUP = {
    "key": "{{payload.repository.full_name}}#{{payload.pull_request.number}}@{{payload.pull_request.head.sha}}",
    "window_seconds": 60,
}


def _review(review_id: int, sha: str = SHA) -> dict:
    return {
        "action": "submitted",
        "repository": {"full_name": "OpenCTI-Platform/opencti"},
        "pull_request": {"number": 19949, "head": {"sha": sha}},
        "review": {"id": review_id},
    }


def test_two_reviews_of_one_head_render_one_key_and_a_new_head_another() -> None:
    config = {"provider": "github", "event": "pull_request_review", "dedup": REVIEW_DEDUP}
    assert event_run_dedup.dedup_config_error(config) is None
    context = {"type": "integration_event", "provider": "github", "event": "pull_request_review"}
    first = event_run_dedup.run_dedup_key(context, _review(1), trigger_config=config, assignment_id="a")
    twin = event_run_dedup.run_dedup_key(context, _review(2), trigger_config=config, assignment_id="a")
    pushed = event_run_dedup.run_dedup_key(context, _review(3, sha="f" * 40), trigger_config=config, assignment_id="a")
    assert first and first == twin
    assert pushed and pushed != first
    assert event_run_dedup.dedup_window_seconds(config, 600, first) == 60
