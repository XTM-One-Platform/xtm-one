# SPDX-License-Identifier: Apache-2.0

"""Google Sheets: find a spreadsheet, list its sheets, read, append, update, clear, delete rows.

The tools an agent needs on a spreadsheet kept as a shared register (an asset
list, an exception tracker, a report export), registered by ``google.build_tools``:

* ``search_google_sheets`` - spreadsheets by name or content (Drive ``files.list``
  restricted to Google Sheets);
* ``list_google_spreadsheet_sheets`` - a spreadsheet's sheets (tabs): ids, sizes
  (``spreadsheets.get``);
* ``get_google_sheet_values`` - a sheet or a range, one line per row with its
  row number, optionally only the rows whose column holds a value
  (``values.get``);
* ``append_google_sheet_rows`` - rows after a sheet's table, given as lists or
  as objects keyed by the header row (``values.append``);
* ``update_google_sheet_values`` - a range overwritten (``values.update``);
* ``update_google_sheet_rows`` / ``append_or_update_google_sheet_rows`` - rows
  matched on a column of the header row and updated in place, the second
  appending the rows it does not find (``values.batchUpdate``, ``values.append``);
* ``clear_google_sheet_values`` - a range or a whole sheet emptied (``values.clear``);
* ``delete_google_sheet_rows`` - rows removed, the rows below moving up
  (``deleteDimension``).

Values are written RAW unless asked otherwise: a string starting with ``=`` is
stored as text, never run as a formula, since what an agent writes often comes
from elsewhere. ``value_input_option='USER_ENTERED'`` parses them as typed in
the editor.

Every call acts as the connected user. A scope the token lacks is named before
the request that needs it (``google_docs.TokenScopes``), and a 403 / 404 / 429
becomes a sentence naming the spreadsheet (``google_docs.failure``).
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import quote as _url_encode

from app.integrations.file_content import MAX_DOCUMENT_CHARS, cut_inline
from app.integrations.http import TRANSIENT_RETRY_STATUSES, AutoRefreshClient, safe_request
from app.integrations.providers.google_docs import (
    DRIVE_READONLY_SCOPE,
    DRIVE_SCOPE,
    ScopeNeed,
    TokenScopes,
    failure,
    parse_file_id,
    scope_error,
)
from app.tools.base import ToolDefinition

_PROVIDER = "Google"
_SOURCE = "integration:google"
_DRIVE_API = "https://www.googleapis.com/drive/v3"
_SHEETS_API = "https://sheets.googleapis.com/v4"

GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
SHEETS_READONLY_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"

#: ``spreadsheets.get``, ``values.get``.
_READ_SHEET = ScopeNeed(
    SHEETS_SCOPE,
    frozenset({SHEETS_SCOPE, SHEETS_READONLY_SCOPE, DRIVE_SCOPE, DRIVE_READONLY_SCOPE}),
)
#: ``values.append`` / ``update`` / ``batchUpdate`` / ``clear``, ``spreadsheets.batchUpdate``.
_WRITE_SHEET = ScopeNeed(SHEETS_SCOPE, frozenset({SHEETS_SCOPE, DRIVE_SCOPE}))
#: Drive ``files.list``.
_SEARCH_DRIVE = ScopeNeed(DRIVE_SCOPE, frozenset({DRIVE_SCOPE, DRIVE_READONLY_SCOPE}))

_META_FIELDS = (
    "spreadsheetId,spreadsheetUrl,properties(title,locale,timeZone),"
    "sheets(properties(sheetId,title,index,sheetType,hidden,gridProperties(rowCount,columnCount,frozenRowCount)))"
)
_INPUT_OPTIONS = ("RAW", "USER_ENTERED")
_RENDER_OPTIONS = ("FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA")
_DEFAULT_READ_ROWS = 200
_MAX_READ_ROWS = 5000
_MAX_WRITE_ROWS = 1000
_MAX_DELETE_ROWS = 1000
_MAX_SEARCH_RESULTS = 100

_CELL_RE = re.compile(r"\$?([A-Za-z]{0,3})\$?(\d*)")
_CELLS_RE = re.compile(r"\$?[A-Za-z]{0,3}\$?\d*(?::\$?[A-Za-z]{0,3}\$?\d*)?")

_BAD_SPREADSHEET_ID = (
    "Error: spreadsheet_id is required: the spreadsheet's ID or its URL "
    "(from search_google_sheets or search_google_drive)."
)


# ---------------------------------------------------------------------------
# Arguments and A1 notation
# ---------------------------------------------------------------------------


def _path(value: str) -> str:
    return _url_encode(value, safe="")


def _bounded(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def column_letter(index: int) -> str:
    """The A1 letters of a 0-based column: 0 → A, 25 → Z, 26 → AA."""
    letters = ""
    index += 1
    while index:
        index, rest = divmod(index - 1, 26)
        letters = chr(65 + rest) + letters
    return letters


def _column_index(letters: str) -> int:
    index = 0
    for char in letters.upper():
        index = index * 26 + ord(char) - 64
    return index - 1


def quoted_sheet(title: str) -> str:
    """A sheet title as the prefix of an A1 range: ``Q3 'final'`` → ``'Q3 ''final'''``."""
    return "'" + title.replace("'", "''") + "'"


def range_start(a1: str) -> tuple[int, int]:
    """The first row (1-based) and column (0-based) of an A1 range like ``'Q3'!C5:F9``."""
    first = a1.rsplit("!", 1)[-1].split(":", 1)[0]
    match = _CELL_RE.fullmatch(first)
    if match is None:
        return 1, 0
    letters, digits = match.groups()
    return (int(digits) if digits else 1), (_column_index(letters) if letters else 0)


def split_range(raw: Any) -> tuple[str, str]:
    """``(sheet title, cells)`` of a range argument; the title is "" when not given."""
    value = str(raw or "").strip()
    if "!" not in value:
        return "", value
    sheet, cells = value.rsplit("!", 1)
    if len(sheet) >= 2 and sheet[0] == sheet[-1] == "'":
        sheet = sheet[1:-1].replace("''", "'")
    return sheet, cells.strip()


def _norm(name: Any) -> str:
    return " ".join(str(name).split()).casefold()


def _is_cell(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _rows_arg(raw: Any, name: str) -> tuple[list | None, str | None]:
    """The rows a write tool was handed: a non-empty JSON array, at most the write limit."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None, f"Error: {name} must be a JSON array of rows."
    if not isinstance(raw, list) or not raw:
        return None, f"Error: {name} is required: a non-empty array of rows."
    if len(raw) > _MAX_WRITE_ROWS:
        return None, (
            f"Error: {name} holds {len(raw)} rows, over the {_MAX_WRITE_ROWS} a call may write. "
            "Split them into several calls."
        )
    return raw, None


def _list_rows(rows: list, name: str) -> str | None:
    """Why *rows* is not a list of rows of cells, or ``None``."""
    for number, row in enumerate(rows, start=1):
        if not isinstance(row, list):
            return f"Error: {name}[{number - 1}] is not an array of cells."
        if not all(_is_cell(cell) for cell in row):
            return (
                f"Error: {name}[{number - 1}] holds a value that is not a cell: a cell is a "
                "string, a number, a boolean or null."
            )
    return None


def _input_option(raw: Any) -> str | None:
    value = str(raw or "RAW").strip().upper()
    return value if value in _INPUT_OPTIONS else None


def _escape_drive_query(term: str) -> str:
    return term.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# What a spreadsheet looks like to the agent
# ---------------------------------------------------------------------------


def _label(meta: dict | None, spreadsheet_id: str) -> str:
    title = ((meta or {}).get("properties") or {}).get("title")
    return f"'{title}'" if title else f"spreadsheet {spreadsheet_id}"


def _sheet_url(meta: dict | None, sheet: dict | None = None) -> str:
    url = (meta or {}).get("spreadsheetUrl") or ""
    if not url:
        return ""
    if sheet is not None and sheet.get("sheetId") is not None:
        url = f"{url.split('#', 1)[0]}#gid={sheet['sheetId']}"
    return f"\nWeb URL: {url}"


def _sheets(meta: dict) -> list[dict]:
    found = [sheet.get("properties") or {} for sheet in meta.get("sheets") or [] if isinstance(sheet, dict)]
    return sorted(found, key=lambda props: int(props.get("index") or 0))


def _find_sheet(meta: dict, title: str, label: str) -> tuple[dict | None, str | None]:
    """The sheet named *title* (the first one when empty), or why there is none."""
    sheets = _sheets(meta)
    if not sheets:
        return None, f"Error: {label} has no sheet."
    found: dict | None = None
    if not title:
        found = sheets[0]
    else:
        exact = [props for props in sheets if props.get("title") == title]
        loose = [props for props in sheets if _norm(props.get("title") or "") == _norm(title)]
        if exact or len(loose) == 1:
            found = (exact or loose)[0]
    if found is None:
        names = ", ".join(f"'{props.get('title')}'" for props in sheets)
        return None, f"Error: {label} has no sheet named '{title}'. Its sheets: {names}."
    if (found.get("sheetType") or "GRID") != "GRID":
        return None, f"Error: sheet '{found.get('title')}' of {label} is a chart, not a grid of cells."
    return found, None


def _row_line(number: int, row: list) -> str:
    return f"Row {number}: {json.dumps(row, ensure_ascii=False)}"


def _header_columns(header: list) -> dict[str, int]:
    """The header row's names (normalised) → their 0-based column; a repeated name maps to -1."""
    columns: dict[str, int] = {}
    for index, name in enumerate(header):
        key = _norm(name)
        if key:
            columns[key] = -1 if key in columns else index
    return columns


def _header_names(header: list) -> str:
    names = [f"'{name}'" for name in header if str(name).strip()]
    return ", ".join(names) if names else "none (row 1 is empty)"


class GoogleSheetTools:
    """The Google Sheets tools of one ``build_tools`` call."""

    def __init__(self, headers: dict[str, str], client_factory: Callable[[], AutoRefreshClient]) -> None:
        self._client = client_factory
        self._scopes = TokenScopes(headers)

    # -- shared calls ---------------------------------------------------------

    async def _open(
        self, client: AutoRefreshClient, spreadsheet_id: str, action: str, need: ScopeNeed
    ) -> tuple[dict | None, str, str | None]:
        """The spreadsheet's metadata and label, once the token is known to hold *need*."""
        resp, err = await safe_request(
            client,
            "get",
            f"{_SHEETS_API}/spreadsheets/{_path(spreadsheet_id)}",
            _PROVIDER,
            "read the spreadsheet",
            expected_statuses=set(),
            retry_statuses=TRANSIENT_RETRY_STATUSES,
            params={"fields": _META_FIELDS},
        )
        label = f"spreadsheet {spreadsheet_id}"
        if err:
            return None, label, failure(
                resp,
                err,
                action=action,
                label=label,
                need=_READ_SHEET,
                role="a viewer",
                not_found=(
                    f"Error: Google Sheets cannot find {label}: it does not exist, was deleted, is "
                    "not shared with the connected Google account, or is not a Google Sheet (read "
                    "an uploaded Excel file with read_google_doc). Check the spreadsheet ID or link."
                ),
            )
        meta = resp.json()
        label = _label(meta, spreadsheet_id)
        missing = await self._scopes.missing(client, need)
        if missing:
            return None, label, scope_error(missing, action, label)
        return meta, label, None

    async def _target(
        self, spreadsheet_id: Any, sheet: Any, range_: Any, action: str, need: ScopeNeed, client: AutoRefreshClient
    ) -> tuple[dict | None, dict | None, str, str, str | None]:
        """``(meta, sheet, label, cells, error)`` for a tool addressing one sheet."""
        sid = parse_file_id(spreadsheet_id)
        if sid is None:
            return None, None, "", "", _BAD_SPREADSHEET_ID
        range_sheet, cells = split_range(range_)
        title = str(sheet or "").strip()
        if range_sheet and title and range_sheet != title:
            return None, None, "", "", (
                f"Error: range names sheet '{range_sheet}' but sheet is '{title}': give the sheet "
                "once, and the range as cells (A1:F200)."
            )
        if cells and not _CELLS_RE.fullmatch(cells):
            return None, None, "", "", (
                f"Error: range '{cells}' is not cells in A1 notation, such as A1:F200, A:C or 2:10. "
                "Name the sheet with sheet."
            )
        meta, label, err = await self._open(client, sid, action, need)
        if err:
            return None, None, label, "", err
        props, err = _find_sheet(meta, title or range_sheet, label)
        if err:
            return None, None, label, "", err
        return meta, props, label, cells, None

    async def _values(
        self, client: AutoRefreshClient, sid: str, a1: str, *, render: str = "FORMATTED_VALUE", by_column: bool = False
    ) -> tuple[dict | None, Any, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{_SHEETS_API}/spreadsheets/{_path(sid)}/values/{_path(a1)}",
            _PROVIDER,
            "read the sheet's values",
            expected_statuses=set(),
            retry_statuses=TRANSIENT_RETRY_STATUSES,
            params={"valueRenderOption": render, "majorDimension": "COLUMNS" if by_column else "ROWS"},
        )
        if err:
            return None, resp, err
        return resp.json(), None, None

    async def _header(
        self, client: AutoRefreshClient, sid: str, props: dict, label: str, action: str
    ) -> tuple[list | None, str | None]:
        data, resp, err = await self._values(client, sid, f"{quoted_sheet(props['title'])}!1:1")
        if data is None:
            return None, failure(resp, err, action=action, label=label, need=_READ_SHEET, role="a viewer")
        return (data.get("values") or [[]])[0], None

    async def _write(
        self, client: AutoRefreshClient, method: str, url: str, action: str, label: str, **kwargs: Any
    ) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            client, method, url, _PROVIDER, action, expected_statuses=set(), **kwargs
        )
        if err:
            return None, failure(resp, err, action=action, label=label, need=_WRITE_SHEET, role="an editor")
        return resp.json(), None

    async def _append(
        self, client: AutoRefreshClient, sid: str, a1: str, rows: list, option: str, label: str
    ) -> tuple[dict | None, str | None]:
        data, err = await self._write(
            client,
            "post",
            f"{_SHEETS_API}/spreadsheets/{_path(sid)}/values/{_path(a1)}:append",
            "append rows to",
            label,
            params={"valueInputOption": option, "insertDataOption": "INSERT_ROWS"},
            json={"majorDimension": "ROWS", "values": rows},
        )
        return ((data or {}).get("updates") or {}) if err is None else None, err

    # -- search_google_sheets -------------------------------------------------

    async def search_google_sheets(self, query: str = "", max_results: int = 20) -> str:
        term = str(query or "").strip()
        clauses = [f"mimeType = '{GOOGLE_SHEET_MIME}'", "trashed = false"]
        if term:
            escaped = _escape_drive_query(term)
            clauses.append(f"(name contains '{escaped}' or fullText contains '{escaped}')")
        async with self._client() as client:
            missing = await self._scopes.missing(client, _SEARCH_DRIVE)
            if missing:
                return scope_error(missing, "search", "Google Drive")
            resp, err = await safe_request(
                client,
                "get",
                f"{_DRIVE_API}/files",
                _PROVIDER,
                "search spreadsheets",
                retry_statuses=TRANSIENT_RETRY_STATUSES,
                params={
                    "q": " and ".join(clauses),
                    "pageSize": str(_bounded(max_results, 20, 1, _MAX_SEARCH_RESULTS)),
                    "fields": "files(id,name,modifiedTime,webViewLink,owners(emailAddress))",
                    "orderBy": "modifiedTime desc",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                },
            )
        if err:
            return err
        files = [f for f in (resp.json().get("files") or []) if isinstance(f, dict)]
        if not files:
            return f"No Google Sheets spreadsheet matches '{term}'." if term else "No Google Sheets spreadsheet found."
        lines = []
        for f in files:
            owners = ", ".join(o.get("emailAddress", "") for o in f.get("owners") or [] if isinstance(o, dict))
            lines.append(
                f"- '{f.get('name', '')}' | ID: {f.get('id', '')} | Modified: {f.get('modifiedTime', '')}"
                + (f" | Owner: {owners}" if owners else "")
                + (f"\n  URL: {f['webViewLink']}" if f.get("webViewLink") else "")
            )
        return f"Found {len(files)} spreadsheet(s):\n" + "\n".join(lines)

    # -- list_google_spreadsheet_sheets ---------------------------------------

    async def list_google_spreadsheet_sheets(self, spreadsheet_id: str) -> str:
        sid = parse_file_id(spreadsheet_id)
        if sid is None:
            return _BAD_SPREADSHEET_ID
        async with self._client() as client:
            meta, label, err = await self._open(client, sid, "read", _READ_SHEET)
        if err:
            return err
        props = meta.get("properties") or {}
        sheets = _sheets(meta)
        head = f"Spreadsheet {label} (ID: {sid})"
        details = [f"locale {props['locale']}" if props.get("locale") else "", props.get("timeZone") or ""]
        if any(details):
            head += ", " + ", ".join(d for d in details if d)
        lines = [f"{head}: {len(sheets)} sheet(s)."]
        for sheet in sheets:
            grid = sheet.get("gridProperties") or {}
            line = f"- '{sheet.get('title')}' (sheet id {sheet.get('sheetId')})"
            if (sheet.get("sheetType") or "GRID") != "GRID":
                line += f": {str(sheet.get('sheetType')).lower()}"
            else:
                line += f": {grid.get('rowCount', 0)} rows x {grid.get('columnCount', 0)} columns"
                if grid.get("frozenRowCount"):
                    line += f", {grid['frozenRowCount']} frozen row(s)"
            if sheet.get("hidden"):
                line += ", hidden"
            lines.append(line)
        return "\n".join(lines) + _sheet_url(meta)

    # -- get_google_sheet_values ----------------------------------------------

    async def get_google_sheet_values(
        self,
        spreadsheet_id: str,
        sheet: str = "",
        range: str = "",
        filter_column: str = "",
        filter_value: str = "",
        value_render_option: str = "FORMATTED_VALUE",
        max_rows: int = _DEFAULT_READ_ROWS,
    ) -> str:
        render = str(value_render_option or "FORMATTED_VALUE").strip().upper()
        if render not in _RENDER_OPTIONS:
            return f"Error: value_render_option must be one of {', '.join(_RENDER_OPTIONS)}."
        column = str(filter_column or "").strip()
        async with self._client() as client:
            meta, props, label, cells, err = await self._target(
                spreadsheet_id, sheet, range, "read", _READ_SHEET, client
            )
            if err:
                return err
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            a1 = quoted_sheet(props["title"]) + (f"!{cells}" if cells else "")
            data, resp, err = await self._values(client, sid, a1, render=render)
        if data is None:
            return failure(resp, err, action="read", label=label, need=_READ_SHEET, role="a viewer")
        rows = [row if isinstance(row, list) else [] for row in data.get("values") or []]
        returned = str(data.get("range") or a1)
        first_row, first_column = range_start(returned)
        where = f"sheet '{props['title']}' of {label} ({returned})"
        if not rows:
            return f"No values in {where}." + _sheet_url(meta, props)
        numbered = list(enumerate(rows, start=first_row))
        header: list[tuple[int, list]] = []
        if column:
            columns = _header_columns(rows[0])
            index = columns.get(_norm(column))
            if index is None or index < 0:
                state = "is repeated in" if index is not None else "is not a column of"
                return (
                    f"Error: filter_column '{column}' {state} the first row of {where}. "
                    f"Its columns: {_header_names(rows[0])}."
                )
            wanted = _norm(filter_value)
            header = numbered[:1]
            numbered = [
                (number, row)
                for number, row in numbered[1:]
                if _norm(row[index] if index < len(row) else "") == wanted
            ]
        limit = _bounded(max_rows, _DEFAULT_READ_ROWS, 1, _MAX_READ_ROWS)
        shown = numbered[:limit]
        if column:
            intro = f"{len(numbered)} row(s) of {where} where '{column}' is '{filter_value}', below the header row"
        else:
            intro = f"{len(rows)} row(s) of {where}"
        lines = [intro + " (row numbers are the sheet's):"]
        if first_column:
            lines.append(f"(The first value of each row is column {column_letter(first_column)}.)")
        lines.extend(_row_line(number, row) for number, row in header + shown)
        if len(shown) < len(numbered):
            next_row = shown[-1][0] + 1
            lines.append(
                f"[{len(numbered) - len(shown)} more row(s) not shown: raise max_rows, or read on "
                f"with range='{next_row}:{numbered[-1][0]}'.]"
            )
        text = "\n".join(lines)
        hint = "Read a narrower range, or fewer rows with max_rows."
        return cut_inline(text, MAX_DOCUMENT_CHARS, hint) + _sheet_url(meta, props)

    # -- append_google_sheet_rows ---------------------------------------------

    async def append_google_sheet_rows(
        self,
        spreadsheet_id: str,
        rows: Any,
        sheet: str = "",
        range: str = "",
        value_input_option: str = "RAW",
    ) -> str:
        option = _input_option(value_input_option)
        if option is None:
            return f"Error: value_input_option must be one of {', '.join(_INPUT_OPTIONS)}."
        items, err = _rows_arg(rows, "rows")
        if err:
            return err
        keyed = all(isinstance(row, dict) for row in items)
        if not keyed and (err := _list_rows(items, "rows")):
            return err
        async with self._client() as client:
            meta, props, label, cells, err = await self._target(
                spreadsheet_id, sheet, range, "append rows to", _WRITE_SHEET, client
            )
            if err:
                return err
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            title = props["title"]
            if keyed:
                if cells:
                    return "Error: rows given as objects are placed by the header row: leave range empty."
                header, err = await self._header(client, sid, props, label, "append rows to")
                if err:
                    return err
                values, err = _positioned(items, header, f"sheet '{title}' of {label}")
                if err:
                    return err
                a1 = f"{quoted_sheet(title)}!A1"
            else:
                values, a1 = items, quoted_sheet(title) + (f"!{cells}" if cells else "")
            updates, err = await self._append(client, sid, a1, values, option, label)
        if err:
            return err
        return (
            f"Appended {updates.get('updatedRows', len(values))} row(s) to sheet '{title}' of {label} "
            f"({updates.get('updatedRange', 'range not returned')})." + _sheet_url(meta, props)
        )

    # -- update_google_sheet_values -------------------------------------------

    async def update_google_sheet_values(
        self,
        spreadsheet_id: str,
        range: str,
        values: Any,
        sheet: str = "",
        value_input_option: str = "RAW",
    ) -> str:
        option = _input_option(value_input_option)
        if option is None:
            return f"Error: value_input_option must be one of {', '.join(_INPUT_OPTIONS)}."
        items, err = _rows_arg(values, "values")
        if err:
            return err
        if err := _list_rows(items, "values"):
            return err
        if not split_range(range)[1]:
            return (
                "Error: range is required: the cells to overwrite, in A1 notation (A2:D5). "
                "To add rows, use append_google_sheet_rows."
            )
        async with self._client() as client:
            meta, props, label, cells, err = await self._target(
                spreadsheet_id, sheet, range, "update", _WRITE_SHEET, client
            )
            if err:
                return err
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            a1 = f"{quoted_sheet(props['title'])}!{cells}"
            data, err = await self._write(
                client,
                "put",
                f"{_SHEETS_API}/spreadsheets/{_path(sid)}/values/{_path(a1)}",
                "update",
                label,
                params={"valueInputOption": option},
                json={"range": a1, "majorDimension": "ROWS", "values": items},
            )
        if err:
            return err
        return (
            f"Updated {data.get('updatedCells', 0)} cell(s) in {data.get('updatedRows', 0)} row(s) of sheet "
            f"'{props['title']}' of {label} ({data.get('updatedRange', a1)})." + _sheet_url(meta, props)
        )

    # -- update_google_sheet_rows / append_or_update_google_sheet_rows -------

    async def update_google_sheet_rows(
        self,
        spreadsheet_id: str,
        key_column: str,
        rows: Any,
        sheet: str = "",
        value_input_option: str = "RAW",
    ) -> str:
        return await self._keyed_write(
            spreadsheet_id, key_column, rows, sheet, value_input_option, append_missing=False
        )

    async def append_or_update_google_sheet_rows(
        self,
        spreadsheet_id: str,
        key_column: str,
        rows: Any,
        sheet: str = "",
        value_input_option: str = "RAW",
    ) -> str:
        return await self._keyed_write(
            spreadsheet_id, key_column, rows, sheet, value_input_option, append_missing=True
        )

    async def _keyed_write(
        self,
        spreadsheet_id: Any,
        key_column: Any,
        rows: Any,
        sheet: Any,
        value_input_option: Any,
        *,
        append_missing: bool,
    ) -> str:
        option = _input_option(value_input_option)
        if option is None:
            return f"Error: value_input_option must be one of {', '.join(_INPUT_OPTIONS)}."
        key = str(key_column or "").strip()
        if not key:
            return "Error: key_column is required: the header of the column that identifies a row."
        items, err = _rows_arg(rows, "rows")
        if err:
            return err
        if not all(isinstance(row, dict) for row in items):
            return "Error: rows must be objects keyed by the header row, each holding key_column."
        action = "update rows of"
        async with self._client() as client:
            meta, props, label, _cells, err = await self._target(
                spreadsheet_id, sheet, "", action, _WRITE_SHEET, client
            )
            if err:
                return err
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            title = props["title"]
            where = f"sheet '{title}' of {label}"
            header, err = await self._header(client, sid, props, label, action)
            if err:
                return err
            columns = _header_columns(header)
            key_index = columns.get(_norm(key))
            if key_index is None or key_index < 0:
                state = "is repeated in" if key_index is not None else "is not a column of"
                return f"Error: key_column '{key}' {state} the header row of {where}. Its columns: {_header_names(header)}."
            letter = column_letter(key_index)
            data, resp, err = await self._values(client, sid, f"{quoted_sheet(title)}!{letter}2:{letter}", by_column=True)
            if data is None:
                return failure(resp, err, action=action, label=label, need=_READ_SHEET, role="a viewer")
            found: dict[str, list[int]] = {}
            for offset, value in enumerate((data.get("values") or [[]])[0]):
                if _key(value):
                    found.setdefault(_key(value), []).append(offset + 2)
            plan, err = _plan_keyed(items, key, key_index, header, found, where, append_missing)
            if err:
                return err
            updates, appends = plan
            updated = 0
            if updates:
                data_ranges = []
                for number, cells in updates:
                    low, high = min(cells), max(cells)
                    data_ranges.append(
                        {
                            "range": f"{quoted_sheet(title)}!{column_letter(low)}{number}:{column_letter(high)}{number}",
                            "majorDimension": "ROWS",
                            "values": [[cells.get(index) for index in range(low, high + 1)]],
                        }
                    )
                result, err = await self._write(
                    client,
                    "post",
                    f"{_SHEETS_API}/spreadsheets/{_path(sid)}/values:batchUpdate",
                    action,
                    label,
                    json={"valueInputOption": option, "data": data_ranges},
                )
                if err:
                    return err
                updated = int(result.get("totalUpdatedRows") or len(updates))
            done = (
                f"Updated {updated} row(s) of {where} (row {', '.join(str(n) for n, _ in updates)})"
                if updates
                else ""
            )
            appended = ""
            if appends:
                result, err = await self._append(client, sid, f"{quoted_sheet(title)}!A1", appends, option, label)
                if err:
                    if done:
                        reason = err.removeprefix("Error: ")
                        return f"Error: {done}, but appending the {len(appends)} new row(s) failed: {reason}"
                    return err
                appended = (
                    f"appended {result.get('updatedRows', len(appends))} new row(s) to {where} "
                    f"({result.get('updatedRange', 'range not returned')})"
                )
        summary = "; ".join(part for part in (done, appended) if part)
        return summary[:1].upper() + summary[1:] + "." + _sheet_url(meta, props)

    # -- clear_google_sheet_values --------------------------------------------

    async def clear_google_sheet_values(
        self,
        spreadsheet_id: str,
        sheet: str = "",
        range: str = "",
    ) -> str:
        if not str(sheet or "").strip() and not split_range(range)[0]:
            return "Error: sheet is required: the title of the sheet to clear (list_google_spreadsheet_sheets)."
        async with self._client() as client:
            meta, props, label, cells, err = await self._target(
                spreadsheet_id, sheet, range, "clear", _WRITE_SHEET, client
            )
            if err:
                return err
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            a1 = quoted_sheet(props["title"]) + (f"!{cells}" if cells else "")
            data, err = await self._write(
                client,
                "post",
                f"{_SHEETS_API}/spreadsheets/{_path(sid)}/values/{_path(a1)}:clear",
                "clear",
                label,
                json={},
            )
        if err:
            return err
        return (
            f"Cleared the values of {data.get('clearedRange', a1)} in {label} (formatting kept)."
            + _sheet_url(meta, props)
        )

    # -- delete_google_sheet_rows ---------------------------------------------

    async def delete_google_sheet_rows(
        self, spreadsheet_id: str, sheet: str, start_row: int, row_count: int = 1
    ) -> str:
        if not str(sheet or "").strip():
            return "Error: sheet is required: the title of the sheet whose rows are deleted."
        try:
            first, count = int(start_row), int(row_count)
        except (TypeError, ValueError):
            return "Error: start_row and row_count must be whole numbers."
        if first < 1 or not 1 <= count <= _MAX_DELETE_ROWS:
            return f"Error: start_row must be 1 or more, and row_count between 1 and {_MAX_DELETE_ROWS}."
        async with self._client() as client:
            meta, props, label, _cells, err = await self._target(
                spreadsheet_id, sheet, "", "delete rows of", _WRITE_SHEET, client
            )
            if err:
                return err
            last = first + count - 1
            total = int((props.get("gridProperties") or {}).get("rowCount") or 0)
            if last > total:
                return (
                    f"Error: sheet '{props['title']}' of {label} has {total} rows, so rows {first}-{last} "
                    "cannot be deleted. Nothing was deleted."
                )
            sid = meta.get("spreadsheetId") or parse_file_id(spreadsheet_id)
            _data, err = await self._write(
                client,
                "post",
                f"{_SHEETS_API}/spreadsheets/{_path(sid)}:batchUpdate",
                "delete rows of",
                label,
                json={
                    "requests": [
                        {
                            "deleteDimension": {
                                "range": {
                                    "sheetId": props.get("sheetId"),
                                    "dimension": "ROWS",
                                    "startIndex": first - 1,
                                    "endIndex": last,
                                }
                            }
                        }
                    ]
                },
            )
        if err:
            return err
        rows = f"row {first}" if count == 1 else f"rows {first}-{last}"
        return (
            f"Deleted {rows} of sheet '{props['title']}' of {label}: the rows below moved up by {count}."
            + _sheet_url(meta, props)
        )

    # -- registration ---------------------------------------------------------

    def handlers(self) -> dict[str, Callable[..., Any]]:
        return {
            "search_google_sheets": self.search_google_sheets,
            "list_google_spreadsheet_sheets": self.list_google_spreadsheet_sheets,
            "get_google_sheet_values": self.get_google_sheet_values,
            "append_google_sheet_rows": self.append_google_sheet_rows,
            "update_google_sheet_values": self.update_google_sheet_values,
            "update_google_sheet_rows": self.update_google_sheet_rows,
            "append_or_update_google_sheet_rows": self.append_or_update_google_sheet_rows,
            "clear_google_sheet_values": self.clear_google_sheet_values,
            "delete_google_sheet_rows": self.delete_google_sheet_rows,
        }

    @staticmethod
    def definitions() -> list[ToolDefinition]:
        spreadsheet_id = {
            "type": "string",
            "description": "The spreadsheet's ID, or its URL (docs.google.com/spreadsheets/d/...)",
        }
        sheet = {
            "type": "string",
            "description": "The sheet (tab) title; default the first sheet (list_google_spreadsheet_sheets)",
            "default": "",
        }
        cell_range = {
            "type": "string",
            "description": "Cells of the sheet in A1 notation (A1:F200, A:C, 2:10); default the whole sheet",
            "default": "",
        }
        input_option = {
            "type": "string",
            "enum": list(_INPUT_OPTIONS),
            "description": (
                "'RAW' (default) stores values as given, a text starting with '=' included; "
                "'USER_ENTERED' parses them as typed in the editor (formulas, dates)"
            ),
            "default": "RAW",
        }
        cell = {"type": ["string", "number", "boolean", "null"]}
        list_rows = {"type": "array", "items": {"type": "array", "items": cell}}
        keyed_rows = {
            "type": "array",
            "description": "Rows as objects mapping a header of row 1 to the cell's value; each holds key_column",
            "items": {"type": "object", "additionalProperties": cell},
        }
        key_column = {
            "type": "string",
            "description": "The header (row 1) of the column whose value identifies a row, e.g. 'Asset'",
        }
        keyed_properties = {
            "spreadsheet_id": spreadsheet_id,
            "key_column": key_column,
            "rows": keyed_rows,
            "sheet": sheet,
            "value_input_option": input_option,
        }
        return [
            ToolDefinition(
                name="search_google_sheets",
                description=(
                    "Find Google Sheets spreadsheets by name or content (most recently modified "
                    "first), or list the latest ones with an empty query. Returns each one's ID "
                    "for the other Google Sheets tools."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Words of the spreadsheet's name or content; empty lists the latest",
                            "default": "",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": f"Max spreadsheets listed (default 20, at most {_MAX_SEARCH_RESULTS})",
                            "default": 20,
                        },
                    },
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="list_google_spreadsheet_sheets",
                description=(
                    "List the sheets (tabs) of a Google Sheets spreadsheet: each one's title, "
                    "sheet id, size and whether it is hidden. Use the titles as sheet in the "
                    "other Google Sheets tools."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"spreadsheet_id": spreadsheet_id},
                    "required": ["spreadsheet_id"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="get_google_sheet_values",
                description=(
                    "Read the values of a Google Sheets sheet or range, one line per row with its "
                    "row number in the sheet (use it with update_google_sheet_values and "
                    "delete_google_sheet_rows). Give filter_column (a header of the range's first "
                    "row) and filter_value to get only the rows holding that value (case and "
                    "spacing ignored), after the header row."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": spreadsheet_id,
                        "sheet": sheet,
                        "range": cell_range,
                        "filter_column": {
                            "type": "string",
                            "description": "Optional: a header of the first row to filter on",
                            "default": "",
                        },
                        "filter_value": {
                            "type": "string",
                            "description": "The value filter_column must hold",
                            "default": "",
                        },
                        "value_render_option": {
                            "type": "string",
                            "enum": list(_RENDER_OPTIONS),
                            "description": "Values as displayed (default), unformatted, or the formulas",
                            "default": "FORMATTED_VALUE",
                        },
                        "max_rows": {
                            "type": "integer",
                            "description": f"Max rows shown (default {_DEFAULT_READ_ROWS}, at most {_MAX_READ_ROWS})",
                            "default": _DEFAULT_READ_ROWS,
                        },
                    },
                    "required": ["spreadsheet_id"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="append_google_sheet_rows",
                description=(
                    "Append rows after the last row of a Google Sheets sheet's table. Give rows "
                    "as arrays of cells (placed from the table's first column, or from range's) "
                    "or as objects mapping headers of row 1 to values (each placed under its "
                    "header; an unknown header writes nothing)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": spreadsheet_id,
                        "rows": {
                            "type": "array",
                            "description": "The rows: arrays of cells, or objects keyed by header",
                            "items": {"type": ["array", "object"]},
                        },
                        "sheet": sheet,
                        "range": {
                            **cell_range,
                            "description": "Optional, for array rows: the table to append to (A:D)",
                        },
                        "value_input_option": input_option,
                    },
                    "required": ["spreadsheet_id", "rows"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="update_google_sheet_values",
                description=(
                    "Overwrite the cells of a range of a Google Sheets sheet with values, row by "
                    "row from the range's top-left cell. A null cell is left unchanged, an empty "
                    "string empties it. Read the range first with get_google_sheet_values."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": spreadsheet_id,
                        "range": {
                            "type": "string",
                            "description": "The cells to overwrite in A1 notation, e.g. B5:D5",
                        },
                        "values": {**list_rows, "description": "The rows of cells, top to bottom"},
                        "sheet": sheet,
                        "value_input_option": input_option,
                    },
                    "required": ["spreadsheet_id", "range", "values"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="update_google_sheet_rows",
                description=(
                    "Update rows of a Google Sheets sheet found by their value in key_column (a "
                    "header of row 1): only the columns each row names change. Refused, changing "
                    "nothing, when a key is not found or matches several rows. To also add the "
                    "rows not found, use append_or_update_google_sheet_rows."
                ),
                input_schema={
                    "type": "object",
                    "properties": keyed_properties,
                    "required": ["spreadsheet_id", "key_column", "rows"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="append_or_update_google_sheet_rows",
                description=(
                    "Upsert rows of a Google Sheets sheet by their value in key_column (a header "
                    "of row 1): a row whose key is found is updated (only the columns it names), "
                    "the others are appended. Refused, changing nothing, when a key matches "
                    "several rows."
                ),
                input_schema={
                    "type": "object",
                    "properties": keyed_properties,
                    "required": ["spreadsheet_id", "key_column", "rows"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="clear_google_sheet_values",
                description=(
                    "Empty the values of a range of a Google Sheets sheet, or of the whole sheet "
                    "when range is empty; formatting stays and no row moves."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": spreadsheet_id,
                        "sheet": {**sheet, "description": "The sheet (tab) title"},
                        "range": cell_range,
                    },
                    "required": ["spreadsheet_id", "sheet"],
                },
                source=_SOURCE,
                priority=1,
            ),
            ToolDefinition(
                name="delete_google_sheet_rows",
                description=(
                    "Delete rows of a Google Sheets sheet by row number (get_google_sheet_values "
                    "shows them); the rows below move up. Read them first: row numbers shift "
                    "after every delete."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "spreadsheet_id": spreadsheet_id,
                        "sheet": {"type": "string", "description": "The sheet (tab) title"},
                        "start_row": {"type": "integer", "description": "The first row deleted (1-based)", "minimum": 1},
                        "row_count": {
                            "type": "integer",
                            "description": f"How many rows (default 1, at most {_MAX_DELETE_ROWS})",
                            "default": 1,
                        },
                    },
                    "required": ["spreadsheet_id", "sheet", "start_row"],
                },
                source=_SOURCE,
                priority=1,
            ),
        ]


def _positioned(
    items: list[dict], header: list, where: str, first: int = 0
) -> tuple[list[list] | None, str | None]:
    """Object rows as arrays placed under their headers (null where a row names no value).

    *first* is the index of ``items[0]`` in the tool's ``rows``, for the errors.
    """
    columns = _header_columns(header)
    rows: list[list] = []
    for number, item in enumerate(items, start=first):
        placed: dict[int, Any] = {}
        for name, value in item.items():
            index = columns.get(_norm(name))
            if index is None or index < 0:
                state = "is repeated in" if index is not None else "is not a header of"
                return None, (
                    f"Error: rows[{number}] names '{name}', which {state} row 1 of {where}, so nothing "
                    f"was written. Its headers: {_header_names(header)}."
                )
            if not _is_cell(value):
                return None, (
                    f"Error: rows[{number}]['{name}'] is not a cell: a cell is a string, a number, a "
                    "boolean or null. Nothing was written."
                )
            placed[index] = value
        if not placed:
            return None, f"Error: rows[{number}] names no column. Nothing was written."
        rows.append([placed.get(index) for index in range(max(placed) + 1)])
    return rows, None


def _key(value: Any) -> str:
    """A key cell as compared: spacing collapsed, case kept."""
    return " ".join(str(value).split()) if value is not None else ""


def _plan_keyed(
    items: list[dict],
    key: str,
    key_index: int,
    header: list,
    found: dict[str, list[int]],
    where: str,
    append_missing: bool,
) -> tuple[tuple[list[tuple[int, dict[int, Any]]], list[list]] | None, str | None]:
    """``(updates, appends)`` of a keyed write, or why nothing is written.

    *updates* pairs a sheet row number with the cells to write (column → value,
    the key column left alone, a null left unchanged); *appends* are the rows
    placed under the header.
    """
    columns = _header_columns(header)
    updates: list[tuple[int, dict[int, Any]]] = []
    appends: list[list] = []
    seen: set[str] = set()
    missing: list[str] = []
    for number, item in enumerate(items):
        placed, err = _positioned([item], header, where, first=number)
        if err:
            return None, err
        wanted = _key(placed[0][key_index] if key_index < len(placed[0]) else None)
        if not wanted:
            return None, f"Error: rows[{number}] has no value for key_column '{key}'. Nothing was written."
        if wanted in seen:
            return None, f"Error: rows hold key '{wanted}' twice. Nothing was written."
        seen.add(wanted)
        matches = found.get(wanted, [])
        if len(matches) > 1:
            listed = ", ".join(str(n) for n in matches[:10])
            return None, (
                f"Error: key '{wanted}' matches {len(matches)} rows of {where} (row {listed}), so nothing "
                "was written. Update them by row number with update_google_sheet_values."
            )
        if not matches:
            if append_missing:
                appends.append(placed[0])
            else:
                missing.append(wanted)
            continue
        cells = {columns[_norm(name)]: value for name, value in item.items() if columns[_norm(name)] != key_index}
        if not cells:
            return None, f"Error: rows[{number}] names no column to update besides '{key}'. Nothing was written."
        updates.append((matches[0], cells))
    if missing:
        listed = ", ".join(f"'{m}'" for m in missing[:10])
        return None, (
            f"Error: no row of {where} has '{key}' {listed}, so nothing was written. Append them "
            "with append_or_update_google_sheet_rows or append_google_sheet_rows."
        )
    return (updates, appends), None
