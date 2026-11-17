"""Live check of the Google Docs review tools on a scratch Doc (opt-in) - see ``README.md``.

The unit tests (``tests/test_google_doc_review.py``) pin what the tools send;
this runs them against the real Drive and Docs APIs, as the connected user, on
a Doc kept for the purpose: a sentence is appended, commented on, replied to,
resolved, listed, rehearsed, edited directly and as a suggestion, then the
comment is deleted, the suggestion rejected and the sentence removed.

Skipped unless ``LIVE_GOOGLE_DOCS=1`` and both ``GOOGLE_DOCS_LIVE_ACCESS_TOKEN``
(an OAuth access token granting the ``drive`` and ``documents`` scopes) and
``GOOGLE_DOCS_LIVE_DOC_ID`` (the scratch Doc, which that account can edit) are
set. ``tests/test_google_doc_review.py`` pins the gate.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Mapping

import httpx
import pytest

from app.integrations.providers import google
from app.tools.base import CallableToolExecutor, ToolCall
from tests.live.live_llm_support import register_secret

LIVE_FLAG = "LIVE_GOOGLE_DOCS"
TOKEN_ENV = "GOOGLE_DOCS_LIVE_ACCESS_TOKEN"
DOC_ENV = "GOOGLE_DOCS_LIVE_DOC_ID"
_DOCS = "https://docs.googleapis.com/v1/documents"
_DRIVE = "https://www.googleapis.com/drive/v3/files"


def skip_reason(environ: Mapping[str, str] | None = None) -> str | None:
    """Why the live Google Docs check is skipped, or ``None`` when it runs."""
    env = os.environ if environ is None else environ
    if env.get(LIVE_FLAG) != "1":
        return f"live Google Docs check: set {LIVE_FLAG}=1 to run it"
    missing = [name for name in (TOKEN_ENV, DOC_ENV) if not env.get(name, "").strip()]
    return f"live Google Docs check: {', '.join(missing)} not set" if missing else None


_REASON = skip_reason()
_TOKEN = os.environ.get(TOKEN_ENV, "").strip()
_DOC_ID = os.environ.get(DOC_ENV, "").strip()
register_secret(_TOKEN)

pytestmark = [pytest.mark.live_google_docs, pytest.mark.skipif(_REASON is not None, reason=_REASON or "")]


async def _tool(executor: CallableToolExecutor, name: str, **arguments) -> str:
    result = await executor.execute_tool(ToolCall(id="live", name=name, arguments=arguments))
    assert not result.is_error, f"{name}: {result.content}"
    return result.content


async def _raw(client: httpx.AsyncClient, method: str, url: str, **kwargs) -> dict:
    resp = await client.request(method, url, **kwargs)
    assert resp.is_success, f"{method} {url.split('?')[0]}: HTTP {resp.status_code}"
    return resp.json() if resp.content else {}


def _suggestion_ids(document: dict, marker: str) -> set[str]:
    """The suggestions on the runs carrying *marker*: this run's, nobody else's."""
    ids: set[str] = set()

    def walk(content: list) -> None:
        for element in content or []:
            for item in (element.get("paragraph") or {}).get("elements") or []:
                run = item.get("textRun") or {}
                if marker in (run.get("content") or ""):
                    ids.update(run.get("suggestedInsertionIds") or [])
                    ids.update(run.get("suggestedDeletionIds") or [])
            for row in (element.get("table") or {}).get("tableRows") or []:
                for cell in row.get("tableCells") or []:
                    walk(cell.get("content"))

    for tab in document.get("tabs") or []:
        walk(((tab.get("documentTab") or {}).get("body") or {}).get("content"))
    return ids


@pytest.mark.asyncio
async def test_review_a_scratch_doc_end_to_end():
    marker = uuid.uuid4().hex[:12]
    sentence = f"XTM One live check {marker} keeps this sentence once."
    edited = sentence.replace("keeps", "kept")
    suggested = edited.replace("once", "exactly once")
    executor = CallableToolExecutor()
    google.build_tools(_TOKEN, executor)
    comment_id = ""
    async with httpx.AsyncClient(headers={"Authorization": f"Bearer {_TOKEN}"}, timeout=30) as raw:
        await _raw(
            raw,
            "POST",
            f"{_DOCS}/{_DOC_ID}:batchUpdate",
            json={"requests": [{"insertText": {"text": "\n" + sentence, "endOfSegmentLocation": {}}}]},
        )
        try:
            read = await _tool(executor, "read_google_doc", file_id=_DOC_ID)
            assert sentence in read and "[a]" not in read.split("Comments")[0]

            added = await _tool(
                executor, "add_google_doc_comment", file_id=_DOC_ID, content="Live check: is this right?", quoted_text=sentence
            )
            comment_id = re.search(r"Comment ID: ([A-Za-z0-9_-]+)\.", added).group(1)

            await _tool(
                executor, "reply_google_doc_comment", file_id=_DOC_ID, comment_id=comment_id, content="quote for your approval pls"
            )
            await _tool(executor, "resolve_google_doc_comment", file_id=_DOC_ID, comment_id=comment_id, reply="Approved.")

            listed = await _tool(executor, "list_google_doc_comments", file_id=_DOC_ID, include_resolved=True)
            thread = listed[listed.index(f"Comment {comment_id} [resolved]") :]
            assert sentence in thread.split("\n\n")[0]
            assert "quote for your approval pls" in thread and "[resolved the thread]: Approved." in thread
            open_only = await _tool(executor, "list_google_doc_comments", file_id=_DOC_ID)
            assert comment_id not in open_only

            edit = [{"find": sentence, "replace": edited, "expected_count": 1}]
            rehearsal = await _tool(executor, "edit_google_doc", file_id=_DOC_ID, edits=edit, dry_run=True)
            assert "nothing was changed" in rehearsal and "1 match(es)" in rehearsal
            assert sentence in await _tool(executor, "read_google_doc", file_id=_DOC_ID, include_comments=False)

            direct = await _tool(executor, "edit_google_doc", file_id=_DOC_ID, edits=edit, mode="direct")
            assert direct.startswith("Replaced 1 occurrence(s)")
            after = await _tool(executor, "read_google_doc", file_id=_DOC_ID, include_comments=False)
            assert edited in after and sentence not in after

            proposal = await _tool(
                executor, "edit_google_doc", file_id=_DOC_ID, edits=[{"find": edited, "replace": suggested}]
            )
            assert proposal.startswith("Suggested 1 replacement(s)")
            pending = await _tool(executor, "read_google_doc", file_id=_DOC_ID, include_comments=False)
            assert "{++" in pending or "{--" in pending
        finally:
            if comment_id:
                await raw.delete(f"{_DRIVE}/{_DOC_ID}/comments/{comment_id}")
            document = await _raw(
                raw,
                "GET",
                f"{_DOCS}/{_DOC_ID}",
                params={"includeTabsContent": "true", "suggestionsViewMode": "SUGGESTIONS_INLINE"},
            )
            requests = [
                {"rejectSuggestion": {"suggestionId": sid}} for sid in sorted(_suggestion_ids(document, marker))
            ]
            requests += [
                {"replaceAllText": {"containsText": {"text": text, "matchCase": True}, "replaceText": ""}}
                for text in (sentence, edited)
            ]
            await _raw(raw, "POST", f"{_DOCS}/{_DOC_ID}:batchUpdate", json={"requests": requests})
