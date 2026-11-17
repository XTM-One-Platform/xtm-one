"""What "Yes, always" leaves behind, and what the trail says about it.

Three failures met here, all of the same kind — the visible half of a feature
worked while its durable half did nothing, so nothing ever complained:

* In chat, "Yes, always" ran the tool and saved no preference, while the prompt
  above the button promised the choice would cover scheduled runs too. The tool
  asked again on the next turn (FR-015, FR-002b, US2/AC4).
* Granting a standing approval wrote no audit record, though revoking one did —
  the trail showed consent withdrawn that was never given (FR-018).
* Invalidation wrote a log line rather than an audit record, so the reason a
  tool started prompting again was legible only to whoever reads logs (FR-019b).
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
        "arguments": {"id": "x"},
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
        "source": "integration:opencti",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("approve_always", 1),
        ("approve", 0),
        ("reject", 0),
    ],
)
async def test_only_approve_always_records_a_preference(verdict, expected):
    """The distinction the whole preference rests on: two of these verdicts
    answer one call, and the third answers every future call too."""
    granted: list[dict] = []

    async def _sink(proposal, _decision):
        granted.append(proposal)

    messages: list = []
    await _apply_approval_decisions(
        executor=_Executor(),
        messages=messages,
        proposals=[_proposal()],
        decisions={"call_1": {"decision": verdict}},
        on_approve_always=_sink,
    )
    assert len(granted) == expected


@pytest.mark.asyncio
async def test_the_preference_is_bound_to_the_proposal_the_user_saw():
    """Consent binds to a shape, not a name (FR-019a). The sink is handed the
    proposal so the fingerprint describes the call actually shown — not what the
    tool may have become since."""
    granted: list[dict] = []

    async def _sink(proposal, _decision):
        granted.append(proposal)

    await _apply_approval_decisions(
        executor=_Executor(),
        messages=[],
        proposals=[_proposal()],
        decisions={"call_1": {"decision": "approve_always"}},
        on_approve_always=_sink,
    )
    assert granted[0]["input_schema"]["properties"] == {"id": {"type": "string"}}
    assert granted[0]["source"] == "integration:opencti"


@pytest.mark.asyncio
async def test_a_failure_to_remember_still_runs_the_approved_call():
    """The reviewer approved this call. Losing the preference is a bad day;
    refusing the call they just authorised would be a worse one."""

    async def _broken(proposal, _decision):
        raise RuntimeError("no database")

    executor = _Executor()
    messages: list = []
    await _apply_approval_decisions(
        executor=executor,
        messages=messages,
        proposals=[_proposal()],
        decisions={"call_1": {"decision": "approve_always"}},
        on_approve_always=_broken,
    )
    assert executor.executed == ["delete_entity"]
    assert len(messages) == 1, "the call still gets its tool result"


@pytest.mark.asyncio
async def test_every_proposal_still_gets_a_result_whatever_the_verdict():
    """Unchanged contract, re-pinned because this function grew a branch: a
    ``tool_use`` block without a ``tool_result`` is rejected on resume."""
    messages: list = []
    await _apply_approval_decisions(
        executor=_Executor(),
        messages=messages,
        proposals=[_proposal("call_1"), _proposal("call_2", "create_report")],
        decisions={
            "call_1": {"decision": "approve_always"},
            "call_2": {"decision": "reject", "rejection_reason": "wrong target"},
        },
        on_approve_always=None,
    )
    assert [m.tool_call_id for m in messages] == ["call_1", "call_2"]


def test_chat_supplies_the_sink_that_persists_the_preference():
    """The regression guard. Chat approvals shipped with the prompt, the pause
    and the verdict all working, and nothing behind the "always"."""
    import inspect

    from app.services import agent_chat

    source = inspect.getsource(agent_chat)
    assert "on_approve_always=" in source, "chat records no standing approval"
    assert "grant_chat_standing_approval(" in source, "chat never reaches the preference store"


# --- the trail ----------------------------------------------------------


class _FakeDb:
    """Enough session for the preference service, with no database."""

    def __init__(self, existing=None, user=None):
        self._existing = existing
        self._user = user or SimpleNamespace(id=uuid.uuid4(), email="a@b.c")
        self._scalars = 0
        self.added: list = []

    async def scalar(self, _stmt):
        # The service looks the preference up first, then the user it belongs
        # to when writing the audit record.
        self._scalars += 1
        return self._existing if self._scalars == 1 else self._user

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        return None

    async def execute(self, _stmt):
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))


@pytest.mark.asyncio
async def test_granting_is_audited(monkeypatch):
    """FR-018 names granting and revoking together. Revoking was audited at its
    endpoint; granting had no endpoint of its own and so had nothing."""
    records: list[dict] = []

    async def _capture(db, **kwargs):
        records.append(kwargs)

    monkeypatch.setattr("app.services.audit.log_audit", _capture)

    from app.services.tool_approval_service import grant_exception

    db = _FakeDb()
    await grant_exception(
        db,
        user_id=db._user.id,
        tool_key="delete_entity",
        tool_source="integration:opencti",
        schema_fingerprint="abc",
        granted_by_admin_id=None,
    )

    assert len(records) == 1
    assert records[0]["entity_type"] == "tool_approval_preference"
    assert records[0]["entity_name"] == "delete_entity"
    assert "Granted" in records[0]["summary"]


@pytest.mark.asyncio
async def test_an_impersonated_grant_names_both_identities(monkeypatch):
    """FR-017b: an administrator acting as somebody else must not disappear
    into that account."""
    records: list[dict] = []

    async def _capture(db, **kwargs):
        records.append(kwargs)

    monkeypatch.setattr("app.services.audit.log_audit", _capture)

    from app.services.tool_approval_service import grant_exception

    admin_id = uuid.uuid4()
    db = _FakeDb()
    await grant_exception(
        db,
        user_id=db._user.id,
        tool_key="delete_entity",
        tool_source="integration:opencti",
        schema_fingerprint="abc",
        granted_by_admin_id=admin_id,
    )

    assert records[0]["details"]["impersonated_by"] == str(admin_id)


@pytest.mark.asyncio
async def test_an_audit_failure_does_not_cost_the_preference(monkeypatch):
    """The record is the safeguard, not the product. Losing it must not lose
    the consent the user just gave."""

    async def _boom(db, **kwargs):
        raise RuntimeError("audit table missing")

    monkeypatch.setattr("app.services.audit.log_audit", _boom)

    from app.services.tool_approval_service import grant_exception

    db = _FakeDb()
    preference = await grant_exception(
        db,
        user_id=db._user.id,
        tool_key="delete_entity",
        tool_source="integration:opencti",
        schema_fingerprint="abc",
    )
    assert preference in db.added
