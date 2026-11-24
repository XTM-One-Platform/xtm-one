# SPDX-License-Identifier: Apache-2.0

"""Persistence and runtime view of an OpenAPI integration's spec.

The seam between the pure conversion (``document`` / ``operations``) and
everything that stores or runs it:

* :func:`analyze_source` loads a source (a URL, a file, or a ZIP archive of
  files), bundles the documents it references, fetches the discovery
  documents its OpenID Connect / OAuth2 schemes name, and converts it;
* :func:`prepare_import` loads + converts a source and validates the caller's
  choices (operations, authentication option) into an :class:`ImportPlan`;
* :func:`apply_plan` writes a plan onto an :class:`IntegrationOpenAPISpec`
  row (create, replace and refresh all go through it);
* :func:`effective_tools` is the ONE runtime view of the stored tools — used
  by the tool builder, the tools listing and the connectivity probe alike, so
  what the UI shows is exactly what an agent gets: the credential is placed
  per the chosen option (and withheld from ``security: []`` operations), and a
  parameter the credential fills is removed from the arguments.
"""

from __future__ import annotations

import copy
import functools
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from app.core.parse_offload import run_parse
from app.core.utils import is_valid_uuid
from app.models.integration_openapi_spec import IntegrationOpenAPISpec
from app.services.declarative_http_executor import field_source
from app.services.openapi_import.bundle import bundle_external_refs, load_spec_archive
from app.services.openapi_import.discovery import fetch_discovery
from app.services.openapi_import.document import (
    OpenAPIImportError,
    SpecDocument,
    load_spec_source,
)
from app.services.openapi_import.operations import (
    MAX_TOOL_DESCRIPTION_CHARS,
    MAX_TOOL_NAME_CHARS,
    SpecAnalysis,
    analyze_spec,
    auth_url_base,
    is_builtin_tool_name,
    method_access,
)
from app.services.openapi_import.schema import clean_text
from app.services.openapi_import.security import AuthOption

logger = logging.getLogger(__name__)

#: The catalog key of OpenAPI integrations. ONE provider for every imported
#: API: agent tool allow-lists, the provider kill switch and multi-instance
#: naming all key on the provider, so a per-API key would silently bypass them.
OPENAPI_PROVIDER = "openapi"

#: Providers whose every integration is a different API: all imported
#: OpenAPI specs share the ``openapi`` provider key. "The acting user's own
#: integration of this provider" therefore names no API in particular, and a
#: per-user (template) binding could hand an agent an unrelated API. These
#: providers only ever bind as one specific integration.
PER_USER_UNRESOLVABLE_PROVIDERS: frozenset[str] = frozenset({OPENAPI_PROVIDER})


def provider_resolves_per_user(provider: str) -> bool:
    """Whether a per-user (template) binding can stand for *provider*.

    Accepts a flattened binding key (``openapi#2``) as well as a bare provider.
    """
    return (provider or "").split("#", 1)[0] not in PER_USER_UNRESOLVABLE_PROVIDERS


def per_user_binding_error(providers: list[str] | None) -> str | None:
    """Why *providers* cannot be per-user (template) bindings, or None."""
    refused = sorted({p for p in providers or [] if not provider_resolves_per_user(p)})
    if not refused:
        return None
    return (
        f"{', '.join(refused)} integrations cannot be resolved per user at runtime: "
        "each one is a different API. Bind the specific integration instead."
    )

#: Serialised ceiling of one integration's stored tool definitions.
MAX_TOOL_DEFINITIONS_BYTES = 24 * 1024 * 1024

_MAX_FILENAME_CHARS = 255
#: A tool name a user may set: provider-safe, short enough for the
#: multi-instance suffix (``__<slug>``), which is why ``__`` itself is refused.
_TOOL_NAME_RE = re.compile(rf"^[A-Za-z][A-Za-z0-9_-]{{0,{MAX_TOOL_NAME_CHARS - 1}}}$")


@dataclass
class ImportPlan:
    """A converted source plus the caller's validated choices."""

    document: SpecDocument
    analysis: SpecAnalysis
    selected_keys: list[str]
    auth_option: AuthOption
    source_type: str  # "url" | "file"
    source_url: str | None
    source_filename: str | None
    #: ``operation key → {"name", "description"}`` set by the user.
    tool_overrides: dict[str, dict[str, str]] = field(default_factory=dict)


def resolve_selection(analysis: SpecAnalysis, requested: list[str] | None) -> list[str]:
    """The operation keys to activate, in document order.

    ``None`` selects the default set (supported, not deprecated). Unknown or
    unsupported keys are refused rather than silently dropped: the caller
    picked them from a preview, and a mismatch means the document changed.
    """
    if requested is None:
        return analysis.default_selection
    by_key = {op.key: op for op in analysis.operations}
    unknown = [k for k in requested if k not in by_key]
    if unknown:
        raise OpenAPIImportError(
            "These operations are not in the spec: " + ", ".join(unknown[:10])
            + (" …" if len(unknown) > 10 else "")
        )
    unsupported = [k for k in requested if not by_key[k].supported]
    if unsupported:
        raise OpenAPIImportError(
            "These operations cannot become tools: " + ", ".join(unsupported[:10])
            + (" …" if len(unsupported) > 10 else "")
        )
    wanted = set(requested)
    return [op.key for op in analysis.operations if op.key in wanted]


def clean_tool_overrides(raw: Any, definitions: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Validate user-set tool names and descriptions against the supported operations.

    An empty name or description means "keep the generated one". Raises
    :class:`OpenAPIImportError` for an unknown operation or an invalid name.
    """
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise OpenAPIImportError("Tool overrides must map operation keys to a name and a description.")
    cleaned: dict[str, dict[str, str]] = {}
    for key, value in raw.items():
        if key not in definitions:
            raise OpenAPIImportError(f"'{key}' is not an operation that can become a tool.")
        value = value if isinstance(value, dict) else {}
        entry: dict[str, str] = {}
        name = value.get("name").strip() if isinstance(value.get("name"), str) else ""
        if name:
            if not _TOOL_NAME_RE.match(name) or "__" in name:
                raise OpenAPIImportError(
                    f"The tool name '{name[:80]}' must start with a letter and use at most "
                    f"{MAX_TOOL_NAME_CHARS} letters, digits, '_' or '-', without '__'."
                )
            if is_builtin_tool_name(name):
                raise OpenAPIImportError(f"'{name}' is the name of a built-in tool: choose another one.")
            entry["name"] = name
        description = clean_text(value.get("description"), MAX_TOOL_DESCRIPTION_CHARS)
        if description:
            entry["description"] = description
        if entry:
            cleaned[str(key)] = entry
    return cleaned


def carry_tool_overrides(
    overrides: dict[str, Any] | None, definitions: dict[str, Any]
) -> dict[str, dict[str, str]]:
    """The overrides of operations that still exist in a new version of the document."""
    return {
        key: dict(value)
        for key, value in (overrides or {}).items()
        if key in definitions and isinstance(value, dict)
    }


def resolve_auth_option(analysis: SpecAnalysis, option_id: str | None) -> AuthOption:
    """The chosen (or default) authentication option; refuses unsupported ones."""
    option = analysis.auth_option(option_id or analysis.default_auth_option)
    if option is None:
        valid = ", ".join(o.id for o in analysis.auth_options if o.supported) or "none"
        raise OpenAPIImportError(f"Unknown authentication option '{option_id}'. Valid options: {valid}.")
    if not option.supported:
        raise OpenAPIImportError(
            f"Authentication '{option.label}' is not supported: {option.unsupported_reason}."
        )
    return option


def _source_filename(filename: str | None) -> str | None:
    if not filename:
        return None
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name[:_MAX_FILENAME_CHARS] or None


async def analyze_source(
    *,
    url: str | None,
    content: str | None,
    tool_prefix: str | None = None,
    archive: bytes | None = None,
    server_url_fallback: str | None = None,
) -> tuple[SpecDocument, SpecAnalysis]:
    """Load, bundle and convert a source (parsing and conversion off the event loop).

    *archive* is an uploaded ZIP of spec files, instead of *url* / *content*.
    *server_url_fallback* (the server URL of the integration a replacement is
    for) resolves relative OAuth URLs when the document has no base of its own.
    """
    if archive is not None:
        if (url or "").strip() or (content or "").strip():
            raise OpenAPIImportError("Provide either a spec URL, the spec file content or an archive.")
        document, spec_archive = await load_spec_archive(archive)
    else:
        document, spec_archive = await load_spec_source(url=url, content=content), None
    document, bundle_warnings = await bundle_external_refs(document, archive=spec_archive)
    discovery = await fetch_discovery(document, auth_url_base(document, server_url_fallback))
    analysis = await run_parse(
        functools.partial(
            analyze_spec,
            document,
            tool_prefix=tool_prefix,
            discovery=discovery,
            server_url_fallback=server_url_fallback,
        )
    )
    analysis.warnings = [*bundle_warnings, *analysis.warnings]
    return document, analysis


async def prepare_import(
    *,
    url: str | None,
    content: str | None,
    filename: str | None,
    selected_operations: list[str] | None,
    auth_option_id: str | None,
    tool_prefix: str | None,
    tool_overrides: dict[str, Any] | None = None,
    archive: bytes | None = None,
    server_url_fallback: str | None = None,
) -> ImportPlan:
    """Load, convert and validate an import request (raises OpenAPIImportError)."""
    document, analysis = await analyze_source(
        url=url,
        content=content,
        tool_prefix=tool_prefix,
        archive=archive,
        server_url_fallback=server_url_fallback,
    )
    selected = resolve_selection(analysis, selected_operations)
    option = resolve_auth_option(analysis, auth_option_id)
    overrides = clean_tool_overrides(tool_overrides, analysis.tool_definitions())
    return ImportPlan(
        document=document,
        analysis=analysis,
        selected_keys=selected,
        auth_option=option,
        source_type="url" if (url or "").strip() else "file",
        source_url=(url or "").strip() or None,
        source_filename=None if (url or "").strip() else _source_filename(filename),
        tool_overrides=overrides,
    )


def active_tools_for(
    tool_definitions: dict[str, Any],
    selected_keys: list[str],
    overrides: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """The tools of *selected_keys*, in selection (document) order, overrides applied.

    Raises :class:`OpenAPIImportError` when two of them would share a name.
    """
    tools: list[dict[str, Any]] = []
    for key in selected_keys:
        tool = tool_definitions.get(key)
        if not isinstance(tool, dict):
            continue
        override = (overrides or {}).get(key)
        if isinstance(override, dict):
            tool = {**tool, **{f: override[f] for f in ("name", "description") if override.get(f)}}
        tools.append(tool)
    seen: set[str] = set()
    for tool in tools:
        name = tool.get("name")
        if name in seen:
            raise OpenAPIImportError(f"Two selected tools would be named '{name}': rename one of them.")
        seen.add(name)
    return tools


def apply_plan(row: IntegrationOpenAPISpec, plan: ImportPlan) -> None:
    """Write *plan* onto *row* (every column the conversion owns)."""
    analysis = plan.analysis
    definitions = analysis.tool_definitions()
    size = len(json.dumps(definitions, ensure_ascii=False, default=str))
    if size > MAX_TOOL_DEFINITIONS_BYTES:
        raise OpenAPIImportError(
            "This API is too large to import: its tools would take "
            f"{size // (1024 * 1024)} MB. Split the spec into smaller documents."
        )
    now = datetime.now(UTC)
    row.source_type = plan.source_type
    row.source_url = plan.source_url
    row.source_filename = plan.source_filename
    row.document_format = analysis.document_format
    row.spec_version = analysis.spec_version
    row.document_sha256 = analysis.document_sha256
    row.api_title = analysis.title[:500]
    row.api_version = analysis.api_version[:100]
    row.api_description = analysis.description or None
    row.default_server_url = (analysis.default_server_url or "")[:2000] or None
    row.tool_prefix = analysis.tool_prefix
    row.auth_option = plan.auth_option.to_dict()
    row.auth_options = [o.to_dict() for o in analysis.auth_options]
    row.tags = list(analysis.tags)
    row.operations = [op.catalogue_entry() for op in analysis.operations]
    row.tool_definitions = definitions
    row.selected_operation_keys = list(plan.selected_keys)
    row.tool_overrides = dict(plan.tool_overrides) or None
    row.active_tools = active_tools_for(definitions, plan.selected_keys, plan.tool_overrides)
    row.warnings = list(analysis.warnings)
    row.refreshed_at = now
    row.updated_at = now


def new_spec_row(integration_id: Any, plan: ImportPlan) -> IntegrationOpenAPISpec:
    row = IntegrationOpenAPISpec(integration_id=integration_id)
    apply_plan(row, plan)
    return row


def update_selection(
    row: IntegrationOpenAPISpec,
    requested: list[str],
    tool_overrides: dict[str, Any] | None = None,
) -> list[str]:
    """Replace the selection with *requested* (validated against the stored catalogue).

    *tool_overrides* replaces the user-set tool names and descriptions;
    ``None`` keeps them.
    """
    by_key = {op.get("key"): op for op in row.operations or [] if isinstance(op, dict)}
    unknown = [k for k in requested if k not in by_key]
    if unknown:
        raise OpenAPIImportError("These operations are not in the spec: " + ", ".join(unknown[:10]))
    definitions = row.tool_definitions or {}
    unsupported = [k for k in requested if k not in definitions]
    if unsupported:
        raise OpenAPIImportError("These operations cannot become tools: " + ", ".join(unsupported[:10]))
    wanted = set(requested)
    ordered = [op["key"] for op in row.operations if op.get("key") in wanted]
    overrides = (
        clean_tool_overrides(tool_overrides, definitions)
        if tool_overrides is not None
        else carry_tool_overrides(row.tool_overrides, definitions)
    )
    row.active_tools = active_tools_for(definitions, ordered, overrides)
    row.selected_operation_keys = ordered
    row.tool_overrides = overrides or None
    row.updated_at = datetime.now(UTC)
    return ordered


async def get_spec(
    db: AsyncSession,
    integration_id: Any,
    *,
    runtime_only: bool = False,
) -> IntegrationOpenAPISpec | None:
    """The spec row of an integration.

    ``runtime_only`` loads just what tools are built from — never the
    catalogue or every operation's definition, which can weigh megabytes.
    """
    stmt = select(IntegrationOpenAPISpec).where(
        IntegrationOpenAPISpec.integration_id == integration_id
    )
    if runtime_only:
        stmt = stmt.options(
            load_only(
                IntegrationOpenAPISpec.id,
                IntegrationOpenAPISpec.integration_id,
                IntegrationOpenAPISpec.active_tools,
                IntegrationOpenAPISpec.auth_option,
                IntegrationOpenAPISpec.api_title,
            )
        )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


def openapi_integration_ids(bindings: Any) -> list[str]:
    """The OpenAPI integrations of an agent's or assignment's ``integration_bindings``."""
    if not isinstance(bindings, dict):
        return []
    ids: list[str] = []
    for key, value in bindings.items():
        if str(key).split("#", 1)[0] != OPENAPI_PROVIDER:
            continue
        for item in value if isinstance(value, list) else [value]:
            if item and str(item) not in ids:
                ids.append(str(item))
    return ids


async def per_integration_tool_scopes(
    db: AsyncSession,
    allowed: dict[str, Any] | None,
    integration_ids: list[str],
) -> dict[str, Any] | None:
    """Split a provider-wide ``openapi`` tool allow-list into per-integration keys.

    Every OpenAPI integration shares the ``openapi`` provider key but exposes
    its own API, so a list stored under ``openapi`` is read per integration:
    an integration whose tools the list names is restricted to those tools
    (``openapi@<integration id>``, narrowing a list already there); an
    integration the list does not mention stays unrestricted. An empty list
    names no tool of any integration, so it restricts every one of them to
    none. Names may carry the multi-instance ``__<slug>`` suffix. The editors
    write per-integration keys directly; this gives the API and the agent
    builder the same meaning.
    """
    if not isinstance(allowed, dict) or OPENAPI_PROVIDER not in allowed:
        return allowed
    listed = allowed[OPENAPI_PROVIDER]
    out = {key: value for key, value in allowed.items() if key != OPENAPI_PROVIDER}
    if not isinstance(listed, list):
        return out or None
    names = {str(name) for name in listed}
    for integration_id in integration_ids:
        if not is_valid_uuid(integration_id):
            # A malformed binding is refused by the binding validation; it
            # must not turn into a failed database cast here first.
            continue
        row = await get_spec(db, integration_id, runtime_only=True)
        tool_names = {
            tool.get("name")
            for tool in (row.active_tools if row else None) or []
            if isinstance(tool, dict) and tool.get("name")
        }
        named = sorted(
            name
            for name in tool_names
            if name in names or any(n.startswith(f"{name}__") for n in names)
        )
        if listed and not named:
            continue
        key = f"{OPENAPI_PROVIDER}@{integration_id}"
        existing = out.get(key)
        out[key] = named if not isinstance(existing, list) else [n for n in existing if n in named]
    return out or None


def effective_tools(active_tools: list[Any] | None, auth_option: Any) -> list[dict[str, Any]]:
    """The tools as an agent sees and runs them (see the module docstring).

    Stored tools are auth-agnostic, so the authentication option can change
    without re-converting the document; everything that depends on it is
    applied here, on copies.
    """
    option = AuthOption.from_dict(auth_option) if auth_option else None
    runtime_auth = option.runtime_auth() if option else {"type": "none"}
    credential_param = option.credential_param() if option else None
    # A spec imported before #10187, while a document-wide ``security: []``
    # marked every operation public (Airflow), would never send the credential
    # its user chose: when no stored tool sends it and none records where its
    # public reading comes from (``public_by``, stamped since), every tool does.
    # A selection of operations public on their own keeps them public.
    metas = [
        tool["openapi"]
        for tool in active_tools or []
        if isinstance(tool, dict) and isinstance(tool.get("openapi"), dict)
    ]
    withhold_from_public = not metas or any(
        meta.get("sends_credential", True) is not False or meta.get("public_by") for meta in metas
    )

    out: list[dict[str, Any]] = []
    for tool in active_tools or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("execution"), dict):
            continue
        execution = copy.deepcopy(tool["execution"])
        input_schema = copy.deepcopy(tool.get("input_schema") or {"type": "object", "properties": {}})
        meta = tool.get("openapi") if isinstance(tool.get("openapi"), dict) else {}
        sends_credential = not withhold_from_public or meta.get("sends_credential", True) is not False
        execution["auth"] = runtime_auth if sends_credential else {"type": "none"}

        if credential_param:
            location, name = credential_param
            mapping_key = f"{location}_mapping"
            mapping = execution.get(mapping_key)
            if isinstance(mapping, dict):
                matches = [
                    key
                    for key in mapping
                    if (key.lower() == name.lower() if location == "header" else key == name)
                ]
                removed_args = {field_source(key, mapping.pop(key)) for key in matches}
                if not mapping:
                    execution.pop(mapping_key, None)
                properties = input_schema.get("properties")
                if removed_args and isinstance(properties, dict):
                    for arg in removed_args:
                        properties.pop(arg, None)
                    required = [r for r in input_schema.get("required") or [] if r not in removed_args]
                    if required:
                        input_schema["required"] = required
                    else:
                        input_schema.pop("required", None)
        out.append({**tool, "execution": execution, "input_schema": input_schema})
    return out


def summarize_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``name / description / input_schema / tool_type`` for listings."""
    return [
        {
            "name": t.get("name"),
            "description": t.get("description") or "",
            "input_schema": t.get("input_schema"),
            "tool_type": t.get("tool_type") or "read",
        }
        for t in tools
        if isinstance(t.get("name"), str)
    ]


def imported_operations(active_tools: list[Any] | None) -> list[dict[str, str]]:
    """``name / method / path / operation`` of each stored tool, for listings.

    Read straight from ``active_tools`` (no copy, no auth placement): what the
    agent-builder ``list_integration_tools`` shows of an OpenAPI integration.
    The operation key is the document's ``"METHOD /path"``.
    """
    out: list[dict[str, str]] = []
    for tool in active_tools or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            continue
        execution = tool.get("execution") if isinstance(tool.get("execution"), dict) else {}
        meta = tool.get("openapi") if isinstance(tool.get("openapi"), dict) else {}
        method = str(execution.get("method") or "").upper()
        path = str(execution.get("path") or "")
        operation = str(meta.get("operation") or f"{method} {path}".strip())
        out.append(
            {
                "name": name,
                "method": method,
                "path": path,
                "operation": operation,
                "access": method_access(method),
            }
        )
    return out


def operation_table(active_tools: list[Any] | None, *, limit: int = 60) -> str:
    """One line per imported operation: tool, access, ``METHOD /path``.

    The access is the one a read-only binding enforces (:func:`method_access`),
    so a caller reads the table instead of rebuilding it from the spec.
    """
    operations = imported_operations(active_tools)
    lines = [f"  - {op['name']} [{op['access']}] `{op['method']} {op['path']}`" for op in operations[:limit]]
    if len(operations) > limit:
        lines.append(f"  - and {len(operations) - limit} more")
    return "\n".join(lines)


#: Words that betray a GET with a side effect (APIs do misuse GET): such an
#: operation is never chosen as the probe, which runs on a button press.
_PROBE_UNSAFE_WORDS = re.compile(
    r"(log_?out|sign_?out|delete|remove|revoke|reset|trigger|run|execute|send|"
    r"start|stop|restart|reboot|shutdown|cancel|approve|reject|purge|clear|flush)",
    re.IGNORECASE,
)


def pick_probe_tool(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A GET operation callable with no argument — the connectivity probe.

    Operations that carry the credential first (the test is mostly about the
    credential), then operations that take no argument at all (a search whose
    parameters are all optional usually still needs one of them), then
    health / status / version / capabilities style routes, then the shallowest
    path. Only reads with no required argument qualify, and never one whose
    name or path suggests a side effect.
    """
    candidates = [
        t
        for t in tools
        if (t.get("execution") or {}).get("method") == "GET"
        and not (t.get("input_schema") or {}).get("required")
        and not _PROBE_UNSAFE_WORDS.search(
            f"{t['execution'].get('path') or ''} {t.get('name') or ''}"
        )
    ]
    if not candidates:
        return None

    def rank(tool: dict[str, Any]) -> tuple[int, int, int, int, int]:
        path = str(tool["execution"].get("path") or "")
        lowered = path.lower()
        auth_type = (tool["execution"].get("auth") or {}).get("type")
        # An operation on its own server is called without the credential.
        sends_credential = auth_type not in (None, "none") and not tool["execution"].get("server_url")
        takes_arguments = bool((tool.get("input_schema") or {}).get("properties"))
        preferred = any(
            w in lowered for w in ("health", "status", "ping", "version", "capabilit", "/me")
        )
        return (
            0 if sends_credential else 1,
            1 if takes_arguments else 0,
            0 if preferred else 1,
            path.count("/") + path.count("{") * 5,
            len(path),
        )

    return sorted(candidates, key=rank)[0]
