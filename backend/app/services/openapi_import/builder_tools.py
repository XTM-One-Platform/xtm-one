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

"""The OpenAPI import as the agent builder's integration tools take it.

``create_integration`` and ``update_integration`` read a provider's catalog
marker ``spec_import`` generically; what the marker means for an OpenAPI
integration lives here: the argument's name and JSON Schema (the import block
of ``POST /api/v1/integrations``, plus ``file_id`` for a conversation file),
its validation by the route's own schema, and the texts the tools answer.
"""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from app.api.v1.schemas.integration_openapi import OpenAPIImportOptions
from app.services.openapi_import.service import OPENAPI_PROVIDER

if TYPE_CHECKING:
    from app.tools.base import FileContext

#: The catalog's ``spec_import`` value this module serves, and the name of the
#: tools' argument carrying the spec (the REST create's field name).
SPEC_IMPORT_KIND = OPENAPI_PROVIDER
SPEC_ARGUMENT = "openapi"

#: The ZIP local-file signature: a conversation file holding a split spec.
_ZIP_MAGIC = b"PK\x03\x04"

SPEC_ARGUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "Provider 'openapi' only: the API's OpenAPI 3.x / Swagger 2.0 spec and the "
        "import choices, as POST /api/v1/integrations takes them. Give one source: "
        "url, content (the JSON or YAML text), archive (a base64 ZIP of a split "
        "spec) or file_id (a conversation file). On update it replaces the spec; "
        "the credential type must stay the same."
    ),
    "properties": {
        "url": {"type": "string", "description": "URL of the spec, fetched by the server"},
        "content": {"type": "string", "description": "The spec's JSON or YAML text"},
        "archive": {"type": "string", "description": "A ZIP of the spec's files, base64"},
        "file_id": {"type": "string", "description": "A conversation file holding the spec (or its ZIP)"},
        "filename": {"type": "string", "description": "The spec's file name (.json, .yaml, .yml)"},
        "selected_operations": {
            "type": "array",
            "items": {"type": "string"},
            "description": (
                "Operation keys to expose as tools ('GET /pets/{petId}'); omitted: every "
                "supported, non-deprecated operation"
            ),
        },
        "auth_option": {
            "type": "string",
            "description": "Id of the spec's authentication option (default: the spec's first one)",
        },
        "tool_prefix": {
            "type": "string",
            "description": "Prefix of every tool name ('' for none; default: from the API title)",
        },
        "tool_overrides": {
            "type": "object",
            "description": "Tool name and description by operation key: {key: {name, description}}",
        },
    },
}

#: Appended to the tools' descriptions so the model knows the argument exists.
CREATE_HINT = (
    " Provider 'openapi' imports a REST API from its OpenAPI / Swagger spec given "
    "in openapi (a url, the content, or the file_id of a conversation file): each "
    "selected operation becomes a tool."
)
UPDATE_HINT = " The openapi argument replaces the spec of an OpenAPI integration."

MISSING_SPEC = (
    "An OpenAPI integration needs its spec: pass openapi with the spec's url, its "
    "content, or the file_id of a conversation file."
)
NOT_A_SPEC_PROVIDER = (
    "the openapi argument replaces the spec of an OpenAPI integration, and this one is not"
)


def wrong_provider(provider: str) -> str:
    return (
        f"The openapi argument imports an API spec: it needs provider "
        f"'{OPENAPI_PROVIDER}', not '{provider}'."
    )


async def options_from_argument(
    raw: object, file_context: FileContext | None
) -> tuple[OpenAPIImportOptions | None, str | None]:
    """The tools' ``openapi`` argument as the REST import block.

    *raw* is the block ``POST /api/v1/integrations`` takes, as an object or its
    JSON text, plus ``file_id``: a conversation file whose bytes become the
    ``content`` (a ZIP becomes the ``archive``). Validated by the route's own
    schema. Returns ``(options, None)``, ``(None, None)`` when the argument is
    absent, or ``(None, error)``.
    """
    from app.services.builtin_tools._shared import resolve_file_ref, unresolved_file_message

    if raw is None or raw == "" or raw == {}:
        return None, None
    value: object = raw
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None, "Invalid openapi: must be a JSON object."
    if not isinstance(value, dict):
        return None, "Invalid openapi: must be an object."
    block = dict(value)
    file_id = block.pop("file_id", None)
    if file_id:
        if any(block.get(key) for key in ("url", "content", "archive")):
            return None, "Invalid openapi: give file_id, url, content or archive, only one of them."
        ref = resolve_file_ref(file_context, str(file_id))
        if ref is None or file_context is None:
            return None, unresolved_file_message(file_context, file_id)
        data = await file_context.read_bytes(ref.file_id)
        if data is None:
            return None, f"Error: the content of '{ref.filename}' could not be loaded from storage."
        block.setdefault("filename", ref.filename)
        if ref.filename.lower().endswith(".zip") or data[:4] == _ZIP_MAGIC:
            block["archive"] = base64.b64encode(data).decode("ascii")
        else:
            try:
                block["content"] = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                return None, (
                    f"Invalid openapi: '{ref.filename}' is not a UTF-8 JSON or YAML "
                    "document, nor a ZIP archive of one."
                )
    try:
        return OpenAPIImportOptions.model_validate(block), None
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'openapi'}: {err['msg']}"
            for err in exc.errors()[:5]
        )
        return None, f"Invalid openapi: {problems}."


async def plan_for_create(
    db: Any, caller: Any, options: OpenAPIImportOptions, extra: dict | None
) -> tuple[Any, Any, dict]:
    """``POST /integrations``'s own steps for a new OpenAPI integration.

    The admin switch, the spec-load rate limit (keyed on *caller*), the import
    and the base URL. Returns ``(plan, spec row, extra config)``; raises the
    route's ``HTTPException`` with the document's problem.
    """
    from app.api.v1.integration_openapi import (
        ensure_openapi_enabled,
        limit_spec_fetches,
        openapi_extra_config,
        plan_openapi_import,
    )

    await ensure_openapi_enabled(db)
    await limit_spec_fetches(None, caller)
    plan, row = await plan_openapi_import(options)
    return plan, row, openapi_extra_config(extra, plan)


def status_after_create(integration: Any, current: str | None) -> str | None:
    """The REST create's status rule: an API key is optional, token grants count."""
    from app.api.v1.integration_openapi import token_grant_status
    from app.services.openapi_import.security import (
        AUTH_TYPE_CLIENT_CREDENTIALS,
        AUTH_TYPE_OAUTH_PASSWORD,
    )

    if integration.auth_type == "api_key":
        return "connected"
    if integration.auth_type in (AUTH_TYPE_CLIENT_CREDENTIALS, AUTH_TYPE_OAUTH_PASSWORD):
        return token_grant_status(integration)
    return current


async def replace_for_update(
    db: Any, integration: Any, caller: Any, options: OpenAPIImportOptions
) -> tuple[Any, str | None]:
    """``PUT /{id}/openapi``'s own path on an integration the tool already loaded.

    An OpenAPI integration without a spec yet gets its first one (the route's
    ``attach_spec``). Returns ``(spec row, None)`` or ``(None, refusal)``;
    nothing is committed.
    """
    from fastapi import HTTPException

    from app.api.v1.integration_openapi import (
        attach_spec,
        ensure_openapi_enabled,
        limit_spec_fetches,
        replace_spec,
    )
    from app.services.openapi_import.service import get_spec

    if integration.provider != OPENAPI_PROVIDER:
        return None, NOT_A_SPEC_PROVIDER
    row = await get_spec(db, integration.id)
    try:
        await ensure_openapi_enabled(db)
        await limit_spec_fetches(None, caller)
        if row is None:
            row, _details = await attach_spec(db, integration, options)
        else:
            await replace_spec(integration, row, options)
    except HTTPException as exc:
        return None, str(exc.detail)
    return row, None


def tools_line(row: Any) -> str:
    """The operation table of the tools an imported spec exposes.

    One line per tool with its access and ``METHOD /path``
    (``service.operation_table``), so the caller never rebuilds it from the spec.
    """
    from app.services.openapi_import.service import imported_operations, operation_table

    operations = imported_operations(row.active_tools)
    reads = sum(1 for op in operations if op["access"] == "read")
    if operations:
        line = (
            f"\n  \u2022 Tools ({len(operations)}: {reads} read, {len(operations) - reads} write):\n"
            + operation_table(row.active_tools)
        )
    else:
        line = "\n  \u2022 Tools (0): none selected"
    if row.warnings:
        line += f"\n  \u2022 Spec warnings: {len(row.warnings)} (see the integration's API page)"
    return line
