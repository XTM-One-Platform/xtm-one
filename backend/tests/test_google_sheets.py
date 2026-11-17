"""Google Sheets tools: search, sheets, read, append, update, upsert, clear, delete rows.

The tools run for real against fake Sheets / Drive / tokeninfo APIs served
through ``httpx.MockTransport`` (documented response shapes, no network, no
token). What they pin:

* every call addresses the sheet by its quoted title, and reads number rows as
  the sheet does;
* values are written RAW unless USER_ENTERED is asked for, so a text starting
  with ``=`` is never run as a formula;
* rows given as objects land under their headers; a keyed update changes only
  the named columns of the one row its key matches, and refuses the whole call,
  writing nothing, on a missing (update) or repeated key;
* a scope the token lacks is named before the write, and a 404 / disabled API
  is a sentence naming the spreadsheet;
* reads skip approval, writes do not, and a read-only binding keeps the reads.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.integrations.providers import google
from app.integrations.providers.google_docs import DRIVE_READONLY_SCOPE, DRIVE_SCOPE
from app.integrations.providers.google_sheets import (
    GOOGLE_SHEET_MIME,
    SHEETS_SCOPE,
    column_letter,
    quoted_sheet,
    range_start,
    split_range,
)
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

SID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgvE2upms"
SHEETS = "sheets.googleapis.com"
DRIVE = "www.googleapis.com"
BASE = f"/v4/spreadsheets/{SID}"
URL = f"https://docs.google.com/spreadsheets/d/{SID}/edit"
TOKEN = "ya29.fake-access-token"

_READS = {"search_google_sheets", "list_google_spreadsheet_sheets", "get_google_sheet_values"}
_WRITES = {
    "append_google_sheet_rows",
    "update_google_sheet_values",
    "update_google_sheet_rows",
    "append_or_update_google_sheet_rows",
    "clear_google_sheet_values",
    "delete_google_sheet_rows",
}

_META = {
    "spreadsheetId": SID,
    "spreadsheetUrl": URL,
    "properties": {"title": "Exposure register", "locale": "en_US", "timeZone": "Europe/Paris"},
    "sheets": [
        {
            "properties": {
                "sheetId": 0,
                "title": "Assets",
                "index": 0,
                "sheetType": "GRID",
                "gridProperties": {"rowCount": 100, "columnCount": 6, "frozenRowCount": 1},
            }
        },
        {
            "properties": {
                "sheetId": 777,
                "title": "Q3 'final'",
                "index": 1,
                "sheetType": "GRID",
                "hidden": True,
                "gridProperties": {"rowCount": 20, "columnCount": 4},
            }
        },
    ],
}
_HEADER = ["Asset", "Owner", "Severity", "Status"]


class FakeGoogle:
    """Routes ``(method, host, decoded path)`` to canned responses and records requests."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str, str], Any] = {}
        self.requests: list[httpx.Request] = []

    def on(self, method: str, host: str, path: str, response: Any) -> None:
        self.routes[(method.upper(), host, path)] = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = self.routes.get((request.method, request.url.host, request.url.path))
        if response is None:
            raise AssertionError(f"no fake route for {request.method} {request.url}")
        return response(request) if callable(response) else response

    def sent(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method and r.url.path == path]

    def bodies(self, method: str, path: str) -> list[Any]:
        return [json.loads(r.content) for r in self.sent(method, path)]

    def writes(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.method != "GET" and r.url.host == SHEETS]


def _json(payload: Any, status: int = 200):
    return lambda _request: httpx.Response(status, json=payload)


def _scopes(*scopes: str):
    return _json({"scope": " ".join(("openid", *scopes))})


@pytest.fixture
def api(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    fake.on("POST", "oauth2.googleapis.com", "/tokeninfo", _scopes(DRIVE_SCOPE, SHEETS_SCOPE))
    fake.on("GET", SHEETS, BASE, _json(_META))
    fake.on("GET", SHEETS, f"{BASE}/values/'Assets'!1:1", _json({"range": "Assets!A1:D1", "values": [_HEADER]}))
    return fake


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    google.build_tools(TOKEN, executor)
    return executor


async def _call(name: str, **arguments: Any) -> ToolResult:
    return await _executor().execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


def _key_column(*keys: str):
    """The Asset column below the header, as ``values.get(majorDimension=COLUMNS)`` returns it."""
    return _json({"range": "Assets!A2:A100", "majorDimension": "COLUMNS", "values": [list(keys)]})


# ── A1 notation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("index", "letters"), [(0, "A"), (25, "Z"), (26, "AA"), (51, "AZ"), (701, "ZZ"), (702, "AAA")]
)
def test_column_letters(index: int, letters: str):
    assert column_letter(index) == letters


@pytest.mark.parametrize(
    ("a1", "start"),
    [
        ("Assets!A1:D5", (1, 0)),
        ("'Q3 ''final'''!C5:F9", (5, 2)),
        ("'a!b'!$B$12", (12, 1)),
        ("Assets!A:C", (1, 0)),
        ("Assets!3:9", (3, 0)),
        ("Assets", (1, 0)),
    ],
)
def test_range_start(a1: str, start: tuple[int, int]):
    assert range_start(a1) == start


@pytest.mark.parametrize(
    ("raw", "parts"),
    [
        ("A1:D5", ("", "A1:D5")),
        ("Assets!A1:D5", ("Assets", "A1:D5")),
        ("'Q3 ''final'''!B2", ("Q3 'final'", "B2")),
        ("", ("", "")),
    ],
)
def test_split_range(raw: str, parts: tuple[str, str]):
    assert split_range(raw) == parts


def test_a_sheet_title_is_quoted_with_its_quotes_doubled():
    assert quoted_sheet("Q3 'final'") == "'Q3 ''final'''"


# ── search / sheets ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_lists_only_spreadsheets_and_escapes_the_term(api):
    api.on(
        "GET",
        DRIVE,
        "/drive/v3/files",
        _json({"files": [{"id": SID, "name": "Exposure register", "modifiedTime": "2026-10-30T09:00:00Z", "webViewLink": URL}]}),
    )

    result = await _call("search_google_sheets", query="O'Brien \\ assets")

    [request] = api.sent("GET", "/drive/v3/files")
    escaped = "O\\'Brien \\\\ assets"
    assert request.url.params["q"] == (
        f"mimeType = '{GOOGLE_SHEET_MIME}' and trashed = false and "
        f"(name contains '{escaped}' or fullText contains '{escaped}')"
    )
    assert request.url.params["includeItemsFromAllDrives"] == "true"
    assert not result.is_error
    assert result.content == (
        "Found 1 spreadsheet(s):\n"
        f"- 'Exposure register' | ID: {SID} | Modified: 2026-10-30T09:00:00Z\n  URL: {URL}"
    )


@pytest.mark.asyncio
async def test_list_sheets_gives_titles_ids_sizes_and_hidden(api):
    result = await _call("list_google_spreadsheet_sheets", spreadsheet_id=URL + "#gid=0")

    assert result.content == (
        f"Spreadsheet 'Exposure register' (ID: {SID}), locale en_US, Europe/Paris: 2 sheet(s).\n"
        "- 'Assets' (sheet id 0): 100 rows x 6 columns, 1 frozen row(s)\n"
        "- 'Q3 'final'' (sheet id 777): 20 rows x 4 columns, hidden\n"
        f"Web URL: {URL}"
    )


# ── get_google_sheet_values ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_read_numbers_rows_as_the_sheet_does(api):
    api.on(
        "GET",
        SHEETS,
        f"{BASE}/values/'Q3 ''final'''!C5:E7",
        _json({"range": "'Q3 ''final'''!C5:E7", "values": [["web-1", "high"], ["db-2"], ["vpn", "low", 3]]}),
    )

    result = await _call("get_google_sheet_values", spreadsheet_id=SID, range="'Q3 ''final'''!C5:E7")

    [request] = api.sent("GET", f"{BASE}/values/'Q3 ''final'''!C5:E7")
    assert request.url.params["valueRenderOption"] == "FORMATTED_VALUE"
    assert result.content == (
        "3 row(s) of sheet 'Q3 'final'' of 'Exposure register' ('Q3 ''final'''!C5:E7) "
        "(row numbers are the sheet's):\n"
        "(The first value of each row is column C.)\n"
        'Row 5: ["web-1", "high"]\n'
        'Row 6: ["db-2"]\n'
        'Row 7: ["vpn", "low", 3]\n'
        f"Web URL: {URL}#gid=777"
    )


@pytest.mark.asyncio
async def test_read_filters_on_a_header_and_keeps_the_header_row(api):
    api.on(
        "GET",
        SHEETS,
        f"{BASE}/values/'Assets'",
        _json(
            {
                "range": "Assets!A1:D5",
                "values": [_HEADER, ["web-1", "ann", "High"], ["db-2", "bob", "low"], ["vpn", "ann", " high "]],
            }
        ),
    )

    result = await _call(
        "get_google_sheet_values", spreadsheet_id=SID, filter_column="severity", filter_value="HIGH"
    )

    assert result.content.splitlines()[1:4] == [
        'Row 1: ["Asset", "Owner", "Severity", "Status"]',
        'Row 2: ["web-1", "ann", "High"]',
        'Row 4: ["vpn", "ann", " high "]',
    ]
    assert result.content.startswith("2 row(s) of sheet 'Assets'")


@pytest.mark.asyncio
async def test_read_says_how_to_read_on_past_max_rows(api):
    rows = [[f"host-{n}"] for n in range(1, 11)]
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A1:A10", _json({"range": "Assets!A1:A10", "values": rows}))

    result = await _call("get_google_sheet_values", spreadsheet_id=SID, range="A1:A10", max_rows=3)

    assert result.content.splitlines()[-2] == (
        "[7 more row(s) not shown: raise max_rows, or read on with range='4:10'.]"
    )


@pytest.mark.asyncio
async def test_an_unknown_sheet_names_the_sheets_and_reads_nothing(api):
    result = await _call("get_google_sheet_values", spreadsheet_id=SID, sheet="Findings")

    assert result.content == (
        "Error: 'Exposure register' has no sheet named 'Findings'. Its sheets: 'Assets', 'Q3 'final''."
    )
    assert [r.url.path for r in api.requests if "/values/" in r.url.path] == []


@pytest.mark.asyncio
async def test_a_range_that_is_not_cells_is_refused_before_any_call(api):
    result = await _call("get_google_sheet_values", spreadsheet_id=SID, range="A1; DROP")

    assert result.content.startswith("Error: range 'A1; DROP' is not cells in A1 notation")
    assert api.requests == []


# ── append ────────────────────────────────────────────────────────────────


def _appended(range_: str, rows: int):
    return _json({"updates": {"updatedRange": range_, "updatedRows": rows}})


@pytest.mark.asyncio
async def test_append_writes_raw_so_a_formula_stays_text(api):
    api.on("POST", SHEETS, f"{BASE}/values/'Assets':append", _appended("Assets!A12:B12", 1))

    result = await _call(
        "append_google_sheet_rows", spreadsheet_id=SID, rows=[['=IMPORTXML("https://x.test","//a")', 7]]
    )

    [request] = api.sent("POST", f"{BASE}/values/'Assets':append")
    assert dict(request.url.params) == {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    assert json.loads(request.content) == {
        "majorDimension": "ROWS",
        "values": [['=IMPORTXML("https://x.test","//a")', 7]],
    }
    assert result.content == (
        f"Appended 1 row(s) to sheet 'Assets' of 'Exposure register' (Assets!A12:B12).\nWeb URL: {URL}#gid=0"
    )


@pytest.mark.asyncio
async def test_append_user_entered_only_when_asked(api):
    api.on("POST", SHEETS, f"{BASE}/values/'Assets'!A:B:append", _appended("Assets!A12:B12", 1))

    await _call(
        "append_google_sheet_rows",
        spreadsheet_id=SID,
        range="A:B",
        rows=[["=1+1", "x"]],
        value_input_option="user_entered",
    )

    [request] = api.sent("POST", f"{BASE}/values/'Assets'!A:B:append")
    assert request.url.params["valueInputOption"] == "USER_ENTERED"


@pytest.mark.asyncio
async def test_append_objects_land_under_their_headers(api):
    api.on("POST", SHEETS, f"{BASE}/values/'Assets'!A1:append", _appended("Assets!A12:D13", 2))

    await _call(
        "append_google_sheet_rows",
        spreadsheet_id=SID,
        rows=[{"severity": "high", "Asset": "web-1"}, {"Status": "open", "Asset": "db-2"}],
    )

    assert api.bodies("POST", f"{BASE}/values/'Assets'!A1:append") == [
        {"majorDimension": "ROWS", "values": [["web-1", None, "high"], ["db-2", None, None, "open"]]}
    ]


@pytest.mark.asyncio
async def test_append_an_unknown_header_writes_nothing(api):
    result = await _call("append_google_sheet_rows", spreadsheet_id=SID, rows=[{"Asset": "a", "CVSS": 9.8}])

    assert result.content == (
        "Error: rows[0] names 'CVSS', which is not a header of row 1 of sheet 'Assets' of "
        "'Exposure register', so nothing was written. Its headers: 'Asset', 'Owner', 'Severity', 'Status'."
    )
    assert result.is_error
    assert api.writes() == []


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([], "Error: rows is required: a non-empty array of rows."),
        ("not json", "Error: rows must be a JSON array of rows."),
        ([["a", {"nested": 1}]], "Error: rows[0] holds a value that is not a cell"),
        ([["a"], "b"], "Error: rows[1] is not an array of cells."),
        ([["x"]] * 1001, "Error: rows holds 1001 rows, over the 1000 a call may write."),
    ],
)
@pytest.mark.asyncio
async def test_append_refuses_bad_rows_before_any_call(api, rows: Any, message: str):
    result = await _call("append_google_sheet_rows", spreadsheet_id=SID, rows=rows)

    assert result.content.startswith(message)
    assert api.requests == []


@pytest.mark.asyncio
async def test_an_unknown_value_input_option_is_refused(api):
    result = await _call("append_google_sheet_rows", spreadsheet_id=SID, rows=[["a"]], value_input_option="FORMULA")

    assert result.content == "Error: value_input_option must be one of RAW, USER_ENTERED."
    assert api.requests == []


# ── update ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_values_overwrites_the_range(api):
    path = f"{BASE}/values/'Assets'!B5:C5"
    api.on("PUT", SHEETS, path, _json({"updatedRange": "Assets!B5:C5", "updatedRows": 1, "updatedCells": 2}))

    result = await _call("update_google_sheet_values", spreadsheet_id=SID, range="B5:C5", values=[["ann", None]])

    [request] = api.sent("PUT", path)
    assert request.url.params["valueInputOption"] == "RAW"
    assert json.loads(request.content) == {"range": "'Assets'!B5:C5", "majorDimension": "ROWS", "values": [["ann", None]]}
    assert result.content.startswith("Updated 2 cell(s) in 1 row(s) of sheet 'Assets' of 'Exposure register' (Assets!B5:C5).")


@pytest.mark.asyncio
async def test_update_values_needs_a_range(api):
    result = await _call("update_google_sheet_values", spreadsheet_id=SID, range="Assets!", values=[["x"]])

    assert result.content.startswith("Error: range is required")
    assert api.requests == []


@pytest.mark.asyncio
async def test_update_rows_changes_only_the_named_columns_of_the_matching_row(api):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column("web-1", "", "db-2"))
    api.on("POST", SHEETS, f"{BASE}/values:batchUpdate", _json({"totalUpdatedRows": 2}))

    result = await _call(
        "update_google_sheet_rows",
        spreadsheet_id=SID,
        key_column="asset",
        rows=[{"Asset": "db-2", "Status": "fixed"}, {"Asset": " web-1 ", "Owner": "bob", "Status": None}],
    )

    [column_read] = api.sent("GET", f"{BASE}/values/'Assets'!A2:A")
    assert column_read.url.params["majorDimension"] == "COLUMNS"
    assert api.bodies("POST", f"{BASE}/values:batchUpdate") == [
        {
            "valueInputOption": "RAW",
            "data": [
                {"range": "'Assets'!D4:D4", "majorDimension": "ROWS", "values": [["fixed"]]},
                {"range": "'Assets'!B2:D2", "majorDimension": "ROWS", "values": [["bob", None, None]]},
            ],
        }
    ]
    assert api.sent("POST", f"{BASE}/values/'Assets'!A1:append") == []
    assert result.content == (
        f"Updated 2 row(s) of sheet 'Assets' of 'Exposure register' (row 4, 2).\nWeb URL: {URL}#gid=0"
    )


@pytest.mark.asyncio
async def test_update_rows_with_a_missing_key_writes_nothing(api):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column("web-1"))

    result = await _call(
        "update_google_sheet_rows",
        spreadsheet_id=SID,
        key_column="Asset",
        rows=[{"Asset": "web-1", "Status": "fixed"}, {"Asset": "Web-1x", "Status": "open"}],
    )

    assert result.content.startswith(
        "Error: no row of sheet 'Assets' of 'Exposure register' has 'Asset' 'Web-1x', so nothing was written."
    )
    assert api.writes() == []


@pytest.mark.asyncio
async def test_a_key_is_matched_with_its_case(api):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column("WEB-1"))

    result = await _call(
        "update_google_sheet_rows", spreadsheet_id=SID, key_column="Asset", rows=[{"Asset": "web-1", "Status": "x"}]
    )

    assert result.content.startswith("Error: no row of sheet 'Assets'")
    assert api.writes() == []


@pytest.mark.parametrize(
    ("keys", "rows", "message"),
    [
        (("web-1", "web-1"), [{"Asset": "web-1", "Status": "x"}], "Error: key 'web-1' matches 2 rows of sheet 'Assets' of 'Exposure register' (row 2, 3)"),
        (("web-1",), [{"Asset": "web-1", "Status": "x"}, {"Asset": "web-1", "Owner": "y"}], "Error: rows hold key 'web-1' twice."),
        (("web-1",), [{"Status": "x"}], "Error: rows[0] has no value for key_column 'Asset'."),
        (("web-1",), [{"Asset": "web-1"}], "Error: rows[0] names no column to update besides 'Asset'."),
        (("web-1",), [{"Asset": "web-1", "Due": "x"}], "Error: rows[0] names 'Due', which is not a header"),
    ],
)
@pytest.mark.asyncio
async def test_update_rows_refusals_write_nothing(api, keys: tuple[str, ...], rows: list, message: str):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column(*keys))

    result = await _call("update_google_sheet_rows", spreadsheet_id=SID, key_column="Asset", rows=rows)

    assert result.content.startswith(message)
    assert api.writes() == []


@pytest.mark.asyncio
async def test_an_unknown_key_column_names_the_headers(api):
    result = await _call("update_google_sheet_rows", spreadsheet_id=SID, key_column="Host", rows=[{"Host": "a"}])

    assert result.content == (
        "Error: key_column 'Host' is not a column of the header row of sheet 'Assets' of 'Exposure register'. "
        "Its columns: 'Asset', 'Owner', 'Severity', 'Status'."
    )
    assert api.writes() == []


@pytest.mark.asyncio
async def test_append_or_update_updates_the_found_rows_and_appends_the_others(api):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column("web-1"))
    api.on("POST", SHEETS, f"{BASE}/values:batchUpdate", _json({"totalUpdatedRows": 1}))
    api.on("POST", SHEETS, f"{BASE}/values/'Assets'!A1:append", _appended("Assets!A3:C3", 1))

    result = await _call(
        "append_or_update_google_sheet_rows",
        spreadsheet_id=SID,
        key_column="Asset",
        rows=[{"Asset": "web-1", "Severity": "low"}, {"Asset": "db-2", "Severity": "high"}],
    )

    assert api.bodies("POST", f"{BASE}/values:batchUpdate") == [
        {
            "valueInputOption": "RAW",
            "data": [{"range": "'Assets'!C2:C2", "majorDimension": "ROWS", "values": [["low"]]}],
        }
    ]
    assert api.bodies("POST", f"{BASE}/values/'Assets'!A1:append") == [
        {"majorDimension": "ROWS", "values": [["db-2", None, "high"]]}
    ]
    assert result.content == (
        "Updated 1 row(s) of sheet 'Assets' of 'Exposure register' (row 2); appended 1 new row(s) to "
        f"sheet 'Assets' of 'Exposure register' (Assets!A3:C3).\nWeb URL: {URL}#gid=0"
    )


@pytest.mark.asyncio
async def test_a_failed_append_after_an_update_says_what_was_written(api):
    api.on("GET", SHEETS, f"{BASE}/values/'Assets'!A2:A", _key_column("web-1"))
    api.on("POST", SHEETS, f"{BASE}/values:batchUpdate", _json({"totalUpdatedRows": 1}))
    api.on("POST", SHEETS, f"{BASE}/values/'Assets'!A1:append", _json({"error": {"code": 500}}, status=500))

    result = await _call(
        "append_or_update_google_sheet_rows",
        spreadsheet_id=SID,
        key_column="Asset",
        rows=[{"Asset": "web-1", "Severity": "low"}, {"Asset": "db-2", "Severity": "high"}],
    )

    assert result.content.startswith(
        "Error: Updated 1 row(s) of sheet 'Assets' of 'Exposure register' (row 2), but appending the 1 new "
        "row(s) failed: "
    )


# ── clear / delete ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_empties_the_range(api):
    path = f"{BASE}/values/'Assets'!A2:D100:clear"
    api.on("POST", SHEETS, path, _json({"clearedRange": "Assets!A2:D100"}))

    result = await _call("clear_google_sheet_values", spreadsheet_id=SID, sheet="Assets", range="A2:D100")

    assert api.bodies("POST", path) == [{}]
    assert result.content.startswith("Cleared the values of Assets!A2:D100 in 'Exposure register' (formatting kept).")


@pytest.mark.asyncio
async def test_clear_needs_the_sheet_named(api):
    result = await _call("clear_google_sheet_values", spreadsheet_id=SID)

    assert result.content.startswith("Error: sheet is required")
    assert api.requests == []


@pytest.mark.asyncio
async def test_delete_rows_removes_them_by_sheet_id(api):
    api.on("POST", SHEETS, f"{BASE}:batchUpdate", _json({"replies": [{}]}))

    result = await _call("delete_google_sheet_rows", spreadsheet_id=SID, sheet="Q3 'final'", start_row=5, row_count=3)

    assert api.bodies("POST", f"{BASE}:batchUpdate") == [
        {
            "requests": [
                {"deleteDimension": {"range": {"sheetId": 777, "dimension": "ROWS", "startIndex": 4, "endIndex": 7}}}
            ]
        }
    ]
    assert result.content.startswith(
        "Deleted rows 5-7 of sheet 'Q3 'final'' of 'Exposure register': the rows below moved up by 3."
    )


@pytest.mark.parametrize(
    ("start_row", "row_count", "message"),
    [
        (19, 3, "Error: sheet 'Q3 'final'' of 'Exposure register' has 20 rows, so rows 19-21 cannot be deleted."),
        (0, 1, "Error: start_row must be 1 or more"),
        (2, 1001, "Error: start_row must be 1 or more, and row_count between 1 and 1000."),
        ("two", 1, "Error: start_row and row_count must be whole numbers."),
    ],
)
@pytest.mark.asyncio
async def test_delete_rows_out_of_bounds_deletes_nothing(api, start_row: Any, row_count: Any, message: str):
    result = await _call(
        "delete_google_sheet_rows", spreadsheet_id=SID, sheet="Q3 'final'", start_row=start_row, row_count=row_count
    )

    assert result.content.startswith(message)
    assert api.writes() == []


# ── scopes and failures ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_write_without_a_write_scope_names_it_and_writes_nothing(api):
    api.on("POST", "oauth2.googleapis.com", "/tokeninfo", _scopes(DRIVE_READONLY_SCOPE))

    result = await _call("append_google_sheet_rows", spreadsheet_id=SID, rows=[["a"]])

    assert result.content.startswith(
        "Error: the Google integration is not authorized to append rows to 'Exposure register': its token "
        f"lacks the OAuth scope {SHEETS_SCOPE}."
    )
    assert api.writes() == []


@pytest.mark.asyncio
async def test_a_read_works_on_a_drive_read_only_token(api):
    api.on("POST", "oauth2.googleapis.com", "/tokeninfo", _scopes(DRIVE_READONLY_SCOPE))

    result = await _call("list_google_spreadsheet_sheets", spreadsheet_id=SID)

    assert result.content.startswith("Spreadsheet 'Exposure register'")


@pytest.mark.asyncio
async def test_a_missing_spreadsheet_is_a_sentence(api):
    api.on("GET", SHEETS, BASE, _json({"error": {"code": 404, "status": "NOT_FOUND"}}, status=404))

    result = await _call("get_google_sheet_values", spreadsheet_id=SID)

    assert result.content.startswith(f"Error: Google Sheets cannot find spreadsheet {SID}: it does not exist")


@pytest.mark.asyncio
async def test_a_disabled_sheets_api_says_which_api_to_enable(api):
    body = {"error": {"code": 403, "status": "PERMISSION_DENIED", "details": [{"reason": "SERVICE_DISABLED"}]}}
    api.on("GET", SHEETS, BASE, _json(body, status=403))

    result = await _call("list_google_spreadsheet_sheets", spreadsheet_id=SID)

    assert result.content.startswith("Error: the Google Sheets API is not enabled in the Google Cloud project")


@pytest.mark.asyncio
async def test_a_bad_spreadsheet_id_is_refused_before_any_call(api):
    result = await _call("get_google_sheet_values", spreadsheet_id="../../etc")

    assert result.content.startswith("Error: spreadsheet_id is required")
    assert api.requests == []


# ── registration and approval ─────────────────────────────────────────────


def test_every_sheets_tool_is_registered_with_the_arguments_it_declares():
    import inspect

    executor = CallableToolExecutor()
    definitions = {d.name: d for d in google.build_tools(TOKEN, executor) if d.name in _READS | _WRITES}

    assert set(definitions) == _READS | _WRITES
    assert set(executor.tool_names) >= _READS | _WRITES
    # The executor drops an argument its handler does not take: every declared one must be taken.
    assert {
        name: set(d.input_schema["properties"]) - set(inspect.signature(executor.handler(name)).parameters)
        for name, d in definitions.items()
    } == {name: set() for name in definitions}


def test_the_sheets_tools_are_classified_read_or_write():
    assert {name: integration_tool_access(name) for name in _READS | _WRITES} == {
        **dict.fromkeys(_READS, "read"),
        **dict.fromkeys(_WRITES, "write"),
    }


def test_a_read_only_binding_keeps_only_the_reading_sheets_tools():
    executor = CallableToolExecutor()
    definitions = google.build_tools(TOKEN, executor)

    kept = {d.name for d in withhold_write_tools(definitions, executor, integration_label="Google")}

    assert kept >= _READS
    assert not _WRITES & kept


def test_the_integration_requests_the_spreadsheets_scope():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    assert SHEETS_SCOPE in google.GoogleIntegration().get_oauth_defaults().scopes
    entry = PROVIDER_CATALOG["google"]
    assert SHEETS_SCOPE in entry["scopes"].split()
    assert "Google Sheets API" in entry["setup_guide"]["apis_to_enable"]
