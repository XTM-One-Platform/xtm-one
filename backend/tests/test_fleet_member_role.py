"""A fleet member's role label set by tool, and the role finding computed in code.

Pinned here, without a database: ``set_fleet_member_parent`` with ``role_label`` writes the label
through ``update_member`` (the service of ``PATCH /fleets/{id}/members/{id}``),
audits the old and the new label and answers both; an empty string clears it;
a fleet the caller cannot manage, a non-member and a label over 100 characters
are refused with nothing saved. ``member_without_role`` is computed from the
configuration: every home member with an empty label, whatever the fleet's
size, and a regeneration drops it once the label is set.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.models.fleet import FLEET_ROLE_MEMBER
from app.services import fleet_doc_generation as gen
from app.services.builtin_tools import fleets as fleet_tools
from app.services.fleet_doc_findings import role_findings, with_role_findings
from app.services.fleet_docs import DOC_SECTIONS

LABEL = "Root orchestrator for threat research and correlation"


class _Session:
    def __init__(self, names: dict | None = None) -> None:
        self.commits = 0
        self.rollbacks = 0
        self.names = names or {}

    async def execute(self, _statement):
        # The agent-name read of the answer: (id, name) rows.
        return SimpleNamespace(all=lambda: list(self.names.items()), scalars=lambda: SimpleNamespace(all=lambda: []))

    async def get(self, _model, _id):
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


@pytest.fixture
def stubs(monkeypatch):
    from app.services import fleets as fleet_service

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    agent_id = uuid.uuid4()
    db = _Session({agent_id: "Research EA"})
    row = SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=agent_id,
        role=FLEET_ROLE_MEMBER,
        role_label="",
        parent_member_id=None,
        organization_scope=None,
        is_exposed=False,
    )
    fleet = SimpleNamespace(
        id=uuid.uuid4(),
        name="Research",
        slug="research",
        is_company_managed=True,
        is_builtin=False,
        is_archived=False,
        description="",
        orchestrator_agent_id=agent_id,
        workspace_id=uuid.uuid4(),
        organization_scope=None,
        documentation=None,
    )
    state = SimpleNamespace(db=db, user=user, agent_id=agent_id, row=row, fleet=fleet, audits=[], updates=[])

    @asynccontextmanager
    async def _factory():
        yield db

    async def _load(_db, _uid):
        return user

    async def _manageable(_db, _model, _uid, _entity_uuid, **_kw):
        return fleet, None

    async def _members(_db, _fleet_id):
        return [row]

    async def _update(_db, _fleet, member_id, *, fields):
        state.updates.append((member_id, fields))
        row.role_label = fields["role_label"]
        return row

    async def _audit(_db, **kw):
        state.audits.append(kw)

    monkeypatch.setattr(fleet_tools.database, "async_session", _factory)
    monkeypatch.setattr(fleet_tools._builder, "_load_caller_user_for_builder", _load)
    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _manageable)
    monkeypatch.setattr(fleet_service, "fleet_members", _members)
    monkeypatch.setattr(fleet_service, "update_member", _update)
    monkeypatch.setattr("app.services.audit.log_audit", _audit)
    return state


async def _call(state, **args) -> str:
    _defs, executor = fleet_tools.build_fleet_tools(str(state.user.id))
    return await executor.handler("set_fleet_member_parent")(fleet_id=str(state.fleet.id), **args)


@pytest.mark.asyncio
async def test_the_label_is_set_through_the_member_service_audited_and_read_back(stubs) -> None:
    answer = await _call(stubs, agent_id=str(stubs.agent_id), role_label=f"  {LABEL}  ")
    assert f'role label of Research EA `{stubs.agent_id}` set from none to "{LABEL}"' in answer
    assert stubs.updates == [(stubs.row.id, {"role_label": LABEL})]
    [audit] = stubs.audits
    assert audit["details"]["role_label"] == LABEL
    assert audit["details"]["previous_role_label"] == ""
    assert audit["details"]["via"] == "agent"
    assert stubs.db.commits == 1
    # The answer is the fleet as get_fleet reads it, the label on the member line.
    assert f"role label: {LABEL}" in answer


@pytest.mark.asyncio
async def test_an_empty_label_clears_it(stubs) -> None:
    stubs.row.role_label = LABEL
    answer = await _call(stubs, agent_id=str(stubs.agent_id), role_label="")
    assert f'set from "{LABEL}" to none' in answer
    assert stubs.row.role_label == ""


@pytest.mark.asyncio
async def test_a_fleet_the_caller_cannot_manage_is_refused_nothing_saved(stubs, monkeypatch) -> None:
    async def _refuse(_db, _model, _uid, _entity_uuid, *, action="modify", entity_label="entity", lock=False):
        return None, f"You don't have permission to {action} this {entity_label}."

    monkeypatch.setattr(fleet_tools._builder, "_load_manageable_entity_for_builder", _refuse)
    answer = await _call(stubs, agent_id=str(stubs.agent_id), role_label=LABEL)
    assert answer == "You don't have permission to edit this fleet."
    assert stubs.updates == [] and stubs.audits == []
    assert (stubs.db.commits, stubs.db.rollbacks) == (0, 1)


@pytest.mark.asyncio
async def test_a_non_member_and_a_long_label_are_refused(stubs) -> None:
    other = uuid.uuid4()
    answer = await _call(stubs, agent_id=str(other), role_label=LABEL)
    assert answer == f"agent `{other}` is not a home member of this fleet. Nothing was saved."
    answer = await _call(stubs, agent_id=str(stubs.agent_id), role_label="x" * 101)
    assert answer == "role_label is 101 characters, at most 100. Nothing was saved."
    assert stubs.updates == []


def test_the_tool_is_documented_and_wants_the_label() -> None:
    definitions, _executor = fleet_tools.build_fleet_tools("")
    tool = next(d for d in definitions if d.name == "set_fleet_member_parent")
    assert "empty clears it" in tool.input_schema["properties"]["role_label"]["description"]
    assert tool.input_schema["required"] == ["fleet_id", "agent_id"]
    assert "role label" in tool.description


@pytest.mark.asyncio
async def test_the_label_alone_keeps_the_parent(stubs) -> None:
    parent_row = uuid.uuid4()
    stubs.row.parent_member_id = parent_row
    await _call(stubs, agent_id=str(stubs.agent_id), role_label=LABEL)
    assert stubs.updates == [(stubs.row.id, {"role_label": LABEL})]
    assert stubs.row.parent_member_id == parent_row


# ── the member_without_role rule ────────────────────────────────────────────


def _members(count: int, *, labelled: int = 0) -> list[dict]:
    return [{"name": f"Member {i}", "role_label": "Does X" if i < labelled else ""} for i in range(count)]


def test_one_unlabelled_member_and_five_get_the_same_treatment() -> None:
    one = role_findings(_members(1))
    five = role_findings(_members(5))
    assert [f["member"] for f in one] == ["Member 0"]
    assert [f["member"] for f in five] == [f"Member {i}" for i in range(5)]
    assert {f["kind"] for f in one + five} == {"member_without_role"}
    # A labelled member gets none; the id holds whatever the sentence.
    assert [f["member"] for f in role_findings(_members(3, labelled=2))] == ["Member 2"]
    assert role_findings(_members(1))[0]["id"] == one[0]["id"]


def test_a_model_role_finding_is_replaced_by_the_computed_set() -> None:
    model = [
        {"kind": "member_without_role", "member": "", "finding": "Members have no role label."},
        {"kind": "other", "member": "Member 0", "finding": "Something else."},
    ]
    from app.services.fleet_doc_findings import clean_findings

    out = with_role_findings(clean_findings(model), _members(2, labelled=1))
    assert [(f["kind"], f["member"]) for f in out] == [("other", "Member 0"), ("member_without_role", "Member 1")]


class _Model:
    async def chat(self, messages, *, model, temperature, max_tokens):
        body = {
            "sections": {k: f"Text of {k}." for k in DOC_SECTIONS},
            "structured": {"purpose_line": "Runs research."},
            "findings": [{"kind": "member_without_role", "member": "", "finding": "Members have no role label."}],
        }
        return SimpleNamespace(content=json.dumps(body), stop_reason="end_turn")


def _fleet() -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Research",
        description="",
        skills=[],
        is_builtin=False,
        documentation=None,
        orchestrator_agent_id=None,
        default_context_inclusion="none",
        default_collaboration_instruction="",
        organization_scope={"mode": "primary"},
        autonomy_level="full_gates",
        approval_gates=[],
    )


@pytest.mark.asyncio
async def test_a_regeneration_drops_the_finding_once_the_label_is_set(monkeypatch) -> None:
    from app.services import fleet_docs

    members = _members(1)

    async def _ctx(_db, _fleet):
        return {"members": members, "automations": [], "approvals": {}}

    async def _fp(_db, _fleet):
        return "f" * 16

    async def _member_prints(_db, _fleet):
        # Unchanged prints: the stored set would be kept, the role finding is recomputed anyway.
        return {"": "fleet-1", "member 0": "m-1"}

    monkeypatch.setattr(gen, "load_draft_context", _ctx)
    monkeypatch.setattr(fleet_docs, "current_fingerprint", _fp)
    monkeypatch.setattr(fleet_docs, "current_member_fingerprints", _member_prints)

    fleet = _fleet()
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model())
    assert [(f["kind"], f["member"]) for f in fleet.documentation["findings"]] == [("member_without_role", "Member 0")]

    members[0]["role_label"] = LABEL
    await gen.regenerate_fleet_documentation(object(), uuid.uuid4(), fleet, llm=_Model())
    assert not fleet.documentation.get("findings")
