# SPDX-License-Identifier: Apache-2.0

"""X (Twitter) integration — tweets, timelines, search, engagement.

API docs: https://developer.x.com/en/docs/x-api
Auth: OAuth 2.0 with PKCE (Authorization Code Flow)
Base URL: https://api.x.com/2

Endpoints used:
  - /2/tweets — create / delete / lookup tweets
  - /2/tweets/search/recent — search recent tweets (7 days)
  - /2/users/me — authenticated user profile
  - /2/users/:id/tweets — user timeline
  - /2/users/:id/mentions — user mentions
  - /2/users/:id/followers — user followers
  - /2/users/:id/following — user following
  - /2/users/:id/likes — user likes
  - /2/users/:id/bookmarks — user bookmarks
  - /2/dm_conversations — direct messages
  - /2/media/upload/initialize, /{id}/append, /{id}/finalize and
    /2/media/upload?command=STATUS — chunked media upload (``media.write``)
  - /2/media/metadata — alt text of an uploaded image
"""

from __future__ import annotations

import json as _json
from typing import Any

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import FetchError, deliver_file, with_extension
from app.integrations.http import (
    AutoRefreshClient,
    format_entity_list,
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

_PROVIDER = "X"
_BASE_URL = "https://api.x.com/2"
_UPLOAD_URL = f"{_BASE_URL}/media/upload"
# "Keep each segment at or below 5 MB (the server maximum is 8 MB)."
_CHUNK_BYTES = 4 * MIB

# Media introduction, "Size and duration limits": images 5 MB (JPG, PNG, GIF,
# WEBP), animated GIF 15 MB; a Post takes up to 4 photos, 1 animated GIF or 1
# video. Post video: 20 minutes / 8 GB by default, 125 minutes / 16 GB with
# X Premium, at least 0.5 seconds - X re-checks the posting user's
# entitlement when the Post is created. DM video: 140 s / 512 MB, 10 min / 1 GB
# with Premium.
_IMAGE = MediaRule(
    types=frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"}),
    types_label="JPG, PNG, WEBP or GIF",
    max_bytes=5 * MIB,
    size_label="5 MB",
)
_ANIMATED_GIF = MediaRule(
    types=frozenset({"image/gif"}),
    types_label="GIF",
    max_bytes=15 * MIB,
    size_label="15 MB",
)
_VIDEO_TYPES = frozenset({"video/mp4", "video/quicktime", "video/webm", "video/mp2t"})
_POST_MEDIA = MediaLimits(
    platform="X",
    image=_IMAGE,
    max_images=4,
    animated_gif=_ANIMATED_GIF,
    video=MediaRule(
        types=_VIDEO_TYPES,
        types_label="MP4, MOV, WebM or MPEG-TS",
        max_bytes=16 * 1024 * MIB,
        size_label="16 GB",
        min_seconds=0.5,
        max_seconds=125 * 60,
    ),
)
_DM_MEDIA = MediaLimits(
    platform="X",
    image=_IMAGE,
    max_images=1,
    unit="message",
    animated_gif=_ANIMATED_GIF,
    video=MediaRule(
        types=_VIDEO_TYPES,
        types_label="MP4, MOV, WebM or MPEG-TS",
        max_bytes=1024 * MIB,
        size_label="1 GB",
        min_seconds=0.5,
        max_seconds=10 * 60,
    ),
)
_MEDIA_CATEGORIES = {"image": "image", "animated_gif": "gif", "video": "video"}
_MEDIA_FIELDS = "media_key,type,url,preview_image_url,variants,alt_text,width,height,duration_ms"
_MEDIA_SCOPE_HINT = (
    "Uploading media needs the media.write OAuth scope: reconnect the X "
    "integration (Authorize) if it was connected before media uploads."
)


class XIntegration(BaseIntegration):
    provider_name = "x"
    display_name = "X"
    icon = "Twitter"
    description = "Connect X (Twitter) for social media management — tweets, search, and engagement."
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        return OAuthDefaults(
            authorize_url="https://twitter.com/i/oauth2/authorize",
            token_url="https://api.x.com/2/oauth2/token",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "tweet.read",
                "tweet.write",
                "users.read",
                "follows.read",
                "follows.write",
                "like.read",
                "like.write",
                "bookmark.read",
                "bookmark.write",
                "list.read",
                "list.write",
                "dm.read",
                "dm.write",
                "media.write",
                "offline.access",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/me",
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
                f"{_BASE_URL}/users/me",
                _PROVIDER,
                "get user info",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "user.fields": "id,name,username,description,profile_image_url,public_metrics"
                },
            )
            if resp is None:
                return {"error": err}
            return resp.json()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _tweet_media(tweet: dict, includes: dict | None) -> list[dict]:
    """The media objects of *tweet*, in its ``attachments.media_keys`` order."""
    keys = (tweet.get("attachments") or {}).get("media_keys") or []
    by_key = {
        m.get("media_key"): m for m in (includes or {}).get("media") or [] if isinstance(m, dict)
    }
    return [by_key[k] for k in keys if k in by_key]


def _media_url(media: dict) -> str:
    """A photo's URL, or the highest-bitrate MP4 variant of a video or animated GIF."""
    if media.get("type") == "photo":
        return str(media.get("url") or "")
    mp4s = [
        v
        for v in media.get("variants") or []
        if isinstance(v, dict) and v.get("content_type") == "video/mp4" and v.get("url")
    ]
    if not mp4s:
        return ""
    return str(max(mp4s, key=lambda v: int(v.get("bit_rate") or 0))["url"])


def _format_media(media: dict) -> str:
    parts = [f"  Media: {media.get('type', '?')} (media_key {media.get('media_key', '?')})"]
    if media.get("width") and media.get("height"):
        parts.append(f"{media['width']}x{media['height']}")
    if media.get("duration_ms"):
        parts.append(f"{round(int(media['duration_ms']) / 1000, 1)} s")
    url = _media_url(media)
    parts.append(f"Download URL: {url}" if url else "no download link")
    if media.get("preview_image_url"):
        parts.append(f"preview: {media['preview_image_url']}")
    if media.get("alt_text"):
        parts.append(f"alt: {media['alt_text']}")
    return " - ".join(parts)


def _media_tip(tweets: list[dict], includes: dict | None) -> str:
    if any(_tweet_media(t, includes) for t in tweets):
        return (
            "\n\nTip: download_x_media(tweet_id, media_key) brings a media into the "
            "conversation as a file (a download card and a file_id)."
        )
    return ""


def _format_tweet(tweet: dict, includes: dict | None = None) -> str:
    """Format a tweet into a readable string."""
    lines: list[str] = []
    tweet_id = tweet.get("id", "?")
    text = tweet.get("text", "")
    created = tweet.get("created_at", "")
    author_id = tweet.get("author_id", "")
    metrics = tweet.get("public_metrics", {})

    # Resolve author name from includes
    author_name = author_id
    if includes and "users" in includes:
        for u in includes["users"]:
            if u.get("id") == author_id:
                author_name = f"@{u.get('username', author_id)}"
                break

    lines.append(f"**{author_name}** ({created})")
    lines.append(f"  {text}")
    if metrics:
        parts = []
        for k, v in metrics.items():
            parts.append(f"{k.replace('_count', '').replace('_', ' ')}: {v}")
        lines.append(f"  [{' · '.join(parts)}]")
    lines.extend(_format_media(m) for m in _tweet_media(tweet, includes))
    lines.append(f"  ID: {tweet_id}")
    return "\n".join(lines)


def _format_user(u: dict) -> str:
    """Format an X user into a single-line string for list display."""
    m = u.get("public_metrics", {})
    return f"• **{u.get('name')}** (@{u.get('username')}) — {m.get('followers_count', 0)} followers"


# ---------------------------------------------------------------------------
# X tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build X (Twitter) integration tools.

    ``token`` is the OAuth 2.0 access token.
    """
    refresh_fn = _kwargs.get("refresh_fn")
    persist_file_fn = _kwargs.get("persist_file_fn")
    hdrs = _headers(token)

    def _client(timeout: int = 30) -> AutoRefreshClient:
        return AutoRefreshClient(hdrs, refresh_fn, timeout=timeout)

    async def _upload(client: AutoRefreshClient, media: PostMedia, *, dm: bool) -> tuple[str, str | None]:
        """The media_id of *media* after the v2 chunked upload (and its processing), or the error."""
        # The stored name goes to X with the bytes; every message names the file by its id.
        name, label = media.filename, media.label
        category = ("dm_" if dm else "tweet_") + _MEDIA_CATEGORIES[media.kind]
        resp, err = await safe_request(
            client,
            "post",
            f"{_UPLOAD_URL}/initialize",
            _PROVIDER,
            f"initialize the upload of {label}",
            content=_json.dumps(
                {
                    "media_type": media.content_type,
                    "total_bytes": len(media.data),
                    "media_category": category,
                }
            ),
            expected_statuses={403},
        )
        if err:
            return "", (f"{err} {_MEDIA_SCOPE_HINT}" if resp is not None else err)
        media_id = str((resp.json().get("data") or {}).get("id") or "")
        if not media_id:
            return "", f"Error: X returned no media_id for {label}."
        def multipart_headers() -> dict[str, str]:
            # The client's JSON Content-Type must not ride along with a
            # multipart body, and the bearer is read per request: a refresh
            # mid-upload updates ``hdrs`` in place.
            return {"Authorization": hdrs["Authorization"]}

        for index, offset in enumerate(range(0, len(media.data), _CHUNK_BYTES)):
            _, err = await safe_request(
                client,
                "post",
                f"{_UPLOAD_URL}/{media_id}/append",
                _PROVIDER,
                f"upload segment {index} of {label}",
                headers=multipart_headers(),
                data={"segment_index": str(index)},
                files={"media": (name, media.data[offset : offset + _CHUNK_BYTES], "application/octet-stream")},
            )
            if err:
                return "", err
        resp, err = await safe_request(
            client,
            "post",
            f"{_UPLOAD_URL}/{media_id}/finalize",
            _PROVIDER,
            f"finalize the upload of {label}",
            headers=multipart_headers(),
        )
        if err:
            return "", err
        info = (resp.json().get("data") or {}).get("processing_info")
        waited = 0
        while isinstance(info, dict) and info.get("state") in ("pending", "in_progress"):
            if waited >= social_media.VIDEO_PROCESSING_WAIT_SECONDS:
                return "", (
                    f"Error: X was still processing {label} after {waited} s. Nothing has "
                    "been published - try again in a few minutes."
                )
            delay = max(1, min(int(info.get("check_after_secs") or 3), 30))
            await social_media.pause(delay)
            waited += delay
            resp, err = await safe_request(
                client,
                "get",
                _UPLOAD_URL,
                _PROVIDER,
                f"check the processing of {label}",
                params={"command": "STATUS", "media_id": media_id},
            )
            if err:
                return "", err
            info = (resp.json().get("data") or {}).get("processing_info")
        if isinstance(info, dict) and info.get("state") == "failed":
            reason = (info.get("error") or {}).get("message") or "no reason given"
            return "", f"Error: X could not process {label} ({reason}). Nothing has been published."
        if media.alt_text and media.kind != "video":
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/media/metadata",
                _PROVIDER,
                f"set the alt text of {label}",
                content=_json.dumps(
                    {"id": media_id, "metadata": {"alt_text": {"text": media.alt_text[:1000]}}}
                ),
            )
            if err:
                return "", err
        return media_id, None

    async def _read_media(
        file_context: FileContext | None, file_ids: Any, limits: MediaLimits, alt: Any
    ) -> list[PostMedia] | ToolResult:
        checked = await read_post_media(file_context, file_ids, limits, alt_text=alt)
        if isinstance(checked, str):
            return ToolResult(tool_call_id="", content=checked, is_error=True)
        return checked

    _me_cache: dict[str, str] = {}

    async def _get_my_id() -> str:
        """Get the authenticated user's ID (cached)."""
        if "id" not in _me_cache:
            async with _client() as client:
                resp, _ = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/users/me",
                    _PROVIDER,
                    "get user ID",
                )
                if resp:
                    _me_cache["id"] = resp.json().get("data", {}).get("id", "")
        return _me_cache.get("id", "")

    # ── Get my profile ────────────────────────────────────────────────────

    async def get_x_profile() -> str:
        """Get the authenticated X (Twitter) user's profile."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/me",
                _PROVIDER,
                "get profile",
                params={
                    "user.fields": "id,name,username,description,profile_image_url,public_metrics,created_at,location,url,verified"
                },
            )
            if err:
                return err
            data = resp.json().get("data", {})
            metrics = data.get("public_metrics", {})
            lines = [
                f"**{data.get('name', 'Unknown')}** (@{data.get('username', '?')})",
                f"  ID: {data.get('id', '?')}",
                f"  Bio: {data.get('description', 'N/A')}",
                f"  Followers: {metrics.get('followers_count', 0)} · Following: {metrics.get('following_count', 0)}",
                f"  Tweets: {metrics.get('tweet_count', 0)}",
                f"  Joined: {data.get('created_at', 'N/A')}",
            ]
            return "\n".join(lines)

    # ── Get user profile ──────────────────────────────────────────────────

    async def get_x_user_profile(username: str) -> str:
        """Get an X (Twitter) user's profile by username."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/by/username/{username}",
                _PROVIDER,
                f"get profile for @{username}",
                params={
                    "user.fields": "id,name,username,description,profile_image_url,public_metrics,created_at,location,url,verified"
                },
            )
            if err:
                return err
            data = resp.json().get("data", {})
            metrics = data.get("public_metrics", {})
            lines = [
                f"**{data.get('name', 'Unknown')}** (@{data.get('username', '?')})",
                f"  ID: {data.get('id', '?')}",
                f"  Bio: {data.get('description', 'N/A')}",
                f"  Followers: {metrics.get('followers_count', 0)} · Following: {metrics.get('following_count', 0)}",
                f"  Tweets: {metrics.get('tweet_count', 0)}",
            ]
            return "\n".join(lines)

    # ── Create tweet ──────────────────────────────────────────────────────

    async def create_x_tweet(
        text: str,
        reply_to_tweet_id: str = "",
        quote_tweet_id: str = "",
        media_file_ids: Any = None,
        media_alt_texts: Any = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Post a tweet, optionally with files of the run. Optionally reply to or quote another tweet."""
        media = await _read_media(file_context, media_file_ids, _POST_MEDIA, media_alt_texts)
        if isinstance(media, ToolResult):
            return media
        body: dict[str, Any] = {"text": text}
        if reply_to_tweet_id:
            body["reply"] = {"in_reply_to_tweet_id": reply_to_tweet_id}
        if quote_tweet_id:
            body["quote_tweet_id"] = quote_tweet_id

        async with _client(social_media.UPLOAD_TIMEOUT_SECONDS if media else 30) as client:
            media_ids: list[str] = []
            for item in media:
                media_id, err = await _upload(client, item, dm=False)
                if err:
                    return with_summaries(f"{err}\nNothing has been published.", media)
                media_ids.append(media_id)
            if media_ids:
                body["media"] = {"media_ids": media_ids}
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/tweets",
                _PROVIDER,
                "create tweet",
                content=_json.dumps(body),
            )
            if err:
                return with_summaries(err, media)
            data = resp.json().get("data", {})
            tweet_id = data.get("id", "?")
            answer = f"Tweet posted successfully. Tweet ID: {tweet_id}"
            if media_ids:
                answer += (
                    f"\nURL: https://x.com/i/status/{tweet_id}"
                    f"\nMedia: {media_kinds(media)} (media_ids {', '.join(media_ids)})"
                )
            return with_summaries(answer, media)

    # ── Delete tweet ──────────────────────────────────────────────────────

    async def delete_x_tweet(tweet_id: str) -> str:
        """Delete a tweet by ID."""
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/tweets/{tweet_id}",
                _PROVIDER,
                "delete tweet",
            )
            if err:
                return err
            return f"Tweet {tweet_id} deleted successfully."

    # ── Get tweet ─────────────────────────────────────────────────────────

    async def get_x_tweet(tweet_id: str) -> str:
        """Get a tweet by ID with full details and metrics."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tweets/{tweet_id}",
                _PROVIDER,
                "get tweet",
                params={
                    "tweet.fields": "id,text,created_at,author_id,public_metrics,conversation_id,in_reply_to_user_id,attachments",
                    "expansions": "author_id,attachments.media_keys",
                    "user.fields": "name,username",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
            if err:
                return err
            body = resp.json()
            tweet = body.get("data", {})
            includes = body.get("includes")
            return (
                _format_tweet(tweet, includes)
                + f"\n  URL: https://x.com/i/status/{tweet.get('id', '?')}"
                + _media_tip([tweet], includes)
            )

    async def download_x_media(tweet_id: str, media_key: str = "") -> str | ToolResult:
        """Download a photo, video or animated GIF of a tweet into the conversation."""
        tweet_id = (tweet_id or "").strip()
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tweets/{tweet_id}",
                _PROVIDER,
                "get tweet media",
                params={
                    "tweet.fields": "attachments",
                    "expansions": "attachments.media_keys",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
        if err:
            return err
        body = resp.json()
        media_list = _tweet_media(body.get("data") or {}, body.get("includes"))
        if not media_list:
            return f"Tweet {tweet_id} carries no media."
        wanted = (media_key or "").strip()
        if wanted:
            chosen = next((m for m in media_list if m.get("media_key") == wanted), None)
            if chosen is None:
                keys = ", ".join(str(m.get("media_key")) for m in media_list)
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: tweet {tweet_id} has no media {wanted} (its media_keys: {keys}).",
                    is_error=True,
                )
        elif len(media_list) == 1:
            chosen = media_list[0]
        else:
            keys = ", ".join(str(m.get("media_key")) for m in media_list)
            return ToolResult(
                tool_call_id="",
                content=f"Error: tweet {tweet_id} carries {len(media_list)} media - name one media_key ({keys}).",
                is_error=True,
            )
        url = _media_url(chosen)
        if not url:
            return f"X gives no download link for media {chosen.get('media_key')} of tweet {tweet_id}."
        fetched = await fetch_public_media(url, source="X")
        if isinstance(fetched, FetchError):
            return fetched
        data, declared = fetched.data, fetched.content_type
        content_type = media_type(data, declared, fetched.filename or url.split("?", 1)[0])
        return await deliver_file(
            data=data,
            filename=with_extension(f"x-{tweet_id}-{chosen.get('media_key', 'media')}", content_type),
            content_type=content_type,
            source="X",
            persist_file_fn=persist_file_fn,
            links=[
                ("Web URL", f"https://x.com/i/status/{tweet_id}"),
                ("Download URL", url),
                ("Preview URL", chosen.get("preview_image_url")),
            ],
            details=[f"Alt text: {chosen['alt_text']}"] if chosen.get("alt_text") else (),
        )

    # ── Search recent tweets ──────────────────────────────────────────────

    async def search_x_tweets(
        query: str,
        max_results: int = 15,
        sort_order: str = "recency",
    ) -> str:
        """Search recent tweets (past 7 days) by keyword or query."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/tweets/search/recent",
                _PROVIDER,
                "search tweets",
                params={
                    "query": query,
                    "max_results": str(min(max(max_results, 10), 100)),
                    "sort_order": sort_order,
                    "tweet.fields": "id,text,created_at,author_id,public_metrics,attachments",
                    "expansions": "author_id,attachments.media_keys",
                    "user.fields": "name,username",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
            if err:
                return err
            body = resp.json()
            tweets = body.get("data", [])
            includes = body.get("includes")
            if not tweets:
                return f"No recent tweets found for '{query}'."
            results = [_format_tweet(t, includes) for t in tweets]
            meta = body.get("meta", {})
            total = meta.get("result_count", len(tweets))
            return (
                f"Found {total} recent tweets for '{query}':\n\n"
                + "\n\n".join(results)
                + _media_tip(tweets, includes)
            )

    # ── Get user tweets (timeline) ────────────────────────────────────────

    async def list_x_user_tweets(
        username: str = "", user_id: str = "", max_results: int = 15
    ) -> str:
        """Get recent tweets from a user's timeline by username or user ID."""
        target_id = user_id
        if not target_id and username:
            async with _client() as client:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/users/by/username/{username}",
                    _PROVIDER,
                    f"find user @{username}",
                )
                if err:
                    return err
                target_id = resp.json().get("data", {}).get("id", "")
        if not target_id:
            target_id = await _get_my_id()
        if not target_id:
            return "Error: Could not determine user ID."

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/{target_id}/tweets",
                _PROVIDER,
                "get user tweets",
                params={
                    "max_results": str(min(max(max_results, 5), 100)),
                    "tweet.fields": "id,text,created_at,public_metrics,attachments",
                    "expansions": "attachments.media_keys",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
            if err:
                return err
            body = resp.json()
            tweets = body.get("data", [])
            includes = body.get("includes")
            if not tweets:
                return "No tweets found."
            results = [_format_tweet(t, includes) for t in tweets]
            return (
                f"Recent tweets ({len(tweets)}):\n\n"
                + "\n\n".join(results)
                + _media_tip(tweets, includes)
            )

    # ── Get mentions ──────────────────────────────────────────────────────

    async def list_x_mentions(max_results: int = 15) -> str:
        """Get recent mentions of the authenticated user."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/{my_id}/mentions",
                _PROVIDER,
                "get mentions",
                params={
                    "max_results": str(min(max(max_results, 5), 100)),
                    "tweet.fields": "id,text,created_at,author_id,public_metrics,attachments",
                    "expansions": "author_id,attachments.media_keys",
                    "user.fields": "name,username",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
            if err:
                return err
            body = resp.json()
            tweets = body.get("data", [])
            includes = body.get("includes")
            if not tweets:
                return "No recent mentions."
            results = [_format_tweet(t, includes) for t in tweets]
            return (
                f"Recent mentions ({len(tweets)}):\n\n"
                + "\n\n".join(results)
                + _media_tip(tweets, includes)
            )

    # ── Like / Unlike tweet ───────────────────────────────────────────────

    async def like_x_tweet(tweet_id: str) -> str:
        """Like a tweet."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/users/{my_id}/likes",
                _PROVIDER,
                "like tweet",
                content=_json.dumps({"tweet_id": tweet_id}),
            )
            if err:
                return err
            return f"Tweet {tweet_id} liked."

    async def unlike_x_tweet(tweet_id: str) -> str:
        """Unlike a previously liked tweet."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/users/{my_id}/likes/{tweet_id}",
                _PROVIDER,
                "unlike tweet",
            )
            if err:
                return err
            return f"Tweet {tweet_id} unliked."

    # ── Retweet / Unretweet ───────────────────────────────────────────────

    async def retweet_x(tweet_id: str) -> str:
        """Retweet a tweet."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/users/{my_id}/retweets",
                _PROVIDER,
                "retweet",
                content=_json.dumps({"tweet_id": tweet_id}),
            )
            if err:
                return err
            return f"Tweet {tweet_id} retweeted."

    async def unretweet_x(tweet_id: str) -> str:
        """Undo a retweet."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/users/{my_id}/retweets/{tweet_id}",
                _PROVIDER,
                "unretweet",
            )
            if err:
                return err
            return f"Retweet of {tweet_id} removed."

    # ── Follow / Unfollow ─────────────────────────────────────────────────

    async def follow_x_user(target_user_id: str) -> str:
        """Follow a user by their user ID."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/users/{my_id}/following",
                _PROVIDER,
                "follow user",
                content=_json.dumps({"target_user_id": target_user_id}),
            )
            if err:
                return err
            return f"Now following user {target_user_id}."

    async def unfollow_x_user(target_user_id: str) -> str:
        """Unfollow a user by their user ID."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/users/{my_id}/following/{target_user_id}",
                _PROVIDER,
                "unfollow user",
            )
            if err:
                return err
            return f"Unfollowed user {target_user_id}."

    # ── Bookmarks ─────────────────────────────────────────────────────────

    async def list_x_bookmarks(max_results: int = 15) -> str:
        """Get the authenticated user's bookmarked tweets."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/{my_id}/bookmarks",
                _PROVIDER,
                "get bookmarks",
                params={
                    "max_results": str(min(max(max_results, 5), 100)),
                    "tweet.fields": "id,text,created_at,author_id,public_metrics,attachments",
                    "expansions": "author_id,attachments.media_keys",
                    "user.fields": "name,username",
                    "media.fields": _MEDIA_FIELDS,
                },
            )
            if err:
                return err
            body = resp.json()
            tweets = body.get("data", [])
            includes = body.get("includes")
            if not tweets:
                return "No bookmarks found."
            results = [_format_tweet(t, includes) for t in tweets]
            return (
                f"Bookmarks ({len(tweets)}):\n\n"
                + "\n\n".join(results)
                + _media_tip(tweets, includes)
            )

    async def add_x_bookmark(tweet_id: str) -> str:
        """Bookmark a tweet."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/users/{my_id}/bookmarks",
                _PROVIDER,
                "add bookmark",
                content=_json.dumps({"tweet_id": tweet_id}),
            )
            if err:
                return err
            return f"Tweet {tweet_id} bookmarked."

    async def remove_x_bookmark(tweet_id: str) -> str:
        """Remove a tweet from bookmarks."""
        my_id = await _get_my_id()
        if not my_id:
            return "Error: Could not determine user ID."
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/users/{my_id}/bookmarks/{tweet_id}",
                _PROVIDER,
                "remove bookmark",
            )
            if err:
                return err
            return f"Bookmark for tweet {tweet_id} removed."

    # ── Send DM ───────────────────────────────────────────────────────────

    async def send_x_dm(
        participant_id: str,
        text: str = "",
        media_file_id: str = "",
        media_alt_text: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Send a direct message to a user by their user ID, optionally with one file of the run."""
        media = await _read_media(file_context, media_file_id, _DM_MEDIA, media_alt_text)
        if isinstance(media, ToolResult):
            return media
        if not (text or "").strip() and not media:
            return ToolResult(
                tool_call_id="", content="Error: a DM needs text or a media_file_id.", is_error=True
            )
        body: dict[str, Any] = {"text": text} if (text or "").strip() else {}
        async with _client(social_media.UPLOAD_TIMEOUT_SECONDS if media else 30) as client:
            if media:
                media_id, err = await _upload(client, media[0], dm=True)
                if err:
                    return with_summaries(f"{err}\nNothing has been sent.", media)
                body["attachments"] = [{"media_id": media_id}]
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/dm_conversations/with/{participant_id}/messages",
                _PROVIDER,
                "send DM",
                content=_json.dumps(body),
            )
            if err:
                return with_summaries(err, media)
            data = resp.json().get("data", {})
            answer = f"DM sent. Message ID: {data.get('dm_event_id', '?')}"
            if media:
                answer += f" Media: {media_kinds(media)}."
            return with_summaries(answer, media)

    # ── Get followers / following ─────────────────────────────────────────

    async def list_x_followers(username: str = "", max_results: int = 50) -> str:
        """Get followers of the authenticated user or a specific user by username."""
        target_id = ""
        if username:
            async with _client() as client:
                resp, _ = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/users/by/username/{username}",
                    _PROVIDER,
                    f"find user @{username}",
                )
                if resp:
                    target_id = resp.json().get("data", {}).get("id", "")
        if not target_id:
            target_id = await _get_my_id()
        if not target_id:
            return "Error: Could not determine user ID."

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/{target_id}/followers",
                _PROVIDER,
                "get followers",
                params={
                    "max_results": str(min(max(max_results, 1), 1000)),
                    "user.fields": "name,username,description,public_metrics",
                },
            )
            if err:
                return err
            body = resp.json()
            users = body.get("data", [])
            meta = body.get("meta", {})
            total = meta.get("result_count", len(users))
            return format_entity_list(users, _format_user, noun="follower", total=total)

    async def list_x_following(username: str = "", max_results: int = 50) -> str:
        """Get accounts followed by the authenticated user or a specific user."""
        target_id = ""
        if username:
            async with _client() as client:
                resp, _ = await safe_request(
                    client,
                    "get",
                    f"{_BASE_URL}/users/by/username/{username}",
                    _PROVIDER,
                    f"find user @{username}",
                )
                if resp:
                    target_id = resp.json().get("data", {}).get("id", "")
        if not target_id:
            target_id = await _get_my_id()
        if not target_id:
            return "Error: Could not determine user ID."

        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/users/{target_id}/following",
                _PROVIDER,
                "get following",
                params={
                    "max_results": str(min(max(max_results, 1), 1000)),
                    "user.fields": "name,username,description,public_metrics",
                },
            )
            if err:
                return err
            users = resp.json().get("data", [])
            return format_entity_list(
                users,
                _format_user,
                noun="followed account",
                empty_msg="Not following anyone.",
            )

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("get_x_profile", get_x_profile)
    executor.register("get_x_user_profile", get_x_user_profile)
    executor.register("create_x_tweet", create_x_tweet)
    executor.register("delete_x_tweet", delete_x_tweet)
    executor.register("get_x_tweet", get_x_tweet)
    executor.register("download_x_media", download_x_media)
    executor.register("search_x_tweets", search_x_tweets)
    executor.register("list_x_user_tweets", list_x_user_tweets)
    executor.register("list_x_mentions", list_x_mentions)
    executor.register("like_x_tweet", like_x_tweet)
    executor.register("unlike_x_tweet", unlike_x_tweet)
    executor.register("retweet_x", retweet_x)
    executor.register("unretweet_x", unretweet_x)
    executor.register("follow_x_user", follow_x_user)
    executor.register("unfollow_x_user", unfollow_x_user)
    executor.register("list_x_bookmarks", list_x_bookmarks)
    executor.register("add_x_bookmark", add_x_bookmark)
    executor.register("remove_x_bookmark", remove_x_bookmark)
    executor.register("send_x_dm", send_x_dm)
    executor.register("list_x_followers", list_x_followers)
    executor.register("list_x_following", list_x_following)

    source = "integration:x"
    return [
        ToolDefinition(
            name="get_x_profile",
            description="Get the authenticated X (Twitter) user's profile — name, username, bio, follower/following counts, tweet count.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_x_user_profile",
            description="Get an X (Twitter) user's profile by username.",
            input_schema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "X username (without @)",
                    }
                },
                "required": ["username"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_x_tweet",
            description=(
                "Post a tweet on X. Optionally reply to or quote another tweet, and attach files "
                "of this conversation by file_id (attached, generated or downloaded): up to 4 "
                "images (JPG, PNG, WEBP or GIF, 5 MB each), OR one animated GIF (15 MB), OR one "
                "video (MP4, MOV, WebM or MPEG-TS, at least 0.5 s; up to 20 minutes / 8 GB, or "
                "125 minutes / 16 GB for an X Premium account - X re-checks the account when "
                "the tweet is created). A file outside these limits is refused before anything "
                "is published; the result carries the visual summary of every image published. "
                "Needs the media.write scope (reconnect an integration authorized before it)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Tweet text (max 280 characters)",
                    },
                    "reply_to_tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to reply to (optional)",
                    },
                    "quote_tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to quote (optional)",
                    },
                    "media_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "file_ids of files of this conversation to attach (optional)",
                    },
                    "media_alt_texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alt text of each image (max 1000 characters), in the order of media_file_ids",
                    },
                },
                "required": ["text"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="download_x_media",
            workspace_ingest=True,
            description=(
                "Download a photo, video or animated GIF of a tweet into the conversation: a "
                "download card, its download link and a file_id to forward to any tool that "
                "takes a file (up to 100 MB; a video is its highest-bitrate MP4). Tweets read "
                "with the X tools list their media with media_key and download link. Include "
                "the returned [[FILE:...]] marker verbatim so the user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "Tweet ID"},
                    "media_key": {
                        "type": "string",
                        "description": "media_key of the media (optional when the tweet has one media)",
                    },
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="delete_x_tweet",
            description="Delete a tweet by ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "ID of tweet to delete",
                    }
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_x_tweet",
            description="Get a tweet by ID with full text, author, creation date, engagement metrics, URL, and its media (media_key, download link, preview, alt text).",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to retrieve",
                    }
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="search_x_tweets",
            description="Search recent tweets (past 7 days) by keyword, hashtag, or advanced query. Supports operators like from:, to:, #hashtag, is:reply, is:retweet, lang:.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports X search operators)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results (10-100)",
                        "default": 15,
                    },
                    "sort_order": {
                        "type": "string",
                        "description": "Sort: recency or relevancy",
                        "default": "recency",
                        "enum": ["recency", "relevancy"],
                    },
                },
                "required": ["query"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="list_x_user_tweets",
            description="Get recent tweets from a user's timeline.",
            input_schema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "X username (without @). If empty, uses authenticated user.",
                    },
                    "user_id": {
                        "type": "string",
                        "description": "User ID (alternative to username)",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of tweets (5-100)",
                        "default": 15,
                    },
                },
                "required": [],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="list_x_mentions",
            description="Get recent tweets mentioning the authenticated user.",
            input_schema={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Number of mentions (5-100)",
                        "default": 15,
                    }
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="like_x_tweet",
            description="Like a tweet.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "Tweet ID to like"}
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="unlike_x_tweet",
            description="Unlike a previously liked tweet.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "Tweet ID to unlike"}
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="retweet_x",
            description="Retweet a tweet.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {"type": "string", "description": "Tweet ID to retweet"}
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="unretweet_x",
            description="Undo a retweet.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to unretweet",
                    }
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="follow_x_user",
            description="Follow a user on X.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_user_id": {
                        "type": "string",
                        "description": "User ID to follow",
                    }
                },
                "required": ["target_user_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="unfollow_x_user",
            description="Unfollow a user on X.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_user_id": {
                        "type": "string",
                        "description": "User ID to unfollow",
                    }
                },
                "required": ["target_user_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_x_bookmarks",
            description="Get the authenticated user's bookmarked tweets.",
            input_schema={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Number of bookmarks (5-100)",
                        "default": 15,
                    }
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="add_x_bookmark",
            description="Bookmark a tweet.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to bookmark",
                    }
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="remove_x_bookmark",
            description="Remove a tweet from bookmarks.",
            input_schema={
                "type": "object",
                "properties": {
                    "tweet_id": {
                        "type": "string",
                        "description": "Tweet ID to unbookmark",
                    }
                },
                "required": ["tweet_id"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="send_x_dm",
            description=(
                "Send a direct message to a user on X, optionally with one file of this "
                "conversation by file_id: an image (JPG, PNG, WEBP or GIF, 5 MB), an animated "
                "GIF (15 MB) or a video (MP4, MOV, WebM or MPEG-TS; 140 s / 512 MB, or "
                "10 minutes / 1 GB with X Premium)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "participant_id": {
                        "type": "string",
                        "description": "User ID to send DM to",
                    },
                    "text": {"type": "string", "description": "Message text (optional with a media)"},
                    "media_file_id": {
                        "type": "string",
                        "description": "file_id of a file of this conversation to attach (optional)",
                    },
                    "media_alt_text": {
                        "type": "string",
                        "description": "Alt text of the attached image (optional)",
                    },
                },
                "required": ["participant_id"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="list_x_followers",
            description="Get followers of the authenticated user or a specific user.",
            input_schema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Username (without @). If empty, uses authenticated user.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results (1-1000)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_x_following",
            description="Get accounts followed by the authenticated user or a specific user.",
            input_schema={
                "type": "object",
                "properties": {
                    "username": {
                        "type": "string",
                        "description": "Username (without @). If empty, uses authenticated user.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results (1-1000)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
    ]
