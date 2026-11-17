"""Signature images embedded as inline attachments on Outlook mail.

A signature service (Letsignit) hands out a signature whose images are
``https`` links on its storage. Sent as links, many mail clients (Outlook
desktop by default) block them and show broken placeholders, while the
signature add-in, when a person sends from Outlook, embeds them as inline
``cid:`` attachments. The mail tools do the same on every path that writes a
signature: each distinct image of the ``<div id="Signature">`` block of the
message's own part is fetched once (no token, HTTPS only, every hop under the
outbound URL policy), attached once as an inline file attachment named after
its URL, and its ``src`` rewritten to ``cid:``. An image already attached
under that name is not attached again, so signing a message twice never
doubles an image; an image that cannot be fetched keeps its link.
"""

from __future__ import annotations

import base64
import email
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from email import policy as _email_policy
from html import unescape as _html_unescape
from typing import Any
from urllib.parse import quote as _url_encode
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: Largest signature image embedded; a larger one keeps its link.
MAX_SIGNATURE_IMAGE_BYTES = 512 * 1024
#: Most images of one signature embedded.
MAX_SIGNATURE_IMAGES = 20

_IMG_SRC_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])(https://[^"'\s>]+)\2""",
    re.IGNORECASE,
)
_CID_SRC_RE = re.compile(
    r"""(<img\b[^>]*?\bsrc\s*=\s*)(["'])cid:([^"'\s>]+)\2""",
    re.IGNORECASE,
)
#: Pauses (seconds) before a signed message is read again while an image its signature references is not on it yet.
SETTLE_DELAYS: tuple[float, ...] = (0.5, 1.0, 2.0)
_TOOL_CID_SUFFIX = "@xtm-one"
_EXTENSIONS = {"image/png": ".png", "image/gif": ".gif", "image/jpeg": ".jpg", "image/webp": ".webp"}
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"<>)]+", re.IGNORECASE)


def loggable(url: str) -> str:
    """*url* as a log line may carry it: its origin and a hash, never its path or query (a signed CDN token)."""
    parts = urlsplit(_html_unescape(url))
    return f"{parts.scheme}://{parts.hostname or ''}/[{_stem(url)}]"


def _redact_urls(text: str) -> str:
    """*text* with every link it quotes (the image's, a redirect hop's) reduced to :func:`loggable`."""
    return _URL_IN_TEXT_RE.sub(lambda m: loggable(m.group(0)), text)


@dataclass(frozen=True)
class InlineImage:
    """One signature image as Outlook attaches it inline."""

    url: str
    name: str
    content_id: str
    content_type: str
    data: bytes


def remote_images(block: str) -> list[str]:
    """The distinct ``https`` image links of a signature block, in order (as written in the HTML)."""
    urls: list[str] = []
    for match in _IMG_SRC_RE.finditer(block or ""):
        url = match.group(3)
        if url not in urls:
            urls.append(url)
        if len(urls) >= MAX_SIGNATURE_IMAGES:
            break
    return urls


def _stem(url: str) -> str:
    """The name an image link is attached under: stable for one link, so a second pass finds it."""
    return "sig-" + hashlib.sha256(_html_unescape(url).encode("utf-8")).hexdigest()[:20]


def attachment_name_prefix(url: str) -> str:
    return _stem(url)


def content_id(url: str) -> str:
    return f"{_stem(url)}@xtm-one"


async def fetch_image(url: str) -> InlineImage | None:
    """The image at a signature link, or ``None`` (not an image, too large, refused): it then keeps its link."""
    from app.integrations.providers.social_media import fetch_public_media

    target = _html_unescape(url)
    fetched = await fetch_public_media(
        target, source="signature", max_bytes=MAX_SIGNATURE_IMAGE_BYTES, what="signature image"
    )
    if isinstance(fetched, str):
        logger.info("A signature image keeps its link (%s): %s", loggable(url), _redact_urls(fetched))
        return None
    mime = (fetched.content_type or "").split(";")[0].strip().lower()
    if not mime.startswith("image/") or not fetched.data:
        logger.info("A signature link is not an image (%s); it keeps its link.", mime or "no type")
        return None
    path_ext = ""
    tail = urlsplit(target).path.rsplit("/", 1)[-1]
    if "." in tail:
        path_ext = "." + tail.rsplit(".", 1)[-1].lower()[:5]
    ext = _EXTENSIONS.get(mime) or path_ext or ".img"
    return InlineImage(
        url=url,
        name=f"{_stem(url)}{ext}",
        content_id=content_id(url),
        content_type=mime,
        data=fetched.data,
    )


#: How a provider sends one Graph request: ``(client, method, url, provider, action, **kwargs) -> (resp, err)``.
GraphRequest = Callable[..., Awaitable[tuple[Any, str | None]]]


async def embed_signature_images(
    request: GraphRequest,
    client: Any,
    message_url: str,
    content: str,
    block: str,
    *,
    provider: str = "microsoft",
) -> tuple[str, str]:
    """*content* with the remote images of its signature *block* attached inline once; and a note.

    *message_url* is the Graph URL of the draft (``.../messages/{id}``). An
    image already attached under its name (a second pass on one draft) is not
    attached again; one that cannot be fetched or attached keeps its link and
    is named in the note. The caller writes the returned body back (``PATCH``)
    when it changed.
    """
    urls = remote_images(block)
    if not urls or block not in content:
        return content, ""
    listed, err = await signature_attachments(request, client, message_url, provider=provider)
    if listed is None:
        return content, f" Its signature images stay links: the attachments could not be listed ({err})."
    names = [a.name for a in listed]
    embedded: dict[str, InlineImage] = {}
    kept: list[str] = []
    for url in urls:
        prefix = attachment_name_prefix(url)
        same = [a for a in listed if a.name.startswith(prefix)]
        if same:
            # An image Outlook copied with the quote, or a second pass: reused
            # under the content id it really carries, the one the
            # quote already references first, never attached a second time.
            reused = next((a for a in same if a.content_id and f"cid:{a.content_id}" in content), None)
            reused = reused or next((a for a in same if a.content_id), None) or same[0]
            embedded[url] = InlineImage(
                url=url, name=reused.name, content_id=reused.content_id or content_id(url), content_type="", data=b""
            )
            continue
        image = await fetch_image(url)
        if image is None:
            kept.append(url)
            continue
        _, err = await request(
            client,
            "post",
            f"{message_url}/attachments",
            provider,
            "attach signature image",
            json=graph_attachment(image),
        )
        if err:
            kept.append(url)
            continue
        names.append(image.name)
        embedded[url] = image
    note = f" {len(kept)} signature image(s) stay links (not fetchable as images)." if kept else ""
    if not embedded:
        # Nothing to rewrite, but a stale copy (an image the body no longer
        # references) still goes: one image set per message.
        await drop_unreferenced_inline_images(request, client, message_url, content, listed, provider=provider)
        return content, note
    start = content.index(block)
    updated = content[:start] + rewrite_sources(block, embedded) + content[start + len(block):]
    await drop_unreferenced_inline_images(request, client, message_url, updated, listed, provider=provider)
    return updated, note


@dataclass(frozen=True)
class ListedAttachment:
    """An attachment of a message as the signature tools need it."""

    id: str
    name: str
    is_inline: bool | None
    content_id: str


async def signature_attachments(
    request: GraphRequest, client: Any, message_url: str, *, provider: str = "microsoft", prefix: str = "sig-"
) -> tuple[list[ListedAttachment] | None, str | None]:
    """The attachments of a message, the inline ones named *prefix*... with their content id: ``(list, error)``.

    The list is ``None`` when the attachments could not be listed. The content
    id is read on those attachments only (Graph gives it on a file attachment
    read alone); one that cannot be read is empty. ``prefix=""`` reads every
    inline attachment's.
    """
    resp, err = await request(
        client, "get", f"{message_url}/attachments", provider, "list attachments",
        params={"$select": "id,name,isInline"},
    )
    if err or resp is None:
        return None, err or "no answer"
    listed: list[ListedAttachment] = []
    for item in (resp.json() or {}).get("value", []):
        name = str(item.get("name") or "")
        attachment_id = str(item.get("id") or "")
        inline = item.get("isInline")
        cid = ""
        if name.startswith(prefix) and attachment_id and inline is not False:
            detail, detail_err = await request(
                client, "get", f"{message_url}/attachments/{_url_encode(attachment_id, safe='')}", provider,
                "read signature image",
            )
            if not detail_err and detail is not None:
                try:
                    data = detail.json() or {}
                except ValueError:
                    data = {}
                cid = str(data.get("contentId") or "").strip("<>")
                if inline is None and "isInline" in data:
                    inline = bool(data.get("isInline"))
        listed.append(
            ListedAttachment(id=attachment_id, name=name, is_inline=inline if isinstance(inline, bool) else None, content_id=cid)
        )
    return listed, None


async def drop_unreferenced_inline_images(
    request: GraphRequest,
    client: Any,
    message_url: str,
    content: str,
    listed: list[ListedAttachment] | None = None,
    *,
    provider: str = "microsoft",
    prefix: str = "sig-",
) -> int:
    """Remove the inline images named *prefix*... that *content* does not reference, or holds twice; the count removed.

    ``createReply`` copies the inline images of the quote it builds: the
    images of a forward note dropped from the quote, or a second copy of a
    signature image, stayed on the message and reached the recipient as a
    second image set. Only an inline attachment whose content id is known and
    appears nowhere in *content* (or a second one under the same id) is
    removed; a file attachment is never touched. *prefix* defaults to the
    signature images the tools attach (``sig-``); ``""`` covers every inline
    image, for a quote the tools cut.
    """
    if listed is None:
        listed, _err = await signature_attachments(request, client, message_url, provider=provider, prefix=prefix)
        if listed is None:
            return 0
    lowered = content.lower()
    removed = 0
    kept_ids: set[str] = set()
    for attachment in listed:
        if not attachment.name.startswith(prefix) or attachment.is_inline is not True or not attachment.content_id:
            continue
        cid = attachment.content_id.lower()
        # One attachment per referenced image: a second copy under the same
        # content id is removed too.
        if f"cid:{cid}" in lowered and cid not in kept_ids:
            kept_ids.add(cid)
            continue
        _, err = await request(
            client, "delete", f"{message_url}/attachments/{_url_encode(attachment.id, safe='')}", provider,
            "remove unused signature image",
        )
        if err:
            logger.info("An unused signature image stays on a message: %s", err)
            continue
        removed += 1
    return removed


def graph_attachment(image: InlineImage) -> dict[str, Any]:
    """*image* as a Graph inline ``fileAttachment``."""
    return {
        "@odata.type": "#microsoft.graph.fileAttachment",
        "name": image.name,
        "contentType": image.content_type,
        "contentBytes": base64.b64encode(image.data).decode("ascii"),
        "isInline": True,
        "contentId": image.content_id,
    }


async def inline_signature_images(content: str, block: str) -> tuple[str, list[dict[str, Any]], str]:
    """For a message written in one request: *content* with its signature images as ``cid:``, their attachments, a note.

    The attachments go in the message's ``attachments`` (``sendMail`` or a
    draft's creation), each image once.
    """
    urls = remote_images(block)
    if not urls or block not in content:
        return content, [], ""
    embedded: dict[str, InlineImage] = {}
    kept = 0
    for url in urls:
        image = await fetch_image(url)
        if image is None:
            kept += 1
            continue
        embedded[url] = image
    note = f" {kept} signature image(s) stay links (not fetchable as images)." if kept else ""
    if not embedded:
        return content, [], note
    start = content.index(block)
    rewritten = content[:start] + rewrite_sources(block, embedded) + content[start + len(block):]
    return rewritten, [graph_attachment(image) for image in embedded.values()], note


_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_ALT_RE = re.compile(r"\balt\s*=", re.IGNORECASE)


def rewrite_sources(block: str, images: dict[str, InlineImage]) -> str:
    """*block* with the link of every embedded image replaced by its ``cid:`` reference.

    An embedded image without alt text is given an empty one (``alt=""``):
    Outlook's text alternative of the message prints an image without alt as
    ``[cid:...]``.
    """

    def _swap(match: re.Match[str]) -> str:
        image = images.get(match.group(3))
        if image is None:
            return match.group(0)
        return f"{match.group(1)}{match.group(2)}cid:{image.content_id}{match.group(2)}"

    def _alt(tag: re.Match[str]) -> str:
        text = tag.group(0)
        if "cid:" not in text.lower() or _ALT_RE.search(text):
            return text
        return f'{text[:4]} alt=""{text[4:]}'

    return _IMG_TAG_RE.sub(_alt, _IMG_SRC_RE.sub(_swap, block))


def cid_references(html: str) -> list[str]:
    """The distinct ``cid:`` image references of *html*, in order."""
    found: list[str] = []
    for match in _CID_SRC_RE.finditer(html or ""):
        cid = _html_unescape(match.group(3)).strip("<>")
        if cid.lower() not in {c.lower() for c in found}:
            found.append(cid)
    return found


def _tool_stem(cid: str) -> str | None:
    """The attachment name stem of a content id the tools gave (``sig-<hash>@xtm-one``), else ``None``."""
    lowered = cid.lower()
    if lowered.startswith("sig-") and lowered.endswith(_TOOL_CID_SUFFIX):
        return lowered[: -len(_TOOL_CID_SUFFIX)]
    return None


async def unresolved_cids(
    request: GraphRequest, client: Any, message_url: str, html: str, *, provider: str = "microsoft"
) -> list[str] | None:
    """The ``cid:`` references of *html* that no inline attachment of the message carries yet.

    ``None`` when the attachments could not be listed. An image the tools
    attached is found by its name (``sig-<hash>``) in one listing; any other
    reference by the content ids of the inline attachments, read only then.
    """
    cids = cid_references(html)
    if not cids:
        return []
    resp, err = await request(
        client,
        "get",
        f"{message_url}/attachments",
        provider,
        "list attachments",
        params={"$select": "id,name,isInline"},
    )
    if err or resp is None:
        return None
    try:
        items = (resp.json() or {}).get("value", [])
    except ValueError:
        return None
    names = [str(item.get("name") or "").lower() for item in items if item.get("isInline") is not False]
    missing = []
    for cid in cids:
        stem = _tool_stem(cid)
        if stem is None or not any(name.startswith(stem) for name in names):
            missing.append(cid)
    if any(_tool_stem(cid) is None for cid in missing):
        listed, _err = await signature_attachments(request, client, message_url, provider=provider, prefix="")
        if listed is None:
            return None
        known = {a.content_id.lower() for a in listed if a.content_id and a.is_inline is not False}
        missing = [cid for cid in missing if cid.lower() not in known]
    return missing


def strip_images(block: str, cids: list[str]) -> str:
    """*block* without the images referenced by one of *cids*: never a ``cid:`` reference nothing resolves."""
    wanted = {cid.lower() for cid in cids}

    def _strip(tag: re.Match[str]) -> str:
        text = tag.group(0)
        match = _CID_SRC_RE.search(text)
        if match is None:
            return text
        return "" if _html_unescape(match.group(3)).strip("<>").lower() in wanted else text

    return _IMG_TAG_RE.sub(_strip, block)


def dangling_mime_cids(mime: bytes) -> list[str]:
    """The ``cid:`` images a message's HTML (as MIME) references that none of its parts carries.

    Read on the MIME itself, so a content id Exchange renamed in both the
    HTML and the part still matches; an unreadable MIME answers none.
    """
    try:
        message = email.message_from_bytes(mime, policy=_email_policy.default)
        ids: set[str] = set()
        html: list[str] = []
        for part in message.walk():
            part_id = part.get("Content-ID")
            if part_id:
                ids.add(str(part_id).strip().strip("<>").lower())
            if part.get_content_type() == "text/html":
                html.append(str(part.get_content()))
    except Exception:  # a MIME the parser refuses is sent as before
        logger.info("A draft's MIME could not be parsed for its inline images; it is sent as read.")
        return []
    return [cid for cid in cid_references("".join(html)) if cid.lower() not in ids]
