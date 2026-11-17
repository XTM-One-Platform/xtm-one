"""Identical file attachments on an Outlook draft, found and removed before a send.

A draft the mail tools created and a person then opened in new Outlook
desktop can have its file attachments attached again by a signature add-in
at a later open: the draft then holds two copies of the same PDF, and Outlook
sends both (it strips only the unreferenced inline images). Before a draft is
sent, every file attachment identical to an earlier one (same name, same
size, same bytes by SHA-256) is removed; the oldest copy stays. Inline images
and item or reference attachments are never touched, and a copy whose bytes
cannot be read is kept. The read-back tools name the copies by name and size.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote as _url_encode

logger = logging.getLogger(__name__)

GraphRequest = Callable[..., Awaitable[tuple[Any, str | None]]]

_FILE_ATTACHMENT = "#microsoft.graph.fileAttachment"


def _is_file_attachment(attachment: dict[str, Any]) -> bool:
    """A non-inline file attachment (Graph omits ``@odata.type`` under some ``$select``)."""
    if attachment.get("isInline"):
        return False
    kind = attachment.get("@odata.type")
    return kind in (None, "", _FILE_ATTACHMENT)


def _key(attachment: dict[str, Any]) -> tuple[str, int]:
    try:
        size = int(attachment.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return str(attachment.get("name") or "attachment"), size


def same_name_and_size(attachments: list[dict[str, Any]]) -> list[tuple[str, int, int]]:
    """The file attachments listed more than once under one name and size: ``(name, size, copies)``."""
    groups: dict[tuple[str, int], int] = {}
    for attachment in attachments:
        if isinstance(attachment, dict) and _is_file_attachment(attachment):
            key = _key(attachment)
            groups[key] = groups.get(key, 0) + 1
    return [(name, size, count) for (name, size), count in groups.items() if count > 1]


def duplicates_line(attachments: list[dict[str, Any]]) -> str:
    """One read-back line naming the duplicate file attachments, or ``""`` when there are none."""
    groups = same_name_and_size(attachments)
    if not groups:
        return ""
    listed = ", ".join(f"{name} x{count} ({size} B each)" for name, size, count in groups)
    return f"Duplicate file attachments (same name and size): {listed}"


def _sha256(content_bytes: Any) -> str | None:
    if not isinstance(content_bytes, str) or not content_bytes:
        return None
    try:
        return hashlib.sha256(base64.b64decode(content_bytes, validate=True)).hexdigest()
    except (binascii.Error, ValueError):
        return None


async def remove_duplicate_file_attachments(
    request: GraphRequest, client: Any, message_url: str, *, provider: str = "microsoft"
) -> list[str]:
    """Remove the file attachments of *message_url* identical to an earlier one; the names removed, one per copy.

    Never raises and never holds a send back: an attachment list or a copy
    that cannot be read leaves the message as it is.
    """
    resp, err = await request(
        client, "get", f"{message_url}/attachments", provider, "list attachments",
        params={"$select": "id,name,size,isInline,lastModifiedDateTime"},
    )
    if err or resp is None:
        return []
    try:
        payload = resp.json()
    except ValueError:
        return []
    listed = payload.get("value") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        return []
    candidates = {(name, size) for name, size, _count in same_name_and_size(listed)}
    if not candidates:
        return []
    ordered = sorted(
        (a for a in listed if isinstance(a, dict) and _is_file_attachment(a) and _key(a) in candidates and a.get("id")),
        key=lambda a: str(a.get("lastModifiedDateTime") or ""),
    )
    seen: set[tuple[str, int, str]] = set()
    removed: list[str] = []
    for attachment in ordered:
        attachment_url = f"{message_url}/attachments/{_url_encode(str(attachment['id']), safe='')}"
        detail, detail_err = await request(client, "get", attachment_url, provider, "read attachment to compare")
        if detail_err or detail is None:
            continue
        try:
            data = detail.json()
        except ValueError:
            continue
        digest = _sha256(data.get("contentBytes") if isinstance(data, dict) else None)
        if digest is None:
            continue
        name, size = _key(attachment)
        if (name, size, digest) not in seen:
            seen.add((name, size, digest))
            continue
        _, del_err = await request(client, "delete", attachment_url, provider, "remove duplicate attachment")
        if del_err:
            logger.info("A duplicate file attachment stays on a draft: %s", del_err)
            continue
        removed.append(name)
    return removed


def removed_note(removed: list[str]) -> str:
    """The send answer's sentence for *removed*, or ``""``."""
    if not removed:
        return ""
    count = len(removed)
    names = ", ".join(dict.fromkeys(removed))
    return f" {count} duplicate attachment{'s' if count > 1 else ''} removed ({names})."
