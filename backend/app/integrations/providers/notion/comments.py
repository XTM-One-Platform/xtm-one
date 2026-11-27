"""Notion comment tools: list + add."""

from __future__ import annotations

import asyncio
import re
from posixpath import basename
from typing import Any
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
    _parse_json_arg,
    _person_label,
    _rich_text_to_plain,
    format_entity_list,
    logger,
    user_capability_error,
)

# Distinct comment authors looked up per call: a comment's ``created_by`` is
# only ever the author's id, whatever the integration may read.
_MAX_AUTHOR_LOOKUPS = 25

# People one comment may mention, and pages of 100 users an email lookup reads.
_MAX_MENTIONS = 20
_EMAIL_SCAN_MAX_PAGES = 50

# ``@{who}`` marks where the mention of *who* (an id or email of ``mentions``)
# goes in the comment text.
_PLACEHOLDER = re.compile(r"@\{([^{}\s]+)\}")

_RICH_TEXT_TYPES = ("text", "mention", "equation")

# One Notion rich text run, as the comments endpoint takes it.
_Run = dict[str, Any]


def _text_run(content: str) -> _Run:
    """One text run, cut to Notion's per-run limit like a plain comment."""
    if len(content) > _NOTION_RICH_TEXT_LIMIT:
        logger.warning(
            "Notion comment truncated from %d to %d chars",
            len(content),
            _NOTION_RICH_TEXT_LIMIT,
        )
        content = content[: _NOTION_RICH_TEXT_LIMIT - 1] + "…"
    return {"type": "text", "text": {"content": content}}


def _mention_run(user_id: str) -> _Run:
    return {"type": "mention", "mention": {"type": "user", "user": {"id": user_id}}}


def _comment_rich_text(text: str, people: dict[str, str]) -> list[_Run]:
    """*text* as rich text, each ``@{who}`` of *people* (who to user id) a mention.

    A person with no placeholder in *text* is mentioned at its start, in the
    order given. A placeholder naming nobody of *people* stays literal text.
    """
    runs: list[_Run] = []
    placed: set[str] = set()
    cursor = 0
    for match in _PLACEHOLDER.finditer(text):
        user_id = people.get(match.group(1).lower())
        if user_id is None:
            continue
        if match.start() > cursor:
            runs.append(_text_run(text[cursor : match.start()]))
        runs.append(_mention_run(user_id))
        placed.add(match.group(1).lower())
        cursor = match.end()
    if cursor < len(text):
        runs.append(_text_run(text[cursor:]))
    leading: list[_Run] = []
    for who, user_id in people.items():
        if who not in placed:
            leading.extend([_mention_run(user_id), {"type": "text", "text": {"content": " "}}])
    return leading + runs


def _rich_text_json_error(rich_text: object) -> str | None:
    """Why *rich_text* (parsed ``rich_text_json``) cannot be posted, or None."""
    if not isinstance(rich_text, list) or not rich_text:
        return "Error: rich_text_json must be a non-empty JSON array of rich text objects."
    for idx, item in enumerate(rich_text):
        if not isinstance(item, dict) or item.get("type") not in _RICH_TEXT_TYPES:
            return (
                f"Error: rich_text_json[{idx}] must be an object whose type is one of "
                f"{', '.join(_RICH_TEXT_TYPES)}; nothing was posted."
            )
        if not isinstance(item.get(item["type"]), dict):
            return f"Error: rich_text_json[{idx}] has no '{item['type']}' object; nothing was posted."
        content = (item.get("text") or {}).get("content") if item["type"] == "text" else None
        if isinstance(content, str) and len(content) > _NOTION_RICH_TEXT_LIMIT:
            return (
                f"Error: rich_text_json[{idx}] text is {len(content)} characters; "
                f"Notion takes at most {_NOTION_RICH_TEXT_LIMIT} per run. Split it; "
                "nothing was posted."
            )
    return None


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

    async def _resolve_mentions(
        client: AutoRefreshClient, wanted: list[str]
    ) -> tuple[dict[str, tuple[str, str]], list[str], str | None]:
        """Each wanted id or email (lower-cased) to (user id, name).

        Returns the resolved people, the entries that matched no workspace
        user, and an error that stops the call (a refused users endpoint).
        Ids are read with GET /users/{id}; emails are matched on the pages of
        GET /users, the endpoint ``list_notion_users`` reads.
        """
        resolved: dict[str, tuple[str, str]] = {}
        missing: list[str] = []
        emails = [w for w in wanted if "@" in w]
        for user_id in (w for w in wanted if "@" not in w):
            resp, err, status_code, _code = await _throttled_request_with_error_metadata(
                client,
                "get",
                f"{_BASE_URL}/users/{user_id}",
                _PROVIDER,
                "get mentioned user",
                report_stale=False,
            )
            if status_code == 403:
                return {}, [], user_capability_error(err or "403", action="look up the people to mention")
            user = None
            if resp is not None and not err:
                try:
                    user = resp.json()
                except ValueError:
                    user = None
            if isinstance(user, dict) and user.get("id"):
                resolved[user_id] = (str(user["id"]), _extract_notion_user_name(user))
            else:
                missing.append(user_id)
        if emails:
            params: dict[str, str] = {"page_size": "100"}
            pages = 0
            while True:
                resp, err, status_code, _code = await _throttled_request_with_error_metadata(
                    client,
                    "get",
                    f"{_BASE_URL}/users",
                    _PROVIDER,
                    "list users",
                    params=params,
                )
                if err or resp is None:
                    if status_code == 403:
                        return {}, [], user_capability_error(err or "403", action="look up the people to mention")
                    return {}, [], f"Error: could not look up the people to mention: {err}"
                pages += 1
                data = resp.json()
                for user in data.get("results", []):
                    email = str((user.get("person") or {}).get("email") or "").lower()
                    if email in emails and user.get("id"):
                        resolved[email] = (str(user["id"]), _extract_notion_user_name(user))
                if all(e in resolved for e in emails):
                    break
                if not data.get("has_more") or not data.get("next_cursor"):
                    break
                if pages >= _EMAIL_SCAN_MAX_PAGES:
                    break
                params["start_cursor"] = data["next_cursor"]
            missing.extend(e for e in emails if e not in resolved)
        return resolved, missing, None

    async def add_notion_comment(
        page_id: str = "",
        text: str = "",
        discussion_id: str = "",
        mentions: list[str] | None = None,
        rich_text_json: str | list[_Run] | None = None,
    ) -> str:
        """Add a page-level comment OR reply into an existing discussion thread.

        Provide ``text`` (or ``rich_text_json``) plus EITHER ``page_id``
        (start a new page-level comment) OR ``discussion_id`` (reply into an
        existing inline/thread discussion — get the discussion_id from
        ``list_notion_comments``).

        ``mentions`` (Notion user ids or emails) are written as user mention
        runs, so the people are notified: at each ``@{id}`` / ``@{email}``
        placeholder of ``text``, else at its start. One that matches no
        workspace user refuses the call and nothing is posted.
        """
        page_id = (page_id or "").strip()
        discussion_id = (discussion_id or "").strip()
        if isinstance(mentions, str):
            mentions = [part for part in re.split(r"[,\s]+", mentions) if part]
        wanted: list[str] = []
        for entry in mentions or []:
            who = str(entry or "").strip().lower()
            if who and who not in wanted:
                wanted.append(who)
        rich_text, parse_err = _parse_json_arg(rich_text_json, what="rich_text_json", expected="array")
        if parse_err:
            return parse_err
        if rich_text is not None:
            if (text or "").strip():
                return "Error: provide EITHER text OR rich_text_json, not both; nothing was posted."
            if wanted:
                return (
                    "Error: mentions goes with text; in rich_text_json write the "
                    "mention runs yourself. Nothing was posted."
                )
            rich_text_err = _rich_text_json_error(rich_text)
            if rich_text_err:
                return rich_text_err
        elif not (text or "").strip():
            return "Error: comment text is required."
        if len(wanted) > _MAX_MENTIONS:
            return (
                f"Error: a comment mentions at most {_MAX_MENTIONS} people ({len(wanted)} given); nothing was posted."
            )
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
        async with _client() as client:
            people: dict[str, tuple[str, str]] = {}
            if wanted:
                people, missing, lookup_err = await _resolve_mentions(client, wanted)
                if lookup_err:
                    return f"{lookup_err} Nothing was posted."
                if missing:
                    return (
                        "Error: no Notion workspace user matches "
                        f"{', '.join(missing)}; nothing was posted. Find the "
                        "right id or email with list_notion_users(query=...)."
                    )
            if rich_text is None:
                rich_text = _comment_rich_text(text, {who: user_id for who, (user_id, _name) in people.items()})
            body: dict[str, Any] = {"rich_text": rich_text}
            if discussion_id:
                body["discussion_id"] = discussion_id
            else:
                body["parent"] = {"page_id": page_id}
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
            if people:
                names = ", ".join(name for _user_id, name in people.values())
                return f"Comment added to {target}, mentioning {names}."
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
                "list_notion_comments). mentions notifies people with real "
                "@mentions; an unknown one refuses the call, nothing posted."
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
                    "text": {
                        "type": "string",
                        "description": (
                            "Comment text; @{id} or @{email} marks where a person of mentions goes (else at the start)."
                        ),
                    },
                    "mentions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Notion user ids or emails to @mention (notified); find them with list_notion_users."
                        ),
                    },
                    "rich_text_json": {
                        "type": "string",
                        "description": (
                            "Instead of text: a raw Notion rich_text JSON array (text, mention, equation runs)."
                        ),
                    },
                },
                "required": [],
            },
            source="integration:notion",
            priority=1,
        ),
    ]
