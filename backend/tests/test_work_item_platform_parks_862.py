"""Platform parks are black or white, and references met at the write release at once (#862).

Follow-ups of #767 found by its end-to-end test:

* D2: the inbox release cap, the dispatch cap and the unreachable park leave
  a blocked item waiting for a person's decision on a card the platform
  writes (put it back in the queue, or cancel it; put it back recommended),
  never a reason alone, so fleet health no longer reports what the platform
  produced. A release cap that still has a release condition keeps it, no
  card. A scheduled one-off blocked or missed has its own reschedule and
  cancel actions and is left out of ``blocked_without_blocker``.
* D3: a pull request or issue reference already met at the write is read
  once at the write, like a deployment or a workflow run.
* D4: one refusal message for the ask card, naming the four fields.
* D5: the agent tool says the owner is kept for an empty owner_agent_id.

No database: the PostgreSQL proofs live in
``test_work_item_platform_parks_862_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
from app.integrations import reference_state
from app.integrations.pull_request_events import PullRequestEvent
from app.services import work_item_blocked_refs as refs_mod
from app.services.fleet_health_signals import blocked_without_blocker
from app.services.work_item_ask import person_ask_missing, refuse_incomplete_person_ask
from app.services.work_item_ask_backfill import card_unreadable
from app.services.work_item_blocked_refs import (
    ASK_REQUIRED_MESSAGE,
    blocked_defects,
    names_reference_state,
    refuse_needs_human_without_ask,
)

REPO = "xtm-one-platform/xtm-one"


def _item(payload=None, **extra):
    fields = {
        "payload": payload or {},
        "claim_notes": None,
        "status": "in_progress",
        "blocked_on": None,
        "blocked_on_refs": None,
        "blocked_reason": None,
        "needs_human": None,
        "ask": None,
        "not_before": None,
        "owner_agent_id": uuid.uuid4(),
    }
    fields.update(extra)
    return SimpleNamespace(**fields)


def _black_or_white(item) -> bool:
    return not blocked_without_blocker(
        blocked_on=item.blocked_on,
        blocked_on_refs=item.blocked_on_refs,
        blocked_reason=item.blocked_reason,
        needs_human=item.needs_human,
        owner_agent_id=item.owner_agent_id,
        ask=item.ask,
        payload=item.payload,
    )


# -- D2: the card the platform writes -----------------------------------------------


def test_the_platform_card_is_complete_readable_and_recommends_putting_it_back():
    card = wi_service.platform_park_card("Put this work item back in the queue, or cancel it?", "Line one.\nLine two.")
    assert not person_ask_missing(card)
    assert not card_unreadable(card)
    refuse_needs_human_without_ask(True, card)  # never refused by the #762 rule
    assert [o["key"] for o in card["options"]] == ["requeue", "cancel"]
    assert card["recommendation"] == "requeue"
    # Each card is its own copy: an edit of one never changes the next.
    card["options"][0]["label"] = "changed"
    assert wi_service.PLATFORM_PARK_OPTIONS[0]["label"] == "Put it back in the queue"


def test_the_release_cap_with_nothing_to_wait_for_parks_for_a_person(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    item = _item({"_dispatch_attempts": 1})
    assert wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC))
    assert item.status == "blocked"
    assert item.needs_human is True and item.ask["recommendation"] == "requeue"
    assert "release_exhausted" in item.blocked_reason  # the context stays, the event is unchanged
    assert _black_or_white(item)
    assert not card_unreadable(item.ask)


def test_the_release_cap_that_still_waits_keeps_its_wait_and_writes_no_card(monkeypatch):
    monkeypatch.setattr(wi_service.settings, "work_item_max_dispatch_attempts", 2)
    refs = [{"repo": REPO, "kind": "pull_request", "number": 12, "release_on": "merged"}]
    item = _item({"_dispatch_attempts": 1}, blocked_on_refs=refs)
    assert wi_service._release_unfinished(item, run_id=uuid.uuid4(), reason=None, now=datetime.now(UTC))
    assert item.status == "blocked"
    assert item.needs_human is None and item.ask is None
    assert _black_or_white(item)


def test_park_for_person_is_what_every_platform_park_writes():
    item = _item()
    item.status = "blocked"
    item.blocked_reason = "Auto-blocked after 5 failed dispatch attempt(s): boom"
    assert not _black_or_white(item)  # what #767's end-to-end test found
    wi_service.park_for_person(item, question="Put this work item back in the queue, or cancel it?", context="Why.")
    assert _black_or_white(item)
    assert blocked_defects(owner_agent_id=item.owner_agent_id, needs_human=True, ask=item.ask) == []


@pytest.mark.asyncio
async def test_the_unreachable_park_writes_the_card(monkeypatch):
    run_id = uuid.uuid4()
    agent = uuid.uuid4()
    item = _item(
        id=uuid.uuid4(),
        status="claimed",
        claimed_by_agent_id=agent,
        claimed_by_run_id=run_id,
        claimed_at=datetime.now(UTC),
        last_activity_at=None,
    )

    class _Result:
        def scalar_one_or_none(self):
            return item

    class _Session:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, statement):
            return _Result()

        async def commit(self):
            _Session.committed = True

    monkeypatch.setattr("app.core.database.async_session", lambda: _Session())

    async def _publish(wid, *, previous_status, note=None):
        return None

    monkeypatch.setattr(wi_service, "publish_transition", _publish)
    parked = await wi_service.park_unreachable_work_item(str(item.id), agent_id=agent, run_id=run_id, reason="gone")
    assert parked is True and _Session.committed
    assert item.status == "blocked" and item.needs_human is True
    assert item.ask["options"][1]["key"] == "cancel"
    assert str(run_id) in item.blocked_reason and str(run_id) not in str(item.ask)


def test_a_scheduled_one_off_is_left_to_its_own_actions_by_the_health_checks():
    import inspect

    from app.services import fleet_health_signals
    from app.services.health import agent_checks

    assert "WorkItem.action_mode.is_(None)" in inspect.getsource(fleet_health_signals._work_item_findings)
    assert 'getattr(row, "action_mode", None) is None' in inspect.getsource(agent_checks.blocked_without_blocker_items)


# -- D3: references met at the write ------------------------------------------------


def test_pull_request_and_issue_references_are_checked_at_the_write():
    assert names_reference_state([{"repo": REPO, "kind": "pull_request", "number": 1}])
    assert names_reference_state([{"repo": REPO, "kind": "issue", "number": 1}])
    assert not names_reference_state([{"repo": REPO, "kind": "deployment", "number": 1}])
    assert not names_reference_state(None)


class _DbSession:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        row = self.row

        class _Result:
            def one_or_none(self):
                return row

        return _Result()


@pytest.fixture
def write_check(monkeypatch):
    state: dict = {"reads": [], "applied": [], "answer": None, "row": None}

    async def token(db, provider, user_id):
        return "tok"

    async def reader(repo, kind, number, tok):
        state["reads"].append((repo, kind, number))
        return state["answer"]

    async def apply(item_id, event, now):
        state["applied"].append((item_id, event))
        return "released"

    import app.core.database as database

    monkeypatch.setattr(database, "async_session", lambda: _DbSession(state["row"]))
    monkeypatch.setattr(refs_mod, "reference_token", token)
    monkeypatch.setattr(refs_mod, "_apply_to_item", apply)
    monkeypatch.setattr(reference_state, "_READERS", {"github": reader})
    return state


@pytest.mark.asyncio
async def test_a_merged_pull_request_releases_the_item_at_the_write(write_check):
    item = uuid.uuid4()
    refs = [
        {"repo": REPO, "kind": "pull_request", "number": 767, "release_on": "merged"},
        {"repo": REPO, "kind": "deployment", "number": 767, "release_on": "deployed"},
    ]
    write_check["row"] = (uuid.uuid4(), "blocked", refs)
    write_check["answer"] = PullRequestEvent(kind="closed", repo=REPO, pr_numbers=(767,), merged=True)
    out = await refs_mod.check_item_reference_states(item)
    assert out["read"] == 1 and out["released"] == 1
    assert write_check["reads"] == [(REPO, "pull_request", 767)]  # the deployment has its own check
    assert write_check["applied"][0][0] == item


@pytest.mark.asyncio
async def test_an_open_pull_request_leaves_the_item_to_the_deliveries(write_check):
    write_check["row"] = (uuid.uuid4(), "blocked", [{"repo": REPO, "kind": "issue", "number": 574}])
    write_check["answer"] = None
    out = await refs_mod.check_item_reference_states(uuid.uuid4())
    assert out["read"] == 1 and out["released"] == 0 and not write_check["applied"]


@pytest.mark.asyncio
async def test_an_item_no_longer_blocked_is_not_read(write_check):
    write_check["row"] = (uuid.uuid4(), "open", [{"repo": REPO, "kind": "issue", "number": 574}])
    out = await refs_mod.check_item_reference_states(uuid.uuid4())
    assert out == {"read": 0, "released": 0, "noted": 0} and not write_check["reads"]


def test_both_writes_schedule_the_check():
    import inspect

    assert "schedule_reference_state_check(item_id)" in inspect.getsource(wi_service.create_work_item)
    assert "schedule_reference_state_check(item_id)" in inspect.getsource(wi_service.update_work_item)


# -- D4: one message for the ask card ----------------------------------------------


@pytest.mark.parametrize("ask", [None, {"question": "Go?"}])
def test_both_checks_answer_one_message(ask):
    with pytest.raises(ValueError) as tool_side:
        refuse_incomplete_person_ask(ask)
    with pytest.raises(ValueError) as rule_side:
        refuse_needs_human_without_ask(True, ask)
    for message in (str(tool_side.value), str(rule_side.value)):
        assert message.startswith(ASK_REQUIRED_MESSAGE)
        for field in ("question", "context", "(at least 2)", "recommendation", "nothing was written"):
            assert field in message


# -- D5: the agent tool never clears an owner silently -------------------------------


def test_the_tool_says_an_empty_owner_is_kept():
    import inspect

    from app.services.builtin_tools import work_items as tools

    source = inspect.getsource(tools)
    assert "OWNER_KEPT_LINE" in source and "owner_kept" in source
    assert "the owner is kept" in tools.OWNER_KEPT_LINE
