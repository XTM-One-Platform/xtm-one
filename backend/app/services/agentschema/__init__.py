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

"""Agent portable formats: the native XTM One bundle and Microsoft AgentSchema.

Public entry points:

- :func:`prepare_agent_import` — parse an uploaded file, auto-detect its
  format, run the verification loop, and return a native bundle ready for
  :mod:`app.services.agent_import`. Nothing is persisted here.
- :func:`build_agentschema_export` — render an agent's native bundle as a
  spec-valid AgentSchema YAML document.
- :func:`portability_report` — what an AgentSchema export would not carry,
  for the pre-export confirmation.

Sub-modules: :mod:`spec` (vendored schemas, parsing, validation),
:mod:`detect` (format detection), :mod:`mapping` (field mapping both ways).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.agentschema.corrections import (
    ImportCorrection,
    apply_corrections,
    derive_corrections,
)
from app.services.agentschema.detect import (
    AGENTSCHEMA_FORMAT,
    FORMAT_LABELS,
    NATIVE_FORMAT,
    SUPPORTED_NATIVE_VERSIONS,
    ImportFormat,
    detect_format,
    validate_native_bundle,
)
from app.services.agentschema.mapping import (
    NATIVE_BUNDLE_VERSION,
    PortabilityEntry,
    PortabilityReport,
    agentschema_to_native_bundle,
    build_agentschema_document,
    portability_report,
)
from app.services.agentschema.spec import (
    AGENTSCHEMA_MAX_BYTES,
    AGENTSCHEMA_METADATA_NAMESPACE,
    AGENTSCHEMA_SPEC_VERSION,
    AGENTSCHEMA_SUPPORTED_KIND,
    AGENTSCHEMA_UPSTREAM_COMMIT,
    NATIVE_BUNDLE_MAX_BYTES,
    AgentSchemaError,
    assert_valid_export,
    dump_yaml,
    parse_document,
    schema_problems,
    validate_import_document,
)

__all__ = [
    "AGENTSCHEMA_FORMAT",
    "AGENTSCHEMA_MAX_BYTES",
    "AGENTSCHEMA_METADATA_NAMESPACE",
    "AGENTSCHEMA_SPEC_VERSION",
    "AGENTSCHEMA_SUPPORTED_KIND",
    "AGENTSCHEMA_UPSTREAM_COMMIT",
    "FORMAT_LABELS",
    "NATIVE_BUNDLE_MAX_BYTES",
    "NATIVE_BUNDLE_VERSION",
    "NATIVE_FORMAT",
    "SUPPORTED_NATIVE_VERSIONS",
    "AgentSchemaError",
    "ImportCheck",
    "ImportCheckStage",
    "ImportCorrection",
    "ImportFormat",
    "PortabilityEntry",
    "PortabilityReport",
    "PreparedImport",
    "apply_corrections",
    "build_agentschema_export",
    "check_agent_import",
    "derive_corrections",
    "max_upload_bytes",
    "portability_report",
    "prepare_agent_import",
    "schema_problems",
    "validate_native_bundle",
]


@dataclass(slots=True)
class PreparedImport:
    """A validated upload, ready for the shared agent-creation pipeline."""

    #: Which format the file turned out to be.
    format: ImportFormat
    #: The upload mapped onto the native bundle shape.
    bundle: dict[str, Any]
    #: Leniencies applied while reading the file (missing ``kind``, shorthand
    #: ``model``, ...) — informational, the import succeeded.
    notes: list[str] = field(default_factory=list)
    #: References the file makes that this instance cannot satisfy (external
    #: MCP servers, knowledge bases, unimplementable tools). Surfaced, never
    #: silently ignored.
    warnings: list[str] = field(default_factory=list)

    @property
    def format_label(self) -> str:
        return FORMAT_LABELS.get(self.format, self.format)


def max_upload_bytes() -> int:
    """Read ceiling for an agent import upload, before the format is known.

    Detection needs the parsed document, so the endpoint reads up to the
    larger of the two per-format limits and the format-specific limit is
    enforced once detection has run.
    """
    return max(NATIVE_BUNDLE_MAX_BYTES, AGENTSCHEMA_MAX_BYTES)


def _enforce_size(format_: ImportFormat, size: int) -> None:
    """Apply the per-format size ceiling.

    The two differ by design: a native bundle is an archive-like document that
    can legitimately carry skill bodies and custom-tool source, while an
    AgentSchema document is a single declaration.
    """
    limit = NATIVE_BUNDLE_MAX_BYTES if format_ == NATIVE_FORMAT else AGENTSCHEMA_MAX_BYTES
    if size > limit:
        raise AgentSchemaError(
            f"This {FORMAT_LABELS[format_]} file is {size // 1024} KB, over the "
            f"{limit // (1024 * 1024)} MB limit for that format."
        )


def prepare_agent_import(
    raw: bytes, *, filename: str | None = None
) -> PreparedImport:
    """Parse, detect, validate and normalize an agent import upload.

    The whole verification loop, in order: safe parse -> format detection ->
    per-format size ceiling -> schema validation -> mapping onto the native
    bundle shape. Raises :class:`AgentSchemaError` (with per-field problems
    where the schema produced them) at the first step that fails, always
    *before* any database write.
    """
    parsed = parse_document(raw, filename=filename)
    detected = detect_format(parsed.data)
    _enforce_size(detected, len(raw))

    notes = list(parsed.warnings)

    if detected == NATIVE_FORMAT:
        notes.extend(validate_native_bundle(parsed.data))
        return PreparedImport(format=detected, bundle=parsed.data, notes=notes)

    document, spec_notes = validate_import_document(parsed.data)
    notes.extend(spec_notes)
    bundle, warnings = agentschema_to_native_bundle(document)
    return PreparedImport(
        format=detected, bundle=bundle, notes=notes, warnings=warnings
    )


#: The verification stages :func:`check_agent_import` reports, in the order
#: they run. Kept as data so the endpoint schema and the UI can rely on a
#: stable set of keys.
IMPORT_CHECK_STAGES: tuple[str, ...] = ("parse", "detect", "schema", "references")


@dataclass(slots=True)
class ImportCheckStage:
    """One step of the verification loop, with its verdict."""

    key: str
    status: str  # "passed" | "failed" | "skipped"
    problems: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportCheck:
    """Dry-run verdict on an agent import upload. Nothing is persisted.

    Unlike :func:`prepare_agent_import`, a failing document does not raise:
    the caller gets the stage that failed and its problems, with the remaining
    stages marked ``skipped``, so the UI can show *where* the verification
    loop stopped.
    """

    ok: bool
    stages: list[ImportCheckStage]
    format: ImportFormat | None = None
    #: The upload mapped onto the native bundle shape — returned so a
    #: confirming call can import exactly what was checked (possibly after
    #: corrections) instead of re-uploading the file blind.
    bundle: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def format_label(self) -> str | None:
        return FORMAT_LABELS.get(self.format) if self.format else None


def check_agent_import(raw: bytes, *, filename: str | None = None) -> ImportCheck:
    """Run the import verification loop as a dry-run, stage by stage.

    Same steps in the same order as :func:`prepare_agent_import` — safe parse,
    format detection (with the per-format size ceiling), schema validation,
    mapping/reference resolution — but failures are reported per stage rather
    than raised. The ``references`` stage carries the unsatisfiable references
    for AgentSchema documents; a native bundle's references are resolved at
    creation time, so for that format the stage passes with no findings.
    """
    stages: list[ImportCheckStage] = []

    def _failed(key: str, err: AgentSchemaError) -> ImportCheck:
        stages.append(
            ImportCheckStage(
                key=key, status="failed", problems=[err.message, *err.problems[:20]]
            )
        )
        done = {stage.key for stage in stages}
        stages.extend(
            ImportCheckStage(key=k, status="skipped")
            for k in IMPORT_CHECK_STAGES
            if k not in done
        )
        return ImportCheck(ok=False, stages=stages, format=detected)

    detected: ImportFormat | None = None
    try:
        parsed = parse_document(raw, filename=filename)
    except AgentSchemaError as err:
        return _failed("parse", err)
    stages.append(ImportCheckStage(key="parse", status="passed"))
    notes = list(parsed.warnings)

    try:
        detected = detect_format(parsed.data)
        _enforce_size(detected, len(raw))
    except AgentSchemaError as err:
        return _failed("detect", err)
    stages.append(ImportCheckStage(key="detect", status="passed"))

    if detected == NATIVE_FORMAT:
        try:
            notes.extend(validate_native_bundle(parsed.data))
        except AgentSchemaError as err:
            return _failed("schema", err)
        stages.append(ImportCheckStage(key="schema", status="passed"))
        stages.append(ImportCheckStage(key="references", status="passed"))
        return ImportCheck(
            ok=True, stages=stages, format=detected, bundle=parsed.data, notes=notes
        )

    try:
        document, spec_notes = validate_import_document(parsed.data)
    except AgentSchemaError as err:
        return _failed("schema", err)
    stages.append(ImportCheckStage(key="schema", status="passed"))
    notes.extend(spec_notes)

    try:
        bundle, warnings = agentschema_to_native_bundle(document)
    except AgentSchemaError as err:
        return _failed("references", err)
    stages.append(ImportCheckStage(key="references", status="passed"))

    return ImportCheck(
        ok=True,
        stages=stages,
        format=detected,
        bundle=bundle,
        notes=notes,
        warnings=warnings,
    )


def build_agentschema_export(bundle: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Render a native bundle as AgentSchema YAML.

    Returns ``(yaml text, document)``. The document is validated against the
    vendored official schema before it is serialized, so an export can never
    leave the platform claiming to be AgentSchema while failing it.
    """
    document = build_agentschema_document(bundle)
    assert_valid_export(document)
    return dump_yaml(document), document
