"""Tests for cross-site SharePoint/OneDrive file access in the Microsoft integration.

Covers the gap that previously made it impossible to read a SharePoint file the
user pasted a link to (the integration could only reach personal OneDrive):

- ``_encode_share_id`` produces the canonical ``u!<base64url>`` Graph share ID.
- ``get_microsoft_file_by_url`` resolves a SharePoint/OneDrive URL via
  ``/shares/{id}/driveItem`` and reads text and Excel content.
- ``get_microsoft_site_file_content`` reads a file inside a SharePoint site.
- ``search_microsoft_files`` uses the unified Graph search API (``/search/query``
  with ``driveItem`` entities) so results span every SharePoint site, not just
  the user's OneDrive.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._drive import _encode_share_id, _split_workbook_path_and_range
from app.tools.base import CallableToolExecutor, ToolCall

# ── _encode_share_id ──────────────────────────────────────────────────────


def test_encode_share_id_matches_graph_scheme():
    url = "https://contoso.sharepoint.com/:x:/r/sites/finance/_layouts/15/Doc.aspx?sourcedoc=%7BABC%7D&file=Budget.xlsx"
    expected = (
        "u!" + base64.urlsafe_b64encode(url.encode()).rstrip(b"=").decode()
    )
    assert _encode_share_id(url) == expected
    # The share id must never carry base64 padding (Graph rejects it).
    assert "=" not in _encode_share_id(url)


def test_encode_share_id_strips_whitespace():
    url = "https://contoso.sharepoint.com/:u:/s/site/AABB"
    assert _encode_share_id(f"  {url}  ") == _encode_share_id(url)


# ── Test harness (mocked HTTP) ────────────────────────────────────────────


class _StubResponse:
    def __init__(self, payload: dict[str, Any] | None = None, text: str = ""):
        self._payload = payload or {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._payload


class _Router:
    """Routes ``safe_request`` calls to canned responses by URL substring."""

    def __init__(self, routes: list[tuple[str, tuple[Any, Any]]]):
        # routes: list of (url_substring, (response_or_None, error_or_None))
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, op, **kwargs):
        self.calls.append({"method": method, "url": url, "op": op, **kwargs})
        for substring, result in self._routes:
            if substring in url:
                return result
        raise AssertionError(f"No stub route for {method.upper()} {url}")


async def _run(monkeypatch, tool_name, routes, **arguments):
    router = _Router(routes)
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", router)

    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)

    result = await executor.execute_tool(
        ToolCall(id="c1", name=tool_name, arguments=arguments)
    )
    return router, result


# ── Tool registration / definitions ───────────────────────────────────────


def _tool_names(tools):
    return {t.name for t in tools}


def test_new_tools_are_registered():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    names = _tool_names(tools)
    assert "get_microsoft_file_by_url" in names
    assert "get_microsoft_site_file_content" in names


def test_search_files_definition_mentions_sharepoint():
    executor = CallableToolExecutor()
    tools = build_tools(token="dummy", executor=executor)
    definition = next(t for t in tools if t.name == "search_microsoft_files")
    assert "SharePoint" in definition.description


# ── get_microsoft_file_by_url ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_file_by_url_rejects_non_http(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[],
        url="/Documents/report.txt",
    )
    assert "http(s) URL" in result.content
    # Must short-circuit before hitting Graph.
    assert router.calls == []


@pytest.mark.asyncio
async def test_get_file_by_url_reads_text(monkeypatch):
    url = "https://contoso.sharepoint.com/:t:/s/site/AABB?e=tok"
    drive_item = {
        "id": "ITEM1",
        "name": "notes.txt",
        "size": 12,
        "file": {"mimeType": "text/plain"},
        "parentReference": {"driveId": "DRIVE1"},
        "webUrl": url,
    }
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(drive_item), None)),
            ("/content", (_StubResponse(text="hello world!"), None)),
        ],
        url=url,
    )
    assert not result.is_error
    assert "hello world!" in result.content

    # The resolve call must target /shares/{u!...}/driveItem.
    share_call = router.calls[0]
    assert "/shares/" in share_call["url"]
    assert _encode_share_id(url) in share_call["url"]
    # The content call must be addressed by drive/item id, not the raw path.
    content_call = router.calls[1]
    assert "/drives/DRIVE1/items/ITEM1/content" in content_call["url"]


@pytest.mark.asyncio
async def test_get_file_by_url_reads_excel(monkeypatch):
    url = "https://contoso.sharepoint.com/:x:/r/sites/finance/_layouts/15/Doc.aspx?file=Budget.xlsx"
    drive_item = {
        "id": "XL1",
        "name": "Budget FY26.xlsx",
        "size": 9000,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            )
        },
        "parentReference": {"driveId": "DRV"},
        "webUrl": url,
    }
    worksheets = {"value": [{"id": "S1", "name": "Summary", "position": 0}]}
    # usedRange is now metadata-only (address + counts, no ``text``)…
    used_range_meta = {"address": "Summary!A1:B3", "rowCount": 3, "columnCount": 2}
    # …and the cell text comes from a bounded ``range(address=…)`` fetch.
    range_text = {
        "text": [["Category", "Amount"], ["Revenue", "1000"], ["Opex", "400"]],
    }
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(drive_item), None)),
            ("/usedRange", (_StubResponse(used_range_meta), None)),
            ("range(address=", (_StubResponse(range_text), None)),
            ("/workbook/worksheets", (_StubResponse(worksheets), None)),
        ],
        url=url,
    )
    assert not result.is_error
    assert "Excel workbook" in result.content
    assert "Summary" in result.content
    assert "Revenue\t1000" in result.content
    assert "Opex\t400" in result.content
    # The metadata call must not request the (potentially huge) cell text.
    meta_calls = [c for c in router.calls if "/usedRange" in c["url"]]
    assert meta_calls and "text" not in meta_calls[0]["params"]["$select"]
    # Text is fetched from a bounded range anchored at the used-range top-left.
    assert any("range(address='A1:B3')" in c["url"] for c in router.calls)
    # It must NOT have tried to download the binary as text.
    assert not any("/content" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_get_file_by_url_propagates_resolve_error(monkeypatch):
    url = "https://contoso.sharepoint.com/:u:/s/site/NOPE"
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (None, "Error: Microsoft resource not found."))],
        url=url,
    )
    assert "not found" in result.content
    # Only the resolve call should have happened.
    assert len(router.calls) == 1


@pytest.mark.asyncio
async def test_get_file_by_url_handles_missing_drive_id(monkeypatch):
    url = "https://contoso.sharepoint.com/:u:/s/site/AABB"
    drive_item = {"id": "ITEM1", "name": "x.txt", "parentReference": {}}
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (_StubResponse(drive_item), None))],
        url=url,
    )
    assert "could not resolve" in result.content.lower()


# ── get_microsoft_site_file_content ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_site_file_content_reads_text(monkeypatch):
    drive_item = {
        "id": "SITEITEM",
        "name": "readme.md",
        "size": 5,
        "file": {"mimeType": "text/markdown"},
        "webUrl": "https://contoso.sharepoint.com/sites/eng/readme.md",
    }
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(drive_item), None)),
            ("/content", (_StubResponse(text="# Hi"), None)),
        ],
        site_id="SITE123",
        item_path="docs/readme.md",
    )
    assert not result.is_error
    assert "# Hi" in result.content
    # Metadata lookup is rooted at the site drive.
    assert "/sites/SITE123/drive/root:/docs/readme.md" in router.calls[0]["url"]
    # Content addressed by item id under the site drive.
    assert "/sites/SITE123/drive/items/SITEITEM/content" in router.calls[1]["url"]


@pytest.mark.asyncio
async def test_get_site_file_content_requires_path(monkeypatch):
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[],
        site_id="SITE123",
        item_path="   ",
    )
    assert "provide the file path" in result.content
    assert router.calls == []


# ── list_microsoft_site_files (usable item_path) ───────────────────────────


@pytest.mark.asyncio
async def test_list_site_files_emits_relative_item_path(monkeypatch):
    """Each entry's ``Path`` must be the library-root-relative path so a file's
    path can be passed straight to get_microsoft_site_file_content as
    ``item_path`` — unambiguous even for files nested in a subfolder."""
    listing = {
        "value": [
            {"id": "F1", "name": "Sub", "folder": {"childCount": 2}, "webUrl": "u/sub"},
            {
                "id": "X1",
                "name": "Budget.xlsx",
                "size": 9000,
                "lastModifiedDateTime": "2026-05-01T10:00:00Z",
                "webUrl": "u/b",
            },
        ]
    }
    router, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        routes=[("/children", (_StubResponse(listing), None))],
        site_id="SITE1",
        folder="Reports",
    )
    assert not result.is_error
    # A file's Path is folder-qualified and usable verbatim as item_path.
    assert "Path: Reports/Budget.xlsx" in result.content
    # A folder's Path is also surfaced (to pass back as the folder argument).
    assert "Path: Reports/Sub" in result.content
    # The listing targeted the requested subfolder, not the root.
    assert "root:/Reports:/children" in router.calls[0]["url"]


@pytest.mark.asyncio
async def test_list_site_files_root_path_is_bare_name(monkeypatch):
    """At the library root the Path is just the file name (no leading slash)."""
    listing = {"value": [{"id": "X1", "name": "README.md", "size": 5, "webUrl": "u/r"}]}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        routes=[("/children", (_StubResponse(listing), None))],
        site_id="SITE1",
    )
    assert not result.is_error
    assert "Path: README.md" in result.content
    assert "root/children" in router.calls[0]["url"]


@pytest.mark.asyncio
async def test_list_site_files_encodes_folder_and_clamps_count(monkeypatch):
    """A folder name with reserved characters must be percent-encoded into the
    Graph path, and ``count`` must be clamped before it reaches ``$top``."""
    router, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        routes=[("/children", (_StubResponse({"value": []}), None))],
        site_id="SITE1",
        folder="Q1 Reports#draft",
        count=999,
    )
    assert not result.is_error
    url = router.calls[0]["url"]
    # No raw space or '#' leaks into the URL path (they are percent-encoded).
    assert "Q1 Reports#draft" not in url
    assert "Q1%20Reports%23draft" in url
    # count is clamped to the 1..50 range.
    assert router.calls[0]["params"]["$top"] == "50"


@pytest.mark.asyncio
async def test_get_site_file_content_percent_encodes_path(monkeypatch):
    """``item_path`` with spaces / reserved chars must be percent-encoded into
    the Graph URL (so a name like ``Budget #2.xlsx`` doesn't truncate the path
    at ``#`` or trip httpx InvalidURL), keeping ``/`` as the separator."""
    drive_item = {
        "id": "SI",
        "name": "Budget #2.txt",
        "size": 5,
        "file": {"mimeType": "text/plain"},
        "webUrl": "u",
    }
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(drive_item), None)),
            ("/content", (_StubResponse(text="hi"), None)),
        ],
        site_id="SITE1",
        item_path="Reports/Budget #2.txt",
    )
    assert not result.is_error
    meta_url = router.calls[0]["url"]
    # The path is percent-encoded; '/' stays a separator, '#'/space do not leak.
    assert "/sites/SITE1/drive/root:/Reports/Budget%20%232.txt" in meta_url
    assert "#2.txt" not in meta_url


# ── search_microsoft_files (unified search) ────────────────────────────────


@pytest.mark.asyncio
async def test_search_files_uses_unified_search_api(monkeypatch):
    search_payload = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            {
                                "resource": {
                                    "name": "Budget FY26.xlsx",
                                    "size": 9000,
                                    "lastModifiedDateTime": "2026-05-01T10:00:00Z",
                                    "webUrl": "https://contoso.sharepoint.com/sites/finance/Budget.xlsx",
                                }
                            }
                        ]
                    }
                ]
            }
        ]
    }
    router, result = await _run(
        monkeypatch,
        "search_microsoft_files",
        routes=[("/search/query", (_StubResponse(search_payload), None))],
        query="Budget FY26",
    )
    assert not result.is_error
    assert "Budget FY26.xlsx" in result.content
    assert "sites/finance" in result.content

    # Must POST to the unified search endpoint with driveItem entities.
    call = router.calls[0]
    assert call["method"] == "post"
    assert "/search/query" in call["url"]
    body = call["json"]["requests"][0]
    assert body["entityTypes"] == ["driveItem"]
    assert body["query"]["queryString"] == "Budget FY26"


@pytest.mark.asyncio
async def test_search_files_empty(monkeypatch):
    empty = {"value": [{"hitsContainers": [{"hits": []}]}]}
    _, result = await _run(
        monkeypatch,
        "search_microsoft_files",
        routes=[("/search/query", (_StubResponse(empty), None))],
        query="nope",
    )
    assert "No files matching 'nope'" in result.content


@pytest.mark.asyncio
async def test_search_files_caps_size_at_25(monkeypatch):
    empty = {"value": []}
    router, _ = await _run(
        monkeypatch,
        "search_microsoft_files",
        routes=[("/search/query", (_StubResponse(empty), None))],
        query="x",
        count=999,
    )
    body = router.calls[0]["json"]["requests"][0]
    assert body["size"] == 25


# ── list_microsoft_sharepoint_sites (multi-source enumeration) ─────────────


@pytest.mark.asyncio
async def test_list_sites_aggregates_search_followed_and_group_sites(monkeypatch):
    # /sites?search=* returns SITE_A + SITE_C; group resolves SITE_A (dup);
    # followedSites returns SITE_B. Result must contain all three, once each.
    search = {
        "value": [
            {"id": "SITE_A", "displayName": "Finance", "webUrl": "u/a"},
            {"id": "SITE_C", "displayName": "Comms", "webUrl": "u/c"},
        ]
    }
    followed = {"value": [{"id": "SITE_B", "displayName": "Followed", "webUrl": "u/b"}]}
    memberof = {
        "value": [
            {"id": "G1", "displayName": "Finance Team", "groupTypes": ["Unified"]}
        ]
    }
    group_site = {"id": "SITE_A", "displayName": "Finance", "webUrl": "u/a"}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse(memberof), None)),
            ("/sites/root", (_StubResponse(group_site), None)),
            ("followedSites", (_StubResponse(followed), None)),
            ("/sites?search=", (_StubResponse(search), None)),
        ],
    )
    assert not result.is_error
    assert "Found 3 site(s)" in result.content
    assert result.content.count("SITE_A") == 1
    for name in ("Finance", "Comms", "Followed"):
        assert name in result.content
    # The wildcard listing (not a keyword search) must be used.
    assert any("/sites?search=*" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_list_sites_keyword_query_uses_search_only(monkeypatch):
    search = {"value": [{"id": "S1", "displayName": "Finance Site", "webUrl": "u"}]}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[("/sites?search=", (_StubResponse(search), None))],
        query="finance",
    )
    assert "Finance Site" in result.content
    # Keyword path hits only the search endpoint — no group/followed walk.
    assert len(router.calls) == 1
    assert "search=finance" in router.calls[0]["url"]


@pytest.mark.asyncio
async def test_list_sites_skips_groups_without_a_site(monkeypatch):
    # A plain security group (no Unified/Team) must NOT trigger a /sites/root
    # lookup. If it did, the router would raise (no route registered).
    memberof = {
        "value": [{"id": "SG", "displayName": "Security", "groupTypes": []}]
    }
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse(memberof), None)),
            ("followedSites", (_StubResponse({"value": []}), None)),
            ("/sites?search=", (_StubResponse({"value": []}), None)),
        ],
    )
    assert not any("/sites/root" in c["url"] for c in router.calls)
    assert "get_microsoft_file_by_url" in result.content


@pytest.mark.asyncio
async def test_list_sites_empty_points_to_by_url(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse({"value": []}), None)),
            ("followedSites", (_StubResponse({"value": []}), None)),
            ("/sites?search=", (_StubResponse({"value": []}), None)),
        ],
    )
    assert "get_microsoft_file_by_url" in result.content


@pytest.mark.asyncio
async def test_list_sites_degrades_when_search_index_fails(monkeypatch):
    # Search index errors, but group sites still come through.
    memberof = {
        "value": [{"id": "G1", "displayName": "Eng", "resourceProvisioningOptions": ["Team"]}]
    }
    group_site = {"id": "SITE_X", "displayName": "Engineering", "webUrl": "u/x"}
    _, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse(memberof), None)),
            ("/sites/root", (_StubResponse(group_site), None)),
            ("followedSites", (_StubResponse({"value": []}), None)),
            ("/sites?search=", (None, "Error: Microsoft server error (500).")),
        ],
    )
    assert "Engineering" in result.content
    assert "Found 1 site(s)" in result.content


@pytest.mark.asyncio
async def test_workbook_url_encodes_braced_worksheet_id(monkeypatch):
    """Graph worksheet ids are wrapped in curly braces ("{...}"); they must be
    URL-encoded before being interpolated into the usedRange path, otherwise
    httpx raises InvalidURL on the raw braces."""
    url = "https://contoso.sharepoint.com/:x:/s/site/AABB?e=tok"
    drive_item = {
        "id": "ITEM1",
        "name": "Budget.xlsx",
        "size": 5000,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            )
        },
        "parentReference": {"driveId": "DRV"},
        "webUrl": url,
    }
    worksheets = {"value": [{"id": "{8A7B-0001}", "name": "Summary", "position": 0}]}
    used_range_meta = {"address": "Summary!A1:B1", "rowCount": 1, "columnCount": 2}
    range_text = {"text": [["A", "B"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(drive_item), None)),
            ("/usedRange", (_StubResponse(used_range_meta), None)),
            ("range(address=", (_StubResponse(range_text), None)),
            ("/workbook/worksheets", (_StubResponse(worksheets), None)),
        ],
        url=url,
    )
    assert not result.is_error
    used_range_calls = [c for c in router.calls if "/usedRange" in c["url"]]
    assert used_range_calls
    target = used_range_calls[0]["url"]
    # No raw braces in the path; the id is percent-encoded instead.
    assert "{" not in target and "}" not in target
    assert "%7B" in target and "%7D" in target


@pytest.mark.asyncio
async def test_list_sites_header_reports_total_not_shown(monkeypatch):
    """When more sites are discovered than the limit shows, the header reports
    the total discovered (with a 'showing N' note), not just the returned
    count."""
    search = {
        "value": [
            {"id": "SITE_A", "displayName": "A", "webUrl": "u/a"},
            {"id": "SITE_B", "displayName": "B", "webUrl": "u/b"},
        ]
    }
    followed = {"value": [{"id": "SITE_C", "displayName": "C", "webUrl": "u/c"}]}
    _, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse({"value": []}), None)),
            ("followedSites", (_StubResponse(followed), None)),
            ("/sites?search=", (_StubResponse(search), None)),
        ],
        count=2,
    )
    assert not result.is_error
    assert "Found 3 site(s)" in result.content
    assert "showing 2" in result.content


@pytest.mark.asyncio
async def test_list_sites_follows_group_membership_pagination(monkeypatch):
    """_collect_group_sites must follow @odata.nextLink so a user in >200 groups
    doesn't silently lose group-backed sites beyond the first page."""
    page1 = {
        "value": [{"id": "G1", "displayName": "T1", "groupTypes": ["Unified"]}],
        "@odata.nextLink": (
            "https://graph.microsoft.com/v1.0/me/memberOf/"
            "microsoft.graph.group?$skiptoken=abc"
        ),
    }
    page2 = {"value": [{"id": "G2", "displayName": "T2", "groupTypes": ["Unified"]}]}
    group_site = {"id": "SITE_G", "displayName": "Group Site", "webUrl": "u/g"}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            # More specific skiptoken route first so the nextLink page resolves
            # to page2 instead of re-matching the page1 "memberOf" route.
            ("skiptoken", (_StubResponse(page2), None)),
            ("memberOf", (_StubResponse(page1), None)),
            ("/sites/root", (_StubResponse(group_site), None)),
            ("followedSites", (_StubResponse({"value": []}), None)),
            ("/sites?search=", (_StubResponse({"value": []}), None)),
        ],
    )
    assert not result.is_error
    # The second membership page (via @odata.nextLink) was fetched.
    assert any("skiptoken" in c["url"] for c in router.calls)
    # Both pages' groups had their sites resolved.
    assert sum(1 for c in router.calls if "/sites/root" in c["url"]) == 2


@pytest.mark.asyncio
async def test_list_sites_resolves_all_groups_beyond_batch(monkeypatch):
    """Every eligible group's site must be resolved — even well past the old
    100-group cap — so a user in many M365 groups/Teams doesn't silently lose
    group-backed sites. Resolution is batched, not truncated."""
    memberof = {
        "value": [
            {"id": f"G{i}", "displayName": f"T{i}", "groupTypes": ["Unified"]}
            for i in range(120)
        ]
    }
    group_site = {"id": "SITE_G", "displayName": "Group Site", "webUrl": "u/g"}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[
            ("memberOf", (_StubResponse(memberof), None)),
            ("/sites/root", (_StubResponse(group_site), None)),
            ("followedSites", (_StubResponse({"value": []}), None)),
            ("/sites?search=", (_StubResponse({"value": []}), None)),
        ],
    )
    assert not result.is_error
    # All 120 groups were resolved (no [:100] cap), even though they dedupe to
    # a single site in the output.
    assert sum(1 for c in router.calls if "/sites/root" in c["url"]) == 120


@pytest.mark.asyncio
async def test_list_sites_keyword_query_is_url_encoded(monkeypatch):
    """A multi-word / reserved-char query must be percent-encoded before being
    interpolated into the URL — a raw space would make httpx raise InvalidURL
    before the request is sent."""
    search = {"value": [{"id": "S1", "displayName": "Finance Team Site", "webUrl": "u"}]}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[("/sites?search=", (_StubResponse(search), None))],
        query="finance team",
    )
    assert not result.is_error
    url = router.calls[0]["url"]
    assert " " not in url
    assert "search=finance%20team" in url


@pytest.mark.asyncio
async def test_list_sites_clamps_nonpositive_count(monkeypatch):
    """A ``count`` <= 0 must not wipe the result to 'No SharePoint sites found';
    it is clamped to >= 1 so discovered sites are still returned, and ``$top``
    is sent as a positive value."""
    search = {"value": [{"id": "S1", "displayName": "Finance", "webUrl": "u"}]}
    router, result = await _run(
        monkeypatch,
        "list_microsoft_sharepoint_sites",
        routes=[("/sites?search=", (_StubResponse(search), None))],
        query="finance",
        count=0,
    )
    assert not result.is_error
    assert "Finance" in result.content
    assert "No SharePoint sites found" not in result.content
    assert router.calls[0]["params"]["$top"] == "1"


@pytest.mark.asyncio
async def test_workbook_fetches_only_bounded_range_for_large_sheet(monkeypatch):
    """A huge worksheet must not pull its whole grid: ``usedRange`` is read
    metadata-only (no ``text``) and the cells come from a ``range(address=…)``
    window clamped to _MAX_WORKBOOK_ROWS×_MAX_WORKBOOK_COLS, anchored at the
    used-range origin. Total counts still surface in the truncation note."""
    url = "https://contoso.sharepoint.com/:x:/s/site/BIG?e=tok"
    drive_item = {
        "id": "XL2",
        "name": "Huge.xlsx",
        "size": 1000,
        "file": {
            "mimeType": (
                "application/vnd.openxmlformats-officedocument"
                ".spreadsheetml.sheet"
            )
        },
        "parentReference": {"driveId": "DRV"},
        "webUrl": url,
    }
    worksheets = {"value": [{"id": "S1", "name": "Data", "position": 0}]}
    used_range_meta = {"address": "Data!A1:CV5000", "rowCount": 5000, "columnCount": 100}
    range_text = {"text": [["a", "b"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(drive_item), None)),
            ("/usedRange", (_StubResponse(used_range_meta), None)),
            ("range(address=", (_StubResponse(range_text), None)),
            ("/workbook/worksheets", (_StubResponse(worksheets), None)),
        ],
        url=url,
    )
    assert not result.is_error
    meta_calls = [c for c in router.calls if "/usedRange" in c["url"]]
    assert meta_calls and "text" not in meta_calls[0]["params"]["$select"]
    # Clamped to 60 rows × 25 cols (A..Y), anchored at the used-range origin A1.
    assert any("range(address='A1:Y60')" in c["url"] for c in router.calls)
    # The true totals are still reported in the truncation note.
    assert "5000" in result.content and "100" in result.content


# ── _split_workbook_path_and_range ────────────────────────────────────────


def test_split_workbook_path_no_suffix():
    """Clean paths (no embedded range) are returned unchanged."""
    assert _split_workbook_path_and_range("Reports/Budget.xlsx") == (
        "Reports/Budget.xlsx",
        None,
        None,
    )


def test_split_workbook_path_exclamation_range_only():
    """Bare range after '!' (no sheet name): worksheet=None, range extracted."""
    path, ws, rng = _split_workbook_path_and_range("Alerts/Alerts List.xlsx!A61:E180")
    assert path == "Alerts/Alerts List.xlsx"
    assert ws is None
    assert rng == "A61:E180"


def test_split_workbook_path_exclamation_sheet_and_range():
    """Sheet name + range after '!': both extracted correctly."""
    path, ws, rng = _split_workbook_path_and_range("Alerts/Alerts.xlsx!Feuil1!A61:E100")
    assert path == "Alerts/Alerts.xlsx"
    assert ws == "Feuil1"
    assert rng == "A61:E100"


def test_split_workbook_path_colon_sheet_and_range():
    """Colon separator: 'file.xlsx:Sheet1!A1:B10' — sheet and range extracted."""
    path, ws, rng = _split_workbook_path_and_range("file.xlsx:Sheet1!A1:B10")
    assert path == "file.xlsx"
    assert ws == "Sheet1"
    assert rng == "A1:B10"


def test_split_workbook_path_hash_separator():
    """Hash separator: 'file.xlsx#Sheet1!A1:B10' — sheet and range extracted."""
    path, ws, rng = _split_workbook_path_and_range("dir/file.xlsx#Sheet1!A1:B10")
    assert path == "dir/file.xlsx"
    assert ws == "Sheet1"
    assert rng == "A1:B10"


def test_split_workbook_path_query_range_param():
    """Query-style: 'file.xlsx?range=Sheet1!A1:E100' parsed correctly."""
    path, ws, rng = _split_workbook_path_and_range("file.xlsx?range=Sheet1!A1:E100")
    assert path == "file.xlsx"
    assert ws == "Sheet1"
    assert rng == "A1:E100"


def test_split_workbook_path_slash_separator():
    """Slash separator: 'file.xlsx/Sheet1!A1:B10' — sheet and range extracted."""
    path, ws, rng = _split_workbook_path_and_range("file.xlsx/Sheet1!A1:B10")
    assert path == "file.xlsx"
    assert ws == "Sheet1"
    assert rng == "A1:B10"


def test_split_workbook_path_embedded_graph_path_recovers_worksheet():
    """An embedded Graph API route yields the
    clean file path AND recovers the worksheet it addressed."""
    path, ws, rng = _split_workbook_path_and_range(
        "file.xlsx:/workbook/worksheets/Sheet1/usedRange"
    )
    assert path == "file.xlsx"
    assert ws == "Sheet1"
    assert rng is None


def test_split_workbook_path_embedded_graph_path_recovers_range():
    """The quoted-worksheet + range(address=…) Graph route form is parsed too."""
    path, ws, rng = _split_workbook_path_and_range(
        "Alerts/Alerts.xlsx:/workbook/worksheets('Feuil1')/range(address='A61:E180')"
    )
    assert path == "Alerts/Alerts.xlsx"
    assert ws == "Feuil1"
    assert rng == "A61:E180"


def test_split_workbook_path_xlsm():
    """Works for .xlsm extension too."""
    path, ws, rng = _split_workbook_path_and_range("macro.xlsm!A1:B5")
    assert path == "macro.xlsm"
    assert ws is None
    assert rng == "A1:B5"


def test_split_workbook_path_anchors_on_last_workbook_extension():
    """A folder whose name contains '.xlsx' must not shadow the real workbook
    later in the path — the split anchors on the LAST workbook extension."""
    path, ws, rng = _split_workbook_path_and_range(
        "archive.xlsx_backup/report.xlsm!A1:B2"
    )
    assert path == "archive.xlsx_backup/report.xlsm"
    assert ws is None
    assert rng == "A1:B2"


def test_split_workbook_path_preserves_nested_path_under_xlsx_folder():
    """A '/' suffix that doesn't look like a worksheet/range spec is a real
    nested path (folder named '*.xlsx') and must be left untouched."""
    raw = "exports.xlsx/data.csv"
    assert _split_workbook_path_and_range(raw) == (raw, None, None)


def test_split_workbook_path_preserves_hash_in_filename():
    """'#' is a legal SharePoint filename character — a bare-word suffix after
    it must not be misread as a worksheet name."""
    raw = "Reports/Budget.xlsx#final"
    assert _split_workbook_path_and_range(raw) == (raw, None, None)


def test_split_workbook_path_preserves_exclamation_in_filename():
    """'!' is a legal SharePoint filename character — only split when the
    suffix provably looks like a worksheet/range spec."""
    raw = "Reports/Budget.xlsx!notes"
    assert _split_workbook_path_and_range(raw) == (raw, None, None)


def test_split_workbook_path_preserves_exclamation_without_valid_range():
    """A '!'-separated suffix whose range part is NOT a valid A1 range (e.g.
    'Budget.xlsx!notes!') may be a legitimate file name — leave it untouched."""
    raw = "Reports/Budget.xlsx!notes!"
    assert _split_workbook_path_and_range(raw) == (raw, None, None)


def test_split_workbook_path_colon_bare_worksheet():
    """':' cannot appear in SharePoint paths, so a bare-word suffix after it
    can only be a worksheet name."""
    path, ws, rng = _split_workbook_path_and_range("file.xlsx:Feuil1")
    assert path == "file.xlsx"
    assert ws == "Feuil1"
    assert rng is None


def test_split_workbook_path_worksheet_named_workbook():
    """A worksheet legitimately named 'workbook1' must not be mistaken for an
    embedded Graph API route (only 'workbook/…' is the route form)."""
    path, ws, rng = _split_workbook_path_and_range("file.xlsx:workbook1")
    assert path == "file.xlsx"
    assert ws == "workbook1"
    assert rng is None


def test_split_workbook_path_trailing_separator_kept_for_legal_chars():
    """'!' and '#' are legal filename characters — a bare trailing separator
    may be part of the real name and must be preserved."""
    assert _split_workbook_path_and_range("Report.xlsx#") == (
        "Report.xlsx#",
        None,
        None,
    )
    assert _split_workbook_path_and_range("Report.xlsx!") == (
        "Report.xlsx!",
        None,
        None,
    )


def test_split_workbook_path_trailing_separator_stripped_for_illegal_chars():
    """A trailing ':' / '/' cannot be part of a SharePoint path — strip it."""
    assert _split_workbook_path_and_range("Report.xlsx:") == (
        "Report.xlsx",
        None,
        None,
    )
    assert _split_workbook_path_and_range("Report.xlsx/") == (
        "Report.xlsx",
        None,
        None,
    )


# ── get_microsoft_site_file_content – worksheet/range params ─────────────


_EXCEL_ITEM = {
    "id": "XLITEM",
    "name": "Alerts List.xlsx",
    "size": 5000,
    "file": {
        "mimeType": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
    },
    "webUrl": "https://contoso.sharepoint.com/sites/eng/Alerts/Alerts%20List.xlsx",
}


@pytest.mark.asyncio
async def test_get_site_file_content_worksheet_and_range(monkeypatch):
    """Explicit worksheet + cell_range params build the correct Graph URL."""
    range_text = {
        "text": [
            ["Date", "Alert", "Severity", "Status", "Owner"],
            ["2026-07-11", "CVE-2026-001", "High", "Open", "alice"],
        ]
    }
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A61:E100",
    )
    assert not result.is_error
    # Correct Graph workbook range URL must be used.
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert len(range_calls) == 1
    url = range_calls[0]["url"]
    assert "/workbook/worksheets/" in url
    assert "Feuil1" in url
    assert "range(address='A61:E100')" in url
    # The cell range in the URL must NOT start with the sheet name.
    assert "Feuil1!A61" not in url
    # Data is present in the output.
    assert "CVE-2026-001" in result.content


@pytest.mark.asyncio
async def test_get_site_file_content_embedded_range_sanitized(monkeypatch):
    """item_path with an embedded range suffix is sanitized automatically.

    This covers the production failure pattern where the LLM agent passes
    ``item_path='Alerts/Alerts List.xlsx!A61:E180'`` (no sheet name, only a
    range suffix).  The suffix is stripped so the meta lookup uses the clean
    path, preventing the malformed Graph URL.  Because no worksheet name was
    identified the function falls back to reading all sheets via
    ``_read_workbook``.
    """
    worksheets = {"value": [{"id": "S1", "name": "Feuil1", "position": 0}]}
    used_range_meta = {"address": "Feuil1!A1:E200", "rowCount": 200, "columnCount": 5}
    range_text = {"text": [["Date", "Alert"], ["2026-07-11", "CVE-2026-001"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            # More specific routes first: usedRange and range(address= must be
            # checked before /workbook/worksheets (which is a substring of both).
            ("/usedRange", (_StubResponse(used_range_meta), None)),
            ("range(address=", (_StubResponse(range_text), None)),
            ("/workbook/worksheets", (_StubResponse(worksheets), None)),
        ],
        site_id="SITE1",
        # LLM appended the range directly to the path (the bad pattern, no sheet name).
        item_path="Alerts/Alerts List.xlsx!A61:E180",
    )
    assert not result.is_error
    # The meta lookup must use the clean path without the range suffix.
    meta_url = router.calls[0]["url"]
    assert "!A61" not in meta_url
    assert "E180" not in meta_url
    assert "/drive/root:/Alerts/Alerts%20List.xlsx" in meta_url
    # Without a worksheet name, the fallback full-workbook reader runs.
    assert any("/workbook/worksheets" in c["url"] for c in router.calls)
    assert "CVE-2026-001" in result.content


@pytest.mark.asyncio
async def test_get_site_file_content_embedded_sheet_and_range_sanitized(monkeypatch):
    """'file.xlsx!Sheet1!A1:B10' — sheet name and range both extracted from path."""
    range_text = {"text": [["A", "B"], ["1", "2"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx:Feuil1!A61:E100",
    )
    assert not result.is_error
    meta_url = router.calls[0]["url"]
    assert "Feuil1" not in meta_url
    assert "E100" not in meta_url
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert any("Feuil1" in c["url"] for c in range_calls)
    assert any("range(address='A61:E100')" in c["url"] for c in range_calls)


@pytest.mark.asyncio
async def test_get_site_file_content_worksheet_only_reads_used_range(monkeypatch):
    """worksheet param without cell_range reads the bounded used range for that sheet."""
    used_range_meta = {"address": "Feuil1!A1:E10", "rowCount": 10, "columnCount": 5}
    range_text = {"text": [["h1", "h2", "h3", "h4", "h5"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("/usedRange", (_StubResponse(used_range_meta), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
    )
    assert not result.is_error
    # The usedRange metadata call targets only the specified worksheet.
    used_range_calls = [c for c in router.calls if "/usedRange" in c["url"]]
    assert len(used_range_calls) == 1
    assert "Feuil1" in used_range_calls[0]["url"]
    # A bounded range(address=…) call follows.
    assert any("range(address=" in c["url"] for c in router.calls)
    assert "h1" in result.content


@pytest.mark.asyncio
async def test_get_site_file_content_explicit_params_win_over_embedded_range(monkeypatch):
    """Explicit worksheet/cell_range params take precedence over embedded path suffix."""
    range_text = {"text": [["X", "Y"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        # Path has an embedded range suffix that would normally be extracted.
        item_path="Alerts/Alerts List.xlsx!BadSheet!Z99:Z100",
        # But explicit params are provided — they should win.
        worksheet="RealSheet",
        cell_range="A1:B2",
    )
    assert not result.is_error
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert any("RealSheet" in c["url"] for c in range_calls)
    assert any("range(address='A1:B2')" in c["url"] for c in range_calls)
    # The "bad" embedded values must not appear in any URL.
    assert not any("BadSheet" in c["url"] for c in router.calls)
    assert not any("Z99" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_get_site_file_content_rejects_malformed_cell_range(monkeypatch):
    """A cell_range that isn't an A1-style address is rejected with an
    actionable error BEFORE any workbook request — the value is LLM-controlled
    and would otherwise break out of the range(address='…') OData segment."""
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None))],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A1') or 1=1",
    )
    assert "invalid cell_range" in result.content
    # Only the meta lookup ran — the malformed value never reached a URL.
    assert not any("range(address=" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_get_site_file_content_rejects_zero_row_range(monkeypatch):
    """A1 addresses are 1-based — row 0 ('A0:B10') is invalid and must be
    rejected early instead of reaching Graph and failing with a confusing 400."""
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None))],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A0:B10",
    )
    assert "invalid cell_range" in result.content
    assert not any("range(address=" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_get_site_file_content_rejects_column_span_range(monkeypatch):
    """Column/row spans ('A:E', '1:50') have unknown extent and would bypass
    the pre-fetch clamping — they are rejected with an actionable error."""
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None))],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A:E",
    )
    assert "invalid cell_range" in result.content
    assert not any("range(address=" in c["url"] for c in router.calls)


@pytest.mark.asyncio
async def test_get_site_file_content_cell_range_requires_worksheet(monkeypatch):
    """An explicit cell_range without a worksheet fails fast instead of
    silently falling back to an expensive full-workbook read."""
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        cell_range="A61:E100",
    )
    assert "cell_range requires the 'worksheet' parameter" in result.content
    # Short-circuits before any Graph request.
    assert router.calls == []


@pytest.mark.asyncio
async def test_get_site_file_content_clamps_oversized_cell_range(monkeypatch):
    """An explicit cell_range larger than the display window is clamped before
    the fetch (same pattern as _read_workbook) and the truncation is surfaced
    in the output instead of silently dropping rows."""
    range_text = {"text": [["a", "b"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A61:E180",  # 120 rows requested; display cap is 60.
    )
    assert not result.is_error
    # The fetch was clamped to the 60-row window anchored at A61.
    assert any("range(address='A61:E120')" in c["url"] for c in router.calls)
    # The requested range and the clamping are both visible to the agent.
    assert "[A61:E180]" in result.content
    assert "showing 60/120" in result.content


@pytest.mark.asyncio
async def test_get_site_file_content_clamps_absolute_ref_range(monkeypatch):
    """Absolute refs ('$A$1:$Y$5000') pass the allowlist and must STILL be
    clamped — the '$'s are stripped before the bounding math so the clamp
    can't be silently skipped while the note claims it was applied."""
    range_text = {"text": [["a"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="$A$1:$Y$5000",
    )
    assert not result.is_error
    # Clamped to 60 rows × 25 cols (A..Y), anchored at A1, without '$'s.
    assert any("range(address='A1:Y60')" in c["url"] for c in router.calls)
    assert "showing 60/5000" in result.content


@pytest.mark.asyncio
async def test_get_site_file_content_empty_string_params_use_extracted(monkeypatch):
    """LLMs often send "" for omitted optional params — empty worksheet /
    cell_range must not mask the values extracted from an embedded suffix."""
    range_text = {"text": [["X"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        site_id="SITE1",
        item_path="Alerts/Alerts List.xlsx:Feuil1!A61:E100",
        worksheet="",
        cell_range="",
    )
    assert not result.is_error
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert any("Feuil1" in c["url"] for c in range_calls)
    assert any("range(address='A61:E100')" in c["url"] for c in range_calls)


# ── get_microsoft_file_content (OneDrive) – same sanitization contract ────


@pytest.mark.asyncio
async def test_get_file_content_worksheet_and_range(monkeypatch):
    """The OneDrive reader honours worksheet/cell_range identically to the
    SharePoint site reader (shared _read_drive_item contract)."""
    range_text = {"text": [["Date", "Alert"], ["2026-07-11", "CVE-2026-001"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        item_path="Alerts/Alerts List.xlsx",
        worksheet="Feuil1",
        cell_range="A61:E100",
    )
    assert not result.is_error
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert any("Feuil1" in c["url"] for c in range_calls)
    assert any("range(address='A61:E100')" in c["url"] for c in range_calls)
    assert "CVE-2026-001" in result.content


@pytest.mark.asyncio
async def test_get_file_content_embedded_range_sanitized(monkeypatch):
    """An embedded suffix in the OneDrive item_path is sanitized too."""
    range_text = {"text": [["X"]]}
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_content",
        routes=[
            ("/drive/root:/", (_StubResponse(_EXCEL_ITEM), None)),
            ("range(address=", (_StubResponse(range_text), None)),
        ],
        item_path="Alerts/Alerts List.xlsx!Feuil1!A61:E100",
    )
    assert not result.is_error
    meta_url = router.calls[0]["url"]
    assert "/me/drive/root:/Alerts/Alerts%20List.xlsx" in meta_url
    assert "!Feuil1" not in meta_url and "E100" not in meta_url
    range_calls = [c for c in router.calls if "range(address=" in c["url"]]
    assert any("range(address='A61:E100')" in c["url"] for c in range_calls)


# ── Documents: extracted server-side or pointed at download_microsoft_file


class _BytesResponse(_StubResponse):
    def __init__(self, content: bytes):
        super().__init__()
        self.content = content


_SHARE_URL = "https://contoso.sharepoint.com/:w:/s/sales/AABB?e=tok"


_GRAPH_DOWNLOAD_URL = "https://contoso.sharepoint.com/_layouts/15/download.aspx?UniqueId=ITEM9&tempauth=tok"


def _item(name: str, size: int, mime: str, *, download_url: str | None = None) -> dict[str, Any]:
    item = {
        "id": "ITEM9",
        "name": name,
        "size": size,
        "file": {"mimeType": mime},
        "parentReference": {"driveId": "DRIVE9"},
        "webUrl": _SHARE_URL,
    }
    if download_url:
        item["@microsoft.graph.downloadUrl"] = download_url
    return item


def _docx(*paragraphs: str, table: list[tuple[str, ...]] | None = None) -> bytes:
    import io

    from docx import Document

    doc = Document()
    for text in paragraphs:
        doc.add_paragraph(text)
    if table:
        grid = doc.add_table(rows=len(table), cols=len(table[0]))
        for r, row in enumerate(table):
            for c, value in enumerate(row):
                grid.cell(r, c).text = value
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.mark.asyncio
async def test_get_file_by_url_extracts_docx_text_including_tables(monkeypatch):
    """A Word file used to be answered "not text-readable" with a Graph URL
    the model could not use."""
    data = _docx("Action Item(s):", table=[("1", "Deploy the connector", "PWC")])
    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(_item("Minutes.docx", len(data), _DOCX_MIME)), None)),
            ("/content", (_BytesResponse(data), None)),
        ],
        url=_SHARE_URL,
    )

    assert not result.is_error
    assert "Action Item(s):" in result.content
    assert "| 1 | Deploy the connector | PWC |" in result.content
    assert "/drives/DRIVE9/items/ITEM9/content" in router.calls[1]["url"]


@pytest.mark.asyncio
async def test_long_docx_text_is_cut_with_a_footer(monkeypatch):
    from app.integrations.providers.microsoft._drive import _MAX_DOCUMENT_CHARS

    data = _docx("word " * (_MAX_DOCUMENT_CHARS // 4))
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(_item("Long.docx", len(data), _DOCX_MIME)), None)),
            ("/content", (_BytesResponse(data), None)),
        ],
        url=_SHARE_URL,
    )

    assert f"[... truncated: the first {_MAX_DOCUMENT_CHARS:,} of" in result.content
    assert f'download_microsoft_file(url="{_SHARE_URL}")' in result.content


@pytest.mark.asyncio
async def test_large_docx_points_at_download_tool_without_downloading(monkeypatch):
    from app.integrations.providers.microsoft._drive import _MAX_DOCUMENT_BYTES

    router, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(_item("Huge.docx", _MAX_DOCUMENT_BYTES + 1, _DOCX_MIME)), None)),
        ],
        url=_SHARE_URL,
    )

    assert "too large to extract inline" in result.content
    assert f'download_microsoft_file(url="{_SHARE_URL}")' in result.content
    assert all("/content" not in call["url"] for call in router.calls)


@pytest.mark.asyncio
async def test_unparseable_document_points_at_download_tool_not_its_bytes(monkeypatch):
    data = b"%PDF-1.7\n" + b"\x00\xff garbage" * 50
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(_item("Broken.pdf", len(data), "application/pdf")), None)),
            ("/content", (_BytesResponse(data), None)),
        ],
        url=_SHARE_URL,
    )

    assert "no extractable text" in result.content
    assert "download_microsoft_file" in result.content
    assert "garbage" not in result.content


@pytest.mark.asyncio
async def test_binary_file_points_at_download_tool(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (_StubResponse(_item("bundle.zip", 2048, "application/zip")), None))],
        url=_SHARE_URL,
    )

    assert "not text-readable inline" in result.content
    assert "download_microsoft_file" in result.content
    assert "Download URL" not in result.content


@pytest.mark.asyncio
async def test_binary_file_keeps_the_graph_download_url(monkeypatch):
    """The Download URL and Web URL lines are what the baseline answered."""
    item = _item("bundle.zip", 2048, "application/zip", download_url=_GRAPH_DOWNLOAD_URL)
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (_StubResponse(item), None))],
        url=_SHARE_URL,
    )

    assert "download_microsoft_file" in result.content
    assert f"Download URL: {_GRAPH_DOWNLOAD_URL}" in result.content
    assert f"Web URL: {_SHARE_URL}" in result.content


@pytest.mark.asyncio
async def test_extracted_docx_keeps_the_links_the_baseline_answered(monkeypatch):
    data = _docx("Action Item(s):")
    item = _item("Minutes.docx", len(data), _DOCX_MIME, download_url=_GRAPH_DOWNLOAD_URL)
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (_StubResponse(item), None)), ("/content", (_BytesResponse(data), None))],
        url=_SHARE_URL,
    )

    assert "Action Item(s):" in result.content
    assert f"Download URL: {_GRAPH_DOWNLOAD_URL}" in result.content
    assert f"Web URL: {_SHARE_URL}" in result.content


@pytest.mark.asyncio
async def test_inline_text_file_carries_the_links(monkeypatch):
    """An answer that inlines the content still carries the links."""
    item = _item("notes.txt", 5, "text/plain", download_url=_GRAPH_DOWNLOAD_URL)
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[("/shares/", (_StubResponse(item), None)), ("/content", (_StubResponse(text="hello"), None))],
        url=_SHARE_URL,
    )

    assert "hello" in result.content
    assert f"Download URL: {_GRAPH_DOWNLOAD_URL}" in result.content
    assert f"Web URL: {_SHARE_URL}" in result.content


@pytest.mark.asyncio
async def test_long_text_file_cut_states_total_and_next_step(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        routes=[
            ("/shares/", (_StubResponse(_item("log.txt", 12_000, "text/plain")), None)),
            ("/content", (_StubResponse(text="x" * 12_000), None)),
        ],
        url=_SHARE_URL,
    )

    assert "[... truncated: the first 10,000 of 12,000 characters are shown." in result.content
    assert "download_microsoft_file" in result.content
