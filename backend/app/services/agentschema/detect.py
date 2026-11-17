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

"""Automatic format detection for agent import uploads.

The import contract is "hand us a file, we work out what it is": the user never
declares a format. Two formats are recognised, both by *structural markers*
only — nothing in an uploaded document is ever evaluated:

- the native XTM One bundle (``export_type: agent`` / ``version: <int>`` +
  ``agent`` object), written by ``GET /agents/{id}/export``;
- a Microsoft AgentSchema document (``kind: prompt|hosted|workflow``, or the
  ``name`` + ``model``/``instructions`` shape real-world files use when they
  omit ``kind``).

Detection never validates — it only decides *which* schema the verification
loop should run. That separation is what lets a broken native bundle report
native-bundle problems instead of "this is not an AgentSchema document".
"""

from __future__ import annotations

from typing import Any, Literal

from app.services.agentschema.spec import (
    AGENTSCHEMA_KNOWN_KINDS,
    AgentSchemaError,
)

#: Wire values for the detected/requested format. Used by the export ``format``
#: query parameter, the import response, audit-log details and telemetry, so
#: there is a single vocabulary for "which format" across the stack.
ImportFormat = Literal["native", "agentschema"]

NATIVE_FORMAT: ImportFormat = "native"
AGENTSCHEMA_FORMAT: ImportFormat = "agentschema"

#: Human-readable labels, for error messages and the UI.
FORMAT_LABELS: dict[str, str] = {
    NATIVE_FORMAT: "XTM One native bundle",
    AGENTSCHEMA_FORMAT: "Microsoft AgentSchema",
}

#: Native bundle versions the importer accepts, mirroring ``import_agent``.
SUPPORTED_NATIVE_VERSIONS = (1, 2, 3)


def _looks_native(data: dict[str, Any]) -> bool:
    """True when the document carries the native bundle's markers.

    ``export_type: "agent"`` is the unambiguous marker. The ``version`` +
    ``agent`` pair is the fallback for v1/v2 bundles and for any hand-edited
    file that dropped ``export_type``.
    """
    if str(data.get("export_type") or "") == "agent":
        return True
    return isinstance(data.get("version"), int) and isinstance(data.get("agent"), dict)


def _looks_agentschema(data: dict[str, Any]) -> bool:
    """True when the document carries AgentSchema's markers.

    ``kind`` is the spec's own discriminator. Files in the wild — including
    Microsoft's published ``examples/analyst/analyst.yaml`` — omit it, so the
    fallback is the shape an ``AgentDefinition`` cannot do without: a ``name``
    plus either a model or instructions.
    """
    kind = data.get("kind")
    if isinstance(kind, str) and kind in AGENTSCHEMA_KNOWN_KINDS:
        return True
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        return False
    return bool(data.get("model")) or bool(data.get("instructions"))


def detect_format(data: dict[str, Any]) -> ImportFormat:
    """Decide which format ``data`` is, or explain why it is neither.

    Raises :class:`AgentSchemaError` when nothing matches, or when a document
    matches both — which means it is malformed in a way that guessing would
    only make worse.
    """
    native = _looks_native(data)
    agentschema = _looks_agentschema(data)

    if native and agentschema:
        raise AgentSchemaError(
            "This file matches both supported formats at once (it carries the "
            "native bundle's 'export_type'/'agent' fields and AgentSchema's "
            "'kind'/'model' fields). Remove the fields that do not belong to the "
            "format you intend so the import is unambiguous."
        )
    if native:
        return NATIVE_FORMAT
    if agentschema:
        return AGENTSCHEMA_FORMAT

    present = ", ".join(sorted(str(key) for key in list(data)[:12])) or "(no fields)"
    raise AgentSchemaError(
        "This file is neither an XTM One agent bundle nor a Microsoft "
        "AgentSchema document. An XTM One bundle has 'export_type: agent' with "
        "an 'agent' object; an AgentSchema document has 'kind: prompt' with "
        f"'name' and 'model'. Top-level fields found: {present}."
    )


def validate_native_bundle(data: dict[str, Any]) -> list[str]:
    """Verification loop for a detected native bundle.

    Mirrors the checks ``POST /agents/import`` performs on its typed body, so
    the file-upload path rejects the same documents with the same reasons
    *before* anything is persisted. Returns normalization notes.
    """
    problems: list[str] = []

    version = data.get("version")
    if not isinstance(version, int):
        problems.append(
            f"version: expected one of {SUPPORTED_NATIVE_VERSIONS}, got "
            f"{version!r}."
        )
    elif version not in SUPPORTED_NATIVE_VERSIONS:
        problems.append(
            f"version: unsupported export version {version} (this platform reads "
            f"{', '.join(str(v) for v in SUPPORTED_NATIVE_VERSIONS)})."
        )

    export_type = data.get("export_type", "agent")
    if export_type != "agent":
        problems.append(
            f"export_type: unsupported export type {export_type!r} (expected 'agent')."
        )

    agent = data.get("agent")
    if not isinstance(agent, dict):
        problems.append(
            f"agent: expected an object describing the agent, got "
            f"{type(agent).__name__}."
        )
    elif not str(agent.get("name") or "").strip():
        problems.append("agent/name: an agent name is required.")

    for key in (
        "assignments",
        "custom_tools",
        "mcp_servers",
        "skills",
        "prompts",
        "variables",
        "memories",
    ):
        value = data.get(key)
        if value is not None and not isinstance(value, list):
            problems.append(f"{key}: expected a list, got {type(value).__name__}.")

    if problems:
        raise AgentSchemaError(
            "This file was detected as an XTM One agent bundle but it is not "
            "valid:",
            problems,
        )
    notes: list[str] = []
    if "export_type" not in data:
        notes.append("The bundle declares no 'export_type'; it was read as 'agent'.")
    return notes
