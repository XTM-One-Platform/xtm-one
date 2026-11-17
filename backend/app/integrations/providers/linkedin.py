# SPDX-License-Identifier: Apache-2.0

"""LinkedIn integration — personal profiles and company pages.

API docs: https://learn.microsoft.com/en-us/linkedin/
Auth: OAuth 2.0 (3-legged)
Base URL: https://api.linkedin.com

Supports two account modes:
  - **Personal**: Post and manage content as the authenticated user.
  - **Page (Organization)**: Post and manage content as a LinkedIn Page.
    The ``account_type`` field in ``extra_config`` selects the mode.
    After OAuth, administered organizations are fetched and stored.

Endpoints used:
  - /v2/userinfo — OpenID Connect profile
  - /rest/posts — create / get / list by author / delete posts
  - /rest/images, /rest/videos, /rest/documents — media upload
    (initializeUpload, upload URL PUT, finalizeUpload for videos) and the
    download links of a post's media
  - /rest/socialActions — reactions, comments
  - /rest/organizationAcls — administered organizations
  - /rest/organizations — organization details
  - /rest/organizationalEntityShareStatistics — page analytics

Reading posts and their media needs ``r_organization_social`` (page mode) or
LinkedIn's restricted ``r_member_social`` (personal posts, approved apps only),
and a media download link needs ``w_organization_social``: a personal token
with only ``w_member_social`` cannot read ``/rest/images`` (Images API,
"Permissions").
"""

from __future__ import annotations

import json as _json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import FetchError, deliver_file, with_extension
from app.integrations.http import (
    AutoRefreshClient,
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
    host_is,
    is_https,
    media_kinds,
    media_type,
    read_post_media,
    with_summaries,
)
from app.services.web_fetch import guarded_client_options
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "LinkedIn"
_BASE_URL = "https://api.linkedin.com"
_VERSION = "202601"  # LinkedIn REST API version header (YYYYMM, update yearly)

# Images API: "Images with less than 36,152,320 pixels. JPG, GIF, and PNG
# formats. GIF format supports up to 250 frames." No byte limit is documented.
_IMAGE = MediaRule(
    types=frozenset({"image/jpeg", "image/png", "image/gif"}),
    types_label="JPG, PNG or GIF",
    max_bytes=None,
    max_pixels=36_152_320,
    max_frames=250,
)
# Videos API: "Length: Three seconds to 30 minutes. File size: Between 75kb
# and 500MB. File format: MP4."
_VIDEO = MediaRule(
    types=frozenset({"video/mp4"}),
    types_label="MP4",
    max_bytes=500 * MIB,
    size_label="500 MB",
    min_bytes=75 * 1024,
    min_seconds=3,
    max_seconds=30 * 60,
)
# Documents API: "The file size can't exceed 100MB and 300 pages. The
# following file types are supported: PPT, PPTX, DOC, DOCX, and PDF."
_DOCUMENT = MediaRule(
    types=frozenset(
        {
            "application/pdf",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.ms-powerpoint",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
    ),
    types_label="PDF, DOC, DOCX, PPT or PPTX (300 pages at most)",
    max_bytes=100 * MIB,
    size_label="100 MB",
)
# MultiImage API: "minimum of 2 images and maximum of 20 images"; one image is
# a single-image post.
_POST_MEDIA = MediaLimits(
    platform="LinkedIn", image=_IMAGE, max_images=20, video=_VIDEO, document=_DOCUMENT
)
# Comments API: a comment carries one image entity.
_COMMENT_MEDIA = MediaLimits(platform="LinkedIn", image=_IMAGE, max_images=1, unit="comment")

_MEDIA_ENDPOINTS = {"image": "images", "video": "videos", "document": "documents"}

#: How many administered Pages ``get_linkedin_organization`` details without an ID.
_MAX_ORGANIZATIONS_DETAILED = 10


class LinkedInIntegration(BaseIntegration):
    provider_name = "linkedin"
    display_name = "LinkedIn"
    icon = "Linkedin"
    description = (
        "Connect LinkedIn for social media management — posts, pages, and analytics."
    )
    auth_types = ["oauth"]

    def get_oauth_defaults(self) -> OAuthDefaults:
        # Default scopes cover personal account mode.
        # Page (organization) scopes are appended dynamically by the OAuth
        # flow in integrations.py / oauth_callbacks.py based on account_type.
        return OAuthDefaults(
            authorize_url="https://www.linkedin.com/oauth/v2/authorization",
            token_url="https://www.linkedin.com/oauth/v2/accessToken",  # noqa: S106
            # OAuth token exchange endpoint URL, not a secret.
            scopes=[
                "openid",
                "profile",
                "email",
                "w_member_social",
            ],
        )

    async def validate_connection(self, token: str) -> bool:
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v2/userinfo",
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
                f"{_BASE_URL}/v2/userinfo",
                _PROVIDER,
                "get user info",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp is None:
                return {"error": err}
            return resp.json()


async def check_linkedin_connectivity(
    token: str | None, extra: dict | None, name: str
) -> dict[str, str]:
    """The "Test connection" verdict of a LinkedIn integration.

    A Page (organization) token carries the Community Management scopes and no
    ``openid`` / ``profile``, so ``/v2/userinfo`` refuses it: it is checked on
    the Pages it administers (``organizationAcls``). A personal token is
    checked on the member profile.
    """
    page = str((extra or {}).get("account_type") or "personal").strip() == "page"
    if page:
        url = f"{_BASE_URL}/rest/organizationAcls"
        params: dict[str, str] | None = {
            "q": "roleAssignee",
            "role": "ADMINISTRATOR",
            "count": "50",
        }
    else:
        url, params = f"{_BASE_URL}/v2/userinfo", None
    try:
        async with egress_client(timeout=15) as client:
            resp = await client.get(url, headers=_headers(token or ""), params=params)
    except httpx.HTTPError:
        return {"status": "error", "message": f"{name} connection failed"}
    if resp.status_code in (401, 403):
        return {
            "status": "error",
            "message": (
                "Authentication failed - authorize the integration again"
                + (" as a LinkedIn Page (the token needs the organization scopes)." if page else ".")
            ),
        }
    if not resp.is_success:
        return {"status": "error", "message": f"{name} returned HTTP {resp.status_code}"}
    if not page:
        return {"status": "ok", "message": f"{name} is connected and responding"}
    try:
        payload = resp.json()
    except ValueError:
        payload = None
    elements = payload.get("elements") if isinstance(payload, dict) else None
    if not isinstance(elements, list):
        return {
            "status": "error",
            "message": f"{name} answered the Pages it administers with an unreadable body.",
        }
    count = len(elements)
    if not count:
        return {
            "status": "error",
            "message": (
                f"{name} is connected as an organization token but administers "
                "no LinkedIn Page."
            ),
        }
    return {
        "status": "ok",
        "message": (
            f"{name} is connected as an organization token administering "
            f"{count} LinkedIn Page{'' if count == 1 else 's'}."
        ),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "LinkedIn-Version": _VERSION,
        "Content-Type": "application/json",
        "X-Restli-Protocol-Version": "2.0.0",
    }


def _org_id(acl: dict) -> str:
    """The organization id an ``organizationAcls`` entry names."""
    org_urn = str(acl.get("organization", "") or "")
    return org_urn.split(":")[-1] if ":" in org_urn else org_urn


def _format_org_acl(e: dict) -> str:
    """Format a LinkedIn organization ACL into a single-line string."""
    role = e.get("role", "?")
    return f"• Organization ID: {_org_id(e)} — Role: {role}"


#: What a Page token answers for the member-profile tool: it has no
#: ``openid`` / ``profile`` scope, so ``/v2/userinfo`` would refuse it.
PAGE_ACCOUNT_PROFILE_ERROR = (
    "Error: organization token - this LinkedIn integration is connected as a "
    "LinkedIn Page, which has no member profile (the Page scopes carry no "
    "openid/profile). Use the organization tools: list_linkedin_organizations, "
    "get_linkedin_organization, list_linkedin_posts, "
    "get_linkedin_organization_followers, get_linkedin_page_statistics."
)


def _format_organization(data: dict, *, configured: bool = False) -> str:
    """A LinkedIn Page's details, as ``get_linkedin_organization`` lists them."""
    lines = [
        f"**{data.get('localizedName', 'Unknown')}**"
        + (" (the integration's configured Page)" if configured else ""),
        f"  ID: {data.get('id', '?')}",
        f"  Vanity Name: {data.get('vanityName', 'N/A')}",
    ]
    if data.get("localizedDescription"):
        lines.append(f"  Description: {data['localizedDescription'][:300]}")
    if data.get("staffCountRange"):
        lines.append(f"  Size: {data['staffCountRange']}")
    if data.get("industries"):
        lines.append(f"  Industries: {', '.join(str(i) for i in data['industries'])}")
    if isinstance(data.get("website"), dict):
        website_url = data["website"].get("localized", {}).get("en_US", "N/A")
        lines.append(f"  Website: {website_url}")
    return "\n".join(lines)


def _format_li_comment(c: dict, resolved: dict[str, dict] | None = None) -> str:
    """Format a LinkedIn comment into a single-line string, with its image."""
    actor = c.get("actor", "Unknown")
    msg = c.get("message", {}).get("text", "")
    created = c.get("created", {}).get("time", "")
    line = f"• **{actor}** ({created}): {msg[:200]}"
    for urn in _comment_image_urns(c):
        url = ((resolved or {}).get(urn) or {}).get("downloadUrl")
        line += f"\n    image: {urn}" + (f" - Download URL: {url}" if url else "")
    return line


def _media_urn_type(urn: str) -> str:
    """``image`` / ``video`` / ``document`` for a LinkedIn media URN, ``""`` otherwise."""
    parts = (urn or "").split(":")
    if len(parts) >= 4 and parts[0] == "urn" and parts[1] == "li" and parts[2] in _MEDIA_ENDPOINTS:
        return parts[2]
    return ""


def _comment_image_urns(comment: dict) -> list[str]:
    urns = []
    for item in comment.get("content") or []:
        entity = item.get("entity") if isinstance(item, dict) else None
        if isinstance(entity, dict) and _media_urn_type(str(entity.get("image") or "")):
            urns.append(str(entity["image"]))
    return urns


def _post_media_entries(post: dict) -> list[dict]:
    """The media a post's ``content`` references: ``{"id", "title"?, "altText"?}`` each."""
    content = post.get("content") or {}
    entries: list[dict] = []
    if isinstance(content.get("media"), dict):
        entries.append(content["media"])
    for image in (content.get("multiImage") or {}).get("images") or []:
        if isinstance(image, dict):
            entries.append(image)
    for card in (content.get("carousel") or {}).get("cards") or []:
        if isinstance(card, dict) and isinstance(card.get("media"), dict):
            entries.append(card["media"])
    thumbnail = (content.get("article") or {}).get("thumbnail")
    if isinstance(thumbnail, str) and thumbnail:
        entries.append({"id": thumbnail, "title": "article thumbnail"})
    return [e for e in entries if _media_urn_type(str(e.get("id") or ""))]


def _post_web_url(urn: str) -> str:
    return f"https://www.linkedin.com/feed/update/{urn}/"


def _when(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _format_li_post(post: dict, resolved: dict[str, dict]) -> str:
    """A post with its text, its web URL and every media it carries, with its download link."""
    urn = post.get("id", "?")
    when = _when(post.get("publishedAt") or post.get("createdAt"))
    lines = [f"**{urn}** by {post.get('author', '?')}" + (f" ({when})" if when else "")]
    if post.get("commentary"):
        lines.append(f"  {str(post['commentary'])[:1000]}")
    lines.append(
        f"  Visibility: {post.get('visibility', '?')} · State: {post.get('lifecycleState', '?')}"
    )
    lines.append(f"  Web URL: {_post_web_url(urn)}")
    article = (post.get("content") or {}).get("article")
    if isinstance(article, dict) and article.get("source"):
        lines.append(f"  Article: {article['source']} - {article.get('title', '')}")
    for index, entry in enumerate(_post_media_entries(post), 1):
        media_urn = str(entry["id"])
        info = resolved.get(media_urn) or {}
        parts = [f"  Media {index}: {_media_urn_type(media_urn)} {media_urn}"]
        if entry.get("title"):
            parts.append(f"title: {entry['title']}")
        if entry.get("altText"):
            parts.append(f"alt: {entry['altText']}")
        if info.get("status") and info["status"] != "AVAILABLE":
            parts.append(f"status: {info['status']}")
        parts.append(
            f"Download URL: {info['downloadUrl']}" if info.get("downloadUrl") else "no download link"
        )
        lines.append(" - ".join(parts))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LinkedIn tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build LinkedIn integration tools.

    ``token`` is ``access_token||account_type||organization_id``.
    """
    access_token, account_type, organization_id = parse_compound_token(
        token, 3, defaults=("", "personal", "")
    )
    refresh_fn = _kwargs.get("refresh_fn")
    persist_file_fn = _kwargs.get("persist_file_fn")
    hdrs = _headers(access_token)

    def _client() -> AutoRefreshClient:
        return AutoRefreshClient(hdrs, refresh_fn)

    async def _administered_orgs(client: AutoRefreshClient) -> tuple[list[dict], str | None]:
        """The Pages this token administers (``organizationAcls``), or the error."""
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/rest/organizationAcls",
            _PROVIDER,
            "list administered organizations",
            headers={**hdrs, "LinkedIn-Version": _VERSION},
            params={"q": "roleAssignee", "role": "ADMINISTRATOR", "count": "50"},
        )
        if err:
            return [], err
        return [e for e in resp.json().get("elements") or [] if isinstance(e, dict)], None

    async def _actor(org_id: str, purpose: str) -> tuple[str, str, str | None]:
        """``(actor URN, where it publishes, error)``: the Page in page mode, the member otherwise."""
        target_org = org_id or organization_id
        if account_type == "page" and target_org:
            return f"urn:li:organization:{target_org}", f"LinkedIn Page ({target_org})", None
        if account_type == "page":
            # An organization token has no member identity (no openid /
            # profile scope): it acts as the one Page it administers, or asks.
            async with _client() as client:
                acls, acl_err = await _administered_orgs(client)
            org_ids = sorted({_org_id(e) for e in acls if _org_id(e)})
            if len(org_ids) == 1:
                return f"urn:li:organization:{org_ids[0]}", f"LinkedIn Page ({org_ids[0]})", None
            return "", "", (
                f"Error: this is an organization (Page) token - pass org_id for {purpose}"
                + (f" (one of: {', '.join(org_ids)})" if org_ids else "")
                + (f". {acl_err}" if acl_err else ".")
            )
        async with _client() as client:
            me_resp, me_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v2/userinfo",
                _PROVIDER,
                f"get user info for {purpose}",
            )
        if me_err:
            return "", "", me_err
        person_id = me_resp.json().get("sub", "")
        if not person_id:
            return "", "", f"Error: Could not determine user ID for {purpose}."
        return f"urn:li:person:{person_id}", "your personal profile", None

    async def _put_upload(url: str, data: bytes, action: str) -> tuple[httpx.Response | None, str | None]:
        """PUT bytes to an upload URL LinkedIn returned; the token only goes to a LinkedIn host.

        The URL is an API answer, so it passes the outbound URL policy before
        any byte is sent, and a redirect is never followed.
        """
        if not is_https(url):
            return None, f"Error: LinkedIn returned no HTTPS upload URL to {action}."
        try:
            url = await social_media.check_public_url(url)
        except ValueError as exc:
            return None, f"Error: the LinkedIn URL to {action} was refused: {exc}"
        headers = {"Content-Type": "application/octet-stream"}
        if host_is(url, "linkedin.com"):
            headers["Authorization"] = hdrs["Authorization"]
        # The connection goes to an address checked when it opens (DNS rebinding).
        async with egress_client(
            timeout=social_media.UPLOAD_TIMEOUT_SECONDS, **guarded_client_options(allow_private=False)
        ) as client:
            return await safe_request(client, "put", url, _PROVIDER, action, content=data, headers=headers)

    async def _upload_video(client: AutoRefreshClient, owner: str, media: PostMedia) -> tuple[str, str | None]:
        """Videos API: initializeUpload, one PUT per upload instruction, finalizeUpload, processing."""
        resp, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/rest/videos?action=initializeUpload",
            _PROVIDER,
            f"initialize the upload of {media.label}",
            content=_json.dumps(
                {
                    "initializeUploadRequest": {
                        "owner": owner,
                        "fileSizeBytes": len(media.data),
                        "uploadCaptions": False,
                        "uploadThumbnail": False,
                    }
                }
            ),
        )
        if err:
            return "", err
        value = resp.json().get("value") or {}
        video_urn = str(value.get("video") or "")
        instructions = sorted(
            (i for i in value.get("uploadInstructions") or [] if isinstance(i, dict)),
            key=lambda i: int(i.get("firstByte") or 0),
        )
        if not video_urn or not instructions:
            return "", f"Error: LinkedIn returned no upload instructions for {media.label}."
        part_ids: list[str] = []
        for part in instructions:
            try:
                first, last = int(part["firstByte"]), int(part["lastByte"])
            except (KeyError, TypeError, ValueError):
                first, last = 0, -1
            if first < 0 or last < first or last >= len(media.data):
                return "", f"Error: LinkedIn returned an upload range outside {media.label}."
            put, err = await _put_upload(
                str(part.get("uploadUrl") or ""),
                media.data[first : last + 1],
                f"upload bytes {first}-{last} of {media.label}",
            )
            if err:
                return "", err
            etag = (put.headers.get("etag") or "").strip().strip('"')
            if not etag:
                return "", f"Error: LinkedIn returned no ETag for a part of {media.label}."
            part_ids.append(etag)
        _, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/rest/videos?action=finalizeUpload",
            _PROVIDER,
            f"finalize the upload of {media.label}",
            content=_json.dumps(
                {
                    "finalizeUploadRequest": {
                        "video": video_urn,
                        "uploadToken": value.get("uploadToken") or "",
                        "uploadedPartIds": part_ids,
                    }
                }
            ),
        )
        if err:
            return "", err
        waited = 0
        while waited < social_media.VIDEO_PROCESSING_WAIT_SECONDS:
            status_resp, status_err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/videos/{quote(video_urn, safe='')}",
                _PROVIDER,
                f"check the processing of {media.label}",
            )
            if status_err:
                break  # a token that may not read videos still publishes one
            info = status_resp.json()
            status = info.get("status")
            if status == "PROCESSING_FAILED":
                reason = info.get("processingFailureReason") or "no reason given"
                return "", (
                    f"Error: LinkedIn could not process {media.label} ({reason}). "
                    "Nothing has been published."
                )
            if status not in ("PROCESSING", "WAITING_UPLOAD"):
                break
            await social_media.pause(3)
            waited += 3
        return video_urn, None

    async def _upload(client: AutoRefreshClient, owner: str, media: PostMedia) -> tuple[str, str | None]:
        """The URN of *media* uploaded for *owner* (Images / Videos / Documents API), or the error."""
        if media.kind == "video":
            return await _upload_video(client, owner, media)
        endpoint = "documents" if media.kind == "document" else "images"
        resp, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/rest/{endpoint}?action=initializeUpload",
            _PROVIDER,
            f"initialize the upload of {media.label}",
            content=_json.dumps({"initializeUploadRequest": {"owner": owner}}),
        )
        if err:
            return "", err
        value = resp.json().get("value") or {}
        urn = str(value.get("image") or value.get("document") or "")
        if not urn:
            return "", f"Error: LinkedIn returned no upload URL for {media.label}."
        _, err = await _put_upload(
            str(value.get("uploadUrl") or ""), media.data, f"upload {media.label}"
        )
        return (urn, None) if err is None else ("", err)

    async def _resolve_media(
        client: AutoRefreshClient, urns: list[str]
    ) -> tuple[dict[str, dict], str | None]:
        """The Images / Videos / Documents API entry (``downloadUrl``, ``status``) of every URN."""
        by_type: dict[str, list[str]] = {}
        for urn in dict.fromkeys(urns):
            by_type.setdefault(_media_urn_type(urn), []).append(urn)
        resolved: dict[str, dict] = {}
        problem = None
        for kind, items in by_type.items():
            ids = ",".join(quote(u, safe="") for u in items)
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/{_MEDIA_ENDPOINTS[kind]}?ids=List({ids})",
                _PROVIDER,
                f"get the download links of {len(items)} {kind}(s)",
            )
            if err:
                problem = err
                continue
            results = resp.json().get("results") or {}
            resolved.update({k: v for k, v in results.items() if isinstance(v, dict)})
        return resolved, problem

    def _links_note(problem: str | None) -> list[str]:
        if not problem:
            return []
        note = f"Media download links unavailable: {problem}"
        if account_type != "page":
            note += (
                " (LinkedIn serves them only to a token with w_organization_social, which "
                "page mode requests; a personal token's w_member_social cannot read them)"
            )
        return [note]

    # ── Get my profile ────────────────────────────────────────────────────

    async def get_linkedin_profile() -> str:
        """Get the authenticated LinkedIn user's profile information."""
        if account_type == "page":
            return PAGE_ACCOUNT_PROFILE_ERROR
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/v2/userinfo",
                _PROVIDER,
                "get profile",
            )
            if err:
                return err
            data = resp.json()
            lines = [
                f"**{data.get('name', 'Unknown')}**",
                f"  Email: {data.get('email', 'N/A')}",
                f"  Sub: {data.get('sub', 'N/A')}",
            ]
            if data.get("picture"):
                lines.append(f"  Picture: {data['picture']}")
            return "\n".join(lines)

    # ── List administered organizations ───────────────────────────────────

    async def list_linkedin_organizations() -> str:
        """List LinkedIn Pages (organizations) administered by the authenticated user."""
        async with _client() as client:
            elements, err = await _administered_orgs(client)
            if err:
                return err
            return format_entity_list(
                elements,
                _format_org_acl,
                noun="administered LinkedIn Page",
                empty_msg="No LinkedIn Pages found where you are an administrator.",
            )

    # ── Get organization info ─────────────────────────────────────────────

    async def _organization_details(client: AutoRefreshClient, org: str) -> tuple[dict | None, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/rest/organizations/{quote(org, safe='')}",
            _PROVIDER,
            "get organization",
            headers={**hdrs, "LinkedIn-Version": _VERSION},
        )
        return (None, err) if err else (resp.json(), None)

    async def get_linkedin_organization(org_id: str = "") -> str:
        """Get details about a LinkedIn Page (organization) by ID.

        Without an ID: every Page the token administers (``organizationAcls``),
        the integration's configured Page included; the configured Page alone
        when the administered list cannot be read.
        """
        requested = str(org_id or "").strip()
        async with _client() as client:
            if requested:
                data, err = await _organization_details(client, requested)
                return err if err else _format_organization(
                    data or {}, configured=requested == organization_id
                )
            acls, acl_err = await _administered_orgs(client)
            org_ids = list(dict.fromkeys(oid for e in acls if (oid := _org_id(e))))
            if not org_ids and organization_id:
                org_ids = [organization_id]
            if not org_ids:
                return (
                    "Error: no organization ID given, and this token administers no "
                    "LinkedIn Page it can list"
                    + (f" ({acl_err})" if acl_err else "")
                    + ". Pass org_id, or connect the integration as a LinkedIn Page."
                )
            blocks: list[str] = []
            for oid in org_ids[:_MAX_ORGANIZATIONS_DETAILED]:
                data, err = await _organization_details(client, oid)
                blocks.append(
                    f"Organization {oid}: {err}"
                    if err
                    else _format_organization(data or {}, configured=oid == organization_id)
                )
        header = f"LinkedIn Pages this token administers ({len(org_ids)}):"
        if len(org_ids) > _MAX_ORGANIZATIONS_DETAILED:
            header += f" the first {_MAX_ORGANIZATIONS_DETAILED} are detailed - pass org_id for another."
        return header + "\n\n" + "\n\n".join(blocks)

    # ── Create post ───────────────────────────────────────────────────────

    async def create_linkedin_post(
        text: str,
        visibility: str = "PUBLIC",
        article_url: str = "",
        article_title: str = "",
        article_description: str = "",
        org_id: str = "",
        media_file_ids: Any = None,
        media_alt_texts: Any = None,
        media_title: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Create a LinkedIn post as either a personal user or a LinkedIn Page.

        For personal posts, the author is the authenticated user.
        For page posts, set org_id or configure the integration in page mode.
        Visibility: PUBLIC, CONNECTIONS (personal only).
        Optionally attach an article link with title and description, or
        files of the run (images, one video or one document) by file_id.
        """
        media: list[PostMedia] = []
        if media_file_ids:
            if article_url:
                return ToolResult(
                    tool_call_id="",
                    content=(
                        "Error: a LinkedIn post carries an article OR media, not both. Put the "
                        "link in the text, or drop article_url. Nothing has been published."
                    ),
                    is_error=True,
                )
            checked = await read_post_media(
                file_context, media_file_ids, _POST_MEDIA, alt_text=media_alt_texts
            )
            if isinstance(checked, str):
                return ToolResult(tool_call_id="", content=checked, is_error=True)
            media = checked

        author_urn, target, actor_err = await _actor(org_id, "posting")
        if actor_err:
            return with_summaries(actor_err, media)

        body: dict[str, Any] = {
            "author": author_urn,
            "commentary": text,
            "visibility": visibility,
            "distribution": {
                "feedDistribution": "MAIN_FEED",
                "targetEntities": [],
                "thirdPartyDistributionChannels": [],
            },
            "lifecycleState": "PUBLISHED",
        }

        if article_url:
            body["content"] = {
                "article": {
                    "source": article_url,
                    "title": article_title or article_url,
                    "description": article_description or "",
                },
            }

        async with _client() as client:
            urns: list[str] = []
            for item in media:
                urn, err = await _upload(client, author_urn, item)
                if err:
                    return with_summaries(f"{err}\nNothing has been published.", media)
                urns.append(urn)
            if len(media) == 1:
                entry: dict[str, Any] = {"id": urns[0]}
                if media[0].kind in ("video", "document"):
                    entry["title"] = media_title or media[0].filename
                elif media[0].alt_text:
                    entry["altText"] = media[0].alt_text
                body["content"] = {"media": entry}
            elif media:
                body["content"] = {
                    "multiImage": {
                        "images": [
                            {"id": urn, **({"altText": item.alt_text} if item.alt_text else {})}
                            for urn, item in zip(urns, media, strict=True)
                        ]
                    }
                }
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/rest/posts",
                _PROVIDER,
                "create post",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                content=_json.dumps(body),
            )
            if err:
                return with_summaries(err, media)
            post_id = resp.headers.get("x-restli-id", "unknown")
            answer = f"Post published successfully on {target}. Post ID: {post_id}"
            if post_id != "unknown":
                answer += f"\nWeb URL: {_post_web_url(post_id)}"
            if media:
                answer += f"\nMedia: {media_kinds(media)} ({', '.join(urns)})"
            return with_summaries(answer, media)

    # ── Delete post ───────────────────────────────────────────────────────

    async def delete_linkedin_post(post_urn: str) -> str:
        """Delete a LinkedIn post by its URN (e.g. urn:li:share:12345 or urn:li:ugcPost:12345)."""
        encoded = quote(post_urn, safe="")
        async with _client() as client:
            _, err = await safe_request(
                client,
                "delete",
                f"{_BASE_URL}/rest/posts/{encoded}",
                _PROVIDER,
                "delete post",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
            )
            if err:
                return err
            return f"Post {post_urn} deleted successfully."

    # ── Create comment ────────────────────────────────────────────────────

    async def create_linkedin_comment(
        post_urn: str,
        text: str,
        org_id: str = "",
        image_file_id: str = "",
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Comment on a LinkedIn post, optionally with an image of the run. For page comments, provide org_id."""
        media: list[PostMedia] = []
        if (image_file_id or "").strip():
            checked = await read_post_media(file_context, image_file_id, _COMMENT_MEDIA)
            if isinstance(checked, str):
                return ToolResult(tool_call_id="", content=checked, is_error=True)
            media = checked

        actor_urn, _target, actor_err = await _actor(org_id, "commenting")
        if actor_err:
            return with_summaries(actor_err, media)

        encoded = quote(post_urn, safe="")
        body: dict[str, Any] = {
            "actor": actor_urn,
            "message": {"text": text},
        }

        async with _client() as client:
            if media:
                urn, err = await _upload(client, actor_urn, media[0])
                if err:
                    return with_summaries(f"{err}\nNothing has been published.", media)
                body["content"] = [{"entity": {"image": urn}}]
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/rest/socialActions/{encoded}/comments",
                _PROVIDER,
                "create comment",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                content=_json.dumps(body),
            )
            if err:
                return with_summaries(err, media)
            answer = f"Comment posted successfully on {post_urn}."
            if media:
                answer += f" Image: {body['content'][0]['entity']['image']}."
            return with_summaries(answer, media)

    # ── Get post comments ─────────────────────────────────────────────────

    async def get_linkedin_comments(post_urn: str, count: int = 20) -> str:
        """Get comments on a LinkedIn post."""
        encoded = quote(post_urn, safe="")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/socialActions/{encoded}/comments",
                _PROVIDER,
                "get comments",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                params={"count": str(min(count, 100))},
            )
            if err:
                return err
            data = resp.json()
            elements = data.get("elements", [])
            image_urns = [urn for c in elements for urn in _comment_image_urns(c)]
            resolved, problem = (
                await _resolve_media(client, image_urns) if image_urns else ({}, None)
            )
            listing = format_entity_list(
                elements,
                lambda c: _format_li_comment(c, resolved),
                noun="comment",
                empty_msg=f"No comments found on {post_urn}.",
            )
            extra = _links_note(problem)
            if image_urns:
                extra.append(
                    "Tip: download_linkedin_media(media_urn) brings an image into the conversation."
                )
            return "\n".join([listing, *extra])

    # ── React to post ─────────────────────────────────────────────────────

    async def react_linkedin_post(
        post_urn: str,
        reaction: str = "LIKE",
    ) -> str:
        """React to a LinkedIn post. Reactions: LIKE, CELEBRATE, SUPPORT, LOVE, INSIGHTFUL, FUNNY."""
        encoded = quote(post_urn, safe="")
        body = {"root": post_urn, "reactionType": reaction.upper()}

        async with _client() as client:
            _, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/rest/socialActions/{encoded}/likes",
                _PROVIDER,
                f"react ({reaction})",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                content=_json.dumps(body),
            )
            if err:
                return err
            return f"Reacted with {reaction.upper()} on {post_urn}."

    # ── Get organization followers count ──────────────────────────────────

    async def get_linkedin_organization_followers(org_id: str = "") -> str:
        """Get the follower count and statistics for a LinkedIn Page."""
        target_org = org_id or organization_id
        if not target_org:
            return "Error: No organization ID provided."
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/networkSizes/urn:li:organization:{target_org}",
                _PROVIDER,
                "get followers count",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                params={"edgeType": "CompanyFollowedByMember"},
            )
            if err:
                return err
            data = resp.json()
            count = data.get("firstDegreeSize", "Unknown")
            return f"LinkedIn Page (org {target_org}) has {count} followers."

    # ── Get post statistics ───────────────────────────────────────────────

    async def get_linkedin_post_statistics(post_urn: str) -> str:
        """Get engagement statistics (likes, comments, shares, impressions) for a post."""
        encoded = quote(post_urn, safe="")
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/socialActions/{encoded}",
                _PROVIDER,
                "get post statistics",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
            )
            if err:
                return err
            data = resp.json()
            likes = data.get("likesSummary", {}).get("totalLikes", 0)
            comments = data.get("commentsSummary", {}).get("totalFirstLevelComments", 0)
            shares = data.get("shareCount", 0) if "shareCount" in data else "N/A"
            return f"Post statistics for {post_urn}:\n  Likes: {likes}\n  Comments: {comments}\n  Shares: {shares}"

    # ── Get organization page statistics ──────────────────────────────────

    async def get_linkedin_page_statistics(org_id: str = "") -> str:
        """Get page-level statistics for a LinkedIn Page (views, followers, engagement)."""
        target_org = org_id or organization_id
        if not target_org:
            return "Error: No organization ID provided."
        async with _client() as client:
            params: dict[str, str] = {
                "q": "organizationalEntity",
                "organizationalEntity": f"urn:li:organization:{target_org}",
            }
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/organizationalEntityShareStatistics",
                _PROVIDER,
                "get page statistics",
                headers={**hdrs, "LinkedIn-Version": _VERSION},
                params=params,
            )
            if err:
                return err
            data = resp.json()
            elements = data.get("elements", [])
            if not elements:
                return f"No statistics available for organization {target_org}."
            stats = elements[0].get("totalShareStatistics", {})
            lines = [f"Page statistics for organization {target_org}:"]
            for k, v in stats.items():
                lines.append(f"  {k}: {v}")
            return "\n".join(lines)

    # ── Read posts and their media ────────────────────────────────────────

    def _media_tip(posts: list[dict]) -> list[str]:
        if any(_post_media_entries(p) for p in posts):
            return [
                "Tip: download_linkedin_media(media_urn) brings a media into the conversation "
                "as a file (a download card and a file_id)."
            ]
        return []

    async def get_linkedin_post(post_urn: str) -> str:
        """Get a LinkedIn post by URN: its text, web URL and media with their download links."""
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/posts/{quote(post_urn.strip(), safe='')}",
                _PROVIDER,
                "get post",
            )
            if err:
                return err
            post = resp.json()
            entries = [str(e["id"]) for e in _post_media_entries(post)]
            resolved, problem = await _resolve_media(client, entries) if entries else ({}, None)
        return "\n".join([_format_li_post(post, resolved), *_links_note(problem), *_media_tip([post])])

    async def list_linkedin_posts(org_id: str = "", count: int = 10) -> str:
        """List the latest posts of the Page (page mode or org_id) or of the member, with their media."""
        author_urn, _target, actor_err = await _actor(org_id, "listing posts")
        if actor_err:
            return actor_err
        size = max(1, min(int(count or 10), 100))
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/posts?q=author&author={quote(author_urn, safe='')}"
                f"&count={size}&sortBy=LAST_MODIFIED",
                _PROVIDER,
                "list posts",
                headers={**hdrs, "X-RestLi-Method": "FINDER"},
            )
            if err:
                return err
            posts = [p for p in resp.json().get("elements") or [] if isinstance(p, dict)]
            if not posts:
                return f"No posts found for {author_urn}."
            entries = [str(e["id"]) for p in posts for e in _post_media_entries(p)]
            resolved, problem = await _resolve_media(client, entries) if entries else ({}, None)
        body = "\n\n".join(_format_li_post(p, resolved) for p in posts)
        return "\n".join(
            [f"Posts by {author_urn} ({len(posts)}):\n\n{body}", *_links_note(problem), *_media_tip(posts)]
        )

    async def download_linkedin_media(media_urn: str) -> str | ToolResult:
        """Download a post's image, video or document by URN into the conversation."""
        media_urn = (media_urn or "").strip()
        kind = _media_urn_type(media_urn)
        if not kind:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: '{media_urn}' is not a LinkedIn media URN (urn:li:image:..., "
                    "urn:li:video:... or urn:li:document:... from get_linkedin_post)."
                ),
                is_error=True,
            )
        async with _client() as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{_BASE_URL}/rest/{_MEDIA_ENDPOINTS[kind]}/{quote(media_urn, safe='')}",
                _PROVIDER,
                f"get {kind} {media_urn}",
            )
        if err:
            return err
        info = resp.json()
        url = str(info.get("downloadUrl") or "")
        if not url:
            return (
                f"LinkedIn gives no download link for {media_urn} "
                f"(status {info.get('status') or 'unknown'})."
            )
        fetched = await fetch_public_media(url, source="LinkedIn")
        if isinstance(fetched, FetchError):
            return fetched
        data, declared = fetched.data, fetched.content_type
        content_type = media_type(data, declared, fetched.filename)
        stem = f"linkedin-{kind}-{media_urn.rsplit(':', 1)[-1]}"
        return await deliver_file(
            data=data,
            filename=with_extension(stem, content_type),
            content_type=content_type,
            source="LinkedIn",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url), ("Thumbnail URL", info.get("thumbnail"))],
        )

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("get_linkedin_profile", get_linkedin_profile)
    executor.register("list_linkedin_organizations", list_linkedin_organizations)
    executor.register("get_linkedin_organization", get_linkedin_organization)
    executor.register("create_linkedin_post", create_linkedin_post)
    executor.register("delete_linkedin_post", delete_linkedin_post)
    executor.register("create_linkedin_comment", create_linkedin_comment)
    executor.register("get_linkedin_comments", get_linkedin_comments)
    executor.register("react_linkedin_post", react_linkedin_post)
    executor.register(
        "get_linkedin_organization_followers", get_linkedin_organization_followers
    )
    executor.register("get_linkedin_post_statistics", get_linkedin_post_statistics)
    executor.register("get_linkedin_page_statistics", get_linkedin_page_statistics)
    executor.register("get_linkedin_post", get_linkedin_post)
    executor.register("list_linkedin_posts", list_linkedin_posts)
    executor.register("download_linkedin_media", download_linkedin_media)

    source = "integration:linkedin"
    return [
        ToolDefinition(
            name="get_linkedin_profile",
            description=(
                "Get the authenticated LinkedIn member's profile (name, email, picture). "
                "Personal accounts only: an integration connected as a LinkedIn Page has "
                "no member profile - use the organization tools."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="list_linkedin_organizations",
            description="List LinkedIn Pages (organizations) where the authenticated user is an administrator. Returns organization IDs needed for page-level operations.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_linkedin_organization",
            description="Get details about a LinkedIn Page (organization) — name, description, industry, size, website.",
            input_schema={
                "type": "object",
                "properties": {
                    "org_id": {
                        "type": "string",
                        "description": (
                            "LinkedIn organization ID. If omitted, details every Page "
                            "the token administers (the configured Page included)."
                        ),
                    },
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_linkedin_post",
            description=(
                "Create a LinkedIn post on either a personal profile or a LinkedIn Page. "
                "Supports text posts, article shares (URL with title/description) and media "
                "posts: files of this conversation by file_id (attached, generated or "
                "downloaded) - 1 to 20 images (JPG, PNG or GIF, under 36,152,320 pixels, a GIF "
                "up to 250 frames), OR one MP4 video (75 KB to 500 MB, 3 seconds to 30 "
                "minutes), OR one document (PDF, DOC, DOCX, PPT or PPTX, up to 100 MB and 300 "
                "pages). A file outside these limits is refused before anything is published; "
                "the result carries the visual summary of every image published. "
                "For page posts, provide org_id or configure the integration in page mode."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Post text/commentary"},
                    "visibility": {
                        "type": "string",
                        "description": "Post visibility: PUBLIC or CONNECTIONS (personal only)",
                        "default": "PUBLIC",
                        "enum": ["PUBLIC", "CONNECTIONS"],
                    },
                    "article_url": {
                        "type": "string",
                        "description": "URL of article to share (optional)",
                    },
                    "article_title": {
                        "type": "string",
                        "description": "Title for the shared article (optional)",
                    },
                    "article_description": {
                        "type": "string",
                        "description": "Description for the shared article (optional)",
                    },
                    "org_id": {
                        "type": "string",
                        "description": "Organization ID for page posts (overrides integration config)",
                    },
                    "media_file_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "file_ids of files of this conversation to publish with the post "
                            "(images, one video or one document); not combined with article_url"
                        ),
                    },
                    "media_alt_texts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Alt text of each image, in the order of media_file_ids",
                    },
                    "media_title": {
                        "type": "string",
                        "description": "Title shown on a video or document post (default: the file name)",
                    },
                },
                "required": ["text"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="delete_linkedin_post",
            description="Delete a LinkedIn post by its URN (e.g. urn:li:share:12345).",
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {
                        "type": "string",
                        "description": "Full post URN (e.g. urn:li:share:12345 or urn:li:ugcPost:12345)",
                    },
                },
                "required": ["post_urn"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_linkedin_comment",
            description=(
                "Post a comment on a LinkedIn post, optionally with one image of this "
                "conversation by file_id (JPG, PNG or GIF, under 36,152,320 pixels). Can "
                "comment as personal user or as a LinkedIn Page."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {
                        "type": "string",
                        "description": "URN of the post to comment on",
                    },
                    "text": {"type": "string", "description": "Comment text"},
                    "org_id": {
                        "type": "string",
                        "description": "Organization ID to comment as page (optional)",
                    },
                    "image_file_id": {
                        "type": "string",
                        "description": "file_id of an image of this conversation to attach (optional)",
                    },
                },
                "required": ["post_urn", "text"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_linkedin_post",
            description=(
                "Get a LinkedIn post by URN: author, text, visibility, web URL, article, and "
                "every image, video or document it carries with its media URN and download "
                "link. Reading a post needs page mode (r_organization_social) for a Page post; "
                "LinkedIn serves member posts only to apps approved for r_member_social."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {
                        "type": "string",
                        "description": "Post URN (e.g. urn:li:share:12345 or urn:li:ugcPost:12345)",
                    },
                },
                "required": ["post_urn"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_linkedin_posts",
            description=(
                "List the latest posts of a LinkedIn Page (page mode or org_id) - or of the "
                "member, for apps approved for r_member_social - with each post's URN, text, "
                "web URL and media (URN + download link)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "org_id": {
                        "type": "string",
                        "description": "Organization ID. If omitted, uses the configured page.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of posts (max 100)",
                        "default": 10,
                    },
                },
                "required": [],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="download_linkedin_media",
            workspace_ingest=True,
            description=(
                "Download an image, video or document of a LinkedIn post or comment by its "
                "media URN (from get_linkedin_post, list_linkedin_posts or "
                "get_linkedin_comments) into the conversation: a download card, its download "
                "link and a file_id to forward to any tool that takes a file (up to 100 MB). "
                "Include the returned [[FILE:...]] marker verbatim so the user gets a download "
                "card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "media_urn": {
                        "type": "string",
                        "description": "urn:li:image:..., urn:li:video:... or urn:li:document:...",
                    },
                },
                "required": ["media_urn"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_linkedin_comments",
            description="Get comments on a LinkedIn post, with the image each comment carries (URN + download link).",
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {"type": "string", "description": "URN of the post"},
                    "count": {
                        "type": "integer",
                        "description": "Number of comments to return (max 100)",
                        "default": 20,
                    },
                },
                "required": ["post_urn"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="react_linkedin_post",
            description="React to a LinkedIn post (like, celebrate, support, love, insightful, funny).",
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {
                        "type": "string",
                        "description": "URN of the post to react to",
                    },
                    "reaction": {
                        "type": "string",
                        "description": "Reaction type",
                        "default": "LIKE",
                        "enum": [
                            "LIKE",
                            "CELEBRATE",
                            "SUPPORT",
                            "LOVE",
                            "INSIGHTFUL",
                            "FUNNY",
                        ],
                    },
                },
                "required": ["post_urn"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_linkedin_organization_followers",
            description="Get the follower count for a LinkedIn Page.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "org_id": {
                        "type": "string",
                        "description": "Organization ID. If omitted, uses the configured page.",
                    },
                },
                "required": [],
            },
            source=source,
        ),
        ToolDefinition(
            name="get_linkedin_post_statistics",
            description="Get engagement statistics (likes, comments, shares) for a LinkedIn post.",
            priority=0,
            input_schema={
                "type": "object",
                "properties": {
                    "post_urn": {"type": "string", "description": "URN of the post"},
                },
                "required": ["post_urn"],
            },
            source=source,
        ),
        ToolDefinition(
            name="get_linkedin_page_statistics",
            description="Get page-level statistics for a LinkedIn Page — share statistics, engagement, impressions.",
            priority=2,
            input_schema={
                "type": "object",
                "properties": {
                    "org_id": {
                        "type": "string",
                        "description": "Organization ID. If omitted, uses the configured page.",
                    },
                },
                "required": [],
            },
            source=source,
        ),
    ]
