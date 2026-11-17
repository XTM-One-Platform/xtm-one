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

"""Microsoft AgentSchema v1.0 — vendored spec loading, parsing and validation.

This module owns everything that talks to the *specification*: loading the
vendored JSON Schema documents, building a validator over them, safely
parsing an untrusted YAML/JSON upload, and turning validation failures into
field-path-oriented messages the API can hand back to a user.

Mapping to and from the XTM One agent model lives in :mod:`mapping`; format
detection lives in :mod:`detect`.

Design notes
------------

- **Vendored, pinned schemas.** The schema files under ``schemas/`` are a
  verbatim copy of ``microsoft/AgentSchema@b7833d1`` ``schemas/v1.0/`` (MIT —
  see ``schemas/NOTICE.md``). Validation never touches the network, and an
  upstream edit can never retroactively change how an existing export
  validates.
- **Strict on export, lenient on import.** Documents *we* produce are
  validated strictly against ``PromptAgent.yaml`` before they leave the
  process. Documents we *receive* are normalized first (see
  :func:`normalize_document`), because real-world AgentSchema files are looser
  than the schema — Microsoft's own ``examples/analyst/analyst.yaml`` omits the
  ``kind`` property that ``AgentDefinition.yaml`` marks required.
- **No evaluation of untrusted content.** Parsing is ``yaml.safe_load`` only
  (never ``yaml.load``), byte-bounded by the caller, and additionally guarded
  against YAML alias-expansion bombs, which a size limit alone does not stop.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

# --- Spec identity ---------------------------------------------------------

#: Spec version of the vendored schemas (the upstream ``schemas/<version>``
#: directory name).
AGENTSCHEMA_SPEC_VERSION = "1.0"

#: Upstream commit the vendored schemas were copied from. Kept in sync with
#: ``schemas/NOTICE.md`` by ``tests/test_agentschema_spec.py``.
AGENTSCHEMA_UPSTREAM_COMMIT = "b7833d1ff6888d655643de2f11355e791133b939"

#: The only ``AgentDefinition.kind`` XTM One agents map to. ``hosted``
#: (``ContainerAgent``) and ``workflow`` (``Workflow``) describe
#: container-deployed and multi-step-graph agents, neither of which has an
#: XTM One counterpart — they are rejected at import with an explicit message
#: rather than partially imported.
AGENTSCHEMA_SUPPORTED_KIND = "prompt"

#: Kinds the spec defines, used to tell "an AgentSchema document we can't
#: import" apart from "not an AgentSchema document at all".
AGENTSCHEMA_KNOWN_KINDS = ("prompt", "hosted", "workflow")

#: Free-form ``metadata`` key under which XTM One preserves the agent fields
#: AgentSchema has no representation for. ``metadata`` is declared as
#: ``RecordUnknown`` (arbitrary properties), so this keeps the document
#: spec-valid while making an XTM One -> XTM One round-trip lossless. Other
#: platforms simply ignore the key.
AGENTSCHEMA_METADATA_NAMESPACE = "x-xtm-one"

#: Upload ceiling for a single AgentSchema document. Two orders of magnitude
#: below the 10 MB integration-bundle limit on purpose: an AgentSchema file is
#: one YAML/JSON document, not a ZIP archive of tool sources.
AGENTSCHEMA_MAX_BYTES = 2 * 1024 * 1024

#: Upload ceiling for a native ``version: 3`` agent bundle, which can legibly
#: carry skill/prompt bodies and custom-tool source. Matches the integration
#: bundle precedent.
NATIVE_BUNDLE_MAX_BYTES = 10 * 1024 * 1024

_SCHEMA_DIR = Path(__file__).parent / "schemas"

#: Root schema an XTM One export is validated against.
_ROOT_SCHEMA = "PromptAgent.yaml"

# Nesting ceiling for parsed documents. Guards the recursive walks in
# ``mapping`` (and anything downstream) against a deeply nested upload
# engineered to exhaust the Python stack.
_MAX_PARSE_DEPTH = 64


class _NoAliasSafeLoader(yaml.SafeLoader):
    """``SafeLoader`` that refuses YAML aliases.

    A YAML alias bomb ("billion laughs") stays comfortably under any byte
    ceiling while expanding to gigabytes as the document is *composed*, so
    :data:`AGENTSCHEMA_MAX_BYTES` is not a defence against it on its own.

    Blocking is done here in the composer rather than by pattern-matching the
    source text, because ``*`` and ``&`` are ordinary characters inside YAML
    scalars — a persona containing markdown emphasis (``*like this*``) must not
    be mistaken for an alias. Anchors on their own are harmless; only an alias
    can duplicate a subtree, so only aliases are rejected.
    """

    def compose_node(self, parent, index):  # type: ignore[no-untyped-def]
        if self.check_event(yaml.events.AliasEvent):
            anchor = self.peek_event().anchor
            raise yaml.YAMLError(
                f"YAML aliases are not accepted in agent imports (found '*{anchor}')"
            )
        return super().compose_node(parent, index)


class AgentSchemaError(ValueError):
    """An AgentSchema document could not be parsed or validated.

    Carries the individual, field-path-oriented problems so the API can report
    *which* rules failed rather than a single opaque string.
    """

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.problems = problems or []

    def detail(self) -> str:
        """Single-string rendering for an ``HTTPException`` detail."""
        if not self.problems:
            return self.message
        return f"{self.message} " + "; ".join(self.problems)


@dataclass(slots=True)
class ParsedDocument:
    """A safely parsed YAML/JSON upload."""

    data: dict[str, Any]
    #: ``"yaml"`` or ``"json"`` — reported back so the user can see how their
    #: file was read when detection surprises them.
    syntax: str
    warnings: list[str] = field(default_factory=list)


# --- Vendored schema loading ----------------------------------------------


@functools.lru_cache(maxsize=1)
def _schema_registry() -> tuple[Registry, dict[str, Any]]:
    """Load every vendored schema into a ``referencing`` registry.

    Each upstream file carries a *relative* ``$id`` (e.g. ``$id:
    Model.yaml``), and refs between them are the matching relative URIs
    (``$ref: Model.yaml``), so registering each document under its file name
    is enough to resolve the whole graph offline.

    Cached for the process: parsing ~50 small YAML files is cheap but it is
    pure overhead on every export/import.
    """
    resources: list[tuple[str, Resource]] = []
    root: dict[str, Any] | None = None
    for path in sorted(_SCHEMA_DIR.glob("*.yaml")):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        resources.append(
            (path.name, Resource.from_contents(document, default_specification=DRAFT202012))
        )
        if path.name == _ROOT_SCHEMA:
            root = document
    if root is None:  # pragma: no cover — vendor integrity is test-enforced
        raise RuntimeError(
            f"Vendored AgentSchema is incomplete: {_ROOT_SCHEMA} is missing from "
            f"{_SCHEMA_DIR}."
        )
    return Registry().with_resources(resources), root


@functools.lru_cache(maxsize=1)
def _prompt_agent_validator() -> Draft202012Validator:
    registry, root = _schema_registry()
    return Draft202012Validator(root, registry=registry)


def schema_problems(document: Any) -> list[str]:
    """Validate ``document`` against the vendored ``PromptAgent`` schema.

    Returns human-readable ``"<field path>: <rule>"`` problems, deduplicated
    and ordered by path. Empty means the document is spec-valid.

    Deduplication matters: ``PromptAgent`` pulls in ``AgentDefinition`` via
    ``allOf``, so a document missing a property both declare required (e.g.
    ``kind``) produces the identical complaint twice.
    """
    validator = _prompt_agent_validator()
    seen: list[tuple[str, str]] = []
    for error in validator.iter_errors(document):
        path = "/".join(str(part) for part in error.absolute_path) or "(document root)"
        entry = (path, error.message)
        if entry not in seen:
            seen.append(entry)
    return [f"{path}: {message}" for path, message in sorted(seen)]


def assert_valid_export(document: Any) -> None:
    """Raise if a document *we generated* is not spec-valid.

    An invalid export is our bug, not the user's, so this is a hard internal
    assertion rather than a user-facing validation path.
    """
    problems = schema_problems(document)
    if problems:
        raise AgentSchemaError(
            "Generated AgentSchema document failed validation against the "
            f"official v{AGENTSCHEMA_SPEC_VERSION} PromptAgent schema:",
            problems,
        )


# --- Safe parsing ----------------------------------------------------------


def _exceeds_depth(value: Any, limit: int, level: int = 0) -> bool:
    """Return True when ``value`` nests deeper than ``limit``."""
    if level > limit:
        return True
    if isinstance(value, dict):
        return any(_exceeds_depth(item, limit, level + 1) for item in value.values())
    if isinstance(value, list):
        return any(_exceeds_depth(item, limit, level + 1) for item in value)
    return False


def parse_document(raw: bytes, *, filename: str | None = None) -> ParsedDocument:
    """Parse an untrusted upload into a mapping, safely.

    Accepts YAML or JSON (YAML 1.1 is a superset of JSON, so one loader covers
    both; the reported ``syntax`` is a best-effort label for the user).
    Structural inspection only — nothing in the document is ever evaluated.
    """
    if not raw.strip():
        raise AgentSchemaError("The uploaded file is empty.")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentSchemaError(
            "The uploaded file is not valid UTF-8 text. Agent imports accept a "
            "JSON or YAML document."
        ) from exc

    try:
        data = yaml.load(text, Loader=_NoAliasSafeLoader)  # noqa: S506 - alias-refusing SafeLoader subclass
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        if mark is None:
            # Not a positioned syntax error — this is the alias refusal above
            # (or another loader-level rejection), whose own message is the
            # actionable part.
            raise AgentSchemaError(
                f"The uploaded document could not be read: {exc}."
            ) from exc
        # ``mark`` carries 0-based line/column — worth surfacing, it is usually
        # the whole diagnosis.
        raise AgentSchemaError(
            "The uploaded file is not valid JSON or YAML at line "
            f"{mark.line + 1}, column {mark.column + 1}."
        ) from exc

    if not isinstance(data, dict):
        raise AgentSchemaError(
            "The uploaded document must be a JSON/YAML object at the top level, "
            f"got {type(data).__name__}."
        )
    if _exceeds_depth(data, _MAX_PARSE_DEPTH):
        raise AgentSchemaError(
            f"The uploaded document nests deeper than {_MAX_PARSE_DEPTH} levels."
        )

    syntax = "json" if text.lstrip().startswith(("{", "[")) else "yaml"
    warnings: list[str] = []
    if filename:
        suffix = Path(filename).suffix.lower()
        if suffix in (".yaml", ".yml") and syntax == "json":
            warnings.append(
                f"'{filename}' contains JSON rather than YAML; it was read as JSON."
            )
        elif suffix == ".json" and syntax == "yaml":
            warnings.append(
                f"'{filename}' contains YAML rather than JSON; it was read as YAML."
            )
    return ParsedDocument(data=data, syntax=syntax, warnings=warnings)


# --- Import-side normalization + validation --------------------------------


def normalize_document(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize a received AgentSchema document into its canonical shape.

    Returns ``(document, notes)``. The document is a shallow copy — the caller's
    mapping is never mutated. ``notes`` records every leniency applied, so the
    API can tell the user what it assumed about their file.

    Leniencies, each one earned from a real-world document shape:

    - **Missing ``kind``** defaults to ``prompt``. ``AgentDefinition.yaml``
      marks ``kind`` required, but Microsoft's published
      ``examples/analyst/analyst.yaml`` omits it, and a spec-conformant
      importer that rejects the spec's own example is not useful.
    - **``model`` as a bare string** is expanded to ``{"id": <string>}``, the
      shorthand ``Model.yaml`` documents as "can be used as the single
      property shorthand".
    - **``tools`` as a mapping** (``RecordTool``, the form the upstream
      examples use) is expanded to the array form, taking each key as the
      tool ``name``. Both forms are spec-valid; the rest of our code only has
      to handle one.
    """
    document = dict(data)
    notes: list[str] = []

    if "kind" not in document:
        document["kind"] = AGENTSCHEMA_SUPPORTED_KIND
        notes.append(
            "The document declares no 'kind'; it was read as "
            f"'{AGENTSCHEMA_SUPPORTED_KIND}'."
        )

    model = document.get("model")
    if isinstance(model, str):
        document["model"] = {"id": model}
        notes.append(f"'model' was given as the shorthand string '{model}'.")

    tools = document.get("tools")
    if isinstance(tools, dict):
        expanded: list[dict[str, Any]] = []
        for name, spec in tools.items():
            if not isinstance(spec, dict):
                raise AgentSchemaError(
                    f"tools/{name}: each tool must be an object, got "
                    f"{type(spec).__name__}."
                )
            entry = dict(spec)
            entry.setdefault("name", str(name))
            expanded.append(entry)
        document["tools"] = expanded
        notes.append(
            f"'tools' was given as a mapping; {len(expanded)} tool(s) were read "
            "from its keys."
        )

    return document, notes


def validate_import_document(data: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Normalize then validate a received AgentSchema document.

    Returns ``(normalized document, notes)``. Raises :class:`AgentSchemaError`
    with per-field problems when the document is not a spec-valid
    ``PromptAgent``, or when it declares a kind XTM One does not support.

    Runs *before* anything is persisted — this is the verification loop the
    import contract promises.
    """
    document, notes = normalize_document(data)

    kind = document.get("kind")
    if kind != AGENTSCHEMA_SUPPORTED_KIND:
        if isinstance(kind, str) and kind in AGENTSCHEMA_KNOWN_KINDS:
            raise AgentSchemaError(
                f"This AgentSchema document declares 'kind: {kind}'. XTM One can "
                f"only import 'kind: {AGENTSCHEMA_SUPPORTED_KIND}' agents — "
                "container-deployed ('hosted') and multi-step ('workflow') agents "
                "have no XTM One equivalent."
            )
        raise AgentSchemaError(
            f"Unknown AgentSchema kind {kind!r}. Expected "
            f"'{AGENTSCHEMA_SUPPORTED_KIND}' (supported), 'hosted' or 'workflow'."
        )

    problems = schema_problems(document)
    if problems:
        raise AgentSchemaError(
            "This file is an AgentSchema document but it does not satisfy the "
            f"official v{AGENTSCHEMA_SPEC_VERSION} PromptAgent schema:",
            problems,
        )
    return document, notes


def dump_yaml(document: dict[str, Any]) -> str:
    """Serialize an AgentSchema document as readable YAML.

    ``sort_keys=False`` preserves the insertion order the exporter chose
    (identity, then model, then tools, then instructions), which reads far
    better than alphabetical. Block style with a wide line width keeps
    multi-line instructions as literal blocks instead of folded soup.
    """
    return yaml.safe_dump(
        document,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=100,
    )
