"""A draft the person opens in Outlook, and the raw read that diagnoses one.

A draft created with the signature and opened in Outlook with a signature
add-in grew duplicate inline images while open. ``create_microsoft_draft_email``
can leave the signature out (the add-in inserts it once; the send tools still
sign it if an agent sends it) and says which case it is in its answer.
``get_microsoft_email(include_raw=True)`` reports the markup and attachment
details a diagnosis needs.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.file_delivery import FetchedFile
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._raw_markup import RAW_WINDOW_CHARS, raw_markup_report
from app.services.email_signatures import EmailSignature, SignaturePlan
from app.tools.base import CallableToolExecutor, ToolCall

SIGNED = SignaturePlan(integration_id=str(uuid.uuid4()), integration_name="Signatures")
LOGO = "https://storage.example.com/sig/effects_logo.png"
MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"


async def _signature_of(_address: str):
    html = f'<table><tr><td><img src="{LOGO}"></td><td>Sam</td></tr></table>'
    return EmailSignature(html=html, reply_html=html, name="Main")


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Graph in memory: the created draft's payload, and one message to read raw."""
    state = SimpleNamespace(calls=[], created=None, fetched=[], message={}, attachments=[])

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if url.endswith("/me/messages") and method == "post":
            state.created = kwargs["json"]
            return _Ok({"id": "DRAFT1"}), None
        if "/attachments/" in url:
            att_id = url.rsplit("/", 1)[-1]
            found = next(a for a in state.attachments if a["id"] == att_id)
            return _Ok({"id": att_id, "contentId": found.get("_cid", "")}), None
        if url.endswith("/attachments"):
            return _Ok({"value": [{k: v for k, v in a.items() if k != "_cid"} for a in state.attachments]}), None
        if f"/me/messages/{MESSAGE_ID}" in url:
            return _Ok(state.message), None
        return _Ok({}), None

    async def _fetch(url: str, **_kwargs):
        state.fetched.append(url)
        return FetchedFile(data=b"\x89PNG-" + url.encode(), content_type="image/png", filename="")

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _fetch)
    return state


async def _run(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def _outlook(plan: SignaturePlan = SIGNED):
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor, signature_fn=_signature_of, signature_plan=plan)
    return executor


async def test_a_signed_draft_says_an_outlook_add_in_may_sign_it_again(graph):
    answer = await _run(_outlook(), "create_microsoft_draft_email", to="bob@example.com", subject="S", body="Hello")
    assert '<div id="Signature">' in graph.created["body"]["content"]
    assert len(graph.created["attachments"]) == 1
    assert "include_signature=false" in answer and "add-in" in answer


async def test_a_draft_without_signature_carries_no_block_no_image_and_says_how_it_gets_signed(graph):
    answer = await _run(
        _outlook(), "create_microsoft_draft_email", to="bob@example.com", subject="S", body="Hello",
        include_signature=False,
    )
    content = graph.created["body"]["content"]
    assert "Hello" in content
    assert "Signature" not in content and "cid:" not in content
    assert "attachments" not in graph.created
    assert graph.fetched == []
    assert "carries no signature" in answer and "send_microsoft_draft" in answer
    assert "include_signature=false" not in answer


async def test_a_draft_without_signature_when_signatures_are_off_adds_no_note(graph):
    answer = await _run(
        _outlook(SignaturePlan(enabled=False)), "create_microsoft_draft_email", to="bob@example.com", subject="S",
        body="Hello", include_signature=False,
    )
    assert "carries no signature" not in answer and "add-in" not in answer


def test_the_draft_tool_declares_include_signature():
    tools = ms.build_tools(
        token="dummy", executor=CallableToolExecutor(), signature_fn=_signature_of, signature_plan=SIGNED
    )
    tool = next(t for t in tools if t.name == "create_microsoft_draft_email")
    assert tool.input_schema["properties"]["include_signature"]["type"] == "boolean"
    read = next(t for t in tools if t.name == "get_microsoft_email")
    assert {"include_raw", "raw_offset"} <= set(read.input_schema["properties"])


async def test_get_email_include_raw_reports_blocks_markers_cids_and_attachment_times(graph):
    graph.message = {
        "id": MESSAGE_ID,
        "subject": "S",
        "hasAttachments": True,
        "body": {
            "contentType": "html",
            "content": (
                '<html><body><p>Hi</p><div id="Signature" class="elementToProof">'
                '<img src="cid:sig-a@xtm-one"></div>'
                '<div id="x_Signature" data-lsi="1"><img src="cid:image001.png@01DB"></div></body></html>'
            ),
        },
    }
    graph.attachments = [
        {"id": "B", "name": "image001.png", "size": 9, "isInline": True,
         "lastModifiedDateTime": "2026-11-02T14:00:00Z", "_cid": "image001.png@01DB"},
        {"id": "A", "name": "sig-a.png", "size": 9, "isInline": True,
         "lastModifiedDateTime": "2026-11-02T13:36:15Z", "_cid": "<sig-a@xtm-one>"},
        {"id": "C", "name": "lsi-attach-effects_1.png", "size": 9, "isInline": True,
         "lastModifiedDateTime": "2026-11-02T14:00:01Z", "_cid": "lsi-1"},
        {"id": "P", "name": "memo.pdf", "size": 13, "isInline": False,
         "lastModifiedDateTime": "2026-11-02T13:36:16Z"},
    ]
    full = await _run(_outlook(), "get_microsoft_email", message_id=MESSAGE_ID, include_raw=True)
    answer = full[full.index("Raw markup (include_raw):"):]
    assert "Signature blocks: 2" in answer
    assert 'class="elementToProof"' in answer
    assert "lsi x1" in answer and "elementtoproof x1" in answer
    assert "inline attachments: 3, 1 not referenced by the body" in answer
    # Oldest first: the tool's image, the file, then the client's copies.
    order = [answer.index(n) for n in ("sig-a.png (", "memo.pdf (", "image001.png (", "lsi-attach-effects_1.png (")]
    assert order == sorted(order)
    assert "content id sig-a@xtm-one" in answer
    # contentId is a fileAttachment property: a $select on the base type fails the read (#8522).
    reads = [c for c in graph.calls if "/attachments/" in c[1]]
    assert reads and all("$select" not in (c[2].get("params") or {}) for c in reads)


async def test_get_email_without_include_raw_reads_no_attachment_details(graph):
    graph.message = {"id": MESSAGE_ID, "subject": "S", "body": {"content": "<p>Hi</p>"}}
    answer = await _run(_outlook(), "get_microsoft_email", message_id=MESSAGE_ID)
    assert "Raw markup" not in answer
    assert not [c for c in graph.calls if c[1].endswith("/attachments")]


def test_the_raw_window_is_bounded_and_says_where_the_next_starts():
    html = "x" * (RAW_WINDOW_CHARS + 10)
    report = raw_markup_report(html, [], 0)
    assert f"characters 0 to {RAW_WINDOW_CHARS} of {RAW_WINDOW_CHARS + 10}" in report
    assert f"raw_offset={RAW_WINDOW_CHARS}" in report
    tail = raw_markup_report(html, [], RAW_WINDOW_CHARS)
    assert "raw_offset=" not in tail


def test_attachments_that_cannot_be_listed_are_said():
    report = raw_markup_report("<p>Hi</p>", [], 0, "Error: 403")
    assert "could not be listed (Error: 403)" in report
