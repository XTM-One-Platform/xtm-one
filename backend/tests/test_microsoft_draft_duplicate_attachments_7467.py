"""A draft whose file attachments an Outlook add-in attached again is sent with one copy of each.

Outlook removes the unreferenced inline images itself at send, but a file
attachment attached twice (same name, same size, same bytes) reached the
recipient twice. ``send_microsoft_draft`` removes the later copies before the
send and says so; ``get_microsoft_email`` names the duplicates.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.providers.microsoft import mail
from app.integrations.providers.microsoft._attachment_dedupe import duplicates_line, removed_note
from app.integrations.providers.microsoft._raw_markup import raw_markup_report
from app.tools.base import CallableToolExecutor, ToolCall

DRAFT = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVysDUP"
PDF = b"%PDF-1.7 test attachment round 2"
BODY = (
    '<html><body><p>Hi</p><div id="Signature">'
    + "".join(f'<img src="cid:sig-{i}@xtm-one">' for i in range(6))
    + "</div></body></html>"
)


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


def _att(att_id: str, name: str, data: bytes, *, inline: bool = False, at: str = "2026-11-02T14:00:00Z") -> dict:
    return {
        "id": att_id,
        "name": name,
        "size": len(data),
        "isInline": inline,
        "lastModifiedDateTime": at,
        "@odata.type": "#microsoft.graph.fileAttachment",
        "_bytes": data,
    }


@pytest.fixture
def graph(monkeypatch):
    """One draft in memory: its body, its attachments, the deletes and the send."""
    state = SimpleNamespace(calls=[], attachments=[], unreadable=set())
    base = f"/me/messages/{DRAFT}"

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url))
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if "/attachments/" in url:
            att_id = url.rsplit("/", 1)[-1]
            if method == "delete":
                state.attachments = [a for a in state.attachments if a["id"] != att_id]
                return _Ok({}), None
            if att_id in state.unreadable:
                return None, "Error: 500"
            found = next(a for a in state.attachments if a["id"] == att_id)
            return _Ok({"id": att_id, "contentBytes": base64.b64encode(found["_bytes"]).decode()}), None
        if url.endswith(base + "/attachments"):
            return _Ok({"value": [{k: v for k, v in a.items() if k != "_bytes"} for a in state.attachments]}), None
        if url.split("?", 1)[0].endswith(base) and method == "get":
            return _Ok({
                "id": DRAFT,
                "subject": "S",
                "toRecipients": [],
                "hasAttachments": bool(state.attachments),
                "body": {"contentType": "html", "content": BODY},
            }), None
        return _Ok({}), None

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    return state


async def _run(name: str, **arguments: Any) -> str:
    executor = CallableToolExecutor()
    # Default signature plan: the draft is read before the send (it is already signed, nothing is added).
    ms.build_tools(token="dummy", executor=executor)
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def _signature_images() -> list[dict]:
    return [_att(f"S{i}", f"sig-{i}.png", b"\x89PNG-%d" % i, inline=True) for i in range(6)]


async def test_send_removes_the_second_identical_pdf_and_says_so(graph):
    graph.attachments = [
        _att("P1", "test-attachment-round2.pdf", PDF, at="2026-11-02T14:00:00Z"),
        *_signature_images(),
        _att("P2", "test-attachment-round2.pdf", PDF, at="2026-11-02T14:05:00Z"),
    ]
    answer = await _run("send_microsoft_draft", draft_id=DRAFT, force_duplicate=True)
    assert "1 duplicate attachment removed (test-attachment-round2.pdf)." in answer
    assert [a["id"] for a in graph.attachments] == ["P1", "S0", "S1", "S2", "S3", "S4", "S5"]
    deletes = [u for m, u in graph.calls if m == "delete"]
    assert len(deletes) == 1 and deletes[0].endswith("/attachments/P2")
    # The send comes after the removal.
    methods = [m for m, u in graph.calls if u.endswith("/send") or m == "delete"]
    assert methods == ["delete", "post"]


async def test_send_keeps_files_that_differ_in_bytes_or_name_and_every_inline_image(graph):
    graph.attachments = [
        _att("P1", "memo.pdf", PDF),
        _att("P2", "memo.pdf", PDF[::-1]),
        _att("P3", "other.pdf", PDF),
        *_signature_images(),
        *[_att(f"I{i}", "image.png", b"\x89PNG", inline=True) for i in range(3)],
    ]
    answer = await _run("send_microsoft_draft", draft_id=DRAFT, force_duplicate=True)
    assert "duplicate attachment" not in answer
    assert len(graph.attachments) == 12
    assert not [c for c in graph.calls if c[0] == "delete"]


async def test_send_keeps_a_copy_whose_bytes_cannot_be_read(graph):
    graph.attachments = [_att("P1", "memo.pdf", PDF), _att("P2", "memo.pdf", PDF, at="2026-11-02T15:00:00Z")]
    graph.unreadable = {"P2"}
    answer = await _run("send_microsoft_draft", draft_id=DRAFT, force_duplicate=True)
    assert "duplicate attachment" not in answer
    assert len(graph.attachments) == 2
    assert any(u.endswith("/send") for _m, u in graph.calls)


async def test_send_without_attachments_reads_no_attachment(graph):
    await _run("send_microsoft_draft", draft_id=DRAFT, force_duplicate=True)
    assert not [u for _m, u in graph.calls if "/attachments" in u]


async def test_get_email_names_the_duplicate_files_without_include_raw(graph):
    graph.attachments = [_att("P1", "memo.pdf", PDF), _att("P2", "memo.pdf", PDF), *_signature_images()]
    answer = await _run("get_microsoft_email", message_id=DRAFT)
    assert f"Duplicate file attachments (same name and size): memo.pdf x2 ({len(PDF)} B each)" in answer
    assert "send_microsoft_draft removes the extra copies" in answer
    assert "Raw markup" not in answer


def test_the_raw_report_says_none_or_names_the_duplicates():
    files = [{"name": "a.pdf", "size": 3, "isInline": False}, {"name": "a.pdf", "size": 3, "isInline": False}]
    assert "Duplicate file attachments (same name and size): a.pdf x2 (3 B each)" in raw_markup_report("", files)
    assert "Duplicate file attachments: none" in raw_markup_report("", files[:1])


def test_inline_images_and_item_attachments_are_never_counted():
    listed = [
        {"name": "image.png", "size": 9, "isInline": True},
        {"name": "image.png", "size": 9, "isInline": True},
        {"name": "Fwd", "size": 9, "@odata.type": "#microsoft.graph.itemAttachment"},
        {"name": "Fwd", "size": 9, "@odata.type": "#microsoft.graph.itemAttachment"},
    ]
    assert duplicates_line(listed) == ""


def test_the_removed_note_counts_copies_and_names_each_file_once():
    assert removed_note([]) == ""
    assert removed_note(["a.pdf", "a.pdf", "b.pdf"]) == " 3 duplicate attachments removed (a.pdf, b.pdf)."
