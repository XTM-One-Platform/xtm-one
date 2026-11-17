"""VirusTotal community tools - comments and votes (read + write).

* ``get_vt_comments`` / ``add_vt_comment`` - community context on any IOC
  (``GET/POST /{type}/{id}/comments``).
* ``delete_vt_comment`` - remove one of your own comments
  (``DELETE /comments/{id}``, permanent).
* ``get_vt_votes`` / ``add_vt_vote`` - community verdicts
  (``GET/POST /{type}/{id}/votes``).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from app.integrations.http import format_pagination_hint, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    _PROVIDER,
    _SOURCE,
    ENTITY_PATHS,
    as_int,
    attributes_of,
    clamp,
    client,
    cursor_of,
    data_of,
    trunc,
    ts,
    url_identifier,
)

_VOTE_VERDICTS = frozenset({"harmless", "malicious"})


def _resolve(entity_type: str, entity_id: str) -> tuple[str | None, str | None]:
    """Return ``(path_segment, object_id)`` or ``(None, error_message)``."""
    entity_type = (entity_type or "").strip().lower()
    path = ENTITY_PATHS.get(entity_type)
    if not path:
        return None, f"Error: entity_type must be one of {', '.join(sorted(ENTITY_PATHS))}."
    object_id = url_identifier(entity_id) if entity_type == "url" else (entity_id or "").strip()
    if not object_id:
        return None, "Error: entity_id is required."
    return f"{path}/{quote(object_id, safe='')}", None


def _format_comment(obj: dict[str, Any]) -> str:
    attrs = attributes_of(obj)
    date = ts(attrs.get("date"))
    tags = attrs.get("tags")
    tag_str = (
        f" [{trunc(', '.join(str(t) for t in tags[:5]), 200)}]"
        if isinstance(tags, list) and tags
        else ""
    )
    votes = attrs.get("votes")
    vote_str = ""
    if isinstance(votes, dict) and any(votes.values()):
        vote_str = f" (+{as_int(votes.get('positive'))}/-{as_int(votes.get('negative'))})"
    return (
        f"  [{date or '?'}]{tag_str}{vote_str} {trunc(attrs.get('text'), 300)} "
        f"(comment id: {trunc(obj.get('id'), 100) or '?'})"
    )


def register(
    api: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    """Register comment + vote tools and return their definitions."""

    async def get_vt_comments(
        entity_type: str,
        entity_id: str,
        limit: int = 10,
        cursor: str = "",
    ) -> str:
        """Read community comments posted on a file, URL, domain, or IP."""
        prefix, err = _resolve(entity_type, entity_id)
        if err:
            return err
        params: dict[str, Any] = {"limit": clamp(limit)}
        if cursor.strip():
            params["cursor"] = cursor.strip()
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{prefix}/comments",
                _PROVIDER,
                f"get comments on {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
                params=params,
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return f"No community comments on this {entity_type}."
        lines = [f"Comments on {entity_type} {trunc(entity_id, 80)} ({len(items)} shown):"]
        lines.extend(_format_comment(o) for o in items if isinstance(o, dict))
        return "\n".join(lines) + format_pagination_hint(cursor_of(payload), "cursor")

    async def add_vt_comment(entity_type: str, entity_id: str, text: str) -> str:
        """Post a community comment on a file, URL, domain, or IP."""
        prefix, err = _resolve(entity_type, entity_id)
        if err:
            return err
        text = (text or "").strip()
        if not text:
            return "Error: comment text is required."
        body = {"data": {"type": "comment", "attributes": {"text": text}}}
        async with client() as c:
            resp, err = await safe_request(
                c,
                "post",
                f"{api}/{prefix}/comments",
                _PROVIDER,
                f"add comment to {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
            payload = resp.json() or {}
        obj = data_of(payload)
        comment_id = (trunc(obj.get("id"), 100) if isinstance(obj, dict) else "") or "?"
        return f"Comment posted on {entity_type} '{trunc(entity_id, 100)}' (comment id: {comment_id})."

    async def delete_vt_comment(comment_id: str) -> str:
        """Permanently delete one of your own VirusTotal comments."""
        comment_id = (comment_id or "").strip()
        if not comment_id:
            return "Error: comment_id is required (see get_vt_comments output)."
        async with client() as c:
            _resp, err = await safe_request(
                c,
                "delete",
                f"{api}/comments/{quote(comment_id, safe='')}",
                _PROVIDER,
                f"delete comment {trunc(comment_id, 60)}",
                headers=headers,
            )
            if err:
                return err
        return f"Comment '{trunc(comment_id, 100)}' deleted."

    async def get_vt_votes(entity_type: str, entity_id: str, limit: int = 10) -> str:
        """Read community harmless/malicious votes on a file, URL, domain, or IP."""
        prefix, err = _resolve(entity_type, entity_id)
        if err:
            return err
        async with client() as c:
            resp, err = await safe_request(
                c,
                "get",
                f"{api}/{prefix}/votes",
                _PROVIDER,
                f"get votes on {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
                params={"limit": clamp(limit)},
            )
            if err:
                return err
            payload = resp.json() or {}
        items = data_of(payload)
        items = items if isinstance(items, list) else []
        if not items:
            return f"No community votes on this {entity_type}."
        lines = [f"Votes on {entity_type} {trunc(entity_id, 80)} ({len(items)} shown):"]
        for obj in items:
            if not isinstance(obj, dict):
                continue
            attrs = attributes_of(obj)
            lines.append(
                f"  {trunc(attrs.get('verdict'), 20) or '?'} "
                f"[{ts(attrs.get('date')) or '?'}] (vote id: {trunc(obj.get('id'), 100) or '?'})"
            )
        return "\n".join(lines)

    async def add_vt_vote(entity_type: str, entity_id: str, verdict: str) -> str:
        """Cast a harmless or malicious community vote on a file, URL, domain, or IP."""
        prefix, err = _resolve(entity_type, entity_id)
        if err:
            return err
        verdict = (verdict or "").strip().lower()
        if verdict not in _VOTE_VERDICTS:
            return f"Error: verdict must be one of {', '.join(sorted(_VOTE_VERDICTS))}."
        body = {"data": {"type": "vote", "attributes": {"verdict": verdict}}}
        async with client() as c:
            _resp, err = await safe_request(
                c,
                "post",
                f"{api}/{prefix}/votes",
                _PROVIDER,
                f"vote {verdict} on {entity_type} {trunc(entity_id, 80)}",
                headers=headers,
                json=body,
            )
            if err:
                return err
        return f"Voted '{verdict}' on {entity_type} '{trunc(entity_id, 100)}'."

    executor.register("get_vt_comments", get_vt_comments)
    executor.register("add_vt_comment", add_vt_comment)
    executor.register("delete_vt_comment", delete_vt_comment)
    executor.register("get_vt_votes", get_vt_votes)
    executor.register("add_vt_vote", add_vt_vote)

    _entity_props: dict[str, Any] = {
        "entity_type": {
            "type": "string",
            "enum": sorted(ENTITY_PATHS),
            "description": "Type of the object",
        },
        "entity_id": {
            "type": "string",
            "description": "Hash for files, raw URL for urls, domain name, or IP address",
        },
    }

    return [
        ToolDefinition(
            name="get_vt_comments",
            description=(
                "Read community comments on a file, URL, domain, or IP - "
                "analyst context, campaign attribution, and IOC notes shared "
                "by the VirusTotal community. Returns comment ids (needed by "
                "delete_vt_comment). Paginate with limit + cursor."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_entity_props,
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max comments to return (1-40)",
                    },
                    "cursor": {
                        "type": "string",
                        "description": "Pagination cursor from a previous call",
                    },
                },
                "required": ["entity_type", "entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_vt_comment",
            description=(
                "Post a public community comment on a file, URL, domain, or "
                "IP. Comments are visible to the whole VirusTotal community - "
                "use #tags inside the text to make them searchable (e.g. "
                "#phishing #apt28). Never include confidential details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_entity_props,
                    "text": {
                        "type": "string",
                        "description": "Comment text (supports #tags)",
                    },
                },
                "required": ["entity_type", "entity_id", "text"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_vt_comment",
            description=(
                "Permanently delete one of your own VirusTotal comments by "
                "its comment id (from get_vt_comments). This cannot be undone."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {
                        "type": "string",
                        "description": "Comment id, e.g. f-SHA256-abcd1234",
                    },
                },
                "required": ["comment_id"],
            },
            source=_SOURCE,
            priority=2,
        ),
        ToolDefinition(
            name="get_vt_votes",
            description=(
                "Read the individual community votes (harmless / malicious) "
                "cast on a file, URL, domain, or IP. For the aggregate count, "
                "the get_vt_*_report tools already include total votes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_entity_props,
                    "limit": {
                        "type": "integer",
                        "default": 10,
                        "description": "Max votes to return (1-40)",
                    },
                },
                "required": ["entity_type", "entity_id"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="add_vt_vote",
            description=(
                "Cast a community vote on a file, URL, domain, or IP - "
                "verdict is either 'harmless' or 'malicious'. Votes are "
                "public and feed the object's community reputation score."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_entity_props,
                    "verdict": {
                        "type": "string",
                        "enum": ["harmless", "malicious"],
                        "description": "Your verdict for the object",
                    },
                },
                "required": ["entity_type", "entity_id", "verdict"],
            },
            source=_SOURCE,
            priority=1,
        ),
    ]
