# SPDX-License-Identifier: Apache-2.0

"""One shape for a file an integration tool meets.

Every tool that downloads a file gives the agent four things at once, with no
setting: the links the source exposes (``Web URL`` / ``Download URL``), the
file stored in the run's FileContext (``persist_file_fn`` registers it; the
``[[FILE:id]]`` marker renders as a chip), the file on the tool result (the
model provider reads its text or sees its pixels, within the loop's caps) and
the ``file_id`` every upload tool takes (:func:`read_pipe_file`).

A file that can be live malware (a submitted sample, a file dropped during a
detonation, a file taken from an endpoint) goes through
:func:`deliver_live_artefact` instead: it is stored only as the vendor's
password-protected archive and never carried on the result.

What the source says about the file - its name, type, details and links -
reaches the model only inside a quarantine block (:func:`untrusted_file_facts`).
An upload tool names the file it sent by its ``file_id`` and puts the stored
name in the same block (:func:`uploaded_file_answer`).
"""

from __future__ import annotations

import hashlib
import io
import logging
import mimetypes
import struct
import zipfile
import zlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import httpx

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.http import AutoRefreshClient, format_http_error, format_network_error
from app.services.content_safety import single_line, wrap_untrusted
from app.services.web_fetch import guarded_client_options, validate_outbound_url_async
from app.tools.base import FileContext, ToolResult

logger = logging.getLogger(__name__)

#: Declared types that say nothing about the bytes (a ``force-download``
#: answer): the file's name decides instead.
_UNINFORMATIVE_TYPES = frozenset(
    {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        "application/force-download",
        "application/download",
        "application/x-download",
    }
)
#: Redirects :func:`fetch_file` follows before giving up.
_MAX_REDIRECTS = 5
#: Timeout of the clean client past the origin when the caller's has none.
_CLEAN_HOP_TIMEOUT = 120
#: Request arguments a redirected GET does not resend: the body, and the
#: query, which the Location already states (and may sign).
_BODY_ARGUMENTS = frozenset({"content", "data", "files", "json", "params"})

#: ``(filename, raw_bytes, content_type) -> FileRef | None``, injected by
#: ``integration_tools._make_persist_file_fn``.
PersistFileFn = Callable[[str, bytes, str], Awaitable[Any]]

#: A link label and its URL; a blank URL is left out.
Link = tuple[str, str | None]


def link_lines(links: Iterable[Link]) -> list[str]:
    """``Label: url`` for every link the source gave, in order, each URL once."""
    lines: list[str] = []
    seen: set[str] = set()
    for label, url in links:
        value = str(url or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        lines.append(f"{label}: {value}")
    return lines


#: What a file name is cut to in a log line.
_MAX_LOGGED_NAME = 200


def untrusted_file_facts(
    *,
    source: str,
    filename: str,
    content_type: str | None = None,
    details: Iterable[str] = (),
    links: Iterable[Link] = (),
) -> str:
    """What *source* says about a file, in one quarantine block.

    The name, the declared type, the provider's detail lines and the links
    all come from a third party (a document title, an attachment name, an alt
    text, a script preview, a signed URL), so they reach the model only
    inside a :func:`wrap_untrusted` block, after the answer's own lines. The
    name and the type stay on one line each. The stored name, the file_id and
    the ``[[FILE:id]]`` marker are not in the block.
    """
    lines = [f"Name: {single_line(filename or '') or '(unnamed)'}"]
    if content_type:
        lines.append(f"Content type: {single_line(content_type)}")
    lines.extend(details)
    lines.extend(link_lines(links))
    return wrap_untrusted("\n".join(lines), source=f"{source} file details")


def uploaded_file_answer(
    text: str,
    *,
    target: str,
    filename: str,
    content_type: str | None = None,
    details: Iterable[str] = (),
    links: Iterable[Link] = (),
) -> str:
    """An upload tool's answer: *text*, then the file's facts in one quarantine block.

    The stored name of a file of the run is a third party's text (a file
    downloaded from a provider, a name the user's client sent), and what
    *target* answers about the uploaded file (its title, its type, its URL) is
    the target's. *text* holds the platform's own lines only and names the
    file by its ``file_id``; the name, the type, *details* and *links* follow
    in the block (:func:`untrusted_file_facts`), which closes the answer.
    """
    facts = untrusted_file_facts(
        source=target, filename=filename, content_type=content_type, details=details, links=links
    )
    return f"{text}\n{facts}"


def format_kb(size: int) -> str:
    return f"{round(size / 1024, 1)} KB"


def format_mb(size: int) -> str:
    return f"{round(size / 1024 / 1024, 1)} MB"


def content_disposition_filename(header: str | None) -> str:
    """The file name a ``Content-Disposition`` header gives, or ``""``.

    The RFC 5987 ``filename*`` form (``charset'language'encoded``) wins over
    the plain ``filename``.
    """
    plain = ""
    for part in (header or "").split(";"):
        key, _, value = part.strip().partition("=")
        key = key.strip().lower()
        value = value.strip().strip('"')
        if key == "filename*" and value.count("'") >= 2:
            charset, _language, encoded = value.split("'", 2)
            try:
                name = unquote(encoded, encoding=charset or "utf-8", errors="replace")
            except LookupError:
                name = unquote(encoded, errors="replace")
            if name:
                return name
        elif key == "filename" and value and not plain:
            plain = value
    return plain


def file_content_type(declared: str | None, filename: str) -> str:
    """The media type of a downloaded file: the declared one, else its name's."""
    value = (declared or "").split(";", 1)[0].strip().lower()
    if value in _UNINFORMATIVE_TYPES:
        guessed, _ = mimetypes.guess_type(filename)
        return guessed or "application/octet-stream"
    return value


def with_extension(filename: str, content_type: str) -> str:
    """*filename*, given the extension of *content_type* when it has none."""
    if "." in filename.rsplit("/", 1)[-1]:
        return filename
    extension = mimetypes.guess_extension(content_type or "") or ""
    return f"{filename}{extension}"


@dataclass(frozen=True)
class FetchedFile:
    data: bytes
    content_type: str
    #: The ``Content-Disposition`` name, ``""`` when the answer gave none.
    filename: str


class FetchError(str):
    """The error :func:`fetch_file` answers with, and what caused it.

    A ``str`` - the text to answer with - that also says whether the file
    was over the cap (``too_large``, with the declared ``size`` when the
    answer gave one) or the HTTP ``status`` of a refused answer, for a tool
    that answers differently in those cases (keeping a link past the cap).
    """

    status: int | None
    too_large: bool
    size: int | None

    def __new__(
        cls,
        text: str,
        *,
        status: int | None = None,
        too_large: bool = False,
        size: int | None = None,
    ) -> FetchError:
        error = super().__new__(cls, text)
        error.status, error.too_large, error.size = status, too_large, size
        return error


#: What an error answer's body is read up to: enough for the provider's
#: message, never a body of any size.
_MAX_ERROR_BODY_BYTES = 64 * 1024
#: Headers that describe the wire body, not the bounded copy of it.
_WIRE_BODY_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding"})


def _decode_bounded(raw: bytes, encoding: str) -> bytes:
    """*raw* decoded, never past ``_MAX_ERROR_BODY_BYTES`` (a compressed body can expand a thousandfold).

    An encoding other than identity / gzip / deflate, or a body that does not
    decode, gives no body: the message is then built from the status alone.
    """
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding in ("gzip", "x-gzip"):
        attempts = [16 + zlib.MAX_WBITS]
    elif encoding == "deflate":
        attempts = [zlib.MAX_WBITS, -zlib.MAX_WBITS]
    else:
        return b""
    for wbits in attempts:
        try:
            return zlib.decompressobj(wbits).decompress(raw, _MAX_ERROR_BODY_BYTES)
        except zlib.error:
            continue
    return b""


async def _error_answer(resp: httpx.Response, provider: str, action: str) -> FetchError:
    """The provider error a refused answer amounts to, read from a bounded body.

    The wire bytes are read up to the bound (``aiter_raw``: ``aiter_bytes``
    would decode a whole transport chunk first), then decoded up to it.
    """
    if resp.is_stream_consumed:
        # Already read into memory (an answer built from bytes): nothing left to bound.
        body = resp.content[:_MAX_ERROR_BODY_BYTES]
    else:
        raw = bytearray()
        async for chunk in resp.aiter_raw():
            raw += chunk[: _MAX_ERROR_BODY_BYTES - len(raw)]
            if len(raw) >= _MAX_ERROR_BODY_BYTES:
                break
        body = _decode_bounded(bytes(raw), resp.headers.get("content-encoding", ""))
    await resp.aclose()
    bounded = httpx.Response(
        resp.status_code,
        headers=[
            (key, value)
            for key, value in resp.headers.multi_items()
            if key.lower() not in _WIRE_BODY_HEADERS
        ],
        content=body,
        request=resp.request,
    )
    return FetchError(format_http_error(bounded, provider, action), status=resp.status_code)


_DEFAULT_PORTS = {"https": 443, "http": 80}


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    """Scheme, host and port, the default port spelled out (``:443`` is no other origin)."""
    return url.scheme, url.host, url.port if url.port is not None else _DEFAULT_PORTS.get(url.scheme)


async def _redirect_refusal(
    start: httpx.URL, current: httpx.URL, target: httpx.URL, *, left_start: bool
) -> str | None:
    """Why the redirect from *current* to *target* is not followed, or ``None``.

    The start origin is the caller's choice (a self-hosted instance may sit on
    a private address) as long as the chain has not left it; any other origin
    a provider's answer names - and the start origin again once the chain
    left it, since its name may resolve elsewhere by then - must pass the
    outbound URL policy, so a signed link cannot bounce the platform to
    loopback, a private network or a metadata endpoint.
    """
    if current.scheme == "https" and target.scheme != "https":
        return "it leaves HTTPS"
    if not left_start and _origin(target) == _origin(start):
        return None
    try:
        await validate_outbound_url_async(str(target))
    except ValueError as exc:
        return str(exc)
    return None


async def fetch_file(
    client: httpx.AsyncClient | AutoRefreshClient,
    method: str,
    url: str,
    *,
    provider: str,
    action: str,
    max_bytes: int,
    headers: dict[str, str] | None = None,
    follow_redirects: bool | None = None,
    **kwargs: Any,
) -> FetchedFile | FetchError:
    """Stream a file answer into memory, never past *max_bytes*.

    For a download whose size nothing declared beforehand (an export, a
    signed link): the answer is refused from its ``Content-Length`` or at the
    first chunk over the cap, never held whole, and every answer is closed
    before this returns; an error answer is read up to
    ``_MAX_ERROR_BODY_BYTES`` for its message. A redirect is followed here,
    as a GET, and one that leaves the origin goes without *headers*: httpx
    only drops ``Authorization`` there, and an API-key header must not reach
    the storage host a provider hands the file off to. A hop that leaves
    HTTPS, or reaches another origin the outbound URL policy refuses, is not
    followed (:func:`_redirect_refusal`). Past the origin every hop goes
    through a clean client with the caller's event hooks: the caller's client
    may carry credentials of its own (default headers, the
    ``AutoRefreshClient`` token and its refresh).
    *follow_redirects* defaults to the client's own setting. Returns the error
    to answer with otherwise.
    """
    if follow_redirects is None:
        follow_redirects = bool(getattr(client, "follow_redirects", False))
    too_large = (
        f"Error: the {provider} file is over the {max_bytes // 1024 // 1024} MB download limit."
    )
    # Headers are passed only when the caller gave some: an AutoRefreshClient
    # adds its token to a request that names none.
    request_headers = {"headers": dict(headers)} if headers is not None else {}
    clean: httpx.AsyncClient | None = None
    try:
        start = current = httpx.URL(url)
        # The first request, then at most _MAX_REDIRECTS redirects.
        for _hop in range(_MAX_REDIRECTS + 1):
            hop_client = clean or client
            async with hop_client.stream(
                method.upper(), current, follow_redirects=False, **request_headers, **kwargs
            ) as resp:
                # A 3xx without a Location is answered as the error it is.
                if follow_redirects and resp.has_redirect_location:
                    target = current.join(resp.headers["location"])
                    # ``clean`` exists once the chain has left the start origin.
                    refusal = await _redirect_refusal(
                        start, current, target, left_start=clean is not None
                    )
                    if refusal:
                        return FetchError(
                            f"Error: the {provider} download redirected to "
                            f"{target.scheme}://{target.host}, which is not followed: {refusal}."
                        )
                    if clean is None and _origin(target) != _origin(current):
                        request_headers = {}
                        # The caller's hooks still see every hop (a policy
                        # check); its headers and token do not follow. Every
                        # hop past the start origin passed the public-only
                        # policy, so each connection goes to an address it
                        # checked when it opens (DNS rebinding).
                        clean = egress_client(
                            timeout=getattr(client, "timeout", None) or _CLEAN_HOP_TIMEOUT,
                            **guarded_client_options(
                                allow_private=False,
                                event_hooks=getattr(client, "event_hooks", None) or {},
                            ),
                        )
                    current, method = target, "GET"
                    kwargs = {k: v for k, v in kwargs.items() if k not in _BODY_ARGUMENTS}
                    await resp.aclose()
                    continue
                if not resp.is_success:
                    return await _error_answer(resp, provider, action)
                declared = resp.headers.get("content-length", "")
                # ASCII digits of a bounded length: int() refuses a 5,000-digit string.
                if declared.isascii() and declared.isdigit() and len(declared) <= 20 and int(declared) > max_bytes:
                    await resp.aclose()
                    return FetchError(too_large, too_large=True, size=int(declared))
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        await resp.aclose()
                        return FetchError(too_large, too_large=True)
                    chunks.append(chunk)
                await resp.aclose()
                return FetchedFile(
                    data=b"".join(chunks),
                    content_type=resp.headers.get("content-type", ""),
                    filename=content_disposition_filename(resp.headers.get("content-disposition")),
                )
        return FetchError(f"Error: the {provider} download was redirected more than {_MAX_REDIRECTS} times.")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        return FetchError(format_network_error(exc, provider, action))
    finally:
        if clean is not None:
            await clean.aclose()


async def deliver_file(
    *,
    data: bytes,
    filename: str,
    content_type: str,
    source: str,
    persist_file_fn: PersistFileFn | None,
    links: Iterable[Link] = (),
    details: Iterable[str] = (),
    notes: Iterable[str] = (),
    mark_unstored: bool = True,
) -> ToolResult:
    """Store *data* as a file of the run and answer with its links and file_id.

    Without a persistence scope (an evaluation run) the file is kept in memory
    for the run: still on the result and still forwardable. Its
    ``[[FILE:id]]`` marker is then offered only when *mark_unstored* (the
    tools whose fallback always carried one). The name, type, *details* and
    *links* follow in an untrusted block (:func:`untrusted_file_facts`).
    *notes* are the platform's own lines (a handling rule), shown outside it:
    never put a third party's text in them.
    """
    from app.llm.base import FileRef
    from app.tools.base import workspace_ingest_requested

    content_type = content_type or "application/octet-stream"
    file_ref = None
    # Bound for the sandbox workspace (``to_workspace_path``): the
    # bytes go there, not into a conversation file.
    if workspace_ingest_requested():
        persist_file_fn = None
    if persist_file_fn is not None:
        try:
            file_ref = await persist_file_fn(filename, data, content_type)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "%s file persistence failed for %r: %s",
                source,
                filename[:_MAX_LOGGED_NAME],
                format_exception_message(exc),
            )
            file_ref = None
    header = f"Downloaded a {format_kb(len(data))} file from {source}"
    stored = file_ref is not None
    if not stored:
        file_ref = FileRef.from_bytes(data=data, filename=filename, content_type=content_type)
        if workspace_ingest_requested():
            from app.tools.base import register_fetched_file

            register_fetched_file(None, file_ref)
    if stored or mark_unstored:
        header += f" as a reusable file. [[FILE:{file_ref.file_id}]]"
        forward = (
            f"file_id: {file_ref.file_id} - forward it to any tool that takes a file "
            "(an upload, an attachment, an import); the user gets a download card "
            "only when the answer carries the [[FILE:...]] marker verbatim."
        )
    else:
        header += " (kept for this run only: this session cannot store files)."
        forward = (
            f"file_id: {file_ref.file_id} - forward it to any tool that takes a file "
            "(an upload, an attachment, an import)."
        )
    facts = untrusted_file_facts(
        source=source, filename=filename, content_type=content_type, details=details, links=links
    )
    return ToolResult(
        tool_call_id="", content="\n".join([header, *notes, forward, facts]), files=[file_ref]
    )


#: The password the vendors seal a live malware archive with - a public
#: convention, not a secret.
SEALED_ARCHIVE_PASSWORD = "infected"  # noqa: S105
#: A WinZip AES entry: its compression method, its extra field, and the key
#: size of each strength (AE-1 / AE-2).
_AES_METHOD = 99
_AES_EXTRA_ID = 0x9901
_AES_KEY_BYTES = {1: 16, 2: 24, 3: 32}
_LOCAL_HEADER = struct.Struct("<4s5H3L2H")


def _aes_strength(extra: bytes) -> int:
    offset = 0
    while offset + 4 <= len(extra):
        header_id, size = struct.unpack_from("<HH", extra, offset)
        if header_id == _AES_EXTRA_ID and size >= 7:
            return extra[offset + 8]
        offset += 4 + size
    return 0


def _aes_sealed(data: bytes, entry: zipfile.ZipInfo, password: bytes) -> bool:
    """Whether the AES entry's password verifier matches *password*."""
    key_bytes = _AES_KEY_BYTES.get(_aes_strength(entry.extra), 0)
    fields = _LOCAL_HEADER.unpack_from(data, entry.header_offset)
    if not key_bytes or fields[0] != b"PK\x03\x04":
        return False
    salt_at = entry.header_offset + _LOCAL_HEADER.size + fields[-2] + fields[-1]
    verifier_at = salt_at + key_bytes // 2
    verifier = data[verifier_at : verifier_at + 2]
    derived = hashlib.pbkdf2_hmac(
        "sha1", password, data[salt_at:verifier_at], 1000, 2 * key_bytes + 2
    )
    return len(verifier) == 2 and derived[-2:] == verifier


def is_sealed_archive(data: bytes) -> bool:
    """Whether *data* is a ZIP whose every file entry is sealed with the password.

    The only form the platform keeps a live-malicious file in: nothing on the
    platform opens a ZIP (it reads as no text and downloads as an
    attachment), and an encrypted entry can neither run nor be read by
    accident. Each entry's password check - the ZipCrypto header, the
    WinZip AES verifier - is read; none of its content is.
    """
    password = SEALED_ARCHIVE_PASSWORD.encode()
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = [entry for entry in archive.infolist() if not entry.is_dir()]
            for entry in entries:
                if not entry.flag_bits & 0x1:
                    return False
                if entry.compress_type == _AES_METHOD:
                    if not _aes_sealed(data, entry, password):
                        return False
                    continue
                with archive.open(entry, pwd=password):
                    pass
    except (
        zipfile.BadZipFile,
        NotImplementedError,
        RuntimeError,
        ValueError,
        OSError,
        struct.error,
    ):
        return False
    return bool(entries)


def live_artefact_links(
    *, filename: str, source: str, links: Iterable[Link] = (), reason: str
) -> str:
    """The answer for a live-malicious file the platform does not store.

    *reason* is the platform's own text; the name and links follow in an
    untrusted block (:func:`untrusted_file_facts`).
    """
    lines = [
        f"A file from {source} can hold live malware and is not stored: {reason}",
        "Fetch it from the source into an isolated analysis environment.",
        untrusted_file_facts(source=source, filename=filename, links=links),
    ]
    return "\n".join(lines)


async def deliver_live_artefact(
    *,
    data: bytes,
    filename: str,
    source: str,
    persist_file_fn: PersistFileFn | None,
    links: Iterable[Link] = (),
    details: Iterable[str] = (),
) -> ToolResult:
    """Store a vendor's password-protected archive of live malware as a file of the run.

    The archive is kept exactly as it came - a download card and a file_id
    that forwards it (to another sandbox) - and is never carried on the
    result, so the model never reads it. Anything but a ZIP sealed with
    :data:`SEALED_ARCHIVE_PASSWORD`, and any session that cannot store files,
    gets the links and the reason instead.
    """
    links = list(links)

    def _linked(reason: str, *, is_error: bool = False) -> ToolResult:
        content = live_artefact_links(
            filename=filename, source=source, links=links, reason=reason
        )
        return ToolResult(tool_call_id="", content=content, is_error=is_error)

    if persist_file_fn is None:
        return _linked("this session cannot store files.")
    if not is_sealed_archive(data):
        return _linked(
            f"{source} did not serve it as a ZIP the platform can confirm is sealed "
            f"with the password '{SEALED_ARCHIVE_PASSWORD}' - the only form it keeps "
            "a live-malicious file in."
        )
    try:
        file_ref = await persist_file_fn(filename, data, "application/zip")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "%s archive persistence failed for %r: %s",
            source,
            filename[:_MAX_LOGGED_NAME],
            format_exception_message(exc),
        )
        file_ref = None
    if file_ref is None:
        return _linked("it could not be stored.", is_error=True)
    lines = [
        f"Downloaded a {format_kb(len(data))} application/zip file from "
        f"{source} as a reusable file. [[FILE:{file_ref.file_id}]]",
        f"Live malware: the ZIP is encrypted with the password "
        f"'{SEALED_ARCHIVE_PASSWORD}' and kept as it came - the platform never "
        "opens or reads it. Extract it only in an isolated analysis environment.",
        f"file_id: {file_ref.file_id} - forward it to any tool that takes a file "
        "(another sandbox, an import); the user gets a download card only when "
        "the answer carries the [[FILE:...]] marker verbatim.",
        untrusted_file_facts(source=source, filename=filename, details=details, links=links),
    ]
    return ToolResult(tool_call_id="", content="\n".join(lines), files=[])


async def read_pipe_file(
    file_context: FileContext | None,
    file_id: str,
    *,
    max_bytes: int,
    target: str,
) -> tuple[bytes, str, str] | str:
    """``(bytes, filename, content_type)`` of *file_id* for an upload to *target*.

    Any file of the run qualifies: attached by the user, generated, or
    downloaded by another tool. Returns the error to answer with otherwise.
    """
    file_id = str(file_id or "").strip()
    if not file_id:
        return "Error: file_id is required (a file attached, generated or downloaded in this conversation)."
    if file_context is None:
        return (
            "Error: no file context available - files can only be forwarded "
            "from a conversation or an assignment run."
        )
    try:
        resolved = await file_context.resolve_upload(file_id)
    except Exception as exc:
        logger.warning(
            "Could not read file %s for an upload to %s: %s",
            file_id,
            target,
            format_exception_message(exc),
        )
        return (
            f"Error: file '{file_id}' could not be read from storage (it may "
            "have expired). Download or attach it again and retry."
        )
    if resolved is None:
        return (
            f"Error: file '{file_id}' was not found in this conversation. Use a "
            "file_id from an attached, generated or downloaded file."
        )
    data, filename, content_type = resolved
    if len(data) > max_bytes:
        # Named by its id: the name may be a third party's text.
        return (
            f"Error: file '{file_id}' is {round(len(data) / 1024 / 1024, 1)} MB, over "
            f"the {max_bytes // 1024 // 1024} MB upload limit of {target}."
        )
    return data, filename, content_type or "application/octet-stream"
