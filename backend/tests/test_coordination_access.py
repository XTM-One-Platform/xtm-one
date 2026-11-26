"""An agent may narrow who reads and writes the company-managed rows it governs.

On top of the always-on base rule (``tests/test_company_managed_writes.py``),
the agent that created a company-managed work item, or owns a company-managed
shared-state entry, may narrow which AGENTS read and write it through its
``coordination_access``. The default - no narrowing - is the base rule exactly,
for every existing agent and row. A narrowing only ever takes agents out: a
work item's assignee and claimer are always admitted, a listed agent counts
only while company-managed, and persons, flows and the platform keep the base
rule. No DB: the services' sessions are a fake that serves one row and the
governing agent's policy (the PostgreSQL mirror is in
``test_coordination_access_postgres.py``).
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
import app.services.work_item as wi_service
from app.services.builtin_tools import build_shared_state_tools, build_work_item_tools
from app.services.company_managed_writes import (
    PLATFORM_WRITER,
    CompanyManagedKeyHiddenError,
    CompanyManagedWriteRefusedError,
    CoordinationAccessError,
    agent_writer,
    coordination_access_of,
    coordination_access_payload,
    coordination_admits,
    flow_writer,
    may_hand_to,
    normalize_coordination_access,
    person_writer,
    portable_coordination_access,
)
from app.tools.base import ToolCall

A, B, C, D = (uuid.UUID(f"{n}" * 8 + "-0000-4000-8000-" + f"{n}" * 12) for n in "abcd")
PERSONAL = uuid.UUID("99999999-0000-4000-8000-999999999999")


def _agent(agent_id: uuid.UUID, *, company_managed: bool = True):
    return agent_writer(is_company_managed=company_managed, agent_id=agent_id)


def _policy(read="all", write="all", read_ids=(), write_ids=()):
    return {
        "read": read,
        "write": write,
        "read_agent_ids": [str(i) for i in read_ids],
        "write_agent_ids": [str(i) for i in write_ids],
    }


# ── Fakes ──────────────────────────────────────────────────────────────────────


class _Result:
    def __init__(self, row=None, rows=None):
        self._row = row
        self._rows = rows

    def scalars(self):
        return self

    def first(self):
        return self._row

    def all(self):
        if self._rows is not None:
            return self._rows
        return [self._row] if self._row is not None else []

    def scalar_one_or_none(self):
        return self._row


class _Session:
    """Serves *row* to every query, and *policies* to the governor lookup."""

    def __init__(self, row, policies=None):
        self.row = row
        self.policies = policies or {}
        self.added: list = []
        self.deleted: list = []
        self.committed = False
        self.policy_reads = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        # The workspace tiers look the caller up first; an unknown user has
        # no workspace tier and executes nothing.
        return None

    async def execute(self, statement):
        if str(statement).startswith("SELECT agents.id, agents.coordination_access"):
            self.policy_reads += 1
            return _Result(rows=[(aid, value) for aid, value in self.policies.items()])
        return _Result(self.row)

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


def _use(monkeypatch, row, policies=None) -> _Session:
    session = _Session(row, policies)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    return session


def _item(**over) -> SimpleNamespace:
    now = datetime.now(UTC)
    base = dict(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        owner_agent_id=B,
        created_by_agent_id=A,
        title="Rotate the leaked key",
        description=None,
        payload={},
        status="open",
        priority="high",
        correlation_key=None,
        due_at=None,
        claimed_by_agent_id=None,
        claimed_at=None,
        source="agent",
        result=None,
        last_activity_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _state(**over) -> SimpleNamespace:
    old = datetime.now(UTC) - timedelta(days=1)
    base = dict(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        workspace_id=None,
        key="incident-42",
        label=None,
        owner_agent_id=A,
        status="active",
        value={"stage": "triage"},
        notes=[],
        resolved_at=None,
        created_at=old,
        updated_at=old,
    )
    base.update(over)
    return SimpleNamespace(**base)


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


# ── The stored form ────────────────────────────────────────────────────────────


def test_normalize_the_default_stores_nothing():
    assert normalize_coordination_access(None) is None
    assert normalize_coordination_access({}) is None
    assert normalize_coordination_access(_policy()) is None


def test_normalize_writing_everywhere_means_reading_everywhere():
    assert normalize_coordination_access(_policy(read="self", write="all")) is None


def test_normalize_every_writer_can_read():
    assert normalize_coordination_access(
        _policy(read="self", write="agents", write_ids=[B])
    ) == _policy(read="agents", write="agents", read_ids=[B], write_ids=[B])
    assert normalize_coordination_access(
        _policy(read="agents", write="agents", read_ids=[C], write_ids=[B])
    ) == _policy(read="agents", write="agents", read_ids=[C, B], write_ids=[B])


def test_normalize_the_agent_itself_and_unused_lists_are_dropped():
    assert normalize_coordination_access(
        _policy(read="agents", write="self", read_ids=[B, A, B], write_ids=[C]),
        self_id=A,
    ) == _policy(read="agents", write="self", read_ids=[B])
    assert normalize_coordination_access(
        _policy(read="all", write="self", read_ids=[B])
    ) == _policy(read="all", write="self")


@pytest.mark.parametrize(
    "value",
    [
        {"read": "fleet"},
        {"write": "admins"},
        {"read": "agents", "read_agent_ids": ["not-a-uuid"]},
        {"read": "agents", "read_agent_ids": "a list"},
        {"owner": "self"},
        ["read", "self"],
        {"read": "agents", "read_agent_ids": [str(uuid.uuid4()) for _ in range(101)]},
    ],
)
def test_normalize_refuses_what_it_cannot_store(value):
    with pytest.raises(CoordinationAccessError):
        normalize_coordination_access(value)


def test_a_stored_value_is_read_fail_closed():
    assert not coordination_access_of(None).restricted
    assert not coordination_access_of("garbage").restricted
    assert coordination_access_of({"read": "fleet"}).read == "self"
    assert coordination_access_of({"read": ""}).read == "self"
    assert coordination_access_of({"read": None, "write": "self"}).read == "all"
    assert coordination_access_payload(None) is None
    assert coordination_access_payload({"write": "agents", "write_agent_ids": [str(B), "x"]}) == {
        "read": "all",
        "write": "agents",
        "read_agent_ids": [],
        "write_agent_ids": [str(B)],
    }


def test_a_copy_to_another_instance_keeps_the_scopes_only():
    assert portable_coordination_access(_policy(write="agents", write_ids=[B])) == _policy(
        write="agents"
    )
    assert portable_coordination_access(_policy()) is None


# ── The rule ───────────────────────────────────────────────────────────────────


def _admits(value, actor, *, mode, participants=()):
    return coordination_admits(
        coordination_access_of(value),
        actor,
        mode=mode,
        governor_id=A,
        participants=participants,
    )


@pytest.mark.parametrize("mode", ["read", "write"])
def test_the_default_admits_everyone(mode):
    for actor in (_agent(B), _agent(PERSONAL, company_managed=False), person_writer(is_admin=False)):
        assert _admits(None, actor, mode=mode)


def test_only_this_agent():
    policy = _policy(read="self", write="self")
    assert _admits(policy, _agent(A), mode="write")
    assert not _admits(policy, _agent(B), mode="read")
    assert not _admits(policy, _agent(B), mode="write")


def test_an_agent_acting_for_an_admin_reads_past_any_narrowing_but_writes_within_it() -> None:
    """#2161 (Sam, 25 Nov 2026): reads only; the write narrowing still holds."""
    admin_b = agent_writer(is_company_managed=True, agent_id=B, acting_admin=True)
    for policy in (_policy(read="self", write="self"), _policy(read="agents", write="agents", read_ids=[C])):
        assert _admits(policy, admin_b, mode="read")
        assert not _admits(policy, admin_b, mode="write")
    assert not _admits(_policy(read="self", write="self"), _agent(B), mode="read")


def test_selected_agents_count_only_while_company_managed():
    policy = _policy(read="agents", write="agents", read_ids=[B, PERSONAL], write_ids=[B])
    assert _admits(policy, _agent(B), mode="write")
    assert not _admits(policy, _agent(C), mode="read")
    # Listed, but not (or no longer) company-managed: never admitted.
    assert not _admits(policy, _agent(PERSONAL, company_managed=False), mode="read")
    assert not _admits(policy, _agent(B, company_managed=False), mode="read")


def test_the_assignee_and_the_claimer_are_always_admitted():
    policy = _policy(read="self", write="self")
    assert _admits(policy, _agent(B), mode="write", participants=(B, None))
    assert _admits(policy, _agent(C), mode="write", participants=(B, C))


def test_persons_flows_and_the_platform_are_not_narrowed():
    policy = _policy(read="self", write="self")
    for actor in (person_writer(is_admin=True), flow_writer(is_company_managed=True), PLATFORM_WRITER):
        assert _admits(policy, actor, mode="write")


def test_handing_a_narrowed_row_to_an_agent_it_does_not_admit():
    access = coordination_access_of(_policy(read="agents", write="agents", read_ids=[B], write_ids=[B]))
    assert may_hand_to(access, governor_id=A, target_id=B)
    assert may_hand_to(access, governor_id=A, target_id=A)
    assert may_hand_to(access, governor_id=A, target_id=None)
    assert not may_hand_to(access, governor_id=A, target_id=C)
    assert may_hand_to(coordination_access_of(None), governor_id=A, target_id=C)


# ── Work items: the service gate ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_default_creator_changes_nothing(monkeypatch):
    """No narrowing on the creator: any company-managed agent works the item."""
    row = _item()
    session = _use(monkeypatch, row)
    out = await wi_service.complete_work_item(
        str(row.id), user_id=None, result="done", writer=_agent(C)
    )
    assert out["status"] == "done" and session.committed


@pytest.mark.asyncio
async def test_an_agent_the_creator_does_not_admit_cannot_change_the_item(monkeypatch):
    row = _item()
    session = _use(monkeypatch, row, {A: _policy(read="all", write="agents", write_ids=[D])})
    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only the agents its creator allows can complete this company-managed work item",
    ):
        await wi_service.complete_work_item(
            str(row.id), user_id=None, result="done", writer=_agent(C)
        )
    assert row.status == "open" and not session.committed


@pytest.mark.asyncio
async def test_a_listed_agent_the_assignee_and_the_creator_work_the_item(monkeypatch):
    policy = {A: _policy(read="agents", write="agents", read_ids=[D], write_ids=[D])}
    for actor in (A, B, D):  # creator, assignee, listed
        row = _item()
        _use(monkeypatch, row, policy)
        claim = await wi_service.claim_work_item(
            str(row.id), agent_id=str(actor), user_id=None, writer=_agent(actor)
        )
        assert claim["claimed"] is True, actor


@pytest.mark.asyncio
async def test_the_claimer_finishes_its_claim_whatever_the_creator_changed(monkeypatch):
    row = _item(status="claimed", claimed_by_agent_id=C, owner_agent_id=B)
    _use(monkeypatch, row, {A: _policy(read="self", write="self")})
    out = await wi_service.release_work_item_claim(
        work_item_id=str(row.id), user_id=None, failed=True, writer=_agent(C)
    )
    assert out["status"] == "open"


@pytest.mark.asyncio
async def test_an_item_hidden_from_an_agent_reads_as_not_found(monkeypatch):
    """A narrowed read never tells another agent the item exists."""
    row = _item()
    session = _use(monkeypatch, row, {A: _policy(read="self", write="self")})
    assert await wi_service.update_work_item(
        str(row.id), user_id=None, status="done", writer=_agent(C)
    ) is None
    assert await wi_service.claim_work_item(
        str(row.id), agent_id=str(C), user_id=None, writer=_agent(C)
    ) == {"claimed": False, "reason": "not found"}
    assert await wi_service.delete_work_item(str(row.id), user_id=None, writer=_agent(C)) is False
    assert row.status == "open" and not session.committed and session.deleted == []


@pytest.mark.asyncio
async def test_a_personal_agent_never_sees_a_narrowed_item(monkeypatch):
    row = _item()
    _use(monkeypatch, row, {A: _policy(read="agents", write="self", read_ids=[PERSONAL])})
    assert await wi_service.update_work_item(
        str(row.id), user_id=None, status="done", writer=_agent(PERSONAL, company_managed=False)
    ) is None


@pytest.mark.asyncio
async def test_rerouting_a_narrowed_item_stays_inside_the_creators_writers(monkeypatch):
    policy = {A: _policy(read="agents", write="agents", read_ids=[D], write_ids=[D])}
    row = _item()
    _use(monkeypatch, row, policy)
    with pytest.raises(CompanyManagedWriteRefusedError, match="can be the owner"):
        await wi_service.update_work_item(
            str(row.id), user_id=None, owner_agent_id=str(C), writer=_agent(B)
        )
    assert row.owner_agent_id == B

    out = await wi_service.update_work_item(
        str(row.id), user_id=None, owner_agent_id=str(D), writer=_agent(B)
    )
    assert out["owner_agent_id"] == str(D)

    # The creator routes its item wherever it wants.
    row = _item()
    _use(monkeypatch, row, policy)
    out = await wi_service.update_work_item(
        str(row.id), user_id=None, owner_agent_id=str(C), writer=_agent(A)
    )
    assert out["owner_agent_id"] == str(C)


@pytest.mark.asyncio
async def test_persons_and_flows_keep_the_base_rule_on_a_narrowed_item(monkeypatch):
    for writer in (person_writer(is_admin=True), flow_writer(is_company_managed=True)):
        row = _item()
        session = _use(monkeypatch, row, {A: _policy(read="self", write="self")})
        out = await wi_service.complete_work_item(
            str(row.id), user_id=None, result="done", writer=writer
        )
        assert out["status"] == "done"
        assert session.policy_reads == 0  # nothing to ask: they are not agents


async def _no_cycle(*_a: object, **_kw: object) -> None:
    return None


@pytest.mark.asyncio
async def test_a_private_item_is_never_narrowed(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _item(is_company_managed=False, user_id=uuid.uuid4())
    session = _use(monkeypatch, row, {A: _policy(read="self", write="self")})
    monkeypatch.setattr("app.services.work_item_wait_cycles.refuse_wait_cycle", _no_cycle)
    out = await wi_service.update_work_item(
        str(row.id),
        user_id=str(row.user_id),
        status="blocked",
        blocked_on_refs=[{"repo": "o/r", "kind": "pull_request", "number": 7}],
        writer=_agent(C),
    )
    assert out["status"] == "blocked" and session.policy_reads == 0


# ── Shared state: the service gate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_the_owner_does_not_admit_cannot_change_the_entry(monkeypatch):
    row = _state()
    session = _use(monkeypatch, row, {A: _policy(read="all", write="self")})
    with pytest.raises(
        CompanyManagedWriteRefusedError,
        match="Only the agents its owner agent allows can modify this company-managed shared state",
    ):
        await shared_state_service.upsert_state(
            user_id=None,
            key=row.key,
            value={"stage": "closed"},
            is_company_managed=True,
            writer=_agent(C),
        )
    assert row.value == {"stage": "triage"} and not session.committed

    out = await shared_state_service.upsert_state(
        user_id=None,
        key=row.key,
        value={"stage": "contained"},
        is_company_managed=True,
        writer=_agent(A),
    )
    assert out["value"] == {"stage": "contained"}


@pytest.mark.asyncio
async def test_only_the_owner_hands_a_narrowed_entry_over(monkeypatch):
    policy = {A: _policy(read="agents", write="agents", read_ids=[B], write_ids=[B])}
    row = _state()
    _use(monkeypatch, row, policy)
    with pytest.raises(CompanyManagedWriteRefusedError, match="Only its owner agent can hand over"):
        await shared_state_service.upsert_state(
            user_id=None,
            key=row.key,
            owner_agent_id=str(B),
            is_company_managed=True,
            writer=_agent(B),
        )
    assert row.owner_agent_id == A

    # A listed writer still merges, keeping the owner.
    out = await shared_state_service.upsert_state(
        user_id=None, key=row.key, value={"k": 1}, is_company_managed=True, writer=_agent(B)
    )
    assert out["value"]["k"] == 1 and out["owner_agent_id"] == str(A)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "allowed"),
    [
        (None, True),
        (_policy(read="agents", write="agents", read_ids=[C], write_ids=[C]), True),
        (_policy(read="all", write="self"), False),
    ],
)
async def test_a_new_entry_named_for_another_owner_needs_that_owners_leave(
    monkeypatch, policy, allowed
):
    """An agent cannot plant a company-managed entry in the name of an owner
    that does not admit it as a writer."""
    session = _use(monkeypatch, None, {A: policy} if policy else {})

    async def _create():
        return await shared_state_service.upsert_state(
            user_id=None,
            key="planted",
            value={"approved": True},
            owner_agent_id=str(A),
            acting_agent_id=str(C),
            is_company_managed=True,
            writer=_agent(C),
        )

    if allowed:
        assert (await _create())["owner_agent_id"] == str(A)
    else:
        with pytest.raises(CompanyManagedWriteRefusedError, match="that owner agent allows"):
            await _create()
        assert session.added == []


@pytest.mark.asyncio
async def test_an_entry_on_the_default_rule_is_handed_over_as_before(monkeypatch):
    row = _state()
    _use(monkeypatch, row)
    out = await shared_state_service.upsert_state(
        user_id=None, key=row.key, owner_agent_id=str(C), is_company_managed=True, writer=_agent(B)
    )
    assert out["owner_agent_id"] == str(C)


@pytest.mark.asyncio
async def test_a_hidden_entry_holding_the_key_refuses_without_telling_its_content(monkeypatch):
    """The unique (user, key) slot is held by an entry the agent cannot read:
    refused as a key clash, never merged, never shown."""
    from sqlalchemy.exc import IntegrityError

    hidden = _state(user_id=uuid.uuid4())

    class _Clash(_Session):
        def __init__(self):
            super().__init__(None, {A: _policy(read="self", write="self")})
            self.reads = 0

        async def execute(self, statement):
            sql = str(statement)
            if sql.startswith("SELECT agents.id, agents.coordination_access"):
                return await super().execute(statement)
            if sql.split("FROM", 1)[0].strip() == "SELECT shared_states.id":
                return _Result(hidden.id)  # the slot is taken
            return _Result(None)  # the agent's lookup skips the hidden entry

        async def commit(self):
            if self.added:
                self.added.clear()
                raise IntegrityError("insert", {}, Exception("uq_shared_states_user_key"))
            self.committed = True

    session = _Clash()
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(CompanyManagedKeyHiddenError):
        await shared_state_service.upsert_state(
            user_id=str(hidden.user_id),
            key=hidden.key,
            value={"stage": "mine"},
            is_company_managed=True,
            writer=_agent(C),
        )
    assert hidden.value == {"stage": "triage"}


# ── The tools ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_work_item_tools_word_a_narrowing_refusal(monkeypatch):
    row = _item()
    _use(monkeypatch, row, {A: _policy(read="all", write="self")})
    _, executor = build_work_item_tools(
        None, "anonymous", str(C), is_company_managed=True, company_managed_agent=True
    )
    result = await _call(executor, "complete_work_item", work_item_id=str(row.id))
    assert result.is_error
    assert "Only the agents its creator allows can complete" in result.content
    assert "nothing was written" in result.content

    listed = await _call(executor, "list_work_items")
    assert "read-only for this agent" in listed.content


@pytest.mark.asyncio
async def test_the_work_item_tools_of_the_creator_see_no_read_only_mark(monkeypatch):
    row = _item()
    _use(monkeypatch, row, {A: _policy(read="all", write="self")})
    _, executor = build_work_item_tools(
        None, "anonymous", str(A), is_company_managed=True, company_managed_agent=True
    )
    listed = await _call(executor, "list_work_items")
    assert "read-only" not in listed.content


@pytest.mark.asyncio
async def test_the_shared_state_tools_mark_a_narrowed_entry_read_only(monkeypatch):
    row = _state()
    _use(monkeypatch, row, {A: _policy(read="all", write="self")})
    _, executor = build_shared_state_tools(
        None, "anonymous", str(C), is_company_managed=True, company_managed_agent=True
    )
    read = await _call(executor, "get_shared_state", key=row.key)
    assert "read-only for this agent" in read.content
    refused = await _call(executor, "delete_shared_state", key=row.key)
    assert refused.is_error and "Only the agents its owner agent allows can delete" in refused.content


# ── Progress notes ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_note_goes_on_top_of_the_description_and_keeps_the_rest(monkeypatch):
    row = _item(description="The brief.\n\n2026-11-01 06:00Z: first note.")
    session = _use(monkeypatch, row)
    await wi_service.update_work_item(
        str(row.id), user_id=None, note="  2026-11-01 07:00Z: second note.\n", writer=_agent(B)
    )
    assert row.description == "2026-11-01 07:00Z: second note.\n\nThe brief.\n\n2026-11-01 06:00Z: first note."
    assert session.committed


@pytest.mark.asyncio
async def test_a_note_keeps_the_previous_text_byte_for_byte(monkeypatch):
    row = _item(description="\n  indented brief\n\n")
    _use(monkeypatch, row)
    await wi_service.update_work_item(str(row.id), user_id=None, note="A note.", writer=_agent(B))
    assert row.description == "A note.\n\n\n  indented brief\n\n"


@pytest.mark.asyncio
async def test_a_note_on_an_empty_item_is_the_description(monkeypatch):
    row = _item(description=None)
    _use(monkeypatch, row)
    await wi_service.update_work_item(str(row.id), user_id=None, note="First note.", writer=_agent(B))
    assert row.description == "First note."


@pytest.mark.asyncio
async def test_a_note_with_a_description_is_refused_before_any_write(monkeypatch):
    row = _item(description="The brief.")
    session = _use(monkeypatch, row)
    with pytest.raises(ValueError, match="give one of them"):
        await wi_service.update_work_item(
            str(row.id), user_id=None, note="A note.", description="Another brief.", writer=_agent(B)
        )
    assert row.description == "The brief." and not session.committed


@pytest.mark.asyncio
async def test_a_note_on_an_item_the_writer_cannot_write_is_refused(monkeypatch):
    row = _item(description="The brief.")
    session = _use(monkeypatch, row, {A: _policy(read="all", write="self")})
    with pytest.raises(CompanyManagedWriteRefusedError):
        await wi_service.update_work_item(str(row.id), user_id=None, note="A note.", writer=_agent(C))
    assert row.description == "The brief." and not session.committed


@pytest.mark.asyncio
async def test_the_update_tool_adds_a_note_and_says_so(monkeypatch):
    row = _item(description="The brief.")
    _use(monkeypatch, row)
    _, executor = build_work_item_tools(
        None, "anonymous", str(B), is_company_managed=True, company_managed_agent=True
    )
    result = await _call(executor, "update_work_item", work_item_id=str(row.id), note="A note.")
    assert not result.is_error
    stamp, rest = row.description.split(": ", 1)
    assert rest == "A note.\n\nThe brief."
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z", stamp)
    assert f"Note added on top of the description, stamped {stamp}" in result.content
    stamped = row.description

    refused = await _call(
        executor, "update_work_item", work_item_id=str(row.id), note="Another.", description="New brief."
    )
    assert refused.content.startswith("Error: ") and row.description == stamped


def test_a_stamped_note_starts_with_the_utc_minute_and_the_run():
    now = datetime(2026, 11, 5, 6, 39, 37, tzinfo=timezone(timedelta(hours=1)))
    assert wi_service.stamped_note("Done.", now, "run-1") == "2026-11-05 05:39Z (run run-1): Done."
    assert wi_service.stamped_note("Done.", now) == "2026-11-05 05:39Z: Done."


@pytest.mark.asyncio
async def test_the_service_stamps_a_note_only_when_asked(monkeypatch):
    row = _item(description="The brief.")
    _use(monkeypatch, row)
    await wi_service.update_work_item(str(row.id), user_id=None, note="Plain.", writer=_agent(B))
    assert row.description == "Plain.\n\nThe brief."
    await wi_service.update_work_item(
        str(row.id), user_id=None, note="Stamped.", writer=_agent(B), stamp_note=True, note_run_id="r-9"
    )
    head = row.description.split("\n\n", 1)[0]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}Z \(run r-9\): Stamped\.", head)
