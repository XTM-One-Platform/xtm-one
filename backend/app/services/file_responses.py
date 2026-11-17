# SPDX-License-Identifier: Apache-2.0

"""The one HTTP answer for the bytes of a ``FileAttachment``.

Every route that hands a stored file to a client answers through here: the chat
downloads (by id and message-scoped), the signed link a channel reply carries,
the template asset download and the render-token fetch behind a deck export.
One contract, so no route drifts on what it renders inline, on who may keep the
bytes, or on how a replaced file is noticed.

**Disposition.** Raster images inline, and on the signed link a channel reply
carries what a browser shows or plays itself too (PDF, plain text, Markdown,
CSV, WebVTT, JSON and its JSON Lines and ``+json`` variants, audio, video);
everything else - HTML, SVG, XML, any other or unknown type - is a download
built by ``build_content_disposition``. Every answer is ``nosniff``, and an
inline one declares the type it was decided on, never the stored string.

**Validator.** A strong ``ETag`` derived from the row, never from the bytes.
Every writer stores a file's bytes under an object key of its own
(``file_storage.upload`` mints a random one, the brand-asset seed derives it from
the content digest), and an in-place replace (``PUT
/template-containers/{id}/assets/{asset_id}``, the seed's refresh) writes a NEW
key and keeps the row id. The key therefore names the bytes behind the row, and a
digest of it (with the type and name the headers are built from, and the version
of the rules that build them) is a validator the route holds after its
authorisation read, without touching the object store. The key itself never
leaves the server.

**Lifetime.** ``private`` always: a file is authorised per request, so no shared
cache may keep it, and ``Vary: Authorization`` keeps a browser from handing one
person's copy to the next person signed in to the same profile without asking
the server. A row whose bytes can change behind its id (a template asset) is
``no-cache``: the browser revalidates on every use and a matching
``If-None-Match`` is a ``304`` with no download. Every other row is written once
and stays a day in the browser's cache, which the deck-picture grant's cost
bound assumes (``file_access._readable_presentation_names_file``); the same
``ETag`` makes an expired entry a cheap ``304`` too. A signed link is never kept
past its token. The render-token fetch is ``no-store``: its token rides in the
query string and an export reads the bytes as they are now, so nothing keeps
the answer and it carries no validator.
"""

from __future__ import annotations

import hashlib
import re
import time

from starlette.responses import Response

from app.core.utils import build_content_disposition, if_none_match_hits
from app.models.template_container import TEMPLATE_ASSET_SOURCE

#: The raster images every file route serves INLINE, under each name clients
#: store them with: a picture the browser paints, or saves when it cannot paint
#: the format (TIFF, HEIF or JPEG XL outside Safari, JPEG 2000 in most browsers).
#: Never ``image/svg+xml``, an XML document that is a page when navigated to,
#: whatever the sanitiser kept. A type outside the inline sets is a download
#: (``Content-Disposition: attachment``); every answer carries
#: ``X-Content-Type-Options: nosniff`` so the browser trusts the declared type
#: over the bytes.
INLINE_FILE_CONTENT_TYPES = frozenset(
    {
        "image/png",
        "image/x-png",
        "image/apng",
        "image/jpeg",
        "image/jpg",
        "image/pjpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "image/bmp",
        "image/x-ms-bmp",
        "image/tiff",
        "image/x-icon",
        "image/vnd.microsoft.icon",
        "image/heic",
        "image/heif",
        "image/heic-sequence",
        "image/heif-sequence",
        "image/jxl",
        "image/jp2",
        "image/jpx",
        "image/jpm",
    }
)

#: The documents the signed channel link serves INLINE too, with the ``+json``
#: and media families below: the browser shows them in its own viewer or plays
#: them, and none runs script against the platform origin.
#: Never ``text/html`` - a stored file is user- or model-authored
#: (``generate_file``, ``render_html_to_image``'s HTML source) and a page
#: rendered on the platform origin runs its scripts against the app's own
#: session - nor any XML type. The session routes keep these downloads: their
#: clients (the web app's file chips, the embedded chat proxy of OpenCTI and
#: OpenAEV) save what they fetch under the name this header carries.
INLINE_DOCUMENT_CONTENT_TYPES = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
        "text/csv",
        "text/vtt",
        "application/json",
        "application/x-ndjson",
    }
)

#: The ``+json`` documents the signed channel link serves inline too, matched on
#: the whole ``type/subtype`` in RFC 6838 names, so no second type rides along.
#: Whatever the name says, the suffix makes the type JSON to every browser.
_JSON_DOCUMENT_RE = re.compile(r"application/[a-z0-9][a-z0-9!#$&^_.+-]{0,121}\+json")

#: The audio and video it serves inline too, matched the same way. An audio or
#: video subtype naming XML or HTML never opens (:func:`_is_inline_document`):
#: navigated to, a ``+xml`` type is an XML document, and one can carry script.
_MEDIA_RE = re.compile(r"(?:audio|video)/[a-z0-9][a-z0-9!#$&^_.+-]{0,126}")

#: The inline types, besides every ``text/*`` and ``+json`` one, whose stored
#: ``charset`` is kept: it is how their bytes decode.
_CHARSET_CONTENT_TYPES = frozenset({"application/json", "application/x-ndjson"})
_CHARSET_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,63}")

#: Seconds a browser keeps a file whose bytes never change behind its id.
IMMUTABLE_FILE_MAX_AGE = 86400

#: ``Cache-Control`` of a file answer no cache may keep at all.
NO_STORE_CACHE_CONTROL = "private, no-store"

#: The version of the rules :func:`file_response_headers` builds a ``200`` with,
#: part of every validator. A ``304`` carries no ``Content-Type`` and no
#: ``Content-Disposition``, so the browser keeps the ones it cached: bump this
#: whenever those rules change, or an answer cached under the previous ones is
#: revalidated into keeping them.
_RESPONSE_CONTRACT_VERSION = "4"


def file_etag(file_att) -> str:
    """The strong ``ETag`` of *file_att*'s bytes and of the headers built from its row."""
    material = "\0".join(
        (
            _RESPONSE_CONTRACT_VERSION,
            getattr(file_att, "s3_object_name", None) or "",
            getattr(file_att, "content_type", None) or "",
            getattr(file_att, "filename", None) or "",
        )
    )
    return f'"{hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]}"'


def file_version(file_att) -> str:
    """:func:`file_etag` without its quotes: a URL-safe token that changes with the bytes."""
    return file_etag(file_att).strip('"')


def file_bytes_may_change(file_att) -> bool:
    """Whether *file_att*'s bytes can be replaced while its id stays (a template asset)."""
    return getattr(file_att, "source", None) == TEMPLATE_ASSET_SOURCE


def file_cache_control(file_att, *, max_age: int = IMMUTABLE_FILE_MAX_AGE) -> str:
    """``Cache-Control`` for *file_att*: revalidated when it can change, kept *max_age* otherwise."""
    if file_bytes_may_change(file_att):
        return "private, no-cache"
    return f"private, max-age={max(0, int(max_age))}"


def _content_type_essence(content_type: str | None) -> str:
    """``type/subtype`` of a stored type, lowercased, its parameters dropped."""
    return (content_type or "").split(";", 1)[0].strip().lower()


def _stored_charset(content_type: str | None) -> str | None:
    """The ``charset`` parameter of a stored type, when it is a plain charset name."""
    for param in (content_type or "").split(";")[1:]:
        name, _, value = param.partition("=")
        if name.strip().lower() == "charset":
            value = value.strip().strip('"')
            return value if _CHARSET_RE.fullmatch(value) else None
    return None


def _is_inline_document(essence: str) -> bool:
    """Whether the signed channel link opens a file of this ``type/subtype`` in the browser."""
    if essence in INLINE_DOCUMENT_CONTENT_TYPES or _JSON_DOCUMENT_RE.fullmatch(essence):
        return True
    return _MEDIA_RE.fullmatch(essence) is not None and "xml" not in essence and "html" not in essence


def _inline_content_type(stored: str | None, essence: str) -> str:
    """The ``Content-Type`` an inline answer declares: the type it was decided on.

    Never the stored string: that is whatever the uploader's client declared,
    and a browser splits ``Content-Type`` on commas and reads the LAST type, so
    ``image/png;x=,text/html`` sent back as stored is an HTML page. A text or
    JSON type keeps a stored charset, and a text type without one is declared
    UTF-8, the charset Starlette gives a ``text/*`` media type.
    """
    if not (essence.startswith("text/") or essence.endswith("+json") or essence in _CHARSET_CONTENT_TYPES):
        return essence
    charset = _stored_charset(stored) or ("utf-8" if essence.startswith("text/") else None)
    return f"{essence}; charset={charset}" if charset else essence


def file_response_headers(
    file_att,
    *,
    max_age: int = IMMUTABLE_FILE_MAX_AGE,
    inline_images: bool = True,
    inline_documents: bool = False,
    store: bool = True,
) -> dict[str, str]:
    """The headers every FileAttachment byte route answers a ``200`` with.

    ``filename`` originates from user uploads and AI-generated output - never
    interpolated raw (a CRLF header-injection vector, and broken quoting on
    names with ``"`` or non-ASCII); ``build_content_disposition`` strips CR/LF
    and emits both the ASCII-quoted form (RFC 6266 fallback) and the
    percent-encoded ``filename*=UTF-8''...`` (RFC 5987) for modern clients.
    ``inline_images=False`` makes a raster image a download like every other
    type, for a route whose client names the saved file from this header;
    ``inline_documents=True`` opens what a browser shows or plays itself too
    (:func:`_is_inline_document`), for the signed link a channel reply carries.
    The decision reads the STORED type, never the filename. An inline answer
    carries its own ``Content-Type`` (:func:`_inline_content_type`), which wins
    over the route's ``media_type``.
    ``store=False`` is an answer no cache may keep (``private, no-store``); it
    carries no validator, since nothing will ever revalidate it.
    """
    content_type = _content_type_essence(file_att.content_type)
    inline = (inline_images and content_type in INLINE_FILE_CONTENT_TYPES) or (
        inline_documents and _is_inline_document(content_type)
    )
    if inline:
        headers = {
            "Content-Disposition": "inline",
            "Content-Type": _inline_content_type(file_att.content_type, content_type),
        }
    else:
        headers = {"Content-Disposition": build_content_disposition(file_att.filename)}
    headers["X-Content-Type-Options"] = "nosniff"
    if not store:
        headers["Cache-Control"] = NO_STORE_CACHE_CONTROL
        return headers
    headers.update(
        {
            "ETag": file_etag(file_att),
            "Cache-Control": file_cache_control(file_att, max_age=max_age),
            "Vary": "Authorization",
        }
    )
    return headers


def file_not_modified(
    file_att, if_none_match: str | None, *, max_age: int = IMMUTABLE_FILE_MAX_AGE
) -> Response | None:
    """The ``304`` for a request that already holds *file_att*'s bytes, else ``None``.

    Call it AFTER the route's authorisation and BEFORE the object-store read: a
    revoked reader must get the refusal, not a ``304`` confirming the file is
    unchanged, and a reader who holds the bytes must not cost a download.
    """
    etag = file_etag(file_att)
    if not if_none_match_hits(if_none_match, etag):
        return None
    return Response(
        status_code=304,
        headers={
            "ETag": etag,
            "Cache-Control": file_cache_control(file_att, max_age=max_age),
            "Vary": "Authorization",
        },
    )


def token_max_age(expires: int, *, now: float | None = None) -> int:
    """How long a signed link's answer may be kept: a day at most, never past its token."""
    remaining = int(expires) - int(time.time() if now is None else now)
    return max(0, min(IMMUTABLE_FILE_MAX_AGE, remaining))


__all__ = [
    "IMMUTABLE_FILE_MAX_AGE",
    "INLINE_DOCUMENT_CONTENT_TYPES",
    "INLINE_FILE_CONTENT_TYPES",
    "NO_STORE_CACHE_CONTROL",
    "file_bytes_may_change",
    "file_cache_control",
    "file_etag",
    "file_not_modified",
    "file_response_headers",
    "file_version",
    "token_max_age",
]
