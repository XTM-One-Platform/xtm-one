"""SharePoint tools: find sites (including the sites behind the user's groups) and read their files."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode
from urllib.parse import urlencode, urlsplit

from app.integrations.http import AutoRefreshClient
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _BASE_URL,
    _MICROSOFT_CONCURRENCY,
    _PROVIDER,
    MicrosoftContext,
)
from ._drive import (
    _SHARING_LINK_PATH_RE,
    _WORKBOOK_EXTS,
    _authorship_lines,
    _encode_drive_path,
    _encode_share_id,
    _read_drive_item,
    _read_workbook_worksheet,
    _split_workbook_path_and_range,
    _with_drive_links,
)

# Max ``@odata.nextLink`` pages to walk when enumerating the user's group
# memberships (200 per page) — bounds the walk for users in very many groups.
_MAX_GROUP_MEMBERSHIP_PAGES = 10

# Group→site resolution is done in batches of this many groups so we never
# materialise thousands of pending tasks at once for a user in very many
# groups. The total work is already bounded by ``_MAX_GROUP_MEMBERSHIP_PAGES``;
# this just caps how many resolutions are in flight per batch (each batch is
# further throttled to ``_MICROSOFT_CONCURRENCY`` concurrent Graph requests).
_GROUP_SITE_BATCH = 50

# The SharePoint managed paths a site's URL starts with: ``/sites/<name>``,
# ``/teams/<name>`` and OneDrive's ``/personal/<user>``.
_MANAGED_PATHS = frozenset({"sites", "teams", "personal"})
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
_SITE_FORMS = (
    "the site id from list_microsoft_sharepoint_sites, the path form "
    "'hostname:/sites/name', or the site's URL ('https://hostname/sites/name')"
)


@dataclass(frozen=True)
class _SiteAddress:
    """A site given by its hostname and server-relative path ("" for the root site)."""

    hostname: str
    path: str


def _parse_site_address(site_id: str) -> tuple[_SiteAddress | None, str | None]:
    """``(address, None)`` for a site given in the Graph path form
    (``hostname:/sites/name``, a trailing colon tolerated) or as its URL,
    ``(None, None)`` for a site id, ``(None, error)`` for neither."""
    value = site_id.strip()
    if re.match(r"^https?://", value, re.IGNORECASE):
        parts = urlsplit(value)
        if _SHARING_LINK_PATH_RE.match(parts.path):
            return None, (
                f"Error: '{value}' is a sharing link, not a site: list the folder it opens with "
                f'list_microsoft_folder_by_url(url="{value}").'
            )
        hostname = parts.hostname or ""
        segments = [_url_decode(s) for s in parts.path.split("/") if s]
        if len(segments) >= 2 and segments[0].lower() in _MANAGED_PATHS:
            segments = segments[:2]
        path = "/".join(segments)
    elif ":/" in value:
        hostname, _, path = value.partition(":/")
        hostname, path = hostname.strip(), path.strip().rstrip(":").strip("/")
    else:
        return None, None
    if not _HOSTNAME_RE.match(hostname):
        return None, f"Error: '{value}' does not name a SharePoint site; give {_SITE_FORMS}."
    return _SiteAddress(hostname, path), None


def _site_not_accessible(site: str, detail: str) -> str:
    return (
        "Error: site not accessible, list the shared folder with list_microsoft_folder_by_url. "
        f"The connected account cannot open the SharePoint site '{site}' "
        f"({detail.removeprefix('Error: ').rstrip('.')}). A folder shared with it on that site is "
        "listed by passing the folder's link to list_microsoft_folder_by_url. "
        "list_microsoft_sharepoint_sites shows the sites it can open."
    )


async def _site_drive_base(
    ctx: MicrosoftContext, client: AutoRefreshClient, site_id: str
) -> tuple[str | None, bool, str | None]:
    """``({GRAPH}/sites/{id}/drive, by_address, None)`` for *site_id*, or
    ``(None, by_address, error)``. A site id is used as given; a path form or
    URL (*by_address*) is resolved to the site's id first, since Graph takes
    no second path segment (``root:/...``) after a path-addressed site."""
    value = (site_id or "").strip()
    if not value:
        return None, False, f"Error: provide the site_id: {_SITE_FORMS}."
    address, err = _parse_site_address(value)
    if err:
        return None, False, err
    if address is None:
        return f"{_BASE_URL}/sites/{value}/drive", False, None
    lookup = f"{_BASE_URL}/sites/{address.hostname}"
    if address.path:
        lookup += f":/{_encode_drive_path(address.path)}"
    resp, err = await ctx.request(
        client,
        "get",
        lookup,
        _PROVIDER,
        "resolve the SharePoint site",
        params={"$select": "id,displayName,webUrl"},
        expected_statuses={400, 403, 404},
    )
    if err:
        if resp is not None and resp.status_code in (400, 403, 404):
            return None, True, _site_not_accessible(value, err)
        return None, True, err
    resolved = (resp.json() or {}).get("id")
    if not resolved:
        return None, True, _site_not_accessible(value, "Microsoft Graph returned no site id")
    return f"{_BASE_URL}/sites/{resolved}/drive", True, None


# A library's items are read 100 per Graph page; a filtered listing walks at
# most this many pages per call before it hands back a page_token.
_LIST_ITEMS_PAGE = 100
_MAX_LIST_ITEM_PAGES = 20
_SCOPE_HINT = (
    "Reading list item fields needs the Sites.Read.All permission (delegated or "
    "application) on the Microsoft integration, and the account must be able to "
    "open the library."
)


def _fields_refused(status: int | None, err: str, what: str) -> str:
    """The error answered for a refused or missing list item / library read."""
    detail = err.removeprefix("Error: ").rstrip(".")
    if status in (401, 403):
        return f"Error: Microsoft Graph refused to read {what} ({detail}). {_SCOPE_HINT}"
    if status == 404:
        return (
            f"Error: {what} was not found ({detail}). A file must sit in a SharePoint "
            "document library (give the library when it is not the site's default one); "
            "a library is named by its display name or its list id."
        )
    return err


def _field_text(value: object) -> str:
    """One column value as text: a multi-value column joined, an object as JSON."""
    if isinstance(value, list):
        return "; ".join(_field_text(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _wanted_columns(columns: str | None) -> list[str]:
    return [c.strip() for c in (columns or "").split(",") if c.strip()]


def _column_key(fields: dict, column: str) -> str | None:
    """The key of *column* in *fields*, matched exactly then case-insensitively."""
    if column in fields:
        return column
    folded = column.casefold()
    return next((k for k in fields if k.casefold() == folded), None)


def _value_matches(value: object, wanted: str) -> bool:
    """True when *value* (or one of its values, for a multi-value column) equals *wanted*."""
    if isinstance(value, list):
        return any(_value_matches(v, wanted) for v in value)
    return _field_text(value).strip().casefold() == wanted.strip().casefold()


def _format_fields(fields: dict, columns: list[str], indent: str) -> list[str]:
    """The ``name: value`` lines of *fields*; with *columns*, those columns only,
    an absent one said so."""
    if columns:
        lines = []
        for column in columns:
            key = _column_key(fields, column)
            if key is None:
                lines.append(f"{indent}{column}: (not set on this item)")
            else:
                lines.append(f"{indent}{key}: {_field_text(fields[key])}")
        return lines
    return [f"{indent}{key}: {_field_text(value)}" for key, value in fields.items() if not key.startswith("@odata")]


async def _site_base(ctx: MicrosoftContext, client: AutoRefreshClient, site_id: str) -> tuple[str | None, str | None]:
    """``({GRAPH}/sites/{id}, None)`` for *site_id* in any form ``_site_drive_base`` takes."""
    drive_base, _by_address, err = await _site_drive_base(ctx, client, site_id)
    if err or not drive_base:
        return None, err or f"Error: provide the site_id: {_SITE_FORMS}."
    return drive_base.removesuffix("/drive"), None


def _list_ref(library: str) -> str:
    return _url_encode(library.strip(), safe="")


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the sharepoint tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request

    # ── SharePoint Sites Tools ─────────────────────────────────────────

    def _format_sites(sites: list[dict], limit: int) -> str:
        total = len(sites)
        shown = sites[:limit]
        if not shown:
            return (
                "No SharePoint sites found. If you have a link to a specific "
                "file, use get_microsoft_file_by_url to read it directly; to a "
                "folder, list it with list_microsoft_folder_by_url."
            )
        lines: list[str] = []
        for s in shown:
            desc = (s.get("description") or "")[:100]
            lines.append(
                f"{s.get('displayName', s.get('name', 'Untitled'))}\n"
                f"  Site ID: {s.get('id', '')}\n"
                f"  URL: {s.get('webUrl', '')}"
                + (f"\n  Description: {desc}" if desc else "")
            )
        # Report the total discovered, not just the returned subset, so the
        # no-query aggregation path doesn't undercount when more sites exist.
        header = f"Found {total} site(s)"
        if total > len(shown):
            header += f" (showing {len(shown)})"
        return f"{header}:\n\n" + "\n---\n".join(lines)

    async def _collect_group_sites(client: AutoRefreshClient) -> list[dict]:
        """Return the SharePoint sites backing the user's M365 groups / Teams.

        ``/sites?search=*`` only surfaces sites in the search index, which
        routinely misses sites the user can reach through group membership.
        This walks ``/me/memberOf`` (groups only), keeps the Microsoft 365 /
        Teams groups that have a SharePoint site, and resolves each one's
        root site with bounded concurrency. Requires ``GroupMember.Read.All``
        (for the membership read) plus the already-granted ``Sites.Read.All``.
        """
        # Walk every page of the membership list. ``/me/memberOf`` is paginated
        # (200 per page here); without following ``@odata.nextLink`` a user in
        # >200 groups would silently lose the group-backed sites beyond page 1.
        groups: list[dict] = []
        next_url: str | None = f"{_BASE_URL}/me/memberOf/microsoft.graph.group"
        params: dict[str, str] | None = {
            "$select": "id,displayName,groupTypes,resourceProvisioningOptions",
            "$top": "200",
        }
        for _page in range(_MAX_GROUP_MEMBERSHIP_PAGES):
            resp, err = await _throttled_request(
                client,
                "get",
                next_url,
                _PROVIDER,
                "list group memberships",
                params=params,
                expected_statuses={403},
            )
            if err or resp is None:
                # A first-page failure (e.g. missing GroupMember.Read.All) means
                # no group sites; a later-page failure keeps what we gathered.
                if not groups:
                    return []
                break
            data = resp.json()
            groups.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
            params = None  # the nextLink already encodes the query params
            if not next_url:
                break
        site_groups = [
            g
            for g in groups
            if g.get("id")
            and (
                "Unified" in (g.get("groupTypes") or [])
                or "Team" in (g.get("resourceProvisioningOptions") or [])
            )
        ]

        sem = asyncio.Semaphore(_MICROSOFT_CONCURRENCY)

        async def _group_site(group_id: str) -> dict | None:
            async with sem:
                site_resp, site_err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/groups/{group_id}/sites/root",
                    _PROVIDER,
                    "get group site",
                    params={"$select": "id,displayName,name,webUrl,description"},
                    expected_statuses={403, 404},
                )
                if site_err or site_resp is None:
                    return None
                return site_resp.json()

        # Resolve every eligible group's root site (membership is already
        # page-capped above), processing in bounded batches so we never spawn
        # thousands of tasks at once instead of silently dropping groups past
        # an arbitrary cap.
        collected: list[dict] = []
        for start in range(0, len(site_groups), _GROUP_SITE_BATCH):
            batch = site_groups[start : start + _GROUP_SITE_BATCH]
            results = await asyncio.gather(*(_group_site(g["id"]) for g in batch))
            collected.extend(s for s in results if s and s.get("id"))
        return collected

    async def list_microsoft_sharepoint_sites(query: str = "", count: int = 10) -> str:
        """List or search SharePoint sites the user has access to.

        With a ``query`` this searches sites by keyword. Without a query it
        enumerates the sites the user can access by merging three sources and
        de-duplicating by site id:

        * the tenant search index (``/sites?search=*``),
        * sites the user follows (``/me/followedSites``), and
        * the SharePoint sites backing the Microsoft 365 groups / Teams the
          user belongs to (``/me/memberOf`` → ``/groups/{id}/sites/root``),
          which catches sites the search index misses.
        """
        # Clamp to a sane positive range: a ``count`` <= 0 would make
        # ``_format_sites`` slice to an empty list and wrongly report "no sites
        # found" even when sites exist, and a very large value bloats the tool
        # output. Mirrors the other list_microsoft_* tools.
        count = max(1, min(count, 50))
        async with _client() as client:
            if query.strip():
                # URL-encode the term — a multi-word / reserved-char query
                # (e.g. "finance team") interpolated raw would make httpx raise
                # InvalidURL before the request is even sent.
                search_term = _url_encode(query.strip(), safe="")
                resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{_BASE_URL}/sites?search={search_term}",
                    _PROVIDER,
                    "search SharePoint sites",
                    params={
                        "$top": str(count),
                        "$select": "id,displayName,name,webUrl,description",
                    },
                )
                if err:
                    return err
                return _format_sites(resp.json().get("value", []), count)

            # No query → aggregate accessible sites from every source. Each
            # source degrades gracefully: a failure just contributes nothing.
            sites_by_id: dict[str, dict] = {}

            search_resp, search_err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/sites?search=*",
                _PROVIDER,
                "list SharePoint sites",
                params={
                    "$top": "100",
                    "$select": "id,displayName,name,webUrl,description",
                },
            )
            if not search_err and search_resp is not None:
                for s in search_resp.json().get("value", []):
                    if s.get("id"):
                        sites_by_id.setdefault(s["id"], s)

            followed_resp, followed_err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/followedSites",
                _PROVIDER,
                "list followed sites",
                params={"$select": "id,displayName,name,webUrl,description"},
                expected_statuses={403, 404},
            )
            if not followed_err and followed_resp is not None:
                for s in followed_resp.json().get("value", []):
                    if s.get("id"):
                        sites_by_id.setdefault(s["id"], s)

            for s in await _collect_group_sites(client):
                sites_by_id.setdefault(s["id"], s)

            sites = sorted(
                sites_by_id.values(),
                key=lambda s: (s.get("displayName") or s.get("name") or "").lower(),
            )
            return _format_sites(sites, count)

    async def list_microsoft_site_files(
        site_id: str,
        folder: str = "root",
        count: int = 20,
    ) -> str:
        """List files in a SharePoint site's default document library.

        ``site_id`` is the id ``list_microsoft_sharepoint_sites`` gives, the
        Graph path form ``hostname:/sites/name`` or the site's URL; the two
        last are resolved to the id first. ``folder`` is a path relative to
        the library root (``"root"`` lists the top level; pass a folder's
        ``Path`` from a previous listing to drill into it).

        Each entry shows a ``Path`` relative to the library root: for a file
        that path is exactly what ``get_microsoft_site_file_content`` expects
        as ``item_path``; for a folder it's the value to pass back as
        ``folder``. You can also read any listed file by handing its URL to
        ``get_microsoft_file_by_url``. A site the account cannot open (it
        only holds a folder shared on it) answers with the pointer to
        ``list_microsoft_folder_by_url``.
        """
        # Clamp to a sane positive range (mirrors the other list_microsoft_*
        # tools) so a huge/<=0 count can't bloat the tool output or send an
        # invalid ``$top``.
        count = max(1, min(count, 50))
        folder_rel = folder.strip().strip("/")
        is_root = folder_rel == "" or folder_rel == "root"
        path = (
            "root/children"
            if is_root
            else f"root:/{_encode_drive_path(folder_rel)}:/children"
        )
        async with _client() as client:
            drive_base, by_address, err = await _site_drive_base(ctx, client, site_id)
            if err:
                return err
            resp, err = await _throttled_request(
                client,
                "get",
                f"{drive_base}/{path}",
                _PROVIDER,
                "list site files",
                params={
                    "$top": str(count),
                    "$select": "id,name,size,lastModifiedDateTime,lastModifiedBy,createdBy,webUrl,folder",
                },
                expected_statuses={400, 403, 404},
            )
            if err:
                status = resp.status_code if resp is not None else None
                if status == 403 or (status == 404 and is_root):
                    return _site_not_accessible(site_id.strip(), err)
                if status == 400 and not by_address:
                    return f"{err} site_id takes {_SITE_FORMS}."
                return err
            items = resp.json().get("value", [])
            if not items:
                return f"No files in '{folder}'."
            # The items are the direct children of ``folder``, so each one's
            # path relative to the library root is ``<folder>/<name>`` (or just
            # ``<name>`` at the root). Surfacing it lets the caller pass a
            # file's path straight to get_microsoft_site_file_content as
            # ``item_path`` — unambiguous even for files nested in subfolders.
            lines: list[str] = []
            for item in items:
                name = item.get("name", "")
                rel_path = name if is_root else f"{folder_rel}/{name}"
                is_folder = "folder" in item
                prefix = "[Folder] " if is_folder else ""
                size_info = (
                    f" ({item.get('size', 0)} bytes)" if not is_folder else ""
                )
                lines.append(
                    "\n".join(
                        [
                            f"{prefix}{name}{size_info}",
                            f"  Path: {rel_path}",
                            f"  Modified: {item.get('lastModifiedDateTime', '')}",
                            *_authorship_lines(item, indent="  "),
                            f"  URL: {item.get('webUrl', '')}",
                        ]
                    )
                )
            return "\n---\n".join(lines)

    async def get_microsoft_site_file_content(
        site_id: str,
        item_path: str,
        worksheet: str | None = None,
        cell_range: str | None = None,
    ) -> str:
        """Read the content of a file in a SharePoint site's document library.

        Use ``list_microsoft_sharepoint_sites`` to find ``site_id`` and
        ``list_microsoft_site_files`` to find ``item_path`` — pass the file's
        ``Path`` from that listing verbatim. It is the path relative to the
        document-library root (e.g. ``Reports/Budget.xlsx`` for a file in the
        ``Reports`` folder, or just ``Budget.xlsx`` at the root). Text files
        are returned inline, Excel workbooks are read cell-by-cell, Word /
        PowerPoint / PDF documents have their text extracted, and other
        binary/large files point at ``download_microsoft_file``. If you only have a file's
        URL (or the path is unclear), use ``get_microsoft_file_by_url`` with
        that URL instead.

        For Excel workbooks use ``worksheet`` to target a specific sheet and
        ``cell_range`` to read a specific address window (e.g. ``"A1:E100"``).
        Do **not** embed worksheet names or ranges in ``item_path`` — patterns
        like ``file.xlsx!Sheet1!A1:E100`` produce malformed Graph URLs and
        will always fail; use the dedicated ``worksheet`` / ``cell_range``
        parameters instead.
        """
        path = item_path.strip().lstrip("/")
        if not path:
            return "Error: provide the file path within the document library."

        # Sanitize: if the caller embedded a worksheet/range suffix directly
        # in item_path (e.g. "Alerts.xlsx!A61:E180"), strip it and fold the
        # extracted values into worksheet/cell_range so the Graph request is
        # well-formed.  Explicit caller-supplied worksheet/cell_range always
        # take precedence over anything extracted from the path (LLMs often
        # send "" for omitted optional params — treat that as absent too).
        worksheet = (worksheet or "").strip() or None
        cell_range = (cell_range or "").strip() or None
        if cell_range and not worksheet:
            # An explicit cell_range is meaningless without a sheet to address
            # — failing fast beats silently reading the whole workbook.
            return (
                "Error: cell_range requires the 'worksheet' parameter — pass "
                "the sheet name to read (see the file's sheet list from a "
                "previous full read, e.g. worksheet='Sheet1')."
            )
        path, extracted_ws, extracted_range = _split_workbook_path_and_range(path)
        worksheet = worksheet or extracted_ws
        cell_range = cell_range or extracted_range

        async with _client() as client:
            drive_base, _by_address, err = await _site_drive_base(ctx, client, site_id)
            if err:
                return err
            meta_resp, err = await _throttled_request(
                client,
                "get",
                f"{drive_base}/root:/{_encode_drive_path(path)}",
                _PROVIDER,
                "access site file",
                params={
                    "$select": "id,name,size,file,folder,webUrl,lastModifiedBy,createdBy,@microsoft.graph.downloadUrl"
                },
            )
            if err:
                return err
            meta = meta_resp.json()
            item_id = meta.get("id")
            if not item_id:
                return "Error: could not resolve the file id for that path."
            item_base = f"{drive_base}/items/{item_id}"
            file_name = meta.get("name", "")
            # Only use the targeted worksheet reader when a worksheet is
            # identified — cell_range alone is not enough to scope a Graph
            # workbook request (we'd need to know which sheet to address).
            # When worksheet is None (including when only cell_range was
            # extracted from an embedded path suffix), fall through to
            # _read_drive_item → _read_workbook so all sheets are returned.
            if worksheet and file_name.lower().endswith(_WORKBOOK_EXTS):
                return _with_drive_links(
                    await _read_workbook_worksheet(
                        ctx,
                        client,
                        item_base,
                        file_name,
                        worksheet=worksheet,
                        cell_range=cell_range,
                    ),
                    meta,
                    hint=True,
                )
            return await _read_drive_item(ctx, client, item_base, meta)

    async def get_microsoft_list_item_fields(
        site_id: str = "",
        item_path: str = "",
        library: str = "",
        url: str = "",
        columns: str = "",
    ) -> str:
        """Read the list item fields (the custom metadata columns) of a file in a
        SharePoint document library: Graph ``.../listItem/fields``. The file is
        ``site_id`` + ``item_path`` (``library`` for a library other than the
        site's default one) or its ``url``. Read-only."""
        wanted = _wanted_columns(columns)
        link = (url or "").strip()
        path = (item_path or "").strip().strip("/")
        async with _client() as client:
            if link:
                if not re.match(r"^https?://", link, re.IGNORECASE):
                    return "Error: url must be the file's http(s) URL or sharing link."
                base = f"{_BASE_URL}/shares/{_encode_share_id(link)}/driveItem"
                what = f"the list item fields of '{link}'"
            else:
                if not path:
                    return (
                        "Error: give the file as site_id plus item_path (its Path from "
                        "list_microsoft_site_files), or as its url."
                    )
                site_base, err = await _site_base(ctx, client, site_id)
                if err:
                    return err
                drive = (
                    f"{site_base}/lists/{_list_ref(library)}/drive" if (library or "").strip() else f"{site_base}/drive"
                )
                base = f"{drive}/root:/{_encode_drive_path(path)}:"
                what = f"the list item fields of '{path}'"
            resp, err = await _throttled_request(
                client,
                "get",
                f"{base}/listItem",
                _PROVIDER,
                "read list item fields",
                params={"$expand": "fields", "$select": "id,webUrl,fields"},
                expected_statuses={401, 403, 404},
            )
            if err:
                return _fields_refused(resp.status_code if resp is not None else None, err, what)
            item = resp.json() or {}
            fields = item.get("fields") or {}
            name = fields.get("FileLeafRef") or path or link
            lines = [
                f"{name}",
                f"  List item ID: {item.get('id', '')}",
                f"  URL: {item.get('webUrl', '')}",
                "  Fields:",
                *_format_fields(fields, wanted, indent="    "),
            ]
            return "\n".join(lines)

    async def list_microsoft_list_items(
        site_id: str,
        library: str,
        filter_column: str = "",
        filter_value: str = "",
        columns: str = "",
        count: int = 50,
        page_token: str = "",
    ) -> str:
        """List the items of a SharePoint list or document library with their
        fields (Graph ``/sites/{id}/lists/{list}/items?expand=fields``),
        optionally only those whose ``filter_column`` equals ``filter_value``.
        ``page_token`` continues a listing. Read-only."""
        lib = (library or "").strip()
        if not lib:
            return "Error: provide the library (or list): its display name or its list id."
        column = (filter_column or "").strip()
        value = (filter_value or "").strip()
        if bool(column) != bool(value):
            return "Error: filter_column and filter_value go together: give both or neither."
        wanted = _wanted_columns(columns)
        count = max(1, min(count, 200))
        token = (page_token or "").strip()
        if token and not token.startswith(f"{_BASE_URL}/"):
            return "Error: page_token must be the value a previous listing returned."
        # A page_token is a Graph page URL, with ``#skip=N`` when the previous
        # call stopped at ``count`` inside that page: the first N entries of the
        # page were already examined, so the follow-up resumes after them.
        skip = 0
        if "#skip=" in token:
            token, _, skip_text = token.partition("#skip=")
            skip = int(skip_text) if skip_text.isdigit() else 0
        async with _client() as client:
            if token:
                next_url: str | None = token
                params: dict | None = None
            else:
                site_base, err = await _site_base(ctx, client, site_id)
                if err:
                    return err
                next_url = f"{site_base}/lists/{_list_ref(lib)}/items"
                params = {
                    "$expand": "fields",
                    "$top": str(_LIST_ITEMS_PAGE if column else count),
                }
            items: list[dict] = []
            scanned = 0
            pages = 0
            while next_url and pages < _MAX_LIST_ITEM_PAGES:
                page_url = f"{next_url}?{urlencode(params, safe='$')}" if params else next_url
                kw: dict = {"params": params} if params else {}
                resp, err = await _throttled_request(
                    client,
                    "get",
                    next_url,
                    _PROVIDER,
                    "list library items",
                    expected_statuses={401, 403, 404},
                    **kw,
                )
                if err:
                    return _fields_refused(
                        resp.status_code if resp is not None else None,
                        err,
                        f"the items of '{lib}'",
                    )
                data = resp.json() or {}
                page = data.get("value", [])
                pages += 1
                resume: str | None = None
                for index, entry in enumerate(page):
                    if index < skip:
                        continue
                    fields = entry.get("fields") or {}
                    if column:
                        key = _column_key(fields, column)
                        if key is None or not _value_matches(fields[key], value):
                            scanned += 1
                            continue
                    if len(items) >= count:
                        # Cap reached inside this page: hand back this page
                        # with the entries already examined skipped.
                        resume = f"{page_url}#skip={index}"
                        break
                    scanned += 1
                    items.append(entry)
                skip = 0
                next_url = resume or data.get("@odata.nextLink")
                params = None
                if resume or not column or len(items) >= count:
                    break
            where = f" where {column} = '{value}'" if column else ""
            header = f"{len(items)} item(s) of '{lib}'{where} ({scanned} scanned)"
            blocks = []
            for entry in items:
                fields = entry.get("fields") or {}
                name = fields.get("FileLeafRef") or fields.get("Title") or entry.get("id", "")
                blocks.append(
                    "\n".join(
                        [
                            f"{name}",
                            f"  List item ID: {entry.get('id', '')}",
                            f"  URL: {entry.get('webUrl', '')}",
                            *_format_fields(fields, wanted, indent="  "),
                        ]
                    )
                )
            footer = f'\n\nMore items: call again with page_token="{next_url}".' if next_url else "\n\nEnd of the list."
            body = "\n---\n".join(blocks) if blocks else "No matching item."
            return f"{header}:\n\n{body}{footer}"

    executor.register("list_microsoft_sharepoint_sites", list_microsoft_sharepoint_sites)
    executor.register("get_microsoft_list_item_fields", get_microsoft_list_item_fields)
    executor.register("list_microsoft_list_items", list_microsoft_list_items)
    executor.register("list_microsoft_site_files", list_microsoft_site_files)
    executor.register("get_microsoft_site_file_content", get_microsoft_site_file_content)

    return [
        # ── SharePoint Sites ──
        ToolDefinition(
            name="list_microsoft_sharepoint_sites",
            description="List or search SharePoint sites the user has access to. If a query is provided, searches by keyword; otherwise returns all accessible sites. A folder shared with the user on a site they are not a member of does not bring its site here: list such a folder with list_microsoft_folder_by_url and its link.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to filter sites (optional)",
                        "default": "",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max sites to return (default 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="list_microsoft_site_files",
            description="List files and folders in a SharePoint site's document library. The site is its id from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL. Each entry shows a Path relative to the library root: pass a file's Path to get_microsoft_site_file_content as item_path, or a folder's Path back as the folder argument to drill in. You can also read any listed file by passing its URL to get_microsoft_file_by_url. If you hold a link to a folder (a folder shared with the user), list it with list_microsoft_folder_by_url instead: it needs no site.",
            input_schema={
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "The site: its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL ('https://hostname/sites/name')",
                    },
                    "folder": {
                        "type": "string",
                        "description": "Folder path within the document library, relative to its root (default 'root'). Pass a folder's Path from a previous listing to drill into it.",
                        "default": "root",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max items to return (default 20)",
                        "default": 20,
                    },
                },
                "required": ["site_id"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="get_microsoft_site_file_content",
            description=(
                "Read the content of a file in a SharePoint site's document library. "
                "Use list_microsoft_sharepoint_sites to find the site_id and "
                "list_microsoft_site_files to find the file's Path — pass it verbatim "
                "as item_path (it's relative to the library root, e.g. 'Reports/Budget.xlsx'). "
                "Text files are returned inline, Excel workbooks (.xlsx/.xlsm) are read "
                "cell-by-cell, Word, PowerPoint and PDF documents have their text "
                "extracted, other files point at download_microsoft_file. "
                "For Excel workbooks, use 'worksheet' to target a specific sheet and "
                "'cell_range' to read a specific address window (e.g. 'A1:E100'). "
                "IMPORTANT: do NOT embed worksheet names or cell ranges in item_path "
                "(e.g. 'Alerts.xlsx!Sheet1!A1:E100') — that produces a malformed Graph "
                "URL and will always fail with 400; use the 'worksheet'/'cell_range' "
                "parameters instead. If you only have a file's URL, use get_microsoft_file_by_url."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "The site: its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL ('https://hostname/sites/name')",
                    },
                    "item_path": {
                        "type": "string",
                        "description": (
                            "File path within the document library, relative to its root — "
                            "use the Path shown by list_microsoft_site_files "
                            "(e.g. 'Reports/Budget.xlsx'). Do NOT append worksheet names or "
                            "cell ranges to this path; use the 'worksheet' and 'cell_range' "
                            "parameters for that."
                        ),
                    },
                    "worksheet": {
                        "type": "string",
                        "description": (
                            "Optional. For Excel workbooks: name of the worksheet to read "
                            "(e.g. 'Sheet1', 'Feuil1'). If omitted all sheets are returned."
                        ),
                    },
                    "cell_range": {
                        "type": "string",
                        "description": (
                            "Optional. For Excel workbooks: cell address range to read "
                            "(e.g. 'A1:E100'). Requires 'worksheet' to be set. "
                            "Must be a single cell or a bounded range like "
                            "'A61:E180' — no sheet-name prefix, no column/row "
                            "spans like 'A:E'."
                        ),
                    },
                },
                "required": ["site_id", "item_path"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="get_microsoft_list_item_fields",
            description=(
                "Read the SharePoint list item fields of a file: the custom metadata "
                "columns of its document library (a choice, text, date or person column) "
                "with their values. Give the file as site_id plus item_path (its Path from "
                "list_microsoft_site_files; library when it is not the site's default "
                "library), or as its url. Read-only. Needs Sites.Read.All."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "The site: its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL",
                    },
                    "item_path": {
                        "type": "string",
                        "description": "The file's path relative to the library root (the Path list_microsoft_site_files shows)",
                    },
                    "library": {
                        "type": "string",
                        "description": "Optional: the document library's display name or list id, when it is not the site's default library",
                    },
                    "url": {
                        "type": "string",
                        "description": "Instead of site_id and item_path: the file's URL or sharing link",
                    },
                    "columns": {
                        "type": "string",
                        "description": "Optional comma-separated column names to return (internal or display key); default every field",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="list_microsoft_list_items",
            description=(
                "List the items of a SharePoint list or document library with their "
                "fields (custom metadata columns), optionally only those whose column "
                "equals a value (filter_column + filter_value, case-insensitive). Pages "
                "with page_token. Read-only. Needs Sites.Read.All."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "site_id": {
                        "type": "string",
                        "description": "The site: its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL",
                    },
                    "library": {
                        "type": "string",
                        "description": "The list or document library: its display name (e.g. 'Documents') or its list id",
                    },
                    "filter_column": {
                        "type": "string",
                        "description": "Optional: the column to filter on (its internal name as the fields show it)",
                    },
                    "filter_value": {
                        "type": "string",
                        "description": "Optional, with filter_column: the value the column must equal",
                    },
                    "columns": {
                        "type": "string",
                        "description": "Optional comma-separated column names to show per item; default every field",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Items to return (default 50, max 200)",
                        "default": 50,
                    },
                    "page_token": {
                        "type": "string",
                        "description": "The page_token a previous call returned, to read the next page",
                    },
                },
                "required": ["site_id", "library"],
            },
            source="integration:microsoft",
            priority=2,
        ),
    ]
