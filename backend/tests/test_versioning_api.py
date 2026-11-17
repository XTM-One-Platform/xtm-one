import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import agents as agents_api
from app.api.v1 import prompts as prompts_api
from app.core.database import get_db


class _FakeDb:
    def __init__(self):
        self.committed = False

    async def commit(self):
        self.committed = True


def _version_item(version_number: int, *, author_id=None, content="text", commit_message=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        version_number=version_number,
        author_id=author_id,
        commit_message=commit_message,
        created_at=datetime.now(UTC),
        content=content,
    )


@pytest.fixture
def fake_user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Test User")


@pytest.fixture
def fake_db():
    return _FakeDb()


@pytest.fixture
def prompts_client(fake_user, fake_db):
    app = FastAPI()
    app.include_router(prompts_api.router, prefix="/api/v1/prompts")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    # Rollback changes an AI resource, so the route now carries the AI
    # gate; this suite asserts the version semantics behind it.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    with TestClient(app) as client:
        yield client


@pytest.fixture
def agents_client(fake_user, fake_db):
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        yield fake_db

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = _get_db
    # Rollback changes an AI resource, so the route now carries the AI
    # gate; this suite asserts the version semantics behind it.
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    with TestClient(app) as client:
        yield client


@pytest.mark.asyncio
async def test_prompt_versions_list_returns_items_and_total(monkeypatch, prompts_client):
    prompt_id = uuid.uuid4()
    author = uuid.uuid4()
    v3 = _version_item(3, author_id=author, content="C", commit_message="third")
    v2 = _version_item(2, author_id=author, content="B", commit_message="second")

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id, content="C")

    async def _list_versions(_db, *, prompt_id, limit, offset):
        assert limit == 2
        assert offset == 1
        assert prompt_id is not None
        return [v3, v2], 4

    async def _resolve_names(_db, _versions):
        return {str(author): "Alice"}

    async def _active_version(_db, *, prompt_id):
        return 3

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "list_prompt_versions", _list_versions)
    monkeypatch.setattr(prompts_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(prompts_api, "get_active_prompt_version_number", _active_version)

    resp = prompts_client.get(f"/api/v1/prompts/{prompt_id}/versions", params={"limit": 2, "offset": 1})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 4
    assert [item["version_number"] for item in data["items"]] == [3, 2]
    assert data["items"][0]["author_name"] == "Alice"


@pytest.mark.asyncio
async def test_prompt_versions_diff_returns_unified_diff(monkeypatch, prompts_client):
    prompt_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id)

    async def _get_version(_db, *, prompt_id, version_number):
        content = "a\nb" if version_number == 1 else "a\nc"
        return SimpleNamespace(version_number=version_number, content=content)

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)

    resp = prompts_client.get(f"/api/v1/prompts/{prompt_id}/versions/diff", params={"from": 1, "to": 2})

    assert resp.status_code == 200
    data = resp.json()
    assert data["from_version"] == 1
    assert data["to_version"] == 2
    assert "--- v1" in data["unified_diff"]
    assert "+ c" in data["unified_diff"] or "+c" in data["unified_diff"]


@pytest.mark.asyncio
async def test_prompt_versions_diff_not_found(monkeypatch, prompts_client):
    prompt_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id)

    async def _get_version(_db, *, prompt_id, version_number):
        return None

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)

    resp = prompts_client.get(
        f"/api/v1/prompts/{prompt_id}/versions/diff",
        params={"from": 1, "to": 2},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_prompt_rollback_creates_new_version_and_commits(monkeypatch, prompts_client, fake_db, fake_user):
    prompt = SimpleNamespace(id=uuid.uuid4(), name="Prompt", content="old")
    target = SimpleNamespace(version_number=2, content="restored")
    captured = {}

    async def _manageable(_db, _model, _user, _prompt_id, **_kwargs):
        return prompt

    async def _get_version(_db, *, prompt_id, version_number):
        assert prompt_id == prompt.id
        assert version_number == 2
        return target

    async def _rollback(_db, *, prompt, target_version, author_id, commit_message):
        captured["author_id"] = author_id
        captured["commit_message"] = commit_message
        return _version_item(2, author_id=author_id, content=target_version.content)

    async def _audit(*_args, **_kwargs):
        return None

    async def _resolve_names(_db, _versions):
        return {str(fake_user.id): "Test User"}

    monkeypatch.setattr(prompts_api, "get_manageable_entity", _manageable)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)
    monkeypatch.setattr(prompts_api, "rollback_prompt_to_version", _rollback)
    monkeypatch.setattr(prompts_api, "log_audit", _audit)
    monkeypatch.setattr(prompts_api, "resolve_author_names", _resolve_names)

    resp = prompts_client.post(
        f"/api/v1/prompts/{prompt.id}/versions/2/rollback",
        json={"commit_message": "rollback now"},
    )

    assert resp.status_code == 200
    assert resp.json()["version_number"] == 2
    assert captured["commit_message"] == "rollback now"
    assert captured["author_id"] == fake_user.id
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_prompt_rollback_denies_when_not_manageable(monkeypatch, prompts_client):
    async def _manageable(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(prompts_api, "get_manageable_entity", _manageable)

    resp = prompts_client.post(
        f"/api/v1/prompts/{uuid.uuid4()}/versions/1/rollback",
        json={"commit_message": "x"},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_prompt_version_detail_success(monkeypatch, prompts_client):
    prompt_id = uuid.uuid4()
    author = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id)

    async def _get_version(_db, *, prompt_id, version_number):
        return _version_item(version_number, author_id=author, content="prompt body")

    async def _resolve_names(_db, _versions):
        return {str(author): "Alice"}

    async def _active_version(_db, *, prompt_id):
        return 4

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)
    monkeypatch.setattr(prompts_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(prompts_api, "get_active_prompt_version_number", _active_version)

    resp = prompts_client.get(f"/api/v1/prompts/{prompt_id}/versions/4")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version_number"] == 4
    assert data["author_name"] == "Alice"
    assert data["content"] == "prompt body"
    # The active head MUST be reported with ``is_active=true`` —
    # callers (notably the prompt-detail "active version" badge) rely
    # on this flag and an earlier revision of the endpoint always
    # returned ``false`` because it never computed the comparison.
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_prompt_version_detail_marks_non_active_versions_as_inactive(
    monkeypatch, prompts_client
):
    """Symmetric coverage for ``test_prompt_version_detail_success`` —
    a request for a non-head version MUST return ``is_active=false``.

    Without this guard, a regression that hardcodes ``is_active=true``
    in ``to_version_detail`` would pass the success test (which only
    checks the active path) but break the prompt-detail "active
    version" badge for every non-active version (it would render every
    historical version as the current head).
    """
    prompt_id = uuid.uuid4()
    author = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id)

    async def _get_version(_db, *, prompt_id, version_number):
        return _version_item(version_number, author_id=author, content="prompt body v2")

    async def _resolve_names(_db, _versions):
        return {str(author): "Alice"}

    async def _active_version(_db, *, prompt_id):
        return 4  # the head is v4, the test asks for v2

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)
    monkeypatch.setattr(prompts_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(prompts_api, "get_active_prompt_version_number", _active_version)

    resp = prompts_client.get(f"/api/v1/prompts/{prompt_id}/versions/2")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version_number"] == 2
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_prompt_version_detail_not_found(monkeypatch, prompts_client):
    prompt_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id)

    async def _get_version(_db, *, prompt_id, version_number):
        return None

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)

    resp = prompts_client.get(f"/api/v1/prompts/{prompt_id}/versions/4")

    assert resp.status_code == 404


def test_prompt_version_detail_rejects_non_positive_version_number(prompts_client):
    resp = prompts_client.get(f"/api/v1/prompts/{uuid.uuid4()}/versions/0")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_prompt_rollback_target_not_found(monkeypatch, prompts_client):
    prompt = SimpleNamespace(id=uuid.uuid4(), name="Prompt", content="old")

    async def _manageable(_db, _model, _user, _prompt_id, **_kwargs):
        return prompt

    async def _get_version(_db, *, prompt_id, version_number):
        return None

    monkeypatch.setattr(prompts_api, "get_manageable_entity", _manageable)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)

    resp = prompts_client.post(
        f"/api/v1/prompts/{prompt.id}/versions/2/rollback",
        json={"commit_message": "rollback now"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_prompt_version_rejects_when_pinned_by_pending_evaluation(
    monkeypatch, prompts_client, fake_db
):
    prompt_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_prompt(_db, _uid, _prompt_id, *_args, **_kwargs):
        return SimpleNamespace(id=prompt_id, name="Prompt")

    async def _get_version(_db, *, prompt_id, version_number):
        return SimpleNamespace(version_number=version_number, is_locked=False)

    async def _active_version(_db, *, prompt_id):
        return 9

    async def _is_pinned(_db, *, prompt_id, version_number):
        return True

    monkeypatch.setattr(prompts_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(prompts_api, "get_user_prompt", _get_prompt)
    monkeypatch.setattr(prompts_api, "get_prompt_version", _get_version)
    monkeypatch.setattr(prompts_api, "get_active_prompt_version_number", _active_version)
    monkeypatch.setattr(prompts_api, "is_prompt_version_pinned_by_evaluation_runs", _is_pinned)

    resp = prompts_client.delete(f"/api/v1/prompts/{prompt_id}/versions/3")

    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "Version is referenced by a pending/running evaluation run"
    )
    assert fake_db.committed is False


@pytest.mark.asyncio
async def test_agent_persona_versions_list_returns_items_and_total(monkeypatch, agents_client):
    agent_id = uuid.uuid4()
    author = uuid.uuid4()
    v2 = _version_item(2, author_id=author, commit_message="v2", content="persona 2")

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id, persona="persona 2")

    async def _list_versions(_db, *, agent_id, limit, offset):
        assert limit == 1
        assert offset == 0
        assert agent_id is not None
        return [v2], 2

    async def _resolve_names(_db, _versions):
        return {str(author): "Bob"}

    async def _active_version(_db, *, agent_id):
        return 2

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "list_agent_persona_versions", _list_versions)
    monkeypatch.setattr(agents_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(agents_api, "get_active_agent_persona_version_number", _active_version)

    resp = agents_client.get(f"/api/v1/agents/{agent_id}/persona/versions", params={"limit": 1, "offset": 0})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    assert data["items"][0]["version_number"] == 2
    assert data["items"][0]["author_name"] == "Bob"


@pytest.mark.asyncio
async def test_agent_persona_rollback_denies_when_not_manageable(monkeypatch, agents_client):
    async def _manageable(*_args, **_kwargs):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(agents_api, "get_manageable_entity", _manageable)

    resp = agents_client.post(
        f"/api/v1/agents/{uuid.uuid4()}/persona/versions/1/rollback",
        json={"commit_message": "x"},
    )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_agent_persona_diff_success(monkeypatch, agents_client):
    agent_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id)

    async def _get_version(_db, *, agent_id, version_number):
        content = "p1\nold" if version_number == 1 else "p1\nnew"
        return _version_item(version_number, content=content)

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)

    resp = agents_client.get(
        f"/api/v1/agents/{agent_id}/persona/versions/diff",
        params={"from": 1, "to": 2},
    )

    assert resp.status_code == 200
    data = resp.json()
    assert data["from_version"] == 1
    assert data["to_version"] == 2
    assert data["added"] == 1
    assert data["removed"] == 1


@pytest.mark.asyncio
async def test_agent_persona_diff_not_found(monkeypatch, agents_client):
    agent_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id)

    async def _get_version(_db, *, agent_id, version_number):
        return None

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)

    resp = agents_client.get(
        f"/api/v1/agents/{agent_id}/persona/versions/diff",
        params={"from": 1, "to": 2},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_agent_persona_version_detail_success(monkeypatch, agents_client):
    agent_id = uuid.uuid4()
    author = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id)

    async def _get_version(_db, *, agent_id, version_number):
        return _version_item(version_number, author_id=author, content="persona")

    async def _resolve_names(_db, _versions):
        return {str(author): "Bob"}

    async def _active_version(_db, *, agent_id):
        return 7

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)
    monkeypatch.setattr(agents_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(agents_api, "get_active_agent_persona_version_number", _active_version)

    resp = agents_client.get(f"/api/v1/agents/{agent_id}/persona/versions/7")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version_number"] == 7
    assert data["author_name"] == "Bob"
    assert data["content"] == "persona"
    # The active head MUST be reported with ``is_active=true``.
    # Mirrors the prompt-version detail contract.
    assert data["is_active"] is True


@pytest.mark.asyncio
async def test_agent_persona_version_detail_marks_non_active_versions_as_inactive(
    monkeypatch, agents_client
):
    """Symmetric coverage for ``test_agent_persona_version_detail_success``
    — a request for a non-head persona version MUST return
    ``is_active=false``.

    A regression that hardcoded ``is_active=true`` in
    ``to_version_detail`` would pass the success test (which only
    covers the active path) but break the agent-detail "active
    persona version" badge for every historical version (it would
    render every past version as the current head).
    """
    agent_id = uuid.uuid4()
    author = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id)

    async def _get_version(_db, *, agent_id, version_number):
        return _version_item(version_number, author_id=author, content="persona v3")

    async def _resolve_names(_db, _versions):
        return {str(author): "Bob"}

    async def _active_version(_db, *, agent_id):
        return 7  # head is v7, test asks for v3

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)
    monkeypatch.setattr(agents_api, "resolve_author_names", _resolve_names)
    monkeypatch.setattr(agents_api, "get_active_agent_persona_version_number", _active_version)

    resp = agents_client.get(f"/api/v1/agents/{agent_id}/persona/versions/3")

    assert resp.status_code == 200
    data = resp.json()
    assert data["version_number"] == 3
    assert data["is_active"] is False


@pytest.mark.asyncio
async def test_agent_persona_version_detail_not_found(monkeypatch, agents_client):
    agent_id = uuid.uuid4()

    async def _groups(_db, _uid):
        return []

    async def _get_agent(_db, _uid, _agent_id, *_args, **_kwargs):
        return SimpleNamespace(id=agent_id)

    async def _get_version(_db, *, agent_id, version_number):
        return None

    monkeypatch.setattr(agents_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(agents_api, "get_user_agent", _get_agent)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)

    resp = agents_client.get(f"/api/v1/agents/{agent_id}/persona/versions/7")

    assert resp.status_code == 404


def test_agent_persona_version_detail_rejects_non_positive_version_number(agents_client):
    resp = agents_client.get(f"/api/v1/agents/{uuid.uuid4()}/persona/versions/0")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_agent_persona_rollback_success(monkeypatch, agents_client, fake_db, fake_user):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", persona="old")
    target = _version_item(2, content="restored")
    captured = {}

    async def _manageable(_db, _model, _user, _agent_id, **_kwargs):
        return agent

    async def _get_version(_db, *, agent_id, version_number):
        return target

    async def _rollback(_db, *, agent, target_version, author_id, commit_message):
        captured["author_id"] = author_id
        captured["commit_message"] = commit_message
        return _version_item(2, author_id=author_id, content="restored")

    async def _audit(*_args, **_kwargs):
        return None

    async def _resolve_names(_db, _versions):
        return {str(fake_user.id): "Test User"}

    monkeypatch.setattr(agents_api, "get_manageable_entity", _manageable)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)
    monkeypatch.setattr(agents_api, "rollback_agent_persona_to_version", _rollback)
    monkeypatch.setattr(agents_api, "log_audit", _audit)
    monkeypatch.setattr(agents_api, "resolve_author_names", _resolve_names)

    resp = agents_client.post(
        f"/api/v1/agents/{agent.id}/persona/versions/2/rollback",
        json={"commit_message": "rollback persona"},
    )

    assert resp.status_code == 200
    assert resp.json()["version_number"] == 2
    assert captured["commit_message"] == "rollback persona"
    assert captured["author_id"] == fake_user.id
    assert fake_db.committed is True


@pytest.mark.asyncio
async def test_agent_persona_rollback_target_not_found(monkeypatch, agents_client):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Agent", persona="old")

    async def _manageable(_db, _model, _user, _agent_id, **_kwargs):
        return agent

    async def _get_version(_db, *, agent_id, version_number):
        return None

    monkeypatch.setattr(agents_api, "get_manageable_entity", _manageable)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)

    resp = agents_client.post(
        f"/api/v1/agents/{agent.id}/persona/versions/2/rollback",
        json={"commit_message": "rollback persona"},
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_agent_persona_version_rejects_when_pinned_by_pending_evaluation(
    monkeypatch, agents_client, fake_db
):
    agent_id = uuid.uuid4()

    class _AgentResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=agent_id, name="Agent")

    async def _execute(_stmt):
        return _AgentResult()

    async def _get_version(_db, *, agent_id, version_number):
        return SimpleNamespace(version_number=version_number, is_locked=False)

    async def _active_version(_db, *, agent_id):
        return 9

    async def _is_pinned(_db, *, agent_id, version_number):
        return True

    monkeypatch.setattr(fake_db, "execute", _execute, raising=False)
    monkeypatch.setattr(agents_api, "get_agent_persona_version", _get_version)
    monkeypatch.setattr(agents_api, "get_active_agent_persona_version_number", _active_version)
    monkeypatch.setattr(
        agents_api,
        "is_agent_persona_version_pinned_by_evaluation_runs",
        _is_pinned,
    )

    resp = agents_client.delete(f"/api/v1/agents/{agent_id}/persona/versions/3")

    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "Version is referenced by a pending/running evaluation run"
    )
    assert fake_db.committed is False


@pytest.mark.asyncio
async def test_delete_agent_config_field_version_rejects_when_pinned_by_pending_evaluation(
    monkeypatch, agents_client, fake_db
):
    agent_id = uuid.uuid4()

    class _AgentResult:
        def scalar_one_or_none(self):
            return SimpleNamespace(id=agent_id, name="Agent")

    async def _execute(_stmt):
        return _AgentResult()

    async def _get_version(_db, *, agent_id, field_name, version_number):
        return SimpleNamespace(version_number=version_number, is_locked=False)

    async def _active_version(_db, *, agent_id, field_name):
        return 9

    async def _is_pinned(_db, *, agent_id, field_name, version_number):
        return True

    monkeypatch.setattr(fake_db, "execute", _execute, raising=False)
    monkeypatch.setattr(agents_api, "get_agent_config_version", _get_version)
    monkeypatch.setattr(agents_api, "get_active_agent_config_version_number", _active_version)
    monkeypatch.setattr(
        agents_api,
        "is_agent_config_version_pinned_by_evaluation_runs",
        _is_pinned,
    )

    resp = agents_client.delete(
        f"/api/v1/agents/{agent_id}/config-versions/temperature/3",
    )

    assert resp.status_code == 409
    assert (
        resp.json()["detail"]
        == "Version is referenced by a pending/running evaluation run"
    )
    assert fake_db.committed is False
