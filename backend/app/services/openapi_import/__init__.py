# SPDX-License-Identifier: Apache-2.0

"""OpenAPI / Swagger import: any REST API described by a spec → agent tools.

A user creates an ``openapi`` integration from a Swagger 2.0 or OpenAPI 3.x
document — JSON or YAML, fetched from a URL, uploaded as a file, or uploaded
as a ZIP archive of the files it is split across — and picks which
operations become tools. There is no generated code: each operation is
converted once into a declarative ``execution`` spec (method, path, where every
argument goes, how it is serialised) that the shared declarative HTTP executor
(``app.services.declarative_http_executor``) interprets on every call — the
same runtime custom integration bundles use.

Modules:

* ``document``   — load + parse (JSON first, safe YAML second), bounded, from
  text or a URL (public and private networks; never loopback / metadata);
* ``bundle``     — the documents a spec references (other files, other URLs,
  an uploaded archive) embedded into one self-contained document;
* ``discovery``  — the OpenID Connect / OAuth2 metadata its schemes name;
* ``schema``     — ``$ref`` resolution and OpenAPI schema → tool JSON Schema;
* ``security``   — security schemes → the integration's authentication options;
* ``operations`` — operations → tools (:func:`analyze_spec`);
* ``service``    — persistence on ``IntegrationOpenAPISpec`` and the runtime view.

The tools are built per call by ``app.integrations.providers.openapi``.

The package re-exports its public names LAZILY (PEP 562 ``__getattr__``):
importing the package imports none of its submodules. importlib holds a
submodule's lock while it imports the parent package, so a package whose
``__init__`` imported its submodules deadlocked two threads on first use: one
importing ``openapi_import.schema`` directly (holding ``schema``, waiting for
the package), the other inside the package ``__init__`` (holding the package,
waiting for ``schema``), which Python reports as "deadlock detected by
_ModuleLock". Keep this module free of eager submodule imports.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.openapi_import.bundle import MAX_ARCHIVE_BYTES
    from app.services.openapi_import.document import (
        MAX_SPEC_BYTES,
        OpenAPIImportError,
        SpecDocument,
        SpecVersion,
        load_spec_source,
        parse_spec_document,
    )
    from app.services.openapi_import.operations import (
        MAX_OPERATIONS,
        MAX_TOOL_NAME_CHARS,
        OperationEntry,
        SpecAnalysis,
        analyze_spec,
    )
    from app.services.openapi_import.security import (
        AUTH_TYPE_CLIENT_CREDENTIALS,
        AUTH_TYPE_OAUTH_PASSWORD,
        KIND_MUTUAL_TLS,
        KIND_OAUTH2_DEVICE,
        KIND_OAUTH2_IMPLICIT,
        AuthOption,
    )
    from app.services.openapi_import.service import (
        OPENAPI_PROVIDER,
        PER_USER_UNRESOLVABLE_PROVIDERS,
        ImportPlan,
        analyze_source,
        apply_plan,
        carry_tool_overrides,
        clean_tool_overrides,
        effective_tools,
        get_spec,
        new_spec_row,
        openapi_integration_ids,
        per_integration_tool_scopes,
        per_user_binding_error,
        pick_probe_tool,
        prepare_import,
        provider_resolves_per_user,
        summarize_tools,
        update_selection,
    )

#: Public name -> the submodule that defines it.
_EXPORTS: dict[str, str] = {
    "MAX_ARCHIVE_BYTES": "bundle",
    **dict.fromkeys(
        (
            "MAX_SPEC_BYTES",
            "OpenAPIImportError",
            "SpecDocument",
            "SpecVersion",
            "load_spec_source",
            "parse_spec_document",
        ),
        "document",
    ),
    **dict.fromkeys(
        ("MAX_OPERATIONS", "MAX_TOOL_NAME_CHARS", "OperationEntry", "SpecAnalysis", "analyze_spec"),
        "operations",
    ),
    **dict.fromkeys(
        (
            "AUTH_TYPE_CLIENT_CREDENTIALS",
            "AUTH_TYPE_OAUTH_PASSWORD",
            "KIND_MUTUAL_TLS",
            "KIND_OAUTH2_DEVICE",
            "KIND_OAUTH2_IMPLICIT",
            "AuthOption",
        ),
        "security",
    ),
    **dict.fromkeys(
        (
            "OPENAPI_PROVIDER",
            "PER_USER_UNRESOLVABLE_PROVIDERS",
            "ImportPlan",
            "analyze_source",
            "apply_plan",
            "carry_tool_overrides",
            "clean_tool_overrides",
            "effective_tools",
            "get_spec",
            "new_spec_row",
            "openapi_integration_ids",
            "per_integration_tool_scopes",
            "per_user_binding_error",
            "pick_probe_tool",
            "prepare_import",
            "provider_resolves_per_user",
            "summarize_tools",
            "update_selection",
        ),
        "service",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve a re-exported name from its submodule on first access."""
    submodule = _EXPORTS.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(importlib.import_module(f"{__name__}.{submodule}"), name)


__all__ = [
    "AUTH_TYPE_CLIENT_CREDENTIALS",
    "AUTH_TYPE_OAUTH_PASSWORD",
    "KIND_MUTUAL_TLS",
    "KIND_OAUTH2_DEVICE",
    "KIND_OAUTH2_IMPLICIT",
    "MAX_ARCHIVE_BYTES",
    "MAX_OPERATIONS",
    "MAX_SPEC_BYTES",
    "MAX_TOOL_NAME_CHARS",
    "OPENAPI_PROVIDER",
    "PER_USER_UNRESOLVABLE_PROVIDERS",
    "AuthOption",
    "ImportPlan",
    "OpenAPIImportError",
    "OperationEntry",
    "SpecAnalysis",
    "SpecDocument",
    "SpecVersion",
    "analyze_source",
    "analyze_spec",
    "apply_plan",
    "carry_tool_overrides",
    "clean_tool_overrides",
    "effective_tools",
    "get_spec",
    "load_spec_source",
    "new_spec_row",
    "openapi_integration_ids",
    "parse_spec_document",
    "per_integration_tool_scopes",
    "per_user_binding_error",
    "pick_probe_tool",
    "prepare_import",
    "provider_resolves_per_user",
    "summarize_tools",
    "update_selection",
]
