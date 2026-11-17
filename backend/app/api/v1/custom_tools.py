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

"""Custom Tools management API – CRUD for user-defined JavaScript tools.

Any authenticated user can create custom tools. Company-managed tools
(``is_company_managed=True``) are visible to all users but only admins can
edit/delete them; visibility comes from the flag and ``user_id`` keeps the
creator as owner.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, require_agentic_ai, require_agentic_ai_licensed
from app.api.v1.helpers import (
    ENTITY_LIST_SCOPE_DESCRIPTION,
    SOURCE_MANAGED_CUSTOM_TOOL_FIELDS,
    EntityListScope,
    apply_group_share_fields,
    apply_owner_transfer,
    apply_user_share_fields,
    assert_custom_tool_not_package_origin,
    can_manage,
    get_manageable_entity,
    get_user_custom_tool,
    get_user_group_ids,
    handle_company_managed_toggle,
    is_package_origin_custom_tool,
    list_scope_admin_bypass,
    package_origin_custom_tool_ids,
    require_admin_for_company_managed,
    resolve_owner_names,
    source_managed_custom_tool_error,
    user_visible_filter,
    user_visible_query,
    wants_access_restriction,
)
from app.api.v1.schemas.custom_tool import (
    CustomToolCreate,
    CustomToolResponse,
    CustomToolSkillLinkResponse,
    CustomToolUpdate,
    GenerateCodeRequest,
    GenerateCodeResponse,
    ImportCustomToolRequest,
    RunCustomToolTestsRequest,
)
from app.core.database import get_db, release_db_connection_before_loop
from app.core.security_options import security_option
from app.core.utils import format_exception_message
from app.llm.router import get_llm_for_user
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_skill_package_origin import AgentSkillPackageOrigin
from app.models.custom_tool import CustomTool
from app.models.custom_tool_source import CustomToolSource
from app.models.user import User
from app.services.audit import log_audit
from app.services.custom_tool_code_generation import (
    CodeGenerationError,
    ToolParameter,
    generate_tool_code,
)
from app.services.custom_tool_executor import CUSTOM_TOOLS_DISABLED_MSG
from app.services.custom_tool_sources import detach_tool, tool_web_url
from app.services.custom_tool_tests import (
    run_tool_tests,
    store_test_report,
    validate_test_cases,
    validate_test_code,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(
    tool: CustomTool,
    *,
    can_manage: bool,
    is_package_origin: bool,
    owner_name: str | None = None,
    source: CustomToolSource | None = None,
) -> CustomToolResponse:
    return CustomToolResponse(
        id=str(tool.id),
        user_id=str(tool.user_id) if tool.user_id else None,
        owner_name=owner_name,
        name=tool.name,
        description=tool.description,
        icon_url=tool.icon_url,
        input_schema=tool.input_schema,
        js_function=tool.js_function,
        language=tool.language or "javascript",
        tags=tool.tags or [],
        timeout=tool.timeout or 300,
        enabled=tool.enabled,
        hide_from_chat=tool.hide_from_chat,
        is_company_managed=tool.is_company_managed,
        is_public_catalog=tool.is_public_catalog,
        shared_group_ids=tool.shared_group_ids,
        shared_group_permissions=tool.shared_group_permissions,
        shared_user_ids=tool.shared_user_ids,
        shared_user_permissions=tool.shared_user_permissions,
        access_restricted=tool.access_restricted,
        can_manage=can_manage,
        is_package_origin=is_package_origin,
        test_cases=tool.test_cases,
        test_code=tool.test_code,
        last_test_status=tool.last_test_status,
        last_test_run_at=tool.last_test_run_at.isoformat() if tool.last_test_run_at else None,
        last_test_results=tool.last_test_results,
        # A tool whose source is gone is an ordinary tool (no lock, no link).
        source_id=str(source.id) if source else None,
        source_name=source.name if source else None,
        source_path=tool.source_path if source else None,
        source_commit_sha=tool.source_commit_sha if source else None,
        source_url=tool_web_url(source, tool) if source else None,
        call_count=tool.call_count or 0,
        last_called_at=(
            tool.last_called_at.isoformat() if tool.last_called_at else None
        ),
        created_at=tool.created_at.isoformat(),
        updated_at=tool.updated_at.isoformat(),
    )


async def _sources_of(
    db: AsyncSession, tools: list[CustomTool]
) -> dict[uuid.UUID, CustomToolSource]:
    """The repository sources of *tools*, by id (one query)."""
    ids = {t.source_id for t in tools if t.source_id}
    if not ids:
        return {}
    rows = await db.execute(select(CustomToolSource).where(CustomToolSource.id.in_(ids)))
    return {source.id: source for source in rows.scalars()}


async def _source_of(db: AsyncSession, tool: CustomTool) -> CustomToolSource | None:
    return (await _sources_of(db, [tool])).get(tool.source_id) if tool.source_id else None


def _validated_tests(test_cases: list | None, test_code: str | None) -> tuple[list | None, str | None]:
    """The normalized declared tests, or a 422 naming what is wrong."""
    try:
        return validate_test_cases(test_cases), validate_test_code(test_code)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("", response_model=list[CustomToolResponse])
async def list_custom_tools(
    scope: EntityListScope = Query("visible", description=ENTITY_LIST_SCOPE_DESCRIPTION),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List custom tools visible to the current user (own + group-shared + company-managed).

    ``scope=all`` lists every tool for an admin (what a detail page links to).
    """
    user_group_ids = await get_user_group_ids(db, user.id)
    result = await db.execute(
        user_visible_query(
            CustomTool, user.id, user_group_ids, is_admin=list_scope_admin_bypass(scope, user)
        )
    )
    tools = result.scalars().all()
    names = await resolve_owner_names(db, tools)
    from_packages = await package_origin_custom_tool_ids(db, [t.id for t in tools])
    sources = await _sources_of(db, list(tools))
    return [
        _to_response(
            tool,
            can_manage=can_manage(user, tool, user_group_ids),
            is_package_origin=str(tool.id) in from_packages,
            owner_name=names.get(str(tool.user_id)),
            source=sources.get(tool.source_id) if tool.source_id else None,
        )
        for tool in tools
    ]


@router.post("", response_model=CustomToolResponse, status_code=status.HTTP_201_CREATED)
async def create_custom_tool(
    body: CustomToolCreate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Create a new custom tool."""
    # A Custom tool is not an AI resource: it is a sandboxed script, and nothing
    # here reaches a model or embedding provider. ``action.script``
    # (``requires_ai=False``) runs one from a deterministic flow, so blocking
    # authoring left a non-AI flow unrepairable with AI off (FR-014).
    # The licence question is unchanged; only the AI-state refusal is dropped.
    require_admin_for_company_managed(
        body, user, "Only admins can create company-managed tools"
    )
    test_cases, test_code = _validated_tests(body.test_cases, body.test_code)

    tool = CustomTool(
        user_id=user.id,
        name=body.name,
        description=body.description,
        icon_url=body.icon_url,
        input_schema=body.input_schema,
        js_function=body.js_function,
        language=body.language,
        tags=body.tags or [],
        timeout=body.timeout,
        enabled=body.enabled,
        hide_from_chat=body.hide_from_chat,
        is_company_managed=body.is_company_managed,
        test_cases=test_cases,
        test_code=test_code,
    )
    await apply_group_share_fields(db, tool, body, partial=False)
    await apply_user_share_fields(db, tool, body, partial=False)
    db.add(tool)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="create",
        entity_type="custom_tool",
        entity_id=tool.id,
        entity_name=tool.name,
        summary=f"Created custom tool '{tool.name}'",
        request=request,
        details={
            "is_company_managed": tool.is_company_managed,
            "tags": tool.tags or [],
            "hide_from_chat": tool.hide_from_chat,
            "has_input_schema": tool.input_schema is not None
            and bool(tool.input_schema),
        },
    )
    await db.commit()
    await db.refresh(tool)
    return _to_response(
        tool,
        can_manage=can_manage(user, tool),
        # Created here, not by a package import: no origin link exists.
        is_package_origin=False,
        owner_name=user.full_name,
    )


@router.get("/{tool_id}", response_model=CustomToolResponse)
async def get_custom_tool(
    tool_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Get a single custom tool (must be own, group-shared, company-managed, or admin)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    tool = await get_user_custom_tool(
        db, user.id, tool_id, user_group_ids, is_admin=user.is_admin
    )
    owner_name = None
    if tool.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == tool.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        tool,
        can_manage=can_manage(user, tool, user_group_ids),
        is_package_origin=await is_package_origin_custom_tool(db, tool.id),
        owner_name=owner_name,
        source=await _source_of(db, tool),
    )


@router.get("/{tool_id}/skills", response_model=list[CustomToolSkillLinkResponse])
async def list_custom_tool_skills(
    tool_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List visible skills linked as origin creators of this custom tool."""
    tool = await get_user_custom_tool(db, user.id, tool_id, is_admin=user.is_admin)
    user_group_ids = await get_user_group_ids(db, user.id)
    rows = await db.execute(
        select(
            AgentSkill.id,
            AgentSkill.name,
            AgentSkill.description,
            AgentSkillPackageOrigin.resource_kind,
            AgentSkillPackageOrigin.resource_path,
            AgentSkillPackageOrigin.created_at,
        )
        .join(AgentSkill, AgentSkill.id == AgentSkillPackageOrigin.skill_id)
        .where(
            AgentSkillPackageOrigin.custom_tool_id == tool.id,
            user_visible_filter(
                AgentSkill, user.id, user_group_ids, is_admin=user.is_admin
            ),
        )
        .order_by(AgentSkill.name.asc(), AgentSkillPackageOrigin.resource_path.asc())
    )
    return [
        CustomToolSkillLinkResponse(
            skill_id=str(skill_id),
            skill_name=skill_name,
            skill_description=skill_description,
            resource_kind=resource_kind,
            resource_path=resource_path,
            linked_at=linked_at.isoformat(),
        )
        for skill_id, skill_name, skill_description, resource_kind, resource_path, linked_at in rows.all()
    ]


@router.put("/{tool_id}", response_model=CustomToolResponse)
async def update_custom_tool(
    tool_id: str,
    body: CustomToolUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update a custom tool (creator or admin only)."""
    user_group_ids = await get_user_group_ids(db, user.id)
    tool = await get_manageable_entity(
        db,
        CustomTool,
        user,
        tool_id,
        action="edit",
        entity_label="Custom tool",
        user_group_ids=user_group_ids,
    )
    await assert_custom_tool_not_package_origin(db, tool.id)
    # A synced tool's content follows its repository: refused when a request
    # CHANGES it (the configuration form sends every field back unchanged
    # along with a sharing edit), as is moving it out of its source's scope.
    new_tests: tuple[list | None, str | None] | None = None
    if body.test_cases is not None or body.test_code is not None:
        new_tests = _validated_tests(
            body.test_cases if body.test_cases is not None else tool.test_cases,
            body.test_code if body.test_code is not None else tool.test_code,
        )
    proposed = {
        name: getattr(body, name) for name in SOURCE_MANAGED_CUSTOM_TOOL_FIELDS if getattr(body, name) is not None
    }
    if new_tests is not None:
        proposed["test_cases"], proposed["test_code"] = new_tests
    changed = [name for name, value in proposed.items() if value != getattr(tool, name)]
    if body.is_company_managed is not None and body.is_company_managed != tool.is_company_managed:
        changed.append("is_company_managed")
    if body.user_id is not None and body.user_id != (str(tool.user_id) if tool.user_id else None):
        changed.append("user_id")
    if changed and (locked := await source_managed_custom_tool_error(db, tool)):
        raise HTTPException(status_code=403, detail=f"{locked} (fields: {', '.join(changed)}).")
    if new_tests is not None:
        tool.test_cases, tool.test_code = new_tests

    if body.name is not None:
        tool.name = body.name
    if body.description is not None:
        tool.description = body.description
    if body.icon_url is not None:
        tool.icon_url = body.icon_url
    if body.input_schema is not None:
        tool.input_schema = body.input_schema
    if body.js_function is not None:
        tool.js_function = body.js_function
    if body.language is not None:
        tool.language = body.language
    if body.tags is not None:
        tool.tags = body.tags
    if body.timeout is not None:
        tool.timeout = body.timeout
    if body.enabled is not None:
        tool.enabled = body.enabled
    if body.hide_from_chat is not None:
        tool.hide_from_chat = body.hide_from_chat
    # Group sharing
    await apply_group_share_fields(db, tool, body, partial=True)

    await handle_company_managed_toggle(
        db,
        user,
        tool,
        body.is_company_managed,
        agent_field="custom_tool_ids",
        keep_grants=wants_access_restriction(body, tool),
    )

    # Per-user sharing — after the company-managed toggle, since
    # ``access_restricted`` is only meaningful relative to it.
    await apply_user_share_fields(db, tool, body, partial=True)

    # Owner transfer (admin only)
    await apply_owner_transfer(
        db,
        actor=user,
        entity=tool,
        new_owner_id=body.user_id,
        entity_type="custom_tool",
        entity_name=tool.name,
        request=request,
    )

    # Public catalog toggle (admin-only for company-managed entities)
    if body.is_public_catalog is not None:
        if tool.is_company_managed and not user.is_admin:
            raise HTTPException(
                status_code=403,
                detail="Only admins can publish company-managed entities to the public catalog",
            )
        tool.is_public_catalog = body.is_public_catalog

    await log_audit(
        db,
        user=user,
        action="update",
        entity_type="custom_tool",
        entity_id=tool.id,
        entity_name=tool.name,
        summary=f"Updated custom tool '{tool.name}'",
        request=request,
        details={
            "is_company_managed": tool.is_company_managed,
            "tags": tool.tags or [],
            "hide_from_chat": tool.hide_from_chat,
        },
    )
    await db.commit()
    await db.refresh(tool)
    owner_name = None
    if tool.user_id:
        owner_row = await db.execute(
            select(User.full_name).where(User.id == tool.user_id)
        )
        owner_name = owner_row.scalar_one_or_none()
    return _to_response(
        tool,
        can_manage=can_manage(user, tool, user_group_ids),
        # The route refused a package-installed tool before writing anything.
        is_package_origin=False,
        owner_name=owner_name,
        source=await _source_of(db, tool),
    )


@router.delete("/{tool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_custom_tool(
    tool_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a custom tool (creator or admin only)."""
    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="delete", entity_label="Custom tool"
    )
    await assert_custom_tool_not_package_origin(db, tool.id, action="deleted")
    if locked := await source_managed_custom_tool_error(db, tool, action="deleted"):
        raise HTTPException(status_code=403, detail=f"{locked}.")
    await log_audit(
        db,
        user=user,
        action="delete",
        entity_type="custom_tool",
        entity_id=tool.id,
        entity_name=tool.name,
        summary=f"Deleted custom tool '{tool.name}'",
        request=request,
        details={
            "is_company_managed": tool.is_company_managed,
        },
    )
    # Purge stale references from all agents' custom_tool_ids (pre-filtered by
    # a text ``contains`` so only rows mentioning the id are loaded).
    tid = str(tool.id)
    agents_result = await db.execute(
        select(Agent).where(cast(Agent.custom_tool_ids, Text).contains(tid))
    )
    for agent in agents_result.scalars().all():
        if agent.custom_tool_ids and tid in agent.custom_tool_ids:
            agent.custom_tool_ids = [v for v in agent.custom_tool_ids if v != tid]

    await db.delete(tool)
    await db.commit()


@router.post("/{tool_id}/test/upload")
async def upload_tool_test_file(
    tool_id: str,
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Upload a file for use during custom tool testing.

    Stores the file in S3 and creates a :class:`FileAttachment` record so
    the test endpoint can resolve it via ``_resolve_file_args``.
    """
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import (
        MAX_UPLOAD_SIZE_BYTES,
        file_storage,
        guess_content_type,
    )

    # Verify access to the tool
    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="test", entity_label="Custom tool"
    )
    await assert_custom_tool_not_package_origin(db, tool.id)

    data = await file.read()
    if len(data) > MAX_UPLOAD_SIZE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"File too large. Maximum size is {MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB.",
        )

    content_type = file.content_type or guess_content_type(file.filename or "file")
    s3_object_name = await file_storage.upload(
        data=data,
        filename=file.filename or "upload",
        content_type=content_type,
        prefix=f"tool-test/{tool_id}",
    )

    file_att = FileAttachment(
        id=uuid.uuid4(),
        user_id=user.id,
        filename=file.filename or "upload",
        content_type=content_type,
        size=len(data),
        s3_object_name=s3_object_name,
        source="tool_test",
    )
    db.add(file_att)
    await db.commit()

    return {
        "file_id": str(file_att.id),
        "filename": file_att.filename,
        "content_type": file_att.content_type,
        "size": file_att.size,
    }


class LintRequest(BaseModel):
    language: str = "javascript"
    code: str = ""


@router.post("/lint")
async def lint_custom_tool_code(
    body: LintRequest,
    _user: User = Depends(get_current_user),
    _agentic_gate: None = Depends(require_agentic_ai_licensed),
):
    """Syntax-check a script WITHOUT executing it.

    The authoring modal's Validate button: Python via ``ast.parse`` on the
    raw code, JavaScript via ``node --check`` on the runtime-wrapped script
    (so top-level ``await``/``return`` stay legal). Returns
    ``{"status": "ok"}`` or ``{"status": "error", "message", "line"}`` —
    invalid syntax is a 200 with a verdict, not an HTTP error.
    """

    if security_option("disable_custom_tools"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CUSTOM_TOOLS_DISABLED_MSG,
        )
    language = body.language if body.language in ("javascript", "python") else None
    if language is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="language must be javascript or python",
        )
    from app.services.custom_tool_executor import lint_tool_code

    try:
        return await lint_tool_code(language, body.code or "")
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc


@router.post("/generate-code", response_model=GenerateCodeResponse)
async def generate_custom_tool_code(
    body: GenerateCodeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    _agentic_gate: None = Depends(require_agentic_ai),
):
    """Draft or rework a tool's code with the caller's model.

    The editor's Generate with AI bar: the request carries the unsaved form
    (language, name, description, parameters, current code), nothing is
    stored or executed, and the answer comes with ``lint_tool_code``'s
    verdict on it.
    """

    if security_option("disable_custom_tools"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CUSTOM_TOOLS_DISABLED_MSG,
        )
    llm, model = await get_llm_for_user(db, str(user.id))
    await release_db_connection_before_loop(db)
    try:
        result = await generate_tool_code(
            llm,
            model,
            language=body.language,
            brief=body.description,
            tool_name=body.tool_name,
            tool_description=body.tool_description,
            parameters=[
                ToolParameter(name=p.name, type=p.type, description=p.description, required=p.required)
                for p in body.parameters
            ],
            current_code=body.current_code,
        )
    except CodeGenerationError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return GenerateCodeResponse(
        code=result.code,
        truncated=result.truncated,
        syntax_ok=result.syntax_ok,
        syntax_error=result.syntax_error,
        syntax_error_line=result.syntax_error_line,
        repaired=result.repaired,
    )


@router.post("/{tool_id}/test")
async def test_custom_tool(
    tool_id: str,
    body: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Test a custom tool by executing its JavaScript function with sample inputs."""
    # No AI gate: the sandboxed script needs no model or embedding provider,
    # and ``action.script`` (``requires_ai=False``) already runs the same tool
    # through the same executor with AI off. Gating here made the API and the
    # flow runner disagree about one operation (FR-014/FR-015).

    if security_option("disable_custom_tools"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=CUSTOM_TOOLS_DISABLED_MSG,
        )

    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="test", entity_label="Custom tool"
    )

    if not tool.js_function:
        return {"status": "error", "message": "No function defined"}

    test_inputs = body or {}

    # Build a FileContext from uploaded test file IDs (same path as runtime).
    raw_file_ids: list[str] = test_inputs.pop("$file_ids", [])
    sandbox_files: list[dict] = []
    if raw_file_ids:
        from app.llm.base import FileRef
        from app.models.file_attachment import FileAttachment
        from app.tools.base import FileContext

        fc = FileContext()
        for fid in raw_file_ids:
            try:
                fa_result = await db.execute(
                    select(FileAttachment).where(
                        FileAttachment.id == uuid.UUID(fid),
                        FileAttachment.user_id == user.id,
                    )
                )
                fa = fa_result.scalar_one_or_none()
                if fa:
                    fc.add(FileRef.from_attachment(fa))
            except Exception:
                logger.warning("Failed to resolve test file %s", fid)
        if not fc.is_empty:
            sandbox_files = await fc.to_sandbox_files()

    test_inputs["$files"] = sandbox_files

    try:
        from app.services.custom_tool_executor import execute_tool_auto
        from app.services.variable_resolver import load_variables, substitute_variables

        user_group_ids = await get_user_group_ids(db, user.id)
        variables = await load_variables(db, str(user.id), user_group_ids)
        js_function = substitute_variables(tool.js_function, variables)

        result_output = await execute_tool_auto(
            js_function,
            test_inputs,
            language=tool.language or "javascript",
            timeout=tool.timeout or 300,
            user_id=user.id,
            db=db,
        )
        return {"status": "ok", "output": result_output}
    except Exception as e:
        logger.error("Custom tool test failed: %s", format_exception_message(e), exc_info=True)
        return {
            "status": "error",
            "message": "Tool execution failed. Check the tool configuration and inputs.",
        }


@router.post("/{tool_id}/test/cleanup")
async def cleanup_tool_test_files(
    tool_id: str,
    body: dict | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Purge test files uploaded during custom tool testing.

    Called by the frontend when the test dialog is closed so that files
    remain available for re-runs within the same dialog session.
    """
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    logger = logging.getLogger(__name__)

    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="test", entity_label="Custom tool"
    )
    await assert_custom_tool_not_package_origin(db, tool.id)

    file_ids: list[str] = (body or {}).get("file_ids", [])
    purged = 0
    for raw_id in file_ids:
        fid = safe_uuid(raw_id)
        if not fid:
            continue
        try:
            row = (
                await db.execute(
                    select(FileAttachment).where(
                        FileAttachment.id == fid,
                        FileAttachment.user_id == user.id,
                    )
                )
            ).scalar_one_or_none()
            if row:
                await file_storage.delete(row.s3_object_name)
                await db.delete(row)
                purged += 1
        except Exception:
            logger.warning("Failed to purge test file %s", raw_id, exc_info=True)
    try:
        await db.commit()
    except Exception:
        logger.warning("Failed to commit test file purge", exc_info=True)
    return {"purged": purged}


@router.post("/{tool_id}/run-tests")
async def run_custom_tool_tests(
    tool_id: str,
    body: RunCustomToolTestsRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Run the tool's declared tests (data cases and test code) and keep the report.

    Ungated like ``/test``: a sandboxed script needs no model. Runs with the
    caller's platform variables, as ``/test`` does.
    """
    if security_option("disable_custom_tools"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=CUSTOM_TOOLS_DISABLED_MSG)
    body = body or RunCustomToolTestsRequest()
    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="test", entity_label="Custom tool"
    )
    if not tool.js_function:
        raise HTTPException(status_code=422, detail="The tool has no code to test.")

    from app.services.variable_resolver import load_variables, substitute_variables

    variables = await load_variables(db, str(user.id), await get_user_group_ids(db, user.id))
    code = substitute_variables(tool.js_function, variables)
    test_code = substitute_variables(tool.test_code, variables) if tool.test_code else None
    test_cases = tool.test_cases
    language = tool.language or "javascript"
    timeout = tool.timeout or 300
    # The sandbox runs for minutes at worst: no pooled connection is held.
    await release_db_connection_before_loop(db)
    report = await run_tool_tests(
        code=code,
        language=language,
        timeout=timeout,
        test_cases=test_cases,
        test_code=test_code,
        user_id=user.id,
        db=db,
        case_names=body.case_names,
        include_code_tests=body.include_code_tests,
        dry_run_only=body.dry_run_only,
    )
    # A partial run (some cases picked) is not the tool's test status.
    if body.case_names is None and body.include_code_tests:
        tool = await get_manageable_entity(
            db, CustomTool, user, tool_id, action="test", entity_label="Custom tool"
        )
        store_test_report(tool, report)
        await db.commit()
    return report


@router.post("/{tool_id}/detach", response_model=CustomToolResponse)
async def detach_custom_tool(
    tool_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Detach a synced tool from its repository source: it becomes an ordinary,
    editable tool, and the source no longer creates or updates it."""
    user_group_ids = await get_user_group_ids(db, user.id)
    tool = await get_manageable_entity(
        db, CustomTool, user, tool_id, action="edit", entity_label="Custom tool",
        user_group_ids=user_group_ids,
    )
    source = await _source_of(db, tool)
    if source is None:
        raise HTTPException(status_code=422, detail="This tool is not synced from a repository.")
    if source.is_company_managed and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only admins can detach tools of a company-managed source.")
    if source.user_id != user.id and not user.is_admin:
        raise HTTPException(status_code=403, detail="Only the source's owner can detach its tools.")
    path = tool.source_path
    if path is not None:
        source.detached_paths = sorted({*(source.detached_paths or []), path})
    detach_tool(tool)
    await log_audit(
        db,
        user=user,
        action="detach",
        entity_type="custom_tool",
        entity_id=tool.id,
        entity_name=tool.name,
        summary=f"Detached custom tool '{tool.name}' from source '{source.name}'",
        request=request,
        details={"source_id": str(source.id), "source_path": path},
    )
    await db.commit()
    await db.refresh(tool)
    return _to_response(
        tool,
        can_manage=can_manage(user, tool, user_group_ids),
        is_package_origin=await is_package_origin_custom_tool(db, tool.id),
    )


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------


@router.get("/{tool_id}/export")
async def export_custom_tool(
    tool_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Export a custom tool as a portable JSON bundle."""
    tool = await get_user_custom_tool(db, user.id, tool_id, is_admin=user.is_admin)

    bundle = {
        "version": 1,
        "export_type": "custom_tool",
        "exported_at": datetime.now(UTC).isoformat(),
        "custom_tool": {
            "name": tool.name,
            "description": tool.description,
            "icon_url": tool.icon_url,
            "input_schema": tool.input_schema,
            "js_function": tool.js_function,
            "language": tool.language or "javascript",
            "tags": tool.tags or [],
            "timeout": tool.timeout or 300,
            "test_cases": tool.test_cases,
            "test_code": tool.test_code,
        },
    }
    from app.core.utils import build_content_disposition

    safe_name = tool.name.lower().replace(" ", "-")
    return JSONResponse(
        content=bundle,
        headers={
            "Content-Disposition": build_content_disposition(
                f"custom-tool-{safe_name}.json"
            )
        },
    )


@router.post(
    "/import", response_model=CustomToolResponse, status_code=status.HTTP_201_CREATED
)
async def import_custom_tool(
    body: ImportCustomToolRequest,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Import a custom tool from an export bundle."""
    if body.version != 1:
        raise HTTPException(
            status_code=400, detail=f"Unsupported export version: {body.version}"
        )
    if body.export_type != "custom_tool":
        raise HTTPException(
            status_code=400, detail=f"Unsupported export type: {body.export_type}"
        )

    data = body.custom_tool
    if not data.get("name"):
        raise HTTPException(status_code=400, detail="Custom tool name is required")
    test_cases, test_code = _validated_tests(data.get("test_cases"), data.get("test_code"))

    tool = CustomTool(
        user_id=user.id,
        name=data["name"],
        description=data.get("description", ""),
        icon_url=data.get("icon_url"),
        input_schema=data.get("input_schema"),
        js_function=data.get("js_function", ""),
        language=data.get("language", "javascript"),
        tags=data.get("tags", []),
        timeout=data.get("timeout", 300),
        is_company_managed=False,
        test_cases=test_cases,
        test_code=test_code,
    )
    db.add(tool)
    await db.flush()
    await log_audit(
        db,
        user=user,
        action="import",
        entity_type="custom_tool",
        entity_id=tool.id,
        entity_name=tool.name,
        summary=f"Imported custom tool '{tool.name}'",
        request=request,
        details={
            "tags": tool.tags or [],
        },
    )
    await db.commit()
    await db.refresh(tool)
    return _to_response(
        tool,
        can_manage=can_manage(user, tool),
        # A bundle import creates a plain tool: no package origin link.
        is_package_origin=False,
        owner_name=user.full_name,
    )
