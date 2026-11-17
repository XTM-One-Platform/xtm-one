"""Outlook mail signs once, with its signature images inline, above a quote.

A signature service hands out ``https`` image links; the mail tools attach
each image once as an inline ``cid:`` attachment on every path that writes a
signature, send a signed draft as it is, and put a new email's signature above
the quoted history its body carries.
"""

from __future__ import annotations

import uuid
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
LINKED = f"{MESSAGE_ID}LINKED"
REPLY_BODY = (
    '<html><body><div>Thanks</div><div id="appendonsend"></div><hr style="display:inline-block">'
    '<div id="divRplyFwdMsg">From: Alice</div><p>Earlier</p></body></html>'
)


async def _signature_of(_address: str):
    html = f'<table><tr><td><img src="{LOGO}"><img src="{ICON}"><img src="{LOGO}"></td><td>Sam</td></tr></table>'
    return EmailSignature(html=html, reply_html=html, name="Main")


class _Ok:
    def __init__(self, payload: dict | None = None):
        self._payload = payload or {}
        self.status_code = 200

    def json(self):
        return self._payload


@pytest.fixture
def graph(monkeypatch):
    """Graph in memory: drafts' bodies and attachments; image downloads counted."""
    state = SimpleNamespace(calls=[], drafts={}, attachments={}, fetched=[])

    async def _fake(_client, method, url, _provider, _action, **kwargs):
        state.calls.append((method, url, kwargs))
        if url.endswith("/me"):
            return _Ok({"mail": "sam@example.com"}), None
        if url.endswith("/createReply") or url.endswith("/createReplyAll"):
            state.drafts[DRAFT] = {"contentType": "html", "content": REPLY_BODY}
            return _Ok({"id": DRAFT, "body": state.drafts[DRAFT], "toRecipients": []}), None
        for draft_id in list(state.drafts):
            base = f"/me/messages/{draft_id}"
            if url.endswith(base + "/attachments"):
                listed = state.attachments.setdefault(draft_id, [])
                if method == "get":
                    return _Ok({"value": [{"id": str(i), "name": a["name"]} for i, a in enumerate(listed)]}), None
                listed.append(kwargs["json"])
                return _Ok({"id": "a"}), None
            if url.endswith(base) and method == "patch":
                state.drafts[draft_id] = kwargs["json"]["body"]
                return _Ok({}), None
            if url.endswith(base) and method == "get":
                return _Ok({"subject": "S", "toRecipients": [], "body": state.drafts[draft_id]}), None
        return _Ok({"id": "draft-2"}), None

    async def _fetch(url: str, **_kwargs):
        state.fetched.append(url)
        return FetchedFile(data=b"\x89PNG-" + url.encode(), content_type="image/png", filename="")

    async def _quiet(*_args, **_kwargs):
        return None

    monkeypatch.setattr(ms._common, "safe_request", _fake)
    monkeypatch.setattr(mail, "record_mail_draft", _quiet)
    monkeypatch.setattr(mail, "record_mail_send", _quiet)
    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _fetch)
    return state


async def _run(executor: CallableToolExecutor, name: str, **arguments: Any) -> str:
    result = await executor.execute_tool(ToolCall(id="c1", name=name, arguments=arguments))
    return result.content


def _outlook():
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor, signature_fn=_signature_of, signature_plan=SIGNED)
    return executor


async def test_a_draft_reply_carries_each_signature_image_inline_once(graph):
    """A draft left with the service's links had them embedded again on each save, up to
    twenty copies of each image: the draft holds them inline once, under their content ids."""
    executor = _outlook()
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    body = graph.drafts[DRAFT]["content"]
    assert LOGO not in body and ICON not in body
    assert body.count('<div id="Signature">') == 1
    assert body.index("Hello") < body.index('<div id="Signature">') < body.index("divRplyFwdMsg")
    assert f"cid:{images.content_id(LOGO)}" in body and f"cid:{images.content_id(ICON)}" in body
    attached = graph.attachments[DRAFT]
    assert sorted(a["contentId"] for a in attached) == sorted({images.content_id(LOGO), images.content_id(ICON)})
    assert all(a["isInline"] for a in attached)
    assert sorted(graph.fetched) == sorted([LOGO, ICON])


async def test_the_agent_sends_a_signed_draft_without_resigning_it_or_adding_a_second_image_set(graph):
    executor = _outlook()
    await _run(executor, "create_microsoft_draft_reply", message_id=MESSAGE_ID, body="Hello")
    created = graph.drafts[DRAFT]["content"]
    graph.calls.clear()
    assert await _run(executor, "send_microsoft_draft", draft_id=DRAFT) == f"Draft {DRAFT} has been sent."
    # Same body, same single signature, the same two inline images: nothing added at the send.
    assert graph.drafts[DRAFT]["content"] == created
    assert not [c for c in graph.calls if c[0] == "patch"]
    assert len(graph.attachments[DRAFT]) == 2
    assert sorted(graph.fetched) == sorted([LOGO, ICON])


async def test_send_draft_embeds_the_images_of_a_signature_still_linked(graph):
    graph.drafts[LINKED] = {
        "contentType": "html",
        "content": f'<html><body><p>Hi</p><div id="Signature"><img src="{LOGO}"><p>Sam</p></div></body></html>',
    }
    graph.attachments[LINKED] = [{"name": images.attachment_name_prefix(ICON) + ".png"}]
    executor = _outlook()
    await _run(executor, "send_microsoft_draft", draft_id=LINKED, force_duplicate=True)
    assert len(graph.attachments[LINKED]) == 2
    assert f"cid:{images.content_id(LOGO)}" in graph.drafts[LINKED]["content"]
    # A second pass finds the image attached under its name and attaches nothing.
    graph.drafts[LINKED]["content"] = graph.drafts[LINKED]["content"].replace(f"cid:{images.content_id(LOGO)}", LOGO)
    await _run(executor, "send_microsoft_draft", draft_id=LINKED, force_duplicate=True)
    assert len(graph.attachments[LINKED]) == 2
    assert graph.fetched == [LOGO]


async def test_send_draft_embeds_a_sent_items_signature_without_a_signature_integration(graph):
    """No signature integration: a draft signed with the Sent Items copy still gets its images inline once."""
    graph.drafts[LINKED] = {
        "contentType": "html",
        "content": f'<html><body><p>Hi</p><div id="Signature"><img src="{LOGO}"><p>Sam</p></div></body></html>',
    }
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor)
    await _run(executor, "send_microsoft_draft", draft_id=LINKED)
    assert [a["contentId"] for a in graph.attachments[LINKED]] == [images.content_id(LOGO)]
    assert f"cid:{images.content_id(LOGO)}" in graph.drafts[LINKED]["content"]
    assert graph.fetched == [LOGO]


async def test_send_draft_with_signatures_off_sends_the_draft_untouched(graph):
    graph.drafts[LINKED] = {
        "contentType": "html",
        "content": f'<html><body><p>Hi</p><div id="Signature"><img src="{LOGO}"></div></body></html>',
    }
    executor = CallableToolExecutor()
    ms.build_tools(token="dummy", executor=executor, signature_plan=SignaturePlan(enabled=False))
    await _run(executor, "send_microsoft_draft", draft_id=LINKED)
    assert graph.attachments.get(LINKED, []) == []
    assert graph.fetched == []
    assert [m for m, _u, _k in graph.calls] == ["post"]


async def test_send_email_attaches_the_signature_images_inline_and_signs_above_the_quote(graph):
    executor = _outlook()
    body = (
        "<p>Kind regards,</p><p>Sam</p>"
        "<div><p>________________________________</p><p>From: Alice &lt;a@example.com&gt;<br>Sent: Monday</p>"
        "<p>Earlier text</p></div>"
    )
    await _run(executor, "send_microsoft_email", to="a@example.com", subject="RE: Plan", body=body, content_type="html")
    (send,) = [k["json"]["message"] for _m, url, k in graph.calls if url.endswith("/sendMail")]
    content = send["body"]["content"]
    assert content.index("Kind regards") < content.index('<div id="Signature">') < content.index("____")
    assert sorted(a["contentId"] for a in send["attachments"]) == sorted(
        {images.content_id(LOGO), images.content_id(ICON)}
    )


@pytest.mark.parametrize(
    ("html", "quote"),
    [
        ("<p>Hello</p>", None),
        ("<p>Hello</p><p>From: Alice<br>Sent: Monday</p><p>Old</p>", "<p>From: Alice"),
        ("<p>Hello</p><hr><p><b>From:</b> Alice<br><b>Date:</b> Monday</p>", "<hr>"),
        ("<p>Hello</p><blockquote>Old</blockquote>", "<blockquote>"),
        ("<p>Hello</p>\n-----Original Message-----\nOld", "-----Original"),
        # The quote opens the body: nothing of the email's own comes first.
        ("<p>From: Alice<br>To: Bob</p><p>Old</p>", None),
        # Prose that says "from:" and "To:" is no header.
        ("<p>Ping me from: home or office. To: be confirmed</p>", None),
    ],
)
def test_sign_above_quote_places_the_signature_before_a_quote(html, quote):
    signed = mail.sign_above_quote(f"<html><body>{html}</body></html>", '<div id="Signature">SIG</div>')
    if quote is None:
        assert signed.index("SIG") > signed.index(html[-6:])
    else:
        assert signed.index("Hello") < signed.index("SIG") < signed.index(quote)


async def test_a_failed_signature_image_fetch_never_logs_its_signed_url(monkeypatch, caplog):
    """A signed CDN link (its token in the query) never reaches the logs, nor the redirect hop the error names."""
    signed = "https://cdn.example.com/sig/logo.png?sig=SECRET-TOKEN&exp=99"
    hop = "https://cdn2.example.com/other/path?token=HOP-SECRET"

    async def _refused(url: str, **_kwargs):
        return f"Error: the signature image at {url} redirected to {hop}, which is refused."

    monkeypatch.setattr("app.integrations.providers.social_media.fetch_public_media", _refused)
    caplog.set_level("INFO", logger=images.logger.name)
    assert await images.fetch_image(signed) is None
    logged = caplog.text
    assert "SECRET-TOKEN" not in logged and "HOP-SECRET" not in logged and "/sig/logo.png" not in logged
    assert "https://cdn.example.com/[sig-" in logged and "https://cdn2.example.com/[sig-" in logged


async def test_an_image_already_attached_is_reused_under_its_own_content_id_and_a_second_copy_goes():
    """The doubled set and the literal [cid:...]: one attachment per image, its real content id."""
    url = "https://graph/me/messages/d"
    prefix = images.attachment_name_prefix(LOGO)
    listed = {
        "a1": {"name": f"{prefix}.png", "isInline": True, "contentId": "copied-by-outlook@x"},
        "a2": {"name": f"{prefix}.png", "isInline": True, "contentId": "copied-by-outlook@x"},
        "a3": {"name": "report.pdf", "isInline": False},
    }
    calls: list[tuple[str, str]] = []

    async def _request(_client, method, target, _provider, _action, **_kwargs):
        calls.append((method, target))
        if target == f"{url}/attachments":
            return _Ok({"value": [{"id": k, "name": v["name"], "isInline": v["isInline"]} for k, v in listed.items()]}), None
        key = target.rsplit("/", 1)[-1]
        if method == "get" and key in listed:
            return _Ok(listed[key]), None
        return _Ok({}), None

    block = f'<div id="Signature"><img src="{LOGO}"></div>'
    content = f"<html><body><p>Hi</p>{block}</body></html>"
    updated, note = await images.embed_signature_images(_request, None, url, content, block)
    assert note == ""
    assert 'src="cid:copied-by-outlook@x"' in updated and LOGO not in updated
    assert not [c for c in calls if c[0] == "post"]
    assert [t.rsplit("/", 1)[-1] for m, t in calls if m == "delete"] == ["a2"]


async def test_a_stale_signature_image_goes_even_when_no_current_image_can_be_fetched(monkeypatch):
    url = "https://graph/me/messages/d"
    listed = {"a/1": {"name": "sig-old.png", "isInline": True, "contentId": "sig-old@xtm-one"}}
    calls: list[tuple[str, str]] = []

    async def _request(_client, method, target, _provider, _action, **_kwargs):
        calls.append((method, target))
        if target == f"{url}/attachments":
            return _Ok({"value": [{"id": k, "name": v["name"], "isInline": v["isInline"]} for k, v in listed.items()]}), None
        if method == "get" and target.endswith("/attachments/a%2F1"):
            return _Ok(listed["a/1"]), None
        return _Ok({}), None

    async def _nothing(_url):
        return None

    monkeypatch.setattr(images, "fetch_image", _nothing)
    block = f'<div id="Signature"><img src="{LOGO}"></div>'
    content = f"<html><body><p>Hi</p>{block}</body></html>"
    updated, note = await images.embed_signature_images(_request, None, url, content, block)
    assert updated == content and "stay links" in note
    # The attachment id is encoded in the path; the stale image is removed.
    assert ("delete", f"{url}/attachments/a%2F1") in calls


def test_rewrite_sources_keeps_an_image_that_was_not_embedded():
    block = f'<img src="{LOGO}"><img src="{ICON}">'
    embedded = {LOGO: images.InlineImage(LOGO, "n.png", images.content_id(LOGO), "image/png", b"x")}
    rewritten = images.rewrite_sources(block, embedded)
    assert f"cid:{images.content_id(LOGO)}" in rewritten and ICON in rewritten
