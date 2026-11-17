"""Skill-package resources follow their skill onto (and off) an agent.

A packaged skill's ``scripts/`` become Custom Tools and its ``references/``
become KB documents at import time. Attaching such a skill to an agent must
attach those entities too — without them the skill's instructions point at
files the agent cannot reach — and detaching it must take them away again.

The fake session below answers the reconciler's real SELECT statements from
in-memory origin rows, compiling each one against the PostgreSQL dialect first,
so a malformed query fails here instead of only in production.
"""

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.models.agent import Agent
from app.services.skill_package_bindings import (
    reconcile_agent_skill_package_bindings,
)


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def scalars(self):
        return _Rows(row[0] for row in self._rows)


class _FakeDB:
    """Answers the three reconciliation queries from in-memory rows."""

    def __init__(self, origins, *, company_managed_kb_ids=()):
        # origins: iterable of (skill_id, custom_tool_id, knowledge_base_id)
        self.origins = list(origins)
        self.company_managed_kb_ids = set(company_managed_kb_ids)
        self.compiled_sql: list[str] = []

    async def execute(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        params = compiled.params
        self.compiled_sql.append(sql)

        if "knowledge_bases" in sql:
            wanted = set(params.get("id_1", []))
            return _Rows((kb,) for kb in self.company_managed_kb_ids & wanted)

        if "agent_skill_package_origins.skill_id IN" in sql:
            wanted = set(params.get("skill_id_1", []))
            return _Rows(
                (tool_id, kb_id)
                for skill_id, tool_id, kb_id in self.origins
                if skill_id in wanted
            )

        # Package-managed lookup for the agent's currently bound entities.
        tools = set(params.get("custom_tool_id_1", []))
        kbs = set(params.get("knowledge_base_id_1", []))
        return _Rows(
            (tool_id, kb_id)
            for _skill_id, tool_id, kb_id in self.origins
            if (tool_id is not None and tool_id in tools)
            or (kb_id is not None and kb_id in kbs)
        )


def _agent(**kwargs) -> Agent:
    """A detached Agent with the JSON list columns explicitly initialized.

    Column defaults only apply on INSERT, so the reconciler would otherwise see
    ``None`` for every list.
    """
    defaults = {
        "id": uuid.uuid4(),
        "name": "Test agent",
        "skill_ids": [],
        "custom_tool_ids": [],
        "knowledge_base_ids": [],
        "knowledge_base_permissions": None,
        "is_company_managed": False,
    }
    return Agent(**{**defaults, **kwargs})


@pytest.mark.asyncio
async def test_binding_a_skill_pulls_in_its_package_tool_and_kb():
    skill_id, tool_id, kb_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _FakeDB([(skill_id, tool_id, None), (skill_id, None, kb_id)])
    agent = _agent(skill_ids=[str(skill_id)])

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.custom_tool_ids == [str(tool_id)]
    assert agent.knowledge_base_ids == [str(kb_id)]
    assert changes.added_custom_tool_ids == [str(tool_id)]
    assert changes.added_knowledge_base_ids == [str(kb_id)]
    assert changes.changed is True


@pytest.mark.asyncio
async def test_several_reference_files_in_one_kb_bind_it_once():
    skill_id, kb_id = uuid.uuid4(), uuid.uuid4()
    db = _FakeDB([(skill_id, None, kb_id), (skill_id, None, kb_id)])
    agent = _agent(skill_ids=[str(skill_id)])

    await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.knowledge_base_ids == [str(kb_id)]


@pytest.mark.asyncio
async def test_detaching_a_skill_unbinds_its_resources_and_write_grant():
    skill_id, tool_id, kb_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _FakeDB([(skill_id, tool_id, None), (skill_id, None, kb_id)])
    # The skill is gone from skill_ids; its resources are still bound.
    agent = _agent(
        skill_ids=[],
        custom_tool_ids=[str(tool_id)],
        knowledge_base_ids=[str(kb_id)],
        knowledge_base_permissions={str(kb_id): "write"},
    )

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.custom_tool_ids == []
    assert agent.knowledge_base_ids == []
    assert changes.removed_custom_tool_ids == [str(tool_id)]
    assert changes.removed_knowledge_base_ids == [str(kb_id)]
    # A write grant must not survive the KB it pointed at.
    assert agent.knowledge_base_permissions is None


@pytest.mark.asyncio
async def test_resources_of_another_still_bound_skill_are_kept():
    kept_skill, dropped_skill = uuid.uuid4(), uuid.uuid4()
    shared_kb, dropped_kb = uuid.uuid4(), uuid.uuid4()
    db = _FakeDB(
        [
            (kept_skill, None, shared_kb),
            (dropped_skill, None, shared_kb),
            (dropped_skill, None, dropped_kb),
        ]
    )
    agent = _agent(
        skill_ids=[str(kept_skill)],
        knowledge_base_ids=[str(shared_kb), str(dropped_kb)],
    )

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.knowledge_base_ids == [str(shared_kb)]
    assert changes.removed_knowledge_base_ids == [str(dropped_kb)]


@pytest.mark.asyncio
async def test_hand_picked_resources_are_never_unbound():
    skill_id = uuid.uuid4()
    manual_tool, manual_kb = uuid.uuid4(), uuid.uuid4()
    # No origin rows reference these — they were attached by hand.
    db = _FakeDB([(skill_id, uuid.uuid4(), None)])
    agent = _agent(
        skill_ids=[],
        custom_tool_ids=[str(manual_tool)],
        knowledge_base_ids=[str(manual_kb)],
    )

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.custom_tool_ids == [str(manual_tool)]
    assert agent.knowledge_base_ids == [str(manual_kb)]
    assert changes.changed is False


@pytest.mark.asyncio
async def test_company_managed_agent_skips_a_personal_package_kb():
    """Binding it would leave the agent failing its own update validation."""
    skill_id = uuid.uuid4()
    shared_kb, personal_kb, tool_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _FakeDB(
        [
            (skill_id, None, shared_kb),
            (skill_id, None, personal_kb),
            (skill_id, tool_id, None),
        ],
        company_managed_kb_ids=[shared_kb],
    )
    agent = _agent(skill_ids=[str(skill_id)], is_company_managed=True)

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert agent.knowledge_base_ids == [str(shared_kb)]
    assert changes.skipped_knowledge_base_ids == [str(personal_kb)]
    # Custom tools have no company-managed constraint (personal tools are
    # allowed on any agent), so the tool is bound either way.
    assert agent.custom_tool_ids == [str(tool_id)]


@pytest.mark.asyncio
async def test_reconciliation_is_idempotent_and_preserves_order():
    skill_id, tool_id, kb_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    manual_kb = uuid.uuid4()
    db = _FakeDB([(skill_id, tool_id, None), (skill_id, None, kb_id)])
    agent = _agent(
        skill_ids=[str(skill_id)],
        knowledge_base_ids=[str(manual_kb)],
    )

    first = await reconcile_agent_skill_package_bindings(db, agent)
    snapshot = (list(agent.custom_tool_ids), list(agent.knowledge_base_ids))
    second = await reconcile_agent_skill_package_bindings(db, agent)

    assert first.changed is True
    assert second.changed is False
    assert second.as_audit_dict() == {}
    assert (agent.custom_tool_ids, agent.knowledge_base_ids) == snapshot
    # The user's own binding keeps its position; derived ids are appended.
    assert agent.knowledge_base_ids == [str(manual_kb), str(kb_id)]


@pytest.mark.asyncio
async def test_non_uuid_ids_are_ignored_instead_of_crashing():
    skill_id, kb_id = uuid.uuid4(), uuid.uuid4()
    db = _FakeDB([(skill_id, None, kb_id)])
    agent = _agent(
        skill_ids=[str(skill_id), "__not_a_uuid__", None],
        knowledge_base_ids=["", "also-not-a-uuid"],
    )

    await reconcile_agent_skill_package_bindings(db, agent)

    # The junk entry is left alone (it is not a package entity) and the real
    # package KB is still bound.
    assert str(kb_id) in agent.knowledge_base_ids


@pytest.mark.asyncio
async def test_agent_without_skills_issues_no_origin_lookup_for_skills():
    db = _FakeDB([])
    agent = _agent()

    changes = await reconcile_agent_skill_package_bindings(db, agent)

    assert changes.changed is False
    # Nothing bound and no skills: no query is worth issuing.
    assert db.compiled_sql == []


# ---------------------------------------------------------------------------
# GET /skills/package-bindings — what the agent config UI reads
# ---------------------------------------------------------------------------


def test_package_bindings_route_is_matched_before_the_skill_id_route():
    """A literal path declared after ``GET /{skill_id}`` would never be reached."""
    from app.api.v1.agent_skills import router

    paths = [
        route.path
        for route in router.routes
        if "GET" in getattr(route, "methods", set())
    ]
    assert paths.index("/package-bindings") < paths.index("/{skill_id}")


@pytest.mark.asyncio
async def test_package_bindings_lists_deduplicated_entities_per_skill():
    from types import SimpleNamespace

    from app.api.v1.agent_skills import list_skill_package_bindings

    carrier_id, bare_id = uuid.uuid4(), uuid.uuid4()
    tool_id, kb_id = uuid.uuid4(), uuid.uuid4()

    class _DB:
        async def execute(self, statement):
            sql = str(statement.compile(dialect=postgresql.dialect()))
            if "FROM group_members" in sql:
                return _Rows([])
            if "FROM custom_tools" in sql or "JOIN custom_tools" in sql:
                return _Rows([(carrier_id, tool_id)])
            if "FROM knowledge_bases" in sql or "JOIN knowledge_bases" in sql:
                # Two reference files landing in the same KB.
                return _Rows([(carrier_id, kb_id), (carrier_id, kb_id)])
            # Visible skills: one carrying package resources, one carrying none.
            return _Rows([(carrier_id, "Packaged skill"), (bare_id, "Plain skill")])

    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True)
    result = await list_skill_package_bindings(user=user, db=_DB())

    assert len(result) == 1
    entry = result[0]
    assert entry.skill_id == str(carrier_id)
    assert entry.skill_name == "Packaged skill"
    assert entry.custom_tool_ids == [str(tool_id)]
    assert entry.knowledge_base_ids == [str(kb_id)]


# ---------------------------------------------------------------------------
# Package import wiring — the other direction (agents already using the skill)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_package_import_reconciles_only_agents_referencing_the_skill(monkeypatch):
    from types import SimpleNamespace

    from app.api.v1 import agent_skills as agent_skills_api
    from app.services.skill_package_bindings import SkillPackageBindingChanges

    skill = SimpleNamespace(id=uuid.uuid4())
    using_agent = SimpleNamespace(id=uuid.uuid4(), skill_ids=[str(skill.id)])
    # Matches the ``contains`` pre-filter by coincidence (the id appears inside a
    # longer value) but does not actually reference the skill.
    other_agent = SimpleNamespace(id=uuid.uuid4(), skill_ids=[f"{skill.id}-suffix"])
    reconciled = []

    class _Scalars:
        def scalars(self):
            return self

        def all(self):
            return [using_agent, other_agent]

    class _DB:
        async def execute(self, _statement):
            return _Scalars()

    async def _fake_reconcile(_db, agent):
        reconciled.append(agent)
        return SkillPackageBindingChanges(added_knowledge_base_ids=[str(uuid.uuid4())])

    monkeypatch.setattr(
        agent_skills_api, "reconcile_agent_skill_package_bindings", _fake_reconcile
    )

    linked = await agent_skills_api._auto_link_mapped_resources_to_agents(
        _DB(),
        skill=skill,
        resolved_manifest={"references": [{"path": "references/a.md"}]},
    )

    assert reconciled == [using_agent]
    assert linked == [str(using_agent.id)]


@pytest.mark.asyncio
async def test_package_import_without_mapped_resources_does_nothing(monkeypatch):
    from types import SimpleNamespace

    from app.api.v1 import agent_skills as agent_skills_api

    async def _boom(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("no reconciliation expected")

    monkeypatch.setattr(
        agent_skills_api, "reconcile_agent_skill_package_bindings", _boom
    )

    linked = await agent_skills_api._auto_link_mapped_resources_to_agents(
        object(),
        skill=SimpleNamespace(id=uuid.uuid4()),
        resolved_manifest={"scripts": [], "references": []},
    )

    assert linked == []


# ---------------------------------------------------------------------------
# PUT /agents/{id} wiring — the endpoint must apply the reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body_kwargs", "expected_calls"),
    [
        ({"skill_ids": [str(uuid.uuid4())]}, 1),
        ({"knowledge_base_ids": [str(uuid.uuid4())]}, 1),
        ({"custom_tool_ids": [str(uuid.uuid4())]}, 1),
        # An update that cannot change the derived bindings must not pay for a
        # reconciliation pass.
        ({"name": "Renamed"}, 0),
    ],
)
async def test_update_agent_reconciles_when_the_bindings_could_change(
    monkeypatch, body_kwargs, expected_calls
):
    from app.api.v1 import agents as agents_api
    from app.api.v1.schemas.agent import UpdateAgentRequest
    from app.services.skill_package_bindings import SkillPackageBindingChanges
    from tests.test_agent_update_partial_payload import _invoke_update

    derived_kb_id = str(uuid.uuid4())
    calls = []

    async def _fake_reconcile(_db, agent):
        calls.append(agent)
        agent.knowledge_base_ids = [*(agent.knowledge_base_ids or []), derived_kb_id]
        return SkillPackageBindingChanges(added_knowledge_base_ids=[derived_kb_id])

    monkeypatch.setattr(
        agents_api, "reconcile_agent_skill_package_bindings", _fake_reconcile
    )

    updated_agent, _response = await _invoke_update(
        monkeypatch, UpdateAgentRequest(**body_kwargs)
    )

    assert len(calls) == expected_calls
    if expected_calls:
        assert calls[0] is updated_agent
        # Whatever the reconciliation attached is what gets persisted.
        assert derived_kb_id in updated_agent.knowledge_base_ids
