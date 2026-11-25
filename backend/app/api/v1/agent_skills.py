# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Agent Skills API – manage reusable skill definitions (agentskills.io spec).

Skills are top-level shared entities (like Integrations, MCP Servers, KBs).
Users create skills, optionally mark them company-managed, and agents reference
them via ``skill_ids``.

Skills can be created in three ways:
1. **By URL** – fetch a SKILL.md, parse YAML frontmatter, cache content.
2. **By pasting content** – provide markdown/text directly in the request body.
3. **By uploading a file** – POST a ``.md`` / ``.txt`` file via multipart.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import mimetypes
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import Text, cast, delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_ai_enabled
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    can_manage,
    enqueue_document_processing,
    get_manageable_entity,
    get_user_group_ids,
    get_user_integration,
    handle_company_managed_toggle,
    list_scope_admin_bypass,
    normalize_group_grants,
    parse_path_id,
    require_admin_for_company_managed,
    resolve_owner_names,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.agent_skill import (
    AgentSkillCreate,
    AgentSkillResponse,
    AgentSkillUpdate,
    BulkSkillCandidateResponse,
    BulkSkillImportOutcome,
    IgnoredArchiveEntryResponse,
    SkillBulkImportResponse,
    SkillBulkPackageValidationResponse,
    SkillFork,
    SkillForkResponse,
    SkillLicenseResponse,
    SkillPackageBindingsResponse,
    SkillPackageValidationResponse,
    SkillTier3ToolLinkResponse,
)
from app.api.v1.schemas.versioning import (
    RollbackRequest,
    VersionDetailResponse,
    VersionDiffResponse,
    VersionListItemResponse,
    VersionListResponse,
)
from app.api.v1.versioning_helpers import to_version_detail, to_version_item
from app.core.database import get_db
from app.core.utils import build_content_disposition, fit_copy_name, text_column_error
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill, is_internal_skill
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.agent_skill_resource import AgentSkillResource
from app.models.custom_tool import CustomTool
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.skill_version import SkillVersion
from app.models.user import User
from app.services.audit import log_audit
from app.services.builtin_tools.skills import is_licence_file
from app.services.integration_credentials import (
    canonical_integration_ids,
    require_oauth_credentials_usable,
)
from app.services.kb_helpers import (
    cancel_document_jobs_after_commit,
    source_type_for_filename,
)
from app.services.skill_package_bindings import (
    reconcile_agent_skill_package_bindings,
)
from app.services.skill_package_manager import (
    ParsedSkillPackage,
    SkillPackageResource,
    SkillPackageVersion,
    build_skill_package_zip,
    complete_package_frontmatter,
    derive_package_root_name,
    parse_skill_bulk_package_zip,
    parse_skill_import_upload,
    parse_skill_markdown,
    rewrite_frontmatter_name,
    sha256_hex,
    validate_frontmatter_warnings,
)
from app.services.skill_source import (
    GITHUB_SKILL_PROVIDER,
    SKILL_CONTENT_MAX_BYTES,
    SkillCredential,
    SkillCredentialError,
    fetch_skill_content,
    parse_skill_frontmatter,
    resolve_skill_credential,
)
from app.services.skill_versioning import (
    build_version_diff,
    create_skill_version,
    get_active_skill_version_number,
    get_skill_version,
    is_skill_version_pinned_by_evaluation_runs,
    list_skill_versions,
    resolve_author_names,
    rollback_skill_to_version,
)

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Skill packages and workspace resources
# ---------------------------------------------------------------------------

# Cap for the compressed scripts/ archive embedded (base64) in workspace tool
# code — protects DB row size, LLM payloads and runtime memory.
_MAX_WORKSPACE_ARCHIVE_BYTES = 10 * 1024 * 1024  # 10 MB
_RESOURCE_MAP_PATH = "assets/xtm-resource-map.json"
_AUTO_SCRIPT_EXTENSIONS = {".py", ".js", ".mjs", ".cjs"}
_AUTO_REFERENCE_TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".rst",
    ".json",
    ".yaml",
    ".yml",
    ".csv",
    ".html",
    ".xml",
}


async def _resolve_source_credential(
    db: AsyncSession,
    user: User,
    integration_id: str | None,
    url: str | None,
    *,
    newly_bound: bool = True,
) -> SkillCredential | None:
    """Validate a skill's GitHub credential binding and return the credential.

    Returns ``None`` when no integration is bound — the skill is then fetched
    anonymously, exactly as every URL-based skill was.

    Every rejection is raised as a 4xx here rather than being discovered on the
    next hourly refresh: the integration must be visible to the caller, be a
    *connected GitHub* integration, and the URL must be a GitHub repository
    file URL (attaching a GitHub token to another host would leak it).

    RBAC: anyone who may create or edit the skill may bind a credential, but
    only one they can already see (own, group-shared, or company-managed) and,
    for a personal OAuth token, one they own or whose owner shared it —
    the same contract KB sync sources use for their ``integration_id``, so no
    new way to reach a credential is introduced. Admins keep binding what
    they can see. The token check applies to a credential
    this request binds (*newly_bound*); one the skill already holds, re-read
    for a URL change, and every later refresh use it as it stands.
    """
    if not integration_id:
        return None
    if not url:
        raise HTTPException(
            status_code=422,
            detail=(
                "A source credential can only be attached to a URL-based skill. "
                "Provide a 'url' pointing at a SKILL.md file, or omit "
                "'source_integration_id'."
            ),
        )

    integration = await get_user_integration(
        db,
        user.id,
        integration_id,
        is_admin=user.is_admin,
    )
    if integration.provider != GITHUB_SKILL_PROVIDER:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Integration '{integration.name}' is a {integration.provider} "
                "integration. Skill sources can only authenticate against GitHub."
            ),
        )
    if integration.status != "connected":
        raise HTTPException(
            status_code=422,
            detail=(
                f"GitHub integration '{integration.name}' is not connected "
                f"(status: {integration.status}). Re-authorize it in Integrations "
                "before attaching it to a skill."
            ),
        )

    if newly_bound:
        require_oauth_credentials_usable(integration, user.id, is_admin=user.is_admin)

    try:
        credential = await resolve_skill_credential(db, integration.id)
    except SkillCredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return credential


# ---------------------------------------------------------------------------
# Response helper
# ---------------------------------------------------------------------------


def _to_response(
    skill: AgentSkill, *, can_manage: bool, owner_name: str | None = None
) -> AgentSkillResponse:
    # The export fills a missing frontmatter name/description from the skill's
    # own properties, so readiness is judged with them as the fallback.
    fm_warnings = validate_frontmatter_warnings(
        skill.content,
        fallback_name=skill.name,
        fallback_description=skill.description,
    )
    return AgentSkillResponse(
        id=str(skill.id),
        user_id=str(skill.user_id) if skill.user_id else None,
        owner_name=owner_name,
        url=skill.url,
        name=skill.name,
        description=skill.description,
        compatibility=skill.compatibility,
        license=skill.license,
        content=skill.content,
        fetched_at=skill.fetched_at.isoformat() if skill.fetched_at else None,
        refresh_failure_count=skill.refresh_failure_count or 0,
        source_integration_id=(
            str(skill.source_integration_id) if skill.source_integration_id else None
        ),
        source_ref=skill.source_ref,
        auth_failure_count=skill.auth_failure_count or 0,
        tags=skill.tags or [],
        enabled=skill.enabled,
        hide_from_chat=skill.hide_from_chat,
        is_company_managed=skill.is_company_managed,
        is_public_catalog=skill.is_public_catalog,
        shared_group_ids=skill.shared_group_ids,
        shared_group_permissions=skill.shared_group_permissions,
        shared_user_ids=skill.shared_user_ids,
        shared_user_permissions=skill.shared_user_permissions,
        access_restricted=skill.access_restricted,
        can_manage=can_manage,
        package_exportable=len(fm_warnings) == 0,
        frontmatter_warnings=fm_warnings,
        forked_from=skill.forked_from if isinstance(skill.forked_from, dict) else None,
        created_at=skill.created_at.isoformat(),
        updated_at=skill.updated_at.isoformat(),
    )


async def _list_skill_resources(
    db: AsyncSession, *, skill_id
) -> list[AgentSkillResource]:
    result = await db.execute(
        select(AgentSkillResource)
        .where(AgentSkillResource.skill_id == skill_id)
        .order_by(AgentSkillResource.path.asc())
    )
    return list(result.scalars().all())


async def _replace_skill_resources(
    db: AsyncSession,
    *,
    skill_id,
    resources: list[SkillPackageResource],
) -> None:
    await db.execute(
        delete(AgentSkillResource).where(AgentSkillResource.skill_id == skill_id)
    )
    for resource in resources:
        content_type = mimetypes.guess_type(resource.path)[0]
        db.add(
            AgentSkillResource(
                skill_id=skill_id,
                path=resource.path,
                content=resource.content,
                content_type=content_type,
                size=len(resource.content),
                sha256=sha256_hex(resource.content),
            )
        )


async def _replace_skill_versions(
    db: AsyncSession,
    *,
    skill_id,
    versions: list[SkillPackageVersion],
) -> None:
    await db.execute(delete(SkillVersion).where(SkillVersion.skill_id == skill_id))
    for version in sorted(versions, key=lambda v: v.number):
        created_at = None
        if version.created_at:
            try:
                created_at = datetime.fromisoformat(version.created_at.replace("Z", "+00:00"))
            except ValueError:
                created_at = None
        db.add(
            SkillVersion(
                skill_id=skill_id,
                version_number=version.number,
                content=version.content,
                description=version.description,
                compatibility=version.compatibility,
                author_id=None,
                commit_message="Imported from skill package",
                created_at=created_at or datetime.now(UTC),
            )
        )


async def _replace_skill_package_origins(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    resolved_manifest: dict,
) -> None:
    await db.execute(
        delete(AgentSkillPackageOrigin).where(
            AgentSkillPackageOrigin.skill_id == skill_id
        )
    )

    for entry in resolved_manifest.get("scripts", []):
        tool = entry.get("tool") if isinstance(entry, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        custom_tool_id = tool.get("custom_tool_id") if isinstance(tool, dict) else None
        if not isinstance(path, str) or not isinstance(custom_tool_id, str):
            continue
        db.add(
            AgentSkillPackageOrigin(
                skill_id=skill_id,
                resource_kind="script",
                resource_path=path,
                custom_tool_id=uuid.UUID(custom_tool_id),
            )
        )

    for entry in resolved_manifest.get("references", []):
        kb_meta = entry.get("knowledge_base") if isinstance(entry, dict) else None
        doc_meta = entry.get("document") if isinstance(entry, dict) else None
        path = entry.get("path") if isinstance(entry, dict) else None
        knowledge_base_id = (
            kb_meta.get("knowledge_base_id") if isinstance(kb_meta, dict) else None
        )
        knowledge_document_id = (
            doc_meta.get("document_id") if isinstance(doc_meta, dict) else None
        )
        if (
            not isinstance(path, str)
            or not isinstance(knowledge_base_id, str)
            or not isinstance(knowledge_document_id, str)
        ):
            continue
        db.add(
            AgentSkillPackageOrigin(
                skill_id=skill_id,
                resource_kind="reference",
                resource_path=path,
                knowledge_base_id=uuid.UUID(knowledge_base_id),
                knowledge_document_id=uuid.UUID(knowledge_document_id),
            )
        )

    # Flush so the provenance rows are queryable by the binding reconciliation
    # that runs next, without depending on session autoflush.
    await db.flush()


async def _resolve_import_name(
    db: AsyncSession,
    *,
    preferred_name: str,
) -> str:
    """Generate a non-conflicting skill name by suffixing ``-imported-N``."""
    column = AgentSkill.__table__.c.name
    candidate = fit_copy_name(preferred_name, column, suffix="-imported")
    idx = 2
    while True:
        exists = await db.execute(
            select(AgentSkill.id).where(AgentSkill.name == candidate).limit(1)
        )
        if exists.scalar_one_or_none() is None:
            return candidate
        candidate = fit_copy_name(preferred_name, column, suffix=f"-imported-{idx}")
        idx += 1


async def _resolve_unique_name(
    db: AsyncSession,
    *,
    model_cls,
    name: str,
) -> str:
    """Return a unique entity name by appending ``-imported-N`` suffix."""
    candidate = name.strip()
    idx = 2
    while True:
        exists = await db.execute(select(model_cls.id).where(model_cls.name == candidate).limit(1))
        if exists.scalar_one_or_none() is None:
            return candidate
        candidate = fit_copy_name(
            name.strip(), model_cls.__table__.c.name, suffix=f"-imported-{idx}"
        )
        idx += 1


def _find_resource_by_path(
    resources: list[SkillPackageResource],
    *,
    path: str,
) -> SkillPackageResource | None:
    for resource in resources:
        if resource.path == path:
            return resource
    return None


def _to_text_resource(resource: SkillPackageResource, *, label: str) -> str:
    try:
        return resource.content.decode("utf-8")
    except UnicodeDecodeError as err:
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be UTF-8 text: {resource.path}",
        ) from err


def _default_tool_name(skill_name: str, path: str) -> str:
    stem = PurePosixPath(path).stem.strip().lower().replace("_", "-")
    stem = stem or "script"
    return fit_copy_name(f"{skill_name}-{stem}", CustomTool.__table__.c.name)


def _default_document_title(path: str) -> str:
    stem = PurePosixPath(path).stem.strip()
    return stem or PurePosixPath(path).name


def _frontmatter_name(meta: dict[str, str]) -> str | None:
    """The SKILL.md frontmatter ``name``, refused when ``AgentSkill.name`` cannot hold it.

    The name a skill declares for itself is written as the skill's name, so
    one too long for the column was a 500 on the write, never a shorter name.
    """
    name = meta.get("name") or None
    if name and (
        error := text_column_error(
            AgentSkill.__table__.c.name, name, field="The SKILL.md frontmatter name"
        )
    ):
        raise HTTPException(status_code=400, detail=error)
    return name


def _manifest_str(
    value: object, *, fallback: str, field: str, path: str, column: Any = None
) -> str:
    """Coerce an optional manifest string field, rejecting non-string values.

    A malformed ``assets/xtm-resource-map.json`` (e.g. ``{"name": 123}``)
    must produce a clean 400 validation error instead of an
    ``AttributeError`` 500 from ``.strip()``. Blank values fall back to the
    generated default like missing ones. A value longer than the ``String(n)``
    *column* it is written to is refused the same way; the fallback is the
    caller's, fitted to that column already.
    """
    if value is None:
        return fallback
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid {_RESOURCE_MAP_PATH}: `{field}` must be a string for {path}."
            ),
        )
    text = value.strip()
    if column is not None and (error := text_column_error(column, text, field=f"`{field}`")):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {_RESOURCE_MAP_PATH}: {error.removesuffix('.')} for {path}.",
        )
    return text or fallback


def _select_default_python_entrypoint(paths: list[str]) -> str:
    """Select a deterministic default entrypoint for Python workspaces.

    Preference order:
    1. main.py
    2. run.py
    3. alphabetical first path
    """
    if not paths:
        raise ValueError("No Python entrypoint candidates provided.")
    for preferred in ("main.py", "run.py"):
        for path in paths:
            if PurePosixPath(path).name == preferred:
                return path
    return sorted(paths)[0]


def _build_python_project_tool_code(
    *,
    entrypoint_path: str,
    workspace_resources: list[SkillPackageResource],
) -> str:
    """Build a Python tool wrapper that rehydrates the full scripts workspace.

    The wrapper unpacks an embedded ZIP into a temp folder, injects that folder
    into ``sys.path``, and executes the requested entrypoint module.
    """
    archive_buf = io.BytesIO()
    with zipfile.ZipFile(archive_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for resource in sorted(workspace_resources, key=lambda item: item.path):
            if not resource.path.startswith("scripts/"):
                continue
            zf.writestr(resource.path, resource.content)
    archive_bytes = archive_buf.getvalue()
    if len(archive_bytes) > _MAX_WORKSPACE_ARCHIVE_BYTES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Python workspace for entrypoint '{entrypoint_path}' exceeds the "
                "maximum embedded archive size of "
                f"{_MAX_WORKSPACE_ARCHIVE_BYTES // (1024 * 1024)} MB "
                f"({len(archive_bytes) // (1024 * 1024)} MB compressed). "
                "Reduce the scripts/ payload or split the package."
            ),
        )
    archive_b64 = base64.b64encode(archive_bytes).decode("ascii")
    entry_module = (
        entrypoint_path[:-3].replace("/", ".")
        if entrypoint_path.endswith(".py")
        else ""
    )
    return (
        "import base64\n"
        "import io\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "import tempfile\n"
        "import zipfile\n"
        f"_XTM_ARCHIVE_B64 = {archive_b64!r}\n"
        f"_XTM_ENTRYPOINT = {entrypoint_path!r}\n"
        f"_XTM_ENTRY_MODULE = {entry_module!r}\n"
        "def _xtm_run_workspace():\n"
        "    inputs = globals().get('__xtm_tool_inputs__', {})\n"
        "    with tempfile.TemporaryDirectory(prefix='xtm_skill_workspace_') as tmp_dir:\n"
        "        payload = base64.b64decode(_XTM_ARCHIVE_B64.encode('ascii'))\n"
        "        with zipfile.ZipFile(io.BytesIO(payload), 'r') as zf:\n"
        "            zf.extractall(tmp_dir)\n"
        "        entrypoint = os.path.join(tmp_dir, _XTM_ENTRYPOINT)\n"
        "        if not os.path.isfile(entrypoint):\n"
        "            raise RuntimeError(f'Entrypoint not found in workspace: {_XTM_ENTRYPOINT}')\n"
        "        argv = inputs.get('argv') if isinstance(inputs, dict) else None\n"
        "        if isinstance(argv, list):\n"
        "            sys.argv = [entrypoint, *[str(v) for v in argv]]\n"
        "        else:\n"
        "            sys.argv = [entrypoint]\n"
        "        old_cwd = os.getcwd()\n"
        "        try:\n"
        "            os.chdir(tmp_dir)\n"
        "            sys.path.insert(0, tmp_dir)\n"
        "            if _XTM_ENTRY_MODULE:\n"
        "                try:\n"
        "                    runpy.run_module(_XTM_ENTRY_MODULE, run_name='__main__', alter_sys=True)\n"
        "                    return\n"
        "                except (ImportError, ModuleNotFoundError):\n"
        "                    pass\n"
        "            runpy.run_path(entrypoint, run_name='__main__')\n"
        "        finally:\n"
        "            os.chdir(old_cwd)\n"
        "            if sys.path and sys.path[0] == tmp_dir:\n"
        "                sys.path.pop(0)\n"
        "_xtm_run_workspace()\n"
    )


def _normalize_resource_mappings(
    *,
    skill_name: str,
    resources: list[SkillPackageResource],
) -> tuple[dict, list[str]]:
    """Load and normalize package resource mappings from ``assets/xtm-resource-map.json``.

    If the mapping file is absent, infer defaults from ``scripts/*`` and
    ``references/*`` resources.
    """
    available_paths = {resource.path for resource in resources}
    mapping_resource = _find_resource_by_path(resources, path=_RESOURCE_MAP_PATH)
    raw_manifest: dict | None = None
    if mapping_resource is not None:
        text = _to_text_resource(mapping_resource, label="Mapping manifest")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as err:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: {err.msg}",
            ) from err
        if not isinstance(decoded, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: root must be a JSON object.",
            )
        schema_version = decoded.get("schema_version")
        if schema_version != 1:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported {_RESOURCE_MAP_PATH} schema_version (expected 1).",
            )
        raw_manifest = decoded

    warnings: list[str] = []
    normalized: dict = {
        "schema_version": 1,
        "builtins": [],
        "scripts": [],
        "references": [],
    }

    # Built-ins mapping
    raw_builtins = []
    if raw_manifest is not None:
        raw_builtins = raw_manifest.get("builtins") or []
        if not isinstance(raw_builtins, list):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: `builtins` must be an array.",
            )
    for entry in raw_builtins:
        if isinstance(entry, str):
            tool_id = entry
        elif isinstance(entry, dict) and isinstance(entry.get("id"), str):
            tool_id = entry["id"]
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: each built-in entry must be a string or an object with `id`.",
            )
        normalized["builtins"].append({"id": tool_id.strip()})

    # Scripts mapping
    script_paths = sorted(p for p in available_paths if p.startswith("scripts/"))
    python_script_paths = [
        path
        for path in script_paths
        if PurePosixPath(path).suffix.lower() == ".py"
    ]
    has_python_project = (
        any(PurePosixPath(path).name == "__init__.py" for path in python_script_paths)
        and len(python_script_paths) > 1
    )

    raw_scripts = []
    scripts_explicit = raw_manifest is not None and "scripts" in raw_manifest
    if raw_manifest is not None and "scripts" in raw_manifest:
        raw_scripts = raw_manifest.get("scripts") or []
        if not isinstance(raw_scripts, list):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: `scripts` must be an array.",
            )
    else:
        inferred_script_paths: list[str] = []
        skipped_script_paths: list[str] = []
        if has_python_project:
            candidate_entrypoints = [
                path
                for path in python_script_paths
                if path.count("/") == 1
                and PurePosixPath(path).name != "__init__.py"
                and not PurePosixPath(path).name.startswith("_")
            ]
            executable_candidates: list[str] = []
            for path in candidate_entrypoints:
                resource = _find_resource_by_path(resources, path=path)
                if resource and b"__main__" in resource.content:
                    executable_candidates.append(path)

            resolved_candidates = executable_candidates or candidate_entrypoints
            if resolved_candidates:
                default_entrypoint = _select_default_python_entrypoint(
                    resolved_candidates
                )
                inferred_script_paths.append(default_entrypoint)
                if len(resolved_candidates) > 1:
                    warnings.append(
                        "Detected Python project workspace with multiple entrypoints; "
                        f"defaulted to a single workspace tool on {default_entrypoint}. "
                        f"Add {_RESOURCE_MAP_PATH} scripts entries to expose additional entrypoints."
                    )

            for path in script_paths:
                if path not in inferred_script_paths:
                    skipped_script_paths.append(path)
        else:
            for path in script_paths:
                ext = PurePosixPath(path).suffix.lower()
                if ext in _AUTO_SCRIPT_EXTENSIONS:
                    inferred_script_paths.append(path)
                else:
                    skipped_script_paths.append(path)
        raw_scripts = [{"path": p} for p in inferred_script_paths]
        if skipped_script_paths:
            warnings.append(
                "Skipped non-executable script resources: "
                + ", ".join(skipped_script_paths)
            )

    mapped_script_paths: set[str] = set()
    for entry in raw_scripts:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: each script entry must include string `path`.",
            )
        path = entry["path"].strip()
        if not path.startswith("scripts/"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: script path must be under scripts/: {path}",
            )
        if path not in available_paths:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: mapped script file not found: {path}",
            )
        tool_meta = entry.get("tool") if isinstance(entry.get("tool"), dict) else {}
        language = tool_meta.get("language")
        if not isinstance(language, str) or language not in {"javascript", "python"}:
            ext = PurePosixPath(path).suffix.lower()
            language = "javascript" if ext in {".js", ".mjs", ".cjs"} else "python" if ext == ".py" else None
        if language is None:
            if not scripts_explicit:
                warnings.append(
                    f"Skipped script mapping for unsupported file extension: {path}"
                )
                continue
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Cannot infer language for mapped script '{path}'. "
                    "Provide tool.language as 'javascript' or 'python'."
                ),
            )
        input_schema = tool_meta.get("input_schema")
        if input_schema is not None and not isinstance(input_schema, dict):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: tool.input_schema must be a JSON object for {path}.",
            )
        workspace_mode = tool_meta.get("workspace_mode")
        if workspace_mode is not None and workspace_mode not in {
            "single_file",
            "project",
        }:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid {_RESOURCE_MAP_PATH}: tool.workspace_mode must be "
                    f"'single_file' or 'project' for {path}."
                ),
            )
        if language == "python":
            if workspace_mode is None:
                workspace_mode = "project" if has_python_project else "single_file"
        else:
            workspace_mode = "single_file"
        normalized["scripts"].append(
            {
                "path": path,
                "tool": {
                    "name": _manifest_str(
                        tool_meta.get("name"),
                        fallback=_default_tool_name(skill_name, path),
                        field="tool.name",
                        path=path,
                        column=CustomTool.__table__.c.name,
                    ),
                    "description": _manifest_str(
                        tool_meta.get("description"),
                        fallback=f"Imported from {path}",
                        field="tool.description",
                        path=path,
                    ),
                    "language": language,
                    "input_schema": input_schema or {"type": "object", "properties": {}},
                    "workspace_mode": workspace_mode,
                },
            }
        )
        mapped_script_paths.add(path)

    # Ensure scripts not explicitly listed still get a mapping by default.
    if not scripts_explicit and not has_python_project:
        for path in sorted(
            p
            for p in available_paths
            if p.startswith("scripts/") and p not in mapped_script_paths
        ):
            ext = PurePosixPath(path).suffix.lower()
            language = (
                "javascript"
                if ext in {".js", ".mjs", ".cjs"}
                else "python" if ext == ".py" else None
            )
            if language is None:
                continue
            workspace_mode = (
                "project" if language == "python" and has_python_project else "single_file"
            )
            normalized["scripts"].append(
                {
                    "path": path,
                    "tool": {
                        "name": _default_tool_name(skill_name, path),
                        "description": f"Imported from {path}",
                        "language": language,
                        "input_schema": {"type": "object", "properties": {}},
                        "workspace_mode": workspace_mode,
                    },
                }
            )

    # References mapping
    raw_references = []
    references_explicit = raw_manifest is not None and "references" in raw_manifest
    if raw_manifest is not None and "references" in raw_manifest:
        raw_references = raw_manifest.get("references") or []
        if not isinstance(raw_references, list):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: `references` must be an array.",
            )
    else:
        inferred_reference_paths: list[str] = []
        skipped_reference_paths: list[str] = []
        for path in sorted(
            p
            for p in available_paths
            if p.startswith("references/") or p.startswith("reference/")
        ):
            ext = PurePosixPath(path).suffix.lower()
            if ext in _AUTO_REFERENCE_TEXT_EXTENSIONS:
                inferred_reference_paths.append(path)
            else:
                skipped_reference_paths.append(path)
        raw_references = [{"path": p} for p in inferred_reference_paths]
        if skipped_reference_paths:
            warnings.append(
                "Skipped non-text reference resources during auto-mapping: "
                + ", ".join(skipped_reference_paths[:8])
                + (" ..." if len(skipped_reference_paths) > 8 else "")
            )

    mapped_reference_paths: set[str] = set()
    default_kb_name = fit_copy_name(skill_name, KnowledgeBase.__table__.c.name, suffix="-references")
    for entry in raw_references:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: each reference entry must include string `path`.",
            )
        path = entry["path"].strip()
        if not (path.startswith("references/") or path.startswith("reference/")):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: reference path must be under references/: {path}",
            )
        if path not in available_paths:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid {_RESOURCE_MAP_PATH}: mapped reference file not found: {path}",
            )
        kb_meta = entry.get("knowledge_base") if isinstance(entry.get("knowledge_base"), dict) else {}
        doc_meta = entry.get("document") if isinstance(entry.get("document"), dict) else {}
        normalized["references"].append(
            {
                "path": path,
                "knowledge_base": {
                    "name": _manifest_str(
                        kb_meta.get("name"),
                        fallback=default_kb_name,
                        field="knowledge_base.name",
                        path=path,
                        column=KnowledgeBase.__table__.c.name,
                    ),
                    "description": _manifest_str(
                        kb_meta.get("description"),
                        fallback=f"Imported references for skill {skill_name}",
                        field="knowledge_base.description",
                        path=path,
                    ),
                },
                "document": {
                    "title": _manifest_str(
                        doc_meta.get("title"),
                        fallback=_default_document_title(path),
                        field="document.title",
                        path=path,
                    ),
                },
            }
        )
        mapped_reference_paths.add(path)

    for path in sorted(
        p
        for p in available_paths
        if (p.startswith("references/") or p.startswith("reference/"))
        and p not in mapped_reference_paths
    ):
        if references_explicit:
            continue
        ext = PurePosixPath(path).suffix.lower()
        if ext not in _AUTO_REFERENCE_TEXT_EXTENSIONS:
            continue
        normalized["references"].append(
            {
                "path": path,
                "knowledge_base": {
                    "name": default_kb_name,
                    "description": f"Imported references for skill {skill_name}",
                },
                "document": {"title": _default_document_title(path)},
            }
        )
    return normalized, warnings


def _render_resource_mapping_manifest(manifest: dict) -> SkillPackageResource:
    return SkillPackageResource(
        path=_RESOURCE_MAP_PATH,
        content=json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8"),
    )


def _merge_manifest_resource(
    resources: list[SkillPackageResource],
    *,
    manifest: dict,
) -> list[SkillPackageResource]:
    merged = [r for r in resources if r.path != _RESOURCE_MAP_PATH]
    merged.append(_render_resource_mapping_manifest(manifest))
    return merged


def _build_builtin_id_set() -> set[str]:
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    return set(build_builtin_tool_catalog().keys())


def _missing_builtin_ids(manifest: dict) -> list[str]:
    """Return requested built-in tool IDs that don't exist on this platform.

    Shared by the validate-package pre-flight and the import materializer so
    a package that validates can never fail import on missing built-ins.
    """
    available_builtin_ids = _build_builtin_id_set()
    return sorted(
        entry["id"]
        for entry in manifest.get("builtins", [])
        if entry["id"] not in available_builtin_ids
    )


async def _materialize_mapped_resources(
    db: AsyncSession,
    *,
    user: User,
    skill: AgentSkill,
    resources: list[SkillPackageResource],
    manifest: dict,
) -> tuple[dict, list[str]]:
    """Create mapped Custom Tools and KB documents from package resources.

    Returns ``(resolved_manifest, queued_document_ids)``.
    """
    missing_builtin_ids = _missing_builtin_ids(manifest)
    if missing_builtin_ids:
        raise HTTPException(
            status_code=400,
            detail=(
                "Skill package requires unavailable built-in tool IDs: "
                + ", ".join(missing_builtin_ids)
            ),
        )

    resolved: dict = {
        "schema_version": 1,
        "builtins": [{"id": entry["id"]} for entry in manifest.get("builtins", [])],
        "scripts": [],
        "references": [],
    }
    queued_document_ids: list[str] = []

    # Scripts -> Custom Tools (as_new collision behavior by default)
    script_workspace_resources = [
        resource for resource in resources if resource.path.startswith("scripts/")
    ]
    for entry in manifest.get("scripts", []):
        path = entry["path"]
        resource = _find_resource_by_path(resources, path=path)
        if resource is None:
            raise HTTPException(status_code=400, detail=f"Mapped script file not found: {path}")
        tool_meta = entry["tool"]
        workspace_mode = (
            tool_meta.get("workspace_mode")
            if isinstance(tool_meta, dict)
            else None
        ) or "single_file"
        if tool_meta.get("language") == "python" and workspace_mode == "project":
            code = _build_python_project_tool_code(
                entrypoint_path=path,
                workspace_resources=script_workspace_resources,
            )
        else:
            code = _to_text_resource(resource, label="Mapped script")
        unique_name = await _resolve_unique_name(db, model_cls=CustomTool, name=tool_meta["name"])
        # Materialized entities inherit the parent skill's visibility:
        # for company-managed or group-shared skills, agents of OTHER
        # users can execute these tools (runtime loading is ID-based),
        # so the tool records must be visible to those users too.
        tool = CustomTool(
            user_id=None if skill.is_company_managed else user.id,
            name=unique_name,
            description=tool_meta["description"],
            input_schema=tool_meta.get("input_schema"),
            js_function=code,
            language=tool_meta["language"],
            tags=["skill-import", skill.name],
            enabled=True,
            hide_from_chat=False,
            is_company_managed=skill.is_company_managed,
            # Per-user grants ride along with the group ones. A skill
            # shared with one person would otherwise materialize a tool that
            # person cannot see, breaking the skill for its only recipient.
            shared_user_ids=skill.shared_user_ids,
            shared_user_permissions=skill.shared_user_permissions,
            access_restricted=skill.access_restricted,
        )
        tool.shared_group_ids, tool.shared_group_permissions = await normalize_group_grants(
            db,
            skill.shared_group_ids,
            skill.shared_group_permissions,
            existing_ids=skill.shared_group_ids,
        )
        db.add(tool)
        await db.flush()
        resolved["scripts"].append(
            {
                "path": path,
                "tool": {
                    "name": tool.name,
                    "description": tool.description,
                    "language": tool.language,
                    "input_schema": tool.input_schema,
                    "workspace_mode": workspace_mode,
                    "custom_tool_id": str(tool.id),
                },
            }
        )

    # references/* -> Knowledge Base documents (as_new collision behavior by default)
    kb_by_name: dict[str, KnowledgeBase] = {}
    for entry in manifest.get("references", []):
        path = entry["path"]
        resource = _find_resource_by_path(resources, path=path)
        if resource is None:
            raise HTTPException(status_code=400, detail=f"Mapped reference file not found: {path}")
        content = _to_text_resource(resource, label="Mapped reference")
        kb_meta = entry["knowledge_base"]
        kb_name = kb_meta["name"]
        if kb_name not in kb_by_name:
            unique_kb_name = await _resolve_unique_name(db, model_cls=KnowledgeBase, name=kb_name)
            # Same visibility inheritance as the materialized custom tools
            # above — shared/company skills need their reference KBs to be
            # visible to every user who can use the skill.
            kb = KnowledgeBase(
                user_id=None if skill.is_company_managed else user.id,
                is_company_managed=skill.is_company_managed,
                shared_user_ids=skill.shared_user_ids,
                shared_user_permissions=skill.shared_user_permissions,
                access_restricted=skill.access_restricted,
                name=unique_kb_name,
                description=kb_meta.get("description"),
                tags=["skill-import", skill.name],
            )
            kb.shared_group_ids, kb.shared_group_permissions = await normalize_group_grants(
                db,
                skill.shared_group_ids,
                skill.shared_group_permissions,
                existing_ids=skill.shared_group_ids,
            )
            db.add(kb)
            await db.flush()
            kb_by_name[kb_name] = kb
        kb = kb_by_name[kb_name]
        document_title = entry["document"]["title"]
        filename = PurePosixPath(path).name
        if "." not in filename:
            filename = f"{filename}.md"
        content_hash = sha256_hex(content.encode("utf-8"))
        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename=filename,
            # Same extension mapping as the KB upload endpoint so download
            # media types and UI labels stay consistent for .json/.csv/...
            source_type=source_type_for_filename(filename),
            content=content,
            content_hash=content_hash,
            status="pending",
        )
        db.add(doc)
        await db.flush()
        kb.document_count = (kb.document_count or 0) + 1
        queued_document_ids.append(str(doc.id))
        resolved["references"].append(
            {
                "path": path,
                "knowledge_base": {
                    "name": kb.name,
                    "description": kb.description,
                    "knowledge_base_id": str(kb.id),
                },
                "document": {
                    "title": document_title,
                    "document_id": str(doc.id),
                    "filename": filename,
                },
            }
        )

    return resolved, queued_document_ids


async def _auto_link_mapped_resources_to_agents(
    db: AsyncSession,
    *,
    skill: AgentSkill,
    resolved_manifest: dict,
) -> list[str]:
    """Push the package's materialized tools/KBs onto agents already using the skill.

    This is the import-time half of the rule: an agent that already references
    the skill picks up the entities this import just created. The reverse half —
    attaching a skill to an agent pulls its package resources in — lives in
    ``services/skill_package_bindings.py`` and is applied by every agent write
    path. Both directions go through the same reconciliation so a skill's
    resources are bound exactly when the skill is, whichever side moved first.

    Requires the ``agent_skill_package_origins`` rows for this import to be
    persisted already (the reconciliation reads them).
    """
    skill_id = str(skill.id)
    has_mapped_resources = bool(
        resolved_manifest.get("scripts") or resolved_manifest.get("references")
    )
    if not has_mapped_resources:
        return []

    # Only agents that reference this skill matter; pre-filter with a text
    # ``contains`` instead of scanning every agent (exact check below stays
    # authoritative).
    result = await db.execute(
        select(Agent).where(cast(Agent.skill_ids, Text).contains(skill_id))
    )
    linked_agent_ids: list[str] = []
    for agent in result.scalars().all():
        if skill_id not in (agent.skill_ids or []):
            continue
        changes = await reconcile_agent_skill_package_bindings(db, agent)
        if changes.changed:
            linked_agent_ids.append(str(agent.id))
    return linked_agent_ids


async def _cleanup_package_origin_entities(
    db: AsyncSession,
    *,
    skill_id: uuid.UUID,
    agents: list[Agent] | None = None,
) -> dict:
    """Delete entities previously materialized from a skill package import.

    Removes the origin rows plus the mapped Custom Tools, Knowledge Bases and
    documents they point to, and unbinds those entities from any agents.
    Used by skill deletion and by package re-import with ``replace`` so stale
    tools/KBs never accumulate. Returns audit-friendly stats.

    ``agents`` lets a caller that has already loaded every agent (e.g.
    ``delete_skill``, which then also purges ``skill_ids``) pass the list in so
    the agent table is scanned once per request instead of once here and once
    in the caller. When ``None`` the agents are loaded lazily, and only when
    there is actually package tool/KB unbinding to do.

    All work here is transactional and rolls back with the caller's session,
    the cancellation of the deleted documents' queued processing jobs included:
    it runs once the caller commits, so a failed request never aborts jobs for
    documents that still exist.
    """
    origins_result = await db.execute(
        select(AgentSkillPackageOrigin).where(
            AgentSkillPackageOrigin.skill_id == skill_id
        )
    )
    origins = origins_result.scalars().all()

    package_custom_tool_ids = {
        origin.custom_tool_id for origin in origins if origin.custom_tool_id is not None
    }
    package_kb_ids = {
        origin.knowledge_base_id for origin in origins if origin.knowledge_base_id is not None
    }
    package_doc_ids = {
        origin.knowledge_document_id
        for origin in origins
        if origin.knowledge_document_id is not None
    }

    package_custom_tool_id_strs = {str(tool_id) for tool_id in package_custom_tool_ids}
    package_kb_id_strs = {str(kb_id) for kb_id in package_kb_ids}

    modified_agent_count = 0
    removed_custom_tool_bindings = 0
    removed_kb_bindings = 0

    if package_custom_tool_id_strs or package_kb_id_strs:
        if agents is None:
            agents_result = await db.execute(select(Agent))
            agents = agents_result.scalars().all()
        for agent in agents:
            changed = False
            if agent.custom_tool_ids:
                filtered_custom_ids = [
                    value
                    for value in agent.custom_tool_ids
                    if value not in package_custom_tool_id_strs
                ]
                removed_custom = len(agent.custom_tool_ids) - len(filtered_custom_ids)
                if removed_custom:
                    removed_custom_tool_bindings += removed_custom
                    agent.custom_tool_ids = filtered_custom_ids
                    changed = True
            if agent.knowledge_base_ids:
                filtered_kb_ids = [
                    value for value in agent.knowledge_base_ids if value not in package_kb_id_strs
                ]
                removed_kbs = len(agent.knowledge_base_ids) - len(filtered_kb_ids)
                if removed_kbs:
                    removed_kb_bindings += removed_kbs
                    agent.knowledge_base_ids = filtered_kb_ids
                    changed = True
            if changed:
                modified_agent_count += 1

    await db.execute(
        delete(AgentSkillPackageOrigin).where(
            AgentSkillPackageOrigin.skill_id == skill_id
        )
    )

    if package_doc_ids:
        await db.execute(
            delete(KnowledgeDocument).where(KnowledgeDocument.id.in_(package_doc_ids))
        )
        cancel_document_jobs_after_commit(db, package_doc_ids)
    if package_kb_ids:
        await db.execute(delete(KnowledgeBase).where(KnowledgeBase.id.in_(package_kb_ids)))
    if package_custom_tool_ids:
        await db.execute(delete(CustomTool).where(CustomTool.id.in_(package_custom_tool_ids)))

    return {
        "package_origin_count": len(origins),
        "cascade_agent_count": modified_agent_count,
        "removed_custom_tool_binding_count": removed_custom_tool_bindings,
        "removed_knowledge_base_binding_count": removed_kb_bindings,
        "deleted_custom_tool_ids": sorted(str(tool_id) for tool_id in package_custom_tool_ids),
        "deleted_knowledge_base_ids": sorted(str(kb_id) for kb_id in package_kb_ids),
        "deleted_knowledge_document_ids": sorted(str(doc_id) for doc_id in package_doc_ids),
    }


# ---------------------------------------------------------------------------
# CRUD Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[AgentSkillResponse])
async def list_skills(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List skills visible to the current user (own + group-shared + company-managed).

    ``scope=all`` lists every skill for an admin (what a detail page links to). The
    default scope is the catalogue and the pickers: an internal built-in skill (bound to
    a fleet's own agents only, #1943) is left out of it.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            AgentSkill, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    skills = result.scalars().all()
    if scope == "visible":
        skills = [s for s in skills if not is_internal_skill(s.tags)]
    names = await resolve_owner_names(db, skills)
    return [
        _to_response(
            s,
            can_manage=can_manage(user, s, user_group_ids),
            owner_name=names.get(str(s.user_id)),
        )
        for s in skills
    ]


@router.get("/package-bindings", response_model=list[SkillPackageBindingsResponse])
async def list_skill_package_bindings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List, per skill, the Custom Tools and KBs its package resources produced.

    These entities are bound to (and unbound from) an agent automatically with
    the skill itself — see ``services/skill_package_bindings.py``. The UI uses
    this map to show what a skill carries before it is attached, and to mark the
    derived bindings as skill-owned rather than hand-picked.

    Declared before ``GET /{skill_id}`` so the literal path wins the match. Only
    skills visible to the caller are listed, and only entities the caller can
    see are reported — an under-count is preferable to leaking a private KB's
    existence.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    skill_rows = await db.execute(
        select(AgentSkill.id, AgentSkill.name).where(
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            )
        )
    )
    skill_names = {row[0]: row[1] for row in skill_rows.all()}
    if not skill_names:
        return []

    tools_by_skill: dict[uuid.UUID, list[str]] = {}
    kbs_by_skill: dict[uuid.UUID, list[str]] = {}
    for model, column, target in (
        (CustomTool, AgentSkillPackageOrigin.custom_tool_id, tools_by_skill),
        (KnowledgeBase, AgentSkillPackageOrigin.knowledge_base_id, kbs_by_skill),
    ):
        rows = await db.execute(
            select(AgentSkillPackageOrigin.skill_id, model.id)
            .join(model, model.id == column)
            .where(
                AgentSkillPackageOrigin.skill_id.in_(skill_names.keys()),
                user_visible_filter(
                    model, user.id, user_group_ids, is_admin=user.is_admin
                ),
            )
        )
        for skill_id, entity_id in rows.all():
            entity_ids = target.setdefault(skill_id, [])
            # A skill can map several reference files into the same KB.
            if str(entity_id) not in entity_ids:
                entity_ids.append(str(entity_id))

    carriers = sorted(
        set(tools_by_skill) | set(kbs_by_skill),
        key=lambda skill_id: (skill_names[skill_id], str(skill_id)),
    )
    return [
        SkillPackageBindingsResponse(
            skill_id=str(skill_id),
            skill_name=skill_names[skill_id],
            custom_tool_ids=sorted(tools_by_skill.get(skill_id, [])),
            knowledge_base_ids=sorted(kbs_by_skill.get(skill_id, [])),
        )
        for skill_id in carriers
    ]


@router.post("", response_model=AgentSkillResponse, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: AgentSkillCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new skill by URL or by direct content.

    - **URL mode**: provide ``url`` — the backend fetches the SKILL.md, extracts
      name/description/compatibility from YAML frontmatter, and caches the content.
      Add ``source_integration_id`` (a connected GitHub integration) when the URL
      points into a **private** repository; the fetch is then authenticated and
      tracks ``source_ref`` — or the repository's default-branch HEAD when unset.
    - **Paste mode**: provide ``content`` (and ``name``) — stored directly without
      fetching.  Frontmatter in the content is still parsed for metadata.

    At least one of ``url`` or ``content`` must be provided.
    Explicit values in the request body override auto-detected frontmatter.
    """
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed skills"
    )

    if not body.url and not body.content:
        raise HTTPException(
            status_code=422,
            detail="Either 'url' or 'content' must be provided.",
        )

    # Validated (and the credential proven readable) before anything is
    # persisted, so an auth problem surfaces here rather than on the next
    # hourly refresh.
    credential = await _resolve_source_credential(
        db, user, body.source_integration_id, body.url
    )

    raw_content: str | None = None
    meta: dict[str, str] = {}

    if body.url:
        # URL mode — fetch content from remote
        try:
            raw_content, meta = await fetch_skill_content(
                body.url, credential=credential, source_ref=body.source_ref
            )
        except HTTPException:
            raise
        except httpx.HTTPStatusError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Failed to fetch skill from URL: HTTP {exc.response.status_code}",
            ) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Failed to fetch skill from URL: {exc}",
            ) from exc
    elif body.content:
        # Paste mode — use content directly, still parse frontmatter
        raw_content = body.content
        meta, _ = parse_skill_frontmatter(raw_content)

    # Determine fields – prefer explicit, then frontmatter, then derive from URL/content
    name = body.name or _frontmatter_name(meta)
    if not name:
        name = (
            fit_copy_name(body.url.rsplit("/", 1)[-1].replace(".md", ""), AgentSkill.__table__.c.name)
            if body.url
            else "Untitled Skill"
        )
    description = body.description or meta.get("description")
    compatibility = body.compatibility or meta.get("compatibility")
    license_ = body.license or meta.get("license")

    skill = AgentSkill(
        user_id=user.id,
        url=body.url or None,
        source_integration_id=credential.integration_id if credential else None,
        # A ref override only means something for the authenticated contents
        # read — an anonymous URL already carries its own ref in the path — so
        # it is not persisted without a credential.
        source_ref=(body.source_ref or None) if credential else None,
        name=name,
        description=description,
        compatibility=compatibility,
        license=license_,
        content=raw_content,
        fetched_at=datetime.now(UTC) if body.url else None,
        tags=body.tags or [],
        hide_from_chat=body.hide_from_chat,
        is_company_managed=body.is_company_managed,
    )
    await apply_group_share_fields(db, skill, body, partial=False)
    await apply_user_share_fields(db, skill, body, partial=False)
    db.add(skill)
    await db.flush()
    if skill.content is not None:
        await create_skill_version(
            db,
            skill=skill,
            author_id=user.id,
            commit_message="Initial version",
        )

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="agent_skill",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Created skill '{skill.name}'",
        details={
            "url": skill.url,
            "source_integration_id": (
                str(skill.source_integration_id) if skill.source_integration_id else None
            ),
            "source_ref": skill.source_ref,
            "is_company_managed": skill.is_company_managed,
            "tags": skill.tags or [],
            "hide_from_chat": skill.hide_from_chat,
            "compatibility": skill.compatibility,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(skill)
    return _to_response(
        skill, can_manage=can_manage(user, skill), owner_name=user.full_name
    )


@router.post(
    "/upload", response_model=AgentSkillResponse, status_code=status.HTTP_201_CREATED
)
async def upload_skill(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Create a new skill by uploading a file (.md or .txt).

    The file content is stored directly.  YAML frontmatter (if present) is
    parsed for name, description, and compatibility metadata.
    """
    if not file.filename:
        raise HTTPException(status_code=422, detail="No file provided.")

    allowed_extensions = (".md", ".txt")
    if not file.filename.lower().endswith(allowed_extensions):
        raise HTTPException(
            status_code=422,
            detail=f"Unsupported file type. Allowed: {', '.join(allowed_extensions)}",
        )

    raw_bytes = await file.read()
    if len(raw_bytes) > SKILL_CONTENT_MAX_BYTES:
        raise HTTPException(
            status_code=400,
            detail=f"Skill file exceeds maximum size of {SKILL_CONTENT_MAX_BYTES // (1024 * 1024)} MB.",
        )
    try:
        raw_content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail="File must be valid UTF-8 text.",
        ) from exc

    meta, _ = parse_skill_frontmatter(raw_content)

    # Derive name from frontmatter or filename
    name = _frontmatter_name(meta) or fit_copy_name(
        file.filename.rsplit(".", 1)[0], AgentSkill.__table__.c.name
    )
    description = meta.get("description")
    compatibility = meta.get("compatibility")

    skill = AgentSkill(
        user_id=user.id,
        url=None,
        name=name,
        description=description,
        compatibility=compatibility,
        license=meta.get("license"),
        content=raw_content,
        fetched_at=None,
        tags=[],
        is_company_managed=False,
        shared_group_ids=None,
    )
    db.add(skill)
    await db.flush()
    if skill.content is not None:
        await create_skill_version(
            db,
            skill=skill,
            author_id=user.id,
            commit_message="Initial version",
        )

    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="agent_skill",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Created skill '{skill.name}' (file upload: {file.filename})",
        details={
            "url": skill.url,
            "is_company_managed": skill.is_company_managed,
            "tags": skill.tags or [],
            "hide_from_chat": skill.hide_from_chat,
            "compatibility": skill.compatibility,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(skill)
    return _to_response(
        skill, can_manage=can_manage(user, skill), owner_name=user.full_name
    )


@router.get("/{skill_id}", response_model=AgentSkillResponse)
async def get_skill(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single skill (must be own, group-shared, company-managed, or admin)."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    owner_name = None
    if skill.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == skill.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        skill,
        can_manage=can_manage(user, skill, user_group_ids),
        owner_name=owner_name,
    )


@router.post("/{skill_id}/fork", response_model=SkillForkResponse, status_code=status.HTTP_201_CREATED)
async def fork_skill_endpoint(
    skill_id: str,
    body: SkillFork,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Fork any skill the caller sees, a built-in one included, as a personal copy.

    The copy starts its own history at v1; with ``repoint_fleet_id`` the fleet's
    member agents the caller may edit are re-bound to it. Starts no run.
    """
    from app.models.fleet import Fleet
    from app.services.fleets import FleetActionRefusedError
    from app.services.skill_fork import fork_skill

    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    source = (
        await db.execute(
            select(AgentSkill).where(
                AgentSkill.id == uid,
                user_visible_filter(AgentSkill, user.id, user_group_ids, is_admin=user.is_admin),
            )
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    fleet = None
    if body.repoint_fleet_id:
        fleet = await get_manageable_entity(
            db, Fleet, user, body.repoint_fleet_id, action="repoint skills in", entity_label="Fleet",
            user_group_ids=user_group_ids,
        )
    try:
        result = await fork_skill(db, source, user, user_group_ids, name=body.name, repoint_fleet=fleet)
    except FleetActionRefusedError as exc:
        await db.rollback()
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from None
    fork = result.skill
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="agent_skill",
        entity_id=fork.id,
        entity_name=fork.name,
        summary=f"Forked the skill '{source.name}' as '{fork.name}'",
        details={
            "source_skill_id": str(source.id),
            "source_is_builtin": bool((fork.forked_from or {}).get("is_builtin")),
            "resources_copied": result.resources_copied,
            "repoint_fleet_id": str(fleet.id) if fleet else None,
            "agents_repointed": len(result.repointed_agent_ids),
        },
        request=request,
    )
    await db.commit()
    await db.refresh(fork)
    return SkillForkResponse(
        skill=_to_response(fork, can_manage=True, owner_name=user.full_name),
        resources_copied=result.resources_copied,
        repointed_agent_ids=[str(a) for a in result.repointed_agent_ids],
        warnings=result.warnings,
    )


#: The most of a licence file the Catalog tab is sent (a licence is a few KB).
_LICENSE_TEXT_MAX_BYTES = 100_000


@router.get("/{skill_id}/license", response_model=SkillLicenseResponse)
async def get_skill_license(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """The licence of a skill the caller can see: its notice and the text of the
    licence the package bundles (``SKILL_LICENSE_PATH``, ``LICENSE.md`` at its root)."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    skill = (
        await db.execute(
            select(AgentSkill).where(
                AgentSkill.id == uid,
                user_visible_filter(
                    AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
                ),
            )
        )
    ).scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")
    licence = next(
        (r for r in await _list_skill_resources(db, skill_id=skill.id) if is_licence_file(r.path)),
        None,
    )
    if licence is None:
        return SkillLicenseResponse(license=skill.license)
    raw = bytes(licence.content or b"")
    return SkillLicenseResponse(
        license=skill.license,
        file_path=licence.path,
        text=raw[:_LICENSE_TEXT_MAX_BYTES].decode("utf-8", errors="replace"),
        truncated=len(raw) > _LICENSE_TEXT_MAX_BYTES,
    )


@router.get("/{skill_id}/versions", response_model=VersionListResponse)
async def get_skill_versions(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List skill versions ordered by latest first."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    versions, total = await list_skill_versions(
        db,
        skill_id=skill.id,
        limit=limit,
        offset=offset,
    )
    active_version_number = await get_active_skill_version_number(db, skill_id=skill.id)
    names = await resolve_author_names(db, versions)
    return VersionListResponse(
        items=[
            to_version_item(
                version,
                author_name=(
                    names.get(str(version.author_id)) if version.author_id else None
                ),
                is_active=(
                    active_version_number is not None
                    and version.version_number == active_version_number
                ),
            )
            for version in versions
        ],
        total=total,
    )


@router.get("/{skill_id}/versions/diff", response_model=VersionDiffResponse)
async def diff_skill_versions(
    skill_id: str,
    from_version: int = Query(..., alias="from", ge=1),
    to_version: int = Query(..., alias="to", ge=1),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Compute line-by-line diff between two skill versions."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    left = await get_skill_version(db, skill_id=skill.id, version_number=from_version)
    right = await get_skill_version(db, skill_id=skill.id, version_number=to_version)
    if left is None or right is None:
        raise HTTPException(status_code=404, detail="Version not found")

    return VersionDiffResponse(
        **build_version_diff(
            from_content=left.content,
            to_content=right.content,
            from_version=from_version,
            to_version=to_version,
        )
    )


@router.get(
    "/{skill_id}/versions/{version_number}", response_model=VersionDetailResponse
)
async def get_skill_version_detail(
    skill_id: str,
    version_number: Annotated[int, Path(ge=1)],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a specific skill version by number."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    version = await get_skill_version(
        db,
        skill_id=skill.id,
        version_number=version_number,
    )
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_skill_version_number(db, skill_id=skill.id)
    return to_version_detail(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=(
            active_version_number is not None
            and version.version_number == active_version_number
        ),
    )


@router.post(
    "/{skill_id}/versions/{version_number}/rollback",
    response_model=VersionDetailResponse,
)
async def rollback_skill_version(
    skill_id: str,
    version_number: Annotated[int, Path(ge=1)],
    body: RollbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Rollback skill content to a previous version as a new head version."""
    skill = await get_manageable_entity(
        db, AgentSkill, user, skill_id, action="edit", entity_label="skill"
    )
    target = await get_skill_version(
        db,
        skill_id=skill.id,
        version_number=version_number,
    )
    if target is None:
        raise HTTPException(status_code=404, detail="Version not found")

    active_version = await rollback_skill_to_version(
        db,
        skill=skill,
        target_version_number=version_number,
        author_id=user.id,
        commit_message=body.commit_message,
    )
    await log_audit(
        db,
        user=user,
        action="rollback",
        entity_type="agent_skill",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Rolled back skill '{skill.name}' to v{version_number}",
        details={
            "rolled_back_to": version_number,
            "active_version": active_version.version_number,
            "rollback_note": body.commit_message,
        },
        request=request,
    )
    await db.commit()

    names = await resolve_author_names(db, [active_version])
    return to_version_detail(
        active_version,
        author_name=(
            names.get(str(active_version.author_id))
            if active_version.author_id
            else None
        ),
        is_active=True,
    )


@router.delete("/{skill_id}/versions/{version_number}", status_code=204)
async def delete_skill_version(
    skill_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a skill version. Admin only. Cannot delete active or locked versions."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    version = await get_skill_version(db, skill_id=skill.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    active_version_number = await get_active_skill_version_number(db, skill_id=skill.id)
    if active_version_number is not None and version.version_number == active_version_number:
        raise HTTPException(status_code=409, detail="Cannot delete the active version")
    if version.is_locked:
        raise HTTPException(status_code=409, detail="Version is locked; unlock it first")
    if await is_skill_version_pinned_by_evaluation_runs(
        db,
        skill_id=skill.id,
        version_number=version.version_number,
    ):
        raise HTTPException(
            status_code=409,
            detail="Version is referenced by a pending/running evaluation run",
        )
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="skill_version",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Deleted version v{version_number} of skill '{skill.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.delete(version)
    await db.commit()


@router.post(
    "/{skill_id}/versions/{version_number}/lock", response_model=VersionListItemResponse
)
async def lock_skill_version(
    skill_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Lock a skill version to protect it from deletion. Admin only."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    version = await get_skill_version(db, skill_id=skill.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version.is_locked = True
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="skill_version",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Locked version v{version_number} of skill '{skill.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.commit()
    await db.refresh(version)
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_skill_version_number(db, skill_id=skill.id)
    return to_version_item(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=(
            active_version_number is not None
            and version.version_number == active_version_number
        ),
    )


@router.post(
    "/{skill_id}/versions/{version_number}/unlock",
    response_model=VersionListItemResponse,
)
async def unlock_skill_version(
    skill_id: str,
    version_number: Annotated[int, Path(ge=1)],
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Unlock a skill version. Admin only."""
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if skill is None:
        raise HTTPException(status_code=404, detail="Skill not found")
    version = await get_skill_version(db, skill_id=skill.id, version_number=version_number)
    if version is None:
        raise HTTPException(status_code=404, detail="Version not found")
    version.is_locked = False
    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="skill_version",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Unlocked version v{version_number} of skill '{skill.name}'",
        details={"version_number": version_number},
        request=request,
    )
    await db.commit()
    await db.refresh(version)
    names = await resolve_author_names(db, [version])
    active_version_number = await get_active_skill_version_number(db, skill_id=skill.id)
    return to_version_item(
        version,
        author_name=names.get(str(version.author_id)) if version.author_id else None,
        is_active=(
            active_version_number is not None
            and version.version_number == active_version_number
        ),
    )


@router.get("/{skill_id}/tier3-tools", response_model=list[SkillTier3ToolLinkResponse])
async def list_skill_tier3_tools(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List Tier 3 script resources linked as Custom Tools for a skill."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    skill_result = await db.execute(
        select(AgentSkill.id).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    if skill_result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Skill not found")

    rows = await db.execute(
        select(
            CustomTool.id,
            CustomTool.name,
            CustomTool.description,
            AgentSkillPackageOrigin.resource_path,
        )
        .join(
            AgentSkillPackageOrigin,
            AgentSkillPackageOrigin.custom_tool_id == CustomTool.id,
        )
        .where(
            AgentSkillPackageOrigin.skill_id == uid,
            AgentSkillPackageOrigin.resource_kind == "script",
            user_visible_filter(
                CustomTool, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
        .order_by(CustomTool.name.asc(), AgentSkillPackageOrigin.resource_path.asc())
    )
    return [
        SkillTier3ToolLinkResponse(
            id=str(tool_id),
            name=name,
            description=description or "",
            resource_path=resource_path,
        )
        for tool_id, name, description, resource_path in rows.all()
    ]


@router.put("/{skill_id}", response_model=AgentSkillResponse)
async def update_skill(
    skill_id: str,
    body: AgentSkillUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Update a skill (creator or admin only).

    - Changing the URL triggers a re-fetch.
    - Changing ``source_integration_id`` or ``source_ref`` also triggers a
      re-fetch, so an unusable credential is rejected here rather than
      discovered by the next hourly auto-refresh.  Pass
      ``source_integration_id: null`` to detach the credential and go back to
      anonymous fetches.
    - Providing ``content`` directly replaces the cached content (clears ``url``
      and ``fetched_at`` unless a URL is also provided in the same request).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    skill = await get_manageable_entity(
        db,
        AgentSkill,
        user,
        skill_id,
        action="edit",
        entity_label="skill",
        user_group_ids=user_group_ids,
    )

    if skill.tags and "builtin" in skill.tags:
        raise HTTPException(
            status_code=403, detail="Built-in skills cannot be modified"
        )

    previous_content = skill.content
    previous_description = skill.description
    previous_compatibility = skill.compatibility

    # Resolve the effective source binding for this request: an omitted field
    # keeps the stored value, an explicit value (including ``null`` for the
    # integration) replaces it.
    effective_url = body.url if body.url is not None else skill.url
    stored_integration_id = (
        str(skill.source_integration_id) if skill.source_integration_id else None
    )
    stored_ref = skill.source_ref

    # Detaching (an explicit ``source_integration_id: null``) is a pure
    # configuration change and must NOT re-read the source.  A private
    # repository is unreadable anonymously, so re-fetching here would 404 and
    # fail the request — leaving the credential attached and the user with no
    # way to ever remove it.  The cached content stays as-is; the next
    # scheduled refresh then fails on its own merits and follows the normal
    # quarantine path, which is the correct outcome for a skill now
    # pointing at a URL nobody can read.
    detaching_credential = (
        "source_integration_id" in body.model_fields_set
        and body.source_integration_id is None
    )

    if detaching_credential:
        effective_integration_id: str | None = None
        # A ref override is meaningless without a credential (see create_skill).
        effective_ref: str | None = None
    else:
        effective_integration_id = (
            body.source_integration_id
            if "source_integration_id" in body.model_fields_set
            else stored_integration_id
        )
        effective_ref = (
            (body.source_ref or None)
            if "source_ref" in body.model_fields_set
            else stored_ref
        )

    url_changed = body.url is not None and body.url != skill.url
    source_binding_changed = (
        effective_integration_id != stored_integration_id or effective_ref != stored_ref
    )
    # A detach on its own never re-fetches; combined with a URL change it does,
    # because the new URL still has to be read (anonymously) to be cached.
    needs_refetch = bool(effective_url) and (
        url_changed or (source_binding_changed and not detaching_credential)
    )

    if effective_integration_id and not effective_url:
        raise HTTPException(
            status_code=422,
            detail=(
                "A source credential can only be attached to a URL-based skill. "
                "Provide a 'url' pointing at a SKILL.md file, or omit "
                "'source_integration_id'."
            ),
        )

    if detaching_credential:
        # Applied after the guard above so a rejected request leaves the row
        # untouched.  The refetch branch below re-asserts the same values when
        # a URL change accompanies the detach.
        skill.source_integration_id = None
        skill.source_ref = None
        skill.auth_failure_count = 0

    # Resolve (and prove) the credential only when this request actually
    # re-reads the source.  A plain rename must keep working even when the
    # bound integration has since been disconnected — otherwise a broken
    # credential would also lock every unrelated edit to the skill.
    credential = (
        await _resolve_source_credential(
            db,
            user,
            effective_integration_id,
            effective_url,
            newly_bound=canonical_integration_ids([effective_integration_id])
            != canonical_integration_ids([stored_integration_id]),
        )
        if needs_refetch
        else None
    )

    # If the URL or the source binding changed, re-fetch content
    if needs_refetch:
        try:
            raw_content, meta = await fetch_skill_content(
                effective_url, credential=credential, source_ref=effective_ref
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Failed to fetch skill from new URL: {exc}",
            ) from exc
        skill.url = effective_url
        skill.content = raw_content
        skill.fetched_at = datetime.now(UTC)
        skill.source_integration_id = (
            credential.integration_id if credential else None
        )
        # See create_skill: a ref override is meaningless without a credential.
        skill.source_ref = effective_ref if credential else None
        # A new URL or credential means the old failure counters are stale —
        # reset both so the fresh source gets a clean retry budget.
        skill.refresh_failure_count = 0
        skill.auth_failure_count = 0
        # Auto-update from new frontmatter if not explicitly set in this request
        if body.name is None and (frontmatter_name := _frontmatter_name(meta)):
            skill.name = frontmatter_name
        if body.description is None and meta.get("description"):
            skill.description = meta.get("description")
        if body.compatibility is None and meta.get("compatibility"):
            skill.compatibility = meta.get("compatibility")
        if body.license is None and meta.get("license"):
            skill.license = meta.get("license")
    elif "content" in body.model_fields_set and body.content is not None:
        # Direct content update — replace content, optionally parse frontmatter
        skill.content = body.content
        meta, _ = parse_skill_frontmatter(body.content)
        if body.name is None and (frontmatter_name := _frontmatter_name(meta)):
            skill.name = frontmatter_name
        if body.description is None and meta.get("description"):
            skill.description = meta.get("description")
        if body.compatibility is None and meta.get("compatibility"):
            skill.compatibility = meta.get("compatibility")
        if body.license is None and meta.get("license"):
            skill.license = meta.get("license")
        # If no URL is provided in this update and we're replacing content,
        # clear the URL since the content is now user-provided
        if "url" not in body.model_fields_set:
            skill.url = None
            skill.fetched_at = None
            # No URL means no auto-refresh — clear the stale failure counters
            # and the now-meaningless source binding so the UI does not show a
            # misleading "refresh failing" state.
            skill.refresh_failure_count = 0
            skill.auth_failure_count = 0
            skill.source_integration_id = None
            skill.source_ref = None

    if body.name is not None:
        skill.name = body.name
    if body.description is not None:
        skill.description = body.description
    if body.compatibility is not None:
        skill.compatibility = body.compatibility
    if body.license is not None:
        # Blank clears it: the Catalog tab edits it as free text.
        skill.license = body.license.strip() or None
    if body.tags is not None:
        skill.tags = body.tags
    if body.enabled is not None:
        # Re-enabling a skill (false -> true transition only) resets the
        # quarantine counter so the URL gets fresh retry attempts.  A routine update that echoes ``enabled: true`` for an
        # already-enabled skill must not clear the counter, or it would
        # silently undo an active quarantine.
        if body.enabled and not skill.enabled:
            skill.refresh_failure_count = 0
        skill.enabled = body.enabled
    if body.hide_from_chat is not None:
        skill.hide_from_chat = body.hide_from_chat

    # Group sharing
    await apply_group_share_fields(db, skill, body, partial=True)

    # Company-managed toggle
    await handle_company_managed_toggle(
        db,
        user,
        skill,
        body.is_company_managed,
        agent_field="skill_ids",
        keep_grants=wants_access_restriction(body, skill),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, skill, body, partial=True)

    # Owner transfer (admin only)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=skill,
        new_owner_id=body.user_id,
        entity_type="agent_skill",
        entity_name=skill.name,
        request=request,
    )

    # Public catalog toggle (admin-only for company-managed entities)
    if body.is_public_catalog is not None:
        if skill.is_company_managed and not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can publish company-managed entities to the public catalog",
            )
        skill.is_public_catalog = body.is_public_catalog

    created_version = None
    if (
        skill.content is not None
        and (
            skill.content != previous_content
            or skill.description != previous_description
            or skill.compatibility != previous_compatibility
        )
    ):
        created_version = await create_skill_version(
            db,
            skill=skill,
            author_id=user.id,
            commit_message=body.commit_message,
        )

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="agent_skill",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Updated skill '{skill.name}'",
        details={
            "url": skill.url,
            "source_integration_id": (
                str(skill.source_integration_id) if skill.source_integration_id else None
            ),
            "source_ref": skill.source_ref,
            "is_company_managed": skill.is_company_managed,
            "tags": skill.tags or [],
            "hide_from_chat": skill.hide_from_chat,
            "compatibility": skill.compatibility,
            "new_version": created_version.version_number if created_version else None,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(skill)
    owner_name = None
    if skill.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == skill.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        skill,
        can_manage=can_manage(user, skill, user_group_ids),
        owner_name=owner_name,
    )


@router.delete("/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(
    skill_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Delete a skill (creator or admin only)."""
    skill = await get_manageable_entity(
        db, AgentSkill, user, skill_id, action="delete", entity_label="skill"
    )
    if not user.is_admin and skill.user_id != user.id:
        raise HTTPException(
            status_code=403,
            detail="Only the creator or an admin can delete this skill",
        )

    if skill.tags and "builtin" in skill.tags:
        raise HTTPException(status_code=403, detail="Built-in skills cannot be deleted")

    # Load every agent once and reuse the same list for both cleanup phases -
    # the package-origin cascade (custom tool / KB unbinding) and the skill_ids
    # purge below - so a package-origin delete scans the agent table once
    # instead of twice.
    agents_result = await db.execute(select(Agent))
    agents = agents_result.scalars().all()

    cleanup_stats = await _cleanup_package_origin_entities(
        db, skill_id=skill.id, agents=agents
    )

    # Purge stale references from all agents' skill_ids. This is deliberately
    # NOT folded into ``_cleanup_package_origin_entities`` because that helper is
    # also reused by package re-import with ``replace`` (where the skill itself
    # survives and must stay bound) - only true deletion should unbind the skill.
    sid = str(skill.id)
    removed_skill_binding_count = 0
    for agent in agents:
        if agent.skill_ids and sid in agent.skill_ids:
            filtered_skill_ids = [v for v in agent.skill_ids if v != sid]
            # Count occurrences removed (not agents touched) so this stays
            # consistent with the ``removed_*_binding_count`` stats produced by
            # ``_cleanup_package_origin_entities`` in the same audit record and
            # stays accurate even if an agent's ``skill_ids`` holds duplicates.
            removed_skill_binding_count += len(agent.skill_ids) - len(filtered_skill_ids)
            agent.skill_ids = filtered_skill_ids

    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="agent_skill",
        entity_id=skill.id,
        entity_name=skill.name,
        summary=f"Deleted skill '{skill.name}'",
        details={
            "is_company_managed": skill.is_company_managed,
            "url": skill.url,
            "removed_skill_binding_count": removed_skill_binding_count,
            **cleanup_stats,
        },
        request=request,
    )
    await db.delete(skill)
    await db.commit()


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/{skill_id}/export")
async def export_skill(
    skill_id: str,
    format: str = Query(default="zip"),
    include_versionning: bool = Query(default=True),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a skill as a package ZIP (default) or legacy JSON bundle."""
    uid = parse_path_id(skill_id, not_found_detail="Skill not found")
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.id == uid,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    if format == "json":
        bundle = {
            "version": 1,
            "export_type": "skill",
            "exported_at": datetime.now(UTC).isoformat(),
            "skill": {
                "url": skill.url,
                "name": skill.name,
                "description": skill.description,
                "compatibility": skill.compatibility,
                "license": skill.license,
                "content": skill.content,
                "tags": skill.tags or [],
            },
        }
        safe_name = skill.name.lower().replace(" ", "-")
        return JSONResponse(
            content=bundle,
            headers={
                "Content-Disposition": build_content_disposition(
                    f"skill-{safe_name}.json"
                )
            },
        )

    if format != "zip":
        raise HTTPException(
            status_code=422, detail="Unsupported export format. Use `zip` or `json`."
        )

    if not skill.content:
        raise HTTPException(
            status_code=422,
            detail="Skill has no cached content and cannot be exported as a package.",
        )

    # Skills created via paste/upload keep their name and description in the
    # skill's properties, not in the SKILL.md frontmatter a package requires:
    # complete it here. Then validate with the same parser the import uses so
    # the export can never produce a ZIP that immediately fails
    # /skills/validate-package or re-import.
    export_content = complete_package_frontmatter(
        skill.content, name=skill.name, description=skill.description
    )
    try:
        parse_skill_markdown(export_content.encode("utf-8"))
    except ValueError as err:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Skill content is not exportable as a package: {err} "
                "Fix the SKILL.md frontmatter or use format=json for a raw export."
            ),
        ) from err

    resource_rows = await _list_skill_resources(db, skill_id=skill.id)
    resources = [
        SkillPackageResource(path=row.path, content=row.content) for row in resource_rows
    ]
    export_manifest, _ = _normalize_resource_mappings(
        skill_name=skill.name, resources=resources
    )
    # Also reflect currently linked built-ins from agents using this skill.
    # Pre-filter by a text ``contains`` so only agents referencing this skill
    # are loaded (exact check below stays authoritative).
    skill_id_str = str(skill.id)
    agents_result = await db.execute(
        select(Agent).where(cast(Agent.skill_ids, Text).contains(skill_id_str))
    )
    linked_builtin_ids: set[str] = set()
    for agent in agents_result.scalars().all():
        if skill_id_str in (agent.skill_ids or []):
            for builtin_id in agent.builtin_tool_ids or []:
                linked_builtin_ids.add(str(builtin_id))
    if linked_builtin_ids:
        existing_ids = {entry["id"] for entry in export_manifest["builtins"]}
        for builtin_id in sorted(linked_builtin_ids):
            if builtin_id not in existing_ids:
                export_manifest["builtins"].append({"id": builtin_id})
    resources = _merge_manifest_resource(resources, manifest=export_manifest)

    versions_result = await db.execute(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.version_number.asc())
    )
    versions = [
        SkillPackageVersion(
            number=row.version_number,
            # The active snapshot must equal the root SKILL.md the package holds.
            content=(
                export_content
                if row.content == skill.content
                else complete_package_frontmatter(
                    row.content,
                    name=skill.name,
                    description=row.description or skill.description,
                )
            ),
            description=row.description,
            compatibility=row.compatibility,
            created_at=row.created_at.isoformat() if row.created_at else None,
        )
        for row in versions_result.scalars().all()
    ]
    if not versions:
        versions = [
            SkillPackageVersion(
                number=1,
                content=export_content,
                description=skill.description,
                compatibility=skill.compatibility,
            )
        ]

    package_root_name = derive_package_root_name(export_content, fallback=skill.name)
    package_bytes = build_skill_package_zip(
        root_dir_name=package_root_name,
        root_skill_content=export_content,
        resources=resources,
        versions=versions,
        include_versionning=include_versionning,
    )
    return StreamingResponse(
        io.BytesIO(package_bytes),
        media_type="application/zip",
        headers={
            "Content-Disposition": build_content_disposition(
                f"skill-{package_root_name}.zip"
            )
        },
    )


@router.post("/validate-package", response_model=SkillPackageValidationResponse)
async def validate_skill_package(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    _ = user
    raw = await file.read()
    try:
        parsed = parse_skill_import_upload(raw, filename=file.filename)
        manifest, mapping_warnings = _normalize_resource_mappings(
            skill_name=parsed.name,
            resources=parsed.resources,
        )
    except (ValueError, HTTPException) as err:
        message = err.detail if isinstance(err, HTTPException) else str(err)
        return SkillPackageValidationResponse(valid=False, errors=[str(message)])
    # Pre-flight the same built-in availability check the import enforces,
    # so a package that validates can never fail import on missing built-ins.
    missing_builtins = _missing_builtin_ids(manifest)
    if missing_builtins:
        return SkillPackageValidationResponse(
            valid=False,
            errors=[
                "Skill package requires unavailable built-in tool IDs: "
                + ", ".join(missing_builtins)
            ],
            detected_name=parsed.name,
            detected_description=parsed.description,
            detected_compatibility=parsed.compatibility,
            resource_count=len(parsed.resources),
            version_count=len(parsed.versions),
        )
    return SkillPackageValidationResponse(
        valid=True,
        warnings=[
            *parsed.warnings,
            *mapping_warnings,
            (
                f"Mapping manifest resolved: "
                f"{len(manifest.get('builtins', []))} built-ins, "
                f"{len(manifest.get('scripts', []))} scripts, "
                f"{len(manifest.get('references', []))} references."
            ),
        ],
        detected_name=parsed.name,
        detected_description=parsed.description,
        detected_compatibility=parsed.compatibility,
        resource_count=len(parsed.resources),
        version_count=len(parsed.versions),
    )


async def _visible_skill_names(db: AsyncSession, user: User) -> set[str]:
    """Names of every skill the user can already see.

    Used by the bulk review screen to flag collisions up front, with the same
    visibility rule the import itself applies.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    rows = await db.execute(
        select(AgentSkill.name).where(
            user_visible_filter(AgentSkill, user.id, user_group_ids, is_admin=user.is_admin)
        )
    )
    return set(rows.scalars().all())


@router.post("/validate-bulk-package", response_model=SkillBulkPackageValidationResponse)
async def validate_skill_bulk_package(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Pre-flight a multi-skill archive: what would be imported, what is ignored.

    Nothing is written — this is the review step the bulk import approval
    screen is built on. Only archive-level problems make the whole
    response invalid; a single malformed skill comes back as a non-importable
    row so the rest of the archive stays usable.
    """
    raw = await file.read()
    try:
        parsed = parse_skill_bulk_package_zip(raw)
    except ValueError as err:
        return SkillBulkPackageValidationResponse(valid=False, errors=[str(err)])

    existing_names = await _visible_skill_names(db, user)

    candidates: list[BulkSkillCandidateResponse] = []
    for entry in parsed.skills:
        errors: list[str] = [entry.error] if entry.error else []
        warnings = list(entry.warnings)
        if entry.package is not None:
            # Same resource-mapping and built-in availability pre-flight the
            # single-package validation runs, so a row that shows as importable
            # cannot fail on those at approval time.
            try:
                manifest, mapping_warnings = _normalize_resource_mappings(
                    skill_name=entry.package.name,
                    resources=entry.package.resources,
                )
            except (ValueError, HTTPException) as err:
                message = err.detail if isinstance(err, HTTPException) else str(err)
                errors.append(str(message))
            else:
                warnings.extend(mapping_warnings)
                missing_builtins = _missing_builtin_ids(manifest)
                if missing_builtins:
                    errors.append(
                        "Skill package requires unavailable built-in tool IDs: "
                        + ", ".join(missing_builtins)
                    )
        candidates.append(
            BulkSkillCandidateResponse(
                path=entry.path,
                name=entry.name,
                description=entry.description,
                compatibility=entry.compatibility,
                resource_count=entry.resource_count,
                version_count=entry.version_count,
                importable=not errors,
                collision=bool(entry.name and entry.name in existing_names),
                warnings=warnings,
                errors=errors,
            )
        )

    return SkillBulkPackageValidationResponse(
        valid=True,
        skills=candidates,
        ignored=[
            IgnoredArchiveEntryResponse(path=item.path, reason=item.reason)
            for item in parsed.ignored
        ],
        skill_count=len(candidates),
        importable_count=sum(1 for candidate in candidates if candidate.importable),
        ignored_count=len(parsed.ignored),
    )


async def _apply_package_import(
    db: AsyncSession,
    *,
    request: Request,
    user: User,
    parsed: ParsedSkillPackage,
    on_collision: str,
    new_name: str | None = None,
    user_group_ids: list[str] | None = None,
) -> tuple[AgentSkill, bool, list[uuid.UUID]]:
    """Materialize one parsed package into a skill, without committing.

    Shared by the single-package import and the bulk importer: the
    caller owns the transaction boundary so a bulk run can commit skill by
    skill and keep the ones that already succeeded when a later one fails.
    Returns ``(skill, created, queued_document_ids)``; the caller must commit
    and then run the post-commit job side effects.
    """
    effective_name = parsed.name

    if user_group_ids is None:
        user_group_ids = await get_user_group_ids(db, user.id)
    existing_result = await db.execute(
        select(AgentSkill).where(
            AgentSkill.name == parsed.name,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
        .order_by(AgentSkill.updated_at.desc(), AgentSkill.id.desc())
    )
    existing_skills = list(existing_result.scalars().all())
    existing_skill = existing_skills[0] if existing_skills else None
    created = False

    if existing_skill and on_collision == "cancel":
        raise HTTPException(
            status_code=409,
            detail=(
                f"A skill named '{parsed.name}' already exists. "
                "Use on_collision=replace or on_collision=as_new."
            ),
        )

    replaced_cleanup_stats: dict | None = None
    if existing_skill and on_collision == "replace":
        target_skill = await get_manageable_entity(
            db,
            AgentSkill,
            user,
            str(existing_skill.id),
            action="edit",
            entity_label="skill",
            user_group_ids=user_group_ids,
        )
        # Same immutability guard as update_skill / delete_skill: built-in
        # skills are managed by seeding and must not be overwritten by
        # importing a package that collides on their name.
        if target_skill.tags and "builtin" in target_skill.tags:
            raise HTTPException(
                status_code=403, detail="Built-in skills cannot be replaced"
            )
    else:
        if existing_skill and on_collision == "as_new":
            if new_name:
                effective_name = new_name
            else:
                effective_name = await _resolve_import_name(
                    db, preferred_name=parsed.name
                )
            if effective_name != parsed.name:
                # Keep the stored SKILL.md (and the version head, which
                # export validation requires to match the root content)
                # consistent with the renamed skill — exports derive the
                # package root from the frontmatter name, so a stale value
                # would collide with the ORIGINAL skill on re-import.
                parsed.root_content = rewrite_frontmatter_name(
                    parsed.root_content, effective_name
                )
                if parsed.versions:
                    head_version = max(parsed.versions, key=lambda v: v.number)
                    head_version.content = rewrite_frontmatter_name(
                        head_version.content, effective_name
                    )
        target_skill = AgentSkill(
            user_id=user.id,
            url=None,
            name=effective_name,
            description=parsed.description,
            compatibility=parsed.compatibility,
            license=parsed.license,
            content=parsed.root_content,
            fetched_at=None,
            tags=[],
            is_company_managed=False,
            shared_group_ids=None,
            shared_group_permissions=None,
        )
        db.add(target_skill)
        created = True
        await db.flush()

    if not created:
        target_skill.url = None
        target_skill.fetched_at = None
        target_skill.name = effective_name
        target_skill.description = parsed.description
        target_skill.compatibility = parsed.compatibility
        target_skill.license = parsed.license
        target_skill.content = parsed.root_content

    # Validate the mapping manifest and built-in availability BEFORE the
    # replace-cleanup below: both can reject the package with a 400, and
    # the cleanup must not run for an import that is going to fail.
    resource_manifest, mapping_warnings = _normalize_resource_mappings(
        skill_name=effective_name,
        resources=parsed.resources,
    )
    early_missing_builtins = _missing_builtin_ids(resource_manifest)
    if early_missing_builtins:
        raise HTTPException(
            status_code=400,
            detail=(
                "Skill package requires unavailable built-in tool IDs: "
                + ", ".join(early_missing_builtins)
            ),
        )

    if not created and on_collision == "replace":
        # Remove entities materialized by the previous import so re-imports
        # don't accumulate orphaned Custom Tools / KBs and stale agent
        # bindings (the new import re-materializes and re-links below).
        # All of this is transactional — a later failure rolls it back; the
        # non-transactional job cancellation happens after the commit.
        replaced_cleanup_stats = await _cleanup_package_origin_entities(
            db, skill_id=target_skill.id
        )

    resolved_manifest, queued_document_ids = await _materialize_mapped_resources(
        db,
        user=user,
        skill=target_skill,
        resources=parsed.resources,
        manifest=resource_manifest,
    )
    # Provenance first: the agent-binding reconciliation below reads
    # ``agent_skill_package_origins`` to know which entities this skill carries.
    await _replace_skill_package_origins(
        db,
        skill_id=target_skill.id,
        resolved_manifest=resolved_manifest,
    )
    auto_linked_agent_ids = await _auto_link_mapped_resources_to_agents(
        db,
        skill=target_skill,
        resolved_manifest=resolved_manifest,
    )
    resolved_manifest["linked_agent_ids"] = auto_linked_agent_ids
    persisted_resources = _merge_manifest_resource(
        parsed.resources,
        manifest=resolved_manifest,
    )

    await _replace_skill_resources(
        db,
        skill_id=target_skill.id,
        resources=persisted_resources,
    )
    await _replace_skill_versions(
        db,
        skill_id=target_skill.id,
        versions=parsed.versions,
    )

    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="agent_skill",
        entity_id=target_skill.id,
        entity_name=target_skill.name,
        summary=f"Imported skill package '{target_skill.name}'",
        details={
            "on_collision": on_collision,
            "resource_count": len(persisted_resources),
            "version_count": len(parsed.versions),
            "warnings": [*parsed.warnings, *mapping_warnings],
            "builtins_count": len(resolved_manifest.get("builtins", [])),
            "scripts_count": len(resolved_manifest.get("scripts", [])),
            "references_count": len(resolved_manifest.get("references", [])),
            "auto_linked_agent_count": len(auto_linked_agent_ids),
            "replaced_package_cleanup": replaced_cleanup_stats,
        },
        request=request,
    )
    return target_skill, created, queued_document_ids


async def _run_package_import_side_effects(*, queued_document_ids: list[uuid.UUID]) -> None:
    """Post-commit side effects of a package import: enqueue the newly
    materialized documents. The replaced import's deleted documents had their
    jobs cancelled by the commit itself (``_cleanup_package_origin_entities``).
    """
    for doc_id in queued_document_ids:
        try:
            await enqueue_document_processing(doc_id)
        except Exception:
            logger.exception("Failed to enqueue mapped KB document %s after skill import", doc_id)


@router.post(
    "/import-package",
    response_model=AgentSkillResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_skill_package(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    on_collision: str = Form(default="cancel"),
    new_name: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    if on_collision not in {"cancel", "replace", "as_new"}:
        raise HTTPException(
            status_code=422,
            detail="Invalid `on_collision` value. Use cancel, replace, or as_new.",
        )
    # A blank/whitespace-only new_name is treated as absent so an as_new
    # import can never create a skill with an empty name.
    new_name = (new_name or "").strip() or None
    if new_name and (
        error := text_column_error(AgentSkill.__table__.c.name, new_name, field="new_name")
    ):
        raise HTTPException(status_code=422, detail=error)

    raw = await file.read()
    try:
        parsed = parse_skill_import_upload(raw, filename=file.filename)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    user_group_ids = await get_user_group_ids(db, user.id)
    target_skill, created, queued_document_ids = await _apply_package_import(
        db,
        request=request,
        user=user,
        parsed=parsed,
        on_collision=on_collision,
        new_name=new_name,
        user_group_ids=user_group_ids,
    )
    if not created:
        # Replacing an existing skill is an update, not a creation — return
        # 200 instead of the route's default 201 so API clients can tell the
        # two outcomes apart via standard HTTP semantics.
        response.status_code = status.HTTP_200_OK
    await db.commit()
    await _run_package_import_side_effects(queued_document_ids=queued_document_ids)
    await db.refresh(target_skill)
    owner_name = None
    if target_skill.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == target_skill.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        target_skill,
        can_manage=can_manage(user, target_skill, user_group_ids),
        owner_name=owner_name,
    )


def _parse_json_form_field(raw: str | None, *, field: str, expected: type):
    if raw is None or not raw.strip():
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as err:
        raise HTTPException(status_code=422, detail=f"`{field}` must be valid JSON.") from err
    if not isinstance(value, expected):
        raise HTTPException(
            status_code=422,
            detail=f"`{field}` must be a JSON {expected.__name__}.",
        )
    return value


@router.post("/import-bulk-package", response_model=SkillBulkImportResponse)
async def import_skill_bulk_package(
    request: Request,
    file: UploadFile = File(...),
    selected_paths: str | None = Form(default=None),
    on_collision: str = Form(default="skip"),
    collision_overrides: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Import the approved skills of a multi-skill archive.

    ``selected_paths`` is the JSON array of archive paths the user approved on
    the review screen (omitted = every importable skill). ``on_collision`` is
    the default policy (``skip`` / ``replace`` / ``as_new``) and
    ``collision_overrides`` a JSON ``{path: policy}`` map for per-skill choices.

    Each skill is committed on its own: a failure part-way through never
    rolls back the skills that already landed, and the response states exactly
    what was created, replaced, skipped and failed.
    """
    policies = {"skip", "replace", "as_new"}
    if on_collision not in policies:
        raise HTTPException(
            status_code=422,
            detail="Invalid `on_collision` value. Use skip, replace, or as_new.",
        )
    selection = _parse_json_form_field(selected_paths, field="selected_paths", expected=list)
    selected = {str(path) for path in selection} if selection is not None else None
    overrides_raw = _parse_json_form_field(
        collision_overrides, field="collision_overrides", expected=dict
    )
    overrides: dict[str, str] = {}
    for path, policy in (overrides_raw or {}).items():
        if policy not in policies:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid collision policy '{policy}' for '{path}'.",
            )
        overrides[str(path)] = policy

    raw = await file.read()
    try:
        parsed = parse_skill_bulk_package_zip(raw)
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err)) from err

    if selected is not None:
        known_paths = {entry.path for entry in parsed.skills}
        unknown = sorted(selected - known_paths)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Selected paths are not skills of this archive: "
                    + ", ".join(unknown[:5])
                ),
            )

    results: list[BulkSkillImportOutcome] = []
    for entry in parsed.skills:
        if selected is not None and entry.path not in selected:
            results.append(
                BulkSkillImportOutcome(
                    path=entry.path,
                    name=entry.name,
                    status="skipped",
                    message="Not selected for import.",
                )
            )
            continue
        if entry.package is None:
            # An omitted selection means "import what can be imported", so an
            # entry that was never importable was never asked for: it is
            # reported as skipped (with its reason kept) rather than counted as
            # a failure. Explicitly selecting a broken path is a real failure.
            explicitly_selected = selected is not None and entry.path in selected
            results.append(
                BulkSkillImportOutcome(
                    path=entry.path,
                    name=entry.name,
                    status="failed" if explicitly_selected else "skipped",
                    message=entry.error or "Skill could not be parsed.",
                )
            )
            continue

        policy = overrides.get(entry.path, on_collision)
        # ``skip`` reuses the single-import ``cancel`` semantics: the helper
        # raises 409 on a name collision, which is exactly the "leave the
        # existing skill alone" outcome, without a second visibility query.
        effective_policy = "cancel" if policy == "skip" else policy
        try:
            skill, created, queued_document_ids = await _apply_package_import(
                db,
                request=request,
                user=user,
                parsed=entry.package,
                on_collision=effective_policy,
            )
            await db.commit()
        except HTTPException as err:
            await db.rollback()
            if err.status_code == status.HTTP_409_CONFLICT and policy == "skip":
                results.append(
                    BulkSkillImportOutcome(
                        path=entry.path,
                        name=entry.name,
                        status="skipped",
                        message=f"A skill named '{entry.name}' already exists.",
                    )
                )
            else:
                results.append(
                    BulkSkillImportOutcome(
                        path=entry.path,
                        name=entry.name,
                        status="failed",
                        message=str(err.detail),
                    )
                )
            continue
        except Exception:
            await db.rollback()
            logger.exception("Bulk skill import failed for %s", entry.path)
            results.append(
                BulkSkillImportOutcome(
                    path=entry.path,
                    name=entry.name,
                    status="failed",
                    message="Unexpected error while importing this skill.",
                )
            )
            continue

        await _run_package_import_side_effects(queued_document_ids=queued_document_ids)
        results.append(
            BulkSkillImportOutcome(
                path=entry.path,
                name=skill.name,
                status="created" if created else "replaced",
                skill_id=str(skill.id),
            )
        )

    return SkillBulkImportResponse(
        created_count=sum(1 for item in results if item.status == "created"),
        replaced_count=sum(1 for item in results if item.status == "replaced"),
        skipped_count=sum(1 for item in results if item.status == "skipped"),
        failed_count=sum(1 for item in results if item.status == "failed"),
        results=results,
    )


@router.post("/{skill_id}/refresh", response_model=AgentSkillResponse)
async def refresh_skill(
    skill_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _ai_gate: None = Depends(require_ai_enabled),
):
    """Re-fetch a skill's content from its URL and update the cache.

    Only works for URL-based skills — returns 422 if the skill has no URL.
    Uses the skill's bound GitHub credential when it has one, so a private
    repository can be refreshed on demand and not only by the hourly
    scheduler.
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    skill = await get_manageable_entity(
        db,
        AgentSkill,
        user,
        skill_id,
        action="refresh",
        entity_label="skill",
        user_group_ids=user_group_ids,
    )

    if not skill.url:
        raise HTTPException(
            status_code=422,
            detail="This skill has no URL — content was provided directly and cannot be refreshed from a remote source.",
        )

    previous_content = skill.content
    previous_description = skill.description
    previous_compatibility = skill.compatibility

    # The bound credential is used directly, without re-checking that *this*
    # caller can see the integration: the binding was already authorised when
    # it was attached, the scheduler refreshes with it unattended anyway, and
    # the only thing this exposes is content the caller can already read on the
    # skill.  The token is never returned.
    try:
        credential = await resolve_skill_credential(db, skill.source_integration_id)
    except SkillCredentialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        raw_content, meta = await fetch_skill_content(
            skill.url, credential=credential, source_ref=skill.source_ref
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to refresh skill content: {exc}",
        ) from exc

    skill.content = raw_content
    skill.fetched_at = datetime.now(UTC)
    # A manual refresh that succeeded clears both failure counters, the same
    # way the scheduler's success path does.
    skill.refresh_failure_count = 0
    skill.auth_failure_count = 0
    # Update metadata from refreshed frontmatter
    if frontmatter_name := _frontmatter_name(meta):
        skill.name = frontmatter_name
    if meta.get("description"):
        skill.description = meta["description"]
    if meta.get("compatibility"):
        skill.compatibility = meta["compatibility"]
    if meta.get("license"):
        skill.license = meta["license"]

    if (
        skill.content is not None
        and (
            skill.content != previous_content
            or skill.description != previous_description
            or skill.compatibility != previous_compatibility
        )
    ):
        await create_skill_version(
            db,
            skill=skill,
            author_id=user.id,
            commit_message="Refreshed from URL",
        )

    await db.commit()
    await db.refresh(skill)
    owner_name = None
    if skill.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == skill.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        skill,
        can_manage=can_manage(user, skill, user_group_ids),
        owner_name=owner_name,
    )
