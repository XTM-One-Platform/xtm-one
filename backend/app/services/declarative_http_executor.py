# SPDX-License-Identifier: Apache-2.0

"""Declarative HTTP tool executor.

One runtime for every tool whose behaviour is DATA rather than code:

* **custom integration bundles** — an admin imports ``tools[].execution``
  specs (``app.services.integration_bundle_manager``);
* **OpenAPI integrations** — ``app.services.openapi_import`` generates the
  very same specs from a Swagger 2.0 / OpenAPI 3.x document.

An execution spec states which HTTP call a tool makes and where each tool
argument goes: the URL path, the query string, a header, a cookie, or the
request body (JSON, form-encoded, multipart fields and files, plain text, or
raw file content, JSON Lines). No code is generated: a spec is validated once
(``validate_execution_spec``, at import time) and interpreted on every call
(``execute_declarative_tool``).

Spec shape (every key optional unless marked)::

    {
      "method": "GET",                        # required: any HTTP method but TRACE / CONNECT
      "path": "/pets/{petId}",                # required, starts with "/"
      "path_params": ["petId"],               # every {placeholder}, exactly
      "path_mapping": {"petId": {"from": "pet_id"}},
      "query_mapping": {"tags": {"from": "tags", "style": "form", "explode": true}},
      "querystring": {"from": "filter",       # the whole query string (OpenAPI 3.2)
                      "media_type": "application/x-www-form-urlencoded"},
      "header_mapping": {"X-Request-Id": {"from": "request_id"}},
      "cookie_mapping": {"locale": {"from": "locale"}},
      "body_mapping": {"name": {"from": "name", "required": true},
                       "data": {"from": "data", "file": true, "content_type": "text/csv"}},
                      # ... or "pass_through" (all arguments) / "none"
      "body_from": "body",                    # the whole body is ONE argument
      "body_required": true,
      "body_encoding": "json",                # json | form | multipart | text | binary
                                              # | jsonl | json-seq (one record per item)
      "content_type": "application/json",
      "accept": "application/json",
      "auth": {"type": "bearer"},             # bearer | header | query | cookie | basic | none
      "base_url_key": "base_url",             # extra_config key holding the base URL
      "server_url": "https://uploads.example.com/v1",   # per-operation server
      "response_path": "data.items",
      "timeout_ms": 30000
    }

A field spec (the values of the ``*_mapping`` dicts) is ``{"from": <tool
argument>, "required": <bool>}`` plus, where it applies, the OpenAPI
serialisation of the value: ``style`` (query: ``form`` / ``spaceDelimited`` /
``pipeDelimited`` / ``tabDelimited`` / ``deepObject``; path and header:
``simple``; cookie: ``form`` / ``cookie``; any location: ``json``, which sends
the value as one JSON text) and ``explode``.

In ``body_mapping`` and ``query_mapping`` an entry may instead be a constant
the bundle fixes, ``{"value": ...}`` (a scalar in a query string): a GraphQL
``query`` document, an API version. A constant is never read from the
arguments, so the model cannot replace it - a bundle whose GraphQL tools each
carry their own query exposes no other operation.

``"untrusted_response": true`` (with an optional ``max_response_bytes``,
capped) marks an answer as third-party data: it is streamed as raw bytes
within that budget, without redirects or proxy settings read from the
environment, a compressed answer is refused, an error status is answered by a
fixed sentence, and the result reaches the model inside ``wrap_untrusted``.
Every other answer is bounded too, and keeps the rest of its path (retries,
the 401/403 refresh, the destination guard, the client certificate): it is
asked for uncompressed, a compressed one is refused, and its raw bytes are
counted as they arrive, up to ``max_response_bytes`` (by default the cap), so
neither a large body nor a small compressed one can fill the worker's memory.

GET and HEAD never carry a body; every other method may. HEAD and OPTIONS
answer with the response status and headers (``Set-Cookie`` left out) when
there is no body to return.

The credential goes to the base URL's origin (scheme, host and port) only: a
``server_url`` elsewhere is called without it, so a spec cannot route the
integration's secret to a server the integration was not configured for.

File arguments
--------------
A multipart field marked ``"file": true``, and the whole body of a ``binary``
spec, carry a file. Its argument is ``{"file_id": ...}`` (a file available in
the conversation, read through the ``FileContext`` the tool runtime injects)
or ``{"content": ...}`` (text sent as the file), plus optional ``filename`` and
``content_type``; a bare string is a file id when one matches, text otherwise.
A multipart file field whose argument is a list sends one part per file.

Security model
--------------
The base URL comes from the integration's configuration (``extra_config``) or
from the validated spec, never from a tool argument, and every argument is
encoded into the part of the request its mapping names: a value cannot change
the host, the shape of the path (``/`` is percent-encoded, ``.`` and ``..``
segments are refused), inject a header line, or replace the credential
(credential-bearing headers are unmappable, and the credential is applied
last). Callers whose URLs are USER-supplied — OpenAPI integrations, and custom
bundle integrations, whose base URL any user of the provider may set —
also pass ``enforce_destination_guard`` so no request can reach loopback,
link-local or a cloud-metadata endpoint, each connection pinned to the address
checked.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, unquote, urlparse

import httpx

from app.core.egress import egress_client
from app.integrations.file_content import TEXT_MIME_PREFIXES
from app.integrations.http import (
    TRANSIENT_RETRY_STATUSES,
    AutoRefreshClient,
    format_network_error,
    safe_request,
)
from app.services.content_safety import wrap_untrusted
from app.services.web_fetch import guarded_client_options, validate_outbound_url_async

logger = logging.getLogger(__name__)

#: Returns the credential to use for this call (e.g. a cached OAuth
#: client-credentials access token). ``None`` means "no credential".
TokenProvider = Callable[[], Awaitable[str | None]]
#: Re-mints a bearer token after a 401/403 (``AutoRefreshClient`` contract).
RefreshFn = Callable[[], Awaitable[str | None]]

#: Token of credential-less integrations (``auth_type == "none"``). A tier
#: marker, never forwarded as a credential whatever the spec's auth type.
NO_AUTH_TOKEN = "__no_auth__"  # noqa: S105 - tier marker, not a secret

_AUTH_TYPES = {"bearer", "header", "query", "cookie", "basic", "none"}
#: Spelled in capitals on the wire; any other method is sent as declared.
_STANDARD_METHODS = frozenset(
    {"GET", "PUT", "POST", "DELETE", "PATCH", "HEAD", "OPTIONS", "QUERY", "TRACE", "CONNECT"}
)
#: Never sent: TRACE echoes the request (credentials included) back, CONNECT
#: opens a tunnel.
_REFUSED_METHODS = frozenset({"TRACE", "CONNECT"})
_BODYLESS_METHODS = frozenset({"GET", "HEAD"})
#: Only these are retried on a transient 502/503/504: a mutation retried
#: behind a failing gateway could be applied twice (see ``safe_request``).
_RETRYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "QUERY"})
#: Methods that answer with headers: returned when the body is empty.
_HEADER_METHODS = frozenset({"HEAD", "OPTIONS"})
_MAX_RESPONSE_HEADERS = 50
_MAX_RESPONSE_HEADER_CHARS = 500
#: Filled in when comparing origins (``https://a`` is ``https://a:443``).
_DEFAULT_PORTS = {"http": 80, "https": 443}
_BODY_ENCODINGS = frozenset({"json", "form", "multipart", "text", "binary", "jsonl", "json-seq"})
#: Largest file one argument may upload.
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
_MAX_UPLOAD_FILENAME_CHARS = 255
#: Python's own extension table, not the host's (``mimetypes.init`` reads the
#: Windows registry or /etc/mime.types): a file is sent as the same type
#: wherever the platform runs.
_MEDIA_TYPES = mimetypes.MimeTypes()
_MEDIA_TYPE_RE = re.compile(r"^[A-Za-z0-9][\w.+-]*/[\w.+-]+(?:\s*;[ -~]*)?$")
_QUERY_STYLES = frozenset(
    {"form", "spaceDelimited", "pipeDelimited", "tabDelimited", "deepObject", "json"}
)
_PATH_STYLES = frozenset({"simple", "json"})
#: Percent-decoding rounds a path value is judged through: each proxy or
#: framework that decodes the path once is one round. A value that still
#: decodes to something new after the last round is refused.
_PATH_DECODE_ROUNDS = 8
#: Separators a stack may split a decoded path on (``\`` on IIS and others).
_PATH_SEPARATORS = re.compile(r"[/\\]")
_HEADER_STYLES = frozenset({"simple", "json"})
_COOKIE_STYLES = frozenset({"form", "cookie", "json"})
_DELIMITERS = {
    "form": ",",
    "simple": ",",
    "spaceDelimited": " ",
    "pipeDelimited": "|",
    "tabDelimited": "\t",
}

#: Request headers a mapping may never set: they belong to the transport
#: (framing, host) or to the credential. A spec mapping an argument onto one
#: is rejected at validation time, so an LLM-supplied value can neither
#: replace the integration's credential nor desynchronise the request.
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "te",
        "trailer",
        "upgrade",
        "expect",
    }
)

#: The reserved headers a credential may ride in (a session cookie is one;
#: mapped cookies are added to it, never written over it).
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie"})


def credential_header_allowed(name: Any) -> bool:
    """Whether a credential may be sent in header *name*.

    A valid token, and not a header the transport owns (framing, host,
    content type, proxy credentials): the credential must not be able to
    desynchronise or retarget the request.
    """
    return (
        isinstance(name, str)
        and bool(_HEADER_NAME_RE.fullmatch(name))
        and (name.lower() in _CREDENTIAL_HEADERS or name.lower() not in _RESERVED_HEADERS)
    )


_DEFAULT_TIMEOUT_MS = 30_000
# ``untrusted_response``: the answer is read as a stream of raw bytes up to
# ``max_response_bytes`` (default below, never above the cap).
_DEFAULT_UNTRUSTED_MAX_BYTES = 4 * 1024 * 1024
_MAX_UNTRUSTED_MAX_BYTES = 20 * 1024 * 1024
# Every other answer is read within ``max_response_bytes`` too, by default the
# cap: a JSON answer reaches flows whole (loop items), so the bound refuses
# what no spec could have asked for rather than trimming today's answers.
_DEFAULT_MAX_RESPONSE_BYTES = _MAX_UNTRUSTED_MAX_BYTES
_MAX_TIMEOUT_MS = 120_000
_MIN_TIMEOUT_MS = 1_000

#: Non-JSON response bodies are returned as text, bounded to their head and
#: tail (the agent loop applies its own per-result budget on top). A call
#: under ``to_workspace_path`` gets the whole body: the workspace holds it.
_MAX_TEXT_RESPONSE_CHARS = 20_000

# RFC 9110 token charset — httpx raises a bare ValueError (not caught by the
# httpx.HTTPError handler) when a header name contains anything else, so an
# invalid name must be rejected at validation time, not at call time. Method
# names are tokens too.
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Header *values* only need to exclude control characters (CR/LF injection,
# NUL); printable ASCII keeps the prefix predictable across HTTP stacks.
_HEADER_VALUE_SAFE_RE = re.compile(r"^[\x20-\x7e]*$")
# Both are applied with ``fullmatch``: ``$`` alone also matches before a
# trailing newline, which would let "Bearer\n" through.

# Characters left as-is in a cookie value built from a tool argument: the RFC
# 6265 cookie-octet set minus "%" (so the encoding is unambiguous). Space,
# DQUOTE, comma, semicolon and backslash are percent-encoded.
_COOKIE_SAFE_CHARS = "!#$&'()*+-./:<=>?@[]^_`{|}~"


class _SpecValueError(ValueError):
    """A tool argument that cannot be encoded as its mapping requires."""


# ---------------------------------------------------------------------------
# Value serialisation (OpenAPI parameter styles)
# ---------------------------------------------------------------------------


def _scalar_text(value: Any) -> str:
    """The wire text of one value: JSON literals for booleans, JSON for containers.

    ``True`` becomes ``true`` (not Python's ``True``): every web stack reads
    the lowercase literal, and a few reject the capitalised one.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return value
    if isinstance(value, int | float):
        return str(value)
    if value is None:
        return ""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_text(value: Any) -> str:
    """The ``style: json`` encoding: strings verbatim, anything else as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def field_source(param: str, field_spec: Any) -> str:
    """The tool argument a mapping entry reads (``from``, default the key)."""
    if isinstance(field_spec, dict):
        source = field_spec.get("from", param)
        return source if isinstance(source, str) else param
    if isinstance(field_spec, str) and field_spec:
        return field_spec
    return param


def _field_style(field_spec: Any, default: str) -> tuple[str, bool]:
    """``(style, explode)`` of a mapping entry, with the OpenAPI defaults."""
    if not isinstance(field_spec, dict):
        return default, default == "form"
    style = field_spec.get("style") or default
    explode = field_spec.get("explode")
    if not isinstance(explode, bool):
        explode = style == "form"
    return style, explode


def _query_pairs(
    name: str, value: Any, style: str, explode: bool, *, declared_style: bool = True
) -> list[tuple[str, str]]:
    """Serialise one query parameter into ``(name, value)`` pairs.

    Implements the OpenAPI 3 query styles (Swagger 2 ``collectionFormat`` is
    mapped onto them at import): an exploded array repeats the name, a
    non-exploded one joins with the style's delimiter, ``deepObject`` spells
    ``name[key]=value``, and an exploded ``form`` object contributes one
    parameter per property. ``None`` members are dropped; an empty array
    sends nothing.

    Only a mapping that DECLARES its style lets an object's keys name
    parameters: a bundle mapping entry (a bare string, or no ``style``) always
    sends under its declared name, an object as its JSON text, so a tool
    argument can never choose which query parameters a call sends.
    """
    if style == "json" or (isinstance(value, dict) and not declared_style):
        return [(name, _json_text(value))]
    if isinstance(value, list):
        items = [_scalar_text(v) for v in value if v is not None]
        if not items:
            return []
        if explode and style in _DELIMITERS:
            return [(name, item) for item in items]
        return [(name, _DELIMITERS.get(style, ",").join(items))]
    if isinstance(value, dict):
        entries = [(str(k), v) for k, v in value.items() if v is not None]
        if not entries:
            return []
        if style == "deepObject":
            return [(f"{name}[{k}]", _scalar_text(v)) for k, v in entries]
        if style == "form" and explode:
            return [(k, _scalar_text(v)) for k, v in entries]
        flat = [part for k, v in entries for part in (k, _scalar_text(v))]
        return [(name, _DELIMITERS.get(style, ",").join(flat))]
    return [(name, _scalar_text(value))]


def _simple_text(value: Any, *, explode: bool, encode: Callable[[str], str]) -> str:
    """OpenAPI ``simple`` style (path and header parameters)."""
    if isinstance(value, list):
        return ",".join(encode(_scalar_text(v)) for v in value if v is not None)
    if isinstance(value, dict):
        entries = [(str(k), v) for k, v in value.items() if v is not None]
        if explode:
            return ",".join(f"{encode(k)}={encode(_scalar_text(v))}" for k, v in entries)
        return ",".join(
            part for k, v in entries for part in (encode(k), encode(_scalar_text(v)))
        )
    return encode(_scalar_text(value))


def _path_segment(param: str, value: Any, field_spec: Any) -> str:
    """Encode one path parameter; refuse values that would reshape the path.

    Every reserved character is percent-encoded (``/`` included), so a value
    stays inside its segment. A segment that comes out as ``""``, ``.`` or
    ``..`` is refused, whatever the value was (``[".."]`` and ``[]`` included):
    an HTTP stack normalises ``/users/..`` to ``/``, which would turn a call
    on one resource into a call on its parent. So is a segment that holds a
    dot segment once decoded (:func:`_decodes_to_dot_segment`).
    """
    style, explode = _field_style(field_spec, "simple")
    if style == "json":
        segment = quote(_json_text(value), safe="")
    else:
        segment = _simple_text(value, explode=explode, encode=lambda s: quote(s, safe=""))
    if segment == "" or _decodes_to_dot_segment(segment):
        raise _SpecValueError(
            f"Path parameter '{param}' must be a non-empty value that is not, and does "
            "not decode to, a '.' or '..' segment."
        )
    return segment


def _decodes_to_dot_segment(segment: str) -> bool:
    """Whether *segment*, as sent or percent-decoded, holds a ``.`` or ``..`` segment.

    A reverse proxy and an application that each decode the path once turn
    ``%252e%252e`` into ``..``, and ``a%2F..%2Fb`` into ``a/../b``: the
    credentialed call would then be normalised onto an endpoint the spec
    never declared. Each of up to ``_PATH_DECODE_ROUNDS`` decodings is judged,
    split on ``/`` and ``\\``, a ``;`` path parameter cut off (``..;x`` is
    ``..`` to a servlet container); a value still decoding past the last
    round is refused. ``v1..2`` holds no dot segment and is sent.
    """
    text = segment
    for _ in range(_PATH_DECODE_ROUNDS + 1):
        for part in _PATH_SEPARATORS.split(text):
            if part.split(";", 1)[0] in (".", ".."):
                return True
        decoded = unquote(text)
        if decoded == text:
            return False
        text = decoded
    return True


def _header_value(name: str, value: Any, field_spec: Any) -> str:
    """Encode one header parameter; refuse anything but printable ASCII."""
    style, explode = _field_style(field_spec, "simple")
    text = (
        _json_text(value)
        if style == "json"
        else _simple_text(value, explode=explode, encode=lambda s: s)
    )
    if not _HEADER_VALUE_SAFE_RE.fullmatch(text):
        raise _SpecValueError(
            f"Header '{name}' only accepts printable ASCII characters (no line breaks)."
        )
    return text


def _cookie_value(value: Any, field_spec: Any = None) -> str:
    style, _explode = _field_style(field_spec, "form")
    text = _json_text(value) if style == "json" else _simple_text(
        value, explode=False, encode=lambda s: s
    )
    return quote(text, safe=_COOKIE_SAFE_CHARS)


# ---------------------------------------------------------------------------
# Request pieces
# ---------------------------------------------------------------------------


def _parse_mapping(mapping: Any) -> dict[str, Any] | None:
    """A mapping spec as a dict, tolerating double-serialised DB values."""
    if isinstance(mapping, str):
        try:
            mapping = json.loads(mapping)
        except (json.JSONDecodeError, ValueError):
            return None
    return mapping if isinstance(mapping, dict) else None


def _parse_field_spec(field_spec: Any) -> Any:
    """A field spec, tolerating a double-serialised JSON object."""
    if isinstance(field_spec, str):
        try:
            parsed = json.loads(field_spec)
        except (json.JSONDecodeError, ValueError):
            return field_spec
        return parsed if isinstance(parsed, dict) else field_spec
    return field_spec


def _resolve_auth_header(
    spec: dict[str, Any],
    token: str,
) -> dict[str, str]:
    """Build the Authorization (or custom) header from the execution spec.

    Header-only view of :func:`_resolve_auth`, kept for callers that only send
    headers (the bundle ``test_connection`` probe).
    """
    return _resolve_auth(spec.get("auth"), token).headers


def _resolve_base_url(
    spec: dict[str, Any],
    extra_config: dict[str, Any] | None,
) -> str | None:
    """Extract base_url from extra_config using the spec's key."""
    key = spec.get("base_url_key") or "base_url"
    raw = (extra_config or {}).get(key)
    if not isinstance(raw, str) or not raw.strip():
        return None
    return raw.strip().rstrip("/")


def same_origin(url: str, other: str | None) -> bool:
    """Whether *url* and *other* share scheme, host and port (default ports filled)."""
    if other is None:
        return False
    if url == other:
        return True
    origins = []
    for value in (url, other):
        try:
            parsed = urlparse(value)
            port = parsed.port
        except ValueError:
            return False
        scheme = (parsed.scheme or "").lower()
        if not scheme or not parsed.hostname:
            return False
        origins.append((scheme, parsed.hostname.lower(), port or _DEFAULT_PORTS.get(scheme)))
    return origins[0] == origins[1]


def _interpolate_path(
    path: str,
    path_params: list[str],
    kwargs: dict[str, Any],
    path_mapping: dict[str, Any] | None = None,
) -> str:
    """Substitute {param} placeholders in the path from tool arguments.

    Each value is percent-encoded (``/`` included) so it cannot inject extra
    path, query or fragment parts; see :func:`_path_segment`.
    """
    result = path
    mapping = path_mapping or {}
    for param in path_params:
        field_spec = _parse_field_spec(mapping.get(param))
        value = kwargs.get(field_source(param, field_spec))
        if value is None:
            continue
        result = result.replace(f"{{{param}}}", _path_segment(param, value, field_spec))
    return result


def _build_body(mapping: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    """Build request body from mapping spec + tool arguments.

    Handles the case where ``mapping`` was stored as a JSON string (e.g. from
    a double-serialized metadata_json column) by attempting to parse it.
    """
    parsed = _parse_mapping(mapping)
    if parsed is None:
        return {}

    body: dict[str, Any] = {}
    for request_key, raw_spec in parsed.items():
        field_spec = _parse_field_spec(raw_spec)
        if isinstance(field_spec, dict) and "value" in field_spec:
            body[request_key] = field_spec["value"]
            continue
        if isinstance(field_spec, dict):
            source_key = field_spec.get("from", request_key)
            required = field_spec.get("required", False)
            value = kwargs.get(source_key)
            if value is None and required:
                # Let the LLM know what's missing
                raise ValueError(f"Required field '{source_key}' is missing.")
            if value is not None:
                body[request_key] = value
        else:
            # Simple string mapping: {"query": "query"} means from=to
            value = kwargs.get(str(field_spec) if field_spec else request_key)
            if value is not None:
                body[str(request_key)] = value
    return body


def _build_query_params(
    mapping: Any, kwargs: dict[str, Any]
) -> dict[str, str | list[str]]:
    """Build query parameters from mapping spec + tool arguments.

    A parameter serialised to several pairs (an exploded array) becomes a list
    value, which httpx sends as a repeated key. Same defensive parsing as
    :func:`_build_body` for double-serialised mappings. ``required`` is not
    enforced here, as it never was for a query mapping: the tool's input
    schema says which arguments a call must carry.
    """
    parsed = _parse_mapping(mapping)
    if parsed is None:
        return {}

    params: dict[str, str | list[str]] = {}
    for param_name, raw_spec in parsed.items():
        field_spec = _parse_field_spec(raw_spec)
        if isinstance(field_spec, dict) and "value" in field_spec:
            params[param_name] = _scalar_text(field_spec["value"])
            continue
        source = field_source(param_name, field_spec)
        value = kwargs.get(source)
        if value is None:
            continue
        style, explode = _field_style(field_spec, "form")
        declared_style = isinstance(field_spec, dict) and isinstance(field_spec.get("style"), str)
        for key, text in _query_pairs(
            param_name, value, style, explode, declared_style=declared_style
        ):
            existing = params.get(key)
            if existing is None:
                params[key] = text
            elif isinstance(existing, list):
                existing.append(text)
            else:
                params[key] = [existing, text]
    return params


def _wire_method(raw: Any) -> str:
    """The method as sent: standard ones in capitals, others as declared."""
    if not isinstance(raw, str) or not raw.strip():
        return "GET"
    method = raw.strip()
    return method.upper() if method.upper() in _STANDARD_METHODS else method


def _build_querystring(
    field_spec: Any, kwargs: dict[str, Any]
) -> tuple[dict[str, str | list[str]], str | None]:
    """An OpenAPI 3.2 ``querystring`` argument: ``(form fields, raw query)``.

    A form (``application/x-www-form-urlencoded``) object becomes one query
    parameter per key; any other media type sends the value serialised as
    that media type (JSON for a JSON one), percent-encoded, as the whole
    query string.
    """
    field_spec = _parse_field_spec(field_spec)
    if not isinstance(field_spec, dict):
        return {}, None
    source = field_source("querystring", field_spec)
    value = kwargs.get(source)
    if value is None:
        if field_spec.get("required") is True:
            raise _SpecValueError(f"Required query string '{source}' is missing.")
        return {}, None
    media_type = str(field_spec.get("media_type") or "application/x-www-form-urlencoded")
    if media_type.split(";", 1)[0].strip().lower() == "application/x-www-form-urlencoded":
        if not isinstance(value, dict):
            raise _SpecValueError(f"The query string '{source}' must be an object of fields.")
        return _form_fields(value), None
    text = _json_text(value) if "json" in media_type.lower() else _scalar_text(value)
    return {}, quote(text, safe="")


def _response_summary(resp: httpx.Response) -> str:
    """A body-less answer (HEAD, OPTIONS): the status and the headers."""
    headers: dict[str, str] = {}
    for name, value in resp.headers.items():
        if name.lower() == "set-cookie" or len(headers) >= _MAX_RESPONSE_HEADERS:
            continue
        headers[name] = value[:_MAX_RESPONSE_HEADER_CHARS]
    return json.dumps({"status": resp.status_code, "headers": headers}, ensure_ascii=False)


def _build_mapped_values(
    mapping: Any,
    kwargs: dict[str, Any],
    *,
    kind: str,
) -> list[tuple[str, Any, Any]]:
    """``(name, value, field_spec)`` for every mapped argument that is present."""
    parsed = _parse_mapping(mapping)
    if parsed is None:
        return []
    out: list[tuple[str, Any, Any]] = []
    for name, raw_spec in parsed.items():
        field_spec = _parse_field_spec(raw_spec)
        source = field_source(name, field_spec)
        value = kwargs.get(source)
        if value is None:
            if isinstance(field_spec, dict) and field_spec.get("required") is True:
                raise _SpecValueError(f"Required {kind} '{source}' is missing.")
            continue
        out.append((str(name), value, field_spec))
    return out


def _form_fields(body: dict[str, Any]) -> dict[str, str | list[str]]:
    """Form-encoded body fields: arrays repeat the key, objects become JSON."""
    fields: dict[str, str | list[str]] = {}
    for key, value in body.items():
        if value is None:
            continue
        if isinstance(value, list):
            fields[str(key)] = [_scalar_text(v) for v in value if v is not None]
        else:
            fields[str(key)] = _scalar_text(value)
    return fields


def _multipart_parts(body: dict[str, Any]) -> list[tuple[str, tuple[Any, ...]]]:
    """Multipart parts: ``(None, text)`` for a field, ``(name, bytes, type)`` for a file.

    Arrays repeat the field, objects are sent as JSON text (as in a form).
    """
    parts: list[tuple[str, tuple[Any, ...]]] = []
    for key, value in body.items():
        if value is None:
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, UploadFile):
                parts.append((str(key), (item.filename, item.content, item.content_type)))
            elif item is not None:
                parts.append((str(key), (None, _scalar_text(item))))
    return parts


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UploadFile:
    """A file argument resolved to what the request sends."""

    filename: str
    content: bytes
    content_type: str


def _file_sources(spec: dict[str, Any]) -> list[tuple[str, str | None]]:
    """``(argument, default media type)`` of every argument that carries a file."""
    sources: list[tuple[str, str | None]] = []
    body_from = spec.get("body_from")
    if spec.get("body_encoding") == "binary" and isinstance(body_from, str) and body_from:
        declared = spec.get("content_type")
        sources.append((body_from, declared if isinstance(declared, str) else None))
    mapping = _parse_mapping(spec.get("body_mapping"))
    if spec.get("body_encoding") == "multipart" and mapping:
        for key, raw_spec in mapping.items():
            field_spec = _parse_field_spec(raw_spec)
            if isinstance(field_spec, dict) and field_spec.get("file") is True:
                declared = field_spec.get("content_type")
                sources.append(
                    (field_source(key, field_spec), declared if isinstance(declared, str) else None)
                )
    return sources


def _safe_filename(raw: Any) -> str:
    """A filename safe in a Content-Disposition header (no path, no control characters)."""
    if not isinstance(raw, str):
        return ""
    name = raw.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\r\n')
    return name.strip()[:_MAX_UPLOAD_FILENAME_CHARS]


def _safe_media_type(raw: Any) -> str | None:
    if isinstance(raw, str) and _MEDIA_TYPE_RE.fullmatch(raw.strip()):
        return raw.strip()
    return None


async def _resolve_upload(
    value: Any,
    *,
    argument: str,
    file_context: Any,
    default_type: str | None,
) -> UploadFile:
    """Read one file argument (see "File arguments" in the module docstring)."""
    if isinstance(value, str):
        known = file_context is not None and file_context.get(value.strip()) is not None
        value = {"file_id": value} if known else {"content": value}
    if not isinstance(value, dict):
        raise _SpecValueError(
            f"'{argument}' must be a file: an object with file_id or content."
        )
    file_id = value.get("file_id")
    content = value.get("content")
    filename = _safe_filename(value.get("filename"))
    content_type = _safe_media_type(value.get("content_type"))
    if isinstance(file_id, str) and file_id.strip():
        if file_context is None:
            raise _SpecValueError(
                f"No files are available here, so '{argument}' cannot use file_id. "
                "Pass the file's text as content instead."
            )
        try:
            resolved = await file_context.resolve_upload(file_id.strip())
        except Exception as exc:
            raise _SpecValueError(
                f"The file '{file_id.strip()[:120]}' could not be read: {type(exc).__name__}."
            ) from None
        if resolved is None:
            raise _SpecValueError(
                f"The file '{file_id.strip()[:120]}' is not available in this conversation."
            )
        data, stored_name, stored_type = resolved
        filename = filename or _safe_filename(stored_name)
        content_type = content_type or _safe_media_type(stored_type)
    elif isinstance(content, str):
        data = content.encode("utf-8")
    else:
        raise _SpecValueError(
            f"'{argument}' needs file_id (a file available in this conversation) "
            "or content (the file's text)."
        )
    if len(data) > MAX_UPLOAD_BYTES:
        raise _SpecValueError(
            f"'{argument}' is {len(data) // (1024 * 1024)} MB; files up to "
            f"{MAX_UPLOAD_BYTES // (1024 * 1024)} MB can be uploaded."
        )
    if not content_type and filename:
        content_type = _MEDIA_TYPES.guess_type(filename)[0]
    content_type = content_type or _safe_media_type(default_type) or "application/octet-stream"
    if not filename:
        extension = _MEDIA_TYPES.guess_extension(content_type.split(";", 1)[0].strip()) or ""
        filename = f"{argument}{extension}"
    return UploadFile(filename=filename, content=data, content_type=content_type)


async def _resolve_file_arguments(
    spec: dict[str, Any], kwargs: dict[str, Any], file_context: Any
) -> dict[str, Any]:
    """*kwargs* with each file argument replaced by the :class:`UploadFile` it names (a list for several)."""
    sources = _file_sources(spec)
    if not sources:
        return kwargs
    resolved = dict(kwargs)
    binary_body = spec.get("body_from") if spec.get("body_encoding") == "binary" else None
    for argument, default_type in sources:
        value = resolved.get(argument)
        if value is None:
            continue
        if isinstance(value, list):
            if argument == binary_body:
                raise _SpecValueError(f"'{argument}' takes one file, not a list.")
            resolved[argument] = [
                await _resolve_upload(
                    item, argument=argument, file_context=file_context, default_type=default_type
                )
                for item in value
                if item is not None
            ]
        else:
            resolved[argument] = await _resolve_upload(
                value, argument=argument, file_context=file_context, default_type=default_type
            )
    return resolved


def _extract_response(data: Any, response_path: str | None) -> Any:
    """Extract a nested value from the response using dot notation."""
    if not response_path or data is None:
        return data
    current = data
    for key in response_path.split("."):
        if isinstance(current, dict):
            current = current.get(key)
        elif isinstance(current, list) and key.isascii() and key.isdigit() and len(key) < 19:
            idx = int(key)
            current = current[idx] if idx < len(current) else None
        else:
            return current
    return current


# ---------------------------------------------------------------------------
# Credential
# ---------------------------------------------------------------------------


@dataclass
class _AuthParts:
    """Where the credential goes for one request."""

    headers: dict[str, str] = field(default_factory=dict)
    params: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)
    #: A bearer token in ``Authorization`` — the one shape a re-minted token
    #: can replace (``AutoRefreshClient`` rewrites exactly that header).
    refreshable: bool = False


def _resolve_auth(auth: Any, token: str | None) -> _AuthParts:
    """Place *token* where the spec's ``auth`` block says.

    ``NO_AUTH_TOKEN`` (and an empty token) means the integration holds no
    credential: nothing is sent, whatever the spec's type.
    """
    if not token or token == NO_AUTH_TOKEN:
        return _AuthParts()

    auth = auth if isinstance(auth, dict) else {}
    auth_type = auth.get("type") or "bearer"
    auth_type = auth_type.lower() if isinstance(auth_type, str) else "bearer"

    if auth_type == "none":
        return _AuthParts()

    if auth_type == "header":
        header_name = auth.get("header") or "Authorization"
        if not credential_header_allowed(header_name):
            raise _SpecValueError(
                f"The credential cannot be sent in the '{str(header_name)[:80]}' header."
            )
        prefix = auth.get("prefix")
        if prefix is None:
            prefix = "Bearer"
        value = f"{prefix} {token}" if prefix else token
        return _AuthParts(
            headers={header_name: value},
            refreshable=header_name.lower() == "authorization" and prefix == "Bearer",
        )

    if auth_type == "query":
        return _AuthParts(params={str(auth.get("param") or "api_key"): token})

    if auth_type == "cookie":
        return _AuthParts(cookies={str(auth.get("cookie") or "api_key"): token})

    if auth_type == "basic":
        # The credential is "username:password" (curl -u). A value with no
        # colon is a username with an empty password — how token-as-username
        # APIs (Stripe style) are called with Basic auth.
        userpass = token if ":" in token else f"{token}:"
        encoded = base64.b64encode(userpass.encode("utf-8")).decode("ascii")
        return _AuthParts(headers={"Authorization": f"Basic {encoded}"})

    # Default: bearer
    return _AuthParts(headers={"Authorization": f"Bearer {token}"}, refreshable=True)


def _with_auth_scheme(err: str, auth: Any, sent: _AuthParts) -> str:
    """*err* (a 401) plus how the call authenticated, so the cause is visible.

    "The token may be expired" misleads when the call carried no credential
    at all, or carried it in a scheme the API does not take.
    """
    if not (sent.headers or sent.params or sent.cookies):
        return f"{err} The call carried NO credential (auth scheme sent: none)."
    kind = auth.get("type") if isinstance(auth, dict) else None
    kind = kind.lower() if isinstance(kind, str) and kind else "bearer"
    return f"{err} Auth scheme sent: {kind}."


def _drop_case_insensitive(target: dict[str, Any], names: set[str]) -> None:
    lowered = {n.lower() for n in names}
    for key in [k for k in target if k.lower() in lowered]:
        del target[key]


# ---------------------------------------------------------------------------
# Bounded answers
# ---------------------------------------------------------------------------


def _max_response_bytes(spec: dict[str, Any], default: int) -> int:
    """The spec's ``max_response_bytes`` (never above the cap), else *default*."""
    value = spec.get("max_response_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return min(value, _MAX_UNTRUSTED_MAX_BYTES)
    return default


def _declared_over(headers: httpx.Headers, max_bytes: int) -> bool:
    """Whether the answer's ``Content-Length`` is over *max_bytes*."""
    declared = headers.get("content-length", "")
    # ASCII digits of a bounded length: int() refuses a 5,000-digit string.
    return declared.isascii() and declared.isdigit() and len(declared) <= 20 and int(declared) > max_bytes


def _too_large_answer(provider_name: str, action: str, max_bytes: int) -> str:
    return f"Error: the {provider_name} answer to {action} is over the {max_bytes // 1024} KB limit; it is not read."


def _compressed_answer(provider_name: str, action: str) -> str:
    return f"Error: {provider_name} answered {action} compressed; the answer is not read."


class _AnswerRefusedError(Exception):
    """An answer the default path stops reading (too large, or compressed).

    Not an ``httpx.HTTPError``, so it leaves ``safe_request`` as it is
    instead of being reported as a network failure.
    """

    def __init__(self, compressed: str | None = None) -> None:
        super().__init__(compressed or "too large")
        self.compressed = compressed


class _BoundedAnswerStream(httpx.AsyncByteStream):
    """A response body whose raw bytes are counted as they arrive.

    Reading stops past *max_bytes*, and at the first byte of a compressed
    body: httpx inflates what it reads, so a compressed body is not bounded
    by the bytes read.
    """

    def __init__(self, stream: httpx.AsyncByteStream, max_bytes: int, compressed: str | None) -> None:
        self._stream = stream
        self._max_bytes = max_bytes
        self._compressed = compressed

    async def __aiter__(self) -> AsyncIterator[bytes]:
        read = 0
        async for chunk in self._stream:
            if not chunk:
                continue
            if self._compressed is not None:
                raise _AnswerRefusedError(self._compressed)
            read += len(chunk)
            if read > self._max_bytes:
                raise _AnswerRefusedError()
            yield chunk

    async def aclose(self) -> None:
        await self._stream.aclose()


def _bounded_answer_hook(max_bytes: int) -> Callable[[httpx.Response], Awaitable[None]]:
    """An httpx response hook reading every answer of the client within *max_bytes*.

    It runs before httpx reads the body, on each attempt ``safe_request`` or
    the 401/403 refresh makes. A HEAD answer has no body: its
    ``Content-Length`` describes a GET's, so it is left as it is.
    """

    async def hook(response: httpx.Response) -> None:
        if response.request.method == "HEAD" or not isinstance(response.stream, httpx.AsyncByteStream):
            return
        if _declared_over(response.headers, max_bytes):
            raise _AnswerRefusedError()
        encoding = response.headers.get("content-encoding", "").strip().lower()
        response.stream = _BoundedAnswerStream(
            response.stream, max_bytes, None if encoding in ("", "identity") else encoding
        )

    return hook


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def execute_declarative_tool(
    spec: dict[str, Any],
    kwargs: dict[str, Any],
    *,
    token: str,
    extra_config: dict[str, Any] | None,
    provider_name: str,
    tool_name: str,
    token_provider: TokenProvider | None = None,
    refresh_fn: RefreshFn | None = None,
    enforce_destination_guard: bool = False,
    spec_servers_own: bool | None = None,
    file_context: Any = None,
    ssl_context: ssl.SSLContext | None = None,
) -> str:
    """Execute a declarative HTTP tool call.

    Args:
        spec: The tool's ``execution`` spec (see the module docstring).
        kwargs: Tool arguments from the LLM.
        token: Decrypted integration credential (API key / access token).
        extra_config: Decrypted extra_config dict (contains base_url etc.).
        provider_name: Human-readable provider name for error messages.
        tool_name: Tool name for error context.
        token_provider: When given, called per request for the credential
            (replaces *token*) — e.g. a cached OAuth client-credentials token.
        refresh_fn: Re-mints a bearer token when the API answers 401/403;
            the request is retried once with the new token.
        enforce_destination_guard: Refuse loopback, link-local, reserved and
            cloud-metadata destinations (private networks stay reachable).
            Set by callers whose URLs are user-supplied.
        spec_servers_own: Whether, without a configured base URL, the spec's
            per-operation server is the integration's own server (it gets the
            credential). ``None``: yes unless *enforce_destination_guard* -
            a bundle's spec is imported by an admin, an OpenAPI document is
            user-supplied.
        file_context: The conversation's files (``app.tools.base.FileContext``),
            which ``file_id`` file arguments are read from.
        ssl_context: A client certificate to present (mutual TLS).

    Returns:
        JSON-formatted string of the response (or extracted sub-path), or an
        ``Error: ...`` string the LLM can act on.
    """
    method = _wire_method(spec.get("method"))
    if method.upper() in _REFUSED_METHODS:
        return f"Error: {provider_name} tool '{tool_name}' cannot send {method.upper()} requests."

    # Resolve base URL — a per-operation server wins over the integration's.
    configured_base_url = _resolve_base_url(spec, extra_config)
    base_url = configured_base_url
    server_url = spec.get("server_url")
    if isinstance(server_url, str) and server_url.strip():
        base_url = server_url.strip().rstrip("/")
    if not base_url:
        key = spec.get("base_url_key") or "base_url"
        return (
            f"Error: {provider_name} integration is missing '{key}' in its "
            f"configuration. Please set the base URL in the integration settings."
        )

    raw_path = spec.get("path") or "/"
    path = raw_path.strip() if isinstance(raw_path, str) else "/"
    path_params = spec.get("path_params") or []
    path_mapping = _parse_mapping(spec.get("path_mapping")) or {}

    # Check required path params
    for param in path_params:
        source = field_source(param, _parse_field_spec(path_mapping.get(param)))
        if kwargs.get(source) is None:
            return f"Error: Required path parameter '{source}' is missing."

    raw_query: str | None = None
    try:
        url = base_url + _interpolate_path(path, path_params, kwargs, path_mapping)
        query_params: dict[str, str | list[str]] | None = None
        if spec.get("query_mapping"):
            query_params = _build_query_params(spec["query_mapping"], kwargs)
        if spec.get("querystring"):
            fields, raw_query = _build_querystring(spec["querystring"], kwargs)
            if fields:
                query_params = {**(query_params or {}), **fields}
        mapped_headers = _build_mapped_values(
            spec.get("header_mapping"), kwargs, kind="header"
        )
        mapped_cookies = _build_mapped_values(
            spec.get("cookie_mapping"), kwargs, kind="cookie"
        )
        headers: dict[str, str] = {
            name: _header_value(name, value, field_spec)
            for name, value, field_spec in mapped_headers
        }
        cookies: dict[str, str] = {
            name: _cookie_value(value, field_spec)
            for name, value, field_spec in mapped_cookies
        }
        if method not in _BODYLESS_METHODS:
            kwargs = await _resolve_file_arguments(spec, kwargs, file_context)
        body_kwargs, body_content_type = _build_request_body(spec, method, kwargs)
    except ValueError as exc:
        return f"Error: {exc}"

    accept = spec.get("accept")
    if isinstance(accept, str) and accept.strip():
        headers["Accept"] = accept.strip()
    if body_content_type:
        headers["Content-Type"] = body_content_type

    # Credential — applied LAST so no mapped argument can override it, and
    # only on the integration's own server: an operation the spec places on
    # another origin is called without it. Without a configured base URL, a
    # bundle's per-operation server IS the integration's server; a caller whose
    # spec is user-supplied (an OpenAPI integration) always configures one.
    if spec_servers_own is None:
        spec_servers_own = not enforce_destination_guard
    own_server = configured_base_url if configured_base_url is not None else (
        base_url if spec_servers_own else None
    )
    foreign_server = not same_origin(base_url, own_server)
    if foreign_server and own_server is None:
        key = spec.get("base_url_key") or "base_url"
        return (
            f"Error: {provider_name} integration is missing '{key}' in its "
            f"configuration. Please set the base URL in the integration settings."
        )
    auth = _AuthParts()
    if not foreign_server:
        if token_provider is not None:
            try:
                token = await token_provider() or ""
            except Exception as exc:
                logger.warning(
                    "Credential acquisition failed for %s tool %s: %s",
                    provider_name,
                    tool_name,
                    type(exc).__name__,
                )
                return (
                    f"Error: {provider_name} could not obtain an access token: {exc}. "
                    f"Check the integration's OAuth credentials and token URL."
                )
        try:
            auth = _resolve_auth(spec.get("auth"), token)
        except _SpecValueError as exc:
            return f"Error: {exc}"
    _drop_case_insensitive(headers, set(auth.headers))
    headers.update(auth.headers)
    if auth.params:
        query_params = dict(query_params or {})
        for name in auth.params:
            query_params.pop(name, None)
        query_params.update(auth.params)
    if auth.cookies:
        for name in auth.cookies:
            cookies.pop(name, None)
        cookies.update({k: _cookie_value(v) for k, v in auth.cookies.items()})
    # A credential sent as the Cookie header itself is kept, first, and no
    # mapped cookie of a name it carries follows it: a server reading the
    # last duplicate would take the caller's value for the credential's.
    credential_cookie = next(
        (value for name, value in auth.headers.items() if name.lower() == "cookie"), None
    )
    if credential_cookie:
        held = {part.partition("=")[0].strip() for part in credential_cookie.split(";")}
        cookies = {name: value for name, value in cookies.items() if name not in held}
    if cookies:
        mapped = "; ".join(f"{k}={v}" for k, v in cookies.items())
        _drop_case_insensitive(headers, {"Cookie"})
        headers["Cookie"] = f"{credential_cookie}; {mapped}" if credential_cookie else mapped

    if raw_query:
        # httpx replaces a URL's query whenever ``params`` is given (even an
        # empty one), so the whole query string and the other parameters (the
        # credential's included) are joined here. The raw query is fully
        # percent-encoded: it cannot name a parameter of its own.
        extra = str(httpx.QueryParams(query_params)) if query_params else ""
        url = f"{url}?{raw_query}" + (f"&{extra}" if extra else "")
        query_params = None

    if enforce_destination_guard:
        try:
            await validate_outbound_url_async(url, allow_private=True)
        except ValueError as exc:
            return f"Error: {provider_name} request refused: {exc}"

    # Timeout — validated at import time, but specs persisted by older
    # versions (or edited in the DB) may carry non-numeric values: fall back
    # to the default instead of crashing the tool call.
    try:
        timeout_ms = int(spec.get("timeout_ms") or _DEFAULT_TIMEOUT_MS)
    except (TypeError, ValueError):
        timeout_ms = _DEFAULT_TIMEOUT_MS
    timeout_ms = max(_MIN_TIMEOUT_MS, min(_MAX_TIMEOUT_MS, timeout_ms))
    timeout_s = timeout_ms / 1000.0

    action = f"execute tool '{tool_name}'"
    # User-supplied destinations: connections are pinned to checked addresses.
    # A client certificate rides on the connection either way.
    client_options: dict[str, Any] = (
        guarded_client_options(allow_private=True, verify=ssl_context or True)
        if enforce_destination_guard
        else ({"verify": ssl_context} if ssl_context is not None else {})
    )
    if spec.get("untrusted_response") is True:
        return await _execute_untrusted(
            spec,
            method,
            url,
            headers=headers,
            params=query_params,
            body_kwargs=body_kwargs,
            client_options=client_options,
            timeout_s=timeout_s,
            provider_name=provider_name,
            action=action,
        )
    # The answer is asked for uncompressed and read within its byte budget
    # (``_bounded_answer_hook``), on every attempt the client makes.
    max_bytes = _max_response_bytes(spec, _DEFAULT_MAX_RESPONSE_BYTES)
    _drop_case_insensitive(headers, {"Accept-Encoding"})
    headers["Accept-Encoding"] = "identity"
    event_hooks = dict(client_options.get("event_hooks") or {})
    event_hooks["response"] = [*event_hooks.get("response", []), _bounded_answer_hook(max_bytes)]
    bounded_options = {**client_options, "event_hooks": event_hooks}
    client: AutoRefreshClient | httpx.AsyncClient
    if refresh_fn is not None and auth.refreshable:
        # ``headers`` is handed to the client AND to the request, so the
        # re-minted token the client writes into it rides on the retry.
        client = AutoRefreshClient(headers, refresh_fn, timeout=timeout_s, **bounded_options)
    else:
        client = egress_client(timeout=timeout_s, **bounded_options)
    try:
        async with client as http:
            resp, err = await safe_request(
                http,
                method,
                url,
                provider_name,
                action,
                retry_statuses=(
                    TRANSIENT_RETRY_STATUSES if method in _RETRYABLE_METHODS else None
                ),
                # ``timeout_ms`` bounds the whole call, retries and their waits
                # included, as it bounded the single request before retries.
                max_total_seconds=timeout_s,
                headers=headers,
                params=query_params,
                # On another server, the failed response tells a refusal apart.
                **({"expected_statuses": set()} if foreign_server else {}),
                **body_kwargs,
            )
    except _AnswerRefusedError as exc:
        if exc.compressed is not None:
            logger.warning("%s: %s answered compressed (%r)", provider_name, action, exc.compressed[:40])
            return _compressed_answer(provider_name, action)
        logger.warning("%s: the answer to %s is over %d bytes", provider_name, action, max_bytes)
        return _too_large_answer(provider_name, action, max_bytes)
    if err or resp is None:
        if err and foreign_server and resp is not None and resp.status_code in (401, 403):
            # Not a stale token: re-authorizing would not help.
            return (
                f"Error: {provider_name} refused the call (HTTP {resp.status_code}). This "
                f"operation runs on {base_url}, another server than the integration's base "
                "URL, so it is called without the integration's credentials."
            )
        if err and resp is not None and resp.status_code == 401:
            return _with_auth_scheme(err, spec.get("auth"), auth)
        return err or f"Error: {provider_name} request failed while trying to {action}."
    if method in _HEADER_METHODS and (method == "HEAD" or not resp.content):
        return _response_summary(resp)

    content_type = resp.headers.get("content-type", "")
    if not _answer_is_text(content_type, resp.content):
        return _unreadable_answer(provider_name, action, f"binary content under {content_type[:80]!r}")
    # Parse and extract response
    try:
        data = resp.json()
    except RecursionError:
        return _unreadable_answer(provider_name, action, "JSON nested past the recursion limit")
    except (json.JSONDecodeError, ValueError):
        # Non-JSON response: the raw text, its head and tail when long, whole
        # when it goes to the sandbox workspace.
        from app.tools.base import clip_head_and_tail, workspace_ingest_requested

        text = resp.text
        if not workspace_ingest_requested():
            text = clip_head_and_tail(
                text,
                _MAX_TEXT_RESPONSE_CHARS,
                hint="Call again with to_workspace_path to write the whole answer to the sandbox workspace.",
            )
        return text if text else f"{provider_name} returned empty response (HTTP {resp.status_code})."

    extracted = _extract_response(data, spec.get("response_path"))
    try:
        return json.dumps(extracted, default=str, ensure_ascii=False)
    except RecursionError:
        return _unreadable_answer(provider_name, action, "JSON nested past the recursion limit")


def _answer_is_text(content_type: str, body: bytes) -> bool:
    """Whether an answer is text the model can be handed.

    A declared text, JSON or XML type is read in its charset, as it always
    was. Any other type, or none (``application/octet-stream``, a vendor type,
    an image, a PDF), is read only when its bytes are strict UTF-8 holding no
    NUL: a binary answer would otherwise reach the model as
    replacement-character noise.
    """
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime.startswith(TEXT_MIME_PREFIXES) or mime.endswith(("+json", "+xml")):
        return True
    if b"\x00" in body:
        return False
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _unreadable_answer(provider_name: str, action: str, reason: str) -> str:
    """The fixed ``Error:`` sentence for an answer that is not read; *reason* goes to the logs only."""
    logger.warning("%s: the answer to %s is not read: %s", provider_name, action, reason)
    return f"Error: the {provider_name} answer to {action} is not readable text; it is not read."


def _with_identity_encoding(headers: dict[str, str]) -> httpx.Headers:
    """*headers* asking for an uncompressed answer, with ONE ``Accept-Encoding``.

    Set on ``httpx.Headers``, which replaces the name whatever its case: a
    plain dict merge would keep an ``accept-encoding`` sent by a header
    mapping or the credential beside it, and httpx would send both.
    """
    merged = httpx.Headers(headers)
    merged["Accept-Encoding"] = "identity"
    return merged


def _untrusted_max_bytes(spec: dict[str, Any]) -> int:
    return _max_response_bytes(spec, _DEFAULT_UNTRUSTED_MAX_BYTES)


async def _execute_untrusted(
    spec: dict[str, Any],
    method: str,
    url: str,
    *,
    headers: dict[str, str],
    params: dict[str, str | list[str]] | None,
    body_kwargs: dict[str, Any],
    client_options: dict[str, Any],
    timeout_s: float,
    provider_name: str,
    action: str,
) -> str:
    """A call whose answer is third-party data (``untrusted_response: true``).

    No redirect followed, no proxy settings read from the environment, no
    retry, the answer streamed as raw bytes up to its byte budget (identity
    encoding, a compressed answer refused), an error answered by a fixed
    sentence (the upstream body never read), and the result handed to the
    model inside a ``wrap_untrusted`` block so its text is never read as
    instructions. A guarded destination keeps its pinned transport, and only
    it: the proxy mounts ``guarded_client_options`` reads from the environment
    (and their URL check) are left out, so the connection is a direct one.
    """
    max_bytes = _untrusted_max_bytes(spec)
    raw = bytearray()
    direct_options = {
        name: value for name, value in client_options.items() if name not in ("mounts", "event_hooks")
    }
    try:
        async with (
            egress_client(
                timeout=timeout_s, trust_env=False, follow_redirects=False, **direct_options
            ) as client,
            client.stream(
                method,
                url,
                headers=_with_identity_encoding(headers),
                params=params,
                **body_kwargs,
            ) as resp,
        ):
            if not resp.is_success:
                logger.warning("%s: %s answered HTTP %s", provider_name, action, resp.status_code)
                return f"Error: {provider_name} answered HTTP {resp.status_code} to {action}."
            encoding = resp.headers.get("content-encoding", "").strip().lower()
            if encoding not in ("", "identity"):
                logger.warning("%s: %s answered compressed (%r)", provider_name, action, encoding[:40])
                return _compressed_answer(provider_name, action)
            too_large = _too_large_answer(provider_name, action, max_bytes)
            if _declared_over(resp.headers, max_bytes):
                return too_large
            async for chunk in resp.aiter_raw():
                raw.extend(chunk)
                if len(raw) > max_bytes:
                    return too_large
            content_type = resp.headers.get("content-type", "")
    except httpx.HTTPError as exc:
        return format_network_error(exc, provider_name, action)

    body = bytes(raw)
    if not _answer_is_text(content_type, body):
        return _unreadable_answer(provider_name, action, f"binary content under {content_type[:80]!r}")
    text = body.decode("utf-8", errors="replace")
    try:
        output = json.dumps(
            _extract_response(json.loads(text), spec.get("response_path")),
            default=str,
            ensure_ascii=False,
        )
    except RecursionError:
        return _unreadable_answer(provider_name, action, "JSON nested past the recursion limit")
    except ValueError:
        output = text[:4000]
    if not output.strip():
        return f"{provider_name} returned an empty response."
    return wrap_untrusted(output, source=f"{provider_name} (third-party content)")


def _build_request_body(
    spec: dict[str, Any], method: str, kwargs: dict[str, Any]
) -> tuple[dict[str, Any], str | None]:
    """``(httpx body kwargs, Content-Type)`` for the request.

    ``json`` is always present in the kwargs (``None`` without a body) so the
    request shape stays uniform. Content-Type only accompanies an actual body:
    a bodyless POST must not carry the header (some endpoints reject it), and
    a multipart body gets its boundary header from httpx.
    """
    body_kwargs: dict[str, Any] = {"json": None}
    if method in _BODYLESS_METHODS:
        return body_kwargs, None

    body: Any = None
    body_from = spec.get("body_from")
    if isinstance(body_from, str) and body_from:
        body = kwargs.get(body_from)
        if body is None and spec.get("body_required") is True:
            raise _SpecValueError(f"Required request body '{body_from}' is missing.")
    else:
        # Bodies for POST/PUT/PATCH/DELETE — DELETE bodies are unusual but
        # real (bulk-delete endpoints); a declared body_mapping must never be
        # silently dropped.
        body_mapping = spec.get("body_mapping")
        if body_mapping == "pass_through":
            # Forward all tool arguments as-is, minus the ones already
            # consumed by URL interpolation.
            path_mapping = _parse_mapping(spec.get("path_mapping")) or {}
            excluded = {
                field_source(p, _parse_field_spec(path_mapping.get(p)))
                for p in spec.get("path_params") or []
            }
            body = {k: v for k, v in kwargs.items() if k not in excluded and v is not None}
        elif body_mapping and body_mapping != "none":
            # A mapping whose every field was omitted still sends ``{}``.
            body = _build_body(body_mapping, kwargs)

    if body is None:
        return body_kwargs, None

    encoding = spec.get("body_encoding") or "json"
    content_type = spec.get("content_type")
    content_type = content_type.strip() if isinstance(content_type, str) else None
    if encoding == "json":
        body_kwargs["json"] = body
        return body_kwargs, content_type or "application/json"
    if encoding == "text":
        body_kwargs["content"] = _scalar_text(body).encode("utf-8")
        return body_kwargs, content_type or "text/plain; charset=utf-8"
    if encoding in ("jsonl", "json-seq"):
        if not isinstance(body, list):
            raise _SpecValueError("The request body must be a list of items.")
        # JSON text sequences (RFC 7464) start each record with RS.
        lead = "\x1e" if encoding == "json-seq" else ""
        lines = "".join(
            f"{lead}{json.dumps(item, ensure_ascii=False, separators=(',', ':'), default=str)}\n"
            for item in body
        )
        body_kwargs["content"] = lines.encode("utf-8")
        default_type = "application/json-seq" if encoding == "json-seq" else "application/jsonl"
        return body_kwargs, content_type or default_type
    if encoding == "binary":
        if not isinstance(body, UploadFile):
            raise _SpecValueError("The request body must be a file.")
        body_kwargs["content"] = body.content
        return body_kwargs, content_type or body.content_type
    if not isinstance(body, dict):
        raise _SpecValueError(
            "The request body must be an object of fields for a form submission."
        )
    if encoding == "form":
        body_kwargs["data"] = _form_fields(body)
        return body_kwargs, content_type or "application/x-www-form-urlencoded"
    # multipart: httpx writes the Content-Type with its generated boundary.
    body_kwargs["files"] = _multipart_parts(body)
    return body_kwargs, None


def make_declarative_handler(
    *,
    spec: dict[str, Any],
    tool_name: str,
    token: str,
    extra_config: dict[str, Any] | None,
    provider_label: str,
    token_provider: TokenProvider | None = None,
    refresh_fn: RefreshFn | None = None,
    enforce_destination_guard: bool = False,
    spec_servers_own: bool | None = None,
    ssl_context: ssl.SSLContext | None = None,
) -> Callable[..., Awaitable[Any]]:
    """Build a tool handler with the spec and credentials closed over.

    The values are captured in a closure — NOT as default keyword parameters —
    so LLM-supplied tool arguments can never override the execution spec, the
    decrypted credential, or the configured ``extra_config`` (which would
    allow redirecting the request, token attached, to an arbitrary URL). The
    single factory for bundle and OpenAPI tools alike.

    The handler returns a :class:`~app.tools.base.ToolResult` flagged with the
    shared failure classifier: integration executors pass a ``ToolResult``
    through as-is, so an ``Error: …`` answer (HTTP 4xx/5xx, network failure)
    is recorded as the failure it is by the trace, the error counters, the
    tool-test endpoint and the connectivity probe.

    ``file_context`` is not a tool argument: the tool runtime injects the
    conversation's files under that name, and file arguments are read from it.
    """
    from app.tools.base import FileContext, ToolResult, is_error_shaped_result

    async def _declarative_handler(file_context: Any = None, **kwargs: Any) -> ToolResult:
        content = await execute_declarative_tool(
            spec,
            kwargs,
            token=token,
            extra_config=extra_config,
            provider_name=provider_label,
            tool_name=tool_name,
            token_provider=token_provider,
            refresh_fn=refresh_fn,
            enforce_destination_guard=enforce_destination_guard,
            spec_servers_own=spec_servers_own,
            file_context=file_context if isinstance(file_context, FileContext) else None,
            ssl_context=ssl_context,
        )
        return ToolResult(
            tool_call_id="",
            content=content,
            is_error=is_error_shaped_result(content),
        )

    return _declarative_handler


def spec_is_read(spec: object) -> bool:
    """Whether an execution spec only reads (GET): its tool offers ``to_workspace_path``.

    The tool's definition then declares ``workspace_ingest``, so a large
    answer (a log, an export) can be written whole to the sandbox workspace
    instead of being cut in the conversation.
    """
    return isinstance(spec, dict) and str(spec.get("method") or "").upper() == "GET"


def make_invalid_spec_handler(
    tool_name: str,
    errors: list[str],
    *,
    source: str = "integration bundle",
    remedy: str = "Re-import the bundle with a corrected spec.",
) -> Callable[..., Awaitable[Any]]:
    """Handler for a tool whose persisted execution spec fails validation.

    Defense-in-depth for rows written before validation existed (or edited
    in the database): the LLM gets a clear configuration error instead of an
    arbitrary runtime crash.
    """
    from app.tools.base import ToolResult

    async def _invalid_spec_handler(**_kwargs: Any) -> ToolResult:
        return ToolResult(
            tool_call_id="",
            content=(
                f"Tool '{tool_name}' has an invalid execution spec in its "
                f"{source}: {'; '.join(errors)}. {remedy}"
            ),
            is_error=True,
        )

    return _invalid_spec_handler


# ---------------------------------------------------------------------------
# Validation (import time)
# ---------------------------------------------------------------------------


def _validate_mapping_entries(
    mapping: dict[str, Any],
    prefix: str,
    errors: list[str],
    *,
    styles: frozenset[str] | None = None,
    constants: str | None = None,
) -> None:
    """Validate the field specs inside a body/query/header/cookie mapping.

    Runtime lookups do ``kwargs.get(field_spec["from"])`` — a non-string
    (or unhashable) ``from`` would crash the tool call with a generic
    exception instead of a clear configuration error, so reject it here.
    A constant (``{"value": ...}``) takes no other key and is allowed only
    where the request builder reads it: *constants* ``"any"`` (the body) or
    ``"scalar"`` (the query string, where it is sent as text).
    """
    for key, field_spec in mapping.items():
        if isinstance(field_spec, dict) and "value" in field_spec:
            if constants is None:
                errors.append(f"{prefix}['{key}'] cannot be a constant 'value' here.")
                continue
            others = sorted(str(k) for k in field_spec if k != "value")
            if others:
                errors.append(
                    f"{prefix}['{key}'] is a constant 'value' and takes no other "
                    f"key (got {others})."
                )
            value = field_spec["value"]
            if constants == "scalar" and (
                value is None or not isinstance(value, (str, int, float, bool))
            ):
                errors.append(
                    f"{prefix}['{key}'].value must be a string, number or boolean."
                )
            continue
        if isinstance(field_spec, dict):
            source = field_spec.get("from")
            if source is not None and not isinstance(source, str):
                errors.append(f"{prefix}['{key}'].from must be a string.")
            required = field_spec.get("required")
            if required is not None and not isinstance(required, bool):
                errors.append(f"{prefix}['{key}'].required must be a boolean.")
            style = field_spec.get("style")
            if style is not None and (styles is None or style not in styles):
                allowed = sorted(styles) if styles else []
                errors.append(
                    f"{prefix}['{key}'].style must be one of {allowed}."
                    if allowed
                    else f"{prefix}['{key}'].style is not supported here."
                )
            explode = field_spec.get("explode")
            if explode is not None and not isinstance(explode, bool):
                errors.append(f"{prefix}['{key}'].explode must be a boolean.")
            is_file = field_spec.get("file")
            if is_file is not None and not isinstance(is_file, bool):
                errors.append(f"{prefix}['{key}'].file must be a boolean.")
            part_type = field_spec.get("content_type")
            if part_type is not None and _safe_media_type(part_type) is None:
                errors.append(f"{prefix}['{key}'].content_type must be a media type.")
        elif field_spec is not None and not isinstance(field_spec, str):
            # None means "map from the same key" at runtime; a string may be
            # a plain source-key name or a double-serialized JSON object.
            errors.append(f"{prefix}['{key}'] must be a string or an object.")


def _validate_named_mapping(
    spec: dict[str, Any],
    key: str,
    prefix: str,
    errors: list[str],
    *,
    styles: frozenset[str],
    reserved: frozenset[str] = frozenset(),
) -> None:
    """Header / cookie mappings: valid token names, none of them reserved."""
    mapping = spec.get(key)
    if mapping is None:
        return
    if not isinstance(mapping, dict):
        errors.append(f"{prefix}.{key} must be an object.")
        return
    for name in mapping:
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            errors.append(
                f"{prefix}.{key} name {name!r} must use RFC 9110 token characters only."
            )
        elif name.lower() in reserved:
            errors.append(
                f"{prefix}.{key} may not set the reserved header '{name}'."
            )
    _validate_mapping_entries(mapping, f"{prefix}.{key}", errors, styles=styles)


def validate_auth_spec(auth: Any, *, prefix: str) -> list[str]:
    """Validate an ``auth`` block (a tool's ``execution.auth``, ``test_connection.auth``).

    Returns a list of error strings (empty = valid).
    """
    if not isinstance(auth, dict):
        return [f"{prefix} must be an object."]
    errors: list[str] = []
    auth_type_raw = auth.get("type")
    if auth_type_raw is not None and not isinstance(auth_type_raw, str):
        # A non-string type would crash .lower() — the validator must
        # report it, never raise.
        errors.append(f"{prefix}.type must be one of {sorted(_AUTH_TYPES)}.")
        auth_type_raw = None
    auth_type = (auth_type_raw or "").lower()
    if auth_type and auth_type not in _AUTH_TYPES:
        errors.append(f"{prefix}.type must be one of {sorted(_AUTH_TYPES)}.")
    if auth_type == "header":
        header_name = auth.get("header")
        header_prefix = auth.get("prefix")
        if header_name is not None and (
            not isinstance(header_name, str)
            or not _HEADER_NAME_RE.fullmatch(header_name)
        ):
            # httpx raises a bare ValueError for invalid header names
            # at request time — reject them upfront instead.
            errors.append(
                f"{prefix}.header must be a valid HTTP header "
                f"name (RFC 9110 token characters only)."
            )
        elif header_name is not None and not credential_header_allowed(header_name):
            errors.append(
                f"{prefix}.header may not be the reserved header '{header_name}'."
            )
        if header_prefix is not None and (
            not isinstance(header_prefix, str)
            or not _HEADER_VALUE_SAFE_RE.fullmatch(header_prefix)
        ):
            errors.append(
                f"{prefix}.prefix must be a printable-ASCII "
                f"string (no control characters)."
            )
    for kind, field_name in (("query", "param"), ("cookie", "cookie")):
        if auth_type != kind:
            continue
        name = auth.get(field_name)
        if not isinstance(name, str) or not _HEADER_NAME_RE.fullmatch(name):
            errors.append(
                f"{prefix}.{field_name} must be a non-empty name "
                f"(RFC 9110 token characters only)."
            )
    return errors


def _validate_body(spec: dict[str, Any], method: Any, prefix: str, errors: list[str]) -> None:
    bodyless = method.upper() if isinstance(method, str) and method.upper() in _BODYLESS_METHODS else None

    # body_mapping — dict for explicit mapping, or "pass_through" / "none"
    body_mapping = spec.get("body_mapping")
    if body_mapping is not None:
        # GET and HEAD requests never carry a body at runtime — a declared
        # body_mapping would be silently ignored, so reject it upfront.
        if bodyless:
            errors.append(
                f"{prefix}.body_mapping is not supported for {bodyless} requests "
                f"(use query_mapping instead)."
            )
        if isinstance(body_mapping, str):
            if body_mapping not in ("pass_through", "none"):
                errors.append(
                    f"{prefix}.body_mapping must be an object, \"pass_through\", or \"none\"."
                )
        elif isinstance(body_mapping, dict):
            _validate_mapping_entries(
                body_mapping, f"{prefix}.body_mapping", errors, constants="any"
            )
        else:
            errors.append(
                f"{prefix}.body_mapping must be an object, \"pass_through\", or \"none\"."
            )

    body_from = spec.get("body_from")
    if body_from is not None:
        if not isinstance(body_from, str) or not body_from.strip():
            errors.append(f"{prefix}.body_from must be a non-empty string.")
        if body_mapping is not None:
            errors.append(f"{prefix}.body_from and body_mapping are mutually exclusive.")
        if bodyless:
            errors.append(f"{prefix}.body_from is not supported for {bodyless} requests.")

    body_required = spec.get("body_required")
    if body_required is not None and not isinstance(body_required, bool):
        errors.append(f"{prefix}.body_required must be a boolean.")

    encoding = spec.get("body_encoding")
    if encoding is not None and encoding not in _BODY_ENCODINGS:
        errors.append(f"{prefix}.body_encoding must be one of {sorted(_BODY_ENCODINGS)}.")
        encoding = None
    encoding = encoding or "json"

    if encoding == "binary" and not (isinstance(body_from, str) and body_from.strip()):
        errors.append(f"{prefix}.body_encoding 'binary' sends one file argument: set body_from.")
    if encoding in ("jsonl", "json-seq") and not (isinstance(body_from, str) and body_from.strip()):
        errors.append(f"{prefix}.body_encoding '{encoding}' sends one list argument: set body_from.")
    if isinstance(body_mapping, dict) and encoding != "multipart":
        files = [
            key
            for key, field_spec in body_mapping.items()
            if isinstance(field_spec, dict) and field_spec.get("file") is True
        ]
        if files:
            errors.append(
                f"{prefix}.body_mapping file fields {sorted(files)} need body_encoding 'multipart'."
            )

    # content_type must describe what the encoding actually sends.
    content_type = spec.get("content_type")
    if content_type is None:
        return
    if not isinstance(content_type, str) or not _HEADER_VALUE_SAFE_RE.fullmatch(content_type):
        kind = "JSON media type" if encoding == "json" else "media type"
        errors.append(
            f"{prefix}.content_type must be a printable-ASCII {kind} (no control characters)."
        )
        return
    lowered = content_type.lower()
    if encoding == "json" and "json" not in lowered:
        errors.append(
            f"{prefix}.content_type must be a printable-ASCII JSON media type (no control characters)."
        )
    elif encoding == "form" and not lowered.startswith("application/x-www-form-urlencoded"):
        errors.append(
            f"{prefix}.content_type must be application/x-www-form-urlencoded for a form body."
        )
    elif encoding == "multipart" and not lowered.startswith("multipart/form-data"):
        errors.append(f"{prefix}.content_type must be multipart/form-data for a multipart body.")
    elif encoding == "text" and not lowered.startswith("text/"):
        errors.append(f"{prefix}.content_type must be a text/* media type for a text body.")
    elif encoding in ("binary", "jsonl", "json-seq") and _safe_media_type(content_type) is None:
        errors.append(f"{prefix}.content_type must be a media type for a {encoding} body.")


def validate_execution_spec(spec: Any, *, tool_index: int) -> list[str]:
    """Validate an execution spec from a bundle's tool declaration.

    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    prefix = f"tools[{tool_index}].execution"

    if not isinstance(spec, dict):
        errors.append(f"{prefix} must be an object.")
        return errors

    # method — any RFC 9110 token, except the methods never sent
    method = spec.get("method")
    if (
        not isinstance(method, str)
        or not _HEADER_NAME_RE.fullmatch(method)
        or method.upper() in _REFUSED_METHODS
    ):
        errors.append(
            f"{prefix}.method must be an HTTP method (GET, POST, PUT, PATCH, DELETE, "
            "HEAD, OPTIONS, QUERY or another token), never TRACE or CONNECT."
        )

    # path
    path = spec.get("path")
    if not isinstance(path, str) or not path.strip().startswith("/"):
        errors.append(f"{prefix}.path must be a string starting with '/'.")

    # path_params
    path_params = spec.get("path_params")
    if path_params is not None:
        if not isinstance(path_params, list):
            errors.append(f"{prefix}.path_params must be an array of strings.")
        elif not all(isinstance(p, str) and p.strip() for p in path_params):
            errors.append(f"{prefix}.path_params entries must be non-empty strings.")

    # path <-> path_params consistency: an undeclared {placeholder} is never
    # interpolated (the literal braces would be sent to the server), and a
    # declared param absent from the path forces the LLM to supply an
    # argument that is silently dropped.
    if isinstance(path, str) and (
        path_params is None
        or (
            isinstance(path_params, list)
            and all(isinstance(p, str) and p.strip() for p in path_params)
        )
    ):
        placeholders = set(re.findall(r"\{([^{}/]+)\}", path))
        # Compare the exact declared names — runtime interpolation matches
        # ``{param}`` literally, so a padded name would fail there too.
        declared = set(path_params or [])
        undeclared = placeholders - declared
        if undeclared:
            errors.append(
                f"{prefix}.path placeholders must be declared in path_params: "
                f"{sorted(undeclared)}."
            )
        unused = declared - placeholders
        if unused:
            errors.append(
                f"{prefix}.path_params entries must appear as {{placeholders}} "
                f"in path: {sorted(unused)}."
            )

    # path_mapping — only for declared path params
    path_mapping = spec.get("path_mapping")
    if path_mapping is not None:
        if not isinstance(path_mapping, dict):
            errors.append(f"{prefix}.path_mapping must be an object.")
        else:
            unknown = set(path_mapping) - set(path_params or [])
            if unknown:
                errors.append(
                    f"{prefix}.path_mapping keys must be declared in path_params: "
                    f"{sorted(unknown)}."
                )
            _validate_mapping_entries(
                path_mapping, f"{prefix}.path_mapping", errors, styles=_PATH_STYLES
            )

    _validate_body(spec, method, prefix, errors)

    # query_mapping
    query_mapping = spec.get("query_mapping")
    if query_mapping is not None:
        if not isinstance(query_mapping, dict):
            errors.append(f"{prefix}.query_mapping must be an object.")
        else:
            _validate_mapping_entries(
                query_mapping,
                f"{prefix}.query_mapping",
                errors,
                styles=_QUERY_STYLES,
                constants="scalar",
            )

    # querystring — one argument, the whole query string
    querystring = spec.get("querystring")
    if querystring is not None:
        source = querystring.get("from") if isinstance(querystring, dict) else None
        media_type = querystring.get("media_type") if isinstance(querystring, dict) else None
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{prefix}.querystring must be an object naming its argument in 'from'.")
        if media_type is not None and (
            not isinstance(media_type, str) or _safe_media_type(media_type) is None
        ):
            errors.append(f"{prefix}.querystring.media_type must be a media type.")

    _validate_named_mapping(
        spec,
        "header_mapping",
        prefix,
        errors,
        styles=_HEADER_STYLES,
        reserved=_RESERVED_HEADERS,
    )
    _validate_named_mapping(spec, "cookie_mapping", prefix, errors, styles=_COOKIE_STYLES)

    # auth
    auth = spec.get("auth")
    if auth is not None:
        errors.extend(validate_auth_spec(auth, prefix=f"{prefix}.auth"))
    # base_url_key
    base_url_key = spec.get("base_url_key")
    if base_url_key is not None and (
        not isinstance(base_url_key, str) or not base_url_key.strip()
    ):
        errors.append(f"{prefix}.base_url_key must be a non-empty string.")

    # server_url — an absolute http(s) URL
    server_url = spec.get("server_url")
    if server_url is not None:
        try:
            parsed = urlparse(server_url) if isinstance(server_url, str) else None
            _ = parsed.port if parsed is not None else None
        except ValueError:
            parsed = None
        if (
            parsed is None
            or parsed.scheme not in ("http", "https")
            or not parsed.netloc
            or not _HEADER_VALUE_SAFE_RE.fullmatch(server_url)
        ):
            errors.append(f"{prefix}.server_url must be an absolute http(s) URL.")

    accept = spec.get("accept")
    if accept is not None and (
        not isinstance(accept, str) or not _HEADER_VALUE_SAFE_RE.fullmatch(accept)
    ):
        errors.append(f"{prefix}.accept must be a printable-ASCII media type.")

    # response_path
    response_path = spec.get("response_path")
    if response_path is not None and not isinstance(response_path, str):
        errors.append(f"{prefix}.response_path must be a string.")

    # timeout_ms
    timeout_ms = spec.get("timeout_ms")
    if timeout_ms is not None and (
        not isinstance(timeout_ms, int | float) or isinstance(timeout_ms, bool) or timeout_ms <= 0
    ):
        errors.append(f"{prefix}.timeout_ms must be a positive number.")

    # untrusted_response / max_response_bytes
    untrusted = spec.get("untrusted_response")
    if untrusted is not None and not isinstance(untrusted, bool):
        errors.append(f"{prefix}.untrusted_response must be true or false.")
    max_response_bytes = spec.get("max_response_bytes")
    if max_response_bytes is not None and (
        not isinstance(max_response_bytes, int)
        or isinstance(max_response_bytes, bool)
        or not 0 < max_response_bytes <= _MAX_UNTRUSTED_MAX_BYTES
    ):
        errors.append(
            f"{prefix}.max_response_bytes must be a whole number of bytes, "
            f"at most {_MAX_UNTRUSTED_MAX_BYTES}."
        )

    return errors
