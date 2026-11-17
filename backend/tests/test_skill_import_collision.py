from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import Response

import app.api.v1.agent_skills as agent_skills_mod


def _response_stub() -> Response:
    """Mimic FastAPI's injected ``Response``: its status_code starts unset so
    the route decorator default (201) applies unless the endpoint overrides."""
    response = Response()
    response.status_code = None  # type: ignore[assignment]
    return response


class _ScalarAllResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return list(self._values)


class _UploadStub:
    def __init__(self, filename: str, payload: bytes):
        self.filename = filename
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


@pytest.mark.asyncio
async def test_import_package_handles_duplicate_name_matches_without_multiple_results(monkeypatch):
    now = datetime.now(UTC)
    existing_a = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="desc",
        compatibility=None,
        license=None,
        content="old",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=[],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=False,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )
    existing_b = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="desc",
        compatibility=None,
        license=None,
        content="old",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=[],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=False,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )

    parsed = SimpleNamespace(
        name="pptx",
        description="new desc",
        compatibility=None,
        license=None,
        root_content="# skill",
        resources=[],
        versions=[],
        warnings=[],
    )

    user = SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")
    request = MagicMock()
    upload = _UploadStub(filename="pptx.zip", payload=b"PK")

    db = AsyncMock()
    execute_calls = {"count": 0}

    async def _execute(_statement):
        execute_calls["count"] += 1
        if execute_calls["count"] == 1:
            return _ScalarAllResult([existing_a, existing_b])
        return MagicMock()

    db.execute.side_effect = _execute

    monkeypatch.setattr(agent_skills_mod, "parse_skill_import_upload", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        agent_skills_mod,
        "get_manageable_entity",
        AsyncMock(return_value=existing_a),
    )
    cleanup_mock = AsyncMock(return_value={})
    monkeypatch.setattr(
        agent_skills_mod, "_cleanup_package_origin_entities", cleanup_mock
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kwargs: ({"schema_version": 1, "builtins": [], "scripts": [], "references": []}, []),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_materialize_mapped_resources",
        AsyncMock(return_value=({"schema_version": 1, "builtins": [], "scripts": [], "references": []}, [])),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_auto_link_mapped_resources_to_agents",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(agent_skills_mod, "_merge_manifest_resource", lambda resources, *, manifest: resources)
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_resources", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_package_origins", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_versions", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "log_audit", AsyncMock())

    response = _response_stub()
    result = await agent_skills_mod.import_skill_package(
        request=request,
        response=response,
        file=upload,
        on_collision="replace",
        new_name=None,
        user=user,
        db=db,
    )

    assert result.id == str(existing_a.id)
    # Replace updates an existing skill — the endpoint must override the
    # route's default 201 with 200 so clients can distinguish the outcomes.
    assert response.status_code == 200
    assert execute_calls["count"] >= 1
    # Replace imports must clean up entities materialized by the previous
    # import before re-materializing, so tools/KBs never accumulate.
    cleanup_mock.assert_awaited_once_with(db, skill_id=existing_a.id)


@pytest.mark.asyncio
async def test_as_new_with_blank_new_name_falls_back_to_auto_suffix(monkeypatch):
    """A whitespace-only ``new_name`` must be treated as absent — an as_new
    import must never create a skill with an empty name."""
    now = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="desc",
        compatibility=None,
        license=None,
        content="old",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=[],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=False,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )
    parsed = SimpleNamespace(
        name="pptx",
        description="new desc",
        compatibility=None,
        license=None,
        root_content="# skill",
        resources=[],
        versions=[],
        warnings=[],
    )
    user = SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")
    request = MagicMock()
    upload = _UploadStub(filename="pptx.zip", payload=b"PK")

    db = AsyncMock()
    execute_calls = {"count": 0}

    async def _execute(_statement):
        execute_calls["count"] += 1
        if execute_calls["count"] == 1:
            return _ScalarAllResult([existing])
        return MagicMock()

    db.execute.side_effect = _execute

    monkeypatch.setattr(
        agent_skills_mod, "parse_skill_import_upload", lambda *_a, **_kw: parsed
    )
    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    resolve_name = AsyncMock(return_value="pptx-imported")
    monkeypatch.setattr(agent_skills_mod, "_resolve_import_name", resolve_name)
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kw: (
            {"schema_version": 1, "builtins": [], "scripts": [], "references": []},
            [],
        ),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_materialize_mapped_resources",
        AsyncMock(
            return_value=(
                {"schema_version": 1, "builtins": [], "scripts": [], "references": []},
                [],
            )
        ),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_auto_link_mapped_resources_to_agents",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        agent_skills_mod, "_merge_manifest_resource", lambda resources, *, manifest: resources
    )
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_resources", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_package_origins", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_versions", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "log_audit", AsyncMock())

    created_names: list[str] = []

    def _capture_to_response(skill, **_kwargs):
        # The unflushed ORM instance has no DB-generated timestamps, so a
        # stub response is returned instead of the real serializer.
        created_names.append(skill.name)
        return SimpleNamespace(id=str(skill.id), name=skill.name)

    monkeypatch.setattr(agent_skills_mod, "_to_response", _capture_to_response)

    response = _response_stub()
    await agent_skills_mod.import_skill_package(
        request=request,
        response=response,
        file=upload,
        on_collision="as_new",
        new_name="   ",
        user=user,
        db=db,
    )

    resolve_name.assert_awaited_once()
    assert created_names == ["pptx-imported"]
    # as_new creates a fresh skill — the status must stay unset so the
    # route decorator's 201 applies.
    assert response.status_code is None


@pytest.mark.asyncio
async def test_as_new_rename_rewrites_frontmatter_in_content_and_head_version(monkeypatch):
    """When as_new renames the skill, the stored SKILL.md frontmatter and
    the head version snapshot must carry the NEW name — exports derive the
    package root from the frontmatter name, so a stale value would make the
    exported package collide with the original skill on re-import."""
    now = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="desc",
        compatibility=None,
        license=None,
        content="old",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=[],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=False,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )
    original_md = "---\nname: pptx\ndescription: d\n---\n\nBody"
    old_snapshot = "---\nname: pptx\ndescription: d\n---\n\nOld body"
    parsed = SimpleNamespace(
        name="pptx",
        description="d",
        compatibility=None,
        license=None,
        root_content=original_md,
        resources=[],
        versions=[
            SimpleNamespace(number=1, content=old_snapshot),
            SimpleNamespace(number=2, content=original_md),
        ],
        warnings=[],
    )
    user = SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")
    request = MagicMock()
    upload = _UploadStub(filename="pptx.zip", payload=b"PK")

    db = AsyncMock()
    execute_calls = {"count": 0}

    async def _execute(_statement):
        execute_calls["count"] += 1
        if execute_calls["count"] == 1:
            return _ScalarAllResult([existing])
        return MagicMock()

    db.execute.side_effect = _execute

    monkeypatch.setattr(
        agent_skills_mod, "parse_skill_import_upload", lambda *_a, **_kw: parsed
    )
    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        agent_skills_mod, "_resolve_import_name", AsyncMock(return_value="pptx-imported")
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kw: (
            {"schema_version": 1, "builtins": [], "scripts": [], "references": []},
            [],
        ),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_materialize_mapped_resources",
        AsyncMock(
            return_value=(
                {"schema_version": 1, "builtins": [], "scripts": [], "references": []},
                [],
            )
        ),
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_auto_link_mapped_resources_to_agents",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        agent_skills_mod, "_merge_manifest_resource", lambda resources, *, manifest: resources
    )
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_resources", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_package_origins", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "_replace_skill_versions", AsyncMock())
    monkeypatch.setattr(agent_skills_mod, "log_audit", AsyncMock())

    created_contents: list[str] = []

    def _capture_to_response(skill, **_kwargs):
        created_contents.append(skill.content)
        return SimpleNamespace(id=str(skill.id), name=skill.name)

    monkeypatch.setattr(agent_skills_mod, "_to_response", _capture_to_response)

    await agent_skills_mod.import_skill_package(
        request=request,
        response=_response_stub(),
        file=upload,
        on_collision="as_new",
        new_name=None,
        user=user,
        db=db,
    )

    # Root content and the head version snapshot carry the renamed
    # frontmatter; the older snapshot keeps its original name (history).
    assert "name: pptx-imported" in created_contents[0]
    assert "name: pptx\n" not in created_contents[0]
    assert "name: pptx-imported" in parsed.versions[1].content
    assert parsed.versions[0].content == old_snapshot


@pytest.mark.asyncio
async def test_replace_import_refuses_builtin_skills(monkeypatch):
    """``on_collision=replace`` must not overwrite a built-in (seeded) skill —
    same immutability guard as update_skill / delete_skill."""
    from fastapi import HTTPException

    now = datetime.now(UTC)
    builtin = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="seeded",
        compatibility=None,
        license=None,
        content="seeded content",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=["builtin"],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=True,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )
    parsed = SimpleNamespace(
        name="pptx",
        description="imported",
        compatibility=None,
        license=None,
        root_content="# skill",
        resources=[],
        versions=[],
        warnings=[],
    )
    user = SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")
    request = MagicMock()
    upload = _UploadStub(filename="pptx.zip", payload=b"PK")

    db = AsyncMock()
    db.execute.return_value = _ScalarAllResult([builtin])

    monkeypatch.setattr(
        agent_skills_mod, "parse_skill_import_upload", lambda *_a, **_kw: parsed
    )
    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=builtin)
    )
    cleanup_mock = AsyncMock(return_value={})
    monkeypatch.setattr(
        agent_skills_mod, "_cleanup_package_origin_entities", cleanup_mock
    )

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.import_skill_package(
            request=request,
            response=_response_stub(),
            file=upload,
            on_collision="replace",
            new_name=None,
            user=user,
            db=db,
        )

    assert exc.value.status_code == 403
    assert "Built-in skills cannot be replaced" in exc.value.detail
    cleanup_mock.assert_not_awaited()
    db.commit.assert_not_awaited()
    assert builtin.content == "seeded content"


@pytest.mark.asyncio
async def test_replace_import_validation_failure_skips_cleanup(monkeypatch):
    """A package that fails validation (e.g. missing built-in tool IDs) must
    NOT trigger the replace-cleanup — otherwise a doomed import would tear
    down the previous import's tools/KBs and agent bindings."""
    from fastapi import HTTPException

    now = datetime.now(UTC)
    existing = SimpleNamespace(
        id=uuid4(),
        user_id=None,
        name="pptx",
        description="desc",
        compatibility=None,
        license=None,
        content="old",
        url=None,
        fetched_at=None,
        refresh_failure_count=0,
        auth_failure_count=0,
        source_integration_id=None,
        source_ref=None,
        tags=[],
        enabled=True,
        hide_from_chat=False,
        forked_from=None,
        is_company_managed=False,
        is_public_catalog=False,
        shared_group_ids=None,
        shared_group_permissions=None,
        shared_user_ids=None,
        shared_user_permissions=None,
        access_restricted=False,
        created_at=now,
        updated_at=now,
    )
    parsed = SimpleNamespace(
        name="pptx",
        description="new desc",
        compatibility=None,
        license=None,
        root_content="# skill",
        resources=[],
        versions=[],
        warnings=[],
    )
    user = SimpleNamespace(id=uuid4(), is_admin=True, role="admin", full_name="Tester")
    request = MagicMock()
    upload = _UploadStub(filename="pptx.zip", payload=b"PK")

    db = AsyncMock()
    db.execute.return_value = _ScalarAllResult([existing])

    monkeypatch.setattr(
        agent_skills_mod, "parse_skill_import_upload", lambda *_a, **_kw: parsed
    )
    monkeypatch.setattr(agent_skills_mod, "get_user_group_ids", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        agent_skills_mod, "get_manageable_entity", AsyncMock(return_value=existing)
    )
    monkeypatch.setattr(
        agent_skills_mod,
        "_normalize_resource_mappings",
        lambda **_kw: (
            {
                "schema_version": 1,
                "builtins": [{"id": "nonexistent_tool"}],
                "scripts": [],
                "references": [],
            },
            [],
        ),
    )
    monkeypatch.setattr(agent_skills_mod, "_build_builtin_id_set", lambda: {"web_fetch"})
    cleanup_mock = AsyncMock(return_value={})
    monkeypatch.setattr(
        agent_skills_mod, "_cleanup_package_origin_entities", cleanup_mock
    )

    with pytest.raises(HTTPException) as exc:
        await agent_skills_mod.import_skill_package(
            request=request,
            response=_response_stub(),
            file=upload,
            on_collision="replace",
            new_name=None,
            user=user,
            db=db,
        )

    assert exc.value.status_code == 400
    assert "nonexistent_tool" in exc.value.detail
    cleanup_mock.assert_not_awaited()
    db.commit.assert_not_awaited()
