# SPDX-License-Identifier: Apache-2.0

"""Facebook integration — personal profiles and pages.

API docs: https://developers.facebook.com/docs/graph-api/
Auth: OAuth 2.0
Base URL: https://graph.facebook.com/v21.0

Supports two account modes:
  - **Personal**: Post and manage content on the user's personal timeline.
  - **Page**: Post and manage content as a Facebook Page.
    The ``account_type`` field in ``extra_config`` selects the mode.
    After OAuth, administered pages are fetched and the page access token
    is stored in extra_config for page-level operations.

Endpoints used:
  - /me — user profile
  - /me/accounts — list administered pages (with page access tokens)
  - /{page-id}/feed — page posts (a multi-photo post through ``attached_media``)
  - /{page-id}/photos — photo posts and unpublished photos
  - graph-video.facebook.com/{page-id}/videos — video posts
  - /{page-id}/insights — page analytics
  - /{post-id}/comments — post comments (a photo comment through ``source``)
  - /{post-id}/insights — post engagement metrics

Photos and videos are published to a Page only: the Graph API publishes
media with a Page access token (Pages API "Publish Media Posts"), and no
permission lets an app publish to a personal timeline.
"""

from __future__ import annotations

import json as _json
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import FetchError, deliver_file, with_extension
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.integrations.providers import social_media
from app.integrations.providers.social_media import (
    MIB,
    MediaLimits,
    MediaRule,
    PostMedia,
    fetch_public_media,
    media_kinds,
    media_type,
    read_post_media,
    with_summaries,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Facebook"
_BASE_URL = "https://graph.facebook.com/v21.0"
_VIDEO_URL = "https://graph-video.facebook.com/v21.0"

# Page Photos reference: "File type: .jpeg, .bmp, .png, .gif, .tiff. File
# size: Files can not exceed 10MB."
_PHOTO = MediaRule(
    types=frozenset({"image/jpeg", "image/bmp", "image/png", "image/gif", "image/tiff"}),
    types_label="JPEG, BMP, PNG, GIF or TIFF",
    max_bytes=10 * MIB,
    size_label="10 MB",
)
# A video sent as multipart form data "cannot exceed 1 GB in size and 20
# minutes in duration" (Graph API video upload reference).
_POST_MEDIA = MediaLimits(
    platform="Facebook",
    image=_PHOTO,
    max_images=10,
    video=MediaRule(
        types=frozenset({"video/*"}),
        types_label="a video file (MP4 or MOV recommended)",
        max_bytes=1024 * MIB,
        size_label="1 GB",
        max_seconds=20 * 60,
    ),
)
# Object Comments reference: ``source`` is "A photo, encoded as form data".
_COMMENT_MEDIA = MediaLimits(platform="Facebook", image=_PHOTO, max_images=1, unit="comment")

_ATTACHMENT_FIELDS = "attachments{media_type,type,url,title,media,target,subattachments}"
_POST_FIELDS = f"id,message,story,created_time,permalink_url,full_picture,{_ATTACHMENT_FIELDS}"


class FacebookIntegration(BaseIntegration):
    provider_name = "facebook"
    display_name = "Facebook"
    icon = "Facebook"
    description = (
        "Connect Facebook for social media management — posts, pages, and insights."
    )
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
            token_url="https://graph.facebook.com/v21.0/oauth/access_token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "public_profile",
                "email",
                "pages_show_list",
                "pages_manage_posts",
                "pages_read_engagement",
                "pages_read_user_content",
                "pages_manage_metadata",
                "pages_manage_engagement",
                "read_insights",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "validate connection",
                params={"access_token": token},
            )
            return resp is not None

    async def get_user_info(self, token: str) -> dict:
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "get user info",
                params={"access_token": token, "fields": "id,name,email,picture"},
            )
            if resp is None:
                return {"error": err}
            return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_insights(data: list[dict], header: str) -> str:
    """Format a list of Facebook insights metrics into a readable string."""
    lines = [header]
    for metric in data:
        title = metric.get("title", metric.get("name", "?"))
        values = metric.get("values", [])
        if values:
            latest = values[-1].get("value", "N/A")
            lines.append(f"  {title}: {latest}")
    return "\n".join(lines)


def _flatten_attachments(attachments: list[dict]) -> list[dict]:
    """Every media item of a post's attachments; an album's subattachments one by one."""
    items: list[dict] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        subs = (attachment.get("subattachments") or {}).get("data") or []
        if subs:
            items.extend(s for s in subs if isinstance(s, dict))
        else:
            items.append(attachment)
    return items


def _post_media(post: dict) -> list[dict]:
    return _flatten_attachments((post.get("attachments") or {}).get("data") or [])


def _media_source(item: dict) -> str:
    """The file URL of an attachment: a video's ``source``, otherwise its image."""
    media = item.get("media") or {}
    return str(media.get("source") or (media.get("image") or {}).get("src") or "")


def _format_attachment(index: int, item: dict) -> str:
    kind = item.get("media_type") or item.get("type") or "attachment"
    parts = [f"  Media {index}: {kind}"]
    media = item.get("media") or {}
    if media.get("source"):
        parts.append(f"video: {media['source']}")
    image = (media.get("image") or {}).get("src")
    if image:
        parts.append(f"image: {image}")
    if item.get("url"):
        parts.append(f"page: {item['url']}")
    if item.get("title"):
        parts.append(f"title: {item['title']}")
    return " - ".join(parts)


def _format_fb_post(post: dict) -> str:
    """Format a Facebook post into a readable string, with the media it carries."""
    lines: list[str] = []
    post_id = post.get("id", "?")
    message = post.get("message", "(no text)")
    created = post.get("created_time", "")
    story = post.get("story", "")
    lines.append(f"**Post {post_id}** ({created})")
    if story:
        lines.append(f"  Story: {story}")
    if message:
        lines.append(f"  {message[:500]}")
    if post.get("permalink_url"):
        lines.append(f"  URL: {post['permalink_url']}")
    items = _post_media(post)
    lines.extend(_format_attachment(i, item) for i, item in enumerate(items, 1))
    if not items and post.get("full_picture"):
        lines.append(f"  Picture: {post['full_picture']}")
    return "\n".join(lines)


def _media_tip(posts: list[dict]) -> str:
    if any(_post_media(p) for p in posts):
        return (
            "\n\nTip: download_facebook_media(post_id=..., index=N) brings a media into the "
            "conversation as a file (a download card and a file_id)."
        )
    return ""


def _format_fb_page(p: dict) -> str:
    """Format a Facebook page into a single-line string for list display."""
    fans = p.get("fan_count", "?")
    return f"• **{p.get('name', '?')}** (ID: {p.get('id', '?')}) — {p.get('category', '?')} — {fans} fans"


def _format_fb_comment(c: dict) -> str:
    """Format a Facebook comment into a single-line string for list display, with its media."""
    author = c.get("from", {}).get("name", "Unknown")
    msg = c.get("message", "")
    created = c.get("created_time", "")
    likes = c.get("like_count", 0)
    line = f"• **{author}** ({created}) [{likes} likes]: {msg[:200]}"
    if c.get("id"):
        line += f" (comment ID: {c['id']})"
    attachment = c.get("attachment")
    if isinstance(attachment, dict) and _media_source(attachment):
        line += "\n  " + _format_attachment(1, attachment).strip()
    return line


# ---------------------------------------------------------------------------
# Facebook tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Facebook integration tools.

    ``token`` is ``user_access_token||account_type||page_id||page_access_token``.
    """
    access_token, account_type, page_id, page_token = parse_compound_token(
        token, 4, defaults=("", "personal", "", "")
    )

    persist_file_fn = _kwargs.get("persist_file_fn")

    def _active_token() -> str:
        """Return page token for page mode, user token otherwise."""
        return page_token if (account_type == "page" and page_token) else access_token

    async def _publish_media(
        pid: str, tok: str, message: str, media: list[PostMedia], media_title: str
    ) -> str:
        """Publish *media* on Page *pid*: one video, one photo, or several photos in one post."""
        timeout = social_media.UPLOAD_TIMEOUT_SECONDS
        first = media[0]
        if first.kind == "video":
            fields = {"access_token": tok, "description": message}
            if media_title:
                fields["title"] = media_title
            async with egress_client(timeout=timeout) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_VIDEO_URL}/{pid}/videos",
                    _PROVIDER,
                    f"publish video {first.label}",
                    data=fields,
                    files={"source": (first.filename, first.data, first.content_type)},
                )
            if err:
                return err
            video_id = resp.json().get("id", "?")
            return (
                f"Video post published on page {pid}. Video ID: {video_id}\n"
                f"URL: https://www.facebook.com/{pid}/videos/{video_id}"
            )
        if len(media) == 1:
            fields = {"access_token": tok, "caption": message}
            if first.alt_text:
                fields["alt_text_custom"] = first.alt_text
            async with egress_client(timeout=timeout) as client:
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/{pid}/photos",
                    _PROVIDER,
                    f"publish photo {first.label}",
                    data=fields,
                    files={"source": (first.filename, first.data, first.content_type)},
                )
            if err:
                return err
            data = resp.json()
            return (
                f"Photo post published on page {pid}. Post ID: {data.get('post_id', '?')} "
                f"(photo ID: {data.get('id', '?')})"
            )
        # Pages API: upload each photo unpublished, then one feed post with
        # attached_media.
        photo_ids: list[str] = []

        def _not_published(err: str) -> str:
            if not photo_ids:
                return f"{err}\nNothing has been published."
            return (
                f"{err}\nNothing has been published: {len(photo_ids)} photo(s) were uploaded "
                "unpublished (IDs " + ", ".join(photo_ids) + "), and Facebook deletes an "
                "unpublished photo no post uses within 24 hours."
            )

        async with egress_client(timeout=timeout) as client:
            for item in media:
                fields = {"access_token": tok, "published": "false"}
                if item.alt_text:
                    fields["alt_text_custom"] = item.alt_text
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{_BASE_URL}/{pid}/photos",
                    _PROVIDER,
                    f"upload photo {item.label}",
                    data=fields,
                    files={"source": (item.filename, item.data, item.content_type)},
                )
                if err:
                    return _not_published(err)
                photo_ids.append(str(resp.json().get("id", "")))
            post_fields = {"access_token": tok, "message": message}
            for index, photo_id in enumerate(photo_ids):
                post_fields[f"attached_media[{index}]"] = _json.dumps({"media_fbid": photo_id})
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{pid}/feed",
                _PROVIDER,
                "publish the photo post",
                data=post_fields,
            )
        if err:
            return _not_published(err)
        return (
            f"Post published on page {pid}. Post ID: {resp.json().get('id', '?')} "
            f"(photo IDs: {', '.join(photo_ids)})"
        )

    # ── Get my profile ────────────────────────────────────────────────────

    async def get_facebook_profile() -> str:
        """Get the authenticated Facebook user's profile."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/me",
                _PROVIDER,
                "get profile",
                params={
                    "access_token": access_token,
                    "fields": "id,name,email,picture",
                },
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"**{data.get('name', 'Unknown')}**",
                f"  ID: {data.get('id', '?')}",
                f"  Email: {data.get('email', 'N/A')}",
            ]
            pic = data.get("picture", {}).get("data", {}).get("url")
            if pic:
                lines.append(f"  Picture: {pic}")
            return "\n".join(lines)

    # ── List managed pages ────────────────────────────────────────────────

    async def list_facebook_pages() -> str:
        """List Facebook Pages managed by the authenticated user."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/me/accounts",
                _PROVIDER,
                "list pages",
                params={
                    "access_token": access_token,
                    "fields": "id,name,category,fan_count,link",
                },
            )
            if err:
                return err
            data = resp.json().get("data", [])
            return format_entity_list(
                data,
                _format_fb_page,
                noun="managed page",
                empty_msg="No Facebook Pages found.",
            )

    # ── Get page info ─────────────────────────────────────────────────────

    async def get_facebook_page_info(target_page_id: str = "") -> str:
        """Get details about a Facebook Page."""
        pid = target_page_id or page_id
        if not pid:
            return (
                "Error: No page ID provided. Use list_facebook_pages to find page IDs."
            )
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{pid}",
                _PROVIDER,
                "get page info",
                params={
                    "access_token": _active_token(),
                    "fields": "id,name,about,category,fan_count,followers_count,link,website,description,location,phone",
                },
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"**{data.get('name', 'Unknown')}**",
                f"  ID: {data.get('id', '?')}",
                f"  Category: {data.get('category', 'N/A')}",
                f"  Fans: {data.get('fan_count', 'N/A')}",
                f"  Followers: {data.get('followers_count', 'N/A')}",
            ]
            if data.get("about"):
                lines.append(f"  About: {data['about'][:300]}")
            if data.get("website"):
                lines.append(f"  Website: {data['website']}")
            if data.get("link"):
                lines.append(f"  Link: {data['link']}")
            return "\n".join(lines)

    # ── Create post ───────────────────────────────────────────────────────

    async def create_facebook_post(
        message: str,
        link: str = "",
        target_page_id: str = "",
        media_file_ids: Any = None,
        media_alt_texts: Any = None,
        media_title: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Create a Facebook post on a personal timeline or a Page.

        For page posts, provide target_page_id or use page mode.
        Optionally attach a link URL, or photos / one video of the run (Pages only).
        """
        pid = target_page_id or page_id
        # Use page token when posting to a page (explicit target or page mode)
        tok = _active_token() if pid else access_token

        if media_file_ids:
            refusal = ""
            if not pid:
                refusal = (
                    "Error: Facebook publishes photos and videos to a Page only - the Graph "
                    "API has no permission to publish to a personal timeline. Set "
                    "target_page_id (list_facebook_pages) or use page mode."
                )
            elif link:
                refusal = (
                    "Error: a Facebook post carries a link OR photos / a video, not both. Put "
                    "the link in the message, or drop link."
                )
            if refusal:
                return ToolResult(
                    tool_call_id="", content=f"{refusal} Nothing has been published.", is_error=True
                )
            checked = await read_post_media(
                file_context, media_file_ids, _POST_MEDIA, alt_text=media_alt_texts
            )
            if isinstance(checked, str):
                return ToolResult(tool_call_id="", content=checked, is_error=True)
            answer = await _publish_media(pid, tok, message, checked, media_title)
            if not answer.startswith("Error"):
                answer += f"\nMedia: {media_kinds(checked)}"
            return with_summaries(answer, checked)

        endpoint = f"{_BASE_URL}/{pid}/feed" if pid else f"{_BASE_URL}/me/feed"
        params: dict[str, str] = {"access_token": tok, "message": message}
        if link:
            params["link"] = link

        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "post",
                endpoint,
                _PROVIDER,
                "create post",
                data=params,
            )
            if err:
                return err
            data = resp.json()
            target = f"page {pid}" if pid else "your timeline"
            return f"Post published on {target}. Post ID: {data.get('id', '?')}"

    # ── Delete post ───────────────────────────────────────────────────────

    async def delete_facebook_post(post_id: str) -> str:
        """Delete a Facebook post by ID."""
        async with egress_client(timeout=15) as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/{post_id}",
                _PROVIDER,
                "delete post",
                params={"access_token": _active_token()},
            )
            if err:
                return err
            return f"Post {post_id} deleted."

    # ── Get post ──────────────────────────────────────────────────────────

    async def get_facebook_post(post_id: str) -> str:
        """Get a Facebook post by ID with details."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{post_id}",
                _PROVIDER,
                "get post",
                params={
                    "access_token": _active_token(),
                    "fields": f"{_POST_FIELDS},shares,from",
                },
            )
            if err:
                return err
            post = resp.json()
            return _format_fb_post(post) + _media_tip([post])

    # ── List posts ────────────────────────────────────────────────────────

    async def list_facebook_posts(target_page_id: str = "", limit: int = 15) -> str:
        """List recent posts from a page or the user's feed."""
        pid = target_page_id or page_id
        endpoint = f"{_BASE_URL}/{pid}/feed" if pid else f"{_BASE_URL}/me/feed"
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                endpoint,
                _PROVIDER,
                "list posts",
                params={
                    "access_token": _active_token(),
                    "fields": _POST_FIELDS,
                    "limit": str(min(limit, 100)),
                },
            )
            if err:
                return err
            posts = resp.json().get("data", [])
            if not posts:
                return "No posts found."
            results = [_format_fb_post(p) for p in posts]
            return f"Recent posts ({len(posts)}):\n\n" + "\n\n".join(results) + _media_tip(posts)

    # ── Download a post's or a comment's media ────────────────────────────

    async def download_facebook_media(
        post_id: str = "", comment_id: str = "", index: int = 1
    ) -> str | ToolResult:
        """Download a photo or video of a post (or a comment's photo) into the conversation."""
        post_id, comment_id = (post_id or "").strip(), (comment_id or "").strip()
        if bool(post_id) == bool(comment_id):
            return ToolResult(
                tool_call_id="",
                content="Error: provide exactly one of post_id or comment_id.",
                is_error=True,
            )
        object_id = post_id or comment_id
        fields = _ATTACHMENT_FIELDS if post_id else "attachment"
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{object_id}",
                _PROVIDER,
                "get media",
                params={"access_token": _active_token(), "fields": fields},
            )
        if err:
            return err
        body = resp.json()
        if post_id:
            items = _post_media(body)
        else:
            items = [body["attachment"]] if isinstance(body.get("attachment"), dict) else []
        if not items:
            return f"{'Post' if post_id else 'Comment'} {object_id} carries no media."
        position = int(index or 1)
        if not 1 <= position <= len(items):
            return ToolResult(
                tool_call_id="",
                content=f"Error: index {position} is out of range - {object_id} carries {len(items)} media.",
                is_error=True,
            )
        item = items[position - 1]
        url = _media_source(item)
        target_id = str((item.get("target") or {}).get("id") or "")
        kind = str(item.get("media_type") or item.get("type") or "")
        if not (item.get("media") or {}).get("source") and "video" in kind and target_id:
            # A video attachment may carry only its poster image: the video's
            # own node gives the file.
            async with egress_client(timeout=15) as client:
                video_resp, _ = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/{target_id}",
                    _PROVIDER,
                    "get video source",
                    params={"access_token": _active_token(), "fields": "source"},
                )
            source_url = str(video_resp.json().get("source") or "") if video_resp is not None else ""
            url = source_url or url
        if not url:
            return f"Facebook gives no download link for media {position} of {object_id}."
        fetched = await fetch_public_media(url, source="Facebook")
        if isinstance(fetched, FetchError):
            return fetched
        data, declared = fetched.data, fetched.content_type
        content_type = media_type(data, declared, fetched.filename or url.split("?", 1)[0])
        return await deliver_file(
            data=data,
            filename=with_extension(f"facebook-{object_id}-{position}", content_type),
            content_type=content_type,
            source="Facebook",
            persist_file_fn=persist_file_fn,
            links=[("Web URL", item.get("url")), ("Download URL", url)],
        )

    # ── Create comment ────────────────────────────────────────────────────

    async def _comment(
        object_id: str,
        message: str,
        image_file_id: str,
        file_context: FileContext | None,
        action: str,
    ) -> str | ToolResult:
        """Comment on *object_id* (a post, or a comment for a reply), with an optional photo."""
        media: list[PostMedia] = []
        if (image_file_id or "").strip():
            checked = await read_post_media(file_context, image_file_id, _COMMENT_MEDIA)
            if isinstance(checked, str):
                return ToolResult(tool_call_id="", content=checked, is_error=True)
            media = checked
        files = (
            {"source": (media[0].filename, media[0].data, media[0].content_type)} if media else None
        )
        async with egress_client(timeout=social_media.UPLOAD_TIMEOUT_SECONDS) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{object_id}/comments",
                _PROVIDER,
                action,
                data={"access_token": _active_token(), "message": message},
                files=files,
            )
        if err:
            return with_summaries(err, media)
        label = "Reply" if action == "reply to comment" else "Comment"
        return with_summaries(
            f"{label} posted. Comment ID: {resp.json().get('id', '?')}"
            + (" (with a photo)" if media else ""),
            media,
        )

    async def create_facebook_comment(
        post_id: str,
        message: str,
        image_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Comment on a Facebook post, optionally with a photo of the run."""
        return await _comment(post_id, message, image_file_id, file_context, "create comment")

    # ── Get comments ──────────────────────────────────────────────────────

    async def get_facebook_comments(post_id: str, limit: int = 25) -> str:
        """Get comments on a Facebook post."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{post_id}/comments",
                _PROVIDER,
                "get comments",
                params={
                    "access_token": _active_token(),
                    "fields": "id,message,from,created_time,like_count,attachment",
                    "limit": str(min(limit, 100)),
                },
            )
            if err:
                return err
            comments = resp.json().get("data", [])
            listing = format_entity_list(
                comments,
                _format_fb_comment,
                noun="comment",
                empty_msg="No comments on this post.",
            )
            if any(isinstance(c.get("attachment"), dict) for c in comments):
                listing += (
                    "\n\nTip: download_facebook_media(comment_id=...) brings a comment's "
                    "photo into the conversation."
                )
            return listing

    # ── Reply to comment ──────────────────────────────────────────────────

    async def reply_to_facebook_comment(
        comment_id: str,
        message: str,
        image_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Reply to a comment on a Facebook post, optionally with a photo of the run."""
        return await _comment(comment_id, message, image_file_id, file_context, "reply to comment")

    # ── Get page insights ─────────────────────────────────────────────────

    async def get_facebook_page_insights(
        target_page_id: str = "",
        period: str = "day",
        metrics: str = "page_impressions,page_engaged_users,page_fan_adds,page_views_total",
    ) -> str:
        """Get Facebook Page analytics — impressions, engagement, fan growth."""
        pid = target_page_id or page_id
        if not pid:
            return "Error: No page ID provided."
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{pid}/insights",
                _PROVIDER,
                "get page insights",
                params={
                    "access_token": _active_token(),
                    "metric": metrics,
                    "period": period,
                },
            )
            if err:
                return err
            data = resp.json().get("data", [])
            if not data:
                return "No insights data available."
            return _format_insights(
                data, f"Page insights for {pid} (period: {period}):"
            )

    # ── Get post insights ─────────────────────────────────────────────────

    async def get_facebook_post_insights(post_id: str) -> str:
        """Get engagement metrics for a Facebook post — impressions, reach, reactions, clicks."""
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/{post_id}/insights",
                _PROVIDER,
                "get post insights",
                params={
                    "access_token": _active_token(),
                    "metric": "post_impressions,post_engaged_users,post_clicks,post_reactions_by_type_total",
                },
            )
            if err:
                return err
            data = resp.json().get("data", [])
            if not data:
                return "No insights available for this post."
            return _format_insights(data, f"Post insights for {post_id}:")

    # ── Like a post ───────────────────────────────────────────────────────

    async def like_facebook_post(post_id: str) -> str:
        """Like a Facebook post."""
        async with egress_client(timeout=15) as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/{post_id}/likes",
                _PROVIDER,
                "like post",
                data={"access_token": _active_token()},
            )
            if err:
                return err
            return f"Post {post_id} liked."

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("get_facebook_profile", get_facebook_profile)
    executor.register("list_facebook_pages", list_facebook_pages)
    executor.register("get_facebook_page_info", get_facebook_page_info)
    executor.register("create_facebook_post", create_facebook_post)
    executor.register("delete_facebook_post", delete_facebook_post)
    executor.register("get_facebook_post", get_facebook_post)
    executor.register("list_facebook_posts", list_facebook_posts)
    executor.register("download_facebook_media", download_facebook_media)
    executor.register("create_facebook_comment", create_facebook_comment)
    executor.register("get_facebook_comments", get_facebook_comments)
    executor.register("reply_to_facebook_comment", reply_to_facebook_comment)
    executor.register("get_facebook_page_insights", get_facebook_page_insights)
    executor.register("get_facebook_post_insights", get_facebook_post_insights)
    executor.register("like_facebook_post", like_facebook_post)

    source = "integration:facebook"
    return [
        ToolDefinition(
            name="get_facebook_profile",
            description="Get the authenticated Facebook user's profile — name, email, picture.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_facebook_pages",
            description="List Facebook Pages managed by the authenticated user. Returns page IDs, names, categories, and fan counts.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="get_facebook_page_info",
            description="Get details about a Facebook Page — name, category, fans, followers, description, website.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_page_id": {
                        "type": "string",
                        "description": "Page ID. If omitted, uses the configured page.",
                    }
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_facebook_post",
            description=(
                "Create a Facebook post on a personal timeline or a Page. Optionally attach a "
                "link URL, or - on a Page only (the Graph API publishes media to Pages, never to "
                "a personal timeline) - files of this conversation by file_id: 1 to 10 photos "
                "(JPEG, BMP, PNG, GIF or TIFF, 10 MB each) OR one video (MP4 or MOV "
                "recommended, up to 1 GB and 20 minutes). A file outside these limits is "
                "refused before anything is published; the result carries the visual summary "
                "of every photo published."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Post text"},
                    "link": {
                        "type": "string",
                        "description": "URL to attach (optional; not combined with media)",
                    },
                    "target_page_id": {
                        "type": "string",
                        "description": "Page ID for page posts (overrides config)",
                    },
                    "media_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "file_ids of photos or one video of this conversation to publish (Page posts only)",
                    },
                    "media_alt_texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alt text of each photo, in the order of media_file_ids",
                    },
                    "media_title": {
                        "type": "string",
                        "description": "Title of a video post (optional)",
                    },
                },
                "required": ["message"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="download_facebook_media",
            workspace_ingest=True,
            description=(
                "Download a photo or video of a Facebook post (by post_id and its 1-based media "
                "index, as listed by get_facebook_post / list_facebook_posts), or the photo of a "
                "comment (by comment_id), into the conversation: a download card, its links and "
                "a file_id to forward to any tool that takes a file (up to 100 MB). Include the "
                "returned [[FILE:...]] marker verbatim so the user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "Post ID (or give comment_id)"},
                    "comment_id": {"type": "string", "description": "Comment ID (or give post_id)"},
                    "index": {
                        "type": "integer",
                        "description": "1-based media index within the post (default 1)",
                        "default": 1,
                    },
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="delete_facebook_post",
            description="Delete a Facebook post by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "Post ID to delete"}
                },
                "required": ["post_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_facebook_post",
            description="Get a Facebook post by ID with message, story, creation time, permalink, and its photos and videos (file and page links).",
            input_schema={
                "type": "object",
                "properties": {"post_id": {"type": "string", "description": "Post ID"}},
                "required": ["post_id"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="list_facebook_posts",
            description="List recent posts from a Facebook Page or the user's feed, with each post's photos and videos (file and page links).",
            input_schema={
                "type": "object",
                "properties": {
                    "target_page_id": {
                        "type": "string",
                        "description": "Page ID. If omitted, uses configured page or user feed.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of posts (max 100)",
                        "default": 15,
                    },
                },
                "required": [],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="create_facebook_comment",
            description=(
                "Comment on a Facebook post, optionally with one photo of this conversation by "
                "file_id (JPEG, BMP, PNG, GIF or TIFF, 10 MB)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {
                        "type": "string",
                        "description": "Post ID to comment on",
                    },
                    "message": {"type": "string", "description": "Comment text"},
                    "image_file_id": {
                        "type": "string",
                        "description": "file_id of a photo of this conversation to attach (optional)",
                    },
                },
                "required": ["post_id", "message"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_facebook_comments",
            description="Get comments on a Facebook post with comment ID, author, text, timestamp, like count, and the photo a comment carries.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "Post ID"},
                    "limit": {
                        "type": "integer",
                        "description": "Number of comments (max 100)",
                        "default": 25,
                    },
                },
                "required": ["post_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="reply_to_facebook_comment",
            description=(
                "Reply to a specific comment on a Facebook post, optionally with one photo of "
                "this conversation by file_id (JPEG, BMP, PNG, GIF or TIFF, 10 MB)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "comment_id": {
                        "type": "string",
                        "description": "Comment ID to reply to",
                    },
                    "message": {"type": "string", "description": "Reply text"},
                    "image_file_id": {
                        "type": "string",
                        "description": "file_id of a photo of this conversation to attach (optional)",
                    },
                },
                "required": ["comment_id", "message"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_facebook_page_insights",
            description="Get Facebook Page analytics — impressions, engagement, fan growth, page views. Periods: day, week, days_28.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_page_id": {
                        "type": "string",
                        "description": "Page ID. If omitted, uses configured page.",
                    },
                    "period": {
                        "type": "string",
                        "description": "Time period: day, week, or days_28",
                        "default": "day",
                        "enum": ["day", "week", "days_28"],
                    },
                    "metrics": {
                        "type": "string",
                        "description": "Comma-separated metrics",
                        "default": "page_impressions,page_engaged_users,page_fan_adds,page_views_total",
                    },
                },
                "required": [],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_facebook_post_insights",
            description="Get engagement metrics for a Facebook post — impressions, engaged users, clicks, reactions.",
            input_schema={
                "type": "object",
                "properties": {"post_id": {"type": "string", "description": "Post ID"}},
                "required": ["post_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="like_facebook_post",
            description="Like a Facebook post.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_id": {"type": "string", "description": "Post ID to like"}
                },
                "required": ["post_id"],
            },
            source=source,
            priority=1,
        ),
    ]
