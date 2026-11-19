"""Notion comment tools: list + add."""

from __future__ import annotations

import asyncio
from posixpath import basename
from urllib.parse import unquote, urlparse

from app.integrations.http import AutoRefreshClient
from app.tools.base import ToolDefinition

from ._common import (
    _BASE_URL,
    _NOTION_RICH_TEXT_LIMIT,
    _PROVIDER,
    NotionContext,
    _extract_notion_user_name,
    _format_comment_scope_error,
    _is_unnamed_user,
    _person_label,
    _rich_text_to_plain,
    format_entity_list,
    logger,
)

# Distinct comment authors looked up per call: a comment's ``created_by`` is
# only ever the author's id, whatever the integration may read.
_MAX_AUTHOR_LOOKUPS = 25


def _attachment_lines(comment: dict) -> list[str]:
    """One line per file attached to a comment: category, name, signed URL, expiry.

    Notion returns ``attachments`` as ``{category, file: {url, expiry_time}}``;
    the URL is signed and short-lived, so the line names the tool that keeps
    the bytes.
    """
    lines: list[str] = []
    for attachment in comment.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        file_obj = attachment.get("file") if isinstance(attachment.get("file"), dict) else {}
        url = str(file_obj.get("url") or "")
        if not url:
            continue
        category = str(attachment.get("category") or "file")
        name = unquote(basename(urlparse(url).path)) or "(unnamed)"
        expiry = str(file_obj.get("expiry_time") or "")
        line = f"  Attachment ({category}): {name}"
        if expiry:
            line += f", URL expires {expiry}"
        lines.append(line)
        lines.append(f"    file_url: {url}")
    if lines:
        lines.append("    Keep the bytes with download_notion_file(file_url=...) before the URL expires.")
    return lines


def register(ctx: NotionContext, executor) -> list[ToolDefinition]:
    _client = ctx.client
    _throttled_request_with_error_metadata = ctx.request_meta

    async def _resolve_authors(
        client: AutoRefreshClient, comments: list[dict]
    ) -> tuple[dict[str, dict], bool]:
        """The authors Notion names, by id, and whether it refused to name any.

        A 403 means the integration lacks the "Read user information"
        capability, so no lookup can succeed and the rest are not attempted.
        """
        author_ids: list[str] = []
        for comment in comments:
            author = comment.get("created_by")
            if _is_unnamed_user(author) and author["id"] not in author_ids:
                author_ids.append(author["id"])
        author_ids = author_ids[:_MAX_AUTHOR_LOOKUPS]
        if not author_ids:
            return {}, False

        async def lookup(user_id: str) -> tuple[str, object, int | None]:
            resp, _err, status_code, _code = await _throttled_request_with_error_metadata(
                client,
                "get",
                f"{_BASE_URL}/users/{user_id}",
                _PROVIDER,
                "get comment author",
                report_stale=False,
            )
            if resp is None:
                return user_id, None, status_code
            try:
                return user_id, resp.json(), status_code
            except ValueError:
                return user_id, None, status_code

        first = await lookup(author_ids[0])
        if first[2] == 403:
            return {}, True
        results = [first, *await asyncio.gather(*(lookup(uid) for uid in author_ids[1:]))]
        resolved = {
            uid: user
            for uid, user, _status in results
            if isinstance(user, dict) and user.get("name")
        }
        return resolved, any(status == 403 for _uid, _user, status in results)

    async def list_notion_comments(page_id: str) -> str:
        """Read all comments on a Notion page or block."""
        async with _client() as client:
            all_comments: list[dict] = []
            params: dict[str, str] = {"block_id": page_id, "page_size": "100"}
            while True:
                resp, err, status_code, error_code = await _throttled_request_with_error_metadata(
                    client,
                    "get",
                    f"{_BASE_URL}/comments",
                    _PROVIDER,
                    "list comments",
                    params=params,
                )
                if err:
                    return _format_comment_scope_error(
                        err,
                        action="read comments",
                        required_scope="comments:read",
                        status_code=status_code,
                        error_code=error_code,
                    )
                data = resp.json()
                all_comments.extend(data.get("results", []))
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                params["start_cursor"] = data["next_cursor"]
            authors, names_withheld = await _resolve_authors(client, all_comments)

        def _author(user: object) -> str:
            if _is_unnamed_user(user):
                if user["id"] in authors:
                    return _extract_notion_user_name(authors[user["id"]])
                if names_withheld:
                    return _person_label(user)
            return _extract_notion_user_name(user)

        def _fmt_comment(c: dict) -> str:
            text = _rich_text_to_plain(c.get("rich_text", []))
            created = c.get("created_time", "")
            author = _author(c.get("created_by"))
            comment_id = c.get("id", "")
            discussion_id = c.get("discussion_id", "")
            lines = [f"• [{created}] {author}: {text or '(no text content)'}"]
            if comment_id:
                lines.append(f"  ID: {comment_id}")
            if discussion_id:
                # Surfaced so the agent can reply into this thread via
                # add_notion_comment(discussion_id=...).
                lines.append(f"  Discussion: {discussion_id}")
            lines.extend(_attachment_lines(c))
            return "\n".join(lines)

        return format_entity_list(
            all_comments,
            _fmt_comment,
            noun="comment",
            empty_msg="No comments on this page.",
        )

    async def add_notion_comment(
        page_id: str = "", text: str = "", discussion_id: str = ""
    ) -> str:
        """Add a page-level comment OR reply into an existing discussion thread.

        Provide ``text`` plus EITHER ``page_id`` (start a new page-level
        comment) OR ``discussion_id`` (reply into an existing inline/thread
        discussion — get the discussion_id from ``list_notion_comments``).
        """
        page_id = (page_id or "").strip()
        discussion_id = (discussion_id or "").strip()
        if not (text or "").strip():
            return "Error: comment text is required."
        if not page_id and not discussion_id:
            return (
                "Error: provide page_id (new page-level comment) or "
                "discussion_id (reply to an existing thread)."
            )
        if page_id and discussion_id:
            return (
                "Error: provide EITHER page_id OR discussion_id, not both "
                "(page_id starts a new comment; discussion_id replies to a "
                "thread)."
            )
        if len(text) > _NOTION_RICH_TEXT_LIMIT:
            logger.warning(
                "Notion comment truncated from %d to %d chars",
                len(text),
                _NOTION_RICH_TEXT_LIMIT,
            )
            text = text[: _NOTION_RICH_TEXT_LIMIT - 1] + "…"
        body: dict = {"rich_text": [{"type": "text", "text": {"content": text}}]}
        if discussion_id:
            body["discussion_id"] = discussion_id
        else:
            body["parent"] = {"page_id": page_id}
        async with _client() as client:
            _, err, status_code, error_code = await _throttled_request_with_error_metadata(
                client,
                "post",
                f"{_BASE_URL}/comments",
                _PROVIDER,
                "add comment",
                json=body,
            )
            if err:
                return _format_comment_scope_error(
                    err,
                    action="write comments",
                    required_scope="comments:write",
                    status_code=status_code,
                    error_code=error_code,
                )
            target = (
                f"discussion {discussion_id}" if discussion_id else f"page {page_id}"
            )
            return f"Comment added to {target}."

    executor.register("list_notion_comments", list_notion_comments)
    executor.register("add_notion_comment", add_notion_comment)

    return [
        # ── Comments ───────────────────────────────────────────────────
        ToolDefinition(
            name="list_notion_comments",
            description=(
                "Read page-level discussion comments and inline block comments "
                "from a Notion page or block. Returns author names, timestamps, "
                "text content and attached files (signed file_url for "
                "download_notion_file)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": "Page or block ID to read comments from",
                    },
                },
                "required": ["page_id"],
            },
            source="integration:notion",
            priority=1,
        ),
        ToolDefinition(
            name="add_notion_comment",
            description=(
                "Add a comment to Notion. Provide text plus EITHER page_id (a new "
                "page-level comment) OR discussion_id (reply into an existing "
                "inline/thread discussion - get the discussion_id from "
                "list_notion_comments)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "page_id": {
                        "type": "string",
                        "description": (
                            "Notion page ID for a new page-level comment "
                            "(mutually exclusive with discussion_id)."
                        ),
                    },
                    "discussion_id": {
                        "type": "string",
                        "description": (
                            "Existing discussion/thread ID to reply into "
                            "(from list_notion_comments; mutually exclusive "
                            "with page_id)."
                        ),
                    },
                    "text": {"type": "string", "description": "Comment text"},
                },
                "required": ["text"],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
