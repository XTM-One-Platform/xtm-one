# SPDX-License-Identifier: Apache-2.0

"""The operations of an OpenAPI document → declarative HTTP tools.

:func:`analyze_spec` is the whole conversion: it reads the servers, the
security schemes and every operation, and produces for each supported
operation ONE tool — a name, a description, an argument schema and the
``execution`` spec the declarative executor interprets
(``app.services.declarative_http_executor``). Nothing is generated as code.

How an operation's inputs become tool arguments:

* **parameters** (path, query, header, cookie; the operation's override the
  path item's by ``(in, name)``) become arguments named after the parameter,
  sanitised to the characters every LLM provider accepts; the mapping records
  the original name, the location and the OpenAPI serialisation style
  (Swagger 2.0 ``collectionFormat`` is translated onto the 3.x styles). An
  OpenAPI 3.2 ``querystring`` parameter is one argument holding the whole
  query string (form fields, or a value serialised as its media type);
* a **JSON object body** is flattened: each top-level property becomes an
  argument (``body_<name>`` on a clash with a parameter). Any other body — an
  array, a primitive, a ``oneOf``, a free-form object, or an object with more
  than :data:`MAX_FLATTENED_BODY_FIELDS` fields — is ONE ``body`` argument;
* **form** (urlencoded / multipart) bodies — Swagger 2.0 ``formData``
  parameters included — are flattened the same way, sent form-encoded;
* **file content** — a multipart file field (Swagger 2.0 ``type: file``
  included), or a body in any other media type (``application/octet-stream``,
  images, PDF, XML, a ``text/*`` file…) — becomes a file argument
  (:func:`file_argument_schema`): a file available in the conversation, or
  text the model writes;
* a **sequential body** (OpenAPI 3.2 ``itemSchema`` on JSON Lines, NDJSON or
  JSON text sequences) is ONE ``items`` array argument, sent one item per
  record.

Every HTTP method becomes a tool: GET, PUT, POST, DELETE, PATCH, HEAD and
OPTIONS, the 3.2 QUERY method and the custom methods of 3.2
``additionalOperations``. HEAD and OPTIONS answer with the status and the
response headers. TRACE and CONNECT never do: TRACE echoes the request back,
credentials included, and CONNECT opens a tunnel instead of calling the API.

What makes an operation unsupported (listed, with the reason, never hidden):
TRACE and CONNECT, file content in a parameter or in a form-urlencoded body,
unsupported path styles, and definitions that cannot be resolved.

Relative server URLs resolve against the document's base URI — its 3.2
``$self`` when declared, else where it was fetched from.

Tool names come from the ``operationId`` (snake_cased; else verb + path), are
prefixed with a short API name (``petstore_find_pets_by_status``), unique, and
capped at :data:`MAX_TOOL_NAME_CHARS` — well under the 64-character provider
limit, because an agent bound to two OpenAPI integrations suffixes every tool
with an instance slug (``integration_tools._suffix_instance_tools``) and a
name leaving no room for it would drop that integration's tools entirely. The
suffixed name is ``<tool>__<slug>``, the slug being 4 to 24 characters: a
48-character name keeps 64 - 48 - 2 = 14 of them (the test suite ties the
three numbers together).
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin, urlparse

from app.services.declarative_http_executor import same_origin, validate_execution_spec
from app.services.openapi_import.bundle import document_base_uri
from app.services.openapi_import.document import SpecDocument
from app.services.openapi_import.schema import (
    SWAGGER2_PARAMETER_SCHEMA_KEYS,
    RefResolver,
    SchemaConverter,
    UnresolvedRefError,
    clean_text,
    is_binary_schema,
)
from app.services.openapi_import.security import (
    AuthOption,
    extract_auth_options,
    requirement_schemes,
    security_scheme_definitions,
)

#: Operations analysed at most (the rest are reported, not imported).
MAX_OPERATIONS = 2000
MAX_TOOL_NAME_CHARS = 48
MAX_TOOL_PREFIX_CHARS = 16
#: Serialised size one tool's argument schema should fit in; nesting is cut
#: step by step (:data:`_DEPTH_STEPS`) until it does.
MAX_TOOL_SCHEMA_CHARS = 16_000
MAX_FLATTENED_BODY_FIELDS = 60

_DEPTH_STEPS = (8, 5, 3, 2, 1)
_MAX_SCHEMA_NODES = 4_000
MAX_TOOL_DESCRIPTION_CHARS = 1_000
#: Argument names a spec may not take: the tool runtime injects the
#: conversation's files into a handler under ``file_context``.
_RESERVED_ARGUMENTS = frozenset({"file_context"})
_PARAM_DESCRIPTION_CHARS = 600
_SUMMARY_CHARS = 200
_MAX_SERVERS = 10
_MAX_ISSUES_LISTED = 5

#: Path-item fields that are operations, by dialect (3.2 adds QUERY).
_METHODS_V2 = ("get", "put", "post", "delete", "options", "head", "patch")
_METHODS_V3 = (*_METHODS_V2, "trace")
_METHODS_V32 = (*_METHODS_V3, "query")
#: Never a tool, and why (the executor refuses to send them too).
_NEVER_METHODS = {
    "TRACE": "the server echoes the request back, credentials included",
    "CONNECT": "it opens a tunnel through the server instead of calling the API",
}
#: Methods that only read (``tool_type = "read"``).
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "QUERY"})
#: Methods that cannot carry a request body.
_BODYLESS_METHODS = frozenset({"GET", "HEAD"})
_METHOD_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


def method_access(method: object) -> str:
    """``read`` or ``write`` for an imported operation, from its HTTP method.

    The one rule for an imported operation's access: the method the spec
    declares, never the generated tool name (a ``GET`` named ``create_...``
    stays a read). What a read-only binding keeps and what listings report.
    """
    return "read" if str(method or "").upper() in _SAFE_METHODS else "write"


_LOCATION_ORDER = {"path": 0, "query": 1, "querystring": 1, "header": 2, "cookie": 3}
#: Sequential media types (OpenAPI 3.2 ``itemSchema``) → body encoding.
_SEQUENTIAL_MEDIA = {
    "application/jsonl": "jsonl",
    "application/x-jsonlines": "jsonl",
    "application/x-ndjson": "jsonl",
    "application/json-seq": "json-seq",
}
#: Header parameters OpenAPI says to ignore — the transport owns them.
_IGNORED_HEADER_PARAMS = frozenset({"accept", "content-type", "authorization"})
#: Header names a parameter may not claim (see the executor's reserved set).
_UNMAPPABLE_HEADERS = frozenset(
    {
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "expect",
    }
)
_PREFIX_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "api",
        "apis",
        "definition",
        "docs",
        "documentation",
        "example",
        "for",
        "http",
        "json",
        "of",
        "openapi",
        "platform",
        "public",
        "rest",
        "restful",
        "sample",
        "server",
        "service",
        "services",
        "spec",
        "specification",
        "swagger",
        "the",
        "to",
        "web",
    }
)
_V2_COLLECTION_STYLES = {
    "csv": ("form", False),
    "ssv": ("spaceDelimited", False),
    "tsv": ("tabDelimited", False),
    "pipes": ("pipeDelimited", False),
    "multi": ("form", True),
}
_DEFAULT_STYLES = {"path": "simple", "query": "form", "header": "simple", "cookie": "form"}
_ALLOWED_STYLES = {
    "path": {"simple"},
    # tabDelimited: Swagger 2.0's collectionFormat "tsv".
    "query": {"form", "spaceDelimited", "pipeDelimited", "tabDelimited", "deepObject"},
    "header": {"simple"},
    "cookie": {"form", "cookie"},
}


def _may_be_object(schema: dict[str, Any]) -> bool:
    """Whether a converted argument schema admits an object value."""
    if not schema:
        return True
    declared = schema.get("type")
    types = declared if isinstance(declared, list) else [declared]
    if "object" in types or "properties" in schema or "additionalProperties" in schema:
        return True
    if declared is None and not any(k in schema for k in ("enum", "const")):
        alternatives = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(alternatives, list):
            return any(isinstance(a, dict) and _may_be_object(a) for a in alternatives)
        return "items" not in schema
    return False

_ARG_CHARS_RE = re.compile(r"[^A-Za-z0-9_]+")
_CAMEL_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_2 = re.compile(r"([a-z0-9])([A-Z])")
_PLACEHOLDER_RE = re.compile(r"\{([^{}/]+)\}")
_VERSION_WORD_RE = re.compile(r"^v?\d+$")
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")


class _UnsupportedOperationError(Exception):
    """This operation cannot become a tool; the message says why."""


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OperationEntry:
    """One operation of the document, supported or not."""

    key: str
    method: str
    path: str
    operation_id: str | None
    summary: str
    tags: list[str]
    deprecated: bool
    tool_type: str
    #: Whether calls carry the integration's credential (``security: []`` = no).
    sends_credential: bool
    security_schemes: list[str]
    supported: bool = True
    skip_reason: str | None = None
    tool_name: str | None = None
    #: Whether a tool argument carries a file.
    accepts_files: bool = False
    warnings: list[str] = field(default_factory=list)
    tool: dict[str, Any] | None = None

    def catalogue_entry(self) -> dict[str, Any]:
        """Everything but the tool definition — what the picker lists."""
        return {
            "key": self.key,
            "method": self.method,
            "path": self.path,
            "operation_id": self.operation_id,
            "tool_name": self.tool_name,
            "summary": self.summary,
            "tags": list(self.tags),
            "deprecated": self.deprecated,
            "tool_type": self.tool_type,
            "sends_credential": self.sends_credential,
            "security_schemes": list(self.security_schemes),
            "supported": self.supported,
            "skip_reason": self.skip_reason,
            "accepts_files": self.accepts_files,
            "warnings": list(self.warnings),
        }


@dataclass
class SpecAnalysis:
    """The converted document: metadata, auth options, operations, tools."""

    document_format: str
    spec_version: str
    spec_family: str
    document_sha256: str
    source_url: str | None
    title: str
    api_version: str
    description: str
    servers: list[dict[str, Any]]
    default_server_url: str | None
    auth_options: list[AuthOption]
    default_auth_option: str
    tool_prefix: str
    tags: list[dict[str, Any]]
    operations: list[OperationEntry]
    warnings: list[str]

    @property
    def supported_keys(self) -> list[str]:
        return [op.key for op in self.operations if op.supported]

    @property
    def default_selection(self) -> list[str]:
        """What the picker pre-selects: supported, not deprecated."""
        return [op.key for op in self.operations if op.supported and not op.deprecated]

    def auth_option(self, option_id: str | None) -> AuthOption | None:
        return next((o for o in self.auth_options if o.id == option_id), None)

    def tool_definitions(self) -> dict[str, dict[str, Any]]:
        """``operation key → tool`` for every supported operation."""
        return {op.key: op.tool for op in self.operations if op.supported and op.tool}


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------


def ascii_text(text: str) -> str:
    """Accented letters to their ASCII base (``Géocodage`` → ``Geocodage``)."""
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")


def snake_case(text: str) -> str:
    """``findPetsByStatus`` / ``repos/list-for-org`` → ``find_pets_by_status``."""
    text = ascii_text(text)
    text = _CAMEL_1.sub(r"\1_\2", text)
    text = _CAMEL_2.sub(r"\1_\2", text)
    text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").lower()
    return re.sub(r"_+", "_", text)


def normalize_tool_prefix(raw: str | None) -> str:
    """A user-supplied prefix, reduced to ``[a-z0-9_]`` and capped."""
    text = re.sub(r"[^a-z0-9]+", "_", ascii_text(raw or "").lower()).strip("_")
    if text and text[0].isdigit():
        text = f"api_{text}"
    return text[:MAX_TOOL_PREFIX_CHARS].rstrip("_")


def default_tool_prefix(title: str) -> str:
    """A short API name from ``info.title``: "Swagger Petstore - OpenAPI 3.0" → ``petstore``.

    Up to two meaningful words, as long as they fit whole in
    :data:`MAX_TOOL_PREFIX_CHARS`.
    """
    words = re.findall(r"[a-z0-9]+", ascii_text(title or "").lower())
    kept = [w for w in words if w not in _PREFIX_STOPWORDS and not _VERSION_WORD_RE.match(w)]
    chosen = kept[:1]
    if len(kept) > 1 and len(f"{kept[0]}_{kept[1]}") <= MAX_TOOL_PREFIX_CHARS:
        chosen = kept[:2]
    return normalize_tool_prefix("_".join(chosen))


def safe_argument_name(raw: Any) -> str:
    """A tool-argument name every provider accepts (``^[A-Za-z_][A-Za-z0-9_]*$``)."""
    text = re.sub(r"_+", "_", _ARG_CHARS_RE.sub("_", ascii_text(str(raw)))).strip("_")
    if not text:
        return ""
    if text[0].isdigit():
        text = f"p_{text}"
    return text[:64]


_FILE_HOW_TO = (
    "Set file_id to a file available in this conversation, or content to the file's text."
)


def file_argument_schema(description: str | None, *, multiple: bool = False) -> dict[str, Any]:
    """The argument schema of a file to upload (or of several, as a list)."""
    one: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_id": {
                "type": "string",
                "description": "id of a file available in this conversation",
            },
            "content": {
                "type": "string",
                "description": "the file's text, when no file_id is given",
            },
            "filename": {"type": "string", "description": "name sent with the file, e.g. data.csv"},
            "content_type": {"type": "string", "description": "media type of the file, e.g. text/csv"},
        },
    }
    lead = clean_text(description, _PARAM_DESCRIPTION_CHARS)
    text = f"{lead.rstrip('.')}. {_FILE_HOW_TO}" if lead else _FILE_HOW_TO
    if multiple:
        return {"type": "array", "description": f"{text} One entry per file.", "items": one}
    return {**one, "description": text}


def _base_tool_name(operation_id: str | None, method: str, path: str) -> str:
    if operation_id:
        name = snake_case(operation_id)
        if name:
            return name
    segments = [s for s in path.split("/") if s]
    words: list[str] = []
    for segment in segments:
        if segment.startswith("{") and segment.endswith("}"):
            words.append("by_" + snake_case(segment[1:-1]))
        else:
            words.append(snake_case(segment))
    last_is_param = bool(segments) and segments[-1].startswith("{")
    verb = {
        "get": "get" if last_is_param else "list",
        "post": "create",
        "put": "update",
        "patch": "update",
        "delete": "delete",
    }.get(method) or snake_case(method) or "call"
    words = [w for w in words if w]
    return "_".join([verb, *words]) if words else f"{verb}_root"


def is_builtin_tool_name(name: str) -> bool:
    try:
        from app.tools.builtin_suites import resolve_builtin_tool
    except Exception:  # pragma: no cover - defensive layering fallback
        return False
    try:
        return resolve_builtin_tool(name) is not None
    except Exception:  # pragma: no cover
        return False


def assign_tool_names(operations: list[OperationEntry], prefix: str) -> None:
    """Give every supported operation a unique, provider-safe tool name.

    Linear in the operations: names are checked against a set, and a name
    already taken resumes numbering where its previous duplicate stopped.
    """
    used: set[str] = set()
    next_counter: dict[str, int] = {}
    for op in operations:
        if not op.supported or op.tool is None:
            continue
        base = _base_tool_name(op.operation_id, op.method.lower(), op.path)
        name = f"{prefix}_{base}" if prefix else base
        name = re.sub(r"_+", "_", name).strip("_") or "operation"
        if name[0].isdigit():
            name = f"op_{name}"
        name = name[:MAX_TOOL_NAME_CHARS].rstrip("_")
        if is_builtin_tool_name(name):
            name = f"{name[: MAX_TOOL_NAME_CHARS - 4]}_api"
        candidate = name
        counter = next_counter.get(name, 2)
        while candidate in used:
            suffix = f"_{counter}"
            candidate = f"{name[: MAX_TOOL_NAME_CHARS - len(suffix)].rstrip('_')}{suffix}"
            counter += 1
        next_counter[name] = counter
        used.add(candidate)
        op.tool_name = candidate
        op.tool["name"] = candidate


# ---------------------------------------------------------------------------
# Servers
# ---------------------------------------------------------------------------


def _substitute_variables(url: str, variables: Any) -> str:
    if not isinstance(variables, dict):
        return url
    for name, spec in variables.items():
        default = spec.get("default") if isinstance(spec, dict) else None
        if default is not None:
            url = url.replace(f"{{{name}}}", str(default))
    return url


def _absolute_http(url: str, base: str | None) -> str | None:
    """An absolute http(s) URL (a relative one joined to *base*), else None.

    A spec string that is no URL at all (an unclosed IPv6 bracket, a bad
    port) is None, never an exception.
    """
    try:
        parsed = urlparse(url)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            _ = parsed.port
            return url
        if base and not parsed.scheme:
            joined = urljoin(base, url)
            joined_parsed = urlparse(joined)
            if joined_parsed.scheme in ("http", "https") and joined_parsed.netloc:
                _ = joined_parsed.port
                return joined
    except ValueError:
        return None
    return None


def _servers(
    data: dict[str, Any], family: str, source_url: str | None, warnings: list[str]
) -> list[dict[str, Any]]:
    """The API's servers as absolute URLs; *source_url* is the document's base URI."""
    source = urlparse(source_url) if source_url else None
    out: list[dict[str, Any]] = []
    if family == "2.0":
        host = data.get("host")
        if not isinstance(host, str) or not host.strip():
            host = source.netloc if source and source.netloc else None
        if not host:
            warnings.append(
                "The spec declares no host, so the API base URL could not be derived — "
                "enter it when creating the integration."
            )
            return []
        base_path = data.get("basePath") if isinstance(data.get("basePath"), str) else "/"
        if not base_path.startswith("/"):
            base_path = "/" + base_path
        raw_schemes = data.get("schemes")
        schemes = [s for s in raw_schemes if s in ("https", "http")] if isinstance(raw_schemes, list) else []
        if not schemes:
            schemes = [source.scheme] if source and source.scheme in ("http", "https") else ["https"]
        for scheme in sorted(set(schemes), key=lambda s: 0 if s == "https" else 1):
            out.append({"url": f"{scheme}://{host.strip()}{base_path}".rstrip("/"), "description": None, "name": None})
        return out

    raw = data.get("servers")
    entries = raw if isinstance(raw, list) and raw else [{"url": "/"}]
    relative: list[str] = []
    for entry in entries[:_MAX_SERVERS]:
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            continue
        url = _substitute_variables(entry["url"].strip(), entry.get("variables"))
        absolute = _absolute_http(url, source_url)
        if absolute is None or "{" in absolute:
            relative.append(url)
            continue
        absolute = absolute.rstrip("/")
        if any(s["url"] == absolute for s in out):
            continue
        out.append(
            {
                "url": absolute,
                "description": clean_text(entry.get("description"), 200) or None,
                # OpenAPI 3.2: a short name for the server.
                "name": clean_text(entry.get("name"), 100) or None,
            }
        )
    if not out and relative:
        warnings.append(
            f"The spec's server URL '{relative[0]}' is relative or templated — enter the "
            "API's full base URL when creating the integration."
        )
    return out


# ---------------------------------------------------------------------------
# Operation conversion
# ---------------------------------------------------------------------------


class _ArgNamer:
    """Hands out unique, provider-safe argument names within one tool."""

    def __init__(self) -> None:
        self.used: set[str] = set(_RESERVED_ARGUMENTS)

    def claim(self, preferred: str, *alternatives: str) -> str:
        for candidate in (preferred, *alternatives):
            name = safe_argument_name(candidate)
            if name and name not in self.used:
                self.used.add(name)
                return name
        base = safe_argument_name(preferred) or "arg"
        counter = 2
        while f"{base[:58]}_{counter}" in self.used:
            counter += 1
        name = f"{base[:58]}_{counter}"
        self.used.add(name)
        return name


def _is_json_media(media_type: str) -> bool:
    base = media_type.split(";", 1)[0].strip().lower()
    return base == "application/json" or (base.startswith("application/") and base.endswith("+json")) or base.endswith("/json")


def _declares_file(schema: Any) -> bool:
    """Whether a raw media schema describes file content (not structured text)."""
    if not isinstance(schema, dict):
        return False
    if schema.get("format") in ("binary", "byte") or schema.get("type") == "file":
        return True
    return isinstance(schema.get("contentMediaType"), str) or isinstance(
        schema.get("contentEncoding"), str
    )


def _pick_request_media(content: dict[str, Any]) -> tuple[str, str, Any] | None:
    """``(media type, body encoding, schema)`` preferred for a request body.

    A media type that is not JSON, a form or text (``application/octet-stream``,
    an image, a PDF, XML…) — or a ``text/*`` one whose schema declares file
    content — is sent as a file (encoding ``binary``). A sequential media type
    with an OpenAPI 3.2 ``itemSchema`` is sent one item per record; its schema
    is returned as the array of items.
    """
    items = [(str(k), v if isinstance(v, dict) else {}) for k, v in content.items()]

    def base(media_type: str) -> str:
        return media_type.split(";", 1)[0].strip().lower()

    for media_type, media in items:
        if base(media_type) == "application/json":
            return media_type, "json", media.get("schema")
    for media_type, media in items:
        if _is_json_media(media_type):
            return media_type, "json", media.get("schema")
    for media_type, media in items:
        encoding = _SEQUENTIAL_MEDIA.get(base(media_type))
        if encoding and "itemSchema" in media:
            return media_type, encoding, {"type": "array", "items": media.get("itemSchema")}
    for media_type, media in items:
        if base(media_type) == "application/x-www-form-urlencoded":
            return media_type, "form", media.get("schema")
    for media_type, media in items:
        if base(media_type) == "multipart/form-data":
            return media_type, "multipart", media.get("schema")
    for media_type, media in items:
        if base(media_type).startswith("text/") and not _declares_file(media.get("schema")):
            return media_type, "text", media.get("schema")
    for media_type, media in items:
        if "*" not in base(media_type) and "/" in base(media_type):
            return media_type, "binary", media.get("schema")
    for media_type, media in items:
        if base(media_type) in ("*/*", "application/*"):
            if _declares_file(media.get("schema")):
                return media_type, "binary", media.get("schema")
            return "application/json", "json", media.get("schema")
    return None


def _preferred_accept(op: dict[str, Any], data: dict[str, Any], family: str) -> str | None:
    """The JSON media type a successful response comes in, if any."""
    if family == "2.0":
        produces = op.get("produces") if isinstance(op.get("produces"), list) else data.get("produces")
        if isinstance(produces, list):
            for media_type in produces:
                if isinstance(media_type, str) and _is_json_media(media_type):
                    return media_type.split(";", 1)[0].strip()
        return None
    responses = op.get("responses")
    if not isinstance(responses, dict):
        return None
    for code, response in responses.items():
        if not (str(code).startswith("2") or str(code) == "default"):
            continue
        content = response.get("content") if isinstance(response, dict) else None
        if isinstance(content, dict):
            for media_type in content:
                if _is_json_media(str(media_type)):
                    return str(media_type).split(";", 1)[0].strip()
    return None


class _ToolBuilder:
    """Builds one operation's tool at a given schema depth."""

    def __init__(self, ctx: _Context, entry: OperationEntry, op: dict[str, Any],
                 params: list[dict[str, Any]], server_url: str | None, max_depth: int) -> None:
        self.ctx = ctx
        self.entry = entry
        self.op = op
        self.params = params
        self.server_url = server_url
        self.max_depth = max_depth
        self.namer = _ArgNamer()
        self.properties: dict[str, Any] = {}
        self.required: list[str] = []
        self.execution: dict[str, Any] = {"method": entry.method, "path": entry.path}
        self.warnings: list[str] = []
        self.accepts_files = False

    def convert(self, schema: Any) -> dict[str, Any] | None:
        return self.ctx.converter.convert(
            schema, max_depth=self.max_depth, max_nodes=_MAX_SCHEMA_NODES
        )

    def build(self) -> dict[str, Any]:
        for param in sorted(self.params, key=lambda p: _LOCATION_ORDER.get(p["in"], 9)):
            if param["in"] == "querystring":
                self._add_querystring(param)
            elif param["in"] in _LOCATION_ORDER:
                self._add_parameter(param)
        method = self.entry.method
        if self.ctx.family == "2.0":
            self._add_swagger2_body(method)
        else:
            self._add_openapi3_body(method)

        if self.server_url:
            self.execution["server_url"] = self.server_url
        accept = _preferred_accept(self.op, self.ctx.data, self.ctx.family)
        if accept:
            self.execution["accept"] = accept

        errors = validate_execution_spec(self.execution, tool_index=0)
        if errors:
            raise _UnsupportedOperationError("the generated request definition is invalid: " + "; ".join(errors))

        input_schema: dict[str, Any] = {"type": "object", "properties": self.properties}
        if self.required:
            input_schema["required"] = self.required
        return {
            "name": None,  # assigned once every operation is known
            "description": self._description(),
            "tool_type": self.entry.tool_type,
            "input_schema": input_schema,
            "execution": self.execution,
        }

    # -- parameters --------------------------------------------------------

    def _parameter_schema(self, param: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, bool | None]:
        location = param["in"]
        if self.ctx.family == "2.0":
            raw = {k: v for k, v in param.items() if k in SWAGGER2_PARAMETER_SCHEMA_KEYS}
            schema = self.convert(raw)
            style: str | None = None
            explode: bool | None = None
            if schema is not None and schema.get("type") == "array":
                collection = str(param.get("collectionFormat") or "csv")
                if location == "query":
                    style, explode = _V2_COLLECTION_STYLES.get(collection, ("form", False))
                elif collection not in ("csv", "multi"):
                    self.warnings.append(
                        f"Parameter '{param['name']}' uses collectionFormat '{collection}'; "
                        "values are sent comma-separated."
                    )
            return schema, style, explode
        if "schema" in param:
            style = param.get("style") if isinstance(param.get("style"), str) else None
            explode = param.get("explode") if isinstance(param.get("explode"), bool) else None
            return self.convert(param.get("schema")), style, explode
        content = param.get("content")
        if isinstance(content, dict) and content:
            media = self._media(next(iter(content.values())))
            raw_schema = media.get("schema") if isinstance(media, dict) else None
            return self.convert(raw_schema), "json", None
        return {}, None, None

    def _media(self, raw: Any) -> dict[str, Any]:
        """A Media Type Object, following an OpenAPI 3.2 ``components/mediaTypes`` reference."""
        try:
            media = self.ctx.resolver.deref(raw)
        except UnresolvedRefError as exc:
            self.ctx.resolver.record(exc)
            raise _UnsupportedOperationError("a media type definition cannot be resolved") from None
        return media if isinstance(media, dict) else {}

    def _add_querystring(self, param: dict[str, Any]) -> None:
        """An OpenAPI 3.2 ``in: querystring`` parameter: the whole query string."""
        name = param["name"]
        required = param.get("required") is True
        content = param.get("content")
        if not isinstance(content, dict) or len(content) != 1:
            if required:
                raise _UnsupportedOperationError(f"the query string parameter '{name}' declares no single media type")
            self.warnings.append(f"The query string parameter '{name}' declares no single media type and was left out.")
            return
        media_type, raw_media = next(iter(content.items()))
        schema = self.convert(self._media(raw_media).get("schema"))
        if schema is None or is_binary_schema(schema):
            if required:
                raise _UnsupportedOperationError(f"the query string parameter '{name}' accepts no value a tool can send")
            return
        form = str(media_type).split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded"
        schema = dict(schema)
        description = clean_text(param.get("description"), _PARAM_DESCRIPTION_CHARS)
        note = "Sent as the whole query string" + (", one field per key." if form else f", as {media_type}.")
        schema["description"] = f"{description.rstrip('.')}. {note}" if description else note
        arg = self.namer.claim(name, "query_string")
        self.properties[arg] = schema
        field_spec: dict[str, Any] = {"from": arg, "media_type": str(media_type)}
        if required:
            field_spec["required"] = True
            self.required.append(arg)
        self.execution["querystring"] = field_spec

    def _add_parameter(self, param: dict[str, Any]) -> None:
        location = param["in"]
        name = param["name"]
        required = location == "path" or param.get("required") is True
        if location == "header":
            if name.lower() in _IGNORED_HEADER_PARAMS:
                return
            if not _HEADER_NAME_RE.match(name) or name.lower() in _UNMAPPABLE_HEADERS:
                if required:
                    raise _UnsupportedOperationError(f"the required header '{name}' cannot be set by a tool")
                self.warnings.append(f"Header parameter '{name}' is not supported and was left out.")
                return
        if location == "cookie" and not _HEADER_NAME_RE.match(name):
            if required:
                raise _UnsupportedOperationError(f"the required cookie '{name}' has an invalid name")
            return

        schema, style, explode = self._parameter_schema(param)
        if schema is None:
            if required:
                raise _UnsupportedOperationError(f"the parameter '{name}' accepts no value")
            return
        if is_binary_schema(schema):
            if required:
                raise _UnsupportedOperationError(f"the parameter '{name}' carries file content")
            return

        effective_style = style or _DEFAULT_STYLES[location]
        if effective_style != "json" and effective_style not in _ALLOWED_STYLES[location]:
            if required:
                raise _UnsupportedOperationError(
                    f"the {location} parameter '{name}' uses the unsupported '{effective_style}' style"
                )
            self.warnings.append(
                f"Parameter '{name}' uses the unsupported '{effective_style}' style and was left out."
            )
            return

        schema = dict(schema)
        description = clean_text(param.get("description"), _PARAM_DESCRIPTION_CHARS)
        if param.get("deprecated") is True:
            description = f"Deprecated. {description}".strip()
        if description:
            schema["description"] = description

        arg = self.namer.claim(name, f"{location}_{name}")
        self.properties[arg] = schema
        field_spec: dict[str, Any] = {"from": arg}
        if required:
            field_spec["required"] = True
            self.required.append(arg)
        if style and style != _DEFAULT_STYLES[location]:
            field_spec["style"] = style
        elif location == "query" and _may_be_object(schema):
            # The executor lets an object's keys name query parameters only
            # under a declared style (an OpenAPI exploded ``form`` object).
            field_spec["style"] = effective_style
        default_explode = effective_style == "form"
        if explode is not None and explode != default_explode:
            field_spec["explode"] = explode

        if location == "path":
            self.execution.setdefault("path_params", []).append(name)
            self.execution.setdefault("path_mapping", {})[name] = field_spec
        else:
            self.execution.setdefault(f"{location}_mapping", {})[name] = field_spec

    # -- request body ------------------------------------------------------

    def _add_openapi3_body(self, method: str) -> None:
        raw = self.op.get("requestBody")
        if raw is None:
            return
        try:
            body = self.ctx.resolver.deref(raw)
        except UnresolvedRefError as exc:
            self.ctx.resolver.record(exc)
            raise _UnsupportedOperationError("the request body definition cannot be resolved") from None
        if not isinstance(body, dict):
            return
        required = body.get("required") is True
        raw_content = body.get("content") if isinstance(body.get("content"), dict) else {}
        content = {str(k): self._media(v) for k, v in raw_content.items()}
        if method in _BODYLESS_METHODS:
            if required:
                raise _UnsupportedOperationError(f"{method} requests with a body are not supported")
            self.warnings.append(f"The optional {method} request body is not sent.")
            return
        picked = _pick_request_media(content) if content else None
        if picked is None:
            if not content:
                return
            media_list = ", ".join(sorted(str(k) for k in content))
            if required:
                raise _UnsupportedOperationError(f"request bodies of type {media_list} are not supported")
            self.warnings.append(f"The optional {media_list} request body is not sent.")
            return
        media_type, encoding, schema = picked
        media = content.get(media_type) or {}
        self._add_body(
            schema,
            media_type=media_type,
            encoding=encoding,
            required=required,
            description=clean_text(body.get("description"), _PARAM_DESCRIPTION_CHARS),
            part_encodings=media.get("encoding") if encoding == "multipart" else None,
        )

    def _add_swagger2_body(self, method: str) -> None:
        body_params = [p for p in self.params if p["in"] == "body"]
        form_params = [p for p in self.params if p["in"] == "formData"]
        if not body_params and not form_params:
            return
        if method in _BODYLESS_METHODS:
            if any(p.get("required") is True for p in body_params + form_params):
                raise _UnsupportedOperationError(f"{method} requests with a body are not supported")
            return
        consumes = self.op.get("consumes") if isinstance(self.op.get("consumes"), list) else self.ctx.data.get("consumes")
        consumes = [c for c in consumes if isinstance(c, str)] if isinstance(consumes, list) else []
        if body_params:
            param = body_params[0]
            json_types = [c for c in consumes if _is_json_media(c)]
            required = param.get("required") is True
            if consumes and not json_types:
                file_types = [
                    c
                    for c in consumes
                    if not c.startswith(("multipart/form-data", "application/x-www-form-urlencoded"))
                ]
                if not file_types:
                    listed = ", ".join(consumes)
                    if required:
                        raise _UnsupportedOperationError(f"request bodies of type {listed} are not supported")
                    self.warnings.append(f"The optional {listed} request body is not sent.")
                    return
                concrete = [c for c in file_types if "*" not in c]
                self._add_body(
                    param.get("schema"),
                    media_type=concrete[0] if concrete else file_types[0],
                    encoding="binary",
                    required=required,
                    description=clean_text(param.get("description"), _PARAM_DESCRIPTION_CHARS),
                )
                return
            self._add_body(
                param.get("schema"),
                media_type=json_types[0] if json_types else "application/json",
                encoding="json",
                required=required,
                description=clean_text(param.get("description"), _PARAM_DESCRIPTION_CHARS),
            )
            return
        multipart = any(c.startswith("multipart/form-data") for c in consumes) or any(
            p.get("type") == "file" for p in form_params
        )
        properties: dict[str, Any] = {}
        required_fields: list[str] = []
        for param in form_params:
            prop = {k: v for k, v in param.items() if k in SWAGGER2_PARAMETER_SCHEMA_KEYS}
            if isinstance(param.get("description"), str):
                prop["description"] = param["description"]
            properties[param["name"]] = prop
            if param.get("required") is True:
                required_fields.append(param["name"])
        self._add_body(
            {"type": "object", "properties": properties, "required": required_fields},
            media_type="multipart/form-data" if multipart else "application/x-www-form-urlencoded",
            encoding="multipart" if multipart else "form",
            required=bool(required_fields),
            description="",
        )

    def _add_body(
        self,
        raw_schema: Any,
        *,
        media_type: str,
        encoding: str,
        required: bool,
        description: str,
        part_encodings: Any = None,
    ) -> None:
        if encoding == "binary":
            self._add_file_body(media_type, required=required, description=description)
            return
        if encoding in ("jsonl", "json-seq"):
            self._add_sequence_body(raw_schema, media_type, encoding, required=required, description=description)
            return
        converted = self.convert(raw_schema) if raw_schema is not None else {}
        if converted is None:
            if required:
                raise _UnsupportedOperationError("the request body accepts no value")
            return
        if encoding == "text":
            converted = {"type": "string"}
        properties = converted.get("properties") if isinstance(converted.get("properties"), dict) else {}
        flatten = bool(properties) and (
            encoding in ("form", "multipart")
            or (
                converted.get("type") == "object"
                and "anyOf" not in converted
                and len(properties) <= MAX_FLATTENED_BODY_FIELDS
            )
        )

        self.execution["body_encoding"] = encoding
        if encoding != "multipart":
            self.execution["content_type"] = media_type
        if flatten:
            self._flatten_body(
                converted,
                properties,
                required=required,
                encoding=encoding,
                part_encodings=part_encodings if isinstance(part_encodings, dict) else {},
            )
            return
        if encoding in ("form", "multipart") and converted.get("type") not in (None, "object"):
            raise _UnsupportedOperationError("the form body is not an object of fields")
        schema = dict(converted)
        if description:
            schema["description"] = description
        arg = self.namer.claim("body", "request_body", "payload")
        self.properties[arg] = schema
        self.execution["body_from"] = arg
        if required:
            self.execution["body_required"] = True
            self.required.append(arg)

    def _add_sequence_body(
        self, raw_schema: Any, media_type: str, encoding: str, *, required: bool, description: str
    ) -> None:
        converted = self.convert(raw_schema)
        if converted is None:
            if required:
                raise _UnsupportedOperationError("the request body accepts no value")
            return
        schema = dict(converted)
        note = f"Sent as {media_type}, one record per item."
        schema["description"] = f"{description.rstrip('.')}. {note}" if description else note
        arg = self.namer.claim("items", "body", "records")
        self.properties[arg] = schema
        self.execution["body_encoding"] = encoding
        self.execution["content_type"] = media_type
        self.execution["body_from"] = arg
        if required:
            self.execution["body_required"] = True
            self.required.append(arg)

    def _add_file_body(self, media_type: str, *, required: bool, description: str) -> None:
        arg = self.namer.claim("file", "body", "upload")
        self.properties[arg] = file_argument_schema(
            description or f"The file sent as the {media_type} request body."
        )
        self.execution["body_encoding"] = "binary"
        self.execution["body_from"] = arg
        if "*" not in media_type:
            self.execution["content_type"] = media_type
        if required:
            self.execution["body_required"] = True
            self.required.append(arg)
        self.accepts_files = True

    def _flatten_body(
        self,
        converted: dict[str, Any],
        properties: dict[str, Any],
        *,
        required: bool,
        encoding: str,
        part_encodings: dict[str, Any],
    ) -> None:
        required_fields = set(converted.get("required") or []) if required else set()
        mapping: dict[str, Any] = {}
        for key, prop_schema in properties.items():
            if is_binary_schema(prop_schema):
                if encoding != "multipart":
                    if key in required_fields:
                        raise _UnsupportedOperationError(
                            f"the form field '{key}' carries file content, which a form-urlencoded body cannot send"
                        )
                    self.warnings.append(f"The file field '{key}' is not sent.")
                    continue
                self._add_file_field(key, prop_schema, key in required_fields, part_encodings, mapping)
                continue
            arg = self.namer.claim(key, f"body_{key}")
            self.properties[arg] = prop_schema
            field_spec: dict[str, Any] = {"from": arg}
            if key in required_fields:
                field_spec["required"] = True
                self.required.append(arg)
            mapping[key] = field_spec
        if not mapping:
            if required:
                raise _UnsupportedOperationError("the request body has no field a tool can send")
            self.execution.pop("body_encoding", None)
            self.execution.pop("content_type", None)
            return
        self.execution["body_mapping"] = mapping

    def _add_file_field(
        self,
        key: str,
        prop_schema: dict[str, Any],
        required: bool,
        part_encodings: dict[str, Any],
        mapping: dict[str, Any],
    ) -> None:
        arg = self.namer.claim(key, f"body_{key}")
        self.properties[arg] = file_argument_schema(
            prop_schema.get("description"), multiple=prop_schema.get("type") == "array"
        )
        field_spec: dict[str, Any] = {"from": arg, "file": True}
        part = part_encodings.get(key) if isinstance(part_encodings.get(key), dict) else {}
        part_type = part.get("contentType")
        if isinstance(part_type, str) and "," not in part_type and "*" not in part_type:
            field_spec["content_type"] = part_type.strip()
        if required:
            field_spec["required"] = True
            self.required.append(arg)
        mapping[key] = field_spec
        self.accepts_files = True

    # -- description -------------------------------------------------------

    def _description(self) -> str:
        summary = clean_text(self.op.get("summary"), 300)
        details = clean_text(self.op.get("description"), 900)
        text = summary
        if details and details != summary:
            text = f"{summary.rstrip('.')}. {details}" if summary else details
        if self.entry.deprecated:
            text = f"Deprecated. {text}".strip()
        head = f"{self.entry.method} {self.entry.path}"
        return clean_text(f"{head} — {text}" if text else head, MAX_TOOL_DESCRIPTION_CHARS)


def _top_level_security_declared(data: dict[str, Any], family: str) -> bool:
    """Whether the document's top-level ``security`` list is a real requirement.

    A document-wide ``security: []`` beside declared security schemes is not
    "every operation is public": Apache Airflow's stable API ships exactly that
    while every call needs its Basic credential (the server enforces the auth
    backend it is configured with). Read as undeclared, so the chosen credential
    is sent; an operation's own ``security: []`` still marks it public.
    """
    if "security" not in data:
        return False
    return not (data.get("security") == [] and security_scheme_definitions(data, family))


@dataclass
class _Context:
    data: dict[str, Any]
    family: str
    resolver: RefResolver
    converter: SchemaConverter
    default_server_url: str | None
    #: The document's base URI (``$self``, else where it was fetched from).
    source_url: str | None
    top_level_security: Any
    has_top_level_security: bool
    #: OpenAPI 3.2 or later (QUERY, ``additionalOperations``).
    v32: bool = False


def _resolved_parameters(ctx: _Context, raw: Any, warnings: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        try:
            param = ctx.resolver.deref(item)
        except UnresolvedRefError as exc:
            ctx.resolver.record(exc)
            raise _UnsupportedOperationError("a parameter definition cannot be resolved") from None
        if not isinstance(param, dict):
            continue
        name, location = param.get("name"), param.get("in")
        if not isinstance(name, str) or not name or not isinstance(location, str):
            warnings.append("A parameter without a name or location was ignored.")
            continue
        out.append(param)
    return out


def _merge_parameters(
    ctx: _Context, path_level: Any, op_level: Any, path: str, warnings: list[str]
) -> list[dict[str, Any]]:
    """Path-item parameters overridden by the operation's, by ``(in, name)``."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for param in _resolved_parameters(ctx, path_level, warnings):
        merged[(param["in"], param["name"])] = param
    for param in _resolved_parameters(ctx, op_level, warnings):
        merged[(param["in"], param["name"])] = param
    params = list(merged.values())

    placeholders = _PLACEHOLDER_RE.findall(path)
    declared = {p["name"] for p in params if p["in"] == "path"}
    for placeholder in placeholders:
        if placeholder not in declared:
            params.append({"name": placeholder, "in": "path", "required": True, "type": "string", "schema": {"type": "string"}})
            warnings.append(f"Path parameter '{placeholder}' is not declared; it is treated as a string.")
    extra = [p for p in params if p["in"] == "path" and p["name"] not in placeholders]
    if extra:
        warnings.append(
            "Declared path parameters missing from the path were ignored: "
            + ", ".join(p["name"] for p in extra)
        )
    return [p for p in params if not (p["in"] == "path" and p["name"] not in placeholders)]


def _operation_server(ctx: _Context, op: dict[str, Any], path_servers: Any) -> str | None:
    """An operation's own server (3.x ``servers`` on the operation or path item)."""
    if ctx.family == "2.0":
        return None
    for servers in (op.get("servers"), path_servers):
        if not isinstance(servers, list) or not servers:
            continue
        first = servers[0]
        if not isinstance(first, dict) or not isinstance(first.get("url"), str):
            continue
        url = _substitute_variables(first["url"].strip(), first.get("variables"))
        absolute = _absolute_http(url, ctx.source_url or ctx.default_server_url)
        if absolute and "{" not in absolute:
            absolute = absolute.rstrip("/")
            return None if absolute == ctx.default_server_url else absolute
    return None


def _build_operation(
    ctx: _Context,
    path: str,
    method: str,
    op: dict[str, Any],
    path_item: dict[str, Any],
) -> OperationEntry:
    """One operation; *method* is sent as it is (``GET``, ``QUERY``, a custom ``COPY``)."""
    operation_id = op.get("operationId") if isinstance(op.get("operationId"), str) else None
    tags = [str(t) for t in op.get("tags") or [] if isinstance(t, str | int)] if isinstance(op.get("tags"), list) else []
    security = op.get(
        "security", ctx.top_level_security if ctx.has_top_level_security else None
    )
    sends_credential, schemes = requirement_schemes(security)
    entry = OperationEntry(
        key=f"{method} {path}",
        method=method,
        path=path,
        operation_id=operation_id.strip() if operation_id else None,
        summary=clean_text(op.get("summary") or op.get("description"), _SUMMARY_CHARS),
        tags=tags,
        deprecated=op.get("deprecated") is True,
        tool_type=method_access(method),
        sends_credential=sends_credential,
        security_schemes=schemes,
    )
    never = _NEVER_METHODS.get(method.upper())
    if never:
        entry.supported = False
        entry.skip_reason = f"HTTP {method.upper()} operations are never tools: {never}."
        return entry
    if not _METHOD_TOKEN_RE.match(method):
        entry.supported = False
        entry.skip_reason = f"'{method[:40]}' is not a valid HTTP method."
        return entry
    try:
        params = _merge_parameters(ctx, path_item.get("parameters"), op.get("parameters"), path, entry.warnings)
        server_url = _operation_server(ctx, op, path_item.get("servers"))
        if server_url and entry.sends_credential and not same_origin(server_url, ctx.default_server_url):
            entry.warnings.append(
                f"Runs on {server_url}, another server than the API's base URL, so it is "
                "called without the integration's credentials."
            )
        tool: dict[str, Any] | None = None
        builder_warnings: list[str] = []
        for depth in _DEPTH_STEPS:
            builder = _ToolBuilder(ctx, entry, op, params, server_url, depth)
            tool = builder.build()
            builder_warnings = builder.warnings
            entry.accepts_files = builder.accepts_files
            if len(json.dumps(tool["input_schema"], ensure_ascii=False)) <= MAX_TOOL_SCHEMA_CHARS:
                break
        else:
            if tool is not None and len(json.dumps(tool["input_schema"], ensure_ascii=False)) > 2 * MAX_TOOL_SCHEMA_CHARS:
                raise _UnsupportedOperationError("the operation has too many parameters to describe as one tool")
        entry.warnings.extend(builder_warnings)
        if tool is not None:
            # Read by the runtime, which loads the tools alone: whether this
            # call carries the credential, and which operation it is.
            tool["openapi"] = {
                "operation": entry.key,
                "sends_credential": entry.sends_credential,
            }
            if not entry.sends_credential:
                # Where the public reading comes from: a tool stored without it
                # predates #10187 and may be public only through a document-wide
                # ``security: []`` (see ``service.effective_tools``).
                tool["openapi"]["public_by"] = "operation" if "security" in op else "document"
        entry.tool = tool
    except _UnsupportedOperationError as exc:
        reason = str(exc) or "the operation cannot be described as a tool"
        entry.supported = False
        entry.skip_reason = f"{reason[:1].upper()}{reason[1:]}."
    return entry


def _collect_operations(ctx: _Context, warnings: list[str]) -> list[OperationEntry]:
    paths = ctx.data.get("paths")
    if not isinstance(paths, dict):
        warnings.append("The spec declares no paths, so it has no operation to import.")
        return []
    entries: list[OperationEntry] = []
    unresolved_paths: list[str] = []
    capped = 0
    for path, raw_item in paths.items():
        if not isinstance(path, str) or not path.startswith("/"):
            continue
        try:
            path_item = ctx.resolver.deref(raw_item)
        except UnresolvedRefError as exc:
            ctx.resolver.record(exc)
            unresolved_paths.append(path)
            continue
        if not isinstance(path_item, dict):
            continue
        fixed = _METHODS_V2 if ctx.family == "2.0" else _METHODS_V32 if ctx.v32 else _METHODS_V3
        operations: list[tuple[str, dict[str, Any]]] = [
            (m.upper(), path_item[m]) for m in fixed if isinstance(path_item.get(m), dict)
        ]
        extra = path_item.get("additionalOperations") if ctx.v32 else None
        if isinstance(extra, dict):
            # Keys are the methods exactly as sent; the fixed ones may not
            # be repeated here.
            taken = {m for m, _ in operations}
            for name, op in extra.items():
                if not isinstance(op, dict):
                    continue
                if str(name).upper() in taken or str(name).lower() in _METHODS_V32:
                    warnings.append(
                        f"{path}: additionalOperations repeats the {str(name).upper()} method; it was ignored."
                    )
                    continue
                operations.append((str(name), op))
        for method, op in operations:
            if len(entries) >= MAX_OPERATIONS:
                capped += 1
                continue
            entries.append(_build_operation(ctx, path, method, op, path_item))
    if unresolved_paths:
        warnings.append(
            f"{len(unresolved_paths)} path(s) reference definitions that could not be "
            f"resolved and were skipped: {', '.join(unresolved_paths[:_MAX_ISSUES_LISTED])}"
            + ("…" if len(unresolved_paths) > _MAX_ISSUES_LISTED else "")
        )
    if capped:
        warnings.append(
            f"The spec has more than {MAX_OPERATIONS} operations; the last {capped} were not imported."
        )
    return entries


def _tags(data: dict[str, Any], operations: list[OperationEntry]) -> list[dict[str, Any]]:
    """The document's tags, then any tag only operations name.

    OpenAPI 3.2 tags may carry a short display ``summary`` and a ``parent``
    tag (nesting).
    """
    tags: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw = data.get("tags")
    for tag in raw if isinstance(raw, list) else []:
        if isinstance(tag, dict) and isinstance(tag.get("name"), str) and tag["name"] not in seen:
            seen.add(tag["name"])
            parent = tag.get("parent")
            tags.append(
                {
                    "name": tag["name"],
                    "description": clean_text(tag.get("description"), 200) or None,
                    "summary": clean_text(tag.get("summary"), 100) or None,
                    "parent": parent if isinstance(parent, str) and parent else None,
                }
            )
    for op in operations:
        for name in op.tags:
            if name not in seen:
                seen.add(name)
                tags.append({"name": name, "description": None, "summary": None, "parent": None})
    return tags


def _is_v32(document: SpecDocument) -> bool:
    """Whether the document is OpenAPI 3.2 or a later 3.x."""
    if document.version.family == "2.0":
        return False
    parts = document.version.raw.split("-", 1)[0].split(".")
    try:
        return int(parts[0]) == 3 and int(parts[1]) >= 2
    except (IndexError, ValueError):
        return False


def auth_url_base(document: SpecDocument, fallback: str | None = None) -> str | None:
    """What relative OAuth and OpenID Connect URLs resolve against.

    The first server (they usually sit beside the API), else the document's
    own base URI, else *fallback*: the server URL an existing integration
    already has, for a replacement uploaded as a file with no ``servers``.
    """
    base = document_base_uri(document)
    servers = _servers(document.data, document.version.family, base, [])
    return servers[0]["url"] if servers else (base or fallback)


def analyze_spec(
    document: SpecDocument,
    *,
    tool_prefix: str | None = None,
    discovery: dict[str, Any] | None = None,
    server_url_fallback: str | None = None,
) -> SpecAnalysis:
    """Convert a parsed document (see the module docstring).

    *discovery* holds the fetched discovery documents of its OpenID Connect
    and OAuth2 metadata URLs (``discovery.fetch_discovery``).
    *server_url_fallback* is what relative OAuth URLs resolve against when the
    document names neither a server nor its own location (see
    :func:`auth_url_base`).
    """
    data = document.data
    family = document.version.family
    warnings: list[str] = []
    resolver = RefResolver(data)
    converter = SchemaConverter(resolver, family=family)
    base_uri = document_base_uri(document)

    info = data.get("info") if isinstance(data.get("info"), dict) else {}
    title = clean_text(info.get("title"), 200) or "API"
    servers = _servers(data, family, base_uri, warnings)
    default_server = servers[0]["url"] if servers else None

    auth_options, default_auth = extract_auth_options(
        data,
        family=family,
        resolver=resolver,
        url_base=default_server or base_uri or server_url_fallback,
        warnings=warnings,
        discovery=discovery,
    )
    prefix = normalize_tool_prefix(tool_prefix) if tool_prefix is not None else default_tool_prefix(title)

    ctx = _Context(
        data=data,
        family=family,
        resolver=resolver,
        converter=converter,
        default_server_url=default_server,
        source_url=base_uri,
        top_level_security=data.get("security"),
        has_top_level_security=_top_level_security_declared(data, family),
        v32=_is_v32(document),
    )
    operations = _collect_operations(ctx, warnings)
    assign_tool_names(operations, prefix)

    skipped = [op for op in operations if not op.supported]
    if skipped:
        warnings.append(
            f"{len(skipped)} of {len(operations)} operation(s) cannot become tools; "
            "their reasons are listed with each operation."
        )
    if resolver.unresolved:
        refs = list(resolver.unresolved)
        warnings.append(
            f"{len(refs)} reference(s) could not be resolved and were replaced by free-form "
            f"values: {', '.join(refs[:_MAX_ISSUES_LISTED])}" + ("…" if len(refs) > _MAX_ISSUES_LISTED else "")
        )
    return SpecAnalysis(
        document_format=document.format,
        spec_version=document.version.raw,
        spec_family=family,
        document_sha256=document.sha256,
        source_url=document.source_url,
        title=title,
        api_version=clean_text(str(info.get("version") or ""), 100),
        description=clean_text(info.get("description"), 1_000),
        servers=servers,
        default_server_url=default_server,
        auth_options=auth_options,
        default_auth_option=default_auth,
        tool_prefix=prefix,
        tags=_tags(data, operations),
        operations=operations,
        warnings=warnings,
    )
