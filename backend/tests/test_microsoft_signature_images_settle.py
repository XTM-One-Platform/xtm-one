"""A signed Outlook message is sent only once Exchange holds its signature images.

The body PATCH and the inline-image POSTs answer before Exchange has committed
them on the item: a send issued at once went out with the signature's logo and
icons missing. The send paths read the draft back until every ``cid:`` of its
own signature resolves to an inline attachment; when it never does, the
signature goes out with its images as links, never with references nothing
resolves, and the tool's answer says so.
"""

from __future__ import annotations

import uuid
from email.message import EmailMessage
from types import SimpleNamespace
from typing import Any

import pytest

import app.integrations.providers.microsoft as ms
from app.integrations.file_delivery import FetchedFile
from app.integrations.providers.microsoft import _signature_images as images
from app.integrations.providers.microsoft import mail
from app.services.email_signatures import EmailSignature, SignaturePlan
from app.tools.base import CallableToolExecutor, ToolCall

SIGNED = SignaturePlan(integration_id=str(uuid.uuid4()), integration_name="Signatures")
LOGO = "https://storage.example.com/sig/effects_logo.png"
ICON = "https://storage.example.com/sig/effects_icon.png"
MESSAGE_ID = "AAMkAGI2NGVhZTVlLTI1OGMtNDI4My1iZmVmLTI1ZmM5OWM5MTIwNgBGAAAAAADUuTJK1K9aTpCdqXop_4NaBwCd9nJ-tVys"
DRAFT = f"{MESSAGE_ID}DRAFT1"
REPLY_BODY = (
    '<html><body><div>Thanks</div><div id="appendonsend"></div><hr style="display:inline-block">'
    '<div id="divRplyFwdMsg">From: Alice</div><p>Earlier</p></body></html>'
)


async def _signature_of(_address: str) -> EmailSignature:
    html = f'<table><tr><td><img src="{LOGO}"><img src="{ICON}"></td><td>Sam</td></tr></table>'
    return EmailSignature(html=html, reply_html=html, name="Main")


class _Ok:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self._payload = payload or {}
        self.status_code = 200
        self.content = b""

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Graph in memory whose attachment listing lags: ``hidden`` listings answer none of them."""
    state = SimpleNamespace(calls=[], drafts={}, attachments={}, hidden=0)

    async def _fake(
        _client: Any, method: str, url: str, _provider: str, action: str, **kwargs: Any
    ) -> tuple[_Ok, None]:
        state.calls.append((method, url, action))
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if url.endswith("/createReply") or url.endswith("/createReplyAll"):
            state.drafts[DRAFT] = {"contentType": "html", "content": REPLY_BODY}
            return _Ok({"id": DRAFT, "body": state.drafts[DRAFT], "toRecipients": []}), None
        base = f"/me/messages/{DRAFT}"
        listed = state.attachments.setdefault(DRAFT, [])
        if url.endswith(base + "/attachments"):
            if method == "get":
                if state.hidden > 0:
                    state.hidden -= 1
                    return _Ok({"value": []}), None
                return _Ok(
                    {"value": [{"id": str(i), "name": a["name"], "isInline": True} for i, a in enumerate(listed)]}
                ), None
            listed.append(kwargs["json"])
            return _Ok({"id": str(len(listed) - 1)}), None
        if "/attachments/" in url:
            index = int(url.rsplit("/", 1)[-1])
            if method == "delete":
                listed[index] = {"name": "deleted", "contentId": ""}
                return _Ok({}), None
            return _Ok({"contentId": listed[index].get("contentId", ""), "isInline": True}), None
        if url.endswith(base) and method == "patch":
            if "body" in kwargs["json"]:
                state.drafts[DRAFT] = kwargs["json"]["body"]
            return _Ok({}), None
        if url.endswith(base) and method == "get":
            return _Ok({"subject": "S", "toRecipients": [], "body": state.drafts[DRAFT]}), None
        return _Ok({}), None

    async def _fetch(url: str, **_kwargs: Any) -> FetchedFile:
        return FetchedFile(data=b"\x89PNG-" + url.encode(), content_type="image/png", filename="")

    async def _quiet(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    monkeypatch.setattr(images, "SETTLE_DELAYS", (0, 0, 0))
    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _fetch)
    return state


def _outlook() -> CallableToolExecutor:
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor, signature_fn=_signature_of, signature_plan=SIGNED)
    return executor


async def _run(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    return (await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))).content


def _send_index(state: SimpleNamespace) -> int:
    return next(i for i, (m, u, _a) in enumerate(state.calls) if m == "post" and u.endswith("/send"))


async def test_a_draft_is_sent_only_after_exchange_lists_its_signature_images(graph: SimpleNamespace) -> None:
    executor = _outlook()
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    created = graph.drafts[DRAFT]["content"]
    graph.calls.clear()
    graph.hidden = 2  # Exchange answers the attachments late: two listings miss them.
    answer = await _run(executor, "send_microsoft_draft", draft_id=DRAFT)
    assert answer == f"Draft {DRAFT} has been sent."
    send = _send_index(graph)
    listings = [i for i, (m, u, _a) in enumerate(graph.calls) if m == "get" and u.endswith("/attachments")]
    # Read back three times, the last one finding the images, before the send.
    assert len([i for i in listings if i < send]) == 3
    assert graph.hidden == 0
    # Nothing rewritten, nothing attached again: one image set.
    assert graph.drafts[DRAFT]["content"] == created
    assert not [c for c in graph.calls if c[0] == "patch"]
    assert len(graph.attachments[DRAFT]) == 2


async def test_a_signature_whose_images_never_land_goes_out_with_links_and_says_so(graph: SimpleNamespace) -> None:
    executor = _outlook()
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    graph.calls.clear()
    graph.hidden = 1000
    answer = await _run(executor, "send_microsoft_draft", draft_id=DRAFT)
    assert answer.startswith(f"Draft {DRAFT} has been sent.")
    assert "images as links" in answer
    body = graph.drafts[DRAFT]["content"]
    own = body[: body.index("divRplyFwdMsg")]
    # No dangling reference in the signature that was sent: its images are links.
    assert "cid:" not in own and LOGO in own and ICON in own
    assert own.count('<div id="Signature">') == 1
    # The re-sign happened before the send, the send happened once.
    patches = [i for i, (m, _u, _a) in enumerate(graph.calls) if m == "patch"]
    assert patches and max(patches) < _send_index(graph)
    assert len([c for c in graph.calls if c[0] == "post" and c[1].endswith("/send")]) == 1


async def test_a_draft_whose_images_land_after_one_more_attach_is_sent_with_them_inline(graph: SimpleNamespace) -> None:
    executor = _outlook()
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    graph.calls.clear()
    graph.hidden = 4  # The four reads of the first wait miss them; the re-embedded draft settles.
    answer = await _run(executor, "send_microsoft_draft", draft_id=DRAFT)
    assert "attached again" in answer
    body = graph.drafts[DRAFT]["content"]
    own = body[: body.index("divRplyFwdMsg")]
    assert f"cid:{images.content_id(LOGO)}" in own and f"cid:{images.content_id(ICON)}" in own
    assert LOGO not in own


def test_cid_references_are_read_once_each_in_order() -> None:
    html = '<img src="cid:a@x"><img alt="" src=\'cid:B@x\'><img src="cid:A@x"><img src="https://x/y.png">'
    assert images.cid_references(html) == ["a@x", "B@x"]


def test_strip_images_drops_only_the_unresolved_ones() -> None:
    block = '<div id="Signature"><img src="cid:a@x" alt=""><img src="cid:b@x"><p>Sam</p></div>'
    assert images.strip_images(block, ["A@x"]) == '<div id="Signature"><img src="cid:b@x"><p>Sam</p></div>'


def _mime(html: str, part_ids: list[str]) -> bytes:
    message = EmailMessage()
    message["Subject"] = "S"
    message.set_content(html, subtype="html")
    for cid in part_ids:
        message.add_related(b"\x89PNG", maintype="image", subtype="png", cid=f"<{cid}>")
    return message.as_bytes()


def test_dangling_mime_cids_names_the_references_no_part_carries() -> None:
    html = '<p>Hi</p><img src="cid:sig-1@xtm-one"><img src="cid:sig-2@xtm-one">'
    assert images.dangling_mime_cids(_mime(html, ["sig-1@xtm-one", "sig-2@xtm-one"])) == []
    assert images.dangling_mime_cids(_mime(html, ["sig-1@xtm-one"])) == ["sig-2@xtm-one"]
    assert images.dangling_mime_cids(b"\x00not a message") == []
