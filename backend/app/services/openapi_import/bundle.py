# SPDX-License-Identifier: Apache-2.0

"""References to other documents, bundled into one self-contained document.

A spec may be split across files — ``$ref: ./schemas/pet.yaml`` or
``$ref: https://example.com/common.yaml#/components/schemas/Error``. The
conversion (``schema.py``) follows references inside ONE document, so
:func:`bundle_external_refs` first gathers every referenced document and
rewrites each reference to point inside the root document:

* each external document is embedded under ``x-bundled`` at the root, keyed
  ``d1``, ``d2``…: ``other.yaml#/components/schemas/Pet`` becomes
  ``#/x-bundled/d1/components/schemas/Pet``;
* references inside an embedded document are rewritten the same way,
  relative to THAT document — its own ``#/...`` pointers included — so a
  document referenced from several places is embedded once, and references
  between documents (cycles included) become references inside the bundle.

A reference is resolved against the base URI of the document holding it:
its ``$self`` (OpenAPI 3.2) when it declares one, else where it was loaded
from — the spec URL, or its path inside an uploaded ZIP archive
(:func:`read_spec_archive`). A single uploaded file has no location, so only
absolute references (and relative ones under a ``$self``) can be followed
from it. In an archive, a document is also found by its ``$self``, and the
root's ``$self`` directory stands for the archive's root directory: an
archive is the spec's files as they are published.

Documents come from the network through the destination guard (like the
spec itself) or from the archive, within :data:`MAX_EXTERNAL_DOCUMENTS` and
:data:`MAX_BUNDLE_BYTES`. A reference that cannot be followed is left as it
is — the conversion reports it like any unresolved reference — and the
reason is returned as a warning. Values that are data, not definitions
(``example``, ``examples``, ``default``, ``enum``, ``const`` and ``x-``
extensions), are never searched for references.
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import hashlib
import io
import logging
import posixpath
import re
import zipfile
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urldefrag, urljoin, urlparse

import httpx

from app.core.archive_reading import ArchiveBudget, ArchiveTooLargeError
from app.core.parse_offload import run_parse
from app.services.openapi_import.document import (
    MAX_SPEC_BYTES,
    SPEC_ACCEPT_HEADER,
    OpenAPIImportError,
    SpecDocument,
    decode_spec_bytes,
    parse_spec_document,
    parse_spec_text,
)

logger = logging.getLogger(__name__)

BUNDLE_KEY = "x-bundled"
MAX_EXTERNAL_DOCUMENTS = 200
#: Everything bundled together: the root document and what it references.
MAX_BUNDLE_BYTES = 30 * 1024 * 1024
#: An uploaded archive, compressed.
MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
_MAX_ARCHIVE_ENTRIES = 1000
_FETCH_CONCURRENCY = 6
_FETCH_TIMEOUT_SECONDS = 20
_MAX_ISSUES_LISTED = 5
UNREACHABLE_REASON = "it could not be reached"

#: Stands for "inside the uploaded archive": a real-looking http base, so
#: ``urljoin`` resolves relative references with its usual rules. Never
#: fetched (``.invalid`` is reserved, RFC 2606).
ARCHIVE_ORIGIN = "https://archive.invalid/"

_SPEC_SUFFIXES = (".json", ".yaml", ".yml")
_ROOT_NAMES = ("openapi", "swagger", "api", "spec", "index", "main", "root")
_ROOT_HINT_RE = re.compile(rb"""(^|\n|[{,]\s*)["']?(openapi|swagger)["']?\s*:""")
#: Keys whose values are data, never definitions to search for references.
_LITERAL_KEYS = frozenset({"example", "examples", "default", "enum", "const"})
#: Keys whose values map NAMES to definitions: every entry is searched, even
#: one named like a keyword (a property called "default" is a schema).
_NAME_MAP_KEYS = frozenset(
    {
        "properties",
        "patternProperties",
        "dependentSchemas",
        "$defs",
        "definitions",
        "schemas",
        "parameters",
        "responses",
        "requestBodies",
        "headers",
        "securitySchemes",
        "securityDefinitions",
        "links",
        "callbacks",
        "pathItems",
        "paths",
        "webhooks",
        "mediaTypes",
        "encoding",
        "content",
    }
)


# ---------------------------------------------------------------------------
# Archives
# ---------------------------------------------------------------------------


@dataclass
class SpecArchive:
    """An uploaded ZIP archive: its spec files by path, and the root document."""

    files: dict[str, bytes]
    root: str
    #: ``$self`` → path, filled the first time a reference needs it.
    by_self: dict[str, str] | None = None

    def url(self, path: str) -> str:
        return ARCHIVE_ORIGIN + path

    def path(self, url: str) -> str | None:
        """The member *url* points at, when it is inside the archive."""
        if not url.startswith(ARCHIVE_ORIGIN):
            return None
        return posixpath.normpath(unquote(url[len(ARCHIVE_ORIGIN):]))


def _safe_member(name: str) -> str | None:
    """A normalised relative path, or None for anything that is not a spec file."""
    name = name.replace("\\", "/")
    if name.endswith("/") or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    normalised = posixpath.normpath(name)
    parts = normalised.split("/")
    if normalised.startswith("..") or any(p in ("", "..") for p in parts):
        return None
    if parts[0] == "__MACOSX" or any(p.startswith(".") for p in parts):
        return None
    if not normalised.lower().endswith(_SPEC_SUFFIXES):
        return None
    return normalised


def read_spec_archive(raw: bytes) -> SpecArchive:
    """Open an uploaded ZIP archive of spec files and find its root document.

    Bounded before anything is decompressed (entry count, declared sizes)
    and while it is (a member larger than it claims is refused). The root is
    the shallowest document with ``openapi`` or ``swagger`` at its root,
    preferring conventional names (``openapi.yaml``, ``swagger.json``…).
    """
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise OpenAPIImportError(
            f"The archive exceeds the maximum size of {MAX_ARCHIVE_BYTES // (1024 * 1024)} MB."
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise OpenAPIImportError("The file is not a valid ZIP archive.") from None
    with archive:
        entries = archive.infolist()
        if len(entries) > _MAX_ARCHIVE_ENTRIES:
            raise OpenAPIImportError(f"The archive holds more than {_MAX_ARCHIVE_ENTRIES} files.")
        files: dict[str, bytes] = {}
        # Declared sizes can lie: what a member expands to is what counts.
        budget = ArchiveBudget(MAX_BUNDLE_BYTES)
        for info in entries:
            path = _safe_member(info.filename)
            if path is None:
                continue
            if info.flag_bits & 0x1:
                raise OpenAPIImportError("Encrypted archives are not supported.")
            if info.file_size > MAX_SPEC_BYTES:
                raise OpenAPIImportError(f"'{path}' in the archive is larger than the document size limit.")
            try:
                content = budget.read(archive, info, max_member=MAX_SPEC_BYTES)
            except ArchiveTooLargeError:
                if budget.remaining >= MAX_SPEC_BYTES:
                    raise OpenAPIImportError(
                        f"'{path}' in the archive is larger than the document size limit."
                    ) from None
                raise OpenAPIImportError("The archive's documents are too large to import together.") from None
            except (zipfile.BadZipFile, OSError):
                raise OpenAPIImportError(f"'{path}' cannot be read from the archive.") from None
            files[path] = content
    if not files:
        raise OpenAPIImportError("The archive holds no .json, .yaml or .yml document.")
    return SpecArchive(files=files, root=_archive_root(files))


def _archive_root(files: dict[str, bytes]) -> str:
    candidates = [path for path, content in files.items() if _ROOT_HINT_RE.search(content[:65536])]
    if not candidates:
        raise OpenAPIImportError(
            "No document in the archive has 'openapi' or 'swagger' at its root."
        )

    def rank(path: str) -> tuple[int, int, str]:
        stem = posixpath.splitext(posixpath.basename(path))[0].lower()
        named = next((i for i, n in enumerate(_ROOT_NAMES) if stem == n), len(_ROOT_NAMES))
        return path.count("/"), named, path

    ranked = sorted(candidates, key=rank)
    best = rank(ranked[0])
    tied = [p for p in ranked if rank(p)[:2] == best[:2]]
    if len(tied) > 1 and best[1] == len(_ROOT_NAMES):
        raise OpenAPIImportError(
            "The archive holds several API documents ("
            + ", ".join(tied[:_MAX_ISSUES_LISTED])
            + "): keep one, or name the root openapi.yaml."
        )
    return ranked[0]


async def load_spec_archive(raw: bytes) -> tuple[SpecDocument, SpecArchive]:
    """Parse an uploaded archive's root document (bundled by :func:`bundle_external_refs`)."""
    archive = await run_parse(functools.partial(read_spec_archive, raw))
    document = await run_parse(functools.partial(parse_spec_document, archive.files[archive.root]))
    return document, archive


# ---------------------------------------------------------------------------
# Bundling
# ---------------------------------------------------------------------------


@dataclass
class _Document:
    key: str | None  # None for the root
    #: Where it was loaded from (without fragment) — how references name it.
    url: str | None
    data: Any
    #: What its relative references resolve against.
    base: str | None
    size: int = 0
    sha256: str = ""


@dataclass
class _Run:
    archive: SpecArchive | None
    #: ``(published directory URL, archive directory URL)`` pairs: where the
    #: root's ``$self`` says the files live, and where they are in the archive.
    mounts: list[tuple[str, str]] = field(default_factory=list)
    documents: dict[str, _Document] = field(default_factory=dict)
    #: URLs naming the root document itself.
    root_urls: set[str] = field(default_factory=set)
    failures: dict[str, str] = field(default_factory=dict)
    unreachable: list[str] = field(default_factory=list)
    total_bytes: int = 0
    counter: int = 0


def self_base(data: Any, location: str | None) -> str | None:
    """A document's base URI: its ``$self`` resolved against *location*, else *location*."""
    declared = data.get("$self") if isinstance(data, dict) else None
    if isinstance(declared, str) and declared.strip():
        resolved = urljoin(location, declared.strip()) if location else declared.strip()
        parsed = urlparse(resolved)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return urldefrag(resolved)[0]
    return location


def document_base_uri(document: SpecDocument) -> str | None:
    """The base URI of a loaded spec for its relative URLs (servers, OAuth endpoints)."""
    base = self_base(document.data, document.source_url)
    return base if base and not base.startswith(ARCHIVE_ORIGIN) else document.source_url


def _ref_sites(data: Any) -> list[dict[str, Any]]:
    """Every mapping holding a string ``$ref`` (each shared node once)."""
    sites: list[dict[str, Any]] = []
    seen: set[int] = set()
    stack: list[tuple[Any, str | None]] = [(data, None)]
    while stack:
        node, parent_key = stack.pop()
        if isinstance(node, list):
            if id(node) in seen:
                continue
            seen.add(id(node))
            stack.extend((item, None) for item in node)
            continue
        if not isinstance(node, dict) or id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node.get("$ref"), str):
            sites.append(node)
        names = parent_key in _NAME_MAP_KEYS
        for key, value in node.items():
            if not names and (key in _LITERAL_KEYS or key.startswith("x-") or key == "$ref"):
                continue
            if isinstance(value, dict | list):
                stack.append((value, None if names else key))
    return sites


def _target(ref: str, base: str | None) -> tuple[str | None, str]:
    """``(document URL or None for the same document, fragment)`` of *ref*."""
    if ref.startswith("#"):
        return None, ref[1:]
    parsed = urlparse(ref)
    if parsed.scheme and parsed.scheme not in ("http", "https"):
        raise ValueError(f"'{parsed.scheme}:' references are not followed")
    if base:
        resolved = urljoin(base, ref)
    elif parsed.scheme in ("http", "https") and parsed.netloc:
        resolved = ref
    else:
        raise LookupError("relative")
    url, fragment = urldefrag(resolved)
    return url, fragment


def _directory(url: str) -> str:
    return url[: url.rfind("/") + 1]


def _display(url: str) -> str:
    return url[len(ARCHIVE_ORIGIN):] if url.startswith(ARCHIVE_ORIGIN) else url


async def _load(run: _Run, url: str, key: str) -> _Document:
    """Fetch or read one referenced document (raises ValueError with the reason)."""
    archive = run.archive
    location = url
    path = archive.path(url) if archive else None
    if archive is not None and path is None:
        for published, inside in run.mounts:
            if url.startswith(published) and archive.path(inside + url[len(published):]) in archive.files:
                path = archive.path(inside + url[len(published):])
                break
    if path is not None:
        raw = archive.files.get(path)  # type: ignore[union-attr]
        if raw is None:
            raw, path = await _by_self(archive, url)  # type: ignore[arg-type]
        location = archive.url(path)  # type: ignore[union-attr]
    elif archive is not None and (found := await _by_self_or_none(archive, url)) is not None:
        raw, path = found
        location = archive.url(path)
    else:
        raw, location = await _fetch(url)
    if run.total_bytes + len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("the referenced documents are too large to import together")
    # Counted before the parse: the loads run concurrently and share one total.
    run.total_bytes += len(raw)
    try:
        data, _fmt = await run_parse(functools.partial(parse_spec_text, decode_spec_bytes(raw)))
    except OpenAPIImportError as exc:
        raise ValueError(str(exc).rstrip(".")) from None
    return _Document(
        key=key,
        url=url,
        data=data,
        base=self_base(data, location),
        size=len(raw),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


async def _by_self_or_none(archive: SpecArchive, url: str) -> tuple[bytes, str] | None:
    try:
        return await _by_self(archive, url)
    except ValueError:
        return None


async def _by_self(archive: SpecArchive, url: str) -> tuple[bytes, str]:
    """The archive member whose ``$self`` is *url*."""
    if archive.by_self is None:
        archive.by_self = await run_parse(functools.partial(_index_self, archive))
    path = archive.by_self.get(url)
    if path is None:
        raise ValueError("it is not in the archive")
    return archive.files[path], path


def _index_self(archive: SpecArchive) -> dict[str, str]:
    index: dict[str, str] = {}
    for path, content in archive.files.items():
        if b"$self" not in content:
            continue
        try:
            data, _fmt = parse_spec_text(decode_spec_bytes(content))
        except OpenAPIImportError:
            continue
        base = self_base(data, archive.url(path))
        if base and not base.startswith(ARCHIVE_ORIGIN):
            index.setdefault(base, path)
    return index


async def _fetch(url: str) -> tuple[bytes, str]:
    from app.services.web_fetch import DestinationUnresolvedError, fetch_url_bytes

    try:
        fetched = await fetch_url_bytes(
            url,
            max_bytes=MAX_SPEC_BYTES,
            timeout=_FETCH_TIMEOUT_SECONDS,
            allow_private=True,
            accept=SPEC_ACCEPT_HEADER,
        )
    except DestinationUnresolvedError:
        raise ValueError(UNREACHABLE_REASON) from None
    except ValueError as exc:
        raise ValueError(str(exc).rstrip(".")) from None
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"the server answered HTTP {exc.response.status_code}") from None
    except httpx.HTTPError:
        # Refused, reset, filtered: one answer (see ``UNREACHABLE_MESSAGE``).
        raise ValueError(UNREACHABLE_REASON) from None
    return fetched.content, fetched.url


def _rewrite(run: _Run, holder: _Document, sites: list[dict[str, Any]]) -> None:
    for node in sites:
        ref = node["$ref"]
        try:
            url, fragment = _target(ref, holder.base)
        except LookupError:
            if ref not in run.unreachable:
                run.unreachable.append(ref)
            continue
        except ValueError as exc:
            run.failures.setdefault(ref, str(exc))
            continue
        if fragment and not fragment.startswith("/"):
            # A JSON Schema anchor (``#name``), not a JSON pointer.
            continue
        if url is None or url in run.root_urls:
            target: _Document | None = None if url is not None or holder.key is None else holder
        else:
            target = run.documents.get(url)
            if target is None:
                continue  # failed to load: left for the conversion to report
        prefix = f"/{BUNDLE_KEY}/{target.key}" if target is not None else ""
        node["$ref"] = f"#{prefix}{fragment}"


async def bundle_external_refs(
    document: SpecDocument, *, archive: SpecArchive | None = None
) -> tuple[SpecDocument, list[str]]:
    """Embed every document *document* references (see the module docstring).

    Returns the self-contained document — unchanged when it references no
    other document — and warnings about references that could not be
    followed.
    """
    location = archive.url(archive.root) if archive else document.source_url
    root = _Document(key=None, url=location, data=document.data, base=self_base(document.data, location))
    run = _Run(archive=archive, total_bytes=document.size_bytes)
    run.root_urls = {u for u in (location, root.base) if u}
    if archive is not None and root.base and root.base != location:
        run.mounts.append((_directory(root.base), _directory(location or ARCHIVE_ORIGIN)))
    queue: deque[_Document] = deque([root])
    semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def load(url: str, key: str) -> tuple[str, _Document | None, str | None]:
        async with semaphore:
            try:
                return url, await _load(run, url, key), None
            except ValueError as exc:
                return url, None, str(exc)

    while queue:
        holder = queue.popleft()
        sites = await run_parse(functools.partial(_ref_sites, holder.data))
        wanted: list[str] = []
        for node in sites:
            try:
                url, _fragment = _target(node["$ref"], holder.base)
            except (LookupError, ValueError):
                continue
            if url is None or url in run.root_urls or url in run.documents or url in run.failures:
                continue
            if url not in wanted:
                wanted.append(url)
        room = max(MAX_EXTERNAL_DOCUMENTS - run.counter, 0)
        for url in wanted[room:]:
            run.failures[url] = f"more than {MAX_EXTERNAL_DOCUMENTS} documents are referenced"
        batch = wanted[:room]
        # Keys follow the order references appear in, whatever order the
        # fetches finish in: the same spec always bundles the same way.
        keys = [f"d{run.counter + index + 1}" for index in range(len(batch))]
        run.counter += len(batch)
        for url, loaded, error in await asyncio.gather(*(load(u, k) for u, k in zip(batch, keys, strict=True))):
            if loaded is None:
                run.failures[url] = error or "it could not be loaded"
                continue
            run.documents[url] = loaded
            # References may also name it by where it ended up, or by its $self.
            if loaded.base and loaded.base not in run.root_urls:
                run.documents.setdefault(loaded.base, loaded)
            queue.append(loaded)
        _rewrite(run, holder, sites)

    warnings = _warnings(run, archive is not None or root.base is not None)
    embedded = list({id(doc): doc for doc in run.documents.values()}.values())
    if not embedded:
        return document, warnings

    data = dict(document.data)
    data[BUNDLE_KEY] = {doc.key: doc.data for doc in embedded}
    digest = hashlib.sha256(document.sha256.encode("ascii"))
    for doc in embedded:
        digest.update(doc.sha256.encode("ascii"))
    bundled = dataclasses.replace(
        document,
        data=data,
        sha256=digest.hexdigest(),
        size_bytes=document.size_bytes + sum(doc.size for doc in embedded),
    )
    return bundled, warnings


def _warnings(run: _Run, has_location: bool) -> list[str]:
    warnings: list[str] = []
    for url, reason in list(run.failures.items())[:_MAX_ISSUES_LISTED]:
        warnings.append(
            f"The referenced document {_display(url)} could not be loaded ({reason}); "
            "the definitions it holds are replaced by free-form values."
        )
    if len(run.failures) > _MAX_ISSUES_LISTED:
        warnings.append(f"{len(run.failures) - _MAX_ISSUES_LISTED} more referenced document(s) could not be loaded.")
    if run.unreachable:
        listed = ", ".join(run.unreachable[:_MAX_ISSUES_LISTED]) + ("…" if len(run.unreachable) > _MAX_ISSUES_LISTED else "")
        if has_location:
            warnings.append(f"{len(run.unreachable)} reference(s) could not be resolved: {listed}.")
        else:
            warnings.append(
                f"{len(run.unreachable)} reference(s) point to other files, which an uploaded "
                f"file cannot reach: {listed}. Import the spec from its URL, or upload a ZIP "
                "archive holding all of its files."
            )
    return warnings
