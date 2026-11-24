"""The ``organization_context`` write group (#11594): administrators only, enforced twice."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.builtin_tools import (
    ADMIN_BOUND_BUILTIN_TOOL_IDS,
    ORGANIZATION_CONTEXT_WRITE_GROUP,
    ORGANIZATION_PROPOSAL_TOOL,
    ORGANIZATION_UPDATE_TOOL,
    build_organization_context_write_tools,
    offers_organization_context_write,
    refused_admin_bound_ids,
)
from app.services.builtin_tools import organization_context_write as module
from app.services.builtin_tools.filtering import AGENT_KEYED_BUILTIN_TOOL_IDS

# A fixed id: it is a parametrize value, and pytest-xdist workers must collect the same ids.
_ADMIN_ID = "00000000-0000-4000-8000-000000011594"


def test_the_group_is_registered_agent_keyed_and_off_by_default():
    group = next(g for g in BUILTIN_TOOL_GROUPS if g.id == ORGANIZATION_CONTEXT_WRITE_GROUP)
    assert group.agent_keyed and not group.default_on
    assert ORGANIZATION_CONTEXT_WRITE_GROUP in AGENT_KEYED_BUILTIN_TOOL_IDS
    assert ORGANIZATION_CONTEXT_WRITE_GROUP in ADMIN_BOUND_BUILTIN_TOOL_IDS


def test_the_registry_serves_the_bind_restriction_pickers_lock_on():
    from app.services.builtin_tool_groups import builtin_tool_groups_payload

    assert {g.id for g in BUILTIN_TOOL_GROUPS if g.bind_admin_only} == set(ADMIN_BOUND_BUILTIN_TOOL_IDS)
    payload = {entry["id"]: entry for entry in builtin_tool_groups_payload({})}
    assert payload[ORGANIZATION_CONTEXT_WRITE_GROUP]["bind_admin_only"] is True
    # Distinct from admin_only (the General Assistant offer of Platform Insights).
    assert payload[ORGANIZATION_CONTEXT_WRITE_GROUP]["admin_only"] is False
    assert payload["platform"]["bind_admin_only"] is False


def test_the_group_carries_the_update_and_the_proposal_tools():
    defs, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    names = {d.name for d in defs}
    assert names == {
        ORGANIZATION_UPDATE_TOOL,
        ORGANIZATION_PROPOSAL_TOOL,
        module.ORGANIZATION_UPSERT_ROWS_TOOL,
        module.ORGANIZATION_REMOVE_ROWS_TOOL,
        module.ORGANIZATION_IMPORT_TOOL,
        module.ORGANIZATION_LIST_SYNCS_TOOL,
        module.ORGANIZATION_CREATE_SYNC_TOOL,
        module.ORGANIZATION_RUN_SYNC_TOOL,
        module.ORGANIZATION_LIST_PROPOSALS_TOOL,
        module.ORGANIZATION_DECIDE_PROPOSAL_TOOL,
        module.ORGANIZATION_VERSIONS_TOOL,
        module.ORGANIZATION_RESTORE_TOOL,
        "reconcile_organization_with_opencrq",
    }
    assert all(d.source == f"builtin:{ORGANIZATION_CONTEXT_WRITE_GROUP}" for d in defs)
    assert executor.handler(ORGANIZATION_UPDATE_TOOL) is not None
    assert executor.handler(ORGANIZATION_PROPOSAL_TOOL) is not None


def test_the_proposal_tool_is_left_out_when_the_turn_already_has_it():
    defs, _ = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, include_proposal=False)
    names = [d.name for d in defs]
    assert ORGANIZATION_PROPOSAL_TOOL not in names
    assert names[0] == ORGANIZATION_UPDATE_TOOL


@pytest.mark.parametrize(
    ("bound", "user_id", "is_admin", "expected"),
    [
        (["organization_context"], _ADMIN_ID, True, True),
        (["organization_context"], _ADMIN_ID, False, False),
        (["organization_context"], None, True, False),
        (["web_search"], _ADMIN_ID, True, False),
        ([], _ADMIN_ID, True, False),
    ],
)
def test_the_group_is_offered_only_bound_and_to_a_known_administrator(bound, user_id, is_admin, expected):
    assert offers_organization_context_write(bound, acting_user_id=user_id, acting_is_admin=is_admin) is expected


@pytest.mark.asyncio
async def test_a_non_admin_call_is_refused_and_writes_nothing(monkeypatch):
    from app.services import organization_context_proposals as proposals

    async def _must_not_run(*_args, **_kwargs):  # pragma: no cover - the refusal comes first
        raise AssertionError("apply_changes reached for a non-administrator")

    monkeypatch.setattr(proposals, "apply_changes", _must_not_run)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=False)
    answer = await executor.handler(ORGANIZATION_UPDATE_TOOL)(changes=[{"field": "tech_stack", "value": []}])
    assert answer == module.REFUSAL


@pytest.mark.asyncio
async def test_an_unknown_identity_is_refused(monkeypatch):
    _, executor = build_organization_context_write_tools(user_id=None, caller_is_admin=True)
    answer = await executor.handler(ORGANIZATION_UPDATE_TOOL)(changes=[{"field": "tech_stack", "value": []}])
    assert answer == module.REFUSAL


@pytest.mark.asyncio
async def test_a_role_taken_away_since_the_turn_began_is_refused_at_the_call(monkeypatch):
    """The executor reads the person again: an administrator no more is refused."""
    from app.core import database
    from app.services import organization_context_proposals as proposals

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return SimpleNamespace(id=_id, is_admin=False)

    async def _must_not_run(*_args, **_kwargs):  # pragma: no cover
        raise AssertionError("apply_changes reached for a former administrator")

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(proposals, "apply_changes", _must_not_run)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    answer = await executor.handler(ORGANIZATION_UPDATE_TOOL)(changes=[{"field": "tech_stack", "value": []}])
    assert answer == module.REFUSAL


@pytest.mark.asyncio
async def test_an_administrator_call_saves_a_version_and_an_audit_entry(monkeypatch):
    from app.core import database
    from app.services import audit
    from app.services import organization_context_proposals as proposals

    calls: dict = {}

    class _Session:
        committed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return SimpleNamespace(id=_id, is_admin=True)

        async def commit(self):
            _Session.committed = True

    async def _apply(db, **kwargs):
        calls["apply"] = kwargs
        return {"organization_id": str(uuid.uuid4()), "organization": "Acme", "version": 4, "fields": ["tech_stack"]}

    async def _log_audit(db, **kwargs):
        calls["audit"] = kwargs

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(proposals, "apply_changes", _apply)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops agent")
    changes = [{"field": "tech_stack", "value": [{"name": "Postgres"}], "basis": "the admin said so"}]
    answer = await executor.handler(ORGANIZATION_UPDATE_TOOL)(changes=changes)
    assert answer.startswith("Saved version 4 of the profile of Acme: tech_stack.")
    assert calls["apply"]["author_user_id"] == uuid.UUID(_ADMIN_ID)
    assert calls["apply"]["agent_name"] == "Ops agent"
    assert calls["apply"]["changes"] == changes
    assert calls["audit"]["entity_type"] == "organization_context"
    assert calls["audit"]["details"]["agent"] == "Ops agent"
    assert _Session.committed


@pytest.mark.asyncio
async def test_a_validation_error_is_returned_and_nothing_is_saved(monkeypatch):
    from app.core import database
    from app.services import organization_context_proposals as proposals

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _model, _id):
            return SimpleNamespace(id=_id, is_admin=True)

        async def commit(self):  # pragma: no cover
            raise AssertionError("committed after a refusal")

    async def _apply(db, **kwargs):
        raise proposals.ProposalError("tech_stack: not a field of this organization's profile.")

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(proposals, "apply_changes", _apply)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    answer = await executor.handler(ORGANIZATION_UPDATE_TOOL)(changes=[{"field": "nope", "value": 1}])
    assert answer.startswith("Not saved: tech_stack")


class _SyncSession:
    """An async session double for the sync tools: an administrator, one stored sync."""

    def __init__(self, *, admin: bool = True, stored=None):
        self.admin, self.stored, self.added, self.committed = admin, stored, [], False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, model, _id):
        if model.__name__ == "User":
            return SimpleNamespace(id=_id, is_admin=self.admin, email="admin@example.com")
        return self.stored if self.stored is not None and self.stored.id == _id else None

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            row.id = row.id or uuid.uuid4()

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_the_sync_tools_refuse_a_former_administrator(monkeypatch):
    from app.core import database

    monkeypatch.setattr(database, "async_session", lambda: _SyncSession(admin=False))
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    assert await executor.handler(module.ORGANIZATION_LIST_SYNCS_TOOL)() == module.REFUSAL
    assert await executor.handler(module.ORGANIZATION_CREATE_SYNC_TOOL)(section="assets", source="opencti") == (
        module.REFUSAL
    )
    assert await executor.handler(module.ORGANIZATION_RUN_SYNC_TOOL)(sync_id=str(uuid.uuid4())) == module.REFUSAL


@pytest.mark.asyncio
async def test_create_sync_saves_the_rest_definition_owned_by_the_administrator(monkeypatch):
    import json

    from app.core import database
    from app.services import audit
    from app.services import organization_context_sync as osync

    session = _SyncSession()
    audits: list = []

    async def _log_audit(db, **kwargs):
        audits.append(kwargs)

    async def _no_global(_db):
        return False

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    monkeypatch.setattr(osync, "global_sync_exists", _no_global)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops")
    answer = json.loads(
        await executor.handler(module.ORGANIZATION_CREATE_SYNC_TOOL)(
            section="assets", source="opencti", frequency="weekly", hour=7, weekday=2, timezone="Europe/Paris"
        )
    )
    assert answer["saved"] is True
    assert answer["notice"] == module.SHARED_CONTEXT_NOTICE
    row = session.added[0]
    assert row.owner_user_id == uuid.UUID(_ADMIN_ID)
    assert (row.section, row.source, row.frequency, row.hour, row.weekday) == ("assets", "opencti", "weekly", 7, 2)
    assert row.paused_reason == "" and row.next_run_at is not None
    assert session.committed
    assert audits[0]["entity_type"] == "organization_context_sync"
    assert audits[0]["details"]["agent"] == "Ops"


@pytest.mark.asyncio
async def test_create_sync_refuses_what_the_scheduler_could_not_run(monkeypatch):
    from app.core import database

    session = _SyncSession()
    monkeypatch.setattr(database, "async_session", lambda: session)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    answer = await executor.handler(module.ORGANIZATION_CREATE_SYNC_TOOL)(
        section="assets", source="opencti", timezone="Mars/Olympus"
    )
    assert answer.startswith("Not saved: unknown time zone")
    assert not session.committed and not session.added


@pytest.mark.asyncio
async def test_run_sync_runs_the_stored_sync_now(monkeypatch):
    import json

    from app.core import database
    from app.services import organization_context_sync as osync

    stored = SimpleNamespace(
        id=uuid.uuid4(),
        section="assets",
        source="opencti",
        filters={},
        remove_gone=False,
        frequency="daily",
        hour=6,
        minute=0,
        weekday=0,
        timezone="UTC",
        enabled=True,
        paused_reason="",
        next_run_at=None,
        last_run_at=None,
        last_result=None,
    )
    session = _SyncSession(stored=stored)
    triggers: list = []

    async def _run_now(_db, sync, *, trigger):
        triggers.append((sync.id, trigger))
        return {"added": 3, "updated": 1, "removed": 0, "version": 9, "error": None}

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(osync, "run_now", _run_now)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops")
    answer = json.loads(await executor.handler(module.ORGANIZATION_RUN_SYNC_TOOL)(sync_id=str(stored.id)))
    assert answer["result"]["version"] == 9
    assert answer["notice"] == module.SHARED_CONTEXT_NOTICE
    assert triggers == [(stored.id, "run now by agent Ops")]
    assert session.committed
    missing = await executor.handler(module.ORGANIZATION_RUN_SYNC_TOOL)(sync_id=str(uuid.uuid4()))
    assert missing.startswith("Not run: no sync")
    assert (await executor.handler(module.ORGANIZATION_RUN_SYNC_TOOL)(sync_id="nope")).startswith("Not run:")


def test_every_import_sync_and_write_tool_states_the_shared_context() -> None:  # #11994
    defs, _ = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    by_name = {d.name: d.description for d in defs}
    warned = {
        ORGANIZATION_UPDATE_TOOL,
        ORGANIZATION_PROPOSAL_TOOL,
        module.ORGANIZATION_UPSERT_ROWS_TOOL,
        module.ORGANIZATION_IMPORT_TOOL,
        module.ORGANIZATION_CREATE_SYNC_TOOL,
        module.ORGANIZATION_RUN_SYNC_TOOL,
        module.ORGANIZATION_RESTORE_TOOL,
        "reconcile_organization_with_opencrq",
    }
    for name in warned:
        assert by_name[name].endswith(module.SHARED_CONTEXT_CLAUSE), name
    for fact in ("your access", "segregation and markings not kept", "every user and agent"):
        assert fact in module.SHARED_CONTEXT_CLAUSE
    for fact in ("permissions of the person", "segregation", "markings", "every user and every agent", "only"):
        assert fact in module.SHARED_CONTEXT_NOTICE


@pytest.mark.asyncio
async def test_the_import_preview_carries_the_shared_context_notice(monkeypatch):  # #11994
    import json

    from app.core import database
    from app.services import organization_context_import as imp

    plan = SimpleNamespace(error=None, new=[], updated=[], profile=None, as_dict=lambda: {"counts": {"new": 0}})

    async def _preview(_db, **_kwargs):
        return plan, 4

    monkeypatch.setattr(database, "async_session", lambda: _SyncSession())
    monkeypatch.setattr(imp, "preview_import", _preview)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    answer = json.loads(await executor.handler(module.ORGANIZATION_IMPORT_TOOL)(section="assets", source="opencti"))
    assert answer["applied"] is False
    assert answer["notice"] == module.SHARED_CONTEXT_NOTICE


def _proposal_row(**overrides):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        kind="fields",
        organization_id=uuid.uuid4(),
        status="pending",
        proposals=[{"field": "tech_stack", "value": [{"product": "Postgres"}], "basis": "chat"}],
        based_on_version=13,
        author_user_id=None,
        created_at="2026-11-17T08:00:00Z",
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


class _ProposalSession(_SyncSession):
    """The sync double, with a stored proposal read under a row lock."""

    async def get(self, model, _id, with_for_update=False):
        if model.__name__ == "OrganizationContextProposal":
            self.locked = with_for_update
            return self.stored if self.stored is not None and self.stored.id == _id else None
        return await super().get(model, _id)


@pytest.mark.asyncio
async def test_the_proposal_tools_refuse_a_former_administrator(monkeypatch):  # #475
    from app.core import database
    from app.services import organization_context_proposals as proposals

    async def _must_not_run(*_args, **_kwargs):  # pragma: no cover - the refusal comes first
        raise AssertionError("a proposal was read or decided for a non-administrator")

    monkeypatch.setattr(database, "async_session", lambda: _ProposalSession(admin=False))
    monkeypatch.setattr(proposals, "list_proposals", _must_not_run)
    monkeypatch.setattr(proposals, "decide", _must_not_run)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    assert await executor.handler(module.ORGANIZATION_LIST_PROPOSALS_TOOL)() == module.REFUSAL
    decide = executor.handler(module.ORGANIZATION_DECIDE_PROPOSAL_TOOL)
    assert await decide(proposal_id=str(uuid.uuid4()), accept=[]) == module.REFUSAL
    _, refused = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=False)
    assert await refused.handler(module.ORGANIZATION_LIST_PROPOSALS_TOOL)() == module.REFUSAL


@pytest.mark.asyncio
async def test_list_proposals_answers_the_stored_proposals(monkeypatch):  # #475
    import json

    from app.core import database
    from app.services import organization_context_proposals as proposals

    row = _proposal_row()
    asked: list = []

    async def _list(_db, *, status, limit):
        asked.append((status, limit))
        return [row]

    monkeypatch.setattr(database, "async_session", lambda: _ProposalSession())
    monkeypatch.setattr(proposals, "list_proposals", _list)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    answer = json.loads(await executor.handler(module.ORGANIZATION_LIST_PROPOSALS_TOOL)(status="pending", limit=5))
    assert asked == [("pending", 5)]
    assert answer["proposals"][0]["id"] == str(row.id)
    assert answer["proposals"][0]["entries"][0]["field"] == "tech_stack"
    assert answer["proposals"][0]["based_on_version"] == 13
    bad = await executor.handler(module.ORGANIZATION_LIST_PROPOSALS_TOOL)(status="stale")
    assert bad.startswith("Not read: status is one of pending")


@pytest.mark.asyncio
async def test_decide_rejects_with_an_empty_accept_and_writes_an_audit_entry(monkeypatch):  # #475
    import json

    from app.core import database
    from app.services import audit
    from app.services import organization_context_proposals as proposals

    row = _proposal_row()
    session = _ProposalSession(stored=row)
    calls: dict = {}

    async def _decide(_db, proposal, *, admin_user_id, accept):
        calls["decide"] = (proposal.id, admin_user_id, accept)
        return {"status": "rejected", "version": None, "created": []}

    async def _log_audit(_db, **kwargs):
        calls["audit"] = kwargs

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(proposals, "decide", _decide)
    monkeypatch.setattr(audit, "log_audit", _log_audit)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True, agent_name="Ops")
    answer = json.loads(
        await executor.handler(module.ORGANIZATION_DECIDE_PROPOSAL_TOOL)(proposal_id=str(row.id), accept=[])
    )
    assert answer == {"proposal_id": str(row.id), "status": "rejected", "version": None, "created": []}
    assert calls["decide"] == (row.id, uuid.UUID(_ADMIN_ID), [])
    assert session.locked is True
    assert calls["audit"]["entity_type"] == "organization_context_proposal"
    assert calls["audit"]["details"]["agent"] == "Ops"
    assert session.committed


@pytest.mark.asyncio
async def test_decide_names_a_refusal_and_commits_nothing(monkeypatch):  # #475
    from app.core import database
    from app.services import organization_context_proposals as proposals

    row = _proposal_row(status="accepted")
    session = _ProposalSession(stored=row)

    async def _decide(_db, proposal, **_kwargs):
        raise proposals.ProposalError(f"This proposal is already {proposal.status}.")

    monkeypatch.setattr(database, "async_session", lambda: session)
    monkeypatch.setattr(proposals, "decide", _decide)
    _, executor = build_organization_context_write_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    decide = executor.handler(module.ORGANIZATION_DECIDE_PROPOSAL_TOOL)
    assert await decide(proposal_id=str(row.id), accept=[]) == "Not decided: This proposal is already accepted."
    assert not session.committed
    assert (await decide(proposal_id=str(uuid.uuid4()), accept=[])).startswith("Not decided: no proposal")
    assert (await decide(proposal_id="nope", accept=[])).startswith("Not decided: proposal_id")


def test_binding_by_a_non_admin_is_refused_and_an_admin_may_bind():
    assert refused_admin_bound_ids(["organization_context", "web_search"], caller_is_admin=False) == [
        "organization_context"
    ]
    assert refused_admin_bound_ids(["organization_context"], caller_is_admin=True) == []
    # A save that keeps a group an administrator already bound is not a new binding.
    assert refused_admin_bound_ids(["organization_context"], before=["organization_context"], caller_is_admin=False) == []


def test_the_agents_api_refuses_a_non_admin_binding():
    from app.api.v1.agents import _admin_bound_or_403

    with pytest.raises(HTTPException) as exc:
        _admin_bound_or_403(SimpleNamespace(is_admin=False), ["organization_context"])
    assert exc.value.status_code == 403
    # The backstop names the card in plain words, never the internal id.
    assert exc.value.detail == "Only an administrator can add Organization Profile Editing to an agent."
    assert "organization_context" not in exc.value.detail
    _admin_bound_or_403(SimpleNamespace(is_admin=True), ["organization_context"])
    _admin_bound_or_403(SimpleNamespace(is_admin=False), ["web_search"])
    _admin_bound_or_403(SimpleNamespace(is_admin=False), ["organization_context"], before=["organization_context"])


def test_the_built_in_proposal_agents_keep_their_offer():
    from app.services.builtin_tools import offers_organization_proposals

    assert offers_organization_proposals("ctem-assistant", is_builtin=True, caller_is_admin=True)
    assert offers_organization_proposals("platform-health", is_builtin=True, caller_is_admin=True)
    assert not offers_organization_proposals("ctem-assistant", is_builtin=True, caller_is_admin=False)
