"""Autonomous runs under per-tool approval.

Two properties matter here, and they pull in opposite directions:

1. A standing approval applies to scheduled runs exactly as it does to
   interactive ones. The user consented to the tool, not to a particular way of
   launching it, so gating must not depend on interactivity.
2. When an unattended run *does* pause, somebody has to be told. Nothing further
   happens until a human acts, so a missed notification means the scheduled work
   silently stops forever.

The second one closes a pre-existing gap: ``_persist_notification_activity``
skipped durable records for company-managed runs entirely, relying on an
ephemeral ``activity:company`` broadcast. That was tolerable when pauses were
rare; per-tool approval makes scheduled runs pause routinely.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.notify import _persist_notification_activity
from app.tools.approval import evaluate_gate, fingerprint

SCHEMA = {"type": "object", "properties": {}}
FP = fingerprint(SCHEMA)


class _FakeDB:
    """Records what would be persisted."""

    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1


def _run(**overrides):
    base = dict(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_human",
        is_company_managed=False,
        triggered_by_user_id=None,  # autonomous by default
        pending_approvals=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _assignment():
    return SimpleNamespace(id=uuid.uuid4(), name="Nightly enrichment")


def _agent():
    return SimpleNamespace(id=uuid.uuid4(), name="Enricher")


# --- Property 1: gating ignores interactivity ---------------------------


def test_standing_approval_applies_to_scheduled_runs():
    """A tool the user accepted must not re-prompt just because a cron ran it."""
    decision = evaluate_gate(
        "delete_entity",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"delete_entity": FP},
        current_fingerprint=FP,
    )
    assert decision.action == "execute"


def test_uncovered_tool_still_pauses_in_a_scheduled_run():
    """Standing approvals narrow which calls pause — they do not disable pausing."""
    decision = evaluate_gate(
        "delete_other",
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={"delete_entity": FP},
        current_fingerprint=FP,
    )
    assert decision.action == "pause"


# --- Property 2: an unattended pause is never silent --------------------


async def test_autonomous_company_managed_pause_persists_a_notification():
    """The gap this closes: previously only an ephemeral broadcast was sent."""
    db = _FakeDB()
    run = _run(
        is_company_managed=True,
        status="awaiting_human",
        pending_approvals=[{"tool_name": "delete_entity", "tool_call_id": "c1"}],
    )

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert len(db.added) == 1, (
        "an unattended company-managed pause produced no durable record — "
        "anyone not connected at that instant would never learn the run stalled"
    )


async def test_company_managed_free_text_pause_is_notified_as_before():
    """A ``request_human_input`` pause (no proposals) writes no personal record.

    An upgraded install that never opted into the gate must not start writing
    personal Activity rows for its company-managed human-in-the-loop runs.
    """
    db = _FakeDB()
    run = _run(is_company_managed=True, status="awaiting_human")

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert db.added == []


async def test_autonomous_personal_pause_persists_a_notification():
    db = _FakeDB()
    run = _run(is_company_managed=False, status="awaiting_human")

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert len(db.added) == 1


async def test_completed_company_managed_run_still_skips_the_personal_record():
    """Unchanged for non-paused runs.

    The broadcast-only behaviour is deliberate for company-managed runs: a
    personal Activity for one owner would not reach the other users such a run
    is meant to notify. Only the *paused* case is special-cased.
    """
    db = _FakeDB()
    run = _run(is_company_managed=True, status="completed")

    await _persist_notification_activity(db, run, _assignment(), _agent(), "done")

    assert db.added == []


async def test_owner_less_run_does_not_crash():
    """Nothing to attribute the Activity to — skip rather than raise."""
    db = _FakeDB()
    run = _run(is_company_managed=True, user_id=None, status="awaiting_human")

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert db.added == []


async def test_notification_names_the_tools_awaiting_approval():
    """'awaiting input' alone does not tell an absent owner whether to hurry."""
    db = _FakeDB()
    run = _run(
        pending_approvals=[
            {"tool_name": "delete_entity", "tool_call_id": "c1"},
            {"tool_name": "send_report", "tool_call_id": "c2"},
        ]
    )

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    activity = db.added[0]
    assert "delete_entity" in activity.title
    assert "send_report" in activity.title
    assert activity.extra_data["pending_tools"] == ["delete_entity", "send_report"]
    assert activity.extra_data["is_autonomous"] is True


async def test_interactive_pause_is_marked_not_autonomous():
    db = _FakeDB()
    run = _run(triggered_by_user_id=uuid.uuid4())

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert db.added[0].extra_data["is_autonomous"] is False


@pytest.mark.parametrize("pending", [None, []])
async def test_free_text_pause_keeps_the_generic_title(pending):
    """A ``request_human_input`` pause has no proposals — title must not break."""
    db = _FakeDB()
    run = _run(pending_approvals=pending)

    await _persist_notification_activity(db, run, _assignment(), _agent(), "paused")

    assert "awaiting input" in db.added[0].title


# --- "Yes, always" on a company-managed run --------------------------------
#
# The gate reads exceptions for ``effective_user_id`` (``run.user_id`` falling
# back to the agent owner), but the grant was written for whoever clicked. On a
# private run those are the same person. On a company-managed one any admin may
# resolve the pause, so the preference landed on an account the gate never
# consults: the tool went on prompting forever while the admin's own preference
# list claimed it never would.


class _Decision:
    def __init__(self, tool_call_id: str, decision: str):
        self.tool_call_id = tool_call_id
        self.decision = decision

    def model_dump(self):
        return {"tool_call_id": self.tool_call_id, "decision": self.decision}


class _Run:
    def __init__(self, user_id, proposals, agent_id=None):
        self.user_id = user_id
        self.agent_id = agent_id
        self.pending_approvals = proposals
        self.is_company_managed = True


class _Db:
    """Answers the one lookup the helper makes for an owner-less run."""

    def __init__(self, agent_owner=None):
        self.agent_owner = agent_owner

    async def scalar(self, _stmt):
        return self.agent_owner


class _User:
    def __init__(self, uid, impersonated_by=None):
        self.id = uid
        self.impersonated_by = impersonated_by


def _proposal_for(tool="delete_entity", call_id="call_1"):
    return {
        "tool_call_id": call_id,
        "tool_name": tool,
        "arguments": {},
        "input_schema": {"type": "object", "properties": {}},
        "source": "custom:1",
    }


async def _capture_grants(monkeypatch, run, user, db=None):
    """Run the grant helper with ``grant_exception`` stubbed, returning its kwargs."""
    from app.api.v1 import assignments as module

    captured: list[dict] = []

    async def _fake_grant(_db, **kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(
        "app.services.tool_approval_service.grant_exception", _fake_grant
    )
    await module._grant_standing_approvals(
        db, run, [_Decision("call_1", "approve_always")], user
    )
    return captured


@pytest.mark.asyncio
async def test_always_on_a_company_managed_run_grants_to_the_run_owner(monkeypatch):
    """The identity the run executes as is the one the gate will consult."""
    owner, admin = uuid.uuid4(), uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch, _Run(owner, [_proposal_for()]), _User(admin)
    )

    assert captured[0]["user_id"] == owner, (
        "granting to the clicker writes the preference where the gate never looks"
    )


@pytest.mark.asyncio
async def test_an_admin_acting_for_someone_else_is_named(monkeypatch):
    """Writing onto another account is acceptable only because the trail says so."""
    owner, admin = uuid.uuid4(), uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch, _Run(owner, [_proposal_for()]), _User(admin)
    )

    assert captured[0]["granted_by_admin_id"] == admin


@pytest.mark.asyncio
async def test_approving_your_own_run_names_no_administrator(monkeypatch):
    """The common case must not acquire a spurious second identity."""
    owner = uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch, _Run(owner, [_proposal_for()]), _User(owner)
    )

    assert captured[0]["user_id"] == owner
    assert captured[0]["granted_by_admin_id"] is None


@pytest.mark.asyncio
async def test_an_impersonated_session_keeps_naming_its_administrator(monkeypatch):
    """Impersonation already carried an administrator; it must still win."""
    owner, real_admin = uuid.uuid4(), uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch, _Run(owner, [_proposal_for()]), _User(owner, impersonated_by=real_admin)
    )

    assert captured[0]["granted_by_admin_id"] == real_admin


@pytest.mark.asyncio
async def test_an_owner_less_run_grants_to_the_agent_owner(monkeypatch):
    """The engine resolves `run.user_id or agent.user_id`, so stopping at the
    first half writes the preference to an account the gate never reads — the
    same silent no-op, one branch further in."""
    agent_owner, admin = uuid.uuid4(), uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch,
        _Run(None, [_proposal_for()], agent_id=uuid.uuid4()),
        _User(admin),
        db=_Db(agent_owner=agent_owner),
    )

    assert captured[0]["user_id"] == agent_owner
    assert captured[0]["granted_by_admin_id"] == admin


@pytest.mark.asyncio
async def test_a_run_with_no_owner_anywhere_falls_back_to_the_clicker(monkeypatch):
    """Nobody for the gate to find either way, so keep the preference visible
    to whoever made it rather than dropping it."""
    admin = uuid.uuid4()

    captured = await _capture_grants(
        monkeypatch,
        _Run(None, [_proposal_for()], agent_id=uuid.uuid4()),
        _User(admin),
        db=_Db(agent_owner=None),
    )

    assert captured[0]["user_id"] == admin
    assert captured[0]["granted_by_admin_id"] is None


# --- The notification has to fit the column it is stored in ----------------


def test_a_gated_batch_cannot_overflow_the_notification_title():
    """`Activity.title` is String(500) and both halves of it are unbounded — a
    user-named assignment and a batch of any size. An over-long title fails the
    insert, and that failure is swallowed by the handler around it, so the pause
    would go unnotified: the one thing this record exists to prevent, failing
    silently on exactly the busy runs that most need it.
    """
    from app.services.notify import (
        _ACTIVITY_TITLE_MAX,
        _MAX_TOOLS_NAMED_IN_TITLE,
        _fit_activity_title,
    )

    assert _fit_activity_title("x" * 900) == "x" * (_ACTIVITY_TITLE_MAX - 1) + "…"
    assert len(_fit_activity_title("x" * 900)) == _ACTIVITY_TITLE_MAX
    assert _fit_activity_title("short") == "short"
    assert _MAX_TOOLS_NAMED_IN_TITLE >= 1


@pytest.mark.asyncio
async def test_the_persisted_title_is_actually_capped():
    """The helpers above existed and were tested, but nothing called them — so
    this test passed while the real title went to the column unbounded. Assert
    the row, not the helper.
    """
    from app.services import notify

    class _CapturingDb:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            pass

    db = _CapturingDb()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_human",
        is_company_managed=False,
        triggered_by_user_id=None,
        pending_approvals=[
            {"tool_name": f"a_very_long_platform_tool_name_{i}"} for i in range(40)
        ],
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="A" * 600)
    agent = SimpleNamespace(id=uuid.uuid4(), name="agent")

    await notify._persist_notification_activity(db, run, assignment, agent, "s")

    assert len(db.added) == 1
    assert len(db.added[0].title) <= notify._ACTIVITY_TITLE_MAX
    # The batch is summarised rather than listed in full.
    assert "and 37 more" in db.added[0].title


def test_the_public_embed_answers_a_gated_call_without_naming_admin_screens():
    """A visitor has no Settings page to visit, and describing one both
    misleads them and tells someone outside the deployment how it is
    configured."""
    from app.tools.approval import no_approval_channel_message

    class _Paused:
        proposals = [{"tool_name": "delete_entity"}]
        question = "Approval required to run delete_entity"

    public = no_approval_channel_message(_Paused(), mention_admin_setting=False)

    assert "delete_entity" in public
    assert "Settings" not in public and "administrator" not in public
    # The internal surfaces still say where to fix it.
    assert "administrator" in no_approval_channel_message(_Paused())


# --- An owner-less pause must still leave a durable record -----------------


@pytest.mark.asyncio
async def test_an_ownerless_pause_is_attributed_to_the_assignment_owner():
    """The worst failure mode in the feature: a company-managed run with no
    owner pauses, `_persist_notification_activity` returns early because there
    is nobody to attribute an Activity to, and the only signal is the broadcast
    — which reaches whoever is connected at that instant and nobody else. The
    run then stops forever with no record that it is waiting.
    """
    from app.services import notify

    class _CapturingDb:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            pass

    owner = uuid.uuid4()
    db = _CapturingDb()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,                 # nobody to attribute to
        status="awaiting_human",
        is_company_managed=True,
        triggered_by_user_id=None,    # autonomous
        pending_approvals=[{"tool_name": "delete_entity"}],
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Nightly sweep", user_id=owner)
    agent = SimpleNamespace(id=uuid.uuid4(), name="agent")

    await notify._persist_notification_activity(db, run, assignment, agent, "s")

    assert len(db.added) == 1, "an unattended pause must leave a durable record"
    assert db.added[0].user_id == owner


@pytest.mark.asyncio
async def test_a_completed_ownerless_run_still_leaves_no_personal_activity():
    """The fallback is scoped to the pause. A *completed* company-managed run
    still relies on the broadcast — a personal Activity for one owner would not
    reach the other users a company-managed run is meant to notify.
    """
    from app.services import notify

    class _CapturingDb:
        def __init__(self):
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        async def commit(self):
            pass

    db = _CapturingDb()
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        status="completed",
        is_company_managed=True,
        triggered_by_user_id=None,
        pending_approvals=None,
    )
    assignment = SimpleNamespace(id=uuid.uuid4(), name="a", user_id=uuid.uuid4())
    agent = SimpleNamespace(id=uuid.uuid4(), name="agent")

    await notify._persist_notification_activity(db, run, assignment, agent, "s")

    assert db.added == []
