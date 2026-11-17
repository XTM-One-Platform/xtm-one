import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.dml import Delete
from sqlalchemy.sql.selectable import Select

import app.api.v1.agent_skills as agent_skills_mod


class _ScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


@pytest.mark.asyncio
async def test_delete_skill_cascades_package_resources_and_unbinds_agents(monkeypatch):
    skill_id = uuid.uuid4()
    custom_tool_id = uuid.uuid4()
    knowledge_base_id = uuid.uuid4()
    knowledge_document_id = uuid.uuid4()

    skill = SimpleNamespace(
        id=skill_id,
        name="Package Skill",
        tags=[],
        is_company_managed=False,
        url="https://example.com/skill",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    request = MagicMock()

    kept_custom_tool_id = uuid.uuid4()
    kept_knowledge_base_id = uuid.uuid4()
    affected_agent = SimpleNamespace(
        custom_tool_ids=[str(custom_tool_id), str(kept_custom_tool_id)],
        knowledge_base_ids=[str(knowledge_base_id), str(kept_knowledge_base_id)],
        skill_ids=[str(skill_id)],
    )
    untouched_agent = SimpleNamespace(
        custom_tool_ids=[str(kept_custom_tool_id)],
        knowledge_base_ids=[str(kept_knowledge_base_id)],
        skill_ids=[],
    )

    origins = [
        SimpleNamespace(
            custom_tool_id=custom_tool_id,
            knowledge_base_id=knowledge_base_id,
            knowledge_document_id=knowledge_document_id,
        )
    ]

    executed_statements = []
    db = AsyncMock()

    async def _execute(statement):
        executed_statements.append(statement)
        if isinstance(statement, Select):
            table_names = {table.name for table in statement.get_final_froms()}
            if "agent_skill_package_origins" in table_names:
                return _ScalarResult(origins)
            if "agents" in table_names:
                return _ScalarResult([affected_agent, untouched_agent])
        return MagicMock()

    db.execute.side_effect = _execute

    get_manageable_entity = AsyncMock(return_value=skill)
    cancel_jobs = MagicMock()
    audit_log = AsyncMock()

    monkeypatch.setattr(agent_skills_mod, "get_manageable_entity", get_manageable_entity)
    monkeypatch.setattr(agent_skills_mod, "cancel_document_jobs_after_commit", cancel_jobs)
    monkeypatch.setattr(agent_skills_mod, "log_audit", audit_log)

    await agent_skills_mod.delete_skill(
        skill_id=str(skill.id),
        request=request,
        user=user,
        db=db,
    )

    assert affected_agent.custom_tool_ids == [str(kept_custom_tool_id)]
    assert affected_agent.knowledge_base_ids == [str(kept_knowledge_base_id)]
    assert affected_agent.skill_ids == []
    assert untouched_agent.custom_tool_ids == [str(kept_custom_tool_id)]
    assert untouched_agent.knowledge_base_ids == [str(kept_knowledge_base_id)]
    assert untouched_agent.skill_ids == []

    # Registered in the transaction, so the jobs go once the delete commits.
    cancel_jobs.assert_called_once_with(db, {knowledge_document_id})
    db.delete.assert_awaited_once_with(skill)
    db.commit.assert_awaited_once()

    deleted_tables = {
        statement.table.name
        for statement in executed_statements
        if isinstance(statement, Delete)
    }
    assert deleted_tables == {
        "agent_skill_package_origins",
        "knowledge_documents",
        "knowledge_bases",
        "custom_tools",
    }

    # The agent table is scanned exactly once for the whole delete: the
    # preloaded list is shared between the package-origin cascade and the
    # skill_ids purge (regression guard for the double-scan flagged in review).
    agent_scan_count = sum(
        1
        for statement in executed_statements
        if isinstance(statement, Select)
        and "agents" in {table.name for table in statement.get_final_froms()}
    )
    assert agent_scan_count == 1

    audit_details = audit_log.await_args.kwargs["details"]
    assert audit_details["cascade_agent_count"] == 1
    assert audit_details["removed_skill_binding_count"] == 1
    assert audit_details["removed_custom_tool_binding_count"] == 1
    assert audit_details["removed_knowledge_base_binding_count"] == 1
    assert audit_details["deleted_custom_tool_ids"] == [str(custom_tool_id)]
    assert audit_details["deleted_knowledge_base_ids"] == [str(knowledge_base_id)]
    assert audit_details["deleted_knowledge_document_ids"] == [str(knowledge_document_id)]


@pytest.mark.asyncio
async def test_delete_skill_purges_agent_skill_ids(monkeypatch):
    """Deleting a (non-package) skill must remove its ID from agents' ``skill_ids``."""
    skill_id = uuid.uuid4()
    kept_skill_id = uuid.uuid4()
    skill = SimpleNamespace(
        id=skill_id,
        name="Bound Skill",
        tags=[],
        user_id=uuid.uuid4(),
        is_company_managed=False,
        url="https://example.com/skill",
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    request = MagicMock()

    affected_agent = SimpleNamespace(skill_ids=[str(skill_id), str(kept_skill_id)])
    # A corrupted binding list holding the same skill twice must still be fully
    # purged AND have both occurrences counted (regression guard for the audit
    # count counting agents instead of removed entries).
    duplicated_agent = SimpleNamespace(
        skill_ids=[str(skill_id), str(skill_id), str(kept_skill_id)]
    )
    untouched_agent = SimpleNamespace(skill_ids=[str(kept_skill_id)])
    none_agent = SimpleNamespace(skill_ids=None)

    db = AsyncMock()

    async def _execute(statement):
        if isinstance(statement, Select):
            table_names = {table.name for table in statement.get_final_froms()}
            if "agent_skill_package_origins" in table_names:
                return _ScalarResult([])  # no package origins for this skill
            if "agents" in table_names:
                return _ScalarResult(
                    [affected_agent, duplicated_agent, untouched_agent, none_agent]
                )
        return MagicMock()

    db.execute.side_effect = _execute

    get_manageable_entity = AsyncMock(return_value=skill)
    cancel_jobs = MagicMock()
    audit_log = AsyncMock()
    monkeypatch.setattr(agent_skills_mod, "get_manageable_entity", get_manageable_entity)
    monkeypatch.setattr(agent_skills_mod, "cancel_document_jobs_after_commit", cancel_jobs)
    monkeypatch.setattr(agent_skills_mod, "log_audit", audit_log)

    await agent_skills_mod.delete_skill(
        skill_id=str(skill.id),
        request=request,
        user=user,
        db=db,
    )

    assert affected_agent.skill_ids == [str(kept_skill_id)]
    assert duplicated_agent.skill_ids == [str(kept_skill_id)]
    assert untouched_agent.skill_ids == [str(kept_skill_id)]
    assert none_agent.skill_ids is None

    db.delete.assert_awaited_once_with(skill)
    db.commit.assert_awaited_once()
    cancel_jobs.assert_not_called()
    # 1 occurrence from affected_agent + 2 from duplicated_agent.
    assert audit_log.await_args.kwargs["details"]["removed_skill_binding_count"] == 3


@pytest.mark.asyncio
async def test_delete_skill_forbidden_for_non_owner_non_admin(monkeypatch):
    owner_id = uuid.uuid4()
    skill = SimpleNamespace(
        id=uuid.uuid4(),
        name="Shared Skill",
        tags=[],
        user_id=owner_id,
        is_company_managed=False,
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    request = MagicMock()
    db = AsyncMock()

    get_manageable_entity = AsyncMock(return_value=skill)
    monkeypatch.setattr(agent_skills_mod, "get_manageable_entity", get_manageable_entity)

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.delete_skill(
            skill_id=str(skill.id),
            request=request,
            user=user,
            db=db,
        )

    assert exc.value.status_code == 403
    assert exc.value.detail == "Only the creator or an admin can delete this skill"
    db.execute.assert_not_called()
    db.delete.assert_not_called()
    db.commit.assert_not_called()
