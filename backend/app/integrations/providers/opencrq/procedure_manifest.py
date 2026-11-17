"""The procedure list a connected OpenCRQ serves about itself, and the tools it cannot serve.

XTM One commits no OpenCRQ input schema: the committed pin of OpenCRQ's procedures
(``tests/data/opencrq_trpc_procedures.json``) holds paths and types only, and each OpenCRQ instance
hands over what it accepts at runtime. That is its ``procedureManifest`` query: the procedure
list with each procedure's input as a JSON Schema, and a fingerprint of the whole.

``tool_procedures.json`` maps each tool to the procedures its handler sends. A tool whose
procedures the connected instance does not all serve is withheld from the model and refused by
name (:func:`availability`, :func:`hidden_refusal`) instead of failing on every call with a bare
"no procedure".

The manifest is read once per tool build, at the first call of one of its tools
(:class:`ManifestProbe`), and fails open: an instance that cannot answer is one whose tools are all
offered and whose inputs are not checked. A document is kept reduced (:func:`reduce_schema`): the
shape of an input, never the value constraints OpenCRQ refuses loudly.

Pure apart from :func:`read_manifest` and :class:`ManifestProbe`: no database, no provider state.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import safe_request

logger = logging.getLogger(__name__)

TOOL_PROCEDURES_PATH = Path(__file__).parent / "tool_procedures.json"
TOOL_INPUT_KEYS_PATH = Path(__file__).parent / "tool_input_keys.json"

#: The query an OpenCRQ instance answers with its manifest; sent by :class:`ManifestProbe`, never through ``_trpc``.
PROCEDURE = "procedureManifest"
#: The layout of the document this module reads.
FORMAT_VERSION = 1
#: How a refusal names the connected OpenCRQ: no instance string (a platform sends it) reaches the model.
INSTANCE_LABEL = "this OpenCRQ instance"

_TIMEOUT = 5.0
# A document is a few hundred KB; past this the answer is not a manifest worth reading.
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_PROCEDURES = 5_000
_MAX_SCHEMA_DEPTH = 64
_FINGERPRINT = re.compile(r"[0-9a-f]{64}")
# An instance without the query, or one that refuses the token, answers these: nothing to warn about.
_EXPECTED_STATUSES = {401, 403, 404, 405}

#: The keywords of an input schema the input check gives a meaning to or ignores on purpose
#: (``enum`` and ``propertyNames`` constrain a value, not the keys).
UNDERSTOOD_KEYWORDS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "additionalProperties",
        "propertyNames",
        "const",
        "enum",
        "$ref",
        "$defs",
    }
)
#: What :func:`reduce_schema` removes: constraints on a value, which OpenCRQ refuses loudly, and annotations.
#: Everything else stays, so a keyword that opens a schema up later is never lost to an allow-list.
VALUE_KEYWORDS = frozenset(
    {
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "multipleOf",
        "default",
        "description",
        "title",
        "examples",
        "uniqueItems",
        "$schema",
        "$comment",
    }
)
# Keywords whose value maps a name to a schema: the names are data, not keywords.
_SCHEMA_MAPS = ("properties", "$defs")
_SCHEMA_LISTS = ("anyOf", "oneOf", "allOf")
_SCHEMA_SINGLES = ("items", "additionalProperties", "propertyNames")


def reduce_schema(schema: Any, unread: set[str] | None = None, _depth: int = 0) -> Any:
    """*schema* without its value constraints, recursively; the names under ``properties`` and ``$defs`` stay.

    A keyword that is neither removed nor read by the input check is added to *unread* when given.
    """
    if not isinstance(schema, dict):
        return schema
    if _depth > _MAX_SCHEMA_DEPTH:
        raise ValueError("input schema nested too deeply")
    reduced: dict[str, Any] = {}
    for keyword, value in schema.items():
        if keyword in VALUE_KEYWORDS:
            continue
        if unread is not None and keyword not in UNDERSTOOD_KEYWORDS:
            unread.add(keyword)
        if keyword in _SCHEMA_MAPS and isinstance(value, dict):
            reduced[keyword] = {name: reduce_schema(child, unread, _depth + 1) for name, child in value.items()}
        elif keyword in _SCHEMA_LISTS and isinstance(value, list):
            reduced[keyword] = [reduce_schema(branch, unread, _depth + 1) for branch in value]
        elif keyword in _SCHEMA_SINGLES:
            reduced[keyword] = reduce_schema(value, unread, _depth + 1)
        else:
            reduced[keyword] = value
    return reduced


@dataclass(frozen=True)
class Manifest:
    """The procedures one OpenCRQ instance serves."""

    #: OpenCRQ's own fingerprint of the document (sha256 hex); ``None`` when it sent none that reads as one.
    fingerprint: str | None
    #: ``path -> {"type": "query" | "mutation", "input": <reduced JSON Schema> | None}``
    procedures: Mapping[str, Mapping[str, Any]]

    def serves(self, procedure: str) -> bool:
        return procedure in self.procedures


def parse_manifest(document: object) -> Manifest | None:
    """The :class:`Manifest` a ``procedureManifest`` document holds, ``None`` when it is not a readable one.

    Format 1: ``formatVersion``, then ``procedures``, a list of ``{path, type, input}``. One entry that
    does not read rejects the document: a procedure dropped from it would hide tools the instance serves.
    Each input is reduced (:func:`reduce_schema`); the keywords the input check does not read are logged once.
    """
    if not isinstance(document, dict):
        return None
    version = document.get("formatVersion")
    if isinstance(version, bool) or version != FORMAT_VERSION:
        return None
    entries = document.get("procedures")
    if not isinstance(entries, list) or not entries or len(entries) > _MAX_PROCEDURES:
        return None
    procedures: dict[str, Mapping[str, Any]] = {}
    unread: set[str] = set()
    try:
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            path, kind, schema = entry.get("path"), entry.get("type"), entry.get("input")
            if not isinstance(path, str) or not path or path in procedures or kind not in ("query", "mutation"):
                return None
            if schema is not None and not isinstance(schema, dict):
                return None
            procedures[path] = MappingProxyType({"type": kind, "input": reduce_schema(schema, unread)})
    except ValueError:
        return None
    if unread:
        logger.info("OpenCRQ manifest: keywords the input check does not read: %s", ", ".join(sorted(unread)))
    fingerprint = document.get("fingerprint")
    return Manifest(
        valid_fingerprint(fingerprint),
        MappingProxyType(procedures),
    )


def valid_fingerprint(value: object) -> str | None:
    """*value* when it reads as a fingerprint (sha256, lower-case hex), else ``None``."""
    return value if isinstance(value, str) and _FINGERPRINT.fullmatch(value) else None


def parse_registered_manifest(document: object, fingerprint: str) -> Manifest | None:
    """The :class:`Manifest` an OpenCRQ registration carried, ``None`` when it is not one to keep.

    *fingerprint* is the one the registration reported. A document of more than the size
    :func:`read_manifest` accepts, one carrying a fingerprint of its own that is not *fingerprint*,
    and one :func:`parse_manifest` cannot read are refused; the manifest returned carries *fingerprint*.
    """
    if not isinstance(document, dict):
        return None
    own = document.get("fingerprint")
    if own is not None and own != fingerprint:
        return None
    try:
        if len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8")) > MAX_MANIFEST_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    manifest = parse_manifest(document)
    return None if manifest is None else Manifest(fingerprint, manifest.procedures)


def stored_manifest(fingerprint: str, procedures: object) -> Manifest | None:
    """The :class:`Manifest` a registration stored: *procedures* as :func:`parse_registered_manifest` kept them.

    ``None`` when what was stored does not read as ``path -> {type, input}``.
    """
    if not isinstance(procedures, dict) or not procedures:
        return None
    kept: dict[str, Mapping[str, Any]] = {}
    for path, entry in procedures.items():
        if not isinstance(entry, dict) or entry.get("type") not in ("query", "mutation"):
            return None
        kept[path] = MappingProxyType({"type": entry["type"], "input": entry.get("input")})
    return Manifest(fingerprint, MappingProxyType(kept))


@functools.cache
def load_tool_procedures(path: Path = TOOL_PROCEDURES_PATH) -> Mapping[str, tuple[str, ...]]:
    """The procedures each tool sends, as ``scripts/dump_opencrq_tool_procedures.py`` wrote them."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return MappingProxyType({tool: tuple(procedures) for tool, procedures in document.items()})


@functools.cache
def load_tool_input_keys(path: Path = TOOL_INPUT_KEYS_PATH) -> Mapping[str, tuple[Mapping[str, Any], ...]]:
    """The input keys each tool sends, as ``scripts/dump_opencrq_tool_procedures.py`` wrote them.

    A tool maps to one entry per call it can send: ``procedures``, ``fields`` (key paths),
    ``conditional`` (the ones sent only sometimes) and ``closed`` (every top-level key is spelled out).
    """
    document = json.loads(path.read_text(encoding="utf-8"))
    return MappingProxyType(
        {tool: tuple(MappingProxyType(entry) for entry in entries) for tool, entries in document.items()}
    )


@dataclass(frozen=True)
class HiddenTool:
    """A tool the connected instance cannot serve."""

    #: The procedures the tool sends that the instance does not list, sorted.
    missing: tuple[str, ...]


def availability(manifest: Manifest, tool_procedures: Mapping[str, Iterable[str]]) -> dict[str, HiddenTool]:
    """The tools *manifest* cannot serve, by name. Empty: every tool is served."""
    hidden: dict[str, HiddenTool] = {}
    for tool, procedures in tool_procedures.items():
        missing = sorted({p for p in procedures if not manifest.serves(p)})
        if missing:
            hidden[tool] = HiddenTool(tuple(missing))
    return hidden


def hidden_refusal(tool: str, label: str, missing: Iterable[str]) -> str:
    """What a call by name to a withheld tool answers."""
    return (
        f"Error: {tool} is not available: {label} does not serve the OpenCRQ procedure(s) "
        f"{', '.join(missing)} that it needs. Use another tool, or ask the OpenCRQ administrator to upgrade."
    )


async def read_manifest(
    base_url: str,
    headers: Mapping[str, str] | None = None,
    refresh: Callable[[], Awaitable[None]] | None = None,
) -> Manifest | None:
    """The manifest the instance at *base_url* serves, ``None`` when it cannot be read.

    One authenticated ``GET``, on the egress route in force. *refresh* runs first: the token of a
    platform-managed integration lives ten minutes, and the build that holds it may be older.
    Fail open: a manifest that cannot be read leaves the tools offered and their inputs unchecked.
    """
    try:
        if refresh is not None:
            await refresh()
        async with egress_client(timeout=_TIMEOUT) as client:
            response, error = await safe_request(
                client,
                "get",
                f"{base_url.rstrip('/')}/trpc/{PROCEDURE}",
                "OpenCRQ",
                "read the procedure manifest",
                headers=dict(headers or {}),
                expected_statuses=_EXPECTED_STATUSES,
                max_total_seconds=_TIMEOUT,
            )
        if error or response is None:
            return None
        if len(response.content) > MAX_MANIFEST_BYTES:
            logger.info("OpenCRQ procedure manifest is larger than %d bytes; ignored", MAX_MANIFEST_BYTES)
            return None
        payload = response.json()
        result = payload.get("result") if isinstance(payload, dict) else None
        manifest = parse_manifest(result.get("data") if isinstance(result, dict) else None)
        if manifest is None:
            logger.info("OpenCRQ answered the procedure manifest query with a document that does not read; ignored")
        return manifest
    except Exception as exc:
        logger.info(
            "OpenCRQ procedure manifest read failed (%s); its tools are all offered", format_exception_message(exc)
        )
        return None


class ProbeLater:
    """The ``opencrq_manifest`` a build is given when no registration holds one: read it at the first call."""

    def __repr__(self) -> str:
        return "PROBE"


PROBE = ProbeLater()


@dataclass(frozen=True)
class Assessment:
    """What a manifest means for the tools: the tools it cannot serve."""

    manifest: Manifest | None
    hidden: Mapping[str, HiddenTool]

    def refusal(self, tool: str) -> str | None:
        """What a call to *tool* answers, ``None`` when the instance serves it."""
        gap = self.hidden.get(tool)
        return None if gap is None else hidden_refusal(tool, INSTANCE_LABEL, gap.missing)


class ManifestProbe:
    """The connected instance's manifest for one tool build, read once and never carried to the next.

    :meth:`known` holds a manifest in hand (``None``: there is none, the build fails open). A probe made
    with a base URL reads it at the first :meth:`assessment` (a concurrent first caller waits for it).
    """

    def __init__(
        self,
        base_url: str | None = None,
        headers: Mapping[str, str] | None = None,
        refresh: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._base_url = base_url
        self._headers = headers
        self._refresh = refresh
        self._lock = asyncio.Lock()
        self._loaded = base_url is None
        self.manifest: Manifest | None = None
        self._assessment: Assessment | None = None

    @classmethod
    def known(cls, manifest: Manifest | None) -> ManifestProbe:
        probe = cls()
        probe.manifest = manifest
        return probe

    @property
    def loaded(self) -> bool:
        """Whether the manifest is in hand (given, or already read)."""
        return self._loaded

    async def load(self) -> Manifest | None:
        """The manifest, ``None`` when there is none or it could not be read."""
        async with self._lock:
            if not self._loaded:
                self._loaded = True
                self.manifest = await read_manifest(self._base_url or "", self._headers, self._refresh)
        return self.manifest

    def assess(self) -> Assessment:
        """The assessment of the manifest in hand: :meth:`known`, or after :meth:`load`."""
        if self._assessment is None:
            hidden = availability(self.manifest, load_tool_procedures()) if self.manifest is not None else {}
            self._assessment = Assessment(self.manifest, hidden)
        return self._assessment

    async def assessment(self) -> Assessment:
        await self.load()
        return self.assess()
