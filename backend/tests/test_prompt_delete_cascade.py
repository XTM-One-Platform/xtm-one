import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.sql.selectable import Select

import app.api.v1.prompts as prompts_mod


class _ScalarResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


@pytest.mark.asyncio
async def test_delete_prompt_purges_agent_prompt_ids(monkeypatch):
    """Deleting a prompt must remove its ID from every agent's ``prompt_ids``."""
    prompt_id = uuid.uuid4()
    kept_prompt_id = uuid.uuid4()
    prompt = SimpleNamespace(
        id=prompt_id,
        name="My Prompt",
        is_company_managed=False,
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")
    request = MagicMock()

    affected_agent = SimpleNamespace(prompt_ids=[str(prompt_id), str(kept_prompt_id)])
    untouched_agent = SimpleNamespace(prompt_ids=[str(kept_prompt_id)])
    empty_agent = SimpleNamespace(prompt_ids=[])
    none_agent = SimpleNamespace(prompt_ids=None)

    db = AsyncMock()

    async def _execute(statement):
        if isinstance(statement, Select):
            return _ScalarResult(
                [affected_agent, untouched_agent, empty_agent, none_agent]
            )
        return MagicMock()

    db.execute.side_effect = _execute

    monkeypatch.setattr(
        prompts_mod, "get_manageable_entity", AsyncMock(return_value=prompt)
    )
    audit_log = AsyncMock()
    monkeypatch.setattr(prompts_mod, "log_audit", audit_log)

    await prompts_mod.delete_prompt(
        prompt_id=str(prompt_id),
        request=request,
        user=user,
        db=db,
    )

    # The deleted prompt's ID is gone, the unrelated binding is preserved.
    assert affected_agent.prompt_ids == [str(kept_prompt_id)]
    # Agents that never referenced the prompt are untouched (no spurious writes).
    assert untouched_agent.prompt_ids == [str(kept_prompt_id)]
    assert empty_agent.prompt_ids == []
    assert none_agent.prompt_ids is None

    db.delete.assert_awaited_once_with(prompt)
    db.commit.assert_awaited_once()
    audit_log.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_prompt_forbidden_bubbles_up_without_touching_agents(monkeypatch):
    """A permission failure must abort before any agent mutation/commit."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")
    request = MagicMock()
    db = AsyncMock()

    monkeypatch.setattr(
        prompts_mod,
        "get_manageable_entity",
        AsyncMock(side_effect=HTTPException(status_code=403, detail="nope")),
    )

    with pytest.raises(HTTPException) as exc:
        await prompts_mod.delete_prompt(
            prompt_id=str(uuid.uuid4()),
            request=request,
            user=user,
            db=db,
        )

    assert exc.value.status_code == 403
    db.execute.assert_not_called()
    db.delete.assert_not_called()
    db.commit.assert_not_called()
