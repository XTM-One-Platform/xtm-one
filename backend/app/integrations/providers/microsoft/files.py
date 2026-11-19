"""OneDrive file tools: read, search, download and upload drive files, comment Word documents, and send files to Teams."""

from __future__ import annotations

import functools
import json
import logging
import mimetypes
import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import quote as _url_encode
from urllib.parse import unquote as _url_decode

from app.core.parse_offload import run_parse
from app.core.utils import format_exception_message
from app.integrations.file_content import (
    keep_file,
    kept_file_line,
    kilobytes,
)
from app.integrations.file_delivery import (
    FetchError,
    fetch_file,
    untrusted_file_facts,
    uploaded_file_answer,
)
from app.integrations.http import AutoRefreshClient, format_entity_list
from app.integrations.word_comments import (
    MAX_ANCHOR_CHARS,
    MAX_COMMENT_CHARS,
    MAX_COMMENTS,
    CommentedDocument,
    CommentRequest,
    ExistingComment,
    WordDocumentError,
    add_word_comments,
    list_word_comments,
    remove_word_comments,
)
from app.llm.base import FileRef
from app.services.agent_posts import (
    record_teams_channel_post,
)
from app.services.teams_format import build_graph_chat_body
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import (
    _BASE_URL,
    _MS_MAX_FILE_BYTES,
    _PROVIDER,
    MicrosoftContext,
    _resolve_platform_file,
)
from ._drive import (
    _FOLDER_PAGE_DEFAULT,
    _FOLDER_PAGE_MAX,
    _MAX_DOCUMENT_BYTES,
    _WORKBOOK_EXTS,
    _drive_item_link_lines,
    _encode_drive_filename,
    _encode_drive_path,
    _identity_label,
    _is_folder,
    _list_folder,
    _read_drive_item,
    _read_workbook_worksheet,
    _resolve_drive_url,
    _resolve_share_url,
    _split_workbook_path_and_range,
    _with_drive_links,
)
from ._teams import (
    _channel_message_url,
    _channel_url,
    _teams_segment,
    _thread_route,
    _validate_teams_channel_ids,
    _validate_teams_chat_id,
    _validate_teams_message_id,
)
from .sharepoint import _site_drive_base

logger = logging.getLogger(__name__)

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# What the comment tool reads of a driveItem: its identity and links, the
# eTag the save is conditioned on, and the checkout state SharePoint reports.
_DOCUMENT_SELECT = "id,name,size,file,eTag,webUrl,parentReference,publication"
_ANCHOR_PREVIEW_CHARS = 80
_COMMENT_PREVIEW_CHARS = 200
#: The ``comment_ids`` argument of ``delete_microsoft_document_comments``.
ALL_MINE = "all_mine"


@dataclass(frozen=True)
class _WordDocument:
    """A ``.docx`` the comment tools work on: where Graph holds it, what it
    said of it, and its bytes."""

    item_base: str
    name: str
    meta: dict
    #: The eTag the save is conditioned on (``If-Match``).
    etag: str
    data: bytes
    #: The connected account as the comment tools name it: the author
    #: ``add_word_comments`` writes and the one a delete may remove without
    #: ``include_others``. Empty when the tool did not read ``/me``.
    account: str = ""


def _parse_comment_requests(comments: object) -> tuple[list[CommentRequest], str | None]:
    """The ``comments`` argument of ``add_microsoft_document_comments`` as
    :class:`CommentRequest` objects, or the error that refuses the whole call
    before any request is made."""
    shape = "comments must be a non-empty list of {anchor, comment, occurrence?} objects"
    if isinstance(comments, str):
        try:
            comments = json.loads(comments)
        except ValueError:
            return [], f"Error: {shape} (the JSON given could not be parsed)."
    if isinstance(comments, dict):
        comments = [comments]
    if not isinstance(comments, list) or not comments:
        return [], f"Error: {shape}."
    if len(comments) > MAX_COMMENTS:
        return [], f"Error: at most {MAX_COMMENTS} comments per call ({len(comments)} given); split the review."
    requests: list[CommentRequest] = []
    for number, entry in enumerate(comments, 1):
        if not isinstance(entry, dict):
            return [], f"Error: comment {number} is not an object; {shape}."
        anchor = entry.get("anchor")
        body = entry.get("comment")
        if not isinstance(anchor, str) or not anchor.strip():
            return [], (
                f"Error: comment {number} has no 'anchor': the exact text of the document it "
                "is on, copied verbatim from the document's text."
            )
        if not isinstance(body, str) or not body.strip():
            return [], f"Error: comment {number} has no 'comment' text."
        anchor, body = anchor.strip(), body.strip()
        if len(anchor) > MAX_ANCHOR_CHARS:
            return [], (
                f"Error: comment {number}'s anchor is {len(anchor):,} characters; quote a "
                f"passage of at most {MAX_ANCHOR_CHARS:,}."
            )
        if len(body) > MAX_COMMENT_CHARS:
            return [], f"Error: comment {number} is over {MAX_COMMENT_CHARS:,} characters."
        raw = entry.get("occurrence")
        occurrence: int | None = None
        if raw not in (None, ""):
            try:
                occurrence = int(raw) if not isinstance(raw, bool) else None
            except (TypeError, ValueError):
                occurrence = None
            if occurrence is None or occurrence < 1:
                return [], f"Error: comment {number}'s occurrence must be a whole number from 1 ({raw!r} given)."
        requests.append(CommentRequest(anchor, body, occurrence))
    return requests, None


def _preview(text: str, limit: int) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 3] + "..."


def _anchor_preview(anchor: str) -> str:
    return _preview(anchor, _ANCHOR_PREVIEW_CHARS)


def _comment_outcome_lines(commented: CommentedDocument) -> list[str]:
    """One line per request, numbered as given: placed with its context, or refused with the reason."""
    return [
        f'- {number}. "{_anchor_preview(outcome.request.anchor)}": '
        + (f"placed on {outcome.detail}" if outcome.placed else f"refused, {outcome.detail}")
        for number, outcome in enumerate(commented.outcomes, 1)
    ]


def _parse_comment_ids(raw: object) -> tuple[list[int], bool, str | None]:
    """``(ids, all_mine, error)`` for the ``comment_ids`` argument of
    ``delete_microsoft_document_comments``: the ids as a list, a JSON list, or
    a comma / space separated string (a leading ``#`` tolerated), or
    ``all_mine``. The error refuses the whole call before any request."""
    shape = (
        'comment_ids must be the ids of the comments to remove, separated by commas (e.g. "3, 7"), '
        f'or "{ALL_MINE}" for every comment by the connected account'
    )
    if isinstance(raw, str):
        text = raw.strip().strip("\"'")
        if text.lower() == ALL_MINE:
            return [], True, None
        if text[:1] == "[":
            try:
                raw = json.loads(text)
            except ValueError:
                return [], False, f"Error: {shape} (the JSON given could not be parsed)."
        else:
            raw = [part for part in re.split(r"[,;\s]+", text) if part]
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return [], False, f"Error: {shape}."
    if len(raw) == 1 and isinstance(raw[0], str) and raw[0].strip().lower() == ALL_MINE:
        return [], True, None
    ids: list[int] = []
    for item in raw:
        if isinstance(item, int) and not isinstance(item, bool):
            ids.append(item)
        elif isinstance(item, float) and item.is_integer():
            ids.append(int(item))
        elif isinstance(item, str) and re.fullmatch(r"#?\d+", item.strip()):
            ids.append(int(item.strip().lstrip("#")))
        else:
            return [], False, f"Error: {item!r} is not a comment id; {shape}."
    return list(dict.fromkeys(ids)), False, None


def _same_author(name: str, account: str) -> bool:
    """Whether the comment author *name* is the connected *account*: the
    name as ``add_microsoft_document_comments`` writes it, compared without
    case or spacing differences. ``w:author`` is free text, so a collaborator
    named like the account matches too; the delete tool's description says
    so. The initials are no better a key (Word derives its own from the
    author's profile, and a namesake has the same ones)."""

    def fold(value: str) -> str:
        return " ".join(value.split()).casefold()

    return bool(account.strip()) and fold(name) == fold(account)


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _comment_line(comment: ExistingComment) -> str:
    """One listed comment: id, author, date, reply / resolved marks, the
    passage it is on and its text."""
    parts = [f"#{comment.comment_id} by {comment.author or 'an unnamed author'}"]
    if comment.date:
        parts.append(comment.date)
    if comment.parent_id is not None:
        parts.append(f"reply to #{comment.parent_id}")
    if comment.done:
        parts.append("resolved")
    if comment.anchor_text:
        where = f'on "{_anchor_preview(comment.anchor_text)}"'
    elif comment.passage_context:
        where = f"at {comment.passage_context}"
    else:
        where = "on no passage"
    return f"- {', '.join(parts)}, {where}: {_preview(comment.text, _COMMENT_PREVIEW_CHARS) or '(no text)'}"


def _comment_label(comment: ExistingComment | None, with_anchor: bool = True) -> str:
    """`` by <author> on "<anchor>"`` for a known comment, nothing otherwise."""
    if comment is None:
        return ""
    label = f" by {comment.author or 'an unnamed author'}"
    if not with_anchor:
        return label
    if comment.anchor_text:
        return f'{label} on "{_anchor_preview(comment.anchor_text)}"'
    return f"{label} (no anchored text)"


def _file_message_text(message: str, file_name: str) -> str:
    """The text an agent account's log keeps of a file message: its caption and the file."""
    caption = (message or "").strip()
    return f"{caption}\n[File: {file_name}]" if caption else f"[File: {file_name}]"


@dataclass(frozen=True)
class _ShareRecipient:
    """A chat member a file sent into the chat is shared with."""

    label: str
    user_id: str
    email: str

    def drive_recipient(self) -> dict[str, str]:
        """The Graph ``driveRecipient``: the Entra object ID when known, else the email."""
        return {"objectId": self.user_id} if self.user_id else {"email": self.email}

    def keys(self) -> set[str]:
        return {key.lower() for key in (self.user_id, self.email) if key}


def _chat_share_recipients(members: list[dict], sender_id: str) -> list[_ShareRecipient]:
    """Every member of a chat but the sender, each once."""
    sender = (sender_id or "").strip().lower()
    recipients: list[_ShareRecipient] = []
    seen: set[str] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        user_id = str(member.get("userId") or "").strip()
        email = str(member.get("email") or "").strip()
        if sender and user_id.lower() == sender:
            continue
        key = (user_id or email).lower()
        if not key or key in seen:
            continue
        seen.add(key)
        name = str(member.get("displayName") or "").strip()
        label = f"{name} <{email}>" if name and email else (name or email or user_id)
        recipients.append(_ShareRecipient(label=label, user_id=user_id, email=email))
    return recipients


def _permission_identities(permission: dict) -> set[str]:
    """The user IDs and emails a drive item permission is granted to, lower-cased."""
    found: set[str] = set()
    users = [
        (permission.get("grantedToV2") or {}).get("user"),
        (permission.get("grantedTo") or {}).get("user"),
        *[(identity or {}).get("user") for identity in permission.get("grantedToIdentitiesV2") or []],
    ]
    for user in users:
        if isinstance(user, dict):
            found.update(str(user.get(field) or "").lower() for field in ("id", "email"))
    invitation = permission.get("invitation") or {}
    found.add(str(invitation.get("email") or "").lower())
    found.discard("")
    return found


def _ungranted_recipients(recipients: list[_ShareRecipient], permissions: list[dict]) -> list[str]:
    """The labels of the recipients no read (or wider) permission of *permissions* names."""
    granted: set[str] = set()
    for permission in permissions:
        if isinstance(permission, dict) and set(permission.get("roles") or []) & {"read", "write", "owner"}:
            granted |= _permission_identities(permission)
    return [recipient.label for recipient in recipients if not recipient.keys() & granted]


def register(ctx: MicrosoftContext, executor: CallableToolExecutor) -> list[ToolDefinition]:
    """Register the files tools on *executor* and return their definitions."""
    _client = ctx.client
    _throttled_request = ctx.request
    persist_file_fn = ctx.persist_file_fn
    _api_sem = ctx.sem

    # ── OneDrive Tools ─────────────────────────────────────────────────

    async def list_microsoft_drive_files(folder: str = "root", count: int = 20) -> str:
        """List files in OneDrive."""
        path = "root/children" if folder == "root" else f"root:/{folder}:/children"
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/drive/{path}?$top={count}&$select=name,size,lastModifiedDateTime,lastModifiedBy,webUrl",
                _PROVIDER,
                "list drive files",
            )
            if err:
                return err
            items = resp.json().get("value", [])

            def _fmt_file(i: dict) -> str:
                line = f"{i['name']} ({i.get('size', 0)} bytes) — {i.get('webUrl', '')}"
                modified_by = _identity_label(i.get("lastModifiedBy"))
                return f"{line} — modified by {modified_by}" if modified_by else line

            return format_entity_list(items, _fmt_file, noun="file")

    async def get_microsoft_file_content(
        item_path: str,
        worksheet: str | None = None,
        cell_range: str | None = None,
    ) -> str:
        """Read the content of a file in OneDrive.

        ``item_path`` is the path relative to the drive root (e.g.
        ``Documents/report.txt``). Text files are returned inline, Excel
        workbooks are read cell-by-cell, Word / PowerPoint / PDF documents
        have their text extracted, and other binary/large files point at
        ``download_microsoft_file``. To read a file in a SharePoint site, or any file you
        only have a URL for, use ``get_microsoft_file_by_url`` instead.

        For Excel workbooks use ``worksheet`` to target a specific sheet and
        ``cell_range`` to read a specific address window (e.g. ``"A1:E100"``).
        Do **not** embed worksheet names or ranges in ``item_path`` —
        patterns like ``file.xlsx!Sheet1!A1:E100`` produce malformed Graph
        URLs and will always fail; use the dedicated ``worksheet`` /
        ``cell_range`` parameters instead.
        """
        # Same sanitization contract as get_microsoft_site_file_content —
        # the two tools must behave identically (see _read_drive_item).
        worksheet = (worksheet or "").strip() or None
        cell_range = (cell_range or "").strip() or None
        if cell_range and not worksheet:
            return (
                "Error: cell_range requires the 'worksheet' parameter — pass "
                "the sheet name to read (see the file's sheet list from a "
                "previous full read, e.g. worksheet='Sheet1')."
            )
        path, extracted_ws, extracted_range = _split_workbook_path_and_range(
            item_path.strip().lstrip("/")
        )
        worksheet = worksheet or extracted_ws
        cell_range = cell_range or extracted_range
        async with _client() as client:
            meta_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/drive/root:/{_encode_drive_path(path)}",
                _PROVIDER,
                "access file",
                params={
                    "$select": "id,name,size,file,folder,webUrl,lastModifiedBy,createdBy,@microsoft.graph.downloadUrl"
                },
            )
            if err:
                return err
            meta = meta_resp.json()
            item_id = meta.get("id")
            if not item_id:
                return "Error: could not resolve the file id for that path."
            item_base = f"{_BASE_URL}/me/drive/items/{item_id}"
            file_name = meta.get("name", "")
            if worksheet and file_name.lower().endswith(_WORKBOOK_EXTS):
                return _with_drive_links(
                    await _read_workbook_worksheet(
                        ctx,
                        client,
                        item_base,
                        file_name,
                        worksheet=worksheet,
                        cell_range=cell_range,
                    ),
                    meta,
                    hint=True,
                )
            return await _read_drive_item(ctx, client, item_base, meta)

    async def get_microsoft_file_by_url(url: str) -> str:
        """Read a file from OneDrive or ANY SharePoint site by its URL.

        Accepts a SharePoint/OneDrive "Copy link" sharing URL **or** the
        plain browser address-bar URL of an Office document (Word, Excel,
        PowerPoint, PDF, text, …). The URL is resolved through the Microsoft
        Graph ``/shares`` endpoint, so this works for files in any SharePoint
        site the signed-in user can access — not just their personal
        OneDrive. Text files are returned inline, Excel workbooks are read
        cell-by-cell, Word / PowerPoint / PDF documents have their text
        extracted, and other files point at ``download_microsoft_file``.
        A folder's URL answers with the first page of its items, nothing
        downloaded; ``list_microsoft_folder_by_url`` pages through the rest.
        """
        cleaned = url.strip()
        if not re.match(r"^https?://", cleaned, re.IGNORECASE):
            return (
                "Error: provide a full http(s) URL to the SharePoint/OneDrive "
                "file (e.g. the link from the browser address bar or a "
                "'Copy link' sharing URL)."
            )
        async with _client() as client:
            target, err = await _resolve_drive_url(ctx, client, cleaned)
            if target is None:
                return err or "Error: could not resolve a file from that URL."
            if _is_folder(target.meta):
                listing = await _list_folder(ctx, client, target, cleaned)
                if not listing.startswith("Folder: "):
                    return listing
                return (
                    f"'{target.meta.get('name') or 'This item'}' is a folder, not a file, so "
                    "nothing was downloaded. Its items:\n\n" + listing
                )
            return await _read_drive_item(ctx, client, target.item_base, target.meta)

    async def list_microsoft_folder_by_url(
        url: str,
        count: int = _FOLDER_PAGE_DEFAULT,
        page_token: str | None = None,
    ) -> str:
        """List the items of a OneDrive / SharePoint folder by its URL.

        The URL is resolved as ``get_microsoft_file_by_url`` resolves it (a
        sharing link or the folder's browser URL, on any site the user can
        reach, a member or not), so a folder shared with the user is listed
        without a site id. A path appended to the URL names a folder under
        it. Each item comes with the URL the URL tools take as is; pages
        follow ``@odata.nextLink`` through ``page_token``.
        """
        cleaned = (url or "").strip()
        if not re.match(r"^https?://", cleaned, re.IGNORECASE):
            return (
                "Error: provide the full http(s) URL of the SharePoint/OneDrive "
                "folder (its 'Copy link' sharing URL or the browser address-bar URL)."
            )
        async with _client() as client:
            target, err = await _resolve_drive_url(ctx, client, cleaned)
            if target is None:
                return err or "Error: could not resolve a folder from that URL."
            if not _is_folder(target.meta):
                return (
                    f"Error: '{target.meta.get('name') or 'that item'}' is a file, not a folder. "
                    f'Read it with get_microsoft_file_by_url(url="{cleaned}").'
                )
            return await _list_folder(ctx, client, target, cleaned, count=count, page_token=page_token)

    async def search_microsoft_files(query: str, count: int = 10) -> str:
        """Search files across OneDrive AND every SharePoint site the user can access.

        Uses the Microsoft Graph unified search API (``POST /search/query``
        with ``driveItem`` entities), so results span the signed-in user's
        OneDrive *and* all SharePoint sites they have access to — not just
        their personal drive. Returns file names, sizes, modification dates,
        and web URLs. Pass a result's URL to ``get_microsoft_file_by_url``
        to read its content.
        """
        size = max(1, min(int(count) if isinstance(count, int) else 10, 25))
        payload = {
            "requests": [
                {
                    "entityTypes": ["driveItem"],
                    "query": {"queryString": query},
                    "from": 0,
                    "size": size,
                }
            ]
        }
        async with _client() as client:
            resp, err = await _throttled_request(
                client,
                "post",
                f"{_BASE_URL}/search/query",
                _PROVIDER,
                "search files",
                json=payload,
            )
            if err:
                return err
            hits: list[dict] = []
            for response in resp.json().get("value", []):
                for container in response.get("hitsContainers", []):
                    hits.extend(container.get("hits", []))
            if not hits:
                return f"No files matching '{query}'."
            lines: list[str] = []
            for hit in hits:
                res = hit.get("resource", {})
                name = res.get("name") or "Untitled"
                size_bytes = res.get("size", 0)
                modified = res.get("lastModifiedDateTime", "")
                web_url = res.get("webUrl", "")
                lines.append(
                    f"{name} ({size_bytes} bytes)\n"
                    f"  Last modified: {modified}\n"
                    f"  URL: {web_url}"
                )
            return f"Found {len(lines)} file(s):\n\n" + "\n---\n".join(lines)

    # ── File transfer tools (platform ↔ OneDrive/SharePoint/Teams) ──────

    async def download_microsoft_file(
        url: str, filename: str = ""
    ) -> ToolResult | str:
        """Download a OneDrive/SharePoint file's RAW BYTES as a platform file.

        Unlike ``get_microsoft_file_by_url`` (which extracts text for
        reading), this fetches the file byte-for-byte and persists it as a
        reusable platform file with a download card — use it to hand the
        actual document to the user or forward it to another tool.
        """
        cleaned = (url or "").strip()
        if not cleaned.lower().startswith("https://"):
            return "Error: provide the full https:// SharePoint/OneDrive file URL."

        async with _client() as client:
            item_base, meta, err = await _resolve_share_url(ctx, client, cleaned)
            if err:
                return err
            meta = meta or {}
            if _is_folder(meta):
                return (
                    f"Error: '{meta.get('name') or 'that item'}' is a folder, not a file, so there "
                    f'is nothing to download. List its items with list_microsoft_folder_by_url(url="{cleaned}") '
                    "and download a file from it by its URL."
                )
            size = meta.get("size") or 0
            if size > _MS_MAX_FILE_BYTES:
                return (
                    f"Error: file is {round(size / 1024 / 1024, 1)} MB, over the "
                    f"{_MS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
                )
            # Graph /content replies 302 to a pre-authenticated download
            # location — follow it (like the text-read path above; httpx
            # drops the bearer header on the cross-origin hop).
            content_resp, err = await _throttled_request(
                client,
                "get",
                f"{item_base}/content",
                _PROVIDER,
                "download file bytes",
                follow_redirects=True,
            )
            if err:
                return err
            content = content_resp.content

        if not content:
            return "Error: the downloaded file is empty."
        # Re-check against the actual byte length — Graph metadata can omit
        # or zero the size field, and the cap must hold regardless.
        if len(content) > _MS_MAX_FILE_BYTES:
            return (
                f"Error: file is {round(len(content) / 1024 / 1024, 1)} MB, over "
                f"the {_MS_MAX_FILE_BYTES // 1024 // 1024} MB download limit."
            )

        derived_name = (
            filename.strip() or meta.get("name") or "microsoft-download"
        )
        mime = (meta.get("file") or {}).get("mimeType") or (
            mimetypes.guess_type(derived_name)[0] or "application/octet-stream"
        )
        file_ref = None
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(derived_name, content, mime)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Microsoft file persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            file_ref = FileRef.from_bytes(
                data=content, filename=derived_name, content_type=mime
            )
        answer = (
            f"Downloaded a {round(len(content) / 1024, 1)} KB file as a reusable file. "
            f"[[FILE:{file_ref.file_id}]]\n\nForward it to another tool with "
            f"file_id={file_ref.file_id}, and include the [[FILE:...]] marker "
            "verbatim so the user gets a download card.\n\n"
            # The download still hands on the file's links.
            + untrusted_file_facts(
                source="Microsoft",
                filename=derived_name,
                content_type=mime,
                details=_drive_item_link_lines(meta),
            )
        )
        return ToolResult(tool_call_id="", content=answer, files=[file_ref])

    async def _download_hosted_content(
        message_url: str,
        tool_name: str,
        hosted_content_id: str,
        filename: str,
        file_context: FileContext | None,
    ) -> ToolResult | str:
        """Keep a Teams message's pasted image (a hosted content) as a file."""
        hosted = _url_decode((hosted_content_id or "").strip())
        async with _client() as client:
            if not hosted:
                list_resp, err = await _throttled_request(
                    client,
                    "get",
                    f"{message_url}/hostedContents",
                    _PROVIDER,
                    "list the message's pasted images",
                )
                if err:
                    return err
                ids = [
                    item.get("id")
                    for item in (list_resp.json() or {}).get("value", [])
                    if isinstance(item, dict) and item.get("id")
                ]
                if not ids:
                    return "This message holds no pasted image."
                if len(ids) > 1:
                    return (
                        f"This message holds {len(ids)} pasted images. Call {tool_name} "
                        "again with one of them as hosted_content_id:\n"
                        + "\n".join(f"- hosted_content_id: {hid}" for hid in ids)
                    )
                hosted = ids[0]
            async with _api_sem:
                download = await fetch_file(
                    client,
                    "get",
                    f"{message_url}/hostedContents/{_url_encode(hosted, safe='')}/$value",
                    provider=_PROVIDER,
                    action="download the pasted image",
                    max_bytes=_MS_MAX_FILE_BYTES,
                    follow_redirects=True,
                )
            if isinstance(download, FetchError):
                return download
            data = download.data
            mime = (
                download.content_type.split(";", 1)[0].strip() or "application/octet-stream"
            )
            message_resp, _msg_err = await _throttled_request(
                client, "get", message_url, _PROVIDER, "read the message link"
            )
            web_url = (message_resp.json() or {}).get("webUrl", "") if message_resp else ""
        if not data:
            return "Error: the pasted image is empty."
        name = filename.strip() or f"teams-image-{hosted[-8:]}"
        if "." not in name:
            name += mimetypes.guess_extension(mime) or ""
        file_ref = await keep_file(persist_file_fn, file_context, name, data, mime)
        parts = [
            f"Pasted image: {name} ({mime}, {kilobytes(len(data))}) from the Teams message.",
            kept_file_line(file_ref),
        ]
        if web_url:
            parts.append(f"Message link: {web_url}")
        if mime.startswith("image/"):
            parts.append("Its pixels are attached to this result.")
        return ToolResult(
            tool_call_id="",
            content="\n".join(parts),
            files=[file_ref] if mime.startswith("image/") else [],
        )

    async def download_microsoft_chat_message_image(
        chat_id: str,
        message_id: str,
        hosted_content_id: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Keep an image pasted in a Teams chat message as a conversation file."""
        if err := _validate_teams_chat_id(chat_id, "download_microsoft_chat_message_image"):
            return err
        if err := _validate_teams_message_id(message_id, "download_microsoft_chat_message_image"):
            return err
        return await _download_hosted_content(
            f"{_BASE_URL}/chats/{_teams_segment(chat_id)}/messages/{_teams_segment(message_id)}",
            "download_microsoft_chat_message_image",
            hosted_content_id,
            filename,
            file_context,
        )

    async def download_microsoft_channel_message_image(
        team_id: str,
        channel_id: str,
        message_id: str,
        hosted_content_id: str = "",
        parent_message_id: str = "",
        filename: str = "",
        file_context: FileContext | None = None,
    ) -> ToolResult | str:
        """Keep an image pasted in a Teams channel message (or reply) as a file."""
        tool = "download_microsoft_channel_message_image"
        message_url, _, err = _channel_message_url(team_id, channel_id, message_id, parent_message_id, tool)
        if err:
            return err
        return await _download_hosted_content(
            message_url, tool, hosted_content_id, filename, file_context
        )

    async def _upload_to_drive_path(
        client: AutoRefreshClient,
        put_url: str,
        data: bytes,
        mime: str,
    ) -> tuple[dict | None, str | None]:
        """PUT file bytes to a drive path. Returns ``(driveItem, error)``."""
        resp, err = await _throttled_request(
            client,
            "put",
            put_url,
            _PROVIDER,
            "upload file",
            headers={"Content-Type": mime or "application/octet-stream"},
            content=data,
        )
        if err:
            return None, err
        return resp.json(), None

    async def _drive_base_for(client: AutoRefreshClient, site: str) -> tuple[str | None, str | None]:
        """``(drive base URL, None)`` - the user's OneDrive, or the library of *site* given in any
        form the SharePoint tools take (``sharepoint._site_drive_base``) - or ``(None, error)``."""
        if not site:
            return f"{_BASE_URL}/me/drive", None
        drive_base, _by_address, err = await _site_drive_base(ctx, client, site)
        return drive_base, err

    async def upload_microsoft_drive_file(
        file_id: str,
        folder: str = "",
        filename: str = "",
        site_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload a platform file (attached/generated in this conversation)
        to the signed-in user's OneDrive, or to a SharePoint site's document
        library when ``site_id`` is given.

        ``folder`` is a path relative to the drive root (created implicitly
        by Graph if missing); empty uploads to the root.  Returns the file's
        web URL, usable with ``send_microsoft_chat_file`` /
        ``send_microsoft_channel_file`` or shareable directly.
        """
        resolved, err = await _resolve_platform_file(file_id, file_context)
        if err:
            return err
        data, upload_name, mime = resolved
        if filename.strip():
            upload_name = filename.strip()

        # Encode the folder (where "/" separates segments) and the filename
        # (a single segment — a "/" in the LLM/user-controlled name must not
        # inject extra path levels) separately.
        path_part = (
            f"{_encode_drive_path(folder.strip().strip('/'))}/"
            if folder.strip()
            else ""
        )
        site = (site_id or "").strip()
        async with _client() as client:
            drive_base, err = await _drive_base_for(client, site)
            if err:
                return err
            put_url = (
                f"{drive_base}/root:/"
                f"{path_part}{_encode_drive_filename(upload_name)}:/content"
            )
            item, err = await _upload_to_drive_path(client, put_url, data, mime)
            if err:
                return err
        where = "the SharePoint site's document library" if site else "OneDrive"
        # The name, and the URLs that carry it, go in the quarantine block.
        return uploaded_file_answer(
            f"Uploaded file {file_id.strip()} ({round(len(data) / 1024, 1)} KB) to "
            f"{where}{' folder ' + folder if folder.strip() else ''}.",
            target=_PROVIDER,
            filename=upload_name,
            links=[
                ("Web URL", item.get("webUrl")),
                ("Download URL", item.get("@microsoft.graph.downloadUrl")),
            ],
        )

    async def _fetch_word_document(
        client: AutoRefreshClient,
        *,
        url: str,
        item_path: str,
        site_id: str,
        verb: str,
        for_write: bool,
    ) -> tuple[_WordDocument | None, str | None]:
        """The ``.docx`` the three comment tools work on, or the error that
        refuses the call: resolve the file (a share URL, or a path with its
        site), check it is a ``.docx`` within the size limit, read its eTag
        and, for a write, that it is not checked out and who the connected
        account is (``/me``), then download it. *verb* words the format and
        size refusals (``added to`` / ``read from`` / ``removed from``); a
        write's refusals end with "Nothing was written"."""
        tail = " Nothing was written." if for_write else ""
        link = (url or "").strip()
        path = (item_path or "").strip().lstrip("/")
        site = (site_id or "").strip()
        if bool(link) == bool(path):
            return None, (
                "Error: give the document by url (its SharePoint / OneDrive link) or by "
                "item_path (with site_id for a SharePoint library), not both."
            )
        if link and not link.lower().startswith("https://"):
            return None, "Error: provide the full https:// SharePoint/OneDrive URL of the document."

        if link:
            item_base, _resolved, err = await _resolve_share_url(ctx, client, link)
            if err:
                return None, err
            located = item_base
        else:
            drive_base, err = await _drive_base_for(client, site)
            if err:
                return None, err
            located = f"{drive_base}/root:/{_encode_drive_path(path)}"
        meta_resp, err = await _throttled_request(
            client,
            "get",
            located,
            _PROVIDER,
            "access the document",
            params={"$select": _DOCUMENT_SELECT},
        )
        if err:
            return None, err
        meta = meta_resp.json()
        item_id = meta.get("id")
        if not item_id:
            return None, "Error: could not resolve the document from that location."
        if not link:
            item_base = f"{drive_base}/items/{item_id}"
        name = meta.get("name") or ""
        if not name.lower().endswith(".docx"):
            return None, (
                f"Error: '{name}' is not a .docx Word document. Comments can only be "
                f"{verb} .docx files (not .doc, .docm, PDF or other formats).{tail}"
            )
        size = meta.get("size") or 0
        if size > _MAX_DOCUMENT_BYTES:
            return None, (
                f"Error: '{name}' is {round(size / 1024 / 1024, 1)} MB; comments can only be "
                f"{verb} documents of up to {_MAX_DOCUMENT_BYTES // 1024 // 1024} MB.{tail}"
            )
        account = ""
        if for_write:
            publication = meta.get("publication") or {}
            if publication.get("level") == "checkout":
                holder = ((publication.get("checkedOutBy") or {}).get("user") or {}).get("displayName")
                return None, (
                    f"Error: '{name}' is checked out{f' by {holder}' if holder else ''}, so it "
                    f"cannot be changed until it is checked in.{tail}"
                )
            me_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "read the connected account",
                params={"$select": "displayName,mail,userPrincipalName"},
            )
            if err:
                return None, err
            me = me_resp.json()
            account = (
                str(me.get("displayName") or me.get("mail") or me.get("userPrincipalName") or "").strip()
                or "XTM One"
            )

        # Graph answers /content with a redirect to a pre-authenticated
        # location (the bearer header is dropped on the hop, as in the readers).
        content_resp, err = await _throttled_request(
            client,
            "get",
            f"{item_base}/content",
            _PROVIDER,
            "download the document",
            follow_redirects=True,
        )
        if err:
            return None, err
        data = content_resp.content or b""
        if not data:
            return None, f"Error: '{name}' is empty.{tail}"
        if len(data) > _MAX_DOCUMENT_BYTES:
            return None, (
                f"Error: '{name}' is {round(len(data) / 1024 / 1024, 1)} MB; comments can only be "
                f"{verb} documents of up to {_MAX_DOCUMENT_BYTES // 1024 // 1024} MB.{tail}"
            )
        return _WordDocument(item_base, name, meta, meta.get("eTag") or "", data, account), None

    async def _save_word_document(
        client: AutoRefreshClient,
        document: _WordDocument,
        data: bytes,
        *,
        action: str,
        doing: str,
        otherwise: str,
    ) -> tuple[dict, str | None]:
        """Save *data* as a new version of *document* with ``If-Match`` on
        the eTag read first: ``(saved driveItem, None)``, or the error that
        says nothing was written - the file changed meanwhile (412), is
        locked (423), or the account may only read it (403). *doing* words
        what the comments were being (``prepared`` / ``removed``) when the
        file changed, *otherwise* the other way out of a read-only file."""
        headers = {"Content-Type": _DOCX_MIME}
        if document.etag:
            headers["If-Match"] = document.etag
        save_resp, err = await _throttled_request(
            client,
            "put",
            f"{document.item_base}/content",
            _PROVIDER,
            action,
            headers=headers,
            content=data,
            expected_statuses={412, 423},
        )
        if err:
            status = save_resp.status_code if save_resp is not None else None
            name = document.name
            if status == 412:
                return {}, (
                    f"Error: '{name}' changed while the comments were being {doing} (a new "
                    "version was saved meanwhile), so nothing was written. Read the document "
                    "again and call the tool again."
                )
            if status == 423:
                return {}, (
                    f"Error: '{name}' is locked - open for editing in Word by someone, or "
                    "checked out - so it cannot be changed now. Nothing was written; once it "
                    "is closed, call the tool again."
                )
            if status == 403:
                return {}, (
                    f"Error: the connected account has no write access to '{name}' (read "
                    f"access only), so nothing was written. Ask its owner for edit access, or "
                    f"{otherwise}."
                )
            return {}, err
        return (save_resp.json() if save_resp.content else {}), None

    async def add_microsoft_document_comments(
        comments: list | str,
        url: str = "",
        item_path: str = "",
        site_id: str = "",
    ) -> str:
        """Add native Word comments to an existing .docx in OneDrive or SharePoint.

        Each comment is anchored on a passage of the document's text (the
        exact text, plus ``occurrence`` when it appears more than once). The
        document is downloaded, the comments are written into it as Word
        writes them (``word_comments.add_word_comments``: the text is never
        changed, an anchor that is not found is refused, never placed
        elsewhere), and the result is saved as a new version of the same
        file with ``If-Match`` on the eTag read first, so a version someone
        saved meanwhile is never overwritten. The comments are authored by
        the connected account. Every request is reported placed or refused
        with its reason; a checked-out or locked file and a file the account
        cannot write are refused with nothing written.
        """
        requests, err = _parse_comment_requests(comments)
        if err:
            return err

        async with _client() as client:
            document, err = await _fetch_word_document(
                client, url=url, item_path=item_path, site_id=site_id, verb="added to", for_write=True
            )
            if err or document is None:
                return err or "Error: could not read the document."
            name, author = document.name, document.account
            try:
                commented = await run_parse(
                    functools.partial(add_word_comments, document.data, requests, author=author)
                )
            except WordDocumentError as exc:
                return f"Error: {exc} Nothing was written."
            lines = _comment_outcome_lines(commented)
            if commented.data is None:
                return "\n".join(
                    [
                        "Error: none of the comments could be placed, so nothing was written to "
                        f"'{name}':",
                        *lines,
                    ]
                )
            saved, err = await _save_word_document(
                client,
                document,
                commented.data,
                action="save the commented document",
                doing="prepared",
                otherwise="give the review another way",
            )
            if err:
                return err

        placed, refused = commented.placed, commented.refused
        head = (
            f"Added {len(placed)} comment{'s' if len(placed) != 1 else ''} to '{name}' as "
            f"{author}, saved as a new version of the document; its text is unchanged."
        )
        if refused:
            head += f" {len(refused)} comment{'s were' if len(refused) != 1 else ' was'} refused and left out."
        links = _drive_item_link_lines(saved) or _drive_item_link_lines(document.meta)
        return "\n".join([head, *lines, *([""] if links else []), *links])

    async def list_microsoft_document_comments(
        url: str = "",
        item_path: str = "",
        site_id: str = "",
    ) -> str:
        """List the native Word comments of a .docx in OneDrive or SharePoint.

        One line per comment: its id, author, date, the passage it is on, its
        text, and whether it answers another comment or is resolved - read
        from the document itself (``word_comments.list_word_comments``), so
        the ids are the ones ``delete_microsoft_document_comments`` takes.
        """
        async with _client() as client:
            document, err = await _fetch_word_document(
                client, url=url, item_path=item_path, site_id=site_id, verb="read from", for_write=False
            )
            if err or document is None:
                return err or "Error: could not read the document."
        try:
            comments = await run_parse(functools.partial(list_word_comments, document.data))
        except WordDocumentError as exc:
            return f"Error: {exc}"
        links = _drive_item_link_lines(document.meta)
        if not comments:
            return "\n".join([f"'{document.name}' has no comments.", *links])
        head = f"{len(comments)} comment{'s' if len(comments) != 1 else ''} in '{document.name}':"
        return "\n".join([head, *(_comment_line(comment) for comment in comments), *([""] if links else []), *links])

    async def delete_microsoft_document_comments(
        comment_ids: list | str,
        url: str = "",
        item_path: str = "",
        site_id: str = "",
        include_others: bool = False,
    ) -> str:
        """Delete native Word comments from a .docx in OneDrive or SharePoint, in place.

        ``comment_ids`` are the ids ``list_microsoft_document_comments``
        shows, or ``all_mine`` for every comment by the connected account. By
        default only the connected account's own comments can be removed: a
        comment by anyone else is refused unless ``include_others`` is true.
        Authorship is the ``w:author`` name, free text in Word, matched by
        :func:`_same_author`: a comment by someone else under the account's
        display name is matched too, as the tool description says. Removing
        a comment removes its replies with it. The document is
        downloaded, the comments taken out as Word takes them out
        (``word_comments.remove_word_comments``: the text is never changed),
        and the result saved as a new version with ``If-Match`` on the eTag
        read first. Every id is reported removed or refused with the reason;
        a checked-out or locked file and a file the account cannot write are
        refused with nothing written.
        """
        ids, all_mine, err = _parse_comment_ids(comment_ids)
        if err:
            return err
        others_too = _truthy(include_others)

        async with _client() as client:
            document, err = await _fetch_word_document(
                client, url=url, item_path=item_path, site_id=site_id, verb="removed from", for_write=True
            )
            if err or document is None:
                return err or "Error: could not read the document."
            name, account = document.name, document.account
            try:
                existing = await run_parse(functools.partial(list_word_comments, document.data))
            except WordDocumentError as exc:
                return f"Error: {exc} Nothing was written."
            by_id = {comment.comment_id: comment for comment in existing}

            if all_mine:
                ids = [c.comment_id for c in existing if _same_author(c.author, account)]
                if not ids:
                    if not existing:
                        return f"Error: '{name}' has no comments, so nothing was written."
                    authors = Counter(c.author or "an unnamed author" for c in existing)
                    listed = ", ".join(f"{author} ({count})" for author, count in authors.most_common())
                    return (
                        f"Error: '{name}' has no comment by the connected account ({account}), so "
                        f"nothing was written. Its {len(existing)} comment"
                        f"{'s are' if len(existing) != 1 else ' is'} by {listed}; pass their ids with "
                        "include_others=true to remove other people's comments."
                    )

            refusals: dict[int, str] = {}
            eligible: list[int] = []
            for comment_id in ids:
                comment = by_id.get(comment_id)
                if comment is None:
                    refusals[comment_id] = (
                        f"no comment with id {comment_id} in the document "
                        "(list_microsoft_document_comments shows the ids)"
                    )
                elif not others_too and not _same_author(comment.author, account):
                    refusals[comment_id] = (
                        f"not by the connected account ({account}); pass include_others=true to "
                        "remove other people's comments"
                    )
                else:
                    eligible.append(comment_id)
            if not eligible:
                return "\n".join(
                    [
                        f"Error: none of the comments could be removed from '{name}', so nothing was written:",
                        *(f"- #{cid}{_comment_label(by_id.get(cid))}: refused, {why}" for cid, why in refusals.items()),
                    ]
                )
            try:
                removed = await run_parse(functools.partial(remove_word_comments, document.data, eligible))
            except WordDocumentError as exc:
                return f"Error: {exc} Nothing was written."
            # An eligible id can still be refused: a marker of its thread sits
            # in a part the module does not edit (footnotes, endnotes).
            refusals.update({outcome.comment_id: outcome.detail for outcome in removed.refused})
            if removed.data is None:
                return "\n".join(
                    [
                        f"Error: none of the comments could be removed from '{name}', so nothing was written:",
                        *(
                            f"- #{cid}{_comment_label(by_id.get(cid))}: refused, {refusals[cid]}"
                            for cid in ids
                            if cid in refusals
                        ),
                    ]
                )
            saved, err = await _save_word_document(
                client,
                document,
                removed.data,
                action="save the document without the comments",
                doing="removed",
                otherwise="ask them to remove the comments in Word",
            )
            if err:
                return err

        outcomes = {outcome.comment_id: outcome for outcome in removed.outcomes}
        gone: set[int] = set()
        lines: list[str] = []
        for comment_id in ids:
            if comment_id in refusals:
                lines.append(f"- #{comment_id}{_comment_label(by_id.get(comment_id))}: refused, {refusals[comment_id]}")
                continue
            outcome = outcomes[comment_id]
            gone.add(comment_id)
            gone.update(outcome.replies)
            line = f"- #{comment_id}{_comment_label(by_id.get(comment_id))}: removed"
            if outcome.replies:
                replies = ", ".join(
                    f"#{reply}{_comment_label(by_id.get(reply), with_anchor=False)}" for reply in outcome.replies
                )
                line += f", with its repl{'y' if len(outcome.replies) == 1 else 'ies'} {replies}"
            lines.append(line)
        asked = len(removed.removed)
        replies_gone = len(gone) - asked
        head = f"Removed {len(gone)} comment{'s' if len(gone) != 1 else ''}"
        if replies_gone:
            head += f" ({asked} asked for, {replies_gone} repl{'y' if replies_gone == 1 else 'ies'} with them)"
        head += f" from '{name}', saved as a new version of the document."
        if refusals:
            head += f" {len(refusals)} id{'s were' if len(refusals) != 1 else ' was'} refused and left as is."
        links = _drive_item_link_lines(saved) or _drive_item_link_lines(document.meta)
        return "\n".join([head, *lines, *([""] if links else []), *links, "The document's text is unchanged."])

    async def _attach_drive_item_to_message(
        client: AutoRefreshClient,
        post_url: str,
        item: dict,
        message: str,
        action: str,
    ) -> tuple[dict | None, str | None]:
        """POST a chatMessage with a driveItem ``reference`` attachment.

        Per the Graph contract: attachment ``id`` = the GUID inside the
        driveItem's eTag, ``contentUrl`` = the item's webDavUrl (fallback
        webUrl), and the HTML body must carry a matching ``<attachment>``
        tag so the file card renders.  A driveItem without an eTag GUID or
        a usable content URL cannot produce a rendering file card, so the
        send fails with a clear error (the file itself is already uploaded
        and stays available in OneDrive/SharePoint) instead of silently
        posting a broken payload.
        """
        etag = item.get("eTag") or ""
        guid_match = re.search(r"\{([0-9a-fA-F-]{36})\}", etag)
        if not guid_match:
            # Its web URL carries its stored name: in the quarantine block.
            return None, uploaded_file_answer(
                "Error: the uploaded file's metadata has no eTag GUID, so a "
                "native Teams file card cannot be built. The file was "
                "uploaded successfully — share its Web URL in a plain message instead.",
                target=_PROVIDER,
                filename=item.get("name") or "",
                links=[("Web URL", item.get("webUrl"))],
            )
        attach_id = guid_match.group(1)
        content_url = item.get("webDavUrl") or item.get("webUrl") or ""
        if not content_url:
            return None, (
                "Error: the uploaded file's metadata has no webDavUrl/webUrl, "
                "so a native Teams file card cannot be built. The file was "
                "uploaded successfully — locate it in OneDrive/SharePoint and "
                "share its link in a plain message instead."
            )
        name = item.get("name") or "file"
        # The text goes through the plain message tools' own converter
        # (Markdown to Teams HTML), so a file message keeps its paragraphs,
        # lists, bold, links and tables on the first send; the file card's
        # <attachment> tag follows it in the same body, one Graph call.
        text_html = build_graph_chat_body(message)["content"] if (message or "").strip() else ""
        body_html = text_html + f'<attachment id="{attach_id}"></attachment>'
        payload = {
            "body": {"contentType": "html", "content": body_html},
            "attachments": [
                {
                    "id": attach_id,
                    "contentType": "reference",
                    "contentUrl": content_url,
                    "name": name,
                }
            ],
        }
        resp, err = await _throttled_request(
            client, "post", post_url, _PROVIDER, action, json=payload
        )
        if err:
            return None, err
        return resp.json(), None

    async def _share_with_chat_members(
        client: AutoRefreshClient, chat_id: str, item: dict
    ) -> tuple[list[str], str | None]:
        """Grant every member of the chat read access to the uploaded drive item.

        A file Teams uploads itself is shared with the chat; a file uploaded
        through Graph is not, so the members saw a card they could not open.
        The members are read from the chat, invited read-only without an
        email (``sendInvitation: false``), and the item's permissions read back:
        a member left without access is an error, before any card is posted.
        Returns the labels of the members who can open the file.
        """
        item_id = str(item.get("id") or "")
        if not item_id:
            return [], "Error: the uploaded file has no drive item id, so it could not be shared with the chat."
        me_resp, err = await _throttled_request(
            client, "get", f"{_BASE_URL}/me", _PROVIDER, "read the sending account", params={"$select": "id"}
        )
        if err:
            return [], err
        members, err = await ctx.collect_all(
            client, f"{_BASE_URL}/chats/{_teams_segment(chat_id)}/members", "list chat members"
        )
        if err:
            return [], (
                f"{err} The file was uploaded ({item.get('webUrl', 'no URL')}) but not shared and not "
                "posted: the chat's members could not be read to grant them access."
            )
        recipients = _chat_share_recipients(members, str(me_resp.json().get("id") or ""))
        if not recipients:
            return [], None
        item_url = f"{_BASE_URL}/me/drive/items/{item_id}"
        _resp, err = await _throttled_request(
            client,
            "post",
            f"{item_url}/invite",
            _PROVIDER,
            "share the file with the chat members",
            json={
                "recipients": [recipient.drive_recipient() for recipient in recipients],
                "roles": ["read"],
                "requireSignIn": True,
                "sendInvitation": False,
            },
        )
        if err:
            return [], (
                f"{err} The file was uploaded ({item.get('webUrl', 'no URL')}) but could not be shared "
                "with the chat's members, so no message was posted: they could not have opened it."
            )
        permissions, err = await ctx.collect_all(client, f"{item_url}/permissions", "read the file's permissions")
        if err:
            return [], f"{err} The file was shared but its permissions could not be read back; no message was posted."
        missing = _ungranted_recipients(recipients, permissions)
        if missing:
            return [], (
                "Error: the file was uploaded and shared, but Microsoft lists no access for "
                f"{', '.join(missing)}, so no message was posted: they could not have opened it. "
                f"File: {item.get('webUrl', 'no URL')}."
            )
        return [recipient.label for recipient in recipients], None

    async def send_microsoft_chat_file(
        chat_id: str,
        file_id: str,
        message: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Send a REAL file into a Teams chat (1:1 or group) as the user.

        Uploads the platform file to the signed-in user's OneDrive
        ("Microsoft Teams Chat Files" folder — where Teams keeps chat
        files), then posts a chat message with the file attached as a
        native Teams file card.
        """
        if err := _validate_teams_chat_id(chat_id, "send_microsoft_chat_file"):
            return err
        resolved, err = await _resolve_platform_file(file_id, file_context)
        if err:
            return err
        data, upload_name, mime = resolved

        put_url = (
            f"{_BASE_URL}/me/drive/root:/Microsoft Teams Chat Files/"
            f"{_encode_drive_filename(upload_name)}:/content"
        )
        async with _client() as client:
            item, err = await _upload_to_drive_path(client, put_url, data, mime)
            if err:
                return err
            # webDavUrl is not returned by the upload response — fetch it.
            meta_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/me/drive/items/{item.get('id')}"
                "?$select=id,name,eTag,webUrl,webDavUrl,@microsoft.graph.downloadUrl",
                _PROVIDER,
                "read uploaded file metadata",
            )
            if not err:
                item = meta_resp.json()
            shared_with, err = await _share_with_chat_members(client, chat_id, item)
            if err:
                await ctx.log_account_send(
                    "microsoft_teams",
                    "send_microsoft_chat_file",
                    conversation_id=chat_id,
                    text=_file_message_text(message, upload_name),
                    message_id=None,
                    error=err,
                )
                return err
            msg, err = await _attach_drive_item_to_message(
                client,
                f"{_BASE_URL}/me/chats/{_teams_segment(chat_id)}/messages",
                item,
                message,
                "send chat file",
            )
            # A file message is a message sent through an agent account too.
            await ctx.log_account_send(
                "microsoft_teams",
                "send_microsoft_chat_file",
                conversation_id=chat_id,
                text=_file_message_text(message, upload_name),
                message_id=(msg or {}).get("id") or None,
                error=err or None,
            )
            if err:
                return err
        location = "Stored in the sending account's OneDrive, folder 'Microsoft Teams Chat Files'."
        access = (
            f"Read access granted to the chat's {len(shared_with)} other member(s) and checked: "
            f"{', '.join(shared_with)}."
            if shared_with
            else "The chat has no member besides the sending account, so the file was not shared."
        )
        return uploaded_file_answer(
            "\n".join(
                [
                    f"File {file_id.strip()} ({round(len(data) / 1024, 1)} KB) sent to the "
                    f"chat. Message ID: {msg.get('id', '')}.",
                    location,
                    access,
                ]
            ),
            target=_PROVIDER,
            filename=item.get("name") or upload_name,
            details=_drive_item_link_lines(item),
        )

    async def send_microsoft_channel_file(
        team_id: str,
        channel_id: str,
        file_id: str,
        message: str = "",
        reply_to_message_id: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Send a REAL file into a Teams channel as the user.

        Uploads the platform file into the channel's Files folder
        (SharePoint), then posts a channel message with the file attached
        as a native Teams file card - a new post, or a reply in the thread
        of ``reply_to_message_id``. Answers with the link to the message
        and the file's links.
        """
        if err := _validate_teams_channel_ids(team_id, channel_id, "send_microsoft_channel_file"):
            return err
        reply_to = (reply_to_message_id or "").strip()
        if reply_to and (
            err := _validate_teams_message_id(reply_to, "send_microsoft_channel_file")
        ):
            return err
        resolved, err = await _resolve_platform_file(file_id, file_context)
        if err:
            return err
        data, upload_name, mime = resolved

        async with _client() as client:
            folder_resp, err = await _throttled_request(
                client,
                "get",
                f"{_channel_url(team_id, channel_id)}/filesFolder",
                _PROVIDER,
                "resolve channel files folder",
            )
            if err:
                return err
            folder = folder_resp.json()
            drive_id = (folder.get("parentReference") or {}).get("driveId")
            folder_id = folder.get("id")
            if not drive_id or not folder_id:
                return "Error: could not resolve the channel's SharePoint folder."

            put_url = (
                f"{_BASE_URL}/drives/{drive_id}/items/{folder_id}:"
                f"/{_encode_drive_filename(upload_name)}:/content"
            )
            item, err = await _upload_to_drive_path(client, put_url, data, mime)
            if err:
                return err
            meta_resp, err = await _throttled_request(
                client,
                "get",
                f"{_BASE_URL}/drives/{drive_id}/items/{item.get('id')}"
                "?$select=id,name,eTag,webUrl,webDavUrl,@microsoft.graph.downloadUrl",
                _PROVIDER,
                "read uploaded file metadata",
            )
            if not err:
                item = meta_resp.json()
            post_url = f"{_channel_url(team_id, channel_id)}/messages"
            if reply_to:
                post_url += f"/{_teams_segment(reply_to)}/replies"
            msg, err = await _attach_drive_item_to_message(
                client,
                post_url,
                item,
                message,
                "send channel file",
            )
            await ctx.log_account_send(
                "microsoft_teams",
                "send_microsoft_channel_file",
                conversation_id=channel_id,
                conversation_type="channel",
                text=_file_message_text(message, upload_name),
                message_id=(msg or {}).get("id") or None,
                error=err or None,
                thread_route=_thread_route(team_id, channel_id, reply_to or (msg or {}).get("id") or ""),
            )
            if err:
                return err
        await record_teams_channel_post(team_id, channel_id, msg)
        answer = (
            f"File {file_id.strip()} ({round(len(data) / 1024, 1)} KB) uploaded to "
            f"the channel's Files folder and posted. Message ID: {msg.get('id', '')}."
        )
        message_link = [f"Message link: {msg['webUrl']}"] if msg.get("webUrl") else []
        return uploaded_file_answer(
            "\n".join([answer, *message_link]),
            target=_PROVIDER,
            filename=item.get("name") or upload_name,
            details=_drive_item_link_lines(item),
        )

    executor.register("list_microsoft_drive_files", list_microsoft_drive_files)
    executor.register("get_microsoft_file_content", get_microsoft_file_content)
    executor.register("get_microsoft_file_by_url", get_microsoft_file_by_url)
    executor.register("list_microsoft_folder_by_url", list_microsoft_folder_by_url)
    executor.register("search_microsoft_files", search_microsoft_files)
    executor.register("download_microsoft_file", download_microsoft_file)
    executor.register("download_microsoft_chat_message_image", download_microsoft_chat_message_image)
    executor.register("download_microsoft_channel_message_image", download_microsoft_channel_message_image)
    executor.register("upload_microsoft_drive_file", upload_microsoft_drive_file)
    executor.register("add_microsoft_document_comments", add_microsoft_document_comments)
    executor.register("list_microsoft_document_comments", list_microsoft_document_comments)
    executor.register("delete_microsoft_document_comments", delete_microsoft_document_comments)
    executor.register("send_microsoft_chat_file", send_microsoft_chat_file)
    executor.register("send_microsoft_channel_file", send_microsoft_channel_file)

    return [
        # ── OneDrive / Files ──
        ToolDefinition(
            name="list_microsoft_drive_files",
            description="List files in OneDrive or a specific folder.",
            input_schema={
                "type": "object",
                "properties": {
                    "folder": {
                        "type": "string",
                        "description": "Folder path (default 'root')",
                        "default": "root",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max files",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_file_content",
            description=(
                "Read the content of a file in OneDrive. Text files (txt, csv, md, "
                "json, xml, html) are returned inline; Excel workbooks (.xlsx/.xlsm) "
                "are read cell-by-cell; Word, PowerPoint and PDF documents have their "
                "text extracted; other binary files and text files over 500KB point "
                "at download_microsoft_file. For Excel workbooks, use 'worksheet' to "
                "target a specific sheet and 'cell_range' to read a specific address "
                "window (e.g. 'A1:E100'); do NOT embed worksheet names or cell "
                "ranges in item_path. To read a file in a SharePoint site, or any "
                "file you only have a URL for, use get_microsoft_file_by_url instead."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item_path": {
                        "type": "string",
                        "description": (
                            "File path relative to OneDrive root (e.g. "
                            "'Documents/report.txt', 'Notes/meeting.md'). Do NOT "
                            "append worksheet names or cell ranges to this path; "
                            "use the 'worksheet' and 'cell_range' parameters."
                        ),
                    },
                    "worksheet": {
                        "type": "string",
                        "description": (
                            "Optional. For Excel workbooks: name of the worksheet to "
                            "read (e.g. 'Sheet1', 'Feuil1'). If omitted all sheets "
                            "are returned."
                        ),
                    },
                    "cell_range": {
                        "type": "string",
                        "description": (
                            "Optional. For Excel workbooks: cell address range to "
                            "read (e.g. 'A1:E100'). Requires 'worksheet' to be set. "
                            "Must be a single cell or a bounded range like "
                            "'A61:E180' — no sheet-name prefix, no column/row "
                            "spans like 'A:E'."
                        ),
                    },
                },
                "required": ["item_path"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="get_microsoft_file_by_url",
            description="Read a file from OneDrive or ANY SharePoint site by its URL. Accepts a 'Copy link' sharing URL or the plain browser address-bar URL of an Office document (Word, Excel, PowerPoint, PDF, text). Resolves the URL via Microsoft Graph /shares, so it works for files in any SharePoint site the user can access — not just personal OneDrive. Text files are returned inline, Excel workbooks (.xlsx/.xlsm) are read cell-by-cell, Word / PowerPoint / PDF documents have their text extracted, other files point at download_microsoft_file. Use this when the user pastes a SharePoint/OneDrive link to a file; for a link to a folder use list_microsoft_folder_by_url (given a folder, this tool lists its first items and downloads nothing).",
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full http(s) URL of the SharePoint/OneDrive file (sharing link or the document's browser URL)",
                    }
                },
                "required": ["url"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="list_microsoft_folder_by_url",
            description=(
                "List the files and subfolders of a OneDrive / SharePoint folder by its URL: a "
                "'Copy link' sharing link or the folder's browser URL. Use it FIRST whenever you "
                "hold a link to a folder, including a folder shared with the user on a site "
                "list_microsoft_sharepoint_sites does not return: no site id is needed. Each item "
                "shows its kind ([Folder] / [File]), size, last modified date, Web URL and a URL "
                "to pass as is to get_microsoft_file_by_url or download_microsoft_file (a file) or "
                "back to this tool (a subfolder). A subfolder is also reached by appending its "
                "path to the folder's URL (the folder's link followed by '/Contracts'). When more "
                "items exist, the answer gives the page_token of the next page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": (
                            "Full http(s) URL of the folder: its sharing link, its browser URL, or an "
                            "item URL from a previous listing (a path may be appended to name a subfolder)"
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": f"Max items per page (default {_FOLDER_PAGE_DEFAULT}, max {_FOLDER_PAGE_MAX})",
                        "default": _FOLDER_PAGE_DEFAULT,
                    },
                    "page_token": {
                        "type": "string",
                        "description": "The page_token a previous listing of the same folder gave for its next page",
                    },
                },
                "required": ["url"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="download_microsoft_file",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Download a OneDrive/SharePoint file's RAW BYTES as a reusable "
                "platform file with a download card ([[FILE:id]]). Unlike "
                "get_microsoft_file_by_url (which extracts text for reading), "
                "this preserves the actual document byte-for-byte — use it to "
                "give the user the file itself or forward it to another tool "
                "via its file_id. Works with any SharePoint/OneDrive URL the "
                "user can access (message attachment URLs, 'Copy link' URLs, "
                "browser URLs). The answer also carries the file's web URL and "
                "direct download URL."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Full https URL of the SharePoint/OneDrive file",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                },
                "required": ["url"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="download_microsoft_chat_message_image",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Save an image pasted in a Teams chat message as a reusable file of "
                "this conversation ([[FILE:id]] + file_id) and see it. The chat message "
                "listings name each pasted image with the ids to pass. Omit "
                "hosted_content_id to take the message's only image, or to list them "
                "when it holds several."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "The chat ID"},
                    "message_id": {"type": "string", "description": "The chat message ID"},
                    "hosted_content_id": {
                        "type": "string",
                        "default": "",
                        "description": "The pasted image's hosted_content_id (from the message listing)",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                },
                "required": ["chat_id", "message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="download_microsoft_channel_message_image",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Save an image pasted in a Teams channel message or reply as a reusable "
                "file of this conversation ([[FILE:id]] + file_id) and see it. "
                "list_microsoft_channel_messages names each pasted image with the ids "
                "to pass (a reply also needs parent_message_id). Omit hosted_content_id "
                "to take the message's only image, or to list them when it holds several."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "The team ID"},
                    "channel_id": {"type": "string", "description": "The channel ID"},
                    "message_id": {
                        "type": "string",
                        "description": "The message ID (the reply's ID for a reply)",
                    },
                    "hosted_content_id": {
                        "type": "string",
                        "default": "",
                        "description": "The pasted image's hosted_content_id (from the message listing)",
                    },
                    "parent_message_id": {
                        "type": "string",
                        "default": "",
                        "description": "For a reply: the ID of the thread's root message",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                },
                "required": ["team_id", "channel_id", "message_id"],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="upload_microsoft_drive_file",
            description=(
                "Upload a REAL file (attached to or generated in this "
                "conversation — pass its file_id) to the signed-in user's "
                "OneDrive, or to a SharePoint site's document library when "
                "site_id is given. Returns the file's web URL. To put a file "
                "directly into a Teams conversation, prefer "
                "send_microsoft_chat_file / send_microsoft_channel_file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_id": {
                        "type": "string",
                        "description": "Platform file id (from an attached or generated file)",
                    },
                    "folder": {
                        "type": "string",
                        "default": "",
                        "description": "Folder path relative to the drive or library root (empty = root)",
                    },
                    "filename": {
                        "type": "string",
                        "default": "",
                        "description": "Optional filename override",
                    },
                    "site_id": {
                        "type": "string",
                        "default": "",
                        "description": "Optional SharePoint site - its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL ('https://hostname/sites/name'): upload into that site's default document library instead of OneDrive",
                    },
                },
                "required": ["file_id"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="add_microsoft_document_comments",
            description=(
                "Add native Word review comments to an existing .docx in OneDrive or "
                "SharePoint, in place, as the connected account: each comment is anchored "
                "on a passage of the document's text and shows on that text in Word "
                "desktop and Word for the web. Copy each anchor verbatim from "
                "get_microsoft_file_by_url / get_microsoft_file_content / "
                "get_microsoft_site_file_content (one paragraph, exact spelling, quotes "
                "and spacing); when the passage appears more than once, pass its "
                "occurrence (1-based, counted through the document). The document's text "
                "is never changed; the file gets one new version. Every comment is "
                "reported placed (with its context) or refused with the reason: an anchor "
                "that is not found (never placed elsewhere; the exact text to quote is "
                "suggested when the quote drifted), an ambiguous anchor, a document that "
                "is checked out or locked, no write access, or a document changed "
                "meanwhile. Give the file by url, or by item_path (plus site_id for a "
                "SharePoint library). To check or correct what was posted, "
                "list_microsoft_document_comments shows every comment of the document "
                "with its id and delete_microsoft_document_comments removes the ones to "
                "take back."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comments": {
                        "type": "array",
                        "description": f"The comments to add, in order (at most {MAX_COMMENTS})",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "anchor": {
                                    "type": "string",
                                    "description": "Exact text of the document the comment is on, within one paragraph",
                                },
                                "comment": {
                                    "type": "string",
                                    "description": "The comment's text (plain text; a line break starts a new paragraph)",
                                },
                                "occurrence": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": "Which occurrence of the anchor, 1-based through the document; required when it appears more than once",
                                },
                            },
                            "required": ["anchor", "comment"],
                        },
                    },
                    "url": {
                        "type": "string",
                        "default": "",
                        "description": "Full https URL of the Word document (sharing link or browser URL)",
                    },
                    "item_path": {
                        "type": "string",
                        "default": "",
                        "description": "Instead of url: the document's path relative to the OneDrive root, or to the SharePoint library root with site_id",
                    },
                    "site_id": {
                        "type": "string",
                        "default": "",
                        "description": "With item_path: the SharePoint site whose document library holds the file - its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL",
                    },
                },
                "required": ["comments"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="list_microsoft_document_comments",
            description=(
                "List the native Word comments of a .docx in OneDrive or SharePoint: one "
                "line per comment with its id, author, date, the passage it is on (first 80 "
                "characters), its text (first 200 characters), and 'reply to #N' or "
                "'resolved' when that applies. The ids are the ones "
                "delete_microsoft_document_comments takes; use it to check what "
                "add_microsoft_document_comments posted, or to see a reviewer's comments. "
                "Reads the document, changes nothing. Give the file by url, or by item_path "
                "(plus site_id for a SharePoint library)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "default": "",
                        "description": "Full https URL of the Word document (sharing link or browser URL)",
                    },
                    "item_path": {
                        "type": "string",
                        "default": "",
                        "description": "Instead of url: the document's path relative to the OneDrive root, or to the SharePoint library root with site_id",
                    },
                    "site_id": {
                        "type": "string",
                        "default": "",
                        "description": "With item_path: the SharePoint site whose document library holds the file - its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL",
                    },
                },
                "required": [],
            },
            source="integration:microsoft",
            priority=1,
        ),
        ToolDefinition(
            name="delete_microsoft_document_comments",
            description=(
                "Delete native Word comments from a .docx in OneDrive or SharePoint, in "
                "place, by their ids (as list_microsoft_document_comments shows them) or "
                f"every comment by the connected account ('{ALL_MINE}'). SAFETY: by default "
                "only comments authored by the connected account can be deleted; a comment "
                "by anyone else is refused unless include_others is true. Authorship is "
                "the author name Word stores on each comment, which is free text: a "
                "comment by someone else under the connected account's display name counts "
                "as the account's own, for the default and for all_mine. Deleting a "
                "comment deletes its replies with it. The document's text is never "
                "changed; the file gets one new version, saved only if nobody saved another "
                "version meanwhile. Every id is reported removed (with its author, its "
                "passage and the replies removed with it) or refused with the reason; a "
                "checked-out or locked file and a file the account cannot write are refused "
                "with nothing written. Give the file by url, or by item_path (plus site_id "
                "for a SharePoint library)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_ids": {
                        "type": "string",
                        "description": (
                            "The ids of the comments to remove, separated by commas (e.g. "
                            f"'3, 7, 12', from list_microsoft_document_comments), or '{ALL_MINE}' "
                            "to remove every comment by the connected account"
                        ),
                    },
                    "url": {
                        "type": "string",
                        "default": "",
                        "description": "Full https URL of the Word document (sharing link or browser URL)",
                    },
                    "item_path": {
                        "type": "string",
                        "default": "",
                        "description": "Instead of url: the document's path relative to the OneDrive root, or to the SharePoint library root with site_id",
                    },
                    "site_id": {
                        "type": "string",
                        "default": "",
                        "description": "With item_path: the SharePoint site whose document library holds the file - its ID from list_microsoft_sharepoint_sites, the path form 'hostname:/sites/name', or its URL",
                    },
                    "include_others": {
                        "type": "boolean",
                        "default": False,
                        "description": "Also remove comments authored by people other than the connected account (refused by default)",
                    },
                },
                "required": ["comment_ids"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="send_microsoft_chat_file",
            description=(
                "Send a REAL file into a Microsoft Teams chat (1:1 or group) as "
                "the signed-in user — the file is uploaded to their OneDrive "
                "('Microsoft Teams Chat Files'), shared read-only with every "
                "other member of the chat (no email sent; the grant is read "
                "back, and a member left without access is an error with no "
                "message posted) and attached to a chat message as a native "
                "Teams file card. The answer names where the file lives and "
                "who can open it. Pass the file_id of a file "
                "attached to or generated in this conversation. Use list_chats "
                "to find the chat_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "chat_id": {
                        "type": "string",
                        "description": "The chat ID (from list_chats)",
                    },
                    "file_id": {
                        "type": "string",
                        "description": "Platform file id (from an attached or generated file)",
                    },
                    "message": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message (Markdown) sent with the file",
                    },
                },
                "required": ["chat_id", "file_id"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="send_microsoft_channel_file",
            description=(
                "Send a REAL file into a Microsoft Teams channel as the "
                "signed-in user — the file is uploaded to the channel's Files "
                "folder (SharePoint) and attached to a channel message as a "
                "native Teams file card, as a new post or as a reply in a "
                "thread (reply_to_message_id). Pass the file_id of a file "
                "attached to or generated in this conversation (an exported "
                "deck, a report). Returns the link to the message and the "
                "file's SharePoint links."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "Team ID"},
                    "channel_id": {"type": "string", "description": "Channel ID"},
                    "file_id": {
                        "type": "string",
                        "description": "Platform file id (from an attached or generated file)",
                    },
                    "message": {
                        "type": "string",
                        "default": "",
                        "description": "Optional message (Markdown) sent with the file",
                    },
                    "reply_to_message_id": {
                        "type": "string",
                        "default": "",
                        "description": "Optional ID of the channel message to reply to (the file is posted in its thread)",
                    },
                },
                "required": ["team_id", "channel_id", "file_id"],
            },
            source="integration:microsoft",
            priority=2,
        ),
        ToolDefinition(
            name="search_microsoft_files",
            description="Search for files across OneDrive AND every SharePoint site the user can access (Microsoft Graph unified search). Returns file names, sizes, modification dates, and web URLs. Pass a result's URL to get_microsoft_file_by_url to read its content. The search index lags behind newly shared files: to see what a folder you hold a link to contains, use list_microsoft_folder_by_url instead.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (file name or content keyword)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (default 10, max 25)",
                        "default": 10,
                    },
                },
                "required": ["query"],
            },
            source="integration:microsoft",
            priority=1,
        ),
    ]
