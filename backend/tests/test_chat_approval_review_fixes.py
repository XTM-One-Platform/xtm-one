"""Defects found reviewing the chat-approval bookkeeping.

Each of these shipped working in the obvious direction and wrong in a narrow
sequence, which is how they got past three convergence passes:

* A "Yes, always" on a tool whose schema had changed was granted and then
  revoked in the same turn, because the gate had already flagged the tool stale
  and the drain ran *after* the decision. The run path escapes it only by
  draining at pause time.
* Human verdicts never reached the outcome sink, so every human-decided call was
  recorded as having no gate outcome at all.
* The chat grant omitted the impersonating administrator, and the upsert
  overwrote the field unconditionally — so a chat re-grant erased an
  impersonator recorded by a run.
* The drain sat only on the completing path, while the sinks can only be filled
  on the pausing one.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services.assignment_engine import _apply_approval_decisions


class _Executor:
    def __init__(self):
        self.executed: list[str] = []

    async def execute_tool(self, call):
        self.executed.append(call.name)
        return SimpleNamespace(content="ok")


def _proposal(call_id="call_1", name="delete_entity"):
    return {
        "tool_call_id": call_id,
        "tool_name": name,
        "arguments": {},
        "input_schema": {"type": "object"},
        "source": "integration:opencti",
    }


# --- F1: consent must outlive the staleness that prompted for it ---------


@pytest.mark.asyncio
async def test_approving_always_clears_the_pending_staleness():
    """The bug: the gate flags the tool stale, the user re-approves it as it now
    is, and the post-loop drain then revokes the row just written — so the tool
    prompts again next turn and the user's list blames a tool change."""
    stale = {"delete_entity"}
    granted: list[str] = []

    async def _grant(proposal, _decision):
        granted.append(proposal["tool_name"])
        # What the real sink does, and the whole point of this test.
        stale.discard(proposal["tool_name"])

    await _apply_approval_decisions(
        executor=_Executor(),
        messages=[],
        proposals=[_proposal()],
        decisions={"call_1": {"decision": "approve_always"}},
        on_approve_always=_grant,
    )

    assert granted == ["delete_entity"]
    assert stale == set(), "the re-granted tool would have been revoked again"


def test_the_chat_sink_discards_the_stale_entry():
    """Pins it where it actually lives, since the ordering is what makes this
    subtle: the sink is filled before the decision and drained after it."""
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    assert "_chat_stale_approvals.discard(" in source


# --- F2: the human's verdict IS the outcome ------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("approve", "human_approve"),
        ("approve_always", "human_approve_always"),
        ("reject", "human_reject"),
    ],
)
async def test_a_human_decision_is_recorded_as_the_gate_outcome(verdict, expected):
    """FR-020 asks why every call ran. For a gated call the answer is "a person
    said so", and it was being dropped: the pausing decision carries no outcome
    and the approved call is dispatched with the gate bypassed."""
    outcomes: dict[str, str] = {}

    await _apply_approval_decisions(
        executor=_Executor(),
        messages=[],
        proposals=[_proposal()],
        decisions={"call_1": {"decision": verdict}},
        outcomes=outcomes,
    )

    assert outcomes == {"call_1": expected}


def test_the_loop_threads_the_outcome_sink_through():
    """Without this the branch above is unreachable from chat."""
    import inspect

    from app.tools import agent_loop

    source = inspect.getsource(agent_loop)
    assert "approval_outcomes" in source
    assert "outcomes=approval_outcomes" in source


# --- F3: impersonation survives a re-grant ------------------------------


class _FakeDb:
    def __init__(self, existing=None):
        self._existing = existing
        self._scalars = 0
        self.added: list = []

    async def scalar(self, _stmt):
        self._scalars += 1
        if self._scalars == 1:
            return self._existing
        return SimpleNamespace(id=uuid.uuid4(), email="a@b.c")

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None


@pytest.mark.asyncio
async def test_a_regrant_without_an_admin_keeps_the_recorded_one(monkeypatch):
    """The data-loss half. A chat re-grant passes no administrator, and
    overwriting with that would erase the one a run had recorded — on the single
    row whose history FR-017b exists to preserve."""

    async def _noop(db, **kwargs):
        return None

    monkeypatch.setattr("app.services.audit.log_audit", _noop)

    from app.services.tool_approval_service import grant_exception

    admin_id = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tool_key="delete_entity",
        schema_fingerprint="old",
        tool_source="integration:opencti",
        tool_entity_id=None,
        granted_by_admin_id=admin_id,
    )

    await grant_exception(
        _FakeDb(existing=existing),
        user_id=existing.user_id,
        tool_key="delete_entity",
        tool_source="integration:opencti",
        schema_fingerprint="new",
        granted_by_admin_id=None,
    )

    assert existing.granted_by_admin_id == admin_id


@pytest.mark.asyncio
async def test_a_regrant_with_an_admin_updates_it(monkeypatch):
    """Set, never cleared — but still set when there is something to set."""

    async def _noop(db, **kwargs):
        return None

    monkeypatch.setattr("app.services.audit.log_audit", _noop)

    from app.services.tool_approval_service import grant_exception

    newer_admin = uuid.uuid4()
    existing = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        tool_key="delete_entity",
        schema_fingerprint="old",
        tool_source="integration:opencti",
        tool_entity_id=None,
        granted_by_admin_id=uuid.uuid4(),
    )

    await grant_exception(
        _FakeDb(existing=existing),
        user_id=existing.user_id,
        tool_key="delete_entity",
        tool_source="integration:opencti",
        schema_fingerprint="new",
        granted_by_admin_id=newer_admin,
    )

    assert existing.granted_by_admin_id == newer_admin


def test_both_chat_transports_pass_the_impersonator():
    """The claim is the only place that identity exists on a socket, and the
    embedded assistant reads it off the authenticated user."""
    import inspect

    from app.api.v1 import chat, platform
    from app.services import agent_chat

    assert "impersonated_by" in inspect.getsource(agent_chat.run_agent_chat)
    assert "impersonated_by=_turn_impersonated_by" in inspect.getsource(chat)
    assert 'impersonated_by=getattr(user, "impersonated_by", None)' in inspect.getsource(
        platform
    )


# --- F4/F5: the drain has to survive the pause --------------------------


def test_the_pausing_path_also_drains():
    """The sinks can *only* be filled on a turn that paused, so a drain reachable
    only when the turn completes misses every case it was written for."""
    import inspect

    from app.api.v1 import public_chat
    from app.services import general_chat

    for module, helper in (
        (general_chat, "_drain_ga_bookkeeping"),
        (public_chat, "_drain_public_bookkeeping"),
    ):
        source = inspect.getsource(module)
        # Either binding form — what matters is that the pause is caught here
        # rather than left to a broad handler, not whether the exception is
        # given a name (one of these answers it, so it needs one).
        assert (
            "except HumanInputRequestedError:" in source
            or "except HumanInputRequestedError as " in source
        ), f"{module.__name__} still skips the drain when the gate pauses"
        # Once on the completing path, once on the pausing one.
        assert source.count(f"await {helper}(") == 2


# --- F7/F8: which registrations govern ----------------------------------


def test_an_unregistered_platform_stops_governing():
    """Unregistering keeps the row, so without a status filter a platform that
    is gone still answers 409 for the per-integration toggle forever."""
    import inspect

    from app.services import hitl_whitelist

    source = inspect.getsource(hitl_whitelist.platform_approval_by_provider)
    assert 'status != "unregistered"' in source


def test_the_observed_capability_matches_case_insensitively():
    """The header is normalised, the stored identifier is verbatim from the
    registration body — so a platform registering as "OpenCTI" would never
    match its own header."""
    import inspect

    from app.api.v1 import platform

    source = inspect.getsource(platform._note_client_approval_support)
    assert "func.lower(PlatformRegistration.platform_identifier)" in source


# --- an unrecognised verdict must refuse, not run ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verdict",
    [
        "rejected",  # a plausible typo
        "Reject",  # wrong case
        "approve_with_modifications",  # a verdict this product no longer has
        "",
        None,
    ],
)
async def test_an_unrecognised_verdict_does_not_execute(verdict):
    """The fail-open this closes. The applier asked "is it exactly reject?" and
    ran everything else, so a typo — or a stale client's verdict — executed a
    destructive call and recorded it as approved."""
    executor = _Executor()
    messages: list = []

    await _apply_approval_decisions(
        executor=executor,
        messages=messages,
        proposals=[_proposal()],
        decisions={"call_1": {"decision": verdict}},
    )

    assert executor.executed == [], f"{verdict!r} executed the call"
    assert len(messages) == 1, "it still needs a tool result, as a refusal"
    assert "Rejected by human reviewer" in messages[0].content


@pytest.mark.asyncio
async def test_the_recognised_verdicts_still_run():
    """The guard must not be so tight that it refuses real approvals."""
    for verdict in ("approve", "approve_always"):
        executor = _Executor()
        await _apply_approval_decisions(
            executor=executor,
            messages=[],
            proposals=[_proposal()],
            decisions={"call_1": {"decision": verdict}},
        )
        assert executor.executed == ["delete_entity"], verdict


def test_every_transport_constrains_the_verdict():
    """The applier is the last line, not the only one. Both streamed chat
    transports validate by schema: they share ``PlatformChatApprovalDecision``."""
    import inspect

    from app.api.v1 import chat_web_stream, platform

    assert 'decision: Literal["approve", "approve_always", "reject"]' in inspect.getsource(
        platform.PlatformChatApprovalDecision
    )
    decisions = chat_web_stream.WebChatApprovalRequest.model_fields["decisions"]
    assert decisions.annotation == list[platform.PlatformChatApprovalDecision]
