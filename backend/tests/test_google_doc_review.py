"""Google Docs review tools: threads, anchored comments, replies, suggestions, edits.

The tools run for real against fake Drive / Docs / tokeninfo APIs served
through ``httpx.MockTransport`` (documented response shapes, no network, no
token). What they pin:

* ``list_google_doc_comments`` reads every page and lists one item per thread;
* ``add_google_doc_comment`` posts a comment attached to a quote only when the
  quote occurs exactly once, on the range Google indexes it at, and refuses
  otherwise without posting anything;
* ``edit_google_doc`` counts every find before applying anything, refuses the
  whole call on a find that matches nothing or too often, and a dry run
  changes nothing;
* a scope the token lacks is named before the request that needs it, and a
  403 / 404 / 429 is a sentence naming the file;
* ``read_google_doc`` gives a Doc's clean text and its threads apart;
* the read / write split a read-only binding enforces.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.integrations.providers import google
from app.integrations.providers.google_docs import (
    DOCS_SCOPE,
    DRIVE_READONLY_SCOPE,
    DRIVE_SCOPE,
    doc_segments,
    parse_file_id,
)
from app.services.integration_read_only import integration_tool_access, withhold_write_tools
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

DOC_ID = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-xy"
DOC_MIME = "application/vnd.google-apps.document"
DRIVE = "www.googleapis.com"
DOCS = "docs.googleapis.com"
FILE_PATH = f"/drive/v3/files/{DOC_ID}"
COMMENTS_PATH = f"{FILE_PATH}/comments"
DOC_PATH = f"/v1/documents/{DOC_ID}"
BATCH_PATH = f"/v1/documents/{DOC_ID}:batchUpdate"
ALL_SCOPES = f"openid {DRIVE_SCOPE} {DOCS_SCOPE}"
TOKEN = "ya29.fake-access-token"

_META = {
    "id": DOC_ID,
    "name": "Q3 plan",
    "mimeType": DOC_MIME,
    "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit",
    "capabilities": {"canComment": True, "canEdit": True},
}


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


@pytest.fixture
def api(monkeypatch) -> FakeGoogle:
    fake = FakeGoogle()
    real_client = httpx.AsyncClient

    class _Client(real_client):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs["transport"] = httpx.MockTransport(fake)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    fake.on("POST", "oauth2.googleapis.com", "/tokeninfo", _json({"scope": ALL_SCOPES}))
    fake.on("GET", DRIVE, FILE_PATH, _json(_META))
    return fake


def _json(payload: Any, status: int = 200, headers: dict[str, str] | None = None):
    return lambda _request: httpx.Response(status, json=payload, headers=headers)


def _executor() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    google.build_tools(TOKEN, executor)
    return executor


async def _call(name: str, executor: CallableToolExecutor | None = None, **arguments: Any) -> ToolResult:
    return await (executor or _executor()).execute_tool(ToolCall(id="c1", name=name, arguments=arguments))


# ── A Docs API document ───────────────────────────────────────────────────


def _run(text: str, start: int, **extra: Any) -> dict:
    return {"startIndex": start, "endIndex": start + len(text.encode("utf-16-le")) // 2, "textRun": {"content": text, **extra}}


def _paragraph(*runs: dict, style: str = "NORMAL_TEXT", bullet: dict | None = None) -> dict:
    paragraph: dict[str, Any] = {"elements": list(runs), "paragraphStyle": {"namedStyleType": style}}
    if bullet is not None:
        paragraph["bullet"] = bullet
    return {"startIndex": runs[0]["startIndex"], "endIndex": runs[-1]["endIndex"], "paragraph": paragraph}


def _document(*paragraph_texts: str, tab_id: str = "t.0", revision: str | None = "rev-1") -> dict:
    """A one-tab Doc whose body holds one paragraph per text, laid out as Google indexes it."""
    index = 1
    content: list[dict] = [{"endIndex": 1, "sectionBreak": {}}]
    for text in paragraph_texts:
        run = _run(text + "\n", index)
        content.append(_paragraph(run))
        index = run["endIndex"]
    document: dict[str, Any] = {
        "documentId": DOC_ID,
        "tabs": [{"tabProperties": {"tabId": tab_id, "title": "Tab 1"}, "documentTab": {"body": {"content": content}}}],
    }
    if revision:
        document["revisionId"] = revision
    return document


# ── Arguments ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (DOC_ID, DOC_ID),
        (f"https://docs.google.com/document/d/{DOC_ID}/edit?tab=t.0", DOC_ID),
        (f"https://drive.google.com/open?id={DOC_ID}", DOC_ID),
        ("  ", None),
        ("../../files", None),
        ("a b", None),
    ],
)
def test_file_id_takes_an_id_or_a_docs_url(raw, expected):
    assert parse_file_id(raw) == expected


# ── list_google_doc_comments ──────────────────────────────────────────────


def _thread(cid: str, content: str, *, quote: str = "", resolved: bool = False, replies: list | None = None) -> dict:
    thread: dict[str, Any] = {
        "id": cid,
        "content": content,
        "resolved": resolved,
        "createdTime": "2026-10-01T09:12:33.000Z",
        "author": {"displayName": "Ann Lee", "emailAddress": "ann@example.com"},
        "replies": replies or [],
    }
    if quote:
        thread["quotedFileContent"] = {"mimeType": "text/html", "value": quote}
    return thread


@pytest.mark.asyncio
async def test_threads_are_read_across_every_page(api):
    pages = {
        "": {
            "comments": [
                _thread(
                    "AAAAc1",
                    "Is this figure right?",
                    quote="Revenue grew 40%",
                    replies=[
                        {
                            "id": "r1",
                            "content": "quote for your approval pls",
                            "createdTime": "2026-10-02T10:00:00.000Z",
                            "author": {"displayName": "Bob", "me": True},
                        },
                        {"id": "r2", "content": "gone", "deleted": True},
                    ],
                ),
                _thread("AAAAgone", "deleted", resolved=False) | {"deleted": True},
            ],
            "nextPageToken": "page-2",
        },
        "page-2": {"comments": [_thread("AAAAc2", "Typo fixed", resolved=True)]},
    }
    api.on("GET", DRIVE, COMMENTS_PATH, lambda r: httpx.Response(200, json=pages[r.url.params.get("pageToken", "")]))

    result = await _call("list_google_doc_comments", file_id=DOC_ID)

    assert not result.is_error
    assert [r.url.params.get("pageToken") for r in api.sent("GET", COMMENTS_PATH)] == [None, "page-2"]
    assert "replies(" in api.sent("GET", COMMENTS_PATH)[0].url.params["fields"]
    assert "Comment threads on 'Q3 plan' (1 open, 1 resolved" in result.content
    assert "1. Comment AAAAc1 [open]" in result.content
    assert "By Ann Lee <ann@example.com>, 2026-10-01 09:12 UTC" in result.content
    assert 'On: "Revenue grew 40%"' in result.content
    assert "- Bob (you), 2026-10-02 10:00 UTC: quote for your approval pls" in result.content
    assert "AAAAc2" not in result.content and "AAAAgone" not in result.content and "gone" not in result.content

    everything = await _call("list_google_doc_comments", file_id=DOC_ID, include_resolved=True)

    assert "2. Comment AAAAc2 [resolved]" in everything.content


@pytest.mark.asyncio
async def test_a_reply_that_resolved_the_thread_says_so(api):
    replies = [{"id": "r1", "action": "resolve", "content": "", "author": {"displayName": "Bob"}}]
    api.on("GET", DRIVE, COMMENTS_PATH, _json({"comments": [_thread("AAAAc1", "Fix", resolved=True, replies=replies)]}))

    result = await _call("list_google_doc_comments", file_id=DOC_ID, include_resolved="true")

    assert "   - Bob [resolved the thread]\n" in result.content


# ── add_google_doc_comment: the quote must occur exactly once ─────────────


@pytest.mark.asyncio
async def test_a_quote_found_once_is_commented_on_its_exact_range(api):
    # An emoji before the quote is two UTF-16 code units: the range is in Google's indexes.
    api.on("GET", DOCS, DOC_PATH, _json(_document("Intro \U0001f600 text.", "Revenue grew 40% in Q3.")))
    api.on("POST", DOCS, BATCH_PATH, _json({"replies": [{"insertComment": {"commentThread": {"commentId": "AAAAnew"}}}], "commentUpdateState": "ALL_SAVED"}))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="Source?", quoted_text="grew 40%")

    assert not result.is_error, result.content
    assert "Comment ID: AAAAnew" in result.content and 'attached to "grew 40%"' in result.content
    [body] = api.bodies("POST", BATCH_PATH)
    # "Intro 😀 text.\n" spans indexes 1..16; "Revenue " starts the next paragraph at 16.
    assert body == {
        "requests": [
            {"insertComment": {"content": "Source?", "range": {"startIndex": 24, "endIndex": 32, "tabId": "t.0"}}}
        ]
    }
    assert api.sent("GET", DOC_PATH)[0].url.params["suggestionsViewMode"] == "SUGGESTIONS_INLINE"
    assert not api.sent("POST", COMMENTS_PATH)


def test_segment_indexes_count_utf16_code_units():
    [segment] = doc_segments(_document("a\U0001f600b", "cd"), body_only=True)
    start = segment.text.index("b")
    assert (segment.start_index(start), segment.end_index(start + 1)) == (4, 5)
    start = segment.text.index("cd")
    assert (segment.start_index(start), segment.end_index(start + 2)) == (6, 8)


@pytest.mark.asyncio
async def test_a_quote_that_does_not_occur_is_refused_with_the_docs_exact_text(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("The vendor\u2019s \u201cbest\u201d offer.")))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="Why?", quoted_text='vendor\'s "best"')

    assert result.is_error
    assert "nothing was posted" in result.content
    assert '"vendor\u2019s \u201cbest\u201d"' in result.content
    assert not api.sent("POST", BATCH_PATH) and not api.sent("POST", COMMENTS_PATH)


@pytest.mark.asyncio
async def test_a_quote_that_occurs_twice_is_refused_with_both_contexts(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Risk is low.", "Cost is low.")))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="Sure?", quoted_text="is low")

    assert result.is_error
    assert "occurs 2 times" in result.content and "nothing was posted" in result.content
    assert "...Risk >>is low<<." in result.content and "Cost >>is low<<." in result.content
    assert not api.sent("POST", BATCH_PATH)


@pytest.mark.asyncio
async def test_without_a_quote_the_comment_is_on_the_file(api):
    api.on("POST", DRIVE, COMMENTS_PATH, _json({"id": "AAAAfile"}))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="  Overall: good.  ")

    assert "Comment ID: AAAAfile" in result.content and "on the file as a whole" in result.content
    assert api.bodies("POST", COMMENTS_PATH) == [{"content": "Overall: good."}]
    assert api.sent("POST", COMMENTS_PATH)[0].url.params["fields"]
    assert not api.sent("GET", DOC_PATH)


@pytest.mark.asyncio
async def test_a_quote_on_a_file_that_is_not_a_doc_is_refused(api):
    api.on("GET", DRIVE, FILE_PATH, _json(_META | {"mimeType": "application/vnd.google-apps.spreadsheet"}))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="x", quoted_text="A1")

    assert result.is_error and "without quoted_text" in result.content
    assert len(api.requests) == 1


@pytest.mark.asyncio
async def test_a_failed_save_is_not_reported_as_a_comment(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Only once here.")))
    api.on("POST", DOCS, BATCH_PATH, _json({"replies": [{}], "commentUpdateState": "ALL_FAILED_UNKNOWN_REASON"}))

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="x", quoted_text="once")

    assert result.is_error and "did not save the comment" in result.content


# ── reply / resolve ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reply_and_resolve_post_drive_replies(api):
    replies = f"{COMMENTS_PATH}/AAAAc1/replies"
    api.on("POST", DRIVE, replies, _json({"id": "rNew"}))

    replied = await _call("reply_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1", content="Approved.")
    resolved = await _call("resolve_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1", reply="Done, thanks.")
    bare = await _call("resolve_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1")
    reopened = await _call("reply_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1", action="reopen")

    assert api.bodies("POST", replies) == [
        {"content": "Approved."},
        {"content": "Done, thanks.", "action": "resolve"},
        {"action": "resolve"},
        {"action": "reopen"},
    ]
    assert "Replied to comment AAAAc1 on 'Q3 plan'. Reply ID: rNew" in replied.content
    assert "and resolved the thread" in resolved.content
    assert bare.content.startswith("Resolved comment AAAAc1 on 'Q3 plan'.")
    assert reopened.content.startswith("Reopened comment AAAAc1")
    assert all(r.url.params["fields"] for r in api.sent("POST", replies))


@pytest.mark.asyncio
async def test_a_reply_needs_content_or_an_action(api):
    empty = await _call("reply_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1")
    bad = await _call("reply_google_doc_comment", file_id=DOC_ID, comment_id="AAAAc1", content="x", action="close")

    assert empty.is_error and bad.is_error
    assert api.requests == []


@pytest.mark.asyncio
async def test_an_unknown_comment_is_named(api):
    api.on("POST", DRIVE, f"{COMMENTS_PATH}/AAAAzz/replies", _json({"error": {"code": 404, "message": "Comment not found"}}, 404))

    result = await _call("resolve_google_doc_comment", file_id=DOC_ID, comment_id="AAAAzz")

    assert result.is_error and "'Q3 plan' has no comment AAAAzz" in result.content


# ── edit_google_doc: count first, refuse the whole call ───────────────────


@pytest.mark.asyncio
async def test_an_edit_whose_find_matches_nothing_refuses_the_whole_call(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Budget: 4.2M.", "Owner: Ann.")))

    result = await _call(
        "edit_google_doc",
        file_id=DOC_ID,
        edits=[{"find": "4.2M", "replace": "4.5M"}, {"find": "Owner: Bob", "replace": "Owner: Carol"}],
    )

    assert result.is_error and "nothing was changed" in result.content
    assert "REFUSED: no match in the Doc" in result.content
    assert not api.sent("POST", BATCH_PATH)


@pytest.mark.asyncio
async def test_an_edit_matching_more_than_its_expected_count_is_refused(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Q3 and Q3.", "Q3 again.")))

    result = await _call("edit_google_doc", file_id=DOC_ID, edits=[{"find": "Q3", "replace": "Q4", "expected_count": 2}])

    assert result.is_error and "REFUSED: 3 matches, more than expected_count=2" in result.content
    assert not api.sent("POST", BATCH_PATH)


@pytest.mark.asyncio
async def test_a_dry_run_shows_each_match_in_context_and_changes_nothing(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Ship in Q3.", "Review q3 numbers.")))

    result = await _call(
        "edit_google_doc",
        file_id=DOC_ID,
        edits=json.dumps([{"find": "q3", "replace": "Q4", "match_case": False}]),
        dry_run=True,
    )

    assert not result.is_error
    assert result.content.startswith("Dry run on 'Q3 plan': nothing was changed.")
    assert '1. "q3" -> "Q4" (any case)' in result.content and "2 match(es)" in result.content
    assert "body: ...Ship in >>Q3<<. Review q3 numbers. ..." in result.content
    assert "dry_run=false to apply them as suggestions" in result.content
    assert not api.sent("POST", BATCH_PATH)


@pytest.mark.asyncio
async def test_edits_are_counted_in_order_and_written_as_suggestions_on_the_counted_revision(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Budget: 4.2M.")))
    api.on("POST", DOCS, BATCH_PATH, _json({"replies": [{"replaceAllText": {"occurrencesChanged": 1}}, {"replaceAllText": {"occurrencesChanged": 1}}]}))

    # The second find only exists once the first edit is applied.
    result = await _call(
        "edit_google_doc",
        file_id=DOC_ID,
        edits=[{"find": "4.2M", "replace": "4.5M"}, {"find": "4.5M.", "replace": "4.5M (revised)."}],
    )

    assert not result.is_error, result.content
    assert result.content.startswith("Suggested 2 replacement(s) in 'Q3 plan'")
    [body] = api.bodies("POST", BATCH_PATH)
    assert body["writeControl"] == {"writeMode": "SUGGEST", "requiredRevisionId": "rev-1"}
    assert body["requests"][1] == {
        "replaceAllText": {"containsText": {"text": "4.5M.", "matchCase": True}, "replaceText": "4.5M (revised)."}
    }


@pytest.mark.asyncio
async def test_a_direct_edit_writes_edits(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Draft", revision=None)))
    api.on("POST", DOCS, BATCH_PATH, _json({"replies": [{"replaceAllText": {"occurrencesChanged": 1}}]}))

    result = await _call("edit_google_doc", file_id=DOC_ID, edits=[{"find": "Draft", "replace": "Final"}], mode="direct")

    assert result.content.startswith("Replaced 1 occurrence(s) in 'Q3 plan' directly.")
    assert api.bodies("POST", BATCH_PATH)[0]["writeControl"] == {"writeMode": "EDIT"}


@pytest.mark.asyncio
async def test_an_edit_refused_by_a_newer_revision_says_the_doc_changed(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Draft")))
    api.on(
        "POST",
        DOCS,
        BATCH_PATH,
        _json({"error": {"code": 400, "message": "The required revision ID 'rev-1' does not match the latest revision.", "status": "INVALID_ARGUMENT"}}, 400),
    )

    result = await _call("edit_google_doc", file_id=DOC_ID, edits=[{"find": "Draft", "replace": "Final"}])

    assert result.is_error and "changed since it was read, so nothing was applied" in result.content


@pytest.mark.asyncio
async def test_a_direct_edit_without_edit_access_is_refused_before_reading(api):
    api.on("GET", DRIVE, FILE_PATH, _json(_META | {"capabilities": {"canComment": True, "canEdit": False}}))

    result = await _call("edit_google_doc", file_id=DOC_ID, edits=[{"find": "a", "replace": "b"}], mode="direct")

    assert result.is_error and "cannot edit 'Q3 plan'" in result.content and "mode='suggest'" in result.content
    assert not api.sent("GET", DOC_PATH)


@pytest.mark.parametrize(
    ("edits", "message"),
    [
        (None, "edits is required"),
        ("not json", "must be a list"),
        ([{"replace": "x"}], "edit 1 has no find text"),
        ([{"find": "x", "replace": "x"}], "with itself"),
        ([{"find": "x", "replace": "y", "expected_count": 0}], "expected_count must be"),
    ],
)
@pytest.mark.asyncio
async def test_malformed_edits_are_refused_before_any_call(api, edits, message):
    result = await _call("edit_google_doc", file_id=DOC_ID, edits=edits)

    assert result.is_error and message in result.content
    assert api.requests == []


# ── Scopes: named before the request that needs them ──────────────────────


@pytest.mark.parametrize(
    ("tool", "arguments", "granted", "named"),
    [
        ("add_google_doc_comment", {"content": "x"}, f"{DRIVE_READONLY_SCOPE} {DOCS_SCOPE}", DRIVE_SCOPE),
        ("reply_google_doc_comment", {"comment_id": "AAAAc1", "content": "x"}, DRIVE_READONLY_SCOPE, DRIVE_SCOPE),
        ("add_google_doc_comment", {"content": "x", "quoted_text": "y"}, DRIVE_READONLY_SCOPE, DOCS_SCOPE),
        ("edit_google_doc", {"edits": [{"find": "a", "replace": "b"}]}, DRIVE_READONLY_SCOPE, DOCS_SCOPE),
        ("list_google_doc_comments", {}, "openid", DRIVE_SCOPE),
    ],
)
@pytest.mark.asyncio
async def test_a_missing_scope_is_named_and_nothing_is_written(api, tool, arguments, granted, named):
    api.on("POST", "oauth2.googleapis.com", "/tokeninfo", _json({"scope": f"openid email {granted}"}))

    result = await _call(tool, file_id=DOC_ID, **arguments)

    assert result.is_error
    assert f"lacks the OAuth scope {named}" in result.content
    assert "Re-authorize with Google" in result.content and "'Q3 plan'" in result.content
    assert [r.url.host for r in api.requests if r.method == "POST"] == ["oauth2.googleapis.com"]
    assert not api.sent("GET", DOC_PATH) and not api.sent("GET", COMMENTS_PATH)
    tokeninfo = api.sent("POST", "/tokeninfo")[0]
    assert tokeninfo.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(tokeninfo.url) and TOKEN.encode() not in tokeninfo.content


@pytest.mark.asyncio
async def test_the_token_scopes_are_read_once_per_token(api):
    api.on("POST", DRIVE, f"{COMMENTS_PATH}/AAAAc1/replies", _json({"id": "r"}))
    executor = _executor()

    for _ in range(3):
        await _call("resolve_google_doc_comment", executor, file_id=DOC_ID, comment_id="AAAAc1")

    assert len(api.sent("POST", "/tokeninfo")) == 1


@pytest.mark.asyncio
async def test_unreadable_token_scopes_let_the_call_run_and_its_403_name_the_scope(api):
    api.on("POST", "oauth2.googleapis.com", "/tokeninfo", _json({"error": "invalid_token"}, 400))
    api.on(
        "POST",
        DRIVE,
        COMMENTS_PATH,
        _json(
            {"error": {"code": 403, "message": "Request had insufficient authentication scopes.", "errors": [{"reason": "insufficientPermissions"}]}},
            403,
            {"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
        ),
    )

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="x")

    assert result.is_error and f"lacks the OAuth scope {DRIVE_SCOPE}" in result.content


# ── Errors: a sentence naming the file ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_file_that_is_not_found_or_not_shared(api):
    api.on("GET", DRIVE, FILE_PATH, _json({"error": {"code": 404, "message": f"File not found: {DOC_ID}."}}, 404))

    result = await _call("list_google_doc_comments", file_id=DOC_ID)

    assert result.is_error
    assert f"cannot find file {DOC_ID}" in result.content and "not shared with the connected Google account" in result.content


@pytest.mark.asyncio
async def test_no_comment_permission_on_the_file(api):
    api.on(
        "POST",
        DRIVE,
        COMMENTS_PATH,
        _json({"error": {"code": 403, "message": "The user does not have sufficient permissions for this file.", "errors": [{"reason": "insufficientFilePermissions"}]}}, 403),
    )

    result = await _call("add_google_doc_comment", file_id=DOC_ID, content="x")

    assert result.is_error
    assert "not allowed to comment on 'Q3 plan'" in result.content and "as a commenter" in result.content


@pytest.mark.asyncio
async def test_the_docs_api_disabled_in_the_cloud_project(api):
    api.on(
        "GET",
        DOCS,
        DOC_PATH,
        _json({"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "Google Docs API has not been used in project 1 before or it is disabled.", "details": [{"reason": "SERVICE_DISABLED"}]}}, 403),
    )

    result = await _call("edit_google_doc", file_id=DOC_ID, edits=[{"find": "a", "replace": "b"}], dry_run=True)

    assert result.is_error and "the Google Docs API is not enabled" in result.content and "'Q3 plan'" in result.content


@pytest.mark.asyncio
async def test_rate_limiting_is_retried_then_explained(api, monkeypatch):
    from app.integrations import http as integration_http

    monkeypatch.setattr(integration_http, "_RATE_LIMIT_MAX_RETRIES", 1)
    monkeypatch.setattr(integration_http.asyncio, "sleep", _no_sleep)
    api.on("GET", DRIVE, COMMENTS_PATH, _json({"error": {"code": 429, "message": "Quota exceeded", "status": "RESOURCE_EXHAUSTED"}}, 429))

    result = await _call("list_google_doc_comments", file_id=DOC_ID)

    assert len(api.sent("GET", COMMENTS_PATH)) == 2
    assert result.is_error and "throttling" in result.content and "'Q3 plan'" in result.content


async def _no_sleep(_seconds: float) -> None:
    return None


# ── read_google_doc: clean text, comments apart ───────────────────────────


def _rich_document() -> dict:
    body = [
        {"endIndex": 1, "sectionBreak": {}},
        _paragraph(_run("Plan\n", 1), style="HEADING_1"),
        _paragraph(_run("Ship it", 6), _run(" now", 13, suggestedInsertionIds=["s1"]), _run(".\n", 17)),
        _paragraph(_run("First\n", 19), bullet={"listId": "l1", "nestingLevel": 0}),
        _paragraph(_run("Second\n", 25), bullet={"listId": "l1", "nestingLevel": 0}),
        {
            "startIndex": 32,
            "endIndex": 40,
            "table": {
                "tableRows": [
                    {"tableCells": [{"content": [_paragraph(_run("A\n", 34))]}, {"content": [_paragraph(_run("B\n", 37))]}]}
                ]
            },
        },
    ]
    lists = {"l1": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}}
    return {"tabs": [{"tabProperties": {"tabId": "t.0"}, "documentTab": {"body": {"content": body}, "lists": lists}}]}


@pytest.mark.asyncio
async def test_a_doc_is_read_as_clean_text_with_its_threads_apart(api):
    api.on("GET", DOCS, DOC_PATH, _json(_rich_document()))
    api.on("GET", DRIVE, COMMENTS_PATH, _json({"comments": [_thread("AAAAc1", "Too vague", quote="Ship it")]}))

    result = await _call("read_google_doc", file_id=DOC_ID)

    assert not result.is_error
    content = result.content
    assert "Content:\n(Pending suggestions are shown inline" in content
    assert "# Plan\nShip it{++ now++}.\n1. First\n2. Second\n\n| A | B |" in content
    assert "[a]" not in content
    assert "Comments (1 open, 0 resolved):" in content and "1. Comment AAAAc1 [open]" in content
    assert content.index("| A | B |") < content.index("Comments (")
    assert f"Web URL: {_META['webViewLink']}" in content
    assert not api.sent("GET", f"{FILE_PATH}/export")


@pytest.mark.asyncio
async def test_include_comments_false_reads_no_threads(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Hello")))

    result = await _call("read_google_doc", file_id=DOC_ID, include_comments=False)

    assert "Content:\nHello" in result.content and "Comments" not in result.content
    assert not api.sent("GET", COMMENTS_PATH)


@pytest.mark.asyncio
async def test_without_the_docs_api_a_doc_falls_back_on_drives_export(api):
    disabled = {"error": {"code": 403, "message": "disabled", "details": [{"reason": "SERVICE_DISABLED"}]}}
    api.on("GET", DOCS, DOC_PATH, _json(disabled, 403))
    api.on("GET", DRIVE, f"{FILE_PATH}/export", lambda _r: httpx.Response(200, text="Roadmap[a]\n\n[a]Too vague"))
    api.on("GET", DRIVE, COMMENTS_PATH, _json({"comments": []}))
    executor = _executor()

    first = await _call("read_google_doc", executor, file_id=DOC_ID)
    await _call("read_google_doc", executor, file_id=DOC_ID)

    assert not first.is_error
    assert "This is Drive's plain-text export, where [a], [b], ... mark comments" in first.content
    assert "The Google Docs API is not enabled" in first.content
    assert "Roadmap[a]" in first.content and "Comments: none." in first.content
    assert len(api.sent("GET", DOC_PATH)) == 1


@pytest.mark.asyncio
async def test_unreadable_comments_do_not_fail_the_read(api):
    api.on("GET", DOCS, DOC_PATH, _json(_document("Hello")))
    api.on("GET", DRIVE, COMMENTS_PATH, _json({"error": {"code": 403, "message": "no", "errors": [{"reason": "insufficientFilePermissions"}]}}, 403))

    result = await _call("read_google_doc", file_id=DOC_ID)

    assert not result.is_error
    assert "Content:\nHello" in result.content
    assert (
        "Comments: not available. The connected Google account is not allowed to list the "
        "comments of 'Q3 plan'"
    ) in result.content


# ── Read / write, scopes in the catalog ───────────────────────────────────

_READS = {"list_google_doc_comments", "read_google_doc"}
_WRITES = {"add_google_doc_comment", "reply_google_doc_comment", "resolve_google_doc_comment", "edit_google_doc"}


def test_the_comment_tools_are_classified_read_or_write():
    assert {name: integration_tool_access(name) for name in _READS | _WRITES} == {
        **dict.fromkeys(_READS, "read"),
        **dict.fromkeys(_WRITES, "write"),
    }


@pytest.mark.asyncio
async def test_list_integration_tools_reports_the_doc_tools_access():
    from app.services.builtin_tools import build_agent_builder_tool

    _definitions, executor = build_agent_builder_tool(None, "00000000-0000-0000-0000-000000000001")

    result = await executor.execute_tool(
        ToolCall(id="c", name="list_integration_tools", arguments={"provider": "google"})
    )

    for name in _READS:
        assert f"  - {name} [read]" in result.content
    for name in _WRITES:
        assert f"  - {name} [write]" in result.content


def test_a_read_only_binding_keeps_only_the_reading_doc_tools():
    executor = CallableToolExecutor()
    definitions = google.build_tools("tok", executor)

    kept = {d.name for d in withhold_write_tools(definitions, executor, integration_label="Google")}

    assert kept >= _READS
    assert not _WRITES & kept


def test_the_live_scratch_doc_check_needs_its_flag_and_both_variables():
    from tests.live.test_live_google_docs import DOC_ENV, LIVE_FLAG, TOKEN_ENV, skip_reason

    complete = {LIVE_FLAG: "1", TOKEN_ENV: "ya29.x", DOC_ENV: DOC_ID}
    assert skip_reason(complete) is None
    for name in complete:
        assert skip_reason({**complete, name: ""}) is not None
    assert skip_reason({**complete, LIVE_FLAG: "true"}) is not None


def test_no_workflow_turns_the_live_scratch_doc_check_on():
    from pathlib import Path

    from tests.live.test_live_google_docs import LIVE_FLAG

    workflows = Path(__file__).resolve().parents[2] / ".github" / "workflows"
    if not workflows.is_dir():
        pytest.skip("no .github/workflows next to the backend")
    assert [p.name for p in workflows.glob("*.y*ml") if LIVE_FLAG in p.read_text(encoding="utf-8")] == []


def test_the_google_integration_requests_drive_and_documents():
    from app.api.v1.integration_catalog import PROVIDER_CATALOG

    requested = set(PROVIDER_CATALOG["google"]["scopes"].split())
    assert {DRIVE_SCOPE, DOCS_SCOPE} <= requested
    assert "Google Docs API" in PROVIDER_CATALOG["google"]["setup_guide"]["apis_to_enable"]
    assert {DRIVE_SCOPE, DOCS_SCOPE} <= set(google.GoogleIntegration().get_oauth_defaults().scopes)
