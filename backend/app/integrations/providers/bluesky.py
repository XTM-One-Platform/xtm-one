# SPDX-License-Identifier: Apache-2.0

"""Bluesky integration — posts, timelines, search, social graph via the AT Protocol.

API docs: https://docs.bsky.app/docs/category/http-reference
Auth: App password (API key) — no OAuth
Base URL: https://bsky.social/xrpc (default, configurable via PDS URL)

Compound token format: ``handle||app_password`` or ``handle||app_password||pds_url``

The integration creates a session on the first tool call, caching the JWT
access token for the session's lifetime.

Endpoints used:
  - com.atproto.server.createSession — authenticate
  - app.bsky.feed.post — create/delete posts (via repo records)
  - app.bsky.feed.getTimeline — home timeline
  - app.bsky.feed.getAuthorFeed — user's posts
  - app.bsky.feed.getPostThread — full thread
  - app.bsky.feed.searchPosts — search
  - app.bsky.actor.getProfile — user profiles
  - app.bsky.actor.searchActors — search users
  - app.bsky.graph.getFollowers / getFollows — social graph
  - app.bsky.notification.listNotifications — notifications
  - com.atproto.repo.createRecord / deleteRecord — create/delete records
  - com.atproto.repo.uploadBlob — images and videos of a post
    (``app.bsky.embed.images`` / ``app.bsky.embed.video``)
  - app.bsky.feed.getPosts + com.atproto.sync.getBlob on the author's PDS
    (resolved from its DID document) — the original file of a post's media
"""

from __future__ import annotations

import json as _json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, unquote, urlsplit

from app.core.egress import egress_client
from app.integrations.file_delivery import FetchError, deliver_file, with_extension
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.integrations.providers import social_media
from app.integrations.providers.social_media import (
    MediaLimits,
    MediaRule,
    PostMedia,
    fetch_public_media,
    https_origin,
    media_kinds,
    media_type,
    read_post_media,
    with_summaries,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Bluesky"
_BASE_URL = "https://bsky.social"
_PLC_DIRECTORY = "https://plc.directory"

# The lexicons: app.bsky.embed.images holds up to 4 images, each a blob of
# "maxSize": 2000000 with alt text required; app.bsky.embed.video holds one
# "video/mp4" blob of "maxSize": 300000000.
_POST_MEDIA = MediaLimits(
    platform="Bluesky",
    image=MediaRule(
        types=frozenset({"image/jpeg", "image/png", "image/webp", "image/gif"}),
        types_label="JPG, PNG, WEBP or GIF",
        max_bytes=2_000_000,
        size_label="2 MB (2,000,000 bytes)",
    ),
    max_images=4,
    video=MediaRule(
        types=frozenset({"video/mp4"}),
        types_label="MP4",
        max_bytes=300_000_000,
        size_label="300 MB (300,000,000 bytes)",
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


def did_document_url(did: str) -> str | None:
    """Where the DID document of *did* is read; ``None`` for a DID atproto does not resolve.

    ``did:plc`` is read from ``plc.directory``, ``did:web`` from the host it
    names - which atproto accepts only as a hostname, its port percent-encoded,
    with no path and no IP address (https://atproto.com/specs/did).
    """
    if did.startswith("did:plc:"):
        return f"{_PLC_DIRECTORY}/{quote(did, safe=':')}"
    if not did.startswith("did:web:"):
        return None
    ident = did[len("did:web:") :]
    if ":" in ident:
        return None
    host, _, port = unquote(ident).lower().partition(":")
    labels = host.split(".")
    if (
        len(host) > 253
        or len(labels) < 2
        or labels[-1].isdigit()
        or not all(_HOST_LABEL.fullmatch(label) for label in labels)
    ):
        return None
    if port and not (port.isdigit() and 0 < int(port) < 65536):
        return None
    return f"https://{host}{f':{port}' if port else ''}/.well-known/did.json"


def pds_origin(endpoint: str) -> str:
    """The ``#atproto_pds`` endpoint of a DID document as a bare origin; ``""`` for anything else.

    atproto gives the endpoint as an HTTPS URL of a host and an optional port:
    one with a path, a query, a fragment or credentials is refused, so a blob
    URL built on the origin names exactly the host the outbound URL policy checks.
    """
    try:
        parsed = urlsplit(endpoint.strip())
    except ValueError:
        return ""
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return ""
    return https_origin(endpoint)


def _embed_media_lines(embed: dict) -> list[str]:
    """The media of a post's embed view, with the links the App View gives."""
    kind = str(embed.get("$type") or "")
    if kind.startswith("app.bsky.embed.recordWithMedia"):
        return _embed_media_lines(embed.get("media") or {})
    if kind.startswith("app.bsky.embed.images"):
        lines = []
        for index, image in enumerate(embed.get("images") or [], 1):
            if not isinstance(image, dict):
                continue
            line = f"  Media {index}: image - {image.get('fullsize') or image.get('thumb') or 'no link'}"
            if image.get("alt"):
                line += f" (alt: {image['alt']})"
            lines.append(line)
        return lines
    if kind.startswith("app.bsky.embed.video"):
        line = f"  Media 1: video - playlist: {embed.get('playlist') or 'no link'}"
        if embed.get("thumbnail"):
            line += f" - thumbnail: {embed['thumbnail']}"
        if embed.get("alt"):
            line += f" (alt: {embed['alt']})"
        return [line]
    if kind.startswith("app.bsky.embed.external"):
        external = embed.get("external") or {}
        line = f"  Link card: {external.get('uri', '?')} - {external.get('title', '')}"
        if external.get("thumb"):
            line += f" - thumbnail: {external['thumb']}"
        return [line]
    return []


def _record_blobs(embed: dict) -> list[dict]:
    """The image / video blobs of a post RECORD's embed, in the order the view lists them."""
    kind = str(embed.get("$type") or "")
    if kind == "app.bsky.embed.recordWithMedia":
        return _record_blobs(embed.get("media") or {})
    if kind == "app.bsky.embed.images":
        return [
            i["image"]
            for i in embed.get("images") or []
            if isinstance(i, dict) and isinstance(i.get("image"), dict)
        ]
    if kind == "app.bsky.embed.video" and isinstance(embed.get("video"), dict):
        return [embed["video"]]
    return []


def _blob_cid(blob: dict) -> str:
    return str((blob.get("ref") or {}).get("$link") or blob.get("cid") or "")


def _post_web_url(uri: str, author: dict) -> str:
    parts = uri.split("/")
    if len(parts) < 5 or not uri.startswith("at://"):
        return ""
    return f"https://bsky.app/profile/{author.get('did') or parts[2]}/post/{parts[-1]}"


def _format_bsky_post(post: dict) -> str:
    """Format a Bluesky post into a readable string, with its media."""
    lines: list[str] = []
    author = post.get("author", {})
    record = post.get("record", {})
    uri = post.get("uri", "?")
    display = author.get("displayName", author.get("handle", "?"))
    handle = author.get("handle", "?")
    text = record.get("text", "")
    created = record.get("createdAt", "")

    lines.append(f"**{display}** (@{handle}) — {created}")
    if text:
        lines.append(f"  {text[:500]}")

    like_count = post.get("likeCount", 0)
    repost_count = post.get("repostCount", 0)
    reply_count = post.get("replyCount", 0)
    lines.append(
        f"  [likes: {like_count} · reposts: {repost_count} · replies: {reply_count}]"
    )
    lines.extend(_embed_media_lines(post.get("embed") or {}))
    lines.append(f"  URI: {uri}")
    if post.get("cid"):
        lines.append(f"  CID: {post['cid']}")
    web_url = _post_web_url(str(uri), author)
    if web_url:
        lines.append(f"  URL: {web_url}")
    return "\n".join(lines)


def _media_tip(posts: list[dict]) -> str:
    if any(_record_blobs((p.get("record") or {}).get("embed") or {}) for p in posts):
        return (
            "\n\nTip: download_bluesky_media(post_uri, index) brings a post's image or video "
            "into the conversation as a file (a download card and a file_id)."
        )
    return ""


def _format_notification(n: dict) -> str:
    """Format a Bluesky notification into a single-line string."""
    reason = n.get("reason", "?")
    author = n.get("author", {})
    name = author.get("displayName", author.get("handle", "?"))
    when = n.get("indexedAt", "")
    return f"• [{reason}] **{name}** — {when}"


# ---------------------------------------------------------------------------
# Bluesky tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Bluesky integration tools.

    ``token`` is ``handle||app_password`` or ``handle||app_password||pds_url``.
    """
    handle, app_password, pds_url = parse_compound_token(
        token, 3, defaults=("", "", _BASE_URL)
    )
    pds_url = pds_url.rstrip("/") if pds_url else _BASE_URL
    if not handle or not app_password:
        return []
    persist_file_fn = _kwargs.get("persist_file_fn")

    # Session cache: stores JWT and DID after first auth
    _session: dict[str, str] = {}

    async def _ensure_session() -> tuple[dict[str, str], str | None]:
        """Create or return cached AT Protocol session.

        Returns ``(session_dict, None)`` on success or ``(empty_dict, error)``
        on failure — no exceptions raised.
        """
        if _session.get("accessJwt"):
            return _session, None
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{pds_url}/xrpc/com.atproto.server.createSession",
                _PROVIDER,
                "create session",
                json={"identifier": handle, "password": app_password},
            )
            if err:
                return {}, err
            data = resp.json()
            _session["accessJwt"] = data["accessJwt"]
            _session["refreshJwt"] = data.get("refreshJwt", "")
            _session["did"] = data["did"]
            _session["handle"] = data.get("handle", handle)
            return _session, None

    async def _get(
        endpoint: str, params: dict | None = None
    ) -> tuple[dict, str | None]:
        """Perform an authenticated GET. Returns ``(data, None)`` or ``({}, error)``."""
        session, sess_err = await _ensure_session()
        if sess_err:
            return {}, sess_err
        url = f"{pds_url}/xrpc/{endpoint}"
        hdrs = {"Authorization": f"Bearer {session['accessJwt']}"}
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                endpoint,
                params=params or {},
                headers=hdrs,
                expected_statuses={401},
            )
            if err is None:
                return resp.json(), None
            # Only an expired/invalid token (401) warrants a re-auth + retry.
            # Every other outcome (400/403/404/5xx/network) is returned as-is:
            # treating them all as "token expired" made a single bad request
            # trigger a fresh createSession login, and Bluesky rate-limits
            # createSession aggressively (~30/5 min per account).
            if resp is None or resp.status_code != 401:
                return {}, err
            _session.clear()
            session, sess_err = await _ensure_session()
            if sess_err:
                return {}, sess_err
            resp, err = await safe_request(
                client,
                "get",
                url,
                _PROVIDER,
                endpoint,
                params=params or {},
                headers={"Authorization": f"Bearer {session['accessJwt']}"},
            )
            if err:
                return {}, err
            return resp.json(), None

    async def _post(
        endpoint: str, body: dict | None = None, *, raw: tuple[bytes, str] | None = None
    ) -> tuple[dict, str | None]:
        """Perform an authenticated POST of JSON *body*, or of *raw* ``(bytes, content type)`` instead.

        A call passes one of the two (an upload passes *raw*, and its *body* is
        ignored). Returns ``(data, None)`` or ``({}, error)``.
        """
        session, sess_err = await _ensure_session()
        if sess_err:
            return {}, sess_err
        url = f"{pds_url}/xrpc/{endpoint}"
        payload: dict[str, Any] = {"content": raw[0]} if raw else {"json": body}
        extra = {"Content-Type": raw[1]} if raw else {}
        hdrs = {"Authorization": f"Bearer {session['accessJwt']}", **extra}
        timeout = social_media.UPLOAD_TIMEOUT_SECONDS if raw else 30
        async with egress_client(timeout=timeout) as client:
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                endpoint,
                headers=hdrs,
                expected_statuses={401},
                **payload,
            )
            if err is None:
                return resp.json(), None
            # Only re-auth + retry on 401 (see _get for the rationale — a fresh
            # createSession per failed request risks a login rate-limit lockout).
            if resp is None or resp.status_code != 401:
                return {}, err
            _session.clear()
            session, sess_err = await _ensure_session()
            if sess_err:
                return {}, sess_err
            resp, err = await safe_request(
                client,
                "post",
                url,
                _PROVIDER,
                endpoint,
                headers={"Authorization": f"Bearer {session['accessJwt']}", **extra},
                **payload,
            )
            if err:
                return {}, err
            return resp.json(), None

    async def _author_pds(did: str) -> tuple[str, str | None]:
        """``(PDS URL, error)`` of the repository *did* lives on.

        The session's own repository is on the configured PDS; any other one is
        read from its DID document (:func:`did_document_url`), service
        ``#atproto_pds``, which must be the document OF *did* and name a bare
        HTTPS origin (:func:`pds_origin`).
        """
        if did == _session.get("did"):
            return pds_url, None
        doc_url = did_document_url(did)
        if doc_url is None:
            return "", (
                f"Error: {did} is not a DID Bluesky resolves (did:plc, or a did:web naming "
                "a hostname - no IP address, no path)."
            )
        fetched = await fetch_public_media(
            doc_url, source="Bluesky", max_bytes=256 * 1024, what="DID document"
        )
        if isinstance(fetched, FetchError):
            return "", fetched
        try:
            doc = _json.loads(fetched.data)
        except ValueError:
            return "", f"Error: the DID document of {did} is not JSON."
        if not isinstance(doc, dict) or doc.get("id") != did:
            return "", f"Error: the document served for {did} is not its DID document."
        services = doc.get("service")
        for service in services if isinstance(services, list) else []:
            if isinstance(service, dict) and str(service.get("id") or "").endswith("#atproto_pds"):
                origin = pds_origin(str(service.get("serviceEndpoint") or ""))
                if origin:
                    return origin, None
        return "", f"Error: the DID document of {did} names no HTTPS PDS."

    # ── Get my profile ────────────────────────────────────────────────────

    async def get_bluesky_profile() -> str:
        """Get the authenticated Bluesky user's profile."""
        session, err = await _ensure_session()
        if err:
            return err
        data, err = await _get("app.bsky.actor.getProfile", {"actor": session["did"]})
        if err:
            return err
        lines = [
            f"**{data.get('displayName', 'Unknown')}** (@{data.get('handle', '?')})",
            f"  DID: {data.get('did', '?')}",
            f"  Followers: {data.get('followersCount', 0)} · Following: {data.get('followsCount', 0)}",
            f"  Posts: {data.get('postsCount', 0)}",
        ]
        if data.get("description"):
            lines.append(f"  Bio: {data['description'][:300]}")
        return "\n".join(lines)

    # ── Get user profile ──────────────────────────────────────────────────

    async def get_bluesky_user_profile(actor: str) -> str:
        """Get a Bluesky user's profile by handle or DID."""
        data, err = await _get("app.bsky.actor.getProfile", {"actor": actor})
        if err:
            return err
        lines = [
            f"**{data.get('displayName', 'Unknown')}** (@{data.get('handle', '?')})",
            f"  DID: {data.get('did', '?')}",
            f"  Followers: {data.get('followersCount', 0)} · Following: {data.get('followsCount', 0)}",
            f"  Posts: {data.get('postsCount', 0)}",
        ]
        if data.get("description"):
            lines.append(f"  Bio: {data['description'][:300]}")
        return "\n".join(lines)

    # ── Create post ───────────────────────────────────────────────────────

    async def create_bluesky_post(
        text: str,
        reply_to_uri: str = "",
        reply_to_cid: str = "",
        media_file_ids: Any = None,
        media_alt_texts: Any = None,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Create a Bluesky post, optionally with images or a video of the run.

        Optionally reply to another post (requires both URI and CID).
        """
        media: list[PostMedia] = []
        if media_file_ids:
            checked = await read_post_media(
                file_context, media_file_ids, _POST_MEDIA, alt_text=media_alt_texts
            )
            if isinstance(checked, str):
                return ToolResult(tool_call_id="", content=checked, is_error=True)
            media = checked
        session, err = await _ensure_session()
        if err:
            return with_summaries(err, media)
        record: dict[str, Any] = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(UTC).isoformat(),
        }
        if reply_to_uri and reply_to_cid:
            record["reply"] = {
                "root": {"uri": reply_to_uri, "cid": reply_to_cid},
                "parent": {"uri": reply_to_uri, "cid": reply_to_cid},
            }
        embeds: list[dict[str, Any]] = []
        for item in media:
            uploaded, err = await _post(
                "com.atproto.repo.uploadBlob", raw=(item.data, item.content_type)
            )
            if err or not isinstance(uploaded.get("blob"), dict):
                problem = err or f"Error: Bluesky returned no blob for {item.label}."
                return with_summaries(f"{problem}\nNothing has been published.", media)
            entry: dict[str, Any] = {"alt": item.alt_text}
            if item.width and item.height:
                entry["aspectRatio"] = {"width": item.width, "height": item.height}
            if item.kind == "video":
                entry["video"] = uploaded["blob"]
                if not item.alt_text:
                    entry.pop("alt")
            else:
                entry["image"] = uploaded["blob"]
            embeds.append(entry)
        if media and media[0].kind == "video":
            record["embed"] = {"$type": "app.bsky.embed.video", **embeds[0]}
        elif media:
            record["embed"] = {"$type": "app.bsky.embed.images", "images": embeds}
        data, err = await _post(
            "com.atproto.repo.createRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.feed.post",
                "record": record,
            },
        )
        if err:
            return with_summaries(err, media)
        answer = f"Post created. URI: {data.get('uri', '?')}, CID: {data.get('cid', '?')}"
        if media:
            web_url = _post_web_url(str(data.get("uri") or ""), {"did": session["did"]})
            if web_url:
                answer += f"\nURL: {web_url}"
            answer += f"\nMedia: {media_kinds(media)}"
            if media[0].kind == "video":
                answer += " (Bluesky processes a video once the post is live: it can show as loading for a moment)"
        return with_summaries(answer, media)

    # ── Delete post ───────────────────────────────────────────────────────

    async def delete_bluesky_post(post_uri: str) -> str:
        """Delete a Bluesky post by its AT URI."""
        session, err = await _ensure_session()
        if err:
            return err
        parts = post_uri.split("/")
        rkey = parts[-1] if parts else ""
        if not rkey:
            return "Error: Invalid post URI."
        _data, err = await _post(
            "com.atproto.repo.deleteRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.feed.post",
                "rkey": rkey,
            },
        )
        if err:
            return err
        return f"Post deleted: {post_uri}"

    # ── Get post thread ───────────────────────────────────────────────────

    async def get_bluesky_post_thread(post_uri: str, depth: int = 6) -> str:
        """Get a full post thread (parent + replies) by AT URI."""
        data, err = await _get(
            "app.bsky.feed.getPostThread", {"uri": post_uri, "depth": str(depth)}
        )
        if err:
            return err
        thread = data.get("thread", {})
        post = thread.get("post", {})
        lines = [_format_bsky_post(post)]

        replies = thread.get("replies", [])
        if replies:
            lines.append(f"\nReplies ({len(replies)}):")
            for r in replies[:10]:
                rpost = r.get("post", {})
                lines.append("  " + _format_bsky_post(rpost).replace("\n", "\n  "))
        shown = [post, *(r.get("post", {}) for r in replies[:10])]
        return "\n".join(lines) + _media_tip(shown)

    # ── Get timeline ──────────────────────────────────────────────────────

    async def list_bluesky_timeline(limit: int = 20) -> str:
        """Get the authenticated user's home timeline."""
        data, err = await _get(
            "app.bsky.feed.getTimeline", {"limit": str(min(limit, 100))}
        )
        if err:
            return err
        feed = data.get("feed", [])
        if not feed:
            return "Timeline is empty."
        posts = [item.get("post", {}) for item in feed]
        results = [_format_bsky_post(p) for p in posts]
        return f"Timeline ({len(feed)} posts):\n\n" + "\n\n".join(results) + _media_tip(posts)

    # ── Get author feed ───────────────────────────────────────────────────

    async def get_bluesky_author_feed(actor: str = "", limit: int = 20) -> str:
        """Get posts by a specific user (or the authenticated user)."""
        session, err = await _ensure_session()
        if err:
            return err
        target = actor or session["did"]
        data, err = await _get(
            "app.bsky.feed.getAuthorFeed",
            {"actor": target, "limit": str(min(limit, 100))},
        )
        if err:
            return err
        feed = data.get("feed", [])
        if not feed:
            return f"No posts found for {target}."
        posts = [item.get("post", {}) for item in feed]
        results = [_format_bsky_post(p) for p in posts]
        return f"Posts by {target} ({len(feed)}):\n\n" + "\n\n".join(results) + _media_tip(posts)

    # ── Search posts ──────────────────────────────────────────────────────

    async def search_bluesky_posts(query: str, limit: int = 20) -> str:
        """Search Bluesky posts by keyword."""
        data, err = await _get(
            "app.bsky.feed.searchPosts", {"q": query, "limit": str(min(limit, 100))}
        )
        if err:
            return err
        posts = data.get("posts", [])
        if not posts:
            return f"No posts found for '{query}'."
        results = [_format_bsky_post(p) for p in posts]
        return (
            f"Search results for '{query}' ({len(posts)}):\n\n"
            + "\n\n".join(results)
            + _media_tip(posts)
        )

    # ── Download a post's media ───────────────────────────────────────────

    async def download_bluesky_media(post_uri: str, index: int = 1) -> str | ToolResult:
        """Download the original file of a post's image or video into the conversation."""
        post_uri = (post_uri or "").strip()
        data, err = await _get("app.bsky.feed.getPosts", {"uris": post_uri})
        if err:
            return err
        posts = [p for p in data.get("posts") or [] if isinstance(p, dict)]
        if not posts:
            return f"Bluesky returned no post for {post_uri}."
        post = posts[0]
        blobs = _record_blobs((post.get("record") or {}).get("embed") or {})
        if not blobs:
            return f"Post {post_uri} carries no image or video."
        position = int(index or 1)
        if not 1 <= position <= len(blobs):
            return ToolResult(
                tool_call_id="",
                content=f"Error: index {position} is out of range - the post carries {len(blobs)} media.",
                is_error=True,
            )
        blob = blobs[position - 1]
        cid = _blob_cid(blob)
        author = post.get("author") or {}
        did = str(author.get("did") or "")
        if not did and post_uri.startswith("at://"):
            did = post_uri.split("/")[2]
        if not cid or not did.startswith("did:"):
            return ToolResult(
                tool_call_id="",
                content=f"Error: the post record names no blob for media {position}.",
                is_error=True,
            )
        pds, err = await _author_pds(did)
        if err:
            return err
        url = (
            f"{pds}/xrpc/com.atproto.sync.getBlob?did={quote(did, safe=':')}&cid={quote(cid, safe='')}"
        )
        # Only the configured PDS the session itself lives on is trusted: a DID
        # document naming that origin grants nothing.
        own = did == _session.get("did")
        fetched = await fetch_public_media(url, source="Bluesky", trusted_origin=pds_url if own else "")
        if isinstance(fetched, FetchError):
            return fetched
        body, declared = fetched.data, fetched.content_type
        content_type = media_type(body, blob.get("mimeType") or declared, fetched.filename)
        rkey = post_uri.rsplit("/", 1)[-1]
        view_links = _embed_media_lines(post.get("embed") or {})
        return await deliver_file(
            data=body,
            filename=with_extension(f"bluesky-{rkey}-{position}", content_type),
            content_type=content_type,
            source="Bluesky",
            persist_file_fn=persist_file_fn,
            links=[("Web URL", _post_web_url(post_uri, author)), ("Download URL", url)],
            details=[line.strip() for line in view_links[position - 1 : position]],
        )

    # ── Search users ──────────────────────────────────────────────────────

    async def search_bluesky_users(query: str, limit: int = 20) -> str:
        """Search Bluesky users by keyword."""
        data, err = await _get(
            "app.bsky.actor.searchActors",
            {"q": query, "limit": str(min(limit, 50))},
        )
        if err:
            return err
        actors = data.get("actors", [])
        if not actors:
            return f"No users found for '{query}'."
        lines = []
        for a in actors:
            followers = a.get("followersCount", 0)
            lines.append(
                f"• **{a.get('displayName', '?')}** (@{a.get('handle', '?')}) — {followers} followers"
            )
            if a.get("description"):
                lines.append(f"    {a['description'][:150]}")
        return f"Users matching '{query}' ({len(actors)}):\n\n" + "\n".join(lines)

    # ── Like / Unlike ─────────────────────────────────────────────────────

    async def like_bluesky_post(post_uri: str, post_cid: str) -> str:
        """Like a Bluesky post (requires both URI and CID)."""
        session, err = await _ensure_session()
        if err:
            return err
        data, err = await _post(
            "com.atproto.repo.createRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.feed.like",
                "record": {
                    "$type": "app.bsky.feed.like",
                    "subject": {"uri": post_uri, "cid": post_cid},
                    "createdAt": datetime.now(UTC).isoformat(),
                },
            },
        )
        if err:
            return err
        return f"Post liked. Record URI: {data.get('uri', '?')}"

    async def unlike_bluesky_post(like_uri: str) -> str:
        """Unlike a Bluesky post by deleting the like record."""
        session, err = await _ensure_session()
        if err:
            return err
        rkey = like_uri.split("/")[-1]
        _data, err = await _post(
            "com.atproto.repo.deleteRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.feed.like",
                "rkey": rkey,
            },
        )
        if err:
            return err
        return "Like removed."

    # ── Repost / Unrepost ─────────────────────────────────────────────────

    async def repost_bluesky_post(post_uri: str, post_cid: str) -> str:
        """Repost a Bluesky post (requires both URI and CID)."""
        session, err = await _ensure_session()
        if err:
            return err
        data, err = await _post(
            "com.atproto.repo.createRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.feed.repost",
                "record": {
                    "$type": "app.bsky.feed.repost",
                    "subject": {"uri": post_uri, "cid": post_cid},
                    "createdAt": datetime.now(UTC).isoformat(),
                },
            },
        )
        if err:
            return err
        return f"Reposted. Record URI: {data.get('uri', '?')}"

    # ── Follow / Unfollow ─────────────────────────────────────────────────

    async def follow_bluesky_user(target_did: str) -> str:
        """Follow a Bluesky user by DID."""
        session, err = await _ensure_session()
        if err:
            return err
        data, err = await _post(
            "com.atproto.repo.createRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.graph.follow",
                "record": {
                    "$type": "app.bsky.graph.follow",
                    "subject": target_did,
                    "createdAt": datetime.now(UTC).isoformat(),
                },
            },
        )
        if err:
            return err
        return f"Now following {target_did}. Record URI: {data.get('uri', '?')}"

    async def unfollow_bluesky_user(follow_uri: str) -> str:
        """Unfollow a user by deleting the follow record URI."""
        session, err = await _ensure_session()
        if err:
            return err
        rkey = follow_uri.split("/")[-1]
        _data, err = await _post(
            "com.atproto.repo.deleteRecord",
            {
                "repo": session["did"],
                "collection": "app.bsky.graph.follow",
                "rkey": rkey,
            },
        )
        if err:
            return err
        return "Unfollowed."

    # ── Get followers / following ─────────────────────────────────────────

    async def get_bluesky_followers(actor: str = "", limit: int = 50) -> str:
        """Get followers of a Bluesky user."""
        session, err = await _ensure_session()
        if err:
            return err
        target = actor or session["did"]
        data, err = await _get(
            "app.bsky.graph.getFollowers",
            {"actor": target, "limit": str(min(limit, 100))},
        )
        if err:
            return err
        followers = data.get("followers", [])
        if not followers:
            return f"No followers for {target}."
        lines = [
            f"• **{f.get('displayName', '?')}** (@{f.get('handle', '?')})"
            for f in followers
        ]
        return f"Followers of {target} ({len(followers)}):\n\n" + "\n".join(lines)

    async def get_bluesky_following(actor: str = "", limit: int = 50) -> str:
        """Get accounts a Bluesky user follows."""
        session, err = await _ensure_session()
        if err:
            return err
        target = actor or session["did"]
        data, err = await _get(
            "app.bsky.graph.getFollows",
            {"actor": target, "limit": str(min(limit, 100))},
        )
        if err:
            return err
        follows = data.get("follows", [])
        if not follows:
            return f"{target} is not following anyone."
        lines = [
            f"• **{f.get('displayName', '?')}** (@{f.get('handle', '?')})"
            for f in follows
        ]
        return f"Following ({len(follows)}):\n\n" + "\n".join(lines)

    # ── Get notifications ─────────────────────────────────────────────────

    async def get_bluesky_notifications(limit: int = 25) -> str:
        """Get recent Bluesky notifications (likes, reposts, follows, replies, mentions)."""
        data, err = await _get(
            "app.bsky.notification.listNotifications",
            {"limit": str(min(limit, 100))},
        )
        if err:
            return err
        notifs = data.get("notifications", [])
        return format_entity_list(
            notifs,
            _format_notification,
            noun="notification",
            empty_msg="No notifications.",
        )

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("get_bluesky_profile", get_bluesky_profile)
    executor.register("get_bluesky_user_profile", get_bluesky_user_profile)
    executor.register("create_bluesky_post", create_bluesky_post)
    executor.register("delete_bluesky_post", delete_bluesky_post)
    executor.register("get_bluesky_post_thread", get_bluesky_post_thread)
    executor.register("list_bluesky_timeline", list_bluesky_timeline)
    executor.register("get_bluesky_author_feed", get_bluesky_author_feed)
    executor.register("search_bluesky_posts", search_bluesky_posts)
    executor.register("download_bluesky_media", download_bluesky_media)
    executor.register("search_bluesky_users", search_bluesky_users)
    executor.register("like_bluesky_post", like_bluesky_post)
    executor.register("unlike_bluesky_post", unlike_bluesky_post)
    executor.register("repost_bluesky_post", repost_bluesky_post)
    executor.register("follow_bluesky_user", follow_bluesky_user)
    executor.register("unfollow_bluesky_user", unfollow_bluesky_user)
    executor.register("get_bluesky_followers", get_bluesky_followers)
    executor.register("get_bluesky_following", get_bluesky_following)
    executor.register("get_bluesky_notifications", get_bluesky_notifications)

    source = "integration:bluesky"
    return [
        ToolDefinition(
            name="get_bluesky_profile",
            description="Get the authenticated Bluesky user's profile — display name, handle, DID, followers, posts count.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_bluesky_user_profile",
            description="Get a Bluesky user's profile by handle (e.g. user.bsky.social) or DID.",
            input_schema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Handle or DID of the user",
                    }
                },
                "required": ["actor"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_bluesky_post",
            description=(
                "Create a Bluesky post (skeet). Max 300 characters. Optionally reply to another "
                "post (requires both URI and CID of parent), and attach files of this "
                "conversation by file_id (attached, generated or downloaded): up to 4 images "
                "(JPG, PNG, WEBP or GIF, 2,000,000 bytes each) OR one MP4 video (300,000,000 "
                "bytes; Bluesky requires a verified account email and limits videos per day). "
                "A file outside these limits is refused before anything is published; the "
                "result carries the visual summary of every image published."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Post text (max 300 chars)",
                    },
                    "reply_to_uri": {
                        "type": "string",
                        "description": "AT URI of post to reply to (optional)",
                    },
                    "reply_to_cid": {
                        "type": "string",
                        "description": "CID of post to reply to (required if replying)",
                    },
                    "media_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "file_ids of images or one video of this conversation to attach (optional)",
                    },
                    "media_alt_texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alt text of each file, in the order of media_file_ids",
                    },
                },
                "required": ["text"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="download_bluesky_media",
            workspace_ingest=True,
            description=(
                "Download the original file of a Bluesky post's image or video (by the post's AT "
                "URI and the 1-based media index the read tools list) into the conversation: a "
                "download card, its links and a file_id to forward to any tool that takes a "
                "file (up to 100 MB). Include the returned [[FILE:...]] marker verbatim so the "
                "user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_uri": {"type": "string", "description": "AT URI of the post"},
                    "index": {
                        "type": "integer",
                        "description": "1-based media index within the post (default 1)",
                        "default": 1,
                    },
                },
                "required": ["post_uri"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="delete_bluesky_post",
            description="Delete a Bluesky post by its AT URI.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_uri": {
                        "type": "string",
                        "description": "AT URI of the post (e.g. at://did:plc:.../app.bsky.feed.post/...)",
                    }
                },
                "required": ["post_uri"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_bluesky_post_thread",
            description="Get a full Bluesky post thread — the post plus parent and child replies.",
            input_schema={
                "type": "object",
                "properties": {
                    "post_uri": {"type": "string", "description": "AT URI of the post"},
                    "depth": {
                        "type": "integer",
                        "description": "Reply depth (default 6)",
                        "default": 6,
                    },
                },
                "required": ["post_uri"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_bluesky_timeline",
            description="Get the authenticated user's Bluesky home timeline.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of posts (max 100)",
                        "default": 20,
                    }
                },
                "required": [],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_bluesky_author_feed",
            description="Get posts by a specific Bluesky user (or the authenticated user).",
            input_schema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Handle or DID. If empty, uses authenticated user.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of posts (max 100)",
                        "default": 20,
                    },
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="search_bluesky_posts",
            description="Search Bluesky posts by keyword.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Number of results (max 100)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="search_bluesky_users",
            description="Search Bluesky users by keyword.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {
                        "type": "integer",
                        "description": "Number of results (max 50)",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="like_bluesky_post",
            description="Like a Bluesky post (requires both AT URI and CID).",
            input_schema={
                "type": "object",
                "properties": {
                    "post_uri": {"type": "string", "description": "AT URI of the post"},
                    "post_cid": {"type": "string", "description": "CID of the post"},
                },
                "required": ["post_uri", "post_cid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="unlike_bluesky_post",
            description="Unlike a Bluesky post by deleting the like record.",
            input_schema={
                "type": "object",
                "properties": {
                    "like_uri": {
                        "type": "string",
                        "description": "AT URI of the like record to delete",
                    }
                },
                "required": ["like_uri"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="repost_bluesky_post",
            description="Repost a Bluesky post (requires both AT URI and CID).",
            input_schema={
                "type": "object",
                "properties": {
                    "post_uri": {"type": "string", "description": "AT URI of the post"},
                    "post_cid": {"type": "string", "description": "CID of the post"},
                },
                "required": ["post_uri", "post_cid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="follow_bluesky_user",
            description="Follow a Bluesky user by their DID.",
            input_schema={
                "type": "object",
                "properties": {
                    "target_did": {
                        "type": "string",
                        "description": "DID of the user to follow",
                    }
                },
                "required": ["target_did"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="unfollow_bluesky_user",
            description="Unfollow a Bluesky user by deleting the follow record.",
            input_schema={
                "type": "object",
                "properties": {
                    "follow_uri": {
                        "type": "string",
                        "description": "AT URI of the follow record to delete",
                    }
                },
                "required": ["follow_uri"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_bluesky_followers",
            description="Get followers of a Bluesky user.",
            input_schema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Handle or DID. If empty, uses authenticated user.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results (max 100)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="get_bluesky_following",
            description="Get accounts a Bluesky user follows.",
            input_schema={
                "type": "object",
                "properties": {
                    "actor": {
                        "type": "string",
                        "description": "Handle or DID. If empty, uses authenticated user.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of results (max 100)",
                        "default": 50,
                    },
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="get_bluesky_notifications",
            description="Get recent Bluesky notifications — likes, reposts, follows, replies, and mentions.",
            input_schema={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Number of notifications (max 100)",
                        "default": 25,
                    }
                },
                "required": [],
            },
            source=source,
            priority=2,
        ),
    ]
