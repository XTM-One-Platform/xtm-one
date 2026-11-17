# SPDX-License-Identifier: Apache-2.0

"""Figma integration – REST API coverage.

Files, Projects, Comments, Components, Component Sets, Styles,
Version History, Dev Resources.

Reference: https://developers.figma.com/docs/rest-api/
"""

from __future__ import annotations

import logging
import mimetypes
from typing import Any
from urllib.parse import unquote, urlparse

from app.core.egress import egress_client
from app.core.utils import format_exception_message
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import link_lines
from app.integrations.http import AutoRefreshClient, safe_request
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_PROVIDER = "Figma"
_BASE_URL = "https://api.figma.com"

# Overall guard so a single download can't read an unbounded blob into memory.
_MAX_FIGMA_DOWNLOAD_BYTES = 50 * 1024 * 1024  # 50 MB
# Figma serves rendered exports from its own S3 buckets / CDN. ``download_figma_image``
# restricts its server-side fetch to these so a crafted image_url can't turn the
# tool into an SSRF vector against internal services.
_FIGMA_IMAGE_HOST_SUFFIXES = ("figma.com", "amazonaws.com")
_FIGMA_MIME_BY_FORMAT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "svg": "image/svg+xml",
    "pdf": "application/pdf",
}


def _is_figma_image_host(url: str) -> bool:
    """True if *url* points at a Figma-hosted/AWS export host (download allow-list)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and any(
        host == suffix or host.endswith("." + suffix)
        for suffix in _FIGMA_IMAGE_HOST_SUFFIXES
    )


def _figma_filename_from_url(url: str) -> str:
    """Best-effort filename from a signed export URL path."""
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    return unquote(path.rsplit("/", 1)[-1]).strip()


class FigmaIntegration(BaseIntegration):
    provider_name = "figma"
    display_name = "Figma"
    icon = "Figma"
    description = "Connect Figma for design files, projects, comments, components, styles, version history, and dev resources."
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://www.figma.com/oauth",
            token_url=f"{_BASE_URL}/v1/oauth/token",
            scopes=[
                # User
                "current_user:read",
                # File content & metadata
                "file_content:read",
                "file_metadata:read",
                # Comments
                "file_comments:read",
                "file_comments:write",
                # Dev resources
                "file_dev_resources:read",
                "file_dev_resources:write",
                # Version history
                "file_versions:read",
                # Projects
                "projects:read",
                # Library assets & content
                "library_assets:read",
                "library_content:read",
                "team_library_content:read",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/me",
                _PROVIDER,
                "validate connection",
                headers={"Authorization": f"Bearer {token}"},
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/me",
                _PROVIDER,
                "get user info",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp is None:
                return {"error": err}
            data = resp.json()
            return {
                "id": data.get("id"),
                "handle": data.get("handle"),
                "email": data.get("email"),
                "img_url": data.get("img_url"),
            }


# ---------------------------------------------------------------------------
# Figma tools
# ---------------------------------------------------------------------------

_TIMEOUT = 30


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    headers = {"Authorization": f"Bearer {token}"}
    persist_file_fn = _kwargs.get("persist_file_fn")
    # Figma OAuth access tokens expire (~24 h) — route tool calls through
    # AutoRefreshClient so a 401/403 triggers the dispatcher's refresh
    # callback and a single retry instead of a "re-authorize" error. The
    # shared ``headers`` dict is mutated in place on refresh so every
    # subsequent tool call uses the new token.
    refresh_fn = _kwargs.get("refresh_fn")

    def _client(timeout: float = _TIMEOUT) -> AutoRefreshClient:
        return AutoRefreshClient(headers, refresh_fn, timeout=int(timeout))

    # ── User ────────────────────────────────────────────────────────────

    async def get_figma_me() -> str:
        """Get the currently authenticated Figma user."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/me",
                _PROVIDER,
                "get user profile",
                headers=headers,
            )
            if err:
                return err
            u = resp.json()
            return (
                f"User: {u.get('handle', 'unknown')}\n"
                f"Email: {u.get('email', 'N/A')}\n"
                f"ID: {u.get('id', 'N/A')}\n"
                f"Avatar: {u.get('img_url', 'N/A')}"
            )

    # ── File metadata (lightweight) ─────────────────────────────────────

    async def get_figma_file_metadata(file_key: str) -> str:
        """Get lightweight metadata for a Figma file (name, thumbnail, last modified, creator)."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/meta",
                _PROVIDER,
                "get file metadata",
                headers=headers,
            )
            if err:
                return err
            f = resp.json().get("file", {})
            creator = f.get("creator", {})
            lines = [
                f"Name: {f.get('name', 'N/A')}",
                f"Folder: {f.get('folder_name', 'N/A')}",
                f"Editor: {f.get('editorType', 'N/A')}",
                f"Last modified: {f.get('last_touched_at', 'N/A')}",
                f"Version: {f.get('version', 'N/A')}",
                f"URL: {f.get('url', 'N/A')}",
                f"Thumbnail: {f.get('thumbnail_url', 'N/A')}",
                f"Creator: {creator.get('handle', 'N/A')} ({creator.get('id', '')})",
                f"Role: {f.get('role', 'N/A')}",
                f"Link access: {f.get('link_access', 'N/A')}",
            ]
            return "\n".join(lines)

    # ── File content (full document tree) ───────────────────────────────

    async def get_figma_file(
        file_key: str, depth: int = 2, branch_data: bool = False
    ) -> str:
        """Get a Figma file's document tree, components, and styles.

        Use depth=1 for pages only, depth=2 for pages + top-level objects.
        Higher depths return more data but may be slow for large files.
        """
        async with _client(timeout=60) as client:
            params: dict[str, Any] = {}
            if depth:
                params["depth"] = depth
            if branch_data:
                params["branch_data"] = "true"
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}",
                _PROVIDER,
                "get file content",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()

            lines = [
                f"Name: {data.get('name', 'N/A')}",
                f"Last modified: {data.get('lastModified', 'N/A')}",
                f"Version: {data.get('version', 'N/A')}",
                f"Editor: {data.get('editorType', 'N/A')}",
                f"Thumbnail: {data.get('thumbnailUrl', 'N/A')}",
            ]

            # List pages
            doc = data.get("document", {})
            children = doc.get("children", [])
            if children:
                lines.append(f"\nPages ({len(children)}):")
                for page in children:
                    page_children = page.get("children", [])
                    lines.append(
                        f"  - {page.get('name', 'Untitled')} (id: {page.get('id', '')}, {len(page_children)} children)"
                    )
                    for child in page_children[:30]:
                        lines.append(
                            f"      {child.get('type', '?')}: {child.get('name', 'Untitled')} (id: {child.get('id', '')})"
                        )
                    if len(page_children) > 30:
                        lines.append(
                            f"      ... and {len(page_children) - 30} more objects"
                        )

            # Components
            components = data.get("components", {})
            if components:
                lines.append(f"\nComponents ({len(components)}):")
                for nid, comp in list(components.items())[:20]:
                    lines.append(
                        f"  - {comp.get('name', 'Untitled')} (node: {nid}, description: {comp.get('description', 'none')})"
                    )
                if len(components) > 20:
                    lines.append(f"  ... and {len(components) - 20} more components")

            # Branches
            branches = data.get("branches", [])
            if branches:
                lines.append(f"\nBranches ({len(branches)}):")
                for b in branches:
                    lines.append(
                        f"  - {b.get('name', 'Untitled')} (key: {b.get('key', '')})"
                    )

            return "\n".join(lines)

    # ── File nodes ──────────────────────────────────────────────────────

    async def get_figma_file_nodes(file_key: str, node_ids: str, depth: int = 0) -> str:
        """Get specific nodes from a Figma file by their IDs.

        node_ids: Comma-separated node IDs (e.g. '1:2,1:3').
        depth: How deep into each node's subtree to return (0 = full subtree).
        """
        async with _client(timeout=60) as client:
            params: dict[str, Any] = {"ids": node_ids}
            if depth > 0:
                params["depth"] = depth
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/nodes",
                _PROVIDER,
                "get file nodes",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()

            lines = [f"File: {data.get('name', 'N/A')}"]
            nodes = data.get("nodes", {})
            for nid, node_data in nodes.items():
                if node_data is None:
                    lines.append(f"\nNode {nid}: Not found or not renderable")
                    continue
                doc = node_data.get("document", {})
                lines.append(
                    f"\nNode {nid}: {doc.get('name', 'Untitled')} (type: {doc.get('type', '?')})"
                )
                children = doc.get("children", [])
                if children:
                    for child in children[:20]:
                        lines.append(
                            f"  - {child.get('type', '?')}: {child.get('name', 'Untitled')} (id: {child.get('id', '')})"
                        )
                    if len(children) > 20:
                        lines.append(f"  ... and {len(children) - 20} more children")

            return "\n".join(lines)

    # ── Export images ───────────────────────────────────────────────────

    async def export_figma_images(
        file_key: str,
        node_ids: str,
        format: str = "png",
        scale: float = 1.0,
    ) -> str:
        """Export/render images from specific nodes in a Figma file.

        node_ids: Comma-separated node IDs to render (e.g. '1:2,1:3').
        format: Output format — 'png', 'jpg', 'svg', or 'pdf'.
        scale: Image scale factor (0.01 to 4).
        Returns download URLs for each rendered node (valid for 30 days).
        """
        async with _client(timeout=60) as client:
            params: dict[str, Any] = {
                "ids": node_ids,
                "format": format,
                "scale": max(0.01, min(4.0, scale)),
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/images/{file_key}",
                _PROVIDER,
                "export images",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            images = data.get("images", {})
            if not images:
                return "No images rendered (nodes may be invisible or have 0% opacity)."
            lines = [f"Rendered {len(images)} node(s) as {format} (scale {scale}x):"]
            for nid, url in images.items():
                if url:
                    lines.append(f"  Node {nid}: {url}")
                else:
                    lines.append(
                        f"  Node {nid}: Render failed (node may be invisible or not renderable)"
                    )
            lines.append(
                "\nNote: these are short-lived signed URLs, not bytes. To embed a "
                "diagram in a document or forward it to another tool (e.g. attach "
                "as Drata evidence), use download_figma_image to fetch the bytes "
                "into a reusable file_id."
            )
            return "\n".join(lines)

    async def download_figma_image(
        file_key: str = "",
        node_id: str = "",
        image_url: str = "",
        format: str = "png",
        scale: float = 2.0,
        filename: str = "",
    ) -> str | ToolResult:
        """Render a Figma node to an image and download the BYTES as a reusable file_id.

        Figma's export API only returns a short-lived signed URL (not bytes),
        and nothing downstream can dereference it (the sandbox has no network).
        This tool fetches the rendered bytes **server-side** (byte-for-byte, no
        truncation) and stores them as a downloadable file, returning a
        ``[[FILE:id]]`` card + a ``file_id`` you can forward to any tool that
        accepts a file — e.g. embed in a PDF recap or attach as Drata evidence
        via ``source_file_id``. This is the way to turn a Figma diagram into
        real, attachable proof (a node tree / signed URL is NOT a picture).

        Provide EITHER:
          - ``file_key`` + ``node_id``: render that single node (PREFERRED). Get
            them from get_figma_file / get_figma_file_nodes.
          - ``image_url``: a Figma signed export URL you already hold (e.g. from
            export_figma_images). Only Figma / AWS hosts are allowed.

        ``format`` is png/jpg/svg/pdf (default png), ``scale`` 0.01-4 (default 2
        for crisp evidence). ``filename`` overrides the saved name.
        """
        from app.llm.base import FileRef

        file_key = (file_key or "").strip()
        node_id = (node_id or "").strip()
        image_url = (image_url or "").strip()
        fmt = (format or "png").strip().lower()
        filename = (filename or "").strip()

        if image_url and (file_key or node_id):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: provide EITHER image_url OR file_key+node_id, not both."
                ),
                is_error=True,
            )
        if not image_url and not (file_key and node_id):
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: provide file_key + node_id (to render a node) or "
                    "image_url (a Figma signed export URL you already hold)."
                ),
                is_error=True,
            )
        if fmt not in _FIGMA_MIME_BY_FORMAT:
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: format must be one of "
                    f"{', '.join(sorted(_FIGMA_MIME_BY_FORMAT))} (got '{fmt}')."
                ),
                is_error=True,
            )

        def _err(msg: str) -> ToolResult:
            return ToolResult(tool_call_id="", content=msg, is_error=True)

        url = image_url
        if not url:
            async with _client(timeout=60) as client:
                params: dict[str, Any] = {
                    "ids": node_id,
                    "format": fmt,
                    "scale": max(0.01, min(4.0, scale)),
                }
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/images/{file_key}",
                    _PROVIDER,
                    "export image",
                    headers=headers,
                    params=params,
                )
            if err:
                return _err(err)
            images = resp.json().get("images", {})
            url = images.get(node_id) or next(
                (u for u in images.values() if u), ""
            )
            if not url:
                return _err(
                    "Error: Figma rendered no image for that node (it may be "
                    "invisible, 0% opacity, or not renderable)."
                )

        if not url.lower().startswith("https://"):
            return _err("Error: the image URL must be an HTTPS Figma export URL.")
        if not _is_figma_image_host(url):
            host = urlparse(url).hostname or "?"
            return _err(
                f"Error: refusing to download from '{host}' — only Figma-hosted "
                "exports (Figma / AWS storage) can be fetched byte-for-byte."
            )

        # Fetch with a plain client: the signed URL carries its own auth in the
        # query string, so the Figma bearer token must NOT be sent to S3.
        async with egress_client(follow_redirects=True, timeout=120.0) as dl:
            resp, err = await safe_request(
                dl, "get", url, _PROVIDER, "download image bytes"
            )
        if err:
            return _err(err)
        content = resp.content
        if not content:
            return _err("Error: the downloaded Figma image is empty.")
        if len(content) > _MAX_FIGMA_DOWNLOAD_BYTES:
            return _err(
                f"Error: image is {round(len(content) / 1024 / 1024, 1)} MB, over "
                f"the {_MAX_FIGMA_DOWNLOAD_BYTES // 1024 // 1024} MB download limit."
            )

        ext = "jpg" if fmt in ("jpg", "jpeg") else fmt
        derived_name = filename or (
            f"figma-{node_id.replace(':', '-')}.{ext}"
            if node_id
            else _figma_filename_from_url(url) or f"figma-image.{ext}"
        )
        mime = (
            resp.headers.get("content-type", "").split(";", 1)[0].strip()
            or _FIGMA_MIME_BY_FORMAT.get(fmt)
            or mimetypes.guess_type(derived_name)[0]
            or "application/octet-stream"
        )

        file_ref = None
        if persist_file_fn is not None:
            try:
                file_ref = await persist_file_fn(derived_name, content, mime)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Figma image persistence failed: %s", format_exception_message(exc))
                file_ref = None
        if file_ref is None:
            file_ref = FileRef.from_bytes(
                data=content, filename=derived_name, content_type=mime
            )
        web_url = (
            f"https://www.figma.com/design/{file_key}?node-id={node_id.replace(':', '-')}"
            if file_key and node_id
            else ""
        )
        links = link_lines([("Web URL", web_url), ("Download URL (Figma export)", url)])
        return ToolResult(
            tool_call_id="",
            content=(
                f"Downloaded Figma render '{derived_name}' "
                f"({round(len(content) / 1024, 1)} KB) as a reusable file. "
                f"[[FILE:{file_ref.file_id}]]\n"
                + "".join(f"{line}\n" for line in links)
                + "\nForward it to another tool with "
                f"file_id={file_ref.file_id} (e.g. embed in a recap or attach as "
                "evidence via source_file_id), and include the [[FILE:...]] marker "
                "verbatim so the user gets a download card."
            ),
            files=[file_ref],
        )

    # ── Comments ────────────────────────────────────────────────────────

    async def get_figma_comments(file_key: str) -> str:
        """Get all comments on a Figma file."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/comments",
                _PROVIDER,
                "get comments",
                headers=headers,
                params={"as_md": "true"},
            )
            if err:
                return err
            data = resp.json()
            comments = data.get("comments", [])
            if not comments:
                return "No comments on this file."
            lines = [f"Comments ({len(comments)}):"]
            for c in comments:
                user = c.get("user", {})
                resolved = " [RESOLVED]" if c.get("resolved_at") else ""
                parent_note = (
                    f" (reply to {c['parent_id']})" if c.get("parent_id") else ""
                )
                lines.append(
                    f"\n--- Comment {c.get('id', '?')}{resolved}{parent_note} ---\n"
                    f"By: {user.get('handle', 'Unknown')} ({user.get('id', '')})\n"
                    f"Date: {c.get('created_at', 'N/A')}\n"
                    f"Message: {c.get('message', '')}"
                )
            return "\n".join(lines)

    async def post_figma_comment(
        file_key: str, message: str, comment_id: str = "", node_id: str = ""
    ) -> str:
        """Post a comment on a Figma file.

        message: The comment text.
        comment_id: If provided, replies to this comment (must be a root comment).
        node_id: If provided, positions the comment on a specific node.
        """
        async with _client() as client:
            payload: dict[str, Any] = {"message": message}
            if comment_id:
                payload["comment_id"] = comment_id
            if node_id:
                payload["client_meta"] = {
                    "node_id": node_id,
                    "node_offset": {"x": 0, "y": 0},
                }
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/v1/files/{file_key}/comments",
                _PROVIDER,
                "post comment",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            c = resp.json()
            return f"Comment posted (id: {c.get('id', '?')}): {c.get('message', '')}"

    async def delete_figma_comment(file_key: str, comment_id: str) -> str:
        """Delete a comment from a Figma file (only the comment author can delete)."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/v1/files/{file_key}/comments/{comment_id}",
                _PROVIDER,
                "delete comment",
                headers=headers,
            )
            if err:
                return err
            return f"Comment {comment_id} deleted."

    async def post_figma_comment_reaction(
        file_key: str, comment_id: str, emoji: str
    ) -> str:
        """React to a comment with an emoji shortcode (e.g. ':heart:', ':+1:')."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/v1/files/{file_key}/comments/{comment_id}/reactions",
                _PROVIDER,
                "post comment reaction",
                headers={**headers, "Content-Type": "application/json"},
                json={"emoji": emoji},
            )
            if err:
                return err
            return f"Reacted with {emoji} on comment {comment_id}."

    async def get_figma_comment_reactions(file_key: str, comment_id: str) -> str:
        """Get all emoji reactions on a specific comment."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/comments/{comment_id}/reactions",
                _PROVIDER,
                "get comment reactions",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            reactions = data.get("reactions", [])
            if not reactions:
                return f"No reactions on comment {comment_id}."
            lines = [f"Reactions on comment {comment_id} ({len(reactions)}):"]
            for r in reactions:
                user = r.get("user", {})
                lines.append(
                    f"  {r.get('emoji', '?')} by {user.get('handle', 'Unknown')} ({user.get('id', '')})"
                )
            return "\n".join(lines)

    # ── Version history ─────────────────────────────────────────────────

    async def get_figma_file_versions(file_key: str) -> str:
        """Get the version history of a Figma file."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/versions",
                _PROVIDER,
                "get file versions",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            versions = data.get("versions", [])
            if not versions:
                return "No version history."
            lines = [f"Versions ({len(versions)}):"]
            for v in versions[:30]:
                user = v.get("user", {})
                label = v.get("label") or "(auto-save)"
                desc = v.get("description") or ""
                lines.append(
                    f"  {v.get('id', '?')} — {label}"
                    f" by {user.get('handle', 'Unknown')}"
                    f" at {v.get('created_at', 'N/A')}" + (f" — {desc}" if desc else "")
                )
            if len(versions) > 30:
                lines.append(f"  ... and {len(versions) - 30} more versions")
            return "\n".join(lines)

    # ── Projects ────────────────────────────────────────────────────────

    async def get_figma_team_projects(team_id: str) -> str:
        """List all projects in a Figma team.

        team_id: The team ID (found in the URL of the team page in Figma).
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/teams/{team_id}/projects",
                _PROVIDER,
                "get team projects",
                headers=headers,
            )
            if err:
                return err
            data = resp.json()
            projects = data.get("projects", [])
            if not projects:
                return "No projects found in this team."
            lines = [f"Projects in team {team_id} ({len(projects)}):"]
            for p in projects:
                lines.append(f"  - {p.get('name', 'Untitled')} (id: {p.get('id', '')})")
            return "\n".join(lines)

    async def get_figma_project_files(project_id: str) -> str:
        """List all files in a Figma project."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/projects/{project_id}/files",
                _PROVIDER,
                "get project files",
                headers=headers,
                params={"branch_data": "true"},
            )
            if err:
                return err
            data = resp.json()
            files = data.get("files", [])
            if not files:
                return "No files found in this project."
            lines = [f"Files in project {project_id} ({len(files)}):"]
            for f in files:
                branches = f.get("branches", [])
                branch_info = f" ({len(branches)} branches)" if branches else ""
                lines.append(
                    f"  - {f.get('name', 'Untitled')} (key: {f.get('key', '')})"
                    f" — modified: {f.get('last_modified', 'N/A')}{branch_info}"
                )
            return "\n".join(lines)

    # ── Components & styles ─────────────────────────────────────────────

    async def get_figma_components(
        scope: str = "", file_key: str = "", team_id: str = "", component_key: str = ""
    ) -> str:
        """Get published components from a Figma file or team library, or get a specific component by key.

        component_key: If provided, return detailed metadata for this single component (ignores scope/file_key/team_id).
        scope: 'file' to list components in a specific file, 'team' for team-wide.
        file_key: Required when scope is 'file'.
        team_id: Required when scope is 'team'.
        """
        async with _client() as client:
            if component_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/components/{component_key}",
                    _PROVIDER,
                    "get component",
                    headers=headers,
                )
                if err:
                    return err
                data = resp.json()
                meta = data.get("meta", {})
                frame = meta.get("containing_frame", {})
                user = meta.get("user", {})
                return (
                    f"Component: {meta.get('name', 'Untitled')}\n"
                    f"Key: {meta.get('key', '')}\n"
                    f"File key: {meta.get('file_key', 'N/A')}\n"
                    f"Node ID: {meta.get('node_id', 'N/A')}\n"
                    f"Description: {meta.get('description') or 'none'}\n"
                    f"Frame: {frame.get('name', 'N/A')} (page: {frame.get('pageName', 'N/A')})\n"
                    f"Created by: {user.get('handle', 'Unknown')}\n"
                    f"Created: {meta.get('created_at', 'N/A')}\n"
                    f"Updated: {meta.get('updated_at', 'N/A')}\n"
                    f"Thumbnail: {meta.get('thumbnail_url', 'N/A')}"
                )
            if scope == "file":
                if not file_key:
                    return "file_key is required when scope is 'file'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/files/{file_key}/components",
                    _PROVIDER,
                    "get components",
                    headers=headers,
                )
            elif scope == "team":
                if not team_id:
                    return "team_id is required when scope is 'team'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/teams/{team_id}/components",
                    _PROVIDER,
                    "get components",
                    headers=headers,
                    params={"page_size": 50},
                )
            else:
                return "scope must be 'file' or 'team'."
            if err:
                return err
            data = resp.json()
            meta = data.get("meta", {})
            components = meta.get("components", [])
            if not components:
                return f"No published components in this {scope}."
            label = "File components" if scope == "file" else "Team components"
            lines = [f"{label} ({len(components)}):"]
            for c in components[:30]:
                entry = f"  - {c.get('name', 'Untitled')} (key: {c.get('key', '')})"
                if scope == "team":
                    entry += f"\n    File: {c.get('file_key', 'N/A')}"
                entry += f"\n    Description: {c.get('description') or 'none'}"
                if scope == "file":
                    frame = c.get("containing_frame", {})
                    entry += f"\n    Frame: {frame.get('name', 'N/A')} (page: {frame.get('pageName', 'N/A')})"
                entry += f"\n    Updated: {c.get('updated_at', 'N/A')}"
                lines.append(entry)
            if len(components) > 30:
                lines.append(f"  ... and {len(components) - 30} more components")
            return "\n".join(lines)

    async def get_figma_component_sets(
        scope: str = "",
        file_key: str = "",
        team_id: str = "",
        component_set_key: str = "",
    ) -> str:
        """Get published component sets (variant groups) from a Figma file or team library, or get a specific component set by key.

        component_set_key: If provided, return detailed metadata for this single component set (ignores scope/file_key/team_id).
        scope: 'file' to list component sets in a specific file, 'team' for team-wide.
        file_key: Required when scope is 'file'.
        team_id: Required when scope is 'team'.
        """
        async with _client() as client:
            if component_set_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/component_sets/{component_set_key}",
                    _PROVIDER,
                    "get component set",
                    headers=headers,
                )
                if err:
                    return err
                data = resp.json()
                meta = data.get("meta", {})
                frame = meta.get("containing_frame", {})
                user = meta.get("user", {})
                return (
                    f"Component Set: {meta.get('name', 'Untitled')}\n"
                    f"Key: {meta.get('key', '')}\n"
                    f"File key: {meta.get('file_key', 'N/A')}\n"
                    f"Node ID: {meta.get('node_id', 'N/A')}\n"
                    f"Description: {meta.get('description') or 'none'}\n"
                    f"Frame: {frame.get('name', 'N/A')} (page: {frame.get('pageName', 'N/A')})\n"
                    f"Created by: {user.get('handle', 'Unknown')}\n"
                    f"Created: {meta.get('created_at', 'N/A')}\n"
                    f"Updated: {meta.get('updated_at', 'N/A')}\n"
                    f"Thumbnail: {meta.get('thumbnail_url', 'N/A')}"
                )
            if scope == "file":
                if not file_key:
                    return "file_key is required when scope is 'file'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/files/{file_key}/component_sets",
                    _PROVIDER,
                    "get component sets",
                    headers=headers,
                )
            elif scope == "team":
                if not team_id:
                    return "team_id is required when scope is 'team'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/teams/{team_id}/component_sets",
                    _PROVIDER,
                    "get component sets",
                    headers=headers,
                    params={"page_size": 50},
                )
            else:
                return "scope must be 'file' or 'team'."
            if err:
                return err
            data = resp.json()
            meta = data.get("meta", {})
            sets = meta.get("component_sets", [])
            if not sets:
                return f"No published component sets in this {scope}."
            label = "File component sets" if scope == "file" else "Team component sets"
            lines = [f"{label} ({len(sets)}):"]
            for s in sets[:30]:
                entry = f"  - {s.get('name', 'Untitled')} (key: {s.get('key', '')})"
                if scope == "team":
                    entry += f"\n    File: {s.get('file_key', 'N/A')}"
                entry += f"\n    Description: {s.get('description') or 'none'}"
                if scope == "file":
                    frame = s.get("containing_frame", {})
                    entry += f"\n    Frame: {frame.get('name', 'N/A')} (page: {frame.get('pageName', 'N/A')})"
                entry += f"\n    Updated: {s.get('updated_at', 'N/A')}"
                lines.append(entry)
            if len(sets) > 30:
                lines.append(f"  ... and {len(sets) - 30} more component sets")
            return "\n".join(lines)

    async def get_figma_styles(
        scope: str = "", file_key: str = "", team_id: str = "", style_key: str = ""
    ) -> str:
        """Get published styles from a Figma file or team library, or get a specific style by key.

        style_key: If provided, return detailed metadata for this single style (ignores scope/file_key/team_id).
        scope: 'file' to list styles in a specific file, 'team' for team-wide.
        file_key: Required when scope is 'file'.
        team_id: Required when scope is 'team'.
        """
        async with _client() as client:
            if style_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/styles/{style_key}",
                    _PROVIDER,
                    "get style",
                    headers=headers,
                )
                if err:
                    return err
                data = resp.json()
                meta = data.get("meta", {})
                user = meta.get("user", {})
                return (
                    f"Style: {meta.get('name', 'Untitled')}\n"
                    f"Key: {meta.get('key', '')}\n"
                    f"Type: {meta.get('style_type', 'N/A')}\n"
                    f"File key: {meta.get('file_key', 'N/A')}\n"
                    f"Node ID: {meta.get('node_id', 'N/A')}\n"
                    f"Description: {meta.get('description') or 'none'}\n"
                    f"Created by: {user.get('handle', 'Unknown')}\n"
                    f"Created: {meta.get('created_at', 'N/A')}\n"
                    f"Updated: {meta.get('updated_at', 'N/A')}\n"
                    f"Thumbnail: {meta.get('thumbnail_url', 'N/A')}"
                )
            if scope == "file":
                if not file_key:
                    return "file_key is required when scope is 'file'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/files/{file_key}/styles",
                    _PROVIDER,
                    "get styles",
                    headers=headers,
                )
            elif scope == "team":
                if not team_id:
                    return "team_id is required when scope is 'team'."
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/v1/teams/{team_id}/styles",
                    _PROVIDER,
                    "get styles",
                    headers=headers,
                    params={"page_size": 50},
                )
            else:
                return "scope must be 'file' or 'team'."
            if err:
                return err
            data = resp.json()
            meta = data.get("meta", {})
            styles = meta.get("styles", [])
            if not styles:
                return f"No published styles in this {scope}."
            label = "File styles" if scope == "file" else "Team styles"
            lines = [f"{label} ({len(styles)}):"]
            for s in styles[:30]:
                entry = f"  - {s.get('name', 'Untitled')} (key: {s.get('key', '')})"
                entry += f"\n    Type: {s.get('style_type', 'N/A')}"
                if scope == "team":
                    entry += f"\n    File: {s.get('file_key', 'N/A')}"
                entry += f"\n    Description: {s.get('description') or 'none'}"
                lines.append(entry)
            if len(styles) > 30:
                lines.append(f"  ... and {len(styles) - 30} more styles")
            return "\n".join(lines)

    # ── Dev resources ───────────────────────────────────────────────────

    async def get_figma_dev_resources(file_key: str, node_id: str = "") -> str:
        """Get dev resources attached to a Figma file or a specific node.

        file_key: The file key.
        node_id: Optional node ID to filter dev resources for a specific node.
        """
        async with _client() as client:
            params: dict[str, str] = {}
            if node_id:
                params["node_ids"] = node_id
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v1/files/{file_key}/dev_resources",
                _PROVIDER,
                "get dev resources",
                headers=headers,
                params=params,
            )
            if err:
                return err
            data = resp.json()
            resources = data.get("dev_resources", [])
            if not resources:
                return "No dev resources found."
            lines = [f"Dev resources ({len(resources)}):"]
            for r in resources:
                lines.append(
                    f"  - {r.get('name', 'Untitled')} ({r.get('url', 'N/A')})\n"
                    f"    Node: {r.get('node_id', 'N/A')}\n"
                    f"    ID: {r.get('id', 'N/A')}"
                )
            return "\n".join(lines)

    async def create_figma_dev_resource(
        file_key: str, node_id: str, name: str, url: str
    ) -> str:
        """Attach a dev resource (link) to a node in a Figma file.

        file_key: The file key.
        node_id: The node to attach the resource to.
        name: Display name for the resource.
        url: URL of the dev resource.
        """
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/v1/dev_resources",
                _PROVIDER,
                "create dev resource",
                headers={**headers, "Content-Type": "application/json"},
                json={
                    "dev_resources": [
                        {
                            "file_key": file_key,
                            "node_id": node_id,
                            "name": name,
                            "url": url,
                        }
                    ]
                },
            )
            if err:
                return err
            data = resp.json()
            links = data.get("links_created", [])
            if links:
                return f"Dev resource created: {name} → {url} on node {node_id}"
            return "Dev resource creation returned no results."

    async def update_figma_dev_resource(
        dev_resource_id: str, file_key: str, name: str = "", url: str = ""
    ) -> str:
        """Update an existing dev resource.

        dev_resource_id: The ID of the dev resource to update.
        file_key: The file key the dev resource belongs to.
        name: New display name (optional, keeps current if empty).
        url: New URL (optional, keeps current if empty).
        """
        async with _client() as client:
            resource: dict[str, Any] = {"id": dev_resource_id, "file_key": file_key}
            if name:
                resource["name"] = name
            if url:
                resource["url"] = url
            _, err = await safe_request(
                client,
                "put",
                f"{_BASE_URL}/v1/dev_resources",
                _PROVIDER,
                "update dev resource",
                headers={**headers, "Content-Type": "application/json"},
                json={"dev_resources": [resource]},
            )
            if err:
                return err
            return f"Dev resource {dev_resource_id} updated."

    async def delete_figma_dev_resource(file_key: str, dev_resource_id: str) -> str:
        """Delete a dev resource from a Figma file.

        file_key: The file key.
        dev_resource_id: The ID of the dev resource to delete.
        """
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/v1/files/{file_key}/dev_resources/{dev_resource_id}",
                _PROVIDER,
                "delete dev resource",
                headers=headers,
            )
            if err:
                return err
            return f"Dev resource {dev_resource_id} deleted from file {file_key}."

    # ── Register all tools ──────────────────────────────────────────────

    executor.register("get_figma_me", get_figma_me)
    executor.register("get_figma_file_metadata", get_figma_file_metadata)
    executor.register("get_figma_file", get_figma_file)
    executor.register("get_figma_file_nodes", get_figma_file_nodes)
    executor.register("export_figma_images", export_figma_images)
    executor.register("download_figma_image", download_figma_image)
    executor.register("get_figma_comments", get_figma_comments)
    executor.register("post_figma_comment", post_figma_comment)
    executor.register("delete_figma_comment", delete_figma_comment)
    executor.register("post_figma_comment_reaction", post_figma_comment_reaction)
    executor.register("get_figma_comment_reactions", get_figma_comment_reactions)
    executor.register("get_figma_file_versions", get_figma_file_versions)
    executor.register("get_figma_team_projects", get_figma_team_projects)
    executor.register("get_figma_project_files", get_figma_project_files)
    executor.register("get_figma_components", get_figma_components)
    executor.register("get_figma_component_sets", get_figma_component_sets)
    executor.register("get_figma_styles", get_figma_styles)
    executor.register("get_figma_dev_resources", get_figma_dev_resources)
    executor.register("create_figma_dev_resource", create_figma_dev_resource)
    executor.register("update_figma_dev_resource", update_figma_dev_resource)
    executor.register("delete_figma_dev_resource", delete_figma_dev_resource)

    return [
        # ── User ──
        ToolDefinition(
            name="get_figma_me",
            description="Get the currently authenticated Figma user's profile (name, email, ID).",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:figma",
            priority=2,
        ),
        # ── File metadata ──
        ToolDefinition(
            name="get_figma_file_metadata",
            description="Get lightweight metadata for a Figma file: name, folder, thumbnail, last modified, creator, URL.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {
                        "type": "string",
                        "description": "The file key (from the Figma URL: figma.com/:type/:file_key/:name)",
                    },
                },
                "required": ["file_key"],
            },
            source="integration:figma",
            priority=1,
        ),
        # ── File content ──
        ToolDefinition(
            name="get_figma_file",
            description="Get a Figma file's full document tree including pages, layers, components, and styles. Use depth to control how deep into the tree to go (1=pages only, 2=pages+top-level objects).",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "depth": {
                        "type": "integer",
                        "description": "How deep into the document tree (1=pages, 2=pages+objects). Default 2.",
                        "default": 2,
                    },
                    "branch_data": {
                        "type": "boolean",
                        "description": "Include branch metadata",
                        "default": False,
                    },
                },
                "required": ["file_key"],
            },
            source="integration:figma",
            priority=0,
        ),
        # ── File nodes ──
        ToolDefinition(
            name="get_figma_file_nodes",
            description="Get specific nodes from a Figma file by their IDs. Returns detailed node data including children and properties.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "node_ids": {
                        "type": "string",
                        "description": "Comma-separated node IDs (e.g. '1:2,1:3')",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Depth of subtree to return (0=full subtree)",
                        "default": 0,
                    },
                },
                "required": ["file_key", "node_ids"],
            },
            source="integration:figma",
            priority=0,
        ),
        # ── Export images ──
        ToolDefinition(
            name="export_figma_images",
            description="Export/render images from specific nodes in a Figma file. Returns download URLs for the rendered images (valid for 30 days).",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "node_ids": {
                        "type": "string",
                        "description": "Comma-separated node IDs to render (e.g. '1:2,1:3')",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["png", "jpg", "svg", "pdf"],
                        "description": "Image format",
                        "default": "png",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Scale factor (0.01 to 4)",
                        "default": 1.0,
                    },
                },
                "required": ["file_key", "node_ids"],
            },
            source="integration:figma",
            priority=0,
        ),
        # ── Download rendered image as a forwardable file ──
        ToolDefinition(
            name="download_figma_image",
            # ``filename`` only names the saved copy: a rename is the same download.
            cosmetic_arguments=("filename",),
            workspace_ingest=True,
            description=(
                "Render a Figma node to an image and download the RAW BYTES as a "
                "reusable file_id (with a download card). Use this to turn a Figma "
                "diagram into real, attachable proof: export only returns a "
                "short-lived signed URL (not bytes) that nothing downstream can "
                "fetch. This fetches the bytes server-side - byte-for-byte, no "
                "truncation - and returns a file_id you can embed in a document or "
                "forward to any tool that accepts a file (e.g. attach as Drata "
                "evidence via source_file_id). Provide file_key + node_id to "
                "render a node (preferred), or image_url for a Figma signed export "
                "URL you already hold."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {
                        "type": "string",
                        "description": ("The Figma file key (with node_id, to render a node)."),
                    },
                    "node_id": {
                        "type": "string",
                        "description": (
                            "A single node ID to render (e.g. '1:2'). Get it from "
                            "get_figma_file / get_figma_file_nodes. Requires "
                            "file_key. Mutually exclusive with image_url."
                        ),
                    },
                    "image_url": {
                        "type": "string",
                        "description": (
                            "A Figma signed export URL you already hold (e.g. from "
                            "export_figma_images). Only Figma / AWS hosts are "
                            "allowed. Mutually exclusive with file_key/node_id."
                        ),
                    },
                    "format": {
                        "type": "string",
                        "enum": ["png", "jpg", "svg", "pdf"],
                        "description": "Image format (default png).",
                        "default": "png",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Scale factor 0.01-4 (default 2 for crisp evidence).",
                        "default": 2.0,
                    },
                    "filename": {
                        "type": "string",
                        "description": "Optional override for the saved file name.",
                    },
                },
                "required": [],
            },
            source="integration:figma",
            priority=0,
        ),
        # ── Comments ──
        ToolDefinition(
            name="get_figma_comments",
            description="Get all comments on a Figma file, including replies and resolution status.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                },
                "required": ["file_key"],
            },
            source="integration:figma",
            priority=0,
        ),
        ToolDefinition(
            name="post_figma_comment",
            description="Post a comment on a Figma file. Can reply to an existing comment or position on a specific node.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "message": {"type": "string", "description": "The comment text"},
                    "comment_id": {
                        "type": "string",
                        "description": "Reply to this root comment ID (optional)",
                        "default": "",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Position comment on this node (optional)",
                        "default": "",
                    },
                },
                "required": ["file_key", "message"],
            },
            source="integration:figma",
            priority=0,
        ),
        ToolDefinition(
            name="delete_figma_comment",
            description="Delete a comment from a Figma file. Only the comment author can delete their own comments.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "comment_id": {
                        "type": "string",
                        "description": "The comment ID to delete",
                    },
                },
                "required": ["file_key", "comment_id"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="post_figma_comment_reaction",
            description="React to a Figma comment with an emoji shortcode (e.g. ':heart:', ':+1:', ':eyes:').",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "comment_id": {
                        "type": "string",
                        "description": "The comment ID to react to",
                    },
                    "emoji": {
                        "type": "string",
                        "description": "Emoji shortcode (e.g. ':heart:', ':+1:')",
                    },
                },
                "required": ["file_key", "comment_id", "emoji"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="get_figma_comment_reactions",
            description="Get all emoji reactions on a specific comment in a Figma file.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "comment_id": {"type": "string", "description": "The comment ID"},
                },
                "required": ["file_key", "comment_id"],
            },
            source="integration:figma",
            priority=1,
        ),
        # ── Version history ──
        ToolDefinition(
            name="get_figma_file_versions",
            description="Get the version history of a Figma file, including labels, descriptions, and authors.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                },
                "required": ["file_key"],
            },
            source="integration:figma",
            priority=2,
        ),
        # ── Projects ──
        ToolDefinition(
            name="get_figma_team_projects",
            description="List all projects in a Figma team. Team ID is found in the URL of the team page in Figma.",
            input_schema={
                "type": "object",
                "properties": {
                    "team_id": {"type": "string", "description": "The team ID"},
                },
                "required": ["team_id"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="get_figma_project_files",
            description="List all files in a Figma project, with branch metadata.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string", "description": "The project ID"},
                },
                "required": ["project_id"],
            },
            source="integration:figma",
            priority=1,
        ),
        # ── Components ──
        ToolDefinition(
            name="get_figma_components",
            description="Get published components from a Figma file or team library, or get detailed metadata for a specific component by key. Use component_key for a single component, or scope='file'/'team' to list all.",
            input_schema={
                "type": "object",
                "properties": {
                    "component_key": {
                        "type": "string",
                        "description": "The component key — when provided, returns detailed metadata for this single component (ignores scope/file_key/team_id)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["file", "team"],
                        "description": "Whether to list components from a file or team (required when component_key is not provided)",
                    },
                    "file_key": {
                        "type": "string",
                        "description": "The file key (required when scope is 'file')",
                    },
                    "team_id": {
                        "type": "string",
                        "description": "The team ID (required when scope is 'team')",
                    },
                },
                "required": [],
            },
            source="integration:figma",
            priority=2,
        ),
        # ── Component sets ──
        ToolDefinition(
            name="get_figma_component_sets",
            description="Get published component sets (variant groups) from a Figma file or team library, or get detailed metadata for a specific component set by key. Use component_set_key for a single set, or scope='file'/'team' to list all.",
            input_schema={
                "type": "object",
                "properties": {
                    "component_set_key": {
                        "type": "string",
                        "description": "The component set key — when provided, returns detailed metadata for this single component set (ignores scope/file_key/team_id)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["file", "team"],
                        "description": "Whether to list component sets from a file or team (required when component_set_key is not provided)",
                    },
                    "file_key": {
                        "type": "string",
                        "description": "The file key (required when scope is 'file')",
                    },
                    "team_id": {
                        "type": "string",
                        "description": "The team ID (required when scope is 'team')",
                    },
                },
                "required": [],
            },
            source="integration:figma",
            priority=2,
        ),
        # ── Styles ──
        ToolDefinition(
            name="get_figma_styles",
            description="Get published styles (colors, text styles, effects, grids) from a Figma file or team library, or get detailed metadata for a specific style by key. Use style_key for a single style, or scope='file'/'team' to list all.",
            input_schema={
                "type": "object",
                "properties": {
                    "style_key": {
                        "type": "string",
                        "description": "The style key — when provided, returns detailed metadata for this single style (ignores scope/file_key/team_id)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["file", "team"],
                        "description": "Whether to list styles from a file or team (required when style_key is not provided)",
                    },
                    "file_key": {
                        "type": "string",
                        "description": "The file key (required when scope is 'file')",
                    },
                    "team_id": {
                        "type": "string",
                        "description": "The team ID (required when scope is 'team')",
                    },
                },
                "required": [],
            },
            source="integration:figma",
            priority=2,
        ),
        # ── Dev resources ──
        ToolDefinition(
            name="get_figma_dev_resources",
            description="Get dev resources (links to code, docs, tickets) attached to a Figma file or specific node.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "node_id": {
                        "type": "string",
                        "description": "Optional node ID to filter resources",
                        "default": "",
                    },
                },
                "required": ["file_key"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="create_figma_dev_resource",
            description="Attach a dev resource (link to code, docs, ticket, etc.) to a node in a Figma file.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "node_id": {
                        "type": "string",
                        "description": "The node ID to attach the resource to",
                    },
                    "name": {
                        "type": "string",
                        "description": "Display name for the resource",
                    },
                    "url": {"type": "string", "description": "URL of the dev resource"},
                },
                "required": ["file_key", "node_id", "name", "url"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="update_figma_dev_resource",
            description="Update an existing dev resource's name or URL in a Figma file.",
            input_schema={
                "type": "object",
                "properties": {
                    "dev_resource_id": {
                        "type": "string",
                        "description": "The dev resource ID to update",
                    },
                    "file_key": {
                        "type": "string",
                        "description": "The file key the resource belongs to",
                    },
                    "name": {
                        "type": "string",
                        "description": "New display name (optional)",
                        "default": "",
                    },
                    "url": {
                        "type": "string",
                        "description": "New URL (optional)",
                        "default": "",
                    },
                },
                "required": ["dev_resource_id", "file_key"],
            },
            source="integration:figma",
            priority=1,
        ),
        ToolDefinition(
            name="delete_figma_dev_resource",
            description="Delete a dev resource from a Figma file.",
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {"type": "string", "description": "The file key"},
                    "dev_resource_id": {
                        "type": "string",
                        "description": "The dev resource ID to delete",
                    },
                },
                "required": ["file_key", "dev_resource_id"],
            },
            source="integration:figma",
            priority=1,
        ),
    ]
