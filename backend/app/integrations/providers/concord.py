# SPDX-License-Identifier: Apache-2.0

"""Concord integration — contract lifecycle management (CLM).

API docs: https://api.doc.concordnow.com/
Auth: API key (passed via ``X-API-KEY`` header)
Base URL: https://api.concordnow.com/api/rest/1


Endpoints used:
  - /user/me — current user info
  - /user/me/organizations — list organisations
  - /user/me/organizations/{orgId}/agreements — list / count agreements
  - /organizations/{orgId}/agreements/{uid} — get / patch / delete agreement
  - /organizations/{orgId}/agreements/{uid}/summary — agreement summary
  - /organizations/{orgId}/agreements/{uid}/metadata — agreement metadata
  - /organizations/{orgId}/agreements/{uid}/signature — signature config
  - /organizations/{orgId}/agreements/{uid}/signature/request — send for signature
  - /organizations/{orgId}/agreements/{uid}/signature/slots — configure signers
  - /organizations/{orgId}/agreements/{uid}/members — collaboration
  - /organizations/{orgId}/agreements/{uid}/comments — comments
  - /organizations/{orgId}/agreements/{uid}/activities — activity log
  - /organizations/{orgId}/templates/automated — automated templates
  - /organizations/{orgId}/templates/automated/{uid}/use — create from template
  - /organizations/{orgId}/folders — folders
  - /organizations/{orgId}/tags — tags
  - /organizations/{orgId}/clauses — clause library
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    deliver_file,
    fetch_file,
    file_content_type,
    read_pipe_file,
    uploaded_file_answer,
)
from app.integrations.http import (
    format_entity_list,
    parse_compound_token,
    safe_request,
)
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.concordnow.com/api/rest/1"
_PROVIDER = "Concord"
#: Largest agreement document or attachment downloaded or uploaded.
_MAX_FILE_BYTES = 50 * 1024 * 1024
_WORD_TYPES = frozenset(
    {
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _hdrs(api_key: str) -> dict[str, str]:
    """Build standard Concord request headers."""
    return {
        "X-API-KEY": api_key,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _fmt_agreement(a: dict) -> str:
    """Format a Concord agreement dict into a readable string."""
    lines: list[str] = []
    title = a.get("title") or a.get("name") or "Untitled"
    uid = a.get("uid") or a.get("id", "?")
    status = a.get("status") or a.get("statusLabel", "Unknown")
    lines.append(f"**{title}** (UID: {uid}) — Status: {status}")
    if a.get("createdAt"):
        lines.append(f"  Created: {a['createdAt']}")
    if a.get("modifiedAt"):
        lines.append(f"  Modified: {a['modifiedAt']}")
    if a.get("parties"):
        parties = ", ".join(
            p.get("name", "?") for p in a["parties"] if isinstance(p, dict)
        )
        if parties:
            lines.append(f"  Parties: {parties}")
    if a.get("tags"):
        tags = ", ".join(
            t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in a["tags"]
        )
        if tags:
            lines.append(f"  Tags: {tags}")
    return "\n".join(lines)


def _document_url(organization_id: int, agreement_uid: str, fmt: str) -> str:
    """The agreement's current document as ``pdf`` or ``docx``."""
    return f"{_BASE_URL}/organizations/{organization_id}/agreements/{quote(str(agreement_uid), safe='')}.{fmt}"


def _attachment_url(organization_id: int, agreement_uid: str, attachment: dict) -> str:
    return (
        f"{_BASE_URL}/organizations/{organization_id}/agreements/"
        f"{quote(str(agreement_uid), safe='')}/attachments/{quote(str(attachment.get('id', '')), safe='')}"
    )


def _fmt_version(version: dict) -> str:
    label = version.get("displayVersion") or version.get("version") or "?"
    parts = [f"v{label} (document id: {version.get('id', '?')})"]
    if version.get("type"):
        parts.append(str(version["type"]))
    if isinstance(version.get("date"), int):
        parts.append(datetime.fromtimestamp(version["date"] / 1000, tz=UTC).strftime("%Y-%m-%d %H:%M UTC"))
    user = version.get("user")
    if isinstance(user, dict) and (user.get("name") or user.get("email")):
        parts.append(f"by {user.get('name') or user.get('email')}")
    if version.get("comment"):
        parts.append(str(version["comment"])[:120])
    return " | ".join(parts)


def _fmt_attachment(organization_id: int, agreement_uid: str, attachment: dict) -> str:
    name = attachment.get("name") or attachment.get("fileName") or "attachment"
    parts = [f"{name} (id: {attachment.get('id', '?')})"]
    if attachment.get("fileName") and attachment.get("fileName") != name:
        parts.append(str(attachment["fileName"]))
    if attachment.get("description"):
        parts.append(str(attachment["description"])[:120])
    if attachment.get("createdAt"):
        parts.append(str(attachment["createdAt"]))
    line = " | ".join(parts)
    if attachment.get("url"):
        line += f"\n      URL: {attachment['url']}"
    line += f"\n      Download URL: {_attachment_url(organization_id, agreement_uid, attachment)}"
    return line


def _json_field(resp: httpx.Response, key: str) -> Any:
    """*key* of a JSON object answer, or ``None``."""
    try:
        data = resp.json()
    except ValueError:
        return None
    return data.get(key) if isinstance(data, dict) else None


def _upload_mode(filename: str, content_type: str) -> str | None:
    """Concord's ``uploadMode`` for a file: ``PDF``, ``WORD``, or ``None``."""
    lowered = filename.lower()
    if content_type == "application/pdf" or lowered.endswith(".pdf"):
        return "PDF"
    if content_type in _WORD_TYPES or lowered.endswith((".docx", ".doc")):
        return "WORD"
    return None


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str, executor: CallableToolExecutor, **_kwargs: Any
) -> list[ToolDefinition]:
    """Build Concord integration tools.

    ``token`` may be either a plain API key or a compound
    ``organization_id||api_key`` string (set by integration_tools.py
    when the user configured an organisation ID in extra_config).
    """
    _, api_key = parse_compound_token(token, 2)
    api_key = api_key.strip() if api_key else token.strip()
    headers = _hdrs(api_key)
    # A file answer or a multipart upload: no JSON Content-Type / Accept.
    file_headers = {"X-API-KEY": api_key}
    persist_file_fn = _kwargs.get("persist_file_fn")

    # ── Internal request helpers (use safe_request) ───────────────────

    async def _get(
        client: httpx.AsyncClient, path: str, params: dict | None = None
    ) -> tuple[dict | list | None, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{_BASE_URL}/{path.lstrip('/')}",
            _PROVIDER,
            path,
            headers=headers,
            params=params,
        )
        if err:
            return None, err
        return resp.json(), None

    async def _post(
        client: httpx.AsyncClient, path: str, body: dict | None = None
    ) -> tuple[dict | list | str | None, str | None]:
        resp, err = await safe_request(
            client,
            "post",
            f"{_BASE_URL}/{path.lstrip('/')}",
            _PROVIDER,
            path,
            headers=headers,
            json=body or {},
        )
        if err:
            return None, err
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return resp.json(), None
        return resp.text or "OK", None

    async def _patch(
        client: httpx.AsyncClient, path: str, body: dict | None = None
    ) -> tuple[dict | str | None, str | None]:
        resp, err = await safe_request(
            client,
            "patch",
            f"{_BASE_URL}/{path.lstrip('/')}",
            _PROVIDER,
            path,
            headers=headers,
            json=body or {},
        )
        if err:
            return None, err
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return resp.json(), None
        return resp.text or "OK", None

    async def _put(
        client: httpx.AsyncClient, path: str, body: dict | list | None = None
    ) -> tuple[dict | str | None, str | None]:
        resp, err = await safe_request(
            client,
            "put",
            f"{_BASE_URL}/{path.lstrip('/')}",
            _PROVIDER,
            path,
            headers=headers,
            json=body or {},
        )
        if err:
            return None, err
        ct = resp.headers.get("content-type", "")
        if "application/json" in ct:
            return resp.json(), None
        return resp.text or "OK", None

    async def _delete(
        client: httpx.AsyncClient, path: str
    ) -> tuple[str | None, str | None]:
        _, err = await safe_request(
            client,
            "delete",
            f"{_BASE_URL}/{path.lstrip('/')}",
            _PROVIDER,
            path,
            headers=headers,
        )
        if err:
            return None, err
        return "Deleted successfully.", None

    # ── 1. List agreements ────────────────────────────────────────────────

    async def list_concord_agreements(
        organization_id: int,
        statuses: str = "",
        search: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> str:
        """List agreements in a Concord organisation with optional filters."""
        params: dict[str, Any] = {
            "page": page,
            "numberOfItemsByPage": min(page_size, 50),
        }
        if statuses:
            params["statuses"] = statuses
        if search:
            params["search"] = search

        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"user/me/organizations/{organization_id}/agreements",
                params,
            )
            if err:
                return err
        items = (
            data
            if isinstance(data, list)
            else data.get("agreements", data.get("items", []))
        )
        return format_entity_list(
            items[:page_size],
            _fmt_agreement,
            noun="agreement",
        )

    # ── 2. Get agreement details ──────────────────────────────────────────

    async def get_concord_agreement(organization_id: int, agreement_uid: str) -> str:
        """Get detailed information about a specific agreement including lifecycle, parties, and metadata."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}",
            )
            if err:
                return err
            base = _fmt_agreement(data) if isinstance(data, dict) else str(data)

            # Fetch summary (lifecycle, parties, clauses)
            summary_section = ""
            summary, s_err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/summary",
            )
            if not s_err and isinstance(summary, dict):
                s_lines: list[str] = []
                if summary.get("lifecycle"):
                    lc = summary["lifecycle"]
                    if lc.get("effectiveDate"):
                        s_lines.append(f"  Effective date: {lc['effectiveDate']}")
                    if lc.get("endDate"):
                        s_lines.append(f"  End date: {lc['endDate']}")
                    if lc.get("renewalType"):
                        s_lines.append(f"  Renewal type: {lc['renewalType']}")
                    if lc.get("noticePeriod"):
                        s_lines.append(f"  Notice period: {lc['noticePeriod']}")
                if summary.get("parties"):
                    for p in summary["parties"]:
                        if isinstance(p, dict):
                            s_lines.append(
                                f"  Party: {p.get('name', '?')} ({p.get('type', '?')})"
                            )
                if summary.get("clauses"):
                    s_lines.append(f"  Clauses: {len(summary['clauses'])}")
                if s_lines:
                    summary_section = "\n**Lifecycle & Parties**\n" + "\n".join(s_lines)

            # Fetch metadata (description, tags)
            metadata_section = ""
            meta, m_err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/metadata",
            )
            if not m_err and isinstance(meta, dict):
                m_lines: list[str] = []
                if meta.get("description"):
                    m_lines.append(f"  Description: {meta['description']}")
                if meta.get("tags"):
                    tags = ", ".join(
                        t.get("name", str(t)) if isinstance(t, dict) else str(t)
                        for t in meta["tags"]
                    )
                    if tags:
                        m_lines.append(f"  Tags: {tags}")
                if m_lines:
                    metadata_section = "\n**Metadata**\n" + "\n".join(m_lines)

            document_section = (
                "\n**Document**\n"
                f"  PDF: {_document_url(organization_id, agreement_uid, 'pdf')}\n"
                f"  Word: {_document_url(organization_id, agreement_uid, 'docx')}\n"
                "  (list_concord_agreement_files lists the versions and attachments)"
            )
            return base + summary_section + metadata_section + document_section

    # ── 5. Update agreement metadata ──────────────────────────────────────

    async def update_concord_agreement_metadata(
        organization_id: int,
        agreement_uid: str,
        title: str = "",
        description: str = "",
        bookmarked: bool | None = None,
    ) -> str:
        """Update metadata for an agreement (title, description, bookmark)."""
        body: dict[str, Any] = {}
        if title:
            body["title"] = title
        if description:
            body["description"] = description
        if bookmarked is not None:
            body["bookmarked"] = bookmarked
        if not body:
            return "No fields to update."

        async with egress_client(timeout=30) as client:
            _, err = await _patch(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/metadata",
                body,
            )
            if err:
                return err
        return f"Agreement {agreement_uid} metadata updated successfully."

    # ── 6. Count agreements ───────────────────────────────────────────────

    async def count_concord_agreements(organization_id: int) -> str:
        """Get agreement counts by status for an organisation."""
        async with egress_client(timeout=30) as client:
            data, err = await _post(
                client,
                f"user/me/organizations/{organization_id}/agreements/count",
            )
            if err:
                return err
        if not isinstance(data, dict):
            return str(data)
        lines = ["**Agreement Counts**"]
        for key, val in data.items():
            if isinstance(val, (int, float)):
                label = key.replace("Count", "").replace("_", " ").title()
                lines.append(f"  {label}: {val}")
        return "\n".join(lines)

    # ── 7. Get signature info ─────────────────────────────────────────────

    async def get_concord_signature(organization_id: int, agreement_uid: str) -> str:
        """Get signature configuration and status for an agreement."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/signature",
            )
            if err:
                return err
        if not isinstance(data, dict):
            return str(data)
        lines = [f"**Signature Info** (Agreement: {agreement_uid})"]
        if data.get("provider"):
            lines.append(f"  Provider: {data['provider']}")
        if data.get("status"):
            lines.append(f"  Status: {data['status']}")
        slots = data.get("slots", [])
        if slots:
            lines.append(f"  Signers ({len(slots)}):")
            for s in slots:
                if isinstance(s, dict):
                    name = s.get("name") or s.get("email", "?")
                    signed = "Signed" if s.get("signed") else "Pending"
                    lines.append(f"    - {name}: {signed}")
        return "\n".join(lines)

    # ── 8. Configure signature slots ──────────────────────────────────────

    async def configure_concord_signature_slots(
        organization_id: int,
        agreement_uid: str,
        slots: str,
    ) -> str:
        """Configure who needs to sign an agreement.

        slots: JSON array string, e.g. [{"name":"John","email":"john@example.com","role":"signer","order":1}]
        """
        try:
            parsed = json.loads(slots)
        except json.JSONDecodeError:
            return "Error: slots must be a valid JSON array of signer objects."

        async with egress_client(timeout=30) as client:
            _, err = await _put(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/signature/slots",
                parsed,
            )
            if err:
                return err
        return f"Signature slots configured for agreement {agreement_uid}."

    # ── 9. Send for signature ─────────────────────────────────────────────

    async def send_for_concord_signature(
        organization_id: int, agreement_uid: str
    ) -> str:
        """Send an agreement out for e-signature (triggers the signature assignment)."""
        async with egress_client(timeout=30) as client:
            _, err = await _post(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/signature/request",
            )
            if err:
                return err
        return f"Agreement {agreement_uid} has been sent for signature."

    # ── 10. Invite member ─────────────────────────────────────────────────

    async def invite_concord_member(
        organization_id: int,
        agreement_uid: str,
        email: str,
        permission: str = "read",
    ) -> str:
        """Invite a user to collaborate on an agreement."""
        body = {"email": email, "permission": permission}
        async with egress_client(timeout=30) as client:
            _, err = await _post(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/members",
                body,
            )
            if err:
                return err
        return f"Invited {email} to agreement {agreement_uid} with {permission} permission."

    # ── 11. List members ──────────────────────────────────────────────────

    async def list_concord_members(organization_id: int, agreement_uid: str) -> str:
        """List all users with access to an agreement."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/members",
            )
            if err:
                return err
        members = data if isinstance(data, list) else data.get("members", [])
        if not members:
            return "No members found."
        lines = [f"**Members** (Agreement: {agreement_uid})"]
        for m in members:
            if isinstance(m, dict):
                name = m.get("fullName") or m.get("name") or m.get("email", "?")
                perm = m.get("permission", "?")
                lines.append(f"  - {name} ({perm})")
        return "\n".join(lines)

    # ── 12. List comments ─────────────────────────────────────────────────

    async def list_concord_comments(organization_id: int, agreement_uid: str) -> str:
        """Get all comments on an agreement."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/comments",
            )
            if err:
                return err
        comments = data if isinstance(data, list) else data.get("comments", [])
        if not comments:
            return "No comments found."
        lines = [f"**Comments** (Agreement: {agreement_uid})"]
        for c in comments[:30]:
            if isinstance(c, dict):
                author = (
                    c.get("author", {}).get("fullName", "?")
                    if isinstance(c.get("author"), dict)
                    else "?"
                )
                text = c.get("content") or c.get("text", "")
                lines.append(f"  - {author}: {text[:200]}")
        return "\n".join(lines)

    # ── 13. Get activity log ──────────────────────────────────────────────

    async def get_concord_agreement_activities(
        organization_id: int, agreement_uid: str
    ) -> str:
        """Get the activity log for an agreement."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/activities",
            )
            if err:
                return err
        activities = data if isinstance(data, list) else data.get("activities", [])
        if not activities:
            return "No activities found."
        lines = [f"**Activity Log** (Agreement: {agreement_uid})"]
        for act in activities[:30]:
            if isinstance(act, dict):
                action = act.get("action") or act.get("type", "?")
                user = (
                    act.get("user", {}).get("fullName", "?")
                    if isinstance(act.get("user"), dict)
                    else "?"
                )
                date = act.get("createdAt") or act.get("date", "")
                lines.append(f"  - [{date}] {user}: {action}")
        return "\n".join(lines)

    # ── 14. List automated templates ──────────────────────────────────────

    async def list_concord_templates(organization_id: int) -> str:
        """List automated templates available in the organisation."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(
                client, f"organizations/{organization_id}/templates/automated"
            )
            if err:
                return err
        templates = data if isinstance(data, list) else data.get("templates", [])
        if not templates:
            return "No automated templates found."
        lines = ["**Automated Templates**"]
        for t in templates:
            if isinstance(t, dict):
                name = t.get("title") or t.get("name", "Untitled")
                uid = t.get("uid") or t.get("id", "?")
                lines.append(f"  - {name} (UID: {uid})")
        return "\n".join(lines)

    # ── 15. Create agreement from template ────────────────────────────────

    async def create_concord_from_template(
        organization_id: int, template_uid: str
    ) -> str:
        """Create a new agreement from an automated template."""
        async with egress_client(timeout=30) as client:
            data, err = await _post(
                client,
                f"organizations/{organization_id}/templates/automated/{template_uid}/use",
            )
            if err:
                return err
        if isinstance(data, dict):
            uid = data.get("uid") or data.get("id", "?")
            title = data.get("title") or data.get("name", "New agreement")
            return f"Created agreement '{title}' (UID: {uid}) from template {template_uid}."
        return f"Agreement created from template {template_uid}."

    # ── 16. Delete agreement ──────────────────────────────────────────────

    async def delete_concord_agreement(organization_id: int, agreement_uid: str) -> str:
        """Delete an agreement."""
        async with egress_client(timeout=30) as client:
            result, err = await _delete(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}",
            )
            if err:
                return err
        return result

    # ── 17. List organisations ────────────────────────────────────────────

    async def list_concord_organizations() -> str:
        """List all organisations the authenticated user belongs to."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(client, "user/me/organizations")
            if err:
                return err
        orgs = data if isinstance(data, list) else data.get("organizations", [])
        if not orgs:
            return "No organisations found."
        lines = ["**Your Organisations**"]
        for o in orgs:
            if isinstance(o, dict):
                name = o.get("name", "Unnamed")
                oid = o.get("id", "?")
                lines.append(f"  - {name} (ID: {oid})")
        return "\n".join(lines)

    # ── 18. List folders ──────────────────────────────────────────────────

    async def list_concord_folders(organization_id: int) -> str:
        """List all folders in an organisation."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(client, f"organizations/{organization_id}/folders")
            if err:
                return err
        folders = data if isinstance(data, list) else data.get("folders", [])
        if not folders:
            return "No folders found."
        lines = ["**Folders**"]
        for f in folders:
            if isinstance(f, dict):
                name = f.get("name", "Unnamed")
                fid = f.get("id", "?")
                lines.append(f"  - {name} (ID: {fid})")
        return "\n".join(lines)

    # ── 19. List tags ─────────────────────────────────────────────────────

    async def list_concord_tags(organization_id: int) -> str:
        """List all tags in an organisation."""
        async with egress_client(timeout=30) as client:
            data, err = await _get(client, f"organizations/{organization_id}/tags")
            if err:
                return err
        tags = data if isinstance(data, list) else data.get("tags", [])
        if not tags:
            return "No tags found."
        names = [t.get("name", str(t)) if isinstance(t, dict) else str(t) for t in tags]
        return f"**Tags** ({len(names)}): {', '.join(names)}"

    # ── 20. Update agreement lifecycle ────────────────────────────────────

    async def update_concord_lifecycle(
        organization_id: int,
        agreement_uid: str,
        effective_date: str = "",
        end_date: str = "",
        renewal_type: str = "",
        notice_period: str = "",
    ) -> str:
        """Update lifecycle fields on an agreement summary (dates, renewal)."""
        body: dict[str, Any] = {}
        if effective_date:
            body["effectiveDate"] = effective_date
        if end_date:
            body["endDate"] = end_date
        if renewal_type:
            body["renewalType"] = renewal_type
        if notice_period:
            body["noticePeriod"] = notice_period
        if not body:
            return "No lifecycle fields provided to update."

        async with egress_client(timeout=30) as client:
            _, err = await _patch(
                client,
                f"organizations/{organization_id}/agreements/{agreement_uid}/summary/lifecycle",
                body,
            )
            if err:
                return err
        return f"Lifecycle updated for agreement {agreement_uid}."

    # ── 21. Agreement documents: versions and attachments ─────────

    async def _attachments(
        client: httpx.AsyncClient, organization_id: int, agreement_uid: str
    ) -> tuple[list[dict], str | None]:
        data, err = await _get(
            client, f"organizations/{organization_id}/agreements/{agreement_uid}/attachments"
        )
        if err:
            return [], err
        items = data.get("attachments", []) if isinstance(data, dict) else data or []
        return [a for a in items if isinstance(a, dict)], None

    async def list_concord_agreement_files(organization_id: int, agreement_uid: str) -> str:
        """List an agreement's document versions and attachments with their links."""
        async with egress_client(timeout=30) as client:
            versions, v_err = await _get(
                client, f"organizations/{organization_id}/agreements/{agreement_uid}/versions"
            )
            attachments, a_err = await _attachments(client, organization_id, agreement_uid)
        if v_err and a_err:
            return v_err
        lines = [
            f"Agreement {agreement_uid} document:",
            f"  PDF: {_document_url(organization_id, agreement_uid, 'pdf')}",
            f"  Word: {_document_url(organization_id, agreement_uid, 'docx')}",
        ]
        if v_err:
            lines.append(f"Versions not read: {v_err.splitlines()[0]}")
        elif isinstance(versions, list) and versions:
            lines.append(f"Versions ({len(versions)}):")
            lines.extend(f"  {_fmt_version(v)}" for v in versions if isinstance(v, dict))
        if a_err:
            lines.append(f"Attachments not read: {a_err.splitlines()[0]}")
        elif attachments:
            lines.append(f"Attachments ({len(attachments)}):")
            lines.extend(
                f"  {_fmt_attachment(organization_id, agreement_uid, a)}" for a in attachments
            )
        else:
            lines.append("No attachments.")
        lines.append(
            "\nTip: download_concord_agreement stores the current document (PDF or Word); "
            "download_concord_attachment stores an attachment."
        )
        return "\n".join(lines)

    async def download_concord_agreement(
        organization_id: int, agreement_uid: str, file_format: str = "pdf"
    ) -> str | ToolResult:
        """Download an agreement's current document as PDF or Word."""
        fmt = (file_format or "pdf").strip().lower().lstrip(".")
        if fmt in ("word", "doc"):
            fmt = "docx"
        if fmt not in ("pdf", "docx"):
            return f"Error: invalid file_format '{file_format}'. Must be pdf or docx."
        url = _document_url(organization_id, agreement_uid, fmt)
        async with egress_client(timeout=60) as client:
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download agreement {agreement_uid} as {fmt}",
                max_bytes=_MAX_FILE_BYTES,
                headers=file_headers,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        filename = fetched.filename or f"concord-{agreement_uid}.{fmt}"
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source="Concord",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
            details=[f"Current document of agreement {agreement_uid}."],
        )

    async def download_concord_attachment(
        organization_id: int, agreement_uid: str, attachment_id: str
    ) -> str | ToolResult:
        """Download an agreement attachment."""
        async with egress_client(timeout=60) as client:
            attachments, err = await _attachments(client, organization_id, agreement_uid)
            if err:
                return err
            attachment = next(
                (a for a in attachments if str(a.get("id")) == str(attachment_id).strip()), None
            )
            if attachment is None:
                listed = ", ".join(f"{a.get('name') or a.get('fileName')} (id: {a.get('id')})" for a in attachments)
                return (
                    f"Error: no attachment '{attachment_id}' on agreement {agreement_uid}."
                    + (f" Attachments: {listed}." if listed else "")
                )
            url = _attachment_url(organization_id, agreement_uid, attachment)
            fetched = await fetch_file(
                client,
                "get",
                url,
                provider=_PROVIDER,
                action=f"download attachment {attachment_id}",
                max_bytes=_MAX_FILE_BYTES,
                headers=file_headers,
                follow_redirects=True,
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {url}"
        filename = (
            str(attachment.get("fileName") or "").strip()
            or fetched.filename
            or f"concord-attachment-{attachment_id}"
        )
        return await deliver_file(
            data=fetched.data,
            filename=filename,
            content_type=file_content_type(fetched.content_type, filename),
            source="Concord",
            persist_file_fn=persist_file_fn,
            links=[("Download URL", url)],
            details=[f"Attachment '{attachment.get('name') or filename}' of agreement {agreement_uid}."],
        )

    async def upload_concord_agreement_version(
        organization_id: int,
        agreement_uid: str,
        file_id: str,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Upload a PDF or Word file of the run as the agreement's new version."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target="Concord"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        file_id = file_id.strip()
        mode = _upload_mode(filename, content_type)
        if mode is None:
            return ToolResult(
                tool_call_id="",
                content=uploaded_file_answer(
                    f"Error: file {file_id} is not a PDF or Word document - "
                    "Concord creates a version only from a PDF or a Word file.",
                    target=_PROVIDER,
                    filename=filename,
                    content_type=content_type,
                ),
                is_error=True,
            )
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/organizations/{organization_id}/agreements/{agreement_uid}/versions/upload",
                _PROVIDER,
                f"upload a new version of agreement {agreement_uid}",
                headers=file_headers,
                params={"uploadMode": mode},
                files={"file": (filename, data, content_type)},
            )
        if err:
            return err
        version = _json_field(resp, "version")
        return uploaded_file_answer(
            f"Uploaded file {file_id} as a new version of agreement {agreement_uid}"
            + (f" (version {version})." if version is not None else "."),
            target=_PROVIDER,
            filename=filename,
        )

    async def upload_concord_attachment(
        organization_id: int,
        agreement_uid: str,
        file_id: str,
        name: str = "",
        description: str = "",
        is_signed_contract: bool = False,
        file_context: FileContext | None = None,
    ) -> str | ToolResult:
        """Attach a file of the run to an agreement."""
        piped = await read_pipe_file(
            file_context, file_id, max_bytes=_MAX_FILE_BYTES, target="Concord"
        )
        if isinstance(piped, str):
            return ToolResult(tool_call_id="", content=piped, is_error=True)
        data, filename, content_type = piped
        file_id = file_id.strip()
        form: dict[str, str] = {"isSignedContract": "true" if is_signed_contract else "false"}
        if (name or "").strip():
            form["name"] = name.strip()[:255]
        if (description or "").strip():
            form["description"] = description.strip()[:1023]
        async with egress_client(timeout=120) as client:
            resp, err = await safe_request(
                client,
                "post",
                f"{_BASE_URL}/organizations/{organization_id}/agreements/{agreement_uid}/attachments",
                _PROVIDER,
                f"attach file {file_id} to agreement {agreement_uid}",
                headers=file_headers,
                data=form,
                files={"file": (filename, data, content_type)},
            )
        if err:
            return err
        attachment_id = _json_field(resp, "id")
        return uploaded_file_answer(
            f"Attached file {file_id} to agreement {agreement_uid}"
            + (f" (attachment id: {attachment_id})." if attachment_id else "."),
            target=_PROVIDER,
            filename=filename,
        )

    # ── Register tools ────────────────────────────────────────────────────

    executor.register("list_concord_organizations", list_concord_organizations)
    executor.register("list_concord_agreements", list_concord_agreements)
    executor.register("get_concord_agreement", get_concord_agreement)
    executor.register(
        "update_concord_agreement_metadata", update_concord_agreement_metadata
    )
    executor.register("count_concord_agreements", count_concord_agreements)
    executor.register("get_concord_signature", get_concord_signature)
    executor.register(
        "configure_concord_signature_slots", configure_concord_signature_slots
    )
    executor.register("send_for_concord_signature", send_for_concord_signature)
    executor.register("invite_concord_member", invite_concord_member)
    executor.register("list_concord_members", list_concord_members)
    executor.register("list_concord_comments", list_concord_comments)
    executor.register(
        "get_concord_agreement_activities", get_concord_agreement_activities
    )
    executor.register("list_concord_templates", list_concord_templates)
    executor.register("create_concord_from_template", create_concord_from_template)
    executor.register("delete_concord_agreement", delete_concord_agreement)
    executor.register("list_concord_folders", list_concord_folders)
    executor.register("list_concord_tags", list_concord_tags)
    executor.register("update_concord_lifecycle", update_concord_lifecycle)
    executor.register("list_concord_agreement_files", list_concord_agreement_files)
    executor.register("download_concord_agreement", download_concord_agreement)
    executor.register("download_concord_attachment", download_concord_attachment)
    executor.register("upload_concord_agreement_version", upload_concord_agreement_version)
    executor.register("upload_concord_attachment", upload_concord_attachment)

    source = "integration:concord"
    agreement_properties = {
        "organization_id": {"type": "integer", "description": "Concord organisation ID"},
        "agreement_uid": {"type": "string", "description": "Agreement UID"},
    }

    return [
        # ── Agreement documents ───────────────────────────────────────────
        ToolDefinition(
            name="list_concord_agreement_files",
            description=(
                "List an agreement's document (PDF and Word links), its versions and its "
                "attachments with each attachment's id and link."
            ),
            input_schema={
                "type": "object",
                "properties": dict(agreement_properties),
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="download_concord_agreement",
            workspace_ingest=True,
            description=(
                "Download an agreement's current document as PDF (default) or Word into the "
                "conversation: a download card, its link and a file_id to forward to any tool "
                "that takes a file; its readable content comes with the result. Include the "
                "returned [[FILE:...]] marker verbatim so the user gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **agreement_properties,
                    "file_format": {
                        "type": "string",
                        "description": "pdf (default) or docx",
                        "default": "pdf",
                    },
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="download_concord_attachment",
            workspace_ingest=True,
            description=(
                "Download an agreement attachment into the conversation: a download card, its "
                "link and a file_id to forward to any tool that takes a file; its readable "
                "content comes with the result. Use list_concord_agreement_files to find "
                "attachment ids. Include the returned [[FILE:...]] marker verbatim so the user "
                "gets a download card."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **agreement_properties,
                    "attachment_id": {"type": "string", "description": "Attachment ID"},
                },
                "required": ["organization_id", "agreement_uid", "attachment_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="upload_concord_agreement_version",
            description=(
                "Upload a PDF or Word file of this conversation as the agreement's new version "
                "byte-for-byte: any file attached by the user, generated, or downloaded by "
                "another tool, by its file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **agreement_properties,
                    "file_id": {"type": "string", "description": "file_id of a PDF or Word file of this conversation"},
                },
                "required": ["organization_id", "agreement_uid", "file_id"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="upload_concord_attachment",
            description=(
                "Attach a file of this conversation to an agreement byte-for-byte (a signed "
                "copy, an annex...): any file attached by the user, generated, or downloaded "
                "by another tool, by its file_id."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **agreement_properties,
                    "file_id": {"type": "string", "description": "file_id of a file of this conversation"},
                    "name": {"type": "string", "description": "Attachment name (default: the file name)", "default": ""},
                    "description": {"type": "string", "description": "Attachment description", "default": ""},
                    "is_signed_contract": {
                        "type": "boolean",
                        "description": "The attachment is the signed contract itself",
                        "default": False,
                    },
                },
                "required": ["organization_id", "agreement_uid", "file_id"],
            },
            source=source,
            priority=2,
        ),
        # ── Read tools ────────────────────────────────────────────────────
        ToolDefinition(
            name="list_concord_organizations",
            description="List all Concord organisations the authenticated user belongs to. Returns organisation names and IDs.",
            input_schema={"type": "object", "properties": {}, "required": []},
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_concord_agreements",
            description=(
                "List agreements in a Concord organisation. Supports filtering by status "
                "(DRAFT, NEGOTIATION, SIGNING, CURRENT_CONTRACT, COMPLETED_CONTRACT, TEMPLATE, etc.) "
                "and full-text search. Returns titles, UIDs, statuses, dates, and parties."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "statuses": {
                        "type": "string",
                        "description": "Comma-separated status filter (e.g. 'DRAFT,SIGNING,CURRENT_CONTRACT')",
                    },
                    "search": {
                        "type": "string",
                        "description": "Full-text search query",
                    },
                    "page": {
                        "type": "integer",
                        "description": "Page number (default 1)",
                        "default": 1,
                    },
                    "page_size": {
                        "type": "integer",
                        "description": "Results per page (max 50)",
                        "default": 20,
                    },
                },
                "required": ["organization_id"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="get_concord_agreement",
            description="Get detailed information about a specific Concord agreement by its UID, including lifecycle dates, parties, renewal info, and metadata (description, tags).",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="count_concord_agreements",
            description="Get agreement counts by status (draft, signing, contract, etc.) for a Concord organisation dashboard.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                },
                "required": ["organization_id"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_concord_signature",
            description="Get signature configuration and status for a Concord agreement — shows provider, signers, and who has signed.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_concord_members",
            description="List all users with access to a Concord agreement (collaborators, reviewers, etc.).",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_concord_comments",
            description="Get all comments on a Concord agreement.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="get_concord_agreement_activities",
            description="Get the activity log / audit trail for a Concord agreement.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="list_concord_templates",
            description="List automated templates available in a Concord organisation for generating new agreements.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                },
                "required": ["organization_id"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_concord_folders",
            description="List all folders in a Concord organisation.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                },
                "required": ["organization_id"],
            },
            source=source,
            priority=2,
        ),
        ToolDefinition(
            name="list_concord_tags",
            description="List all tags available in a Concord organisation.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                },
                "required": ["organization_id"],
            },
            source=source,
            priority=2,
        ),
        # ── Write tools ───────────────────────────────────────────────────
        ToolDefinition(
            name="update_concord_agreement_metadata",
            description="Update metadata on a Concord agreement — title, description, or bookmark status.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                    "title": {"type": "string", "description": "New title"},
                    "description": {"type": "string", "description": "New description"},
                    "bookmarked": {
                        "type": "boolean",
                        "description": "Set bookmark flag",
                    },
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="configure_concord_signature_slots",
            description=(
                "Configure who needs to sign a Concord agreement. Provide a JSON array of signer objects, "
                "each with name, email, role (e.g. 'signer'), and order."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                    "slots": {
                        "type": "string",
                        "description": 'JSON array of signers, e.g. [{"name":"Jane","email":"jane@co.com","role":"signer","order":1}]',
                    },
                },
                "required": ["organization_id", "agreement_uid", "slots"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="send_for_concord_signature",
            description="Send a Concord agreement out for e-signature. Triggers the signature assignment for all configured signers.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="invite_concord_member",
            description="Invite a user to collaborate on a Concord agreement.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                    "email": {
                        "type": "string",
                        "description": "Email of the person to invite",
                    },
                    "permission": {
                        "type": "string",
                        "description": "Permission level (read, write, admin)",
                        "default": "read",
                    },
                },
                "required": ["organization_id", "agreement_uid", "email"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="create_concord_from_template",
            description="Create a new Concord agreement from an automated template.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "template_uid": {
                        "type": "string",
                        "description": "UID of the automated template to use",
                    },
                },
                "required": ["organization_id", "template_uid"],
            },
            source=source,
            priority=0,
        ),
        ToolDefinition(
            name="delete_concord_agreement",
            description="Delete a Concord agreement. Use with caution — this action cannot be undone.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {
                        "type": "string",
                        "description": "Agreement UID to delete",
                    },
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
        ToolDefinition(
            name="update_concord_lifecycle",
            description="Update lifecycle fields on a Concord agreement summary — effective date, end date, renewal type, notice period.",
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "integer",
                        "description": "Concord organisation ID",
                    },
                    "agreement_uid": {"type": "string", "description": "Agreement UID"},
                    "effective_date": {
                        "type": "string",
                        "description": "Effective date (ISO 8601, e.g. 2025-01-15)",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "End date (ISO 8601)",
                    },
                    "renewal_type": {
                        "type": "string",
                        "description": "Renewal type (e.g. MANUAL, TACIT, NONE)",
                    },
                    "notice_period": {
                        "type": "string",
                        "description": "Notice period (e.g. '30 days')",
                    },
                },
                "required": ["organization_id", "agreement_uid"],
            },
            source=source,
            priority=1,
        ),
    ]
