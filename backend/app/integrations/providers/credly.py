# SPDX-License-Identifier: Apache-2.0

"""Credly integration – Badge Templates, Issued Badges, Organizations.

Implements tools against the Credly API v1.
Docs: https://docs.credly.com/browse/reference/get_v1-organizations-organization-id-badge-templates
Auth: Basic Auth with authorization token (Base64-encoded "token:").
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.core.egress import egress_client
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, ToolDefinition

_PROVIDER = "Credly"
_BASE_URL = "https://api.credly.com/v1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _image_url(obj: dict[str, Any]) -> str:
    """The badge image URL of a Credly template or issued badge, or ``N/A``."""
    image = obj.get("image")
    nested = image.get("url") if isinstance(image, dict) else None
    return str(obj.get("image_url") or nested or "N/A")


def _headers(token: str) -> dict[str, str]:
    """Build Basic Auth headers for the Credly API.

    Credly uses Basic Auth where the token is base64-encoded as ``token:``.
    """
    encoded = base64.b64encode(f"{token}:".encode()).decode()
    return {"Authorization": f"Basic {encoded}", "Accept": "application/json"}


async def _paginated_fetch(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    params: dict[str, Any] | None = None,
    *,
    max_items: int = 200,
) -> tuple[list[dict], dict, str | None]:
    """Fetch pages from Credly's page-based pagination.

    Returns ``(items, metadata, error)`` where *error* is a human-readable
    string on failure or ``None`` on success.
    """
    params = dict(params or {})
    items: list[dict] = []
    page = 1
    metadata: dict = {}

    while True:
        params["page"] = page
        resp, err = await safe_request(
            client,
            "get",
            url,
            _PROVIDER,
            "fetch page",
            headers=headers,
            params=params,
        )
        if err:
            return [], {}, err
        data = resp.json()
        batch = data.get("data", [])
        if isinstance(batch, dict):
            batch = [batch]
        items.extend(batch)
        metadata = data.get("metadata", {})

        total_pages = metadata.get("total_pages", 1)
        if page >= total_pages or len(items) >= max_items:
            break
        page += 1

    return items[:max_items], metadata, None


# ---------------------------------------------------------------------------
# Credly tools
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    organization_id, token = parse_compound_token(token, 2)
    headers = _headers(token)
    org_base = f"{_BASE_URL}/organizations/{organization_id}" if organization_id else ""

    # ── Organization ─────────────────────────────────────────────────────

    async def get_credly_organization() -> str:
        """Get the Credly organization profile."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                org_base,
                _PROVIDER,
                "get organization",
                headers=headers,
            )
            if err:
                return err
            o = resp.json().get("data", resp.json())
            lines = [
                f"Organization: {o.get('name', 'N/A')} (ID: {o.get('id', 'N/A')})",
                f"Vanity URL: {o.get('vanity_url', 'N/A')}",
                f"Verified: {o.get('verified', 'N/A')}",
                f"Badge Count: {o.get('badge_templates_count', 'N/A')}",
            ]
            return "\n".join(lines)

    # ── Badge Templates ──────────────────────────────────────────────────

    async def list_credly_badge_templates(
        query: str = "",
        state: str = "",
        sort: str = "",
    ) -> str:
        """List badge templates from Credly. Optionally filter by query, state, or sort."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {}
            filters: list[str] = []
            if query:
                filters.append(f"query::{query}")
            if state:
                filters.append(f"state::{state}")
            if filters:
                params["filter"] = "|".join(filters)
            if sort:
                params["sort"] = sort
            items, meta, err = await _paginated_fetch(
                client,
                f"{org_base}/badge_templates",
                headers,
                params,
            )
            if err:
                return err
            total = meta.get("total_count", len(items))

            def _fmt(t: dict) -> str:
                skills = ", ".join(t.get("skills", [])) or "None"
                return (
                    f"[{t.get('id', '?')}] {t.get('name', 'Untitled')} — "
                    f"State: {t.get('state', 'N/A')} | "
                    f"Public: {t.get('public', 'N/A')} | "
                    f"Issued: {t.get('badges_count', 0)} | "
                    f"Skills: {skills[:100]} | "
                    f"Created: {t.get('created_at', 'N/A')}"
                )

            return format_entity_list(
                items,
                _fmt,
                noun="badge template",
                total=total,
            )

    async def get_credly_badge_template(badge_template_id: str) -> str:
        """Get full details of a specific badge template by ID."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{org_base}/badge_templates/{badge_template_id}",
                _PROVIDER,
                "get badge template",
                headers=headers,
            )
            if err:
                return err
            t = resp.json().get("data", resp.json())

            skills = ", ".join(t.get("skills", [])) or "None"
            reporting_tags = ", ".join(t.get("reporting_tags", [])) or "None"

            lines = [
                f"Badge Template: {t.get('name', 'Untitled')} (ID: {t.get('id')})",
                f"Description: {t.get('description', 'N/A')}",
                f"State: {t.get('state', 'N/A')}",
                f"Public: {t.get('public', 'N/A')}",
                f"Badges Issued: {t.get('badges_count', 0)}",
                f"Vanity Slug: {t.get('vanity_slug', 'N/A')}",
                f"URL: {t.get('url', 'N/A')}",
                f"Image URL: {_image_url(t)}",
                f"Certification: {t.get('certification', 'N/A')}",
                f"Level: {t.get('level', 'N/A')}",
                f"Cost: {t.get('cost', 'N/A')}",
                f"Time to Earn: {t.get('time_to_earn', 'N/A')}",
                f"Category: {t.get('type_category', 'N/A')}",
                f"Criteria URL: {t.get('global_activity_url', 'N/A')}",
                f"Allow Duplicates: {t.get('allow_duplicate_badges', 'N/A')}",
                f"Skills: {skills}",
                f"Reporting Tags: {reporting_tags}",
                f"Created: {t.get('created_at', 'N/A')}",
                f"Updated: {t.get('updated_at', 'N/A')}",
            ]

            # Expiration settings
            expires_at = t.get("default_expires_at")
            expires_date = t.get("default_expires_at_date")
            if expires_at:
                lines.append(f"Default Expiration (duration): {expires_at}")
            if expires_date:
                lines.append(f"Default Expiration (date): {expires_date}")

            owner = t.get("owner", {})
            if owner:
                lines.append(
                    f"\nOwner: {owner.get('name', 'N/A')} (ID: {owner.get('id', 'N/A')})"
                )

            alignments = t.get("alignments", [])
            if alignments:
                lines.append(f"\nAlignments ({len(alignments)}):")
                for a in alignments:
                    lines.append(f"  {a.get('name', 'N/A')} — {a.get('url', 'N/A')}")

            activities = t.get("badge_template_activities", [])
            if activities:
                lines.append(f"\nActivities ({len(activities)}):")
                for a in activities:
                    lines.append(
                        f"  [{a.get('activity_type', '?')}] {a.get('title', 'Untitled')} — "
                        f"{a.get('activity_url', 'N/A')}"
                    )

            recommendations = t.get("recommendations", [])
            if recommendations:
                lines.append(f"\nRecommendations ({len(recommendations)}):")
                for r in recommendations:
                    lines.append(
                        f"  [{r.get('type', '?')}] {r.get('title', 'Untitled')} — {r.get('activity_url', 'N/A')}"
                    )

            return "\n".join(lines)

    # ── Issued Badges ────────────────────────────────────────────────────

    async def list_credly_issued_badges(
        query: str = "",
        state: str = "",
        badge_template_id: str = "",
        sort: str = "",
    ) -> str:
        """List issued badges from Credly. Optionally filter by query, state, badge template, or sort."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        async with egress_client(timeout=30) as client:
            params: dict[str, Any] = {}
            filters: list[str] = []
            if query:
                filters.append(f"query::{query}")
            if state:
                filters.append(f"state::{state}")
            if badge_template_id:
                filters.append(f"badge_template_id::{badge_template_id}")
            if filters:
                params["filter"] = "|".join(filters)
            if sort:
                params["sort"] = sort
            items, meta, err = await _paginated_fetch(
                client,
                f"{org_base}/badges",
                headers,
                params,
            )
            if err:
                return err
            total = meta.get("total_count", len(items))

            def _fmt(b: dict) -> str:
                template = b.get("badge_template") or {}
                template_name = template.get("name", "N/A")
                return (
                    f"[{b.get('id', '?')}] {template_name} → "
                    f"{b.get('issued_to', 'N/A')} ({b.get('recipient_email', 'N/A')}) — "
                    f"State: {b.get('state', 'N/A')} | "
                    f"Issued: {b.get('issued_at_date', 'N/A')} | "
                    f"Expires: {b.get('expires_at_date', 'Never')}"
                )

            return format_entity_list(
                items,
                _fmt,
                noun="issued badge",
                total=total,
            )

    async def get_credly_issued_badge(badge_id: str) -> str:
        """Get full details of a specific issued badge by ID."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{org_base}/badges/{badge_id}",
                _PROVIDER,
                "get issued badge",
                headers=headers,
            )
            if err:
                return err
            b = resp.json().get("data", resp.json())

            template = b.get("badge_template", {})
            template_name = template.get("name", "N/A") if template else "N/A"
            template_id = template.get("id", "N/A") if template else "N/A"

            user = b.get("user", {})
            user_name = (
                f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                if user
                else "N/A"
            )
            user_email = user.get("email", "N/A") if user else "N/A"

            lines = [
                f"Issued Badge: {template_name} (Badge ID: {b.get('id')})",
                f"Badge Template ID: {template_id}",
                f"Issued To: {b.get('issued_to', 'N/A')}",
                f"Recipient Email: {b.get('recipient_email', 'N/A')}",
                f"User: {user_name} ({user_email})",
                f"State: {b.get('state', 'N/A')}",
                f"Locale: {b.get('locale', 'N/A')}",
                f"Public: {b.get('public', 'N/A')}",
                f"Issued At: {b.get('issued_at', 'N/A')}",
                f"Expires At: {b.get('expires_at', 'Never')}",
                f"Image URL: {_image_url(b)}",
                f"Created: {b.get('created_at', 'N/A')}",
                f"Updated: {b.get('updated_at', 'N/A')}",
                f"Country: {b.get('country', 'N/A')}",
                f"State/Province: {b.get('state_or_province', 'N/A')}",
                f"Issuer Earner ID: {b.get('issuer_earner_id', 'N/A')}",
            ]

            if b.get("revocation_reason"):
                lines.append(f"Revocation Reason: {b['revocation_reason']}")

            badge_url = b.get("badge_url") or b.get("accept_badge_url")
            if badge_url:
                lines.append(f"Badge URL: {badge_url}")

            evidence = b.get("evidence", [])
            if evidence:
                lines.append(f"\nEvidence ({len(evidence)}):")
                for e in evidence:
                    etype = e.get("type", "Unknown")
                    if etype == "UrlEvidence":
                        lines.append(
                            f"  [{etype}] {e.get('name', 'N/A')} — {e.get('value', 'N/A')}"
                        )
                    elif etype == "PlainTextEvidence":
                        lines.append(
                            f"  [{etype}] {e.get('title', 'N/A')} — {e.get('description', 'N/A')[:200]}"
                        )
                    elif etype == "IdEvidence":
                        lines.append(
                            f"  [{etype}] {e.get('title', 'N/A')} — {e.get('description', 'N/A')}"
                        )
                    else:
                        lines.append(
                            f"  [{etype}] {e.get('name', e.get('title', 'N/A'))}"
                        )

            if template:
                skills = ", ".join(template.get("skills", [])) or "None"
                lines.append(f"\nBadge Template Skills: {skills}")
                lines.append(f"Badge Template State: {template.get('state', 'N/A')}")

            return "\n".join(lines)

    # ── Issue Badge ──────────────────────────────────────────────────────

    async def issue_credly_badge(
        badge_template_id: str,
        recipient_email: str,
        issued_to_first_name: str,
        issued_to_last_name: str,
        issued_at: str = "",
        expires_at: str = "",
        issuer_earner_id: str = "",
        locale: str = "en",
        suppress_badge_notification_email: bool = False,
    ) -> str:
        """Issue a badge to a recipient via Credly."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        payload: dict[str, Any] = {
            "badge_template_id": badge_template_id,
            "recipient_email": recipient_email,
            "issued_to_first_name": issued_to_first_name,
            "issued_to_last_name": issued_to_last_name,
            "locale": locale,
            "suppress_badge_notification_email": suppress_badge_notification_email,
        }
        if issued_at:
            payload["issued_at"] = issued_at
        if expires_at:
            payload["expires_at"] = expires_at
        if issuer_earner_id:
            payload["issuer_earner_id"] = issuer_earner_id

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{org_base}/badges",
                _PROVIDER,
                "issue badge",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            b = resp.json().get("data", resp.json())
            return (
                f"Badge issued successfully!\n"
                f"Badge ID: {b.get('id', 'N/A')}\n"
                f"Template: {b.get('badge_template', {}).get('name', 'N/A')}\n"
                f"Issued To: {b.get('issued_to', 'N/A')} ({b.get('recipient_email', 'N/A')})\n"
                f"State: {b.get('state', 'N/A')}\n"
                f"Accept URL: {b.get('accept_badge_url', 'N/A')}"
            )

    # ── Revoke Badge ─────────────────────────────────────────────────────

    async def revoke_credly_badge(badge_id: str, reason: str = "") -> str:
        """Revoke a previously issued badge."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        payload: dict[str, Any] = {}
        if reason:
            payload["reason"] = reason

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "put",
                f"{org_base}/badges/{badge_id}/revoke",
                _PROVIDER,
                "revoke badge",
                headers={**headers, "Content-Type": "application/json"},
                json=payload,
            )
            if err:
                return err
            b = resp.json().get("data", resp.json())
            return (
                f"Badge revoked successfully.\n"
                f"Badge ID: {b.get('id', 'N/A')}\n"
                f"State: {b.get('state', 'N/A')}\n"
                f"Revocation Reason: {b.get('revocation_reason', 'N/A')}"
            )

    # ── Replace Badge ────────────────────────────────────────────────────

    async def replace_credly_badge(
        badge_template_id: str,
        recipient_email: str,
        issued_to_first_name: str,
        issued_to_last_name: str,
        issued_at: str = "",
        expires_at: str = "",
    ) -> str:
        """Replace an existing badge with a new one (re-issue to same recipient)."""
        if not organization_id:
            return "Error: Organization ID is not configured. Edit the integration and set the Organization ID."
        payload: dict[str, Any] = {
            "badge_template_id": badge_template_id,
            "recipient_email": recipient_email,
            "issued_to_first_name": issued_to_first_name,
            "issued_to_last_name": issued_to_last_name,
        }
        if issued_at:
            payload["issued_at"] = issued_at
        if expires_at:
            payload["expires_at"] = expires_at

        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{org_base}/badges",
                _PROVIDER,
                "replace badge",
                headers={**headers, "Content-Type": "application/json"},
                json={**payload, "duplicate_behavior": "replace"},
            )
            if err:
                return err
            b = resp.json().get("data", resp.json())
            return (
                f"Badge replaced successfully!\n"
                f"New Badge ID: {b.get('id', 'N/A')}\n"
                f"Template: {b.get('badge_template', {}).get('name', 'N/A')}\n"
                f"Issued To: {b.get('issued_to', 'N/A')} ({b.get('recipient_email', 'N/A')})\n"
                f"State: {b.get('state', 'N/A')}"
            )

    # ── Register ─────────────────────────────────────────────────────────

    executor.register("get_credly_organization", get_credly_organization)
    executor.register("list_credly_badge_templates", list_credly_badge_templates)
    executor.register("get_credly_badge_template", get_credly_badge_template)
    executor.register("list_credly_issued_badges", list_credly_issued_badges)
    executor.register("get_credly_issued_badge", get_credly_issued_badge)
    executor.register("issue_credly_badge", issue_credly_badge)
    executor.register("revoke_credly_badge", revoke_credly_badge)
    executor.register("replace_credly_badge", replace_credly_badge)

    return [
        # Organization
        ToolDefinition(
            name="get_credly_organization",
            description="Get the Credly organization profile including name, verification status, and badge count.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source="integration:credly",
            priority=2,
        ),
        # Badge Templates
        ToolDefinition(
            name="list_credly_badge_templates",
            description=(
                "List badge templates from Credly with their state, skills, and issue count. "
                "Optionally filter by search query, state (active, archived, draft), "
                "or sort by name, created_at, updated_at, badges_count (prefix with - for descending)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search by badge template name or description",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["active", "archived", "draft"],
                        "description": "Filter by template state",
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort results: name, created_at, updated_at, badges_count (prefix with - for descending, e.g. -created_at)",
                    },
                },
                "required": [],
            },
            source="integration:credly",
            priority=0,
        ),
        ToolDefinition(
            name="get_credly_badge_template",
            description="Get full details of a specific Credly badge template including description, skills, activities, alignments, and recommendations.",
            input_schema={
                "type": "object",
                "properties": {
                    "badge_template_id": {
                        "type": "string",
                        "description": "Credly badge template ID (UUID)",
                    },
                },
                "required": ["badge_template_id"],
            },
            source="integration:credly",
            priority=1,
        ),
        # Issued Badges
        ToolDefinition(
            name="list_credly_issued_badges",
            description=(
                "List issued badges from Credly with recipient info, state, and dates. "
                "Optionally filter by search query (recipient name/email), state (pending, accepted, rejected, revoked), "
                "badge template ID, or sort by issued_at, created_at, state_updated_at (prefix with - for descending)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search by recipient name or email",
                    },
                    "state": {
                        "type": "string",
                        "enum": ["pending", "accepted", "rejected", "revoked"],
                        "description": "Filter by badge state",
                    },
                    "badge_template_id": {
                        "type": "string",
                        "description": "Filter by badge template ID",
                    },
                    "sort": {
                        "type": "string",
                        "description": "Sort results: issued_at, created_at, state_updated_at (prefix with - for descending)",
                    },
                },
                "required": [],
            },
            source="integration:credly",
            priority=0,
        ),
        ToolDefinition(
            name="get_credly_issued_badge",
            description="Get full details of a specific issued badge including recipient, state, evidence, and badge template info.",
            input_schema={
                "type": "object",
                "properties": {
                    "badge_id": {
                        "type": "string",
                        "description": "Issued badge ID (UUID)",
                    },
                },
                "required": ["badge_id"],
            },
            source="integration:credly",
            priority=1,
        ),
        # Issue Badge
        ToolDefinition(
            name="issue_credly_badge",
            description="Issue a badge to a recipient via Credly. Requires badge template ID, recipient email, and first/last name.",
            input_schema={
                "type": "object",
                "properties": {
                    "badge_template_id": {
                        "type": "string",
                        "description": "Badge template ID to issue",
                    },
                    "recipient_email": {
                        "type": "string",
                        "description": "Recipient's email address",
                    },
                    "issued_to_first_name": {
                        "type": "string",
                        "description": "Recipient's first name",
                    },
                    "issued_to_last_name": {
                        "type": "string",
                        "description": "Recipient's last name",
                    },
                    "issued_at": {
                        "type": "string",
                        "description": "Issue date (YYYY-MM-DD HH:MM:SS -0500 format). Defaults to now.",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Expiration date. Set to 'false' for no expiration.",
                    },
                    "issuer_earner_id": {
                        "type": "string",
                        "description": "Optional external ID for the earner in your system",
                    },
                    "locale": {
                        "type": "string",
                        "enum": [
                            "en",
                            "fr",
                            "de",
                            "ko",
                            "ja",
                            "zh-CN",
                            "pt",
                            "es-ES",
                            "es-US",
                            "fr-CA",
                            "pt-BR",
                        ],
                        "description": "Notification email language (default: en)",
                    },
                    "suppress_badge_notification_email": {
                        "type": "boolean",
                        "description": "If true, don't send notification email (default: false)",
                    },
                },
                "required": [
                    "badge_template_id",
                    "recipient_email",
                    "issued_to_first_name",
                    "issued_to_last_name",
                ],
            },
            source="integration:credly",
            priority=0,
        ),
        # Revoke Badge
        ToolDefinition(
            name="revoke_credly_badge",
            description="Revoke a previously issued Credly badge. Optionally provide a reason for revocation.",
            input_schema={
                "type": "object",
                "properties": {
                    "badge_id": {
                        "type": "string",
                        "description": "Issued badge ID to revoke",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Reason for revocation (optional)",
                    },
                },
                "required": ["badge_id"],
            },
            source="integration:credly",
            priority=1,
        ),
        # Replace Badge
        ToolDefinition(
            name="replace_credly_badge",
            description="Replace an existing Credly badge with a new one (re-issue to the same recipient, replacing the previous badge).",
            input_schema={
                "type": "object",
                "properties": {
                    "badge_template_id": {
                        "type": "string",
                        "description": "Badge template ID to issue",
                    },
                    "recipient_email": {
                        "type": "string",
                        "description": "Recipient's email address",
                    },
                    "issued_to_first_name": {
                        "type": "string",
                        "description": "Recipient's first name",
                    },
                    "issued_to_last_name": {
                        "type": "string",
                        "description": "Recipient's last name",
                    },
                    "issued_at": {
                        "type": "string",
                        "description": "Issue date (optional)",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": "Expiration date (optional)",
                    },
                },
                "required": [
                    "badge_template_id",
                    "recipient_email",
                    "issued_to_first_name",
                    "issued_to_last_name",
                ],
            },
            source="integration:credly",
            priority=1,
        ),
    ]
