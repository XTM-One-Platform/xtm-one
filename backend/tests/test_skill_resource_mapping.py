"""Unit tests for skill package resource mapping normalization."""

from __future__ import annotations

import json
import os

import pytest
from fastapi import HTTPException

from app.api.v1.agent_skills import (
    _RESOURCE_MAP_PATH,
    _build_python_project_tool_code,
    _merge_manifest_resource,
    _missing_builtin_ids,
    _normalize_resource_mappings,
)
from app.services.skill_package_manager import SkillPackageResource


def test_normalize_resource_mappings_infers_default_entries() -> None:
    resources = [
        SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n"),
        SkillPackageResource(path="references/REFERENCE.md", content=b"# Ref\n"),
    ]

    normalized, warnings = _normalize_resource_mappings(
        skill_name="my-skill", resources=resources
    )

    assert normalized["schema_version"] == 1
    assert warnings == []
    assert normalized["builtins"] == []
    assert len(normalized["scripts"]) == 1
    assert normalized["scripts"][0]["path"] == "scripts/run.py"
    assert normalized["scripts"][0]["tool"]["language"] == "python"
    assert normalized["scripts"][0]["tool"]["workspace_mode"] == "single_file"
    assert len(normalized["references"]) == 1
    assert normalized["references"][0]["path"] == "references/REFERENCE.md"
    assert normalized["references"][0]["knowledge_base"]["name"] == "my-skill-references"


def test_normalize_resource_mappings_rejects_non_string_tool_name() -> None:
    manifest = {
        "schema_version": 1,
        "scripts": [{"path": "scripts/run.py", "tool": {"name": 123}}],
    }
    resources = [
        SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n"),
        SkillPackageResource(
            path=_RESOURCE_MAP_PATH,
            content=json.dumps(manifest).encode("utf-8"),
        ),
    ]

    with pytest.raises(HTTPException, match=r"tool\.name") as exc:
        _normalize_resource_mappings(skill_name="my-skill", resources=resources)
    assert exc.value.status_code == 400


def test_normalize_resource_mappings_rejects_non_string_document_title() -> None:
    manifest = {
        "schema_version": 1,
        "references": [
            {"path": "references/REFERENCE.md", "document": {"title": ["x"]}}
        ],
    }
    resources = [
        SkillPackageResource(path="references/REFERENCE.md", content=b"# Ref\n"),
        SkillPackageResource(
            path=_RESOURCE_MAP_PATH,
            content=json.dumps(manifest).encode("utf-8"),
        ),
    ]

    with pytest.raises(HTTPException, match=r"document\.title") as exc:
        _normalize_resource_mappings(skill_name="my-skill", resources=resources)
    assert exc.value.status_code == 400


def test_normalize_resource_mappings_rejects_invalid_schema_version() -> None:
    manifest = {"schema_version": 2, "builtins": []}
    resources = [
        SkillPackageResource(
            path=_RESOURCE_MAP_PATH,
            content=json.dumps(manifest).encode("utf-8"),
        )
    ]

    with pytest.raises(HTTPException, match="schema_version"):
        _normalize_resource_mappings(skill_name="my-skill", resources=resources)


def test_normalize_resource_mappings_requires_language_for_unknown_script_extension() -> None:
    manifest = {"schema_version": 1, "scripts": [{"path": "scripts/run.sh"}]}
    resources = [
        SkillPackageResource(path="scripts/run.sh", content=b"echo ok\n"),
        SkillPackageResource(
            path=_RESOURCE_MAP_PATH,
            content=json.dumps(manifest).encode("utf-8"),
        ),
    ]

    with pytest.raises(HTTPException, match="Cannot infer language"):
        _normalize_resource_mappings(skill_name="my-skill", resources=resources)


def test_normalize_resource_mappings_skips_non_executable_scripts_in_auto_mode() -> None:
    resources = [
        SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n"),
        SkillPackageResource(path="scripts/schema.xsd", content=b"<xsd/>\n"),
    ]

    normalized, warnings = _normalize_resource_mappings(
        skill_name="my-skill",
        resources=resources,
    )

    assert len(normalized["scripts"]) == 1
    assert normalized["scripts"][0]["path"] == "scripts/run.py"
    assert any("Skipped non-executable script resources" in warning for warning in warnings)


def test_normalize_resource_mappings_detects_python_project_and_uses_project_mode() -> None:
    resources = [
        SkillPackageResource(path="scripts/__init__.py", content=b""),
        SkillPackageResource(
            path="scripts/thumbnail.py",
            content=b"if __name__ == '__main__':\n    print('ok')\n",
        ),
        SkillPackageResource(path="scripts/office/helpers.py", content=b"def x():\n    return 1\n"),
    ]

    normalized, warnings = _normalize_resource_mappings(
        skill_name="my-skill",
        resources=resources,
    )

    assert len(normalized["scripts"]) == 1
    script = normalized["scripts"][0]
    assert script["path"] == "scripts/thumbnail.py"
    assert script["tool"]["language"] == "python"
    assert script["tool"]["workspace_mode"] == "project"
    assert any("Skipped non-executable script resources" in warning for warning in warnings)


def test_normalize_resource_mappings_python_project_defaults_to_single_workspace_tool() -> None:
    resources = [
        SkillPackageResource(path="scripts/__init__.py", content=b""),
        SkillPackageResource(
            path="scripts/main.py",
            content=b"if __name__ == '__main__':\n    print('main')\n",
        ),
        SkillPackageResource(
            path="scripts/thumbnail.py",
            content=b"if __name__ == '__main__':\n    print('thumbnail')\n",
        ),
        SkillPackageResource(
            path="scripts/add_slide.py",
            content=b"if __name__ == '__main__':\n    print('add_slide')\n",
        ),
        SkillPackageResource(path="scripts/office/helpers.py", content=b"def x():\n    return 1\n"),
    ]

    normalized, warnings = _normalize_resource_mappings(
        skill_name="my-skill",
        resources=resources,
    )

    assert len(normalized["scripts"]) == 1
    script = normalized["scripts"][0]
    assert script["path"] == "scripts/main.py"
    assert script["tool"]["language"] == "python"
    assert script["tool"]["workspace_mode"] == "project"
    assert any(
        "Detected Python project workspace with multiple entrypoints" in warning
        for warning in warnings
    )


def test_normalize_resource_mappings_auto_maps_singular_reference_directory() -> None:
    resources = [
        SkillPackageResource(path="reference/NOTES.md", content=b"# Notes\n"),
    ]

    normalized, warnings = _normalize_resource_mappings(
        skill_name="my-skill", resources=resources
    )

    assert warnings == []
    assert len(normalized["references"]) == 1
    assert normalized["references"][0]["path"] == "reference/NOTES.md"


def test_build_python_project_tool_code_rejects_oversized_workspace(monkeypatch) -> None:
    monkeypatch.setattr("app.api.v1.agent_skills._MAX_WORKSPACE_ARCHIVE_BYTES", 64)

    resources = [
        SkillPackageResource(path="scripts/__init__.py", content=b""),
        SkillPackageResource(path="scripts/main.py", content=os.urandom(4096)),
    ]

    with pytest.raises(HTTPException, match="maximum embedded archive size"):
        _build_python_project_tool_code(
            entrypoint_path="scripts/main.py",
            workspace_resources=resources,
        )


def test_missing_builtin_ids_reports_unavailable_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.api.v1.agent_skills._build_builtin_id_set",
        lambda: {"web_fetch", "code_interpreter"},
    )
    manifest = {
        "schema_version": 1,
        "builtins": [{"id": "web_fetch"}, {"id": "nonexistent_tool"}],
    }

    assert _missing_builtin_ids(manifest) == ["nonexistent_tool"]
    assert _missing_builtin_ids({"schema_version": 1, "builtins": []}) == []


@pytest.mark.asyncio
async def test_materialized_entities_inherit_skill_visibility(monkeypatch) -> None:
    """Custom Tools and KBs materialized from a shared/company skill must
    inherit its visibility — runtime tool loading is ID-based, so other
    users' agents can execute these tools and need to SEE them too."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock
    from uuid import uuid4

    from app.api.v1.agent_skills import _materialize_mapped_resources

    monkeypatch.setattr(
        "app.api.v1.agent_skills._resolve_unique_name",
        AsyncMock(side_effect=lambda _db, *, model_cls, name: name),
    )
    monkeypatch.setattr("app.api.v1.agent_skills._build_builtin_id_set", set)

    group_id, deleted_group_id = str(uuid4()), str(uuid4())
    added: list = []
    db = AsyncMock()
    db.add = MagicMock(side_effect=added.append)
    # The group lookup of ``normalize_group_grants``: the skill's
    # second group was deleted since, so the copies leave it out.
    db.execute = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[(group_id,)])))

    user = SimpleNamespace(id=uuid4())
    grantee_id = str(uuid4())
    skill = SimpleNamespace(
        name="shared-skill",
        is_company_managed=True,
        shared_group_ids=[group_id, deleted_group_id],
        shared_group_permissions={group_id: "read_only", deleted_group_id: "config"},
        # Per-user grants inherit exactly like the group ones — a skill
        # shared with one person must not materialize resources that person
        # cannot see.
        shared_user_ids=[grantee_id],
        shared_user_permissions={grantee_id: "read_only"},
        access_restricted=True,
    )
    resources = [
        SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n"),
        SkillPackageResource(path="references/REF.md", content=b"# Ref\n"),
    ]
    manifest = {
        "schema_version": 1,
        "builtins": [],
        "scripts": [
            {
                "path": "scripts/run.py",
                "tool": {
                    "name": "shared-skill-run",
                    "description": "d",
                    "language": "python",
                    "input_schema": {"type": "object", "properties": {}},
                    "workspace_mode": "single_file",
                },
            }
        ],
        "references": [
            {
                "path": "references/REF.md",
                "knowledge_base": {"name": "shared-skill-references", "description": "d"},
                "document": {"title": "REF"},
            }
        ],
    }

    await _materialize_mapped_resources(
        db, user=user, skill=skill, resources=resources, manifest=manifest
    )

    from app.models.custom_tool import CustomTool
    from app.models.knowledge_base import KnowledgeBase

    tools = [e for e in added if isinstance(e, CustomTool)]
    kbs = [e for e in added if isinstance(e, KnowledgeBase)]
    assert len(tools) == 1 and len(kbs) == 1
    for entity in (*tools, *kbs):
        assert entity.is_company_managed is True
        assert entity.user_id is None
        assert entity.shared_group_ids == [group_id]
        assert entity.shared_group_permissions == {group_id: "read_only"}
        assert entity.shared_user_ids == [grantee_id]
        assert entity.shared_user_permissions == {grantee_id: "read_only"}
        # The restriction rides along too: inheriting the audience but not the
        # narrowing would publish the resources company-wide while the skill
        # itself stayed scoped.
        assert entity.access_restricted is True


def test_source_type_for_filename_matches_upload_mapping() -> None:
    from app.services.kb_helpers import source_type_for_filename

    assert source_type_for_filename("REFERENCE.md") == "markdown"
    assert source_type_for_filename("data.json") == "json"
    assert source_type_for_filename("table.csv") == "csv"
    assert source_type_for_filename("notes.txt") == "text"
    assert source_type_for_filename("no_extension") == "text"


def test_merge_manifest_resource_replaces_existing_mapping_file() -> None:
    resources = [
        SkillPackageResource(path=_RESOURCE_MAP_PATH, content=b"{}"),
        SkillPackageResource(path="scripts/run.py", content=b"print('ok')\n"),
    ]
    manifest = {"schema_version": 1, "builtins": [], "scripts": [], "references": []}

    merged = _merge_manifest_resource(resources, manifest=manifest)

    mapping_resources = [r for r in merged if r.path == _RESOURCE_MAP_PATH]
    assert len(mapping_resources) == 1
    decoded = json.loads(mapping_resources[0].content.decode("utf-8"))
    assert decoded["schema_version"] == 1
