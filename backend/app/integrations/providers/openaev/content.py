# SPDX-License-Identifier: Apache-2.0

"""OpenAEV content tools - documents, channels, articles, challenges.

* ``manage_openaev_document``  - document upload/list/get/download/update/delete.
* ``manage_openaev_channel``   - media channel CRUD.
* ``manage_openaev_article``   - media-pressure articles on a scenario/simulation.
* ``manage_openaev_challenge`` - challenges CRUD + try.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote

from app.integrations.file_delivery import (
    PersistFileFn,
    content_disposition_filename,
    deliver_file,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import safe_request
from app.services.content_safety import single_line
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

from ._common import (
    _PROVIDER,
    _api,
    _scope_path,
    _search_body,
    _with_scope_hint,
    json_answer,
    tool_client,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:openaev"

#: Largest document the tool downloads or uploads.
_MAX_DOCUMENT_BYTES = 50 * 1024 * 1024


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    persist_file_fn: PersistFileFn | None = None,
) -> list[ToolDefinition]:
    def _document_file_url(document_id: str) -> str:
        return f"{base_url}/api/documents/{quote(str(document_id), safe='')}/file"

    async def manage_openaev_document(
        action: str = "",
        document_id: str = "",
        name: str = "",
        content: str = "",
        mime_type: str = "text/plain",
        description: str = "",
        tags: list[str] | None = None,
        search: str = "",
        limit: int = 20,
        file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "post", "/api/documents/search", "search documents",
                json=_search_body(size=min(limit, 50), text_search=search or None),
            )
            if err:
                return err
            data = resp.json()
            items = data.get("content", data) if isinstance(data, dict) else data
            if not items:
                return "No documents found."
            lines = [f"Documents ({len(items)}):"]
            for d in items:
                line = f"- {d.get('document_name', '?')} (id: {d.get('document_id')})"
                if d.get("document_id"):
                    line += f" - download: {_document_file_url(d['document_id'])}"
                lines.append(line)
            return "\n".join(lines)

        if action == "get":
            if not document_id:
                return "document_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/documents/{document_id}",
                "get document",
            )
            if err:
                return err
            return (
                json_answer(resp.json(), 2000)
                + f"\nDownload URL: {_document_file_url(document_id)}"
            )

        if action == "download":
            if not document_id:
                return "document_id is required for action 'download'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/documents/{document_id}/file",
                "download document",
            )
            if err:
                return err
            data = resp.content or b""
            if not data:
                return f"Document {document_id} returned an empty file."
            if len(data) > _MAX_DOCUMENT_BYTES:
                return (
                    f"Document {document_id} is {round(len(data) / 1024 / 1024, 1)} MB, over the "
                    f"{_MAX_DOCUMENT_BYTES // 1024 // 1024} MB download limit. Download URL: "
                    f"{_document_file_url(document_id)}"
                )
            filename = (
                (name or "").strip()
                or content_disposition_filename(resp.headers.get("content-disposition"))
                or f"openaev-document-{document_id}"
            )
            return await deliver_file(
                data=data,
                filename=filename,
                content_type=resp.headers.get("content-type", "").split(";", 1)[0].strip(),
                source="OpenAEV",
                persist_file_fn=persist_file_fn,
                links=[("Download URL", _document_file_url(document_id))],
                mark_unstored=False,
            )

        if action == "upload":
            if (file_id or "").strip():
                piped = await read_pipe_file(
                    file_context, file_id, max_bytes=_MAX_DOCUMENT_BYTES, target="OpenAEV"
                )
                if isinstance(piped, str):
                    return ToolResult(tool_call_id="", content=piped, is_error=True)
                upload_bytes, piped_name, piped_mime = piped
                name = (name or "").strip() or piped_name
                upload_mime = piped_mime
            elif name and content:
                upload_bytes = content.encode("utf-8")
                upload_mime = mime_type or "text/plain"
            else:
                return (
                    "name and content (or file_id, a file of this conversation) are "
                    "required for action 'upload'."
                )
            doc_input = {"document_description": description or None}
            if tags is not None:
                doc_input["document_tags"] = tags
            # auth_only drops the tenant resolver that rides on `headers`, so
            # scope the path with the full headers.
            upload_path, scope_hint = await _scope_path(headers, "/api/documents")
            files = {
                "file": (name, upload_bytes, upload_mime),
                "input": (None, json.dumps(doc_input).encode("utf-8"), "application/json"),
            }
            # tool_client re-mints a platform-managed JWT first (and once on a
            # 401/403), so auth_only is read inside it, after that re-mint (#2372).
            async with tool_client(headers, timeout=60) as client:
                auth_only = {"Authorization": headers.get("Authorization", "")}
                # tenant-scoped by hand: upload_path
                resp, err = await safe_request(
                    client, "post", f"{base_url}{upload_path}", _PROVIDER,
                    "upload document", headers=auth_only, files=files,
                )
            if err:
                return _with_scope_hint(err, scope_hint)
            d = resp.json() if resp is not None and resp.content else {}
            what = f"file {file_id.strip()}" if (file_id or "").strip() else "the text"
            uploaded = f"Uploaded {what} as document {d.get('document_id')}."
            if d.get("document_id"):
                uploaded += f" Download URL: {_document_file_url(d['document_id'])}"
            # The stored name and OpenAEV's document name go in the block.
            return uploaded_file_answer(
                uploaded,
                target=_PROVIDER,
                filename=name,
                details=(
                    [f"Document name: {single_line(str(d['document_name']))}"]
                    if d.get("document_name")
                    else []
                ),
            )

        if action == "update":
            if not document_id:
                return "document_id is required for action 'update'."
            body: dict = {}
            if description:
                body["document_description"] = description
            if tags is not None:
                body["document_tags"] = tags
            if not body:
                return "Provide description or tags to update."
            _, err = await _api(
                base_url, headers, "put", f"/api/documents/{document_id}",
                "update document", json=body,
            )
            if err:
                return err
            return f"Updated document {document_id}."

        if action == "delete":
            if not document_id:
                return "document_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/documents/{document_id}",
                "delete document",
            )
            if err:
                return err
            return f"Deleted document {document_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, download, upload, update, delete."
        )

    async def manage_openaev_channel(
        action: str = "",
        channel_id: str = "",
        channel_type: str = "",
        name: str = "",
        description: str = "",
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "get", "/api/channels", "list channels"
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No channels found."
            lines = [f"Channels ({len(items)}):"]
            for c in items:
                lines.append(
                    f"- {c.get('channel_name', '?')} [{c.get('channel_type', '?')}] "
                    f"(id: {c.get('channel_id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not channel_id:
                return "channel_id is required for action 'get'."
            resp, err = await _api(
                base_url, headers, "get", f"/api/channels/{channel_id}", "get channel"
            )
            if err:
                return err
            return json_answer(resp.json(), 2000)

        if action in ("create", "update"):
            if action == "create" and not (channel_type and name and description):
                return "channel_type, name and description are required for 'create'."
            if action == "update" and not channel_id:
                return "channel_id is required for action 'update'."
            body: dict = {}
            if channel_type:
                body["channel_type"] = channel_type
            if name:
                body["channel_name"] = name
            if description:
                body["channel_description"] = description
            if action == "update" and not body:
                return "Provide at least one field to update (channel_type, name, or description)."
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/channels", "create channel",
                    json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/channels/{channel_id}",
                    "update channel", json=body,
                )
            if err:
                return err
            c = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} channel '{c.get('channel_name')}' (id: {c.get('channel_id')})."

        if action == "delete":
            if not channel_id:
                return "channel_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/channels/{channel_id}",
                "delete channel",
            )
            if err:
                return err
            return f"Deleted channel {channel_id}."

        return f"Unknown action '{action}'. Valid actions: list, get, create, update, delete."

    async def manage_openaev_article(
        action: str = "",
        scope: str = "simulation",
        parent_id: str = "",
        article_id: str = "",
        name: str = "",
        channel_id: str = "",
        article_content: str = "",
        author: str = "",
        published: bool | None = None,
        document_ids: list[str] | None = None,
    ) -> str:
        action = (action or "").strip().lower()
        scope = (scope or "simulation").strip().lower()
        root = "/api/exercises" if scope == "simulation" else "/api/scenarios"
        if not parent_id:
            return "parent_id (exercise_id or scenario_id) is required."
        base = f"{root}/{parent_id}/articles"

        def _article_body() -> dict:
            body: dict = {}
            if published is not None:
                body["article_published"] = published
            if name:
                body["article_name"] = name
            if channel_id:
                body["article_channel"] = channel_id
            if article_content:
                body["article_content"] = article_content
            if author:
                body["article_author"] = author
            if document_ids is not None:
                body["article_documents"] = document_ids
            return body

        if action == "create":
            if not name or not channel_id:
                return "name and channel_id are required for action 'create'."
            resp, err = await _api(
                base_url, headers, "post", base, "create article", json=_article_body()
            )
            if err:
                return err
            a = resp.json()
            return f"Created article '{a.get('article_name')}' (id: {a.get('article_id')})."

        if action == "update":
            if not article_id:
                return "article_id is required for action 'update'."
            body = _article_body()
            if not body:
                return (
                    "Provide at least one field to update (name, channel_id, "
                    "article_content, author, published, or document_ids)."
                )
            _, err = await _api(
                base_url, headers, "put", f"{base}/{article_id}", "update article",
                json=body,
            )
            if err:
                return err
            return f"Updated article {article_id}."

        if action == "delete":
            if not article_id:
                return "article_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"{base}/{article_id}", "delete article"
            )
            if err:
                return err
            return f"Deleted article {article_id}."

        return f"Unknown action '{action}'. Valid actions: create, update, delete."

    async def manage_openaev_challenge(
        action: str = "",
        challenge_id: str = "",
        name: str = "",
        category: str = "",
        content: str = "",
        score: float | None = None,
        max_attempts: int | None = None,
        tags: list[str] | None = None,
        flags: str = "",
        value: str = "",
    ) -> str:
        action = (action or "").strip().lower()

        if action == "list":
            resp, err = await _api(
                base_url, headers, "get", "/api/challenges", "list challenges"
            )
            if err:
                return err
            items = resp.json() or []
            if not items:
                return "No challenges found."
            lines = [f"Challenges ({len(items)}):"]
            for c in items:
                lines.append(
                    f"- {c.get('challenge_name', '?')} (id: {c.get('challenge_id')})"
                )
            return "\n".join(lines)

        if action in ("create", "update"):
            if action == "create" and (not name or not flags):
                return (
                    "name and flags (a JSON array of {flag_type, flag_value}) are "
                    "required for action 'create'."
                )
            if action == "update" and not challenge_id:
                return "challenge_id is required for action 'update'."
            body: dict = {}
            if name:
                body["challenge_name"] = name
            if category:
                body["challenge_category"] = category
            if content:
                body["challenge_content"] = content
            if score is not None:
                body["challenge_score"] = score
            if max_attempts is not None:
                body["challenge_max_attempts"] = max_attempts
            if tags is not None:
                body["challenge_tags"] = tags
            if flags:
                try:
                    body["challenge_flags"] = json.loads(flags)
                except ValueError as exc:
                    return f"flags must be a valid JSON array: {exc}"
            if action == "update" and not body:
                return (
                    "Provide at least one field to update (name, category, content, "
                    "score, max_attempts, tags, or flags)."
                )
            if action == "create":
                resp, err = await _api(
                    base_url, headers, "post", "/api/challenges", "create challenge",
                    json=body,
                )
            else:
                resp, err = await _api(
                    base_url, headers, "put", f"/api/challenges/{challenge_id}",
                    "update challenge", json=body,
                )
            if err:
                return err
            c = resp.json()
            verb = "Created" if action == "create" else "Updated"
            return f"{verb} challenge '{c.get('challenge_name')}' (id: {c.get('challenge_id')})."

        if action == "delete":
            if not challenge_id:
                return "challenge_id is required for action 'delete'."
            _, err = await _api(
                base_url, headers, "delete", f"/api/challenges/{challenge_id}",
                "delete challenge",
            )
            if err:
                return err
            return f"Deleted challenge {challenge_id}."

        if action == "try":
            if not challenge_id or not value:
                return "challenge_id and value (the flag value) are required for 'try'."
            resp, err = await _api(
                base_url, headers, "post", f"/api/challenges/{challenge_id}/try",
                "try challenge", json={"challenge_value": value},
            )
            if err:
                return err
            return json_answer(resp.json(), 1000)

        return (
            f"Unknown action '{action}'. Valid actions: list, create, update, delete, try."
        )

    executor.register("manage_openaev_document", manage_openaev_document)
    executor.register("manage_openaev_channel", manage_openaev_channel)
    executor.register("manage_openaev_article", manage_openaev_article)
    executor.register("manage_openaev_challenge", manage_openaev_challenge)

    return [
        ToolDefinition(
            name="manage_openaev_document",
            description=(
                "Manage OpenAEV documents. Actions: 'list' (search), 'get' "
                "(document_id), 'download' (document_id - the file into the "
                "conversation: a download card, its URL and a file_id to forward; its "
                "readable content comes with the result), 'upload' (name + content "
                "text, or file_id - any file of this conversation - optional mime_type/"
                "description/tags), 'update' (document_id + description/tags), 'delete' "
                "(document_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "get", "download", "upload", "update", "delete"],
                    },
                    "document_id": {"type": "string"},
                    "name": {"type": "string", "description": "Filename (upload; optional with file_id)."},
                    "content": {"type": "string", "description": "Text content of the file (upload)."},
                    "file_id": {
                        "type": "string",
                        "description": "A file of this conversation to upload byte-for-byte (instead of content).",
                    },
                    "mime_type": {"type": "string", "default": "text/plain"},
                    "description": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_channel",
            description=(
                "Manage OpenAEV media channels (used by media-pressure articles). "
                "Actions: 'list', 'get' (channel_id), 'create' (channel_type e.g. "
                "'newspaper'/'tv'/'social-network' + name + description), 'update' "
                "(channel_id + fields), 'delete' (channel_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "get", "create", "update", "delete"]},
                    "channel_id": {"type": "string"},
                    "channel_type": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_article",
            description=(
                "Manage OpenAEV media-pressure articles on a scenario or simulation. "
                "scope: 'simulation' (exercise) or 'scenario'; parent_id is the "
                "exercise_id or scenario_id. Actions: 'create' (name + channel_id, "
                "optional article_content/author/published/document_ids), 'update' "
                "(article_id + fields), 'delete' (article_id)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["create", "update", "delete"]},
                    "scope": {"type": "string", "enum": ["simulation", "scenario"], "default": "simulation"},
                    "parent_id": {"type": "string", "description": "exercise_id or scenario_id."},
                    "article_id": {"type": "string"},
                    "name": {"type": "string"},
                    "channel_id": {"type": "string"},
                    "article_content": {"type": "string"},
                    "author": {"type": "string"},
                    "published": {"type": "boolean", "description": "Whether the article is published (create default false). Omit on update to leave unchanged."},
                    "document_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["action", "parent_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_openaev_challenge",
            description=(
                "Manage OpenAEV challenges (capture-the-flag style). Actions: 'list', "
                "'create' (name + flags as a JSON array of {flag_type, flag_value}, "
                "optional category/content/score/max_attempts/tags), 'update' "
                "(challenge_id + fields), 'delete' (challenge_id), 'try' (challenge_id "
                "+ value to submit a flag)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "create", "update", "delete", "try"]},
                    "challenge_id": {"type": "string"},
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "content": {"type": "string"},
                    "score": {"type": "number"},
                    "max_attempts": {"type": "integer"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                    "flags": {"type": "string", "description": "JSON array of {flag_type, flag_value} (create)."},
                    "value": {"type": "string", "description": "Flag value to submit (try)."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
    ]
