"""Shared SharePoint / OneDrive folders, listed and read by their URL.

- ``list_microsoft_folder_by_url`` resolves a folder's sharing link or browser
  URL through ``/shares`` and lists its items, each with a URL the URL tools
  take as is; a path appended to a sharing link names an item under the folder
  it opens; pages follow ``@odata.nextLink`` through a page token that can only
  re-read the same folder.
- ``get_microsoft_file_by_url`` answers a folder with its first items and
  downloads nothing; ``download_microsoft_file`` refuses a folder.
- ``list_microsoft_site_files`` takes a site as its id, the Graph path form
  ``hostname:/sites/name`` or its URL, and says "site not accessible" when the
  account cannot open the site.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft._drive import (
    _encode_share_id,
    _next_page_token,
    _page_params,
    _sharing_link_splits,
)
from app.tools.base import CallableToolExecutor, ToolCall
from tests.integration_file_fakes import fenced_and_open

GRAPH = "https://graph.microsoft.com/v1.0"
HOST = "https://contoso.sharepoint.com"
LINK_PATH = f"{HOST}/:f:/s/test-site/EaBcDeFgHiJ"
LINK = f"{LINK_PATH}?e=Xy12Z"
FOLDER_WEB = f"{HOST}/sites/test-site/Shared%20Documents/Test%20Corporate"
SITE_ID = "contoso.sharepoint.com,1111-aaaa,2222-bbbb"

FOLDER = {
    "id": "FOLDER1",
    "name": "Test Corporate",
    "size": 4096,
    "folder": {"childCount": 2},
    "parentReference": {"driveId": "DRIVE1"},
    "webUrl": FOLDER_WEB,
}
SUBFOLDER = {
    "id": "SUB1",
    "name": "Contracts",
    "size": 2048,
    "folder": {"childCount": 1},
    "parentReference": {"driveId": "DRIVE1"},
    "webUrl": f"{FOLDER_WEB}/Contracts",
}
REPORT = {
    "id": "FILE1",
    "name": "Q3 report.txt",
    "size": 11,
    "file": {"mimeType": "text/plain"},
    "parentReference": {"driveId": "DRIVE1"},
    "webUrl": f"{FOLDER_WEB}/Q3%20report.txt",
}
CHILDREN = {
    "value": [
        {**SUBFOLDER, "lastModifiedDateTime": "2026-10-01T09:00:00Z"},
        {**REPORT, "lastModifiedDateTime": "2026-10-02T10:00:00Z"},
    ]
}


# ── Mocked Graph ──────────────────────────────────────────────────────────


class _Resp:
    def __init__(self, payload: Any = None, *, text: str = "", status: int = 200):
        self._payload = payload if payload is not None else {}
        self.text = text
        self.content = text.encode()
        self.status_code = status

    def json(self) -> Any:
        return self._payload


def _fail(status: int) -> _Resp:
    return _Resp({"error": {"code": "itemNotFound"}}, status=status)


class _Graph:
    """Answers ``safe_request`` from the first route whose matcher takes the URL.

    A failed answer follows ``safe_request``'s contract: the response comes
    back with the error only when the caller passed ``expected_statuses``.
    """

    def __init__(self, routes: list[tuple[Callable[[str], bool], _Resp]]):
        self._routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, action, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        for matches, resp in self._routes:
            if matches(url):
                if resp.status_code < 300:
                    return resp, None
                err = f"Error: {provider} returned {resp.status_code} while trying to {action}."
                return (resp if kwargs.get("expected_statuses") is not None else None), err
        raise AssertionError(f"No Graph route for {method.upper()} {url}")

    def urls(self) -> list[str]:
        return [call["url"] for call in self.calls]


def _ends(suffix: str) -> Callable[[str], bool]:
    return lambda url: url.endswith(suffix)


def _share(url: str, tail: str = "") -> Callable[[str], bool]:
    return _ends(f"/shares/{_encode_share_id(url)}/driveItem{tail}")


async def _run(monkeypatch, tool: str, routes, **arguments):
    graph = _Graph(routes)
    from app.integrations.providers import microsoft as ms

    monkeypatch.setattr(ms._common, "safe_request", graph)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=tool, arguments=arguments))
    return graph, result


def _item_url(*names: str) -> str:
    """The URL a listing of the shared folder gives the item under it at *names*."""
    return f"{LINK_PATH}/{'/'.join(names)}?e=Xy12Z"


# ── list_microsoft_folder_by_url ──────────────────────────────────────────


async def test_shared_folder_is_listed_in_one_call(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [
            (_share(LINK, "/children"), _Resp(CHILDREN)),
            (_share(LINK), _Resp(FOLDER)),
        ],
        url=LINK,
    )

    assert not result.is_error
    text = result.content
    assert text.startswith("Folder: Test Corporate (2 items)")
    assert f"Web URL: {FOLDER_WEB}" in text
    assert "[Folder] Contracts (1 item, 2048 bytes)" in text
    assert "[File] Q3 report.txt (11 bytes)" in text
    assert "Modified: 2026-10-02T10:00:00Z" in text
    # Every item is named by the link with its name appended, so it opens
    # through the share as the folder did, next to the page a person opens.
    assert f"URL: {_item_url('Contracts')}" in text
    assert f"URL: {_item_url('Q3%20report.txt')}" in text
    assert f"Web URL: {FOLDER_WEB}/Q3%20report.txt" in text
    assert "More items" not in text
    # The link is resolved, then its items read through the share.
    assert graph.urls() == [
        f"{GRAPH}/shares/{_encode_share_id(LINK)}/driveItem",
        f"{GRAPH}/shares/{_encode_share_id(LINK)}/driveItem/children",
    ]
    assert "folder" in graph.calls[0]["params"]["$select"].split(",")
    assert graph.calls[1]["params"]["$top"] == "50"


async def test_listed_file_is_read_by_its_url(monkeypatch):
    """The URL a listing gives a file is read by get_microsoft_file_by_url."""
    item_url = _item_url("Q3%20report.txt")
    graph, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        [
            (_share(item_url), _fail(404)),
            (_share(LINK), _Resp(FOLDER)),
            (_ends("/drives/DRIVE1/items/FOLDER1:/Q3%20report.txt"), _Resp(REPORT)),
            (_ends("/drives/DRIVE1/items/FILE1/content"), _Resp(text="hello world")),
        ],
        url=item_url,
    )

    assert not result.is_error
    assert "hello world" in result.content
    assert graph.urls() == [
        f"{GRAPH}/shares/{_encode_share_id(item_url)}/driveItem",
        f"{GRAPH}/shares/{_encode_share_id(LINK)}/driveItem",
        f"{GRAPH}/drives/DRIVE1/items/FOLDER1:/Q3%20report.txt",
        f"{GRAPH}/drives/DRIVE1/items/FILE1/content",
    ]
    # The lookups another reading may still resolve stay out of the warning log.
    assert graph.calls[0]["expected_statuses"] == {400, 403, 404}


async def test_listed_file_downloads_by_its_url(monkeypatch):
    item_url = _item_url("Q3%20report.txt")
    _, result = await _run(
        monkeypatch,
        "download_microsoft_file",
        [
            (_share(item_url), _fail(404)),
            (_share(LINK), _Resp(FOLDER)),
            (_ends("/drives/DRIVE1/items/FOLDER1:/Q3%20report.txt"), _Resp(REPORT)),
            (_ends("/drives/DRIVE1/items/FILE1/content"), _Resp(text="hello world")),
        ],
        url=item_url,
    )

    inside, outside = fenced_and_open(result.content)
    assert "Name: Q3 report.txt" in inside.splitlines()
    assert "Q3 report.txt" not in outside
    assert len(result.files) == 1


# ── Who last changed an item, before a write ───────────────────────────────

ALICE = {"user": {"displayName": "Alice Martin", "email": "alice@contoso.com"}}
BOB = {"user": {"displayName": "Bob Stone"}}


async def test_folder_listing_names_who_modified_and_created_each_item(monkeypatch):
    children = {
        "value": [
            {**SUBFOLDER, "lastModifiedDateTime": "2026-10-01T09:00:00Z"},
            {
                **REPORT,
                "lastModifiedDateTime": "2026-10-02T10:00:00Z",
                "lastModifiedBy": ALICE,
                "createdBy": BOB,
            },
        ]
    }
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(_share(LINK, "/children"), _Resp(children)), (_share(LINK), _Resp(FOLDER))],
        url=LINK,
    )

    assert not result.is_error
    text = result.content
    assert "  Modified by: Alice Martin <alice@contoso.com>" in text
    assert "  Created by: Bob Stone" in text
    # An item Graph returned no authorship for gets no empty line.
    assert text.count("Modified by:") == 1
    selected = graph.calls[1]["params"]["$select"].split(",")
    assert {"lastModifiedBy", "createdBy"} <= set(selected)


async def test_file_read_by_url_names_who_modified_it(monkeypatch):
    item_url = _item_url("Q3%20report.txt")
    graph, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        [
            (_share(item_url), _fail(404)),
            (_share(LINK), _Resp(FOLDER)),
            (
                _ends("/drives/DRIVE1/items/FOLDER1:/Q3%20report.txt"),
                _Resp({**REPORT, "lastModifiedBy": ALICE, "createdBy": BOB}),
            ),
            (_ends("/drives/DRIVE1/items/FILE1/content"), _Resp(text="hello world")),
        ],
        url=item_url,
    )

    assert not result.is_error
    assert "Modified by: Alice Martin <alice@contoso.com>" in result.content
    assert "Created by: Bob Stone" in result.content
    assert "lastModifiedBy" in graph.calls[0]["params"]["$select"].split(",")


@pytest.mark.parametrize(
    ("identity_set", "expected"),
    [
        (ALICE, "Alice Martin <alice@contoso.com>"),
        (BOB, "Bob Stone"),
        ({"user": {"email": "carol@contoso.com"}}, "carol@contoso.com"),
        ({"application": {"displayName": "Power Automate"}}, "Power Automate"),
        ({"user": {}}, ""),
        (None, ""),
        ("not a dict", ""),
    ],
)
def test_identity_label(identity_set, expected):
    from app.integrations.providers.microsoft._drive import _identity_label

    assert _identity_label(identity_set) == expected


@pytest.mark.parametrize(
    "subfolder_url",
    [
        _item_url("Contracts"),
        f"{LINK}/Contracts",
        f"{LINK_PATH}/Contracts",
    ],
    ids=["before-query", "after-query", "no-query"],
)
async def test_subfolder_is_listed_by_appending_its_name(monkeypatch, subfolder_url):
    contract = {
        "id": "FILE2",
        "name": "MSA.docx",
        "size": 300,
        "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"},
        "lastModifiedDateTime": "2026-09-30T08:00:00Z",
        "webUrl": f"{FOLDER_WEB}/Contracts/MSA.docx",
    }
    base_link = LINK if "?" in subfolder_url else LINK_PATH
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [
            (_share(subfolder_url), _fail(400)),
            (_share(base_link), _Resp(FOLDER)),
            (_ends("/drives/DRIVE1/items/FOLDER1:/Contracts"), _Resp(SUBFOLDER)),
            (_ends("/drives/DRIVE1/items/SUB1/children"), _Resp({"value": [contract]})),
        ],
        url=subfolder_url,
    )

    assert not result.is_error
    assert result.content.startswith("Folder: Contracts (1 item)")
    assert "[File] MSA.docx (300 bytes)" in result.content
    query = "?e=Xy12Z" if "?" in subfolder_url else ""
    assert f"URL: {LINK_PATH}/Contracts/MSA.docx{query}" in result.content
    # A folder reached by a path under the share is read by drive path.
    assert graph.urls()[-1] == f"{GRAPH}/drives/DRIVE1/items/SUB1/children"


async def test_missing_subfolder_names_the_shared_folder(monkeypatch):
    url = _item_url("Contrcts")
    _, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [
            (_share(url), _fail(404)),
            (_share(LINK), _Resp(FOLDER)),
            (_ends("/drives/DRIVE1/items/FOLDER1:/Contrcts"), _fail(404)),
        ],
        url=url,
    )

    assert "the shared folder 'Test Corporate' holds no 'Contrcts'" in result.content
    assert f'list_microsoft_folder_by_url(url="{LINK}")' in result.content


async def test_unresolvable_link_keeps_the_first_error(monkeypatch):
    url = _item_url("Contracts")
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(lambda u: "/shares/" in u, _fail(403))],
        url=url,
    )

    assert "returned 403" in result.content
    assert graph.urls()[0] == f"{GRAPH}/shares/{_encode_share_id(url)}/driveItem"
    assert all("/drives/" not in u for u in graph.urls())


async def test_pages_follow_the_next_link(monkeypatch):
    next_link = (
        f"{GRAPH}/drives/DRIVE1/items/FOLDER1/children"
        "?$top=1&$select=id,name&$skiptoken=UGFnZWQ9VFJVRSZwX0lEPTI%3D"
    )
    first = {"value": [CHILDREN["value"][0]], "@odata.nextLink": next_link}
    _, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(_share(LINK, "/children"), _Resp(first)), (_share(LINK), _Resp(FOLDER))],
        url=LINK,
        count=1,
    )
    token = "$skiptoken=UGFnZWQ9VFJVRSZwX0lEPTI%3D"
    assert f'More items: list_microsoft_folder_by_url(url="{LINK}", page_token="{token}")' in result.content

    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [
            (_share(LINK, "/children"), _Resp({"value": [CHILDREN["value"][1]]})),
            (_share(LINK), _Resp(FOLDER)),
        ],
        url=LINK,
        count=1,
        page_token=token,
    )
    assert "[File] Q3 report.txt" in result.content
    assert "More items" not in result.content
    params = graph.calls[-1]["params"]
    assert params["$skiptoken"] == "UGFnZWQ9VFJVRSZwX0lEPTI="
    assert params["$top"] == "1"


async def test_share_route_failure_falls_back_to_the_drive_path(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [
            (_share(LINK, "/children"), _fail(403)),
            (_share(LINK), _Resp(FOLDER)),
            (_ends("/drives/DRIVE1/items/FOLDER1/children"), _Resp(CHILDREN)),
        ],
        url=LINK,
    )

    assert not result.is_error
    assert "[Folder] Contracts" in result.content
    assert graph.calls[1]["expected_statuses"] == {400, 403, 404}
    assert graph.urls()[-1] == f"{GRAPH}/drives/DRIVE1/items/FOLDER1/children"


async def test_browser_url_items_carry_their_web_url(monkeypatch):
    """Under a folder's browser URL, an item's URL is its own web URL."""
    _, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(_share(FOLDER_WEB, "/children"), _Resp(CHILDREN)), (_share(FOLDER_WEB), _Resp(FOLDER))],
        url=FOLDER_WEB,
    )

    assert f"URL: {FOLDER_WEB}/Q3%20report.txt" in result.content
    assert f"Web URL: {FOLDER_WEB}/Q3%20report.txt" not in result.content


async def test_empty_folder(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(_share(LINK, "/children"), _Resp({"value": []})), (_share(LINK), _Resp({**FOLDER, "folder": {"childCount": 0}}))],
        url=LINK,
    )

    assert "Folder: Test Corporate (0 items)" in result.content
    assert "The folder is empty." in result.content


async def test_a_file_url_points_at_the_reader(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_folder_by_url",
        [(_share(LINK), _Resp(REPORT))],
        url=LINK,
    )

    assert "'Q3 report.txt' is a file, not a folder" in result.content
    assert f'get_microsoft_file_by_url(url="{LINK}")' in result.content
    assert len(graph.calls) == 1


async def test_rejects_a_non_url(monkeypatch):
    graph, result = await _run(monkeypatch, "list_microsoft_folder_by_url", [], url="Test Corporate")

    assert "full http(s) URL" in result.content
    assert graph.calls == []


# ── A folder given to the file tools ──────────────────────────────────────


async def test_file_by_url_lists_a_folder_and_downloads_nothing(monkeypatch):
    """Even a folder over the download limit is listed, never downloaded."""
    huge = {**FOLDER, "size": 5 * 1024 * 1024 * 1024}
    graph, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        [(_share(LINK, "/children"), _Resp(CHILDREN)), (_share(LINK), _Resp(huge))],
        url=LINK,
    )

    assert not result.is_error
    assert "'Test Corporate' is a folder, not a file, so nothing was downloaded" in result.content
    assert "[File] Q3 report.txt (11 bytes)" in result.content
    assert "list_microsoft_folder_by_url" in result.content
    assert "download_microsoft_file(url=" not in result.content
    assert all("/content" not in url for url in graph.urls())


async def test_file_by_url_points_at_the_next_page(monkeypatch):
    first = {"value": CHILDREN["value"], "@odata.nextLink": f"{GRAPH}/drives/DRIVE1/items/FOLDER1/children?$skiptoken=P2"}
    _, result = await _run(
        monkeypatch,
        "get_microsoft_file_by_url",
        [(_share(LINK, "/children"), _Resp(first)), (_share(LINK), _Resp(FOLDER))],
        url=LINK,
    )

    assert f'list_microsoft_folder_by_url(url="{LINK}", page_token="$skiptoken=P2")' in result.content


async def test_download_refuses_a_folder(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "download_microsoft_file",
        [(_share(LINK), _Resp(FOLDER))],
        url=LINK,
    )

    assert "'Test Corporate' is a folder, not a file" in result.content
    assert f'list_microsoft_folder_by_url(url="{LINK}")' in result.content
    assert not result.files
    assert len(graph.calls) == 1


async def test_onedrive_path_to_a_folder_points_at_the_listing(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "get_microsoft_file_content",
        [(_ends("/me/drive/root:/Projects"), _Resp(FOLDER))],
        item_path="Projects",
    )

    assert "'Test Corporate' is a folder, not a file" in result.content
    assert f'list_microsoft_folder_by_url(url="{FOLDER_WEB}")' in result.content
    assert "folder" in graph.calls[0]["params"]["$select"].split(",")
    assert len(graph.calls) == 1


# ── list_microsoft_site_files: the site forms ─────────────────────────────

_SITE_LISTING = {"value": [{"id": "X1", "name": "README.md", "size": 5, "webUrl": "u/r"}]}


@pytest.mark.parametrize(
    "site",
    [
        "contoso.sharepoint.com:/sites/test-site:",
        "contoso.sharepoint.com:/sites/test-site",
        "https://contoso.sharepoint.com/sites/test-site",
        "https://contoso.sharepoint.com/sites/test-site/Shared%20Documents/Forms/AllItems.aspx",
    ],
    ids=["path-form-colon", "path-form", "site-url", "library-url"],
)
async def test_site_path_form_and_url_resolve_to_the_site_id(monkeypatch, site):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [
            (_ends("/sites/contoso.sharepoint.com:/sites/test-site"), _Resp({"id": SITE_ID})),
            (_ends(f"/sites/{SITE_ID}/drive/root:/Reports:/children"), _Resp(_SITE_LISTING)),
        ],
        site_id=site,
        folder="Reports",
    )

    assert not result.is_error
    assert "Path: Reports/README.md" in result.content
    assert graph.urls() == [
        f"{GRAPH}/sites/contoso.sharepoint.com:/sites/test-site",
        f"{GRAPH}/sites/{SITE_ID}/drive/root:/Reports:/children",
    ]


async def test_site_id_form_is_used_as_given(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [(_ends(f"/sites/{SITE_ID}/drive/root/children"), _Resp(_SITE_LISTING))],
        site_id=SITE_ID,
    )

    assert "Path: README.md" in result.content
    assert graph.urls() == [f"{GRAPH}/sites/{SITE_ID}/drive/root/children"]


async def test_inaccessible_site_points_at_the_folder_listing(monkeypatch):
    graph, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [(_ends("/sites/contoso.sharepoint.com:/sites/test-site"), _fail(403))],
        site_id="contoso.sharepoint.com:/sites/test-site:",
    )

    assert "site not accessible, list the shared folder with list_microsoft_folder_by_url" in result.content
    assert "returned 403" in result.content
    assert len(graph.calls) == 1


@pytest.mark.parametrize(("status", "folder"), [(403, "root"), (403, "Reports"), (404, "root")])
async def test_unreadable_library_points_at_the_folder_listing(monkeypatch, status, folder):
    """A site the account only holds a shared folder on resolves, but its
    library does not list."""
    _, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [(lambda url: "/children" in url, _fail(status))],
        site_id=SITE_ID,
        folder=folder,
    )

    assert "site not accessible, list the shared folder with list_microsoft_folder_by_url" in result.content


async def test_missing_folder_in_a_readable_site_keeps_the_graph_error(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [(lambda url: "/children" in url, _fail(404))],
        site_id=SITE_ID,
        folder="Reportz",
    )

    assert result.content == "Error: Microsoft returned 404 while trying to list site files."


async def test_malformed_site_id_names_the_accepted_forms(monkeypatch):
    _, result = await _run(
        monkeypatch,
        "list_microsoft_site_files",
        [(lambda url: "/children" in url, _fail(400))],
        site_id="contoso.sharepoint.com,sites/test-site",
    )

    assert "returned 400" in result.content
    assert "hostname:/sites/name" in result.content


async def test_sharing_link_as_site_points_at_the_folder_listing(monkeypatch):
    graph, result = await _run(monkeypatch, "list_microsoft_site_files", [], site_id=LINK)

    assert f'list_microsoft_folder_by_url(url="{LINK}")' in result.content
    assert graph.calls == []


async def test_site_file_content_takes_the_path_form(monkeypatch):
    readme = {"id": "SITEITEM", "name": "readme.md", "size": 4, "file": {"mimeType": "text/markdown"}, "webUrl": "u"}
    graph, result = await _run(
        monkeypatch,
        "get_microsoft_site_file_content",
        [
            (_ends("/sites/contoso.sharepoint.com:/sites/test-site"), _Resp({"id": SITE_ID})),
            (_ends(f"/sites/{SITE_ID}/drive/root:/docs/readme.md"), _Resp(readme)),
            (_ends(f"/sites/{SITE_ID}/drive/items/SITEITEM/content"), _Resp(text="# Hi")),
        ],
        site_id="contoso.sharepoint.com:/sites/test-site:",
        item_path="docs/readme.md",
    )

    assert "# Hi" in result.content
    assert len(graph.calls) == 3


# ── Tool catalogue ────────────────────────────────────────────────────────


def test_folder_tool_is_offered_and_named_by_the_site_tools():
    tools = {t.name: t for t in build_tools(token="dummy", executor=CallableToolExecutor())}

    folder_tool = tools["list_microsoft_folder_by_url"]
    assert folder_tool.input_schema["required"] == ["url"]
    assert set(folder_tool.input_schema["properties"]) == {"url", "count", "page_token"}
    for name in ("get_microsoft_file_by_url", "list_microsoft_site_files", "list_microsoft_sharepoint_sites"):
        assert "list_microsoft_folder_by_url" in tools[name].description


# ── Parsing ───────────────────────────────────────────────────────────────


def test_sharing_link_splits():
    assert _sharing_link_splits(LINK) == []
    assert _sharing_link_splits(FOLDER_WEB) == []
    assert _sharing_link_splits(_item_url("Contracts", "MSA.docx")) == [
        (LINK, "Contracts/MSA.docx"),
        (_item_url("Contracts"), "MSA.docx"),
    ]
    assert _sharing_link_splits(f"{LINK}/Contracts")[0] == (LINK, "Contracts")
    # A personal OneDrive link has one more segment before its token.
    personal = f"{HOST}/:f:/g/personal/ana_contoso_com/EaBc"
    assert _sharing_link_splits(f"{personal}/Sub")[:2] == [
        (f"{HOST}/:f:/g/personal/ana_contoso_com", "EaBc/Sub"),
        (personal, "Sub"),
    ]


def test_page_token_never_carries_the_link():
    token = _next_page_token("https://evil.example/drives/x/children?$top=5&$select=id&$skiptoken=abc")
    assert token == "$skiptoken=abc"
    assert _page_params(token) == {"$skiptoken": "abc"}
    assert _page_params("abc") == {"$skiptoken": "abc"}
    assert _page_params("$top=999&$skiptoken=abc") == {"$skiptoken": "abc"}
    assert _page_params("") == {}
    assert _next_page_token(None) == ""
