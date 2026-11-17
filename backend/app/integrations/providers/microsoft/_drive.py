"""Drive-item helpers shared by the OneDrive and SharePoint tools: addressing, links and readers."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from app.integrations.http import AutoRefreshClient

from ._common import (
    _BASE_URL,
    _PROVIDER,
    MicrosoftContext,
    _coerce_int,
)

# ── Drive-item / file-reading limits ──────────────────────────────────────
# Shared by the OneDrive, SharePoint, and "open file by URL" reading tools so
# every file path enforces the same truncation budget regardless of where the
# file lives.
# Only the OpenXML spreadsheet formats are addressable via the Graph Excel API
# (``/workbook/...``). The legacy binary ``.xls`` and ``.xlsb`` formats are not,
# so they fall through to the download-URL path instead of erroring.
_WORKBOOK_EXTS = (".xlsx", ".xlsm")
# Extensions we always treat as plain text even when Graph reports a generic
# ``application/octet-stream`` mime (common for ``.md`` / ``.csv`` / ``.log``).
_TEXT_EXTS = (
    ".txt",
    ".csv",
    ".tsv",
    ".md",
    ".markdown",
    ".json",
    ".xml",
    ".html",
    ".htm",
    ".log",
    ".yaml",
    ".yml",
    ".ini",
    ".rst",
)
_TEXT_MIME_PREFIXES = (
    "text/",
    "application/json",
    "application/xml",
    "application/csv",
    "application/javascript",
)
_MAX_TEXT_BYTES = 500_000
_MAX_CONTENT_CHARS = 10_000
# Office / PDF documents whose text is extracted server-side (the platform's
# own document extraction) instead of answering with a download URL the model
# cannot use. ``.xlsx`` / ``.xlsm`` keep the Excel API path above.
_DOCUMENT_EXTS = (".docx", ".pptx", ".pdf", ".xls")
# Largest document downloaded for inline extraction; above it the answer
# points at ``download_microsoft_file`` instead. An image-heavy Word file is
# mostly pictures, so its text is small even when the file is not.
_MAX_DOCUMENT_BYTES = 25 * 1024 * 1024
# Characters of extracted document text returned inline - the same budget as
# a chat upload's message.
_MAX_DOCUMENT_CHARS = 50_000
_MAX_WORKBOOK_SHEETS = 5
_MAX_WORKBOOK_ROWS = 60
_MAX_WORKBOOK_COLS = 25


def _download_tool_hint(web_url: str) -> str:
    """How to get a file the reading tools cannot show inline.

    A Graph pre-authenticated download URL is useless to the model: the code
    interpreter has no network. ``download_microsoft_file`` puts the bytes in
    the conversation, where ``read_file_content`` and the code interpreter's
    mounted copy can reach them.
    """
    target = (
        f'download_microsoft_file(url="{web_url}")'
        if web_url
        else "download_microsoft_file with the file's SharePoint / OneDrive URL"
    )
    return (
        f"To work on the whole file, put it in the conversation with {target}, "
        "then read it with read_file_content or process it with code_interpreter."
    )


def _cut_inline(text: str, limit: int, web_url: str) -> str:
    """*text* up to *limit* characters, with a footer that says so when cut."""
    if len(text) <= limit:
        return text
    return (
        f"{text[:limit]}\n\n[... truncated: the first {limit:,} of {len(text):,} "
        f"characters are shown. {_download_tool_hint(web_url)}]"
    )


def _drive_item_link_lines(meta: dict | None) -> list[str]:
    """The links Graph returns for a drive item.

    ``@microsoft.graph.downloadUrl`` is the pre-authenticated direct download
    (short-lived), ``webUrl`` the page a person opens and the URL the other
    Microsoft file tools take.
    """
    meta = meta or {}
    lines = []
    download_url = meta.get("@microsoft.graph.downloadUrl") or ""
    web_url = meta.get("webUrl") or ""
    if download_url:
        lines.append(f"Download URL: {download_url}")
    if web_url:
        lines.append(f"Web URL: {web_url}")
    return lines


def _with_drive_links(answer: str, meta: dict | None, *, hint: bool = False) -> str:
    """*answer* followed by the drive item's links, never glued onto an error.

    Every successful answer of the drive readers starts with ``File:``; an
    error string (Graph, argument validation) is returned unchanged. *hint*
    adds the pointer to ``download_microsoft_file`` for answers that show a
    bounded view of the file (an Excel workbook).
    """
    if not answer.startswith("File: "):
        return answer
    tail = [_download_tool_hint((meta or {}).get("webUrl") or "")] if hint else []
    tail.extend(_drive_item_link_lines(meta))
    return answer + "\n\n" + "\n".join(tail) if tail else answer


# Parse an A1 cell reference (e.g. ``B5``, ``AZ5000``) into its column letters
# and row number — used to derive a bounded sub-range of a worksheet's used
# range so we only fetch the cells we'll actually display.
_CELL_REF_RE = re.compile(r"^([A-Za-z]+)([0-9]+)$")


def _col_to_index(letters: str) -> int:
    """1-based spreadsheet column index for ``A``→1, ``Z``→26, ``AA``→27."""
    index = 0
    for ch in letters.upper():
        index = index * 26 + (ord(ch) - ord("A") + 1)
    return index


def _index_to_col(index: int) -> str:
    """Inverse of :func:`_col_to_index` (1→``A``, 27→``AA``)."""
    letters = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def _bounded_range_address(used_range_address: str, rows: int, cols: int) -> str | None:
    """Clamp a worksheet ``usedRange`` address to the display window.

    Given a Graph ``usedRange`` address (e.g. ``Sheet1!B5:AZ5000``) and the
    used row/column counts, return a worksheet-relative ``A1`` range
    (e.g. ``B5:Z64``) anchored at the used range's top-left and extended only
    far enough to cover ``min(rows, _MAX_WORKBOOK_ROWS)`` ×
    ``min(cols, _MAX_WORKBOOK_COLS)`` cells. Returns ``None`` if the address
    can't be parsed (caller falls back to the full ``usedRange``).
    """
    # Strip any ``Sheet!`` prefix — the range is requested relative to the
    # worksheet, so the cell range alone (no sheet name) is what we need.
    range_part = used_range_address.rsplit("!", 1)[-1]
    start_ref = range_part.split(":", 1)[0]
    match = _CELL_REF_RE.match(start_ref)
    if not match or rows <= 0 or cols <= 0:
        return None
    start_col_letters, start_row_str = match.group(1), match.group(2)
    start_col = _col_to_index(start_col_letters)
    start_row = int(start_row_str)
    end_col = _index_to_col(start_col + min(cols, _MAX_WORKBOOK_COLS) - 1)
    end_row = start_row + min(rows, _MAX_WORKBOOK_ROWS) - 1
    return f"{start_col_letters.upper()}{start_row}:{end_col}{end_row}"


# Matches an A1-style address accepted by the Graph ``range(address=…)``
# endpoint: a single cell ("A1", "$B$5") or a bounded cell range ("A1:B10",
# "$A$1:$B$10"). Column spans ("A:E") and row spans ("1:50") are deliberately
# NOT accepted: their extent is unknown up front, so they would bypass the
# pre-fetch clamping and pull an entire sheet's grid. Doubles as the
# allowlist that guards URL interpolation of LLM-supplied ranges — anything
# else (quotes, spaces, braces, slashes) would break out of the OData
# function-call segment or make httpx raise ``InvalidURL``. Row numbers are
# 1-based ("A0" is not a valid A1 address), so require a non-zero first digit.
_CELL_RANGE_RE = re.compile(
    r"^\$?[A-Za-z]{1,3}\$?[1-9]\d*(?::\$?[A-Za-z]{1,3}\$?[1-9]\d*)?$"
)

# Extracts the worksheet (and optional range) from an embedded Graph workbook
# path suffix — a production variant of the malformed-item_path bug
# where the LLM appends the raw Graph API route to the file path, e.g.
# ``Alerts.xlsx:/workbook/worksheets/Feuil1/usedRange`` or
# ``…/workbook/worksheets('Feuil1')/range(address='A61:E180')``.
_EMBEDDED_WORKBOOK_PATH_RE = re.compile(
    r"^workbook/worksheets(?:\('([^']+)'\)|/([^/(]+))"
    r"(?:/range\(address='([^']+)'\))?",
    re.IGNORECASE,
)


def _split_workbook_path_and_range(
    raw_path: str,
) -> tuple[str, str | None, str | None]:
    """Separate a workbook file path from any embedded worksheet/range suffix.

    LLM agents sometimes embed a worksheet name or cell range directly into
    the ``item_path`` argument using separators that are invalid for Graph
    drive-item paths (e.g. ``Reports/Alerts.xlsx!A1:E100``,
    ``Reports/Alerts.xlsx:Sheet1!A1:E100``,
    ``Reports/Alerts.xlsx?range=Sheet1!A1:E100``).  Every such form produces
    a malformed ``/drive/root:/{path}`` segment and results in a 400
    BadRequest from Graph ("Resource not found for the segment …").

    This function detects such suffixes and strips them, returning
    ``(clean_path, worksheet_or_None, range_or_None)`` so the caller can
    construct the correct Graph workbook range URL instead.  When no embedded
    suffix is detected the original path is returned with ``(None, None)``.

    Parsed cases (anything after the LAST workbook extension in the path):
    - ``!A61:E180``                 → (None, "A61:E180")
    - ``!Sheet1!A61:E180``          → ("Sheet1", "A61:E180")
    - ``:Sheet1!A61:E180``          → ("Sheet1", "A61:E180")
    - ``#Sheet1!A61:E180``          → ("Sheet1", "A61:E180")
    - ``?range=Sheet1!A61:E180``    → ("Sheet1", "A61:E180")
    - ``/Sheet1!A61:E180``          → ("Sheet1", "A61:E180")
    - ``:/workbook/worksheets/Feuil1/usedRange`` → ("Feuil1", None)

    Ambiguity guard: ``!`` and ``#`` are legal characters in SharePoint file
    names and ``/`` is the path separator, so a bare-word suffix after those
    separators (e.g. ``Report.xlsx#final``, ``Data.xlsx/notes.txt``) is left
    untouched — only a suffix that provably looks like a worksheet/range spec
    (contains a ``!`` range split, matches the A1 range shape, or follows a
    separator that is illegal in SharePoint paths: ``:`` / ``?``) is stripped.
    """
    lower = raw_path.lower()
    # Anchor on the LAST workbook extension anywhere in the path so a folder
    # whose name contains ".xlsx" doesn't shadow the real file (e.g.
    # "archive.xlsx_backup/report.xlsm!A1:B2" must split on ".xlsm").
    end_of_ext = max(
        (lower.rfind(ext) + len(ext) for ext in _WORKBOOK_EXTS if ext in lower),
        default=0,
    )
    if not end_of_ext or end_of_ext >= len(raw_path):
        return raw_path, None, None  # no workbook extension / no suffix
    sep = raw_path[end_of_ext]
    if sep not in "!:#?/":
        return raw_path, None, None  # extension is part of a longer name
    clean_path = raw_path[:end_of_ext]
    suffix = raw_path[end_of_ext:]
    # Strip "?range=" style query-param prefix as well as leading separators.
    suffix = re.sub(r"^\?range=", "", suffix, flags=re.IGNORECASE)
    suffix = suffix.lstrip("!:#?/").strip()
    if not suffix:
        # Bare trailing separator. "!" and "#" are legal filename characters,
        # so "Report.xlsx#" may be the real name — leave it untouched. ":",
        # "?", and a trailing "/" cannot be part of the name — strip them.
        if sep in "!#":
            return raw_path, None, None
        return clean_path, None, None
    if suffix.lower().startswith("workbook/"):
        # Embedded Graph API route (":/workbook/worksheets/…") — recover the
        # worksheet/range when possible. A worksheet merely NAMED "workbook…"
        # (no "/") falls through to the normal suffix handling below.
        embedded = _EMBEDDED_WORKBOOK_PATH_RE.match(suffix)
        if embedded:
            worksheet = (embedded.group(1) or embedded.group(2) or "").strip()
            cell_range = (embedded.group(3) or "").strip()
            return clean_path, worksheet or None, cell_range or None
        return clean_path, None, None
    if "!" in suffix:
        ws_part, _, range_part = suffix.partition("!")
        worksheet = ws_part.strip() or None
        cell_range = range_part.strip() or None
        # For separators that are legal in real names ("!", "#", "/"), commit
        # to the split only when the part after "!" is a valid A1 range —
        # "Report.xlsx#a!b" or "Budget.xlsx!notes!" could be legitimate file
        # names. ":" and "?" cannot appear in SharePoint paths, so the split
        # is always safe for them.
        if sep not in ":?" and not (cell_range and _CELL_RANGE_RE.match(cell_range)):
            return raw_path, None, None
        return clean_path, worksheet, cell_range
    if _CELL_RANGE_RE.match(suffix):
        return clean_path, None, suffix
    if sep in ":?":
        # ":" and "?" cannot appear in SharePoint paths, so a bare-word suffix
        # after them can only be a worksheet name.
        return clean_path, suffix, None
    return raw_path, None, None


def _encode_share_id(url: str) -> str:
    """Encode a SharePoint/OneDrive URL into a Graph ``shares`` share ID.

    Microsoft Graph can address *any* file URL the signed-in user can reach
    (a "Copy link" sharing URL **or** the plain browser address-bar URL of an
    Office document) via ``GET /shares/{shareId}/driveItem``, where the share
    ID is ``u!<base64url(url)>`` with the ``=`` padding stripped. See
    https://learn.microsoft.com/graph/api/shares-get. This is the canonical
    way to open a file in a SharePoint site by URL — it does not require
    knowing the site ID or drive ID up front.
    """
    encoded = base64.urlsafe_b64encode(url.strip().encode()).rstrip(b"=").decode()
    return f"u!{encoded}"


def _encode_drive_path(path: str) -> str:
    """Percent-encode a drive-relative path for safe interpolation into a URL.

    Graph drive items are addressed as ``…/root:/{path}`` where *path* is a
    user/agent-supplied file or folder path. Interpolating it raw is unsafe: a
    name containing ``?`` or ``#`` (SharePoint/OneDrive permit ``#``) would
    truncate the path into a query/fragment, and a literal ``%`` that isn't a
    valid ``%xx`` escape makes httpx raise ``InvalidURL``. We decode first so an
    already percent-encoded path isn't double-encoded, then re-encode, keeping
    ``/`` as the path separator.
    """
    return _url_encode(_url_decode(path), safe="/")


def _encode_drive_filename(name: str) -> str:
    """Percent-encode a filename as a SINGLE drive path segment.

    Same decode-then-encode contract as :func:`_encode_drive_path`, but with
    ``safe=""`` so a ``/`` in the (user/LLM-controlled) filename is encoded
    too — the name can never inject extra path segments into the Graph
    ``…/root:/{path}:/content`` addressing. Use this for filenames; reserve
    ``_encode_drive_path`` for explicit folder paths where ``/`` is the
    intended separator.
    """
    return _url_encode(_url_decode(name), safe="")

# ── OneDrive / Files Tools ─────────────────────────────────────────

# What the URL resolvers read of a driveItem: its identity and links, and its
# ``file`` or ``folder`` facet (a folder is listed, never downloaded).
_SHARE_ITEM_SELECT = "id,name,size,file,folder,parentReference,webUrl,@microsoft.graph.downloadUrl"
# What a folder listing reads of each item.
_FOLDER_ITEM_SELECT = "id,name,size,file,folder,lastModifiedDateTime,webUrl"
_FOLDER_PAGE_DEFAULT = 50
_FOLDER_PAGE_MAX = 200
# Query parameters a listing sets itself, left out of the page token.
_OWN_PAGE_PARAMS = frozenset({"$top", "$select"})
# A SharePoint / OneDrive sharing link (``https://host/:f:/s/site/<token>``,
# ``/:w:/g/personal/<user>/<token>``, ``/:x:/r/sites/...``): its path names
# the link, not the item, so a path appended to it is resolved from the item
# the link opens.
_SHARING_LINK_PATH_RE = re.compile(r"^/:[a-z]:/", re.IGNORECASE)
# The fewest path segments after the ``/:x:/`` marker a sharing link has
# (``s/<site>/<token>``), and the most link / path readings tried.
_MIN_SHARING_LINK_SEGMENTS = 3
_MAX_SHARING_LINK_SPLITS = 4
_UNRESOLVED_URL = (
    "Error: could not resolve a file from that URL. Make sure it "
    "points to a SharePoint/OneDrive file (a sharing link or the "
    "document's browser URL) and that you have access to it."
)


@dataclass(frozen=True)
class _SharingLink:
    """A sharing link, split where a path is appended to it."""

    origin: str
    marker: str
    #: The path segments after the marker, as given, with a path appended
    #: after the query string (``...?e=abc/Sub``) moved here.
    segments: tuple[str, ...]
    query: str

    def url(self, segments: tuple[str, ...]) -> str:
        query = f"?{self.query}" if self.query else ""
        return f"{self.origin}{self.marker}/{'/'.join(segments)}{query}"


def _parse_sharing_link(url: str) -> _SharingLink | None:
    parts = urlsplit(url.strip())
    match = _SHARING_LINK_PATH_RE.match(parts.path)
    if not match or not parts.netloc:
        return None
    query, _, appended = parts.query.partition("/")
    segments = [s for s in parts.path[match.end() :].split("/") if s]
    segments += [s for s in appended.split("/") if s]
    return _SharingLink(
        f"{parts.scheme}://{parts.netloc}", parts.path[: match.end() - 1], tuple(segments), query
    )


def _sharing_link_splits(url: str) -> list[tuple[str, str]]:
    """``(link, relative path)`` readings of *url*, shortest link first, when
    it is a sharing link a path may have been appended to; empty otherwise."""
    link = _parse_sharing_link(url)
    if link is None:
        return []
    splits: list[tuple[str, str]] = []
    for n in range(_MIN_SHARING_LINK_SEGMENTS, len(link.segments) + 1):
        base = link.url(link.segments[:n])
        if n == len(link.segments) and base == url.strip():
            break
        splits.append((base, "/".join(link.segments[n:])))
    return splits[:_MAX_SHARING_LINK_SPLITS]


@dataclass(frozen=True)
class _DriveTarget:
    """The driveItem a SharePoint / OneDrive URL names."""

    #: ``{GRAPH}/drives/{driveId}/items/{itemId}``, the base of its
    #: ``/content``, ``/children`` and ``/workbook`` sub-resources.
    item_base: str
    meta: dict
    #: The ``/shares`` id that opened it when the URL is its own link; None
    #: when it was reached by a path under a shared folder.
    share_id: str | None


def _is_folder(meta: dict | None) -> bool:
    return (meta or {}).get("folder") is not None


def _drive_target(item: dict, share_id: str | None) -> tuple[_DriveTarget | None, str | None]:
    drive_id = (item.get("parentReference") or {}).get("driveId")
    item_id = item.get("id")
    if not drive_id or not item_id:
        return None, _UNRESOLVED_URL
    return _DriveTarget(f"{_BASE_URL}/drives/{drive_id}/items/{item_id}", item, share_id), None


async def _get_shared_item(
    ctx: MicrosoftContext, client: AutoRefreshClient, share_id: str, *, probe: bool
) -> tuple[dict | None, str | None]:
    """``GET /shares/{shareId}/driveItem``; a *probe* that another reading
    may still resolve keeps its 400 / 403 / 404 out of the warning log."""
    resp, err = await ctx.request(
        client,
        "get",
        f"{_BASE_URL}/shares/{share_id}/driveItem",
        _PROVIDER,
        "resolve shared file URL",
        params={"$select": _SHARE_ITEM_SELECT},
        **({"expected_statuses": {400, 403, 404}} if probe else {}),
    )
    if err:
        return None, err
    return resp.json(), None


async def _resolve_relative(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    base: _DriveTarget,
    base_url: str,
    relative: str,
) -> tuple[_DriveTarget | None, str | None]:
    """The item at *relative* under the shared folder *base* (``base_url``)."""
    name = base.meta.get("name") or "the shared item"
    shown = _url_decode(relative)
    if not _is_folder(base.meta):
        return None, (
            f"Error: '{name}' is a file, so it holds no '{shown}'. Read it with "
            f'get_microsoft_file_by_url(url="{base_url}").'
        )
    resp, err = await ctx.request(
        client,
        "get",
        f"{base.item_base}:/{_encode_drive_path(relative)}",
        _PROVIDER,
        "resolve a path in the shared folder",
        params={"$select": _SHARE_ITEM_SELECT},
        expected_statuses={404},
    )
    if err:
        if resp is not None and resp.status_code == 404:
            return None, (
                f"Error: the shared folder '{name}' holds no '{shown}'. List it with "
                f'list_microsoft_folder_by_url(url="{base_url}") to see the names of its items.'
            )
        return None, err
    return _drive_target(resp.json(), None)


async def _resolve_drive_url(
    ctx: MicrosoftContext, client: AutoRefreshClient, url: str
) -> tuple[_DriveTarget | None, str | None]:
    """Resolve a SharePoint/OneDrive URL to the driveItem it names.

    The URL is encoded with the ``u!<base64url>`` share scheme and looked up
    with ``GET /shares/{shareId}/driveItem``, which opens a sharing link and a
    browser URL alike. When that fails and the URL is a sharing link with a
    path appended (``<link>/Sub/report.docx``, as a folder listing names the
    items under a shared folder), the link is resolved alone and the path
    from the folder it opens (``/drives/{d}/items/{i}:/{path}``). When no
    reading resolves, the error is the first lookup's.
    """
    splits = _sharing_link_splits(url)
    share_id = _encode_share_id(url)
    item, err = await _get_shared_item(ctx, client, share_id, probe=bool(splits))
    if item is not None:
        return _drive_target(item, share_id)
    for base_url, relative in splits:
        base_share = _encode_share_id(base_url)
        base_item, _base_err = await _get_shared_item(ctx, client, base_share, probe=True)
        if base_item is None:
            continue
        base, base_err = _drive_target(base_item, base_share)
        if base is None or not relative:
            return base, base_err
        return await _resolve_relative(ctx, client, base, base_url, relative)
    return None, err


async def _resolve_share_url(
    ctx: MicrosoftContext, client: AutoRefreshClient, url: str
) -> tuple[str | None, dict | None, str | None]:
    """``(item_base, meta, None)`` for the driveItem *url* names (see
    :func:`_resolve_drive_url`), or ``(None, None, error_string)``."""
    target, err = await _resolve_drive_url(ctx, client, url)
    if target is None:
        return None, None, err or _UNRESOLVED_URL
    return target.item_base, target.meta, None


def _folder_item_url(folder_url: str, item: dict) -> str:
    """The URL the URL tools take for *item*, an item of the folder
    *folder_url* names: under a sharing link, the link with the item's name
    appended, so it opens through the share as the folder did; under any
    other URL, the item's web URL."""
    name = _url_encode(item.get("name") or "", safe="")
    link = _parse_sharing_link(folder_url)
    if link is not None:
        return link.url((*link.segments, name))
    if item.get("webUrl"):
        return item["webUrl"]
    parts = urlsplit(folder_url.strip())
    return urlunsplit(parts._replace(path=f"{parts.path.rstrip('/')}/{name}", fragment=""))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _folder_entry(folder_url: str, item: dict) -> str:
    name = item.get("name") or "(unnamed)"
    size = item.get("size")
    if _is_folder(item):
        child_count = (item.get("folder") or {}).get("childCount")
        details = [_plural(child_count, "item")] if isinstance(child_count, int) else []
        if isinstance(size, int):
            details.append(f"{size} bytes")
        head = f"[Folder] {name}" + (f" ({', '.join(details)})" if details else "")
    else:
        head = f"[File] {name} ({size or 0} bytes)"
    lines = [head]
    if item.get("lastModifiedDateTime"):
        lines.append(f"  Modified: {item['lastModifiedDateTime']}")
    url = _folder_item_url(folder_url, item)
    lines.append(f"  URL: {url}")
    web_url = item.get("webUrl") or ""
    if web_url and web_url != url:
        lines.append(f"  Web URL: {web_url}")
    return "\n".join(lines)


def _next_page_token(next_link: object) -> str:
    """The page token of a listing's ``@odata.nextLink``: its query, but for
    the parameters the listing sets itself. Never the link: a token cannot
    send the next request anywhere but the folder's own items."""
    if not isinstance(next_link, str) or not next_link:
        return ""
    pairs = parse_qsl(urlsplit(next_link).query, keep_blank_values=True)
    return urlencode([(k, v) for k, v in pairs if k not in _OWN_PAGE_PARAMS], safe="$")


def _page_params(page_token: str | None) -> dict[str, str]:
    token = (page_token or "").strip()
    if not token:
        return {}
    if "=" not in token:
        return {"$skiptoken": token}
    return {k: v for k, v in parse_qsl(token, keep_blank_values=True) if k not in _OWN_PAGE_PARAMS}


async def _list_folder(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    target: _DriveTarget,
    url: str,
    *,
    count: object = _FOLDER_PAGE_DEFAULT,
    page_token: str | None = None,
) -> str:
    """One page of the items of the folder *target*, given by *url*.

    The items of a folder the URL's own link opens are read through the
    share (``/shares/{id}/driveItem/children``), then by drive path; those of
    a folder under it by drive path. Each item carries the URL the URL tools
    take as is (:func:`_folder_item_url`).
    """
    url = url.strip()
    size = max(1, min(_coerce_int(count, _FOLDER_PAGE_DEFAULT), _FOLDER_PAGE_MAX))
    params = {"$top": str(size), "$select": _FOLDER_ITEM_SELECT, **_page_params(page_token)}
    routes = [f"{target.item_base}/children"]
    if target.share_id:
        routes.insert(0, f"{_BASE_URL}/shares/{target.share_id}/driveItem/children")
    data: dict = {}
    err: str | None = None
    for index, route in enumerate(routes):
        probe = index < len(routes) - 1
        resp, err = await ctx.request(
            client,
            "get",
            route,
            _PROVIDER,
            "list the folder's items",
            params=params,
            **({"expected_statuses": {400, 403, 404}} if probe else {}),
        )
        if not err and resp is not None:
            data = resp.json() or {}
            break
    if err:
        return err

    meta = target.meta
    total = (meta.get("folder") or {}).get("childCount")
    head = f"Folder: {meta.get('name') or 'the folder'}"
    if isinstance(total, int):
        head += f" ({_plural(total, 'item')})"
    lines = [head]
    if meta.get("webUrl"):
        lines.append(f"Web URL: {meta['webUrl']}")
    items = [item for item in data.get("value") or [] if isinstance(item, dict)]
    if not items:
        lines.append("No more items." if page_token else "The folder is empty.")
        return "\n".join(lines)
    lines += ["", "\n---\n".join(_folder_entry(url, item) for item in items), ""]
    token = _next_page_token(data.get("@odata.nextLink"))
    if token:
        lines.append(f'More items: list_microsoft_folder_by_url(url="{url}", page_token="{token}")')
    lines.append(
        "Pass an item's URL as is to get_microsoft_file_by_url (read a file), "
        "download_microsoft_file (a file's bytes) or list_microsoft_folder_by_url (a subfolder)."
    )
    return "\n".join(lines)


async def _read_workbook(
    ctx: MicrosoftContext, client: AutoRefreshClient, item_base: str, file_name: str
) -> str:
    """Read an Excel workbook's worksheets as tab-separated text.

    Uses the Graph Excel API (``/workbook/worksheets``). For each sheet it
    first reads ``usedRange`` **metadata only** (``address`` + row/column
    counts, no ``text``), then fetches just the bounded display window via
    ``range(address=…)`` — so a huge worksheet doesn't return its entire
    cell grid as JSON only for us to truncate it to 60×25 client-side
    (avoids the timeouts / bandwidth / memory of pulling the full grid).
    Output is capped per worksheet and across worksheets so a large model
    never floods the context window.
    """
    resp, err = await ctx.request(
        client,
        "get",
        f"{item_base}/workbook/worksheets",
        _PROVIDER,
        "list workbook worksheets",
        params={"$select": "id,name,position"},
    )
    if err:
        return err
    worksheets = resp.json().get("value", [])
    if not worksheets:
        return f"File: {file_name} (Excel workbook)\nThe workbook has no worksheets."

    sections: list[str] = []
    for ws in worksheets[:_MAX_WORKBOOK_SHEETS]:
        ws_name = ws.get("name", "Sheet")
        # Graph worksheet ids are wrapped in curly braces (e.g. "{...}"),
        # which httpx rejects unescaped in a URL path. URL-encode the id
        # (falling back to the name) before interpolating it; both are valid
        # Graph worksheet keys. Skip the sheet if neither is present.
        ws_key = (ws.get("id") or "").strip() or (ws.get("name") or "").strip()
        if not ws_key:
            sections.append("## (unnamed worksheet)\n(skipped: no id or name)")
            continue
        ws_ref = _url_encode(ws_key, safe="")
        # 1. Cheap metadata only (no ``text``) — gives the used-range
        #    address + true row/column counts without pulling the grid.
        meta_resp, meta_err = await ctx.request(
            client,
            "get",
            f"{item_base}/workbook/worksheets/{ws_ref}/usedRange",
            _PROVIDER,
            "read worksheet",
            params={"$select": "address,rowCount,columnCount"},
        )
        if meta_err:
            sections.append(f"## {ws_name}\n({meta_err})")
            continue
        meta = meta_resp.json()
        row_count = meta.get("rowCount", 0)
        col_count = meta.get("columnCount", 0)
        if not row_count or not col_count:
            sections.append(f"## {ws_name}\n(empty)")
            continue
        # 2. Fetch only the bounded display window. If the used-range
        #    address can't be parsed, fall back to the full ``usedRange``
        #    text so we never lose data on an unexpected address shape.
        bounded = _bounded_range_address(
            meta.get("address", ""), row_count, col_count
        )
        if bounded:
            text_path = (
                f"{item_base}/workbook/worksheets/{ws_ref}"
                f"/range(address='{bounded}')"
            )
        else:
            text_path = f"{item_base}/workbook/worksheets/{ws_ref}/usedRange"
        range_resp, range_err = await ctx.request(
            client,
            "get",
            text_path,
            _PROVIDER,
            "read worksheet range",
            params={"$select": "text"},
        )
        if range_err:
            sections.append(f"## {ws_name}\n({range_err})")
            continue
        rows = range_resp.json().get("text") or []
        if not rows:
            sections.append(f"## {ws_name}\n(empty)")
            continue
        lines = [
            "\t".join(str(cell) for cell in row[:_MAX_WORKBOOK_COLS])
            for row in rows[:_MAX_WORKBOOK_ROWS]
        ]
        note = ""
        if row_count > _MAX_WORKBOOK_ROWS or col_count > _MAX_WORKBOOK_COLS:
            note = (
                f" (showing {min(row_count, _MAX_WORKBOOK_ROWS)}/{row_count} "
                f"rows × {min(col_count, _MAX_WORKBOOK_COLS)}/{col_count} cols)"
            )
        sections.append(f"## {ws_name}{note}\n" + "\n".join(lines))

    extra = ""
    if len(worksheets) > _MAX_WORKBOOK_SHEETS:
        hidden = len(worksheets) - _MAX_WORKBOOK_SHEETS
        extra = f"\n\n(+{hidden} more worksheet(s) not shown)"
    return (
        f"File: {file_name} (Excel workbook)\n\n" + "\n\n".join(sections) + extra
    )


async def _read_workbook_worksheet(
    ctx: MicrosoftContext,
    client: AutoRefreshClient,
    item_base: str,
    file_name: str,
    worksheet: str | None,
    cell_range: str | None,
) -> str:
    """Read a targeted worksheet (and optional cell range) from an Excel workbook.

    Constructs the correct Microsoft Graph workbook range URL instead of
    string-concatenating the range onto the drive-item path (which always
    results in a malformed URL and a 400 BadRequest from Graph).

    - *worksheet* + *cell_range*: ``GET …/worksheets/{ws}/range(address='{range}')``
    - *worksheet* only (no range): reads the bounded used range for that sheet.
    - Neither provided: falls back to ``_read_workbook`` (all sheets).
    """
    if not worksheet:
        return await _read_workbook(ctx, client, item_base, file_name)

    ws_ref = _url_encode(worksheet.strip(), safe="")

    if cell_range:
        # Strip any sheet-name prefix from the range address (e.g. "Sheet1!A1:B10"
        # → "A1:B10") — the worksheet is already encoded in the URL path.
        clean_range = cell_range.rsplit("!", 1)[-1].strip()
        # Allowlist-validate before interpolating into the OData function
        # segment: the value is LLM-controlled and a quote / space / brace
        # would break out of range(address='…') or trip httpx InvalidURL.
        if not _CELL_RANGE_RE.match(clean_range):
            return (
                f"Error: invalid cell_range {cell_range!r}. Use an A1-style "
                "cell or bounded range like 'B5' or 'A1:E100' without any "
                "sheet-name prefix (pass the sheet via 'worksheet'). Column "
                "spans ('A:E') and row spans ('1:50') are not supported — "
                "give explicit start and end cells instead."
            )
        # Clamp an oversized request to the display window (same
        # metadata-free pattern as _read_workbook) so a huge range doesn't
        # pull its whole grid only to be truncated to 60×25 client-side.
        # Work on the "$"-stripped form throughout: absolute refs like
        # "$A$1:$Y$5000" pass the allowlist but _bounded_range_address /
        # _CELL_REF_RE can't parse "$A$1", which would silently skip the
        # clamp while the output note claims it was applied.
        plain_range = clean_range.replace("$", "")
        req_rows = req_cols = 0
        start_ref, _, end_ref = plain_range.partition(":")
        m_start = _CELL_REF_RE.match(start_ref)
        m_end = _CELL_REF_RE.match(end_ref)
        if m_start and m_end:
            req_rows = abs(int(m_end.group(2)) - int(m_start.group(2))) + 1
            req_cols = (
                abs(_col_to_index(m_end.group(1)) - _col_to_index(m_start.group(1)))
                + 1
            )
        fetch_range = clean_range
        note = ""
        if req_rows > _MAX_WORKBOOK_ROWS or req_cols > _MAX_WORKBOOK_COLS:
            fetch_range = (
                _bounded_range_address(plain_range, req_rows, req_cols)
                or clean_range
            )
            note = (
                f" (showing {min(req_rows, _MAX_WORKBOOK_ROWS)}/{req_rows} "
                f"rows × {min(req_cols, _MAX_WORKBOOK_COLS)}/{req_cols} cols "
                "of the requested range)"
            )
        range_resp, range_err = await ctx.request(
            client,
            "get",
            f"{item_base}/workbook/worksheets/{ws_ref}/range(address='{fetch_range}')",
            _PROVIDER,
            "read worksheet range",
            params={"$select": "text"},
        )
        if range_err:
            return range_err
        rows = range_resp.json().get("text") or []
        if not rows:
            return (
                f"File: {file_name} (Excel workbook)\n"
                f"## {worksheet} [{clean_range}]\n(empty)"
            )
        lines = [
            "\t".join(str(cell) for cell in row[:_MAX_WORKBOOK_COLS])
            for row in rows[:_MAX_WORKBOOK_ROWS]
        ]
        return (
            f"File: {file_name} (Excel workbook)\n\n"
            f"## {worksheet} [{clean_range}]{note}\n" + "\n".join(lines)
        )

    # Worksheet specified but no range: read the bounded used range for that sheet
    # (same metadata + bounded-range pattern as _read_workbook, but for one sheet).
    meta_resp, meta_err = await ctx.request(
        client,
        "get",
        f"{item_base}/workbook/worksheets/{ws_ref}/usedRange",
        _PROVIDER,
        "read worksheet",
        params={"$select": "address,rowCount,columnCount"},
    )
    if meta_err:
        return meta_err
    meta = meta_resp.json()
    row_count = meta.get("rowCount", 0)
    col_count = meta.get("columnCount", 0)
    if not row_count or not col_count:
        return f"File: {file_name} (Excel workbook)\n## {worksheet}\n(empty)"
    bounded = _bounded_range_address(meta.get("address", ""), row_count, col_count)
    if bounded:
        text_path = (
            f"{item_base}/workbook/worksheets/{ws_ref}/range(address='{bounded}')"
        )
    else:
        text_path = f"{item_base}/workbook/worksheets/{ws_ref}/usedRange"
    range_resp, range_err = await ctx.request(
        client,
        "get",
        text_path,
        _PROVIDER,
        "read worksheet range",
        params={"$select": "text"},
    )
    if range_err:
        return range_err
    rows = range_resp.json().get("text") or []
    if not rows:
        return f"File: {file_name} (Excel workbook)\n## {worksheet}\n(empty)"
    lines = [
        "\t".join(str(cell) for cell in row[:_MAX_WORKBOOK_COLS])
        for row in rows[:_MAX_WORKBOOK_ROWS]
    ]
    note = ""
    if row_count > _MAX_WORKBOOK_ROWS or col_count > _MAX_WORKBOOK_COLS:
        note = (
            f" (showing {min(row_count, _MAX_WORKBOOK_ROWS)}/{row_count} "
            f"rows × {min(col_count, _MAX_WORKBOOK_COLS)}/{col_count} cols)"
        )
    return (
        f"File: {file_name} (Excel workbook)\n\n"
        f"## {worksheet}{note}\n" + "\n".join(lines)
    )


async def _read_drive_item(
    ctx: MicrosoftContext, client: AutoRefreshClient, item_base: str, meta: dict
) -> str:
    """Render a drive item's content from its addressing base + metadata.

    Excel workbooks are read cell-by-cell via the Excel API, small text
    files are returned inline, Word / PowerPoint / PDF documents have
    their text extracted server-side, and everything else (binary or
    large) is described with a pointer to ``download_microsoft_file``.
    Shared by the OneDrive, SharePoint, and open-by-URL reading tools so
    they behave identically.
    """
    file_name = meta.get("name", "")
    file_size = meta.get("size", 0)
    mime_type = meta.get("file", {}).get("mimeType", "")
    web_url = meta.get("webUrl", "")
    lower_name = file_name.lower()

    if _is_folder(meta):
        lister = (
            f'list_microsoft_folder_by_url(url="{web_url}")'
            if web_url
            else "list_microsoft_folder_by_url and its URL"
        )
        return "\n".join(
            [f"'{file_name}' is a folder, not a file: list its items with {lister}.", *_drive_item_link_lines(meta)]
        )

    if lower_name.endswith(_WORKBOOK_EXTS):
        return _with_drive_links(
            await _read_workbook(ctx, client, item_base, file_name), meta, hint=True
        )

    # Every answer carries the links Graph returned - the inline ones too:
    # agents and assignment prompts hand them on.
    link_lines = _drive_item_link_lines(meta)

    def _not_inline(reason: str) -> str:
        parts = [
            f"File: {file_name} ({file_size} bytes, "
            f"{mime_type or 'unknown type'})",
            f"This file is {reason}.",
            _download_tool_hint(web_url),
            *link_lines,
        ]
        return "\n".join(parts)

    if lower_name.endswith(_DOCUMENT_EXTS):
        if file_size > _MAX_DOCUMENT_BYTES:
            return _not_inline("too large to extract inline")
        content_resp, err = await ctx.request(
            client,
            "get",
            f"{item_base}/content",
            _PROVIDER,
            "download document",
            follow_redirects=True,
        )
        if err:
            return err
        data = content_resp.content or b""
        if len(data) > _MAX_DOCUMENT_BYTES:
            return _not_inline("too large to extract inline")
        from app.services.chunking import extract_text_from_file_async

        full_text = (await extract_text_from_file_async(data, file_name)).strip()
        if not full_text:
            return _not_inline("a document with no extractable text")
        answer = f"File: {file_name} ({file_size} bytes)\nContent:\n" + _cut_inline(
            full_text, _MAX_DOCUMENT_CHARS, web_url
        )
        return _with_drive_links(answer, meta)

    is_text = any(
        mime_type.startswith(prefix) for prefix in _TEXT_MIME_PREFIXES
    ) or lower_name.endswith(_TEXT_EXTS)

    if not is_text:
        return _not_inline("not text-readable inline")
    if file_size > _MAX_TEXT_BYTES:
        return _not_inline("too large to read inline")

    content_resp, err = await ctx.request(
        client,
        "get",
        f"{item_base}/content",
        _PROVIDER,
        "download file content",
        follow_redirects=True,
    )
    if err:
        return err
    return _with_drive_links(
        f"File: {file_name} ({file_size} bytes)\nContent:\n"
        + _cut_inline(content_resp.text, _MAX_CONTENT_CHARS, web_url),
        meta,
    )
