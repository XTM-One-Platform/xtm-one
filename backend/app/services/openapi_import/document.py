# SPDX-License-Identifier: Apache-2.0

"""Load a Swagger / OpenAPI document — JSON or YAML, from text or a URL.

Everything here treats the document as hostile input:

* **Bounded.** At most :data:`MAX_SPEC_BYTES` of text (the integration bundle
  ceiling), enforced before parsing — and, for a URL, while streaming.
* **JSON first, YAML second.** JSON is parsed exactly (``NaN`` / ``Infinity``
  refused). Anything else is parsed as YAML with a *safe* loader, so no tag
  can construct an arbitrary object.
* **YAML read the way a spec author means it.** PyYAML implements YAML 1.1,
  under which an unquoted ``200:`` response key becomes an ``int``, ``on:`` /
  ``yes:`` become booleans and ``2024-01-01`` becomes a ``date``. The loader
  keeps every mapping key as its literal text and every timestamp as a string,
  so ``responses: {200: ...}`` stays the ``"200"`` the spec defines.
* **No alias bombs.** YAML anchors let a few hundred bytes describe billions
  of nodes. The loaded structure is measured as a DAG (shared aliases counted
  once per reference, with memoisation — linear in the unique nodes) and
  refused past :data:`_MAX_EXPANDED_NODES`, before anything walks it.
* **JSON-shaped result.** Only dict / list / str / int / finite float / bool /
  None survive; a ``!!binary`` or ``!!set`` value is refused with a message.

Parsing is CPU-bound, so the async entry points run it on the shared parse
executor (``app.core.parse_offload``) instead of the event loop.
"""

from __future__ import annotations

import codecs
import functools
import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
import yaml

from app.core.parse_offload import run_parse

#: Same ceiling as an integration bundle upload.
MAX_SPEC_BYTES = 10 * 1024 * 1024

#: Expanded-node ceiling (aliases counted per reference). Generous for the
#: largest public specs (a few hundred thousand nodes), fatal for bombs.
_MAX_EXPANDED_NODES = 5_000_000
#: Nesting ceiling; real specs stay far below it.
_MAX_DEPTH = 200

#: ``Accept`` sent when fetching a spec URL: every media type an OpenAPI
#: document is served as, JSON first.
SPEC_ACCEPT_HEADER = (
    "application/json, application/vnd.oai.openapi+json;q=0.9, "
    "application/yaml;q=0.9, application/vnd.oai.openapi;q=0.9, "
    "application/x-yaml;q=0.9, text/yaml;q=0.9, text/plain;q=0.5, */*;q=0.1"
)

_FETCH_TIMEOUT_SECONDS = 30

# BOM → codec, UTF-32 first: its little-endian BOM starts with UTF-16's.
_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
)

_HTML_START_RE = re.compile(r"^\s*(<!doctype\s+html|<html[\s>])", re.IGNORECASE)
_OPENAPI_VERSION_RE = re.compile(
    r"^3\.(\d{1,4})(?:\.\d{1,4})?(?:-[0-9A-Za-z.-]{1,64})?$", re.ASCII
)
#: What a URL fetch that reached nothing says, whatever stopped it (refused,
#: reset, filtered, a name that does not resolve): the reason would map the
#: hosts and ports of the networks the platform can reach.
UNREACHABLE_MESSAGE = "The URL could not be reached."


class OpenAPIImportError(ValueError):
    """A document that cannot be imported. The message is safe to show the user."""


@dataclass(frozen=True)
class SpecVersion:
    """Which specification a document follows.

    ``family`` is the dialect the converter implements: ``"2.0"`` (Swagger),
    ``"3.0"`` or ``"3.1"`` (3.1 and every later 3.x minor, which only add to
    it). ``raw`` is the version string exactly as the document states it.
    """

    family: str
    raw: str

    @property
    def label(self) -> str:
        return f"Swagger {self.raw}" if self.family == "2.0" else f"OpenAPI {self.raw}"


@dataclass(frozen=True)
class SpecDocument:
    """A parsed document plus what is known about where it came from."""

    data: dict[str, Any]
    format: str  # "json" | "yaml"
    version: SpecVersion
    sha256: str
    size_bytes: int
    #: Final URL after redirects when fetched; the base for relative URLs.
    source_url: str | None = None


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

_BaseLoader: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


class _SpecLoader(_BaseLoader):  # type: ignore[misc, valid-type]
    """Safe YAML loader that keeps keys and timestamps as their literal text."""


# Timestamps stay strings: ``info.version: 2024-01-01`` must not become a date.
_SpecLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in _BaseLoader.yaml_implicit_resolvers.items()
}


def _construct_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[str, Any]:
    """Build a mapping whose keys are the literal scalar text of the source.

    ``200:`` stays ``"200"``, ``on:`` stays ``"on"``. Merge keys (``<<``) are
    honoured. A non-scalar key (a YAML complex key) has no JSON equivalent and
    is refused.
    """
    loader.flatten_mapping(node)
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        if not isinstance(key_node, yaml.ScalarNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                "complex mapping keys are not supported",
                key_node.start_mark,
            )
        mapping[key_node.value] = loader.construct_object(value_node, deep=True)
    return mapping


_SpecLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def decode_spec_bytes(raw: bytes) -> str:
    """Decode document bytes: BOM-announced UTF-8/16/32, else strict UTF-8."""
    for bom, codec in _BOMS:
        if raw.startswith(bom):
            try:
                return raw[len(bom) :].decode(codec)
            except UnicodeDecodeError as exc:
                raise OpenAPIImportError(
                    f"The document is not valid {codec.upper()} text."
                ) from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OpenAPIImportError("The document is not valid UTF-8 text.") from exc


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a valid JSON value")


def _json_error(exc: json.JSONDecodeError) -> str:
    return f"Invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}."


def _yaml_error(exc: yaml.YAMLError) -> str:
    mark = getattr(exc, "problem_mark", None)
    problem = getattr(exc, "problem", None) or str(exc).splitlines()[0]
    if mark is not None:
        return f"Invalid YAML at line {mark.line + 1}, column {mark.column + 1}: {problem}."
    return f"Invalid YAML: {problem}."


def _check_tree(root: Any) -> None:
    """Refuse non-JSON values, cycles, excessive depth and alias bombs.

    Iterative post-order walk with a memo keyed by container identity: a
    container shared through YAML aliases is measured once and its size
    reused for every further reference, so the cost is linear in the unique
    nodes while the total counts the EXPANDED document — what every later
    walk would actually traverse.
    """
    sizes: dict[int, int] = {}
    on_path: set[int] = set()
    stack: list[tuple[Any, int, bool]] = [(root, 0, False)]
    while stack:
        node, depth, done = stack.pop()
        if not isinstance(node, dict | list):
            _check_scalar(node)
            continue
        node_id = id(node)
        children = list(node.values()) if isinstance(node, dict) else node
        if done:
            total = 1
            for child in children:
                total += sizes.get(id(child), 1) if isinstance(child, dict | list) else 1
            if total > _MAX_EXPANDED_NODES:
                raise OpenAPIImportError(
                    "The document expands to too many nodes (YAML aliases?) to be imported."
                )
            sizes[node_id] = total
            on_path.discard(node_id)
            continue
        if node_id in sizes:
            continue
        if node_id in on_path:
            raise OpenAPIImportError("The document contains a recursive YAML alias.")
        if depth > _MAX_DEPTH:
            raise OpenAPIImportError("The document is nested too deeply to be imported.")
        if isinstance(node, dict) and not all(isinstance(k, str) for k in node):
            raise OpenAPIImportError("Every mapping key in the document must be a string.")
        on_path.add(node_id)
        stack.append((node, depth, True))
        for child in children:
            stack.append((child, depth + 1, False))


def _check_scalar(value: Any) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OpenAPIImportError("The document contains a non-finite number (NaN or Infinity).")
        return
    raise OpenAPIImportError(
        f"The document contains a value of an unsupported YAML type ({type(value).__name__})."
    )


def parse_spec_text(text: str) -> tuple[dict[str, Any], str]:
    """Parse document text as JSON, falling back to YAML.

    Returns ``(data, format)``. When both parsers fail, the error of the
    format the text most looks like is reported (JSON for ``{`` / ``[``).
    """
    stripped = text.lstrip("﻿").strip()
    if not stripped:
        raise OpenAPIImportError("The document is empty.")
    if _HTML_START_RE.match(stripped):
        raise OpenAPIImportError(
            "This is an HTML page, not an OpenAPI document. Point to the raw "
            "swagger.json / openapi.yaml file instead — Swagger UI pages load it "
            "from a URL such as …/swagger.json, …/openapi.json or …/v3/api-docs."
        )

    json_error: str | None = None
    try:
        data: Any = json.loads(stripped, parse_constant=_reject_constant)
        fmt = "json"
    except json.JSONDecodeError as exc:
        json_error = _json_error(exc)
    except ValueError as exc:
        json_error = f"Invalid JSON: {exc}."
    except RecursionError:
        raise OpenAPIImportError("The document is nested too deeply to be imported.") from None

    if json_error is not None:
        try:
            # The unstripped text: YAML is indentation-sensitive, and trimming
            # the first line alone would change its nesting.
            data = yaml.load(text.lstrip("﻿"), Loader=_SpecLoader)  # noqa: S506 - safe loader subclass
            fmt = "yaml"
        except yaml.YAMLError as exc:
            looks_like_json = stripped[0] in "{["
            raise OpenAPIImportError(json_error if looks_like_json else _yaml_error(exc)) from None
        except RecursionError:
            raise OpenAPIImportError("The document is nested too deeply to be imported.") from None
        except ValueError:
            # A constructor refusing a value: an integer past Python's digit
            # limit, a malformed base-60 or binary literal.
            raise OpenAPIImportError("The document holds a value that cannot be read (a number too large?).") from None

    if not isinstance(data, dict):
        raise OpenAPIImportError(
            "The document must be a JSON or YAML object with 'openapi' or 'swagger' at its root."
        )
    _check_tree(data)
    return data, fmt


def detect_spec_version(data: dict[str, Any]) -> SpecVersion:
    """Identify Swagger 2.0 / OpenAPI 3.x, refusing anything else with a reason."""
    if "swagger" in data:
        raw = str(data.get("swagger")).strip()
        if raw in ("2.0", "2"):
            return SpecVersion(family="2.0", raw="2.0")
        raise OpenAPIImportError(
            f"Swagger {raw} is not supported. Supported versions: Swagger 2.0 and OpenAPI 3.x."
        )
    if "openapi" in data:
        raw = str(data.get("openapi")).strip()
        match = _OPENAPI_VERSION_RE.match(raw)
        if not match:
            raise OpenAPIImportError(
                f"OpenAPI {raw} is not supported. Supported versions: Swagger 2.0 and OpenAPI 3.x."
            )
        minor = int(match.group(1))
        return SpecVersion(family="3.0" if minor == 0 else "3.1", raw=raw)
    if "swaggerVersion" in data:
        raise OpenAPIImportError(
            "Swagger 1.x documents are not supported. Convert the API description "
            "to Swagger 2.0 or OpenAPI 3.x first."
        )
    raise OpenAPIImportError(
        "This is not an OpenAPI or Swagger document: the root has no 'openapi' "
        "or 'swagger' field."
    )


def parse_spec_document(raw: bytes | str, source_url: str | None = None) -> SpecDocument:
    """Parse a whole document (synchronous; use the async helpers from routes)."""
    if isinstance(raw, bytes):
        size = len(raw)
        if size > MAX_SPEC_BYTES:
            raise OpenAPIImportError(_too_large())
        text = decode_spec_bytes(raw)
        digest_source = raw
    else:
        text = raw
        digest_source = raw.encode("utf-8")
        size = len(digest_source)
        if size > MAX_SPEC_BYTES:
            raise OpenAPIImportError(_too_large())
    data, fmt = parse_spec_text(text)
    version = detect_spec_version(data)
    return SpecDocument(
        data=data,
        format=fmt,
        version=version,
        sha256=hashlib.sha256(digest_source).hexdigest(),
        size_bytes=size,
        source_url=source_url,
    )


def _too_large() -> str:
    return f"The document exceeds the maximum size of {MAX_SPEC_BYTES // (1024 * 1024)} MB."


# ---------------------------------------------------------------------------
# Async entry points
# ---------------------------------------------------------------------------


def normalize_spec_url(url: str) -> str:
    """Trim and check a spec URL's shape (the network policy is applied on fetch)."""
    url = (url or "").strip()
    try:
        parsed = urlparse(url)
        _ = parsed.port
    except ValueError:
        raise OpenAPIImportError("The URL is not a valid URL.") from None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise OpenAPIImportError("The URL must be an absolute http:// or https:// URL.")
    if parsed.username or parsed.password:
        raise OpenAPIImportError(
            "Credentials inside the URL are not supported. Download the document "
            "and upload the file instead."
        )
    return url


async def fetch_spec_document(url: str) -> SpecDocument:
    """Fetch and parse a spec URL.

    Public and private-network hosts are reachable; loopback, link-local and
    cloud-metadata addresses are refused, on the first request and on every
    redirect hop (``fetch_url_bytes`` with ``allow_private=True``).
    """
    from app.services.web_fetch import DestinationUnresolvedError, fetch_url_bytes

    url = normalize_spec_url(url)
    try:
        fetched = await fetch_url_bytes(
            url,
            max_bytes=MAX_SPEC_BYTES,
            timeout=_FETCH_TIMEOUT_SECONDS,
            allow_private=True,
            accept=SPEC_ACCEPT_HEADER,
        )
    except DestinationUnresolvedError:
        raise OpenAPIImportError(UNREACHABLE_MESSAGE) from None
    except ValueError as exc:
        raise OpenAPIImportError(f"The URL cannot be fetched: {exc}") from None
    except httpx.HTTPStatusError as exc:
        raise OpenAPIImportError(
            f"The server answered HTTP {exc.response.status_code} for this URL."
        ) from None
    except httpx.HTTPError:
        raise OpenAPIImportError(UNREACHABLE_MESSAGE) from None
    return await run_parse(functools.partial(parse_spec_document, fetched.content, fetched.url))


async def load_spec_source(
    *,
    url: str | None = None,
    content: str | None = None,
) -> SpecDocument:
    """Load a document from exactly one of *url* or *content* (the file's text)."""
    has_url = bool((url or "").strip())
    has_content = bool((content or "").strip())
    if has_url == has_content:
        raise OpenAPIImportError("Provide either a spec URL or the spec file content.")
    if has_url:
        return await fetch_spec_document(url or "")
    return await run_parse(functools.partial(parse_spec_document, content or ""))
