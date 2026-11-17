# SPDX-License-Identifier: Apache-2.0

"""1Password integration - Agentic secrets management (Connect + Events + SCIM).

Full-fledged 1Password integration, designed as a best-in-class building
block for agentic credential workflows. It covers the THREE HTTP API
surfaces 1Password exposes:

1. **1Password Connect API** (self-hosted Connect server, primary surface):
   - Vaults: list with name filtering
   - Items: search across one or all vaults (title keyword, category, tag,
     favorite filters), full detail with concealed values and OTP secrets
     ALWAYS masked (the read-classified detail tool never exposes secret
     material)
   - Secrets retrieval: ``reveal_onepassword_field`` for one explicit field
     (least-privilege by design, write-classified, refuses OTP fields so
     the permanent seed never leaves 1Password) and ``get_onepassword_otp``
     for the current TOTP code (*the 2FA tool*) - prefers the
     server-computed ``totp`` attribute and falls back to a local RFC 6238
     computation when only the ``otpauth://`` URI / raw base32 secret is
     available
   - Files: list attachments with their Connect download URL, read text file
    content (size-guarded), store any file as a file of the run; the
    Connect API has no file upload
   - Item lifecycle: create (with password generation recipe + TOTP seeding),
     update (title/tags/favorite/urls + field add/set/remove via RFC 6902
     PATCH with field-ID addressing), delete (permanent)
   - Connect API activity log (which token did what, from where)

2. **1Password Events Reporting API** (1Password Business, hosted at
   ``events.1password.com`` / regional variants): sign-in attempts, item
   usage events, and audit events - cursor-paginated security telemetry.

3. **1Password SCIM bridge** (self-hosted, 1Password Business): user and
   group directory - list/search users and groups, plus a consolidated
   provisioning tool (create/suspend/reactivate users, group lifecycle and
   membership). 1Password deprovisions by SUSPENDING users; the SCIM API
   deliberately has no user hard-delete.

Killer agent use cases: an agent that fetches an API credential + its
current one-time password to authenticate a workflow; a security agent that
audits failed sign-ins and item access; an IT agent that provisions or
suspends users and manages group membership.

Authentication
--------------
All three surfaces use independent **bearer tokens**.
``integration_tools._get_token`` assembles the 6-part compound token this
builder parses::

    connect_base_url||connect_token||events_base_url||events_token||scim_base_url||scim_token

The Connect server pair is the primary credential (catalog-required); the
Events and SCIM credentials are optional. Every tool is always registered -
tools whose subsystem is not configured return an actionable configuration
error at call time (CrowdSec precedent), so a partially-configured
integration still exposes its full tool surface for scoping.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.core import totp
from app.core.egress import egress_client
from app.integrations.base import BaseIntegration, OAuthDefaults
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    fetch_file,
    file_content_type,
)
from app.integrations.http import (
    format_entity_list,
    format_pagination_hint,
    parse_compound_token,
    safe_request,
)
from app.integrations.onepassword_connect import (
    ConnectAPI,
    ConnectFailure,
    build_onepassword_headers,
    field_secret_kind,
    onepassword_api_base,
)
from app.integrations.onepassword_connect import find_field as _find_field
from app.integrations.onepassword_connect import json_dict as _json_dict
from app.integrations.onepassword_connect import json_list as _json_list
from app.integrations.onepassword_connect import pid as _pid
from app.integrations.onepassword_connect import sid as _sid
from app.tools.base import CallableToolExecutor, ToolDefinition, ToolResult

_PROVIDER = "1Password"
_SOURCE = "integration:onepassword"

# Default (US) Events Reporting host. Regional accounts use
# events.1password.ca / events.1password.eu / events.ent.1password.com.
EVENTS_DEFAULT_BASE = "https://events.1password.com"

# Upper bound on the number of entities displayed in a single list call.
_MAX_LIMIT = 100
# The Events API accepts up to 1000 events per page.
_MAX_EVENT_LIMIT = 1000
# Cross-vault item search fans out one request per vault - keep it bounded.
_MAX_SEARCH_VAULTS = 25
# SCIM resolution pages through at most this many pages of 100 entries.
_MAX_SCIM_PAGES = 10
# File reads are size-guarded: refuse anything above this many bytes and
# truncate the decoded text at the max_chars argument.
_MAX_FILE_BYTES = 1_000_000
_MAX_FILE_CHARS = 50_000
# A download stores the whole file as a file of the run.
_MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024

# Connect item categories (OpenAPI 1.8.1 enum).
_ITEM_CATEGORIES = (
    "LOGIN",
    "PASSWORD",
    "API_CREDENTIAL",
    "SERVER",
    "DATABASE",
    "CREDIT_CARD",
    "MEMBERSHIP",
    "PASSPORT",
    "SOFTWARE_LICENSE",
    "OUTDOOR_LICENSE",
    "SECURE_NOTE",
    "WIRELESS_ROUTER",
    "BANK_ACCOUNT",
    "DRIVER_LICENSE",
    "IDENTITY",
    "REWARD_PROGRAM",
    "DOCUMENT",
    "EMAIL_ACCOUNT",
    "SOCIAL_SECURITY_NUMBER",
    "MEDICAL_RECORD",
    "SSH_KEY",
    "CUSTOM",
)

# Sign-in attempt categories (Events API 1.4.1 enum) - used for the
# client-side category filter on list_onepassword_signin_attempts.
_SIGNIN_CATEGORIES = (
    "success",
    "credentials_failed",
    "mfa_failed",
    "sso_failed",
    "modern_version_failed",
    "firewall_failed",
    "firewall_reported_success",
)

_PROVISIONING_ACTIONS = frozenset(
    {
        "get_user",
        "create_user",
        "update_user_name",
        "deactivate_user",
        "activate_user",
        "get_group",
        "create_group",
        "rename_group",
        "add_group_member",
        "remove_group_member",
        "delete_group",
    }
)

_SCIM_PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
_SCIM_USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
_SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"


def scim_api_base(base_url: str) -> str:
    """Normalise the SCIM bridge URL into the full SCIM 2.0 base path.

    The 1Password SCIM bridge serves the SCIM protocol under ``/scim/v2``
    (what IdPs are configured with; 1Password's own endpoint doc also lists
    a ``/scim`` prefix on recent bridge versions). Accepts either the server
    root or a URL that already carries the prefix:

    - ``https://scim.example.com``          -> ``https://scim.example.com/scim/v2``
    - ``https://scim.example.com/scim/v2/`` -> ``https://scim.example.com/scim/v2``
    - ``https://scim.example.com/scim``     -> kept as-is (explicit prefix wins)

    Returns ``""`` when unset.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/scim/v2") or base.endswith("/scim"):
        return base
    return f"{base}/scim/v2"


def parse_onepassword_token(
    token: str,
) -> tuple[str, str, str, str, str, str]:
    """Split the 6-part compound token into normalised credentials.

    Returns ``(connect_base, connect_key, events_base, events_key,
    scim_base, scim_key)``. The events base falls back to the US cloud host
    when an events token is configured without an explicit URL; the SCIM
    base is normalised to the full ``/scim/v2`` path.
    """
    parts = parse_compound_token(token, 6)
    connect_base = onepassword_api_base(parts[0])
    connect_key = (parts[1] or "").strip()
    events_key = (parts[3] or "").strip()
    events_base = onepassword_api_base(parts[2])
    if events_key and not events_base:
        events_base = EVENTS_DEFAULT_BASE
    scim_base = scim_api_base(parts[4])
    scim_key = (parts[5] or "").strip()
    return connect_base, connect_key, events_base, events_key, scim_base, scim_key


class OnePasswordIntegration(BaseIntegration):
    provider_name = "onepassword"
    display_name = "1Password"
    icon = "KeyRound"
    description = (
        "Connect 1Password for agentic secrets management - retrieve "
        "credentials and one-time passwords (TOTP) from vaults via a "
        "Connect server, audit sign-ins and item usage via the Events API, "
        "and manage users and groups via the SCIM bridge."
    )
    auth_types = ["api_key"]

    def get_oauth_defaults(self) -> OAuthDefaults | None:
        return None

    async def validate_connection(self, token: str) -> bool:
        """Validate the primary (Connect server) credential."""
        connect_base, connect_key, *_rest = parse_onepassword_token(token)
        if not connect_base or not connect_key:
            return False
        async with egress_client(timeout=15) as client:
            resp, _err = await safe_request(
                client,
                "get",
                f"{connect_base}/v1/vaults",
                _PROVIDER,
                "validate connection",
                headers=build_onepassword_headers(connect_key),
            )
            if resp is None:
                return False
            # A 2xx HTML login/proxy page must not count as connected -
            # GET /v1/vaults always returns a JSON array.
            data, jerr = _json_list(resp)
            return jerr is None and isinstance(data, list)

    async def get_user_info(self, token: str) -> dict:
        connect_base, connect_key, *_rest = parse_onepassword_token(token)
        # Same guards as validate_connection: a misconfigured integration
        # must surface a clear configuration error, not a relative-URL
        # request failure.
        if not connect_base:
            return {
                "error": (
                    "1Password Connect base URL is not configured - set "
                    "base_url in the integration's extra config."
                )
            }
        if not connect_key:
            return {"error": "1Password Connect access token is not configured."}
        async with egress_client(timeout=15) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{connect_base}/v1/vaults",
                _PROVIDER,
                "get server info",
                headers=build_onepassword_headers(connect_key),
            )
            if resp is None:
                return {"error": err}
            vaults, jerr = _json_list(resp)
            if jerr:
                return {"error": jerr}
            info: dict[str, Any] = {
                "name": "1Password Connect",
                "vaults": len(vaults),
            }
            # /health (server root, unauthenticated) adds the Connect
            # version - best-effort, a failure never masks the vault count.
            health_resp, _health_err = await safe_request(
                client,
                "get",
                f"{connect_base}/health",
                _PROVIDER,
                "get server health",
                headers={"Accept": "application/json"},
            )
            if health_resp is not None:
                health, health_jerr = _json_dict(health_resp)
                if not health_jerr and health.get("version"):
                    info["version"] = health.get("version")
            return info

    async def describe_connectivity(self, token: str) -> tuple[bool, str]:
        """Test every CONFIGURED subsystem and aggregate a status message.

        Used by the integration connectivity test so the UI "Test" button
        reports Connect, Events API, and SCIM bridge health in one shot.
        Unconfigured optional subsystems are skipped silently; a configured
        subsystem that fails flips the overall status to error.
        """
        (
            connect_base,
            connect_key,
            events_base,
            events_key,
            scim_base,
            scim_key,
        ) = parse_onepassword_token(token)
        parts: list[str] = []
        ok = True
        async with egress_client(timeout=20) as client:
            if connect_base and connect_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{connect_base}/v1/vaults",
                    _PROVIDER,
                    "test Connect server",
                    headers=build_onepassword_headers(connect_key),
                )
                if resp is None:
                    ok = False
                    parts.append(f"Connect server: FAILED - {err}")
                else:
                    vaults, jerr = _json_list(resp)
                    if jerr:
                        ok = False
                        parts.append(
                            "Connect server: FAILED - the response is not "
                            "the Connect API (check that base_url points at "
                            "the Connect server, not a login page or proxy)"
                        )
                    else:
                        count = len(vaults)
                        parts.append(
                            f"Connect server OK ({count} vault"
                            f"{'s' if count != 1 else ''} in token scope)"
                        )
            else:
                ok = False
                parts.append(
                    "Connect server: not configured (base_url and the "
                    "Connect access token are required)"
                )
            if events_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{events_base}/api/v2/auth/introspect",
                    _PROVIDER,
                    "test Events API",
                    headers=build_onepassword_headers(events_key),
                )
                if resp is None:
                    ok = False
                    parts.append(f"Events API: FAILED - {err}")
                else:
                    data, jerr = _json_dict(resp)
                    if jerr:
                        ok = False
                        parts.append(f"Events API: FAILED - {jerr}")
                    else:
                        features = data.get("features") or []
                        parts.append(
                            "Events API OK (features: "
                            + (", ".join(str(f) for f in features) or "none")
                            + ")"
                        )
            if scim_base and scim_key:
                resp, err = await safe_request(
                    client,
                    "get",
                    f"{scim_base}/Users",
                    _PROVIDER,
                    "test SCIM bridge",
                    headers=build_onepassword_headers(scim_key),
                    params={"startIndex": 1, "count": 1},
                )
                if resp is None:
                    ok = False
                    parts.append(f"SCIM bridge: FAILED - {err}")
                else:
                    data, jerr = _json_dict(resp)
                    if jerr:
                        ok = False
                        parts.append(f"SCIM bridge: FAILED - {jerr}")
                    else:
                        total = data.get("totalResults")
                        parts.append(
                            f"SCIM bridge OK ({total} user"
                            f"{'s' if total != 1 else ''})"
                            if isinstance(total, int)
                            else "SCIM bridge OK"
                        )
        return ok, "; ".join(parts)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if text is None:
        return ""
    text = str(text).replace("\r", " ").replace("\n", " ").strip()
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _clamp(limit: Any, default: int = 25, maximum: int = _MAX_LIMIT) -> int:
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = default
    return max(1, min(limit, maximum))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _iso_to_str(value: Any) -> str:
    """Render an RFC 3339 timestamp as a compact UTC string."""
    raw = _sid(value)
    if not raw:
        return "?"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return raw


def _human_size(size: Any) -> str:
    try:
        num = float(size)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if num < 1024 or unit == "GB":
            return f"{num:.1f} {unit}" if unit != "B" else f"{int(num)} B"
        num /= 1024
    return "?"


def _split_csv(value: Any) -> list[str]:
    """Split a comma-separated string into a trimmed, non-empty list."""
    return [v.strip() for v in _sid(value).split(",") if v.strip()]


def _rfc3339(dt: datetime) -> str:
    """Format a datetime as RFC 3339 with a UTC offset."""
    return dt.astimezone(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


async def _request(*args: Any, **kwargs: Any) -> tuple[httpx.Response | None, str | None]:
    return await safe_request(*args, **kwargs)


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build 1Password tools; ``persist_file_fn`` stores a downloaded item file."""
    (
        connect_base,
        connect_key,
        events_base,
        events_key,
        scim_base,
        scim_key,
    ) = parse_onepassword_token(token)

    has_connect = bool(connect_base and connect_key)
    has_events = bool(events_key)
    has_scim = bool(scim_base and scim_key)

    if not (has_connect or has_events or has_scim):
        return []  # Misconfigured - no API calls possible

    connect_api = f"{connect_base}/v1" if connect_base else ""
    connect_headers = build_onepassword_headers(connect_key)
    events_headers = build_onepassword_headers(events_key)
    scim_headers = build_onepassword_headers(scim_key)

    connect_unconfigured_msg = (
        "Error: the 1Password Connect server is not configured for this "
        "integration. Set base_url (Connect server URL) and the Connect "
        "access token in the integration settings to enable vault and item "
        "tools."
    )
    events_unconfigured_msg = (
        "Error: the 1Password Events Reporting API is not configured for "
        "this integration. Add events_api_key (an Events Reporting bearer "
        "token from 1Password.com > Integrations) and optionally "
        "events_base_url (default https://events.1password.com; regional "
        "hosts: events.1password.ca, events.1password.eu, "
        "events.ent.1password.com) in the integration settings."
    )
    scim_unconfigured_msg = (
        "Error: the 1Password SCIM bridge is not configured for this "
        "integration. Add scim_base_url (your SCIM bridge URL) and "
        "scim_api_key (the SCIM bridge bearer token) in the integration "
        "settings to enable user and group provisioning tools."
    )

    def _connect_guard() -> str | None:
        return None if has_connect else connect_unconfigured_msg

    def _events_guard() -> str | None:
        return None if has_events else events_unconfigured_msg

    def _scim_guard() -> str | None:
        return None if has_scim else scim_unconfigured_msg

    # ------------------------------------------------------------------
    # Connect resolution helpers (the shared client, onepassword_connect.py)
    # ------------------------------------------------------------------

    def _api(client: httpx.AsyncClient) -> ConnectAPI:
        return ConnectAPI(client, connect_base, connect_key, request=_request)

    def _text(failure: ConnectFailure | None) -> str | None:
        return failure.message if failure else None

    async def _list_vaults(client: httpx.AsyncClient) -> tuple[list[dict], str | None]:
        vaults, failure = await _api(client).list_vaults()
        return vaults, _text(failure)

    async def _resolve_vault(client: httpx.AsyncClient, ref: str) -> tuple[dict | None, str | None]:
        vault, failure = await _api(client).resolve_vault(ref)
        return vault, _text(failure)

    async def _list_vault_items(client: httpx.AsyncClient, vault_id: str) -> tuple[list[dict], str | None]:
        items, failure = await _api(client).list_items(vault_id)
        return items, _text(failure)

    async def _resolve_vault_and_item(
        client: httpx.AsyncClient, vault_ref: str, item_ref: str
    ) -> tuple[dict, dict, str | None]:
        vault, item, failure = await _api(client).resolve_vault_and_item(vault_ref, item_ref)
        return vault, item, _text(failure)

    # ------------------------------------------------------------------
    # Connect formatting helpers
    # ------------------------------------------------------------------

    def _item_line(item: dict, vault_name: str = "") -> str:
        tags = item.get("tags") or []
        bits = [
            f"- '{item.get('title', '?')}' [{item.get('category', '?')}]",
            f"id={item.get('id', '?')}",
        ]
        if vault_name:
            bits.append(f"vault='{vault_name}'")
        if tags:
            bits.append(f"tags={','.join(str(t) for t in tags[:6])}")
        if item.get("favorite"):
            bits.append("favorite")
        if item.get("state"):
            bits.append(f"state={item.get('state')}")
        if item.get("updatedAt"):
            bits.append(f"updated={_iso_to_str(item.get('updatedAt'))}")
        return " ".join(bits)

    def _format_fields(item: dict) -> list[str]:
        sections = {
            _sid(s.get("id")): _sid(s.get("label")) or _sid(s.get("id"))
            for s in item.get("sections") or []
            if isinstance(s, dict)
        }
        lines: list[str] = []
        by_section: dict[str, list[dict]] = {}
        for field in item.get("fields") or []:
            if not isinstance(field, dict):
                continue
            sec_id = _sid((field.get("section") or {}).get("id"))
            by_section.setdefault(sec_id, []).append(field)
        for sec_id, fields in by_section.items():
            if sec_id:
                lines.append(f"  [Section '{sections.get(sec_id, sec_id)}']")
            for field in fields:
                label = _sid(field.get("label")) or _sid(field.get("id")) or "?"
                ftype = _sid(field.get("type")).upper() or "STRING"
                purpose = _sid(field.get("purpose")).upper()
                kind = field_secret_kind(field)
                meta = ftype + (f", purpose {purpose}" if purpose else "")
                if kind == "otp":
                    shown = "[one-time password - use get_onepassword_otp]"
                elif kind == "concealed":
                    shown = "[concealed - use reveal_onepassword_field]"
                elif purpose == "NOTES":
                    shown = _trunc(field.get("value"), 500)
                else:
                    shown = _trunc(field.get("value"), 200)
                lines.append(f"  - {label} ({meta}): {shown}")
        return lines

    def _format_item_detail(vault: dict, item: dict) -> str:
        lines = [
            f"Item: '{item.get('title', '?')}' [{item.get('category', '?')}]",
            f"  Id: {item.get('id', '?')} (vault '{vault.get('name', '?')}', "
            f"id {vault.get('id', '?')})",
        ]
        tags = item.get("tags") or []
        if tags:
            lines.append(f"  Tags: {', '.join(str(t) for t in tags)}")
        if item.get("favorite"):
            lines.append("  Favorite: yes")
        if item.get("state"):
            lines.append(f"  State: {item.get('state')}")
        version = item.get("version")
        updated = _iso_to_str(item.get("updatedAt")) if item.get("updatedAt") else ""
        meta_bits = []
        if version is not None:
            meta_bits.append(f"Version: {version}")
        if updated:
            meta_bits.append(f"Updated: {updated}")
        if item.get("lastEditedBy"):
            meta_bits.append(f"Last edited by: {item.get('lastEditedBy')}")
        if meta_bits:
            lines.append("  " + "  ".join(meta_bits))
        urls = [u for u in item.get("urls") or [] if isinstance(u, dict)]
        if urls:
            lines.append("  URLs:")
            for u in urls[:10]:
                primary = " (primary)" if u.get("primary") else ""
                label = f" [{u.get('label')}]" if u.get("label") else ""
                lines.append(f"    - {u.get('href', '?')}{label}{primary}")
        field_lines = _format_fields(item)
        if field_lines:
            lines.append("  Fields:")
            lines.extend(field_lines)
        files = [f for f in item.get("files") or [] if isinstance(f, dict)]
        if files:
            lines.append(f"  Files ({len(files)}, download_onepassword_file stores one):")
            for f in files[:20]:
                content = str(f.get("content_path") or "").lstrip("/")
                link = f" - Download URL: {connect_base}/{content}" if content and connect_base else ""
                lines.append(
                    f"    - {f.get('name', '?')} ({_human_size(f.get('size'))}, "
                    f"id={f.get('id')}){link}"
                )
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # 1. VAULTS + ITEMS (Connect)
    # ══════════════════════════════════════════════════════════════════════

    async def list_onepassword_vaults(name: str = "") -> str:
        """List the 1Password vaults in the Connect token's scope.

        Optionally filters by (case-insensitive substring) vault name.
        Returns vault ids usable in every item tool.
        """
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vaults, err = await _list_vaults(client)
            if err:
                return err
            needle = _sid(name).lower()
            if needle:
                vaults = [v for v in vaults if needle in _sid(v.get("name")).lower()]

            def _line(v: dict) -> str:
                bits = [
                    f"- '{v.get('name', '?')}' id={v.get('id', '?')}",
                    f"type={v.get('type', '?')}",
                ]
                if v.get("items") is not None:
                    bits.append(f"items={v.get('items')}")
                if v.get("updatedAt"):
                    bits.append(f"updated={_iso_to_str(v.get('updatedAt'))}")
                return " ".join(bits)

            return format_entity_list(
                vaults,
                _line,
                noun="vault",
                empty_msg=(
                    "No vaults found. The Connect token only sees the vaults "
                    "it was granted when the access token was issued."
                ),
            )

    async def search_onepassword_items(
        keyword: str = "",
        vault: str = "",
        category: str = "",
        tag: str = "",
        favorites_only: bool = False,
        limit: int = 25,
        offset: int = 0,
    ) -> str:
        """Search items in one vault or across all vaults in scope.

        Filters are combined with AND and applied to item summaries
        (title keyword, category, tag, favorite). Newest first.
        """
        if err := _connect_guard():
            return err
        limit_i = _clamp(limit)
        offset_i = _offset(offset)
        needle = _sid(keyword).lower()
        category_f = _sid(category).upper()
        tag_f = _sid(tag).lower()
        async with egress_client(timeout=60) as client:
            scoped: list[tuple[dict, str]] = []
            vault_note = ""
            if _sid(vault):
                vlt, err = await _resolve_vault(client, vault)
                if err or vlt is None:
                    return err or "Error: vault not found."
                items, err = await _list_vault_items(client, _sid(vlt.get("id")))
                if err:
                    return err
                scoped = [(i, _sid(vlt.get("name"))) for i in items]
            else:
                vaults, err = await _list_vaults(client)
                if err:
                    return err
                if len(vaults) > _MAX_SEARCH_VAULTS:
                    vault_note = (
                        f"\n[Searched the first {_MAX_SEARCH_VAULTS} of "
                        f"{len(vaults)} vaults - pass vault to search a "
                        "specific one.]"
                    )
                    vaults = vaults[:_MAX_SEARCH_VAULTS]
                for vlt in vaults:
                    items, err = await _list_vault_items(client, _sid(vlt.get("id")))
                    if err:
                        return err
                    scoped.extend((i, _sid(vlt.get("name"))) for i in items)

            def _matches(item: dict) -> bool:
                if needle and needle not in _sid(item.get("title")).lower():
                    return False
                if category_f and _sid(item.get("category")).upper() != category_f:
                    return False
                if tag_f and tag_f not in [
                    _sid(t).lower() for t in item.get("tags") or []
                ]:
                    return False
                return not (favorites_only and not item.get("favorite"))

            filtered = [(i, vn) for i, vn in scoped if _matches(i)]
            filtered.sort(key=lambda pair: _sid(pair[0].get("updatedAt")), reverse=True)
            total = len(filtered)
            page = [
                {**i, "_vault_name": vn}
                for i, vn in filtered[offset_i : offset_i + limit_i]
            ]
            body = format_entity_list(
                page,
                lambda item: _item_line(item, _sid(item.get("_vault_name"))),
                noun="item",
                total=total,
                empty_msg="No items matched the filters.",
            )
            hint = format_pagination_hint(
                str(offset_i + len(page)) if offset_i + len(page) < total else None,
                param_name="offset",
            )
            return body + hint + vault_note

    async def get_onepassword_item(item: str, vault: str) -> str:
        """Get the full detail of one item (fields, sections, URLs, files).

        Concealed values and OTP secrets are ALWAYS masked - the
        write-classified reveal_onepassword_field / get_onepassword_otp
        tools are the only paths to secret material.
        """
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            return _format_item_detail(vlt, full)

    async def get_onepassword_otp(
        item: str, vault: str, field_label: str = ""
    ) -> str:
        """Get the CURRENT one-time password (TOTP) for an item."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            _vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            fields = [f for f in full.get("fields") or [] if isinstance(f, dict)]
            otp_fields = [
                f for f in fields if _sid(f.get("type")).upper() == "OTP"
            ]
            if not otp_fields:
                labels = ", ".join(
                    f"'{_sid(f.get('label')) or _sid(f.get('id'))}'"
                    for f in fields[:15]
                )
                return (
                    f"Error: item '{full.get('title', '?')}' has no one-time "
                    "password (OTP) field. Add a TOTP secret to the item "
                    "first (update_onepassword_item / create_onepassword_item "
                    f"with otp_secret). Fields present: {labels or 'none'}."
                )
            selected = otp_fields[0]
            ref = _sid(field_label)
            if ref:
                lowered = ref.lower()
                matched = [
                    f
                    for f in otp_fields
                    if _sid(f.get("id")) == ref
                    or _sid(f.get("label")).lower() == lowered
                ]
                if not matched:
                    labels = ", ".join(
                        f"'{_sid(f.get('label')) or _sid(f.get('id'))}'"
                        for f in otp_fields
                    )
                    return (
                        f"Error: no OTP field matching '{ref}' on this item. "
                        f"OTP fields present: {labels}."
                    )
                selected = matched[0]
            # Parameters (period/digits/algorithm) come from the otpauth URI
            # when present; the code itself prefers the server-computed
            # ``totp`` attribute and falls back to a local RFC 6238
            # computation from the stored secret.
            period, digits, algorithm = 30, 6, "SHA1"
            params_err = ""
            config: totp.TOTPConfig | None = None
            raw_value = _sid(selected.get("value"))
            if raw_value:
                try:
                    config = totp.parse_secret(raw_value)
                    period, digits, algorithm = config.period, config.digits, config.algorithm
                except ValueError as exc:
                    params_err = str(exc)
            now = time.time()
            server_code = _sid(selected.get("totp"))
            if server_code:
                code = server_code
                remaining = period - int(now % period)
            else:
                if not raw_value:
                    return (
                        "Error: the OTP field exposes neither a computed "
                        "code nor its secret - the Connect token likely "
                        "lacks permission to read this field's value."
                    )
                if config is None:
                    return f"Error: cannot compute the one-time password - {params_err}"
                code, remaining = totp.code_at(config, now), totp.seconds_left(config, now)
            label = _sid(selected.get("label")) or _sid(selected.get("id")) or "otp"
            lines = [
                f"One-time password for '{full.get('title', '?')}' "
                f"[field: {label}]: {code}",
                f"Valid for ~{remaining} more second{'s' if remaining != 1 else ''} "
                f"({period}s period, {digits} digits, {algorithm}).",
            ]
            if remaining <= 5:
                lines.append(
                    "The current period is about to roll over - if the code "
                    "is rejected, call this tool again for a fresh one."
                )
            if len(otp_fields) > 1 and not ref:
                others = ", ".join(
                    f"'{_sid(f.get('label')) or _sid(f.get('id'))}'"
                    for f in otp_fields[1:]
                )
                lines.append(
                    f"Note: this item has other OTP fields ({others}) - pass "
                    "field_label to select one."
                )
            return "\n".join(lines)

    async def reveal_onepassword_field(item: str, vault: str, field: str) -> str:
        """Reveal the plaintext value of ONE field (password, API key...)."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            fld, err = _find_field(full, field)
            if err or fld is None:
                return err or f"Error: field '{field}' could not be resolved."
            label = _sid(fld.get("label")) or _sid(fld.get("id")) or "?"
            if _sid(fld.get("type")).upper() == "OTP":
                # The permanent TOTP seed is 2FA-bypass material - it never
                # leaves 1Password through this integration. Only the
                # ephemeral current code is surfaced.
                return (
                    f"Error: field '{label}' is a one-time password field - "
                    "its permanent TOTP seed is never revealed. Use "
                    "get_onepassword_otp for the current code."
                )
            value = fld.get("value")
            if value is None or value == "":
                return (
                    f"Field '{label}' of item '{full.get('title', '?')}' has "
                    "no value (or the Connect token cannot read it)."
                )
            return "\n".join(
                [
                    f"Field '{label}' of item '{full.get('title', '?')}' "
                    f"(vault '{vlt.get('name', '?')}'):",
                    str(value),
                    "\n[Handle with care - this is a live credential.]",
                ]
            )

    # ══════════════════════════════════════════════════════════════════════
    # 2. FILES (Connect)
    # ══════════════════════════════════════════════════════════════════════

    async def list_onepassword_files(item: str, vault: str) -> str:
        """List the files attached to an item."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            resp, err = await safe_request(
                client,
                "get",
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
                f"{_pid(full.get('id'))}/files",
                _PROVIDER,
                "list item files",
                headers=connect_headers,
            )
            if err:
                return err
            files, jerr = _json_list(resp)
            if jerr:
                return jerr
            base = f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/{_pid(full.get('id'))}/files"
            return format_entity_list(
                [f for f in files if isinstance(f, dict)],
                lambda f: (
                    f"- {f.get('name', '?')} ({_human_size(f.get('size'))}) "
                    f"id={f.get('id', '?')}\n"
                    f"    Download URL: {base}/{_pid(f.get('id'))}/content"
                ),
                noun="file",
                empty_msg=f"Item '{full.get('title', '?')}' has no attached files.",
            )

    async def _resolve_item_file(
        client: httpx.AsyncClient, vault: str, item: str, file: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str | None]:
        """``(vault, item, file, error)`` for a file named by id or name on an item."""
        vlt, full, err = await _resolve_vault_and_item(client, vault, item)
        if err:
            return {}, {}, {}, err
        files_resp, err = await safe_request(
            client,
            "get",
            f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
            f"{_pid(full.get('id'))}/files",
            _PROVIDER,
            "list item files",
            headers=connect_headers,
        )
        if err:
            return {}, {}, {}, err
        files, jerr = _json_list(files_resp)
        if jerr:
            return {}, {}, {}, jerr
        ref = _sid(file)
        lowered = ref.lower()
        matched = [
            f
            for f in files
            if isinstance(f, dict)
            and (_sid(f.get("id")) == ref or _sid(f.get("name")).lower() == lowered)
        ]
        if not matched:
            names = ", ".join(
                f"'{f.get('name')}' (id={f.get('id')})"
                for f in files
                if isinstance(f, dict)
            )
            return {}, {}, {}, (
                f"Error: no file matching '{ref}' on item "
                f"'{full.get('title', '?')}'. Files present: {names or 'none'}."
            )
        if len(matched) > 1:
            names = ", ".join(f"'{f.get('name')}' (id={f.get('id')})" for f in matched)
            return {}, {}, {}, (
                f"Error: file reference '{ref}' is ambiguous - matches: "
                f"{names}. Use the file id."
            )
        return vlt, full, matched[0], None

    async def read_onepassword_file(
        item: str, vault: str, file: str, max_chars: int = 20_000
    ) -> str:
        """Read the text content of a file attached to an item."""
        if err := _connect_guard():
            return err
        max_chars_i = _clamp(max_chars, default=20_000, maximum=_MAX_FILE_CHARS)
        async with egress_client(timeout=60) as client:
            vlt, full, target, err = await _resolve_item_file(client, vault, item, file)
            if err:
                return err
            size = target.get("size") or 0
            if isinstance(size, (int, float)) and size > _MAX_FILE_BYTES:
                return (
                    f"Error: file '{target.get('name', '?')}' is "
                    f"{_human_size(size)} - too large to read inline (limit "
                    f"{_human_size(_MAX_FILE_BYTES)}). Store it with "
                    "download_onepassword_file instead."
                )
            resp, err = await safe_request(
                client,
                "get",
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
                f"{_pid(full.get('id'))}/files/{_pid(target.get('id'))}/content",
                _PROVIDER,
                "read file content",
                headers={"Authorization": connect_headers.get("Authorization", "")},
            )
            if err:
                return err
            raw = resp.content
            if b"\x00" in raw[:4096]:
                return (
                    f"Error: file '{target.get('name', '?')}' looks binary - "
                    "only text files can be read inline."
                )
            text = raw.decode("utf-8", errors="replace")
            truncated = len(text) > max_chars_i
            shown = text[:max_chars_i]
            header = (
                f"File '{target.get('name', '?')}' "
                f"({_human_size(len(raw))}) from item "
                f"'{full.get('title', '?')}':\n"
            )
            note = (
                f"\n\n[Truncated at {max_chars_i} characters - pass a larger "
                "max_chars (up to 50000) for more.]"
                if truncated
                else ""
            )
            return header + shown + note

    async def download_onepassword_file(
        item: str, vault: str, file: str
    ) -> str | ToolResult:
        """Store a file attached to an item (a document item's file included) as a file of the run."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=120) as client:
            vlt, full, target, err = await _resolve_item_file(client, vault, item, file)
            if err:
                return err
            name = str(target.get("name") or target.get("id") or "1password-file")
            content_url = (
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
                f"{_pid(full.get('id'))}/files/{_pid(target.get('id'))}/content"
            )
            size = target.get("size") or 0
            if isinstance(size, (int, float)) and size > _MAX_DOWNLOAD_BYTES:
                return (
                    f"Error: file '{name}' is {_human_size(size)}, over the "
                    f"{_human_size(_MAX_DOWNLOAD_BYTES)} download limit.\n"
                    f"Download URL: {content_url}"
                )
            fetched = await fetch_file(
                client,
                "get",
                content_url,
                provider=_PROVIDER,
                action="download file content",
                max_bytes=_MAX_DOWNLOAD_BYTES,
                headers={"Authorization": connect_headers.get("Authorization", "")},
            )
        if isinstance(fetched, str):
            return f"{fetched}\nDownload URL: {content_url}"
        return await deliver_file(
            data=fetched.data,
            filename=name,
            content_type=file_content_type(fetched.content_type, name),
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=[("Download URL", content_url)],
            details=[f"From item '{full.get('title', '?')}' (vault '{vlt.get('name', '?')}')."],
            notes=["It can hold secrets: share it only where its 1Password item itself may go."],
        )

    # ══════════════════════════════════════════════════════════════════════
    # 3. ITEM LIFECYCLE (Connect)
    # ══════════════════════════════════════════════════════════════════════

    async def create_onepassword_item(
        vault: str,
        title: str,
        category: str = "LOGIN",
        username: str = "",
        password: str = "",
        generate_password: bool = False,
        password_length: int = 32,
        url: str = "",
        notes: str = "",
        otp_secret: str = "",
        tags: str = "",
        custom_fields: list[dict[str, Any]] | None = None,
    ) -> str:
        """Create a new item in a vault."""
        if err := _connect_guard():
            return err
        title_s = _sid(title)
        if not title_s:
            return "Error: title is required."
        category_s = _sid(category).upper() or "LOGIN"
        if category_s not in _ITEM_CATEGORIES:
            return (
                f"Error: unknown category '{category_s}'. Valid categories: "
                + ", ".join(_ITEM_CATEGORIES)
            )
        if _sid(password) and generate_password:
            return (
                "Error: pass either password or generate_password, not both."
            )
        if _sid(otp_secret):
            # Accepts a full otpauth:// URI or a raw base32 secret - validate
            # locally BEFORE any API call so a typo is caught early.
            try:
                totp.validate_secret(_sid(otp_secret))
            except ValueError as exc:
                return f"Error: invalid otp_secret - {exc}."
        async with egress_client(timeout=30) as client:
            vlt, err = await _resolve_vault(client, vault)
            if err or vlt is None:
                return err or "Error: vault not found."
            fields: list[dict[str, Any]] = []
            if _sid(username):
                fields.append(
                    {
                        "id": "username",
                        "type": "STRING",
                        "purpose": "USERNAME",
                        "label": "username",
                        "value": _sid(username),
                    }
                )
            if _sid(password):
                fields.append(
                    {
                        "id": "password",
                        "type": "CONCEALED",
                        "purpose": "PASSWORD",
                        "label": "password",
                        "value": str(password),
                    }
                )
            elif generate_password:
                length = max(1, min(_clamp(password_length, default=32, maximum=64), 64))
                fields.append(
                    {
                        "id": "password",
                        "type": "CONCEALED",
                        "purpose": "PASSWORD",
                        "label": "password",
                        "generate": True,
                        "recipe": {
                            "length": length,
                            "characterSets": ["LETTERS", "DIGITS", "SYMBOLS"],
                        },
                    }
                )
            if _sid(notes):
                fields.append(
                    {
                        "id": "notesPlain",
                        "type": "STRING",
                        "purpose": "NOTES",
                        "label": "notesPlain",
                        "value": str(notes),
                    }
                )
            if _sid(otp_secret):
                fields.append(
                    {
                        "label": "one-time password",
                        "type": "OTP",
                        "value": _sid(otp_secret),
                    }
                )
            for cf in custom_fields or []:
                if not isinstance(cf, dict):
                    continue
                label = _sid(cf.get("label"))
                if not label:
                    return "Error: every custom field needs a 'label'."
                ftype = _sid(cf.get("type")).upper() or (
                    "CONCEALED" if cf.get("concealed") else "STRING"
                )
                fields.append(
                    {
                        "label": label,
                        "type": ftype,
                        "value": _sid(cf.get("value")),
                    }
                )
            body: dict[str, Any] = {
                "vault": {"id": _sid(vlt.get("id"))},
                "title": title_s,
                "category": category_s,
                "fields": fields,
            }
            tag_list = _split_csv(tags)
            if tag_list:
                body["tags"] = tag_list
            if _sid(url):
                body["urls"] = [{"href": _sid(url), "primary": True}]
            resp, err = await safe_request(
                client,
                "post",
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items",
                _PROVIDER,
                "create item",
                headers=connect_headers,
                json=body,
            )
            if err:
                return err
            created, jerr = _json_dict(resp)
            if jerr:
                return jerr
            bits = [
                f"Created item '{created.get('title', title_s)}' "
                f"[{created.get('category', category_s)}] with id "
                f"{created.get('id', '?')} in vault '{vlt.get('name', '?')}'."
            ]
            if generate_password:
                bits.append(
                    "A strong password was generated server-side - use "
                    "reveal_onepassword_field to read it if needed."
                )
            if _sid(otp_secret):
                bits.append(
                    "TOTP is configured - get_onepassword_otp returns the "
                    "current code."
                )
            return " ".join(bits)

    async def update_onepassword_item(
        item: str,
        vault: str,
        new_title: str = "",
        set_tags: str = "",
        favorite: bool | None = None,
        set_url: str = "",
        set_fields: list[dict[str, Any]] | None = None,
        add_fields: list[dict[str, Any]] | None = None,
        remove_field: str = "",
    ) -> str:
        """Update an existing item (attributes and fields) via JSON Patch."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            ops: list[dict[str, Any]] = []
            applied: list[str] = []
            if _sid(new_title):
                ops.append(
                    {"op": "replace", "path": "/title", "value": _sid(new_title)}
                )
                applied.append(f"title -> '{_sid(new_title)}'")
            if _sid(set_tags):
                tag_list = _split_csv(set_tags)
                ops.append({"op": "replace", "path": "/tags", "value": tag_list})
                applied.append(f"tags -> {','.join(tag_list)}")
            if favorite is not None:
                ops.append(
                    {"op": "replace", "path": "/favorite", "value": bool(favorite)}
                )
                applied.append(f"favorite -> {bool(favorite)}")
            if _sid(set_url):
                ops.append(
                    {
                        "op": "replace",
                        "path": "/urls",
                        "value": [{"href": _sid(set_url), "primary": True}],
                    }
                )
                applied.append(f"url -> {_sid(set_url)}")
            for sf in set_fields or []:
                if not isinstance(sf, dict):
                    continue
                fld, ferr = _find_field(full, _sid(sf.get("field")))
                if ferr or fld is None:
                    return ferr or "Error: field could not be resolved."
                new_value = sf.get("value")
                if _sid(fld.get("type")).upper() == "OTP":
                    try:
                        totp.validate_secret(_sid(new_value))
                    except ValueError as exc:
                        return f"Error: invalid TOTP secret - {exc}."
                ops.append(
                    {
                        "op": "replace",
                        "path": f"/fields/{_sid(fld.get('id'))}/value",
                        "value": "" if new_value is None else str(new_value),
                    }
                )
                applied.append(
                    f"field '{_sid(fld.get('label')) or _sid(fld.get('id'))}' updated"
                )
            for af in add_fields or []:
                if not isinstance(af, dict):
                    continue
                label = _sid(af.get("label"))
                if not label:
                    return "Error: every added field needs a 'label'."
                ftype = _sid(af.get("type")).upper() or (
                    "CONCEALED" if af.get("concealed") else "STRING"
                )
                if ftype == "OTP":
                    try:
                        totp.validate_secret(_sid(af.get("value")))
                    except ValueError as exc:
                        return f"Error: invalid TOTP secret for '{label}' - {exc}."
                ops.append(
                    {
                        "op": "add",
                        "path": "/fields",
                        "value": {
                            "label": label,
                            "type": ftype,
                            "value": _sid(af.get("value")),
                        },
                    }
                )
                applied.append(f"field '{label}' added")
            if _sid(remove_field):
                fld, ferr = _find_field(full, remove_field)
                if ferr or fld is None:
                    return ferr or "Error: field could not be resolved."
                ops.append(
                    {"op": "remove", "path": f"/fields/{_sid(fld.get('id'))}"}
                )
                applied.append(
                    f"field '{_sid(fld.get('label')) or _sid(fld.get('id'))}' removed"
                )
            if not ops:
                return (
                    "Error: nothing to update - pass at least one of "
                    "new_title, set_tags, favorite, set_url, set_fields, "
                    "add_fields, remove_field."
                )
            resp, err = await safe_request(
                client,
                "patch",
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
                f"{_pid(full.get('id'))}",
                _PROVIDER,
                "update item",
                headers=connect_headers,
                json=ops,
            )
            if err:
                return err
            updated, jerr = _json_dict(resp)
            if jerr:
                return jerr
            return (
                f"Updated item '{updated.get('title', full.get('title', '?'))}' "
                f"(id {updated.get('id', '?')}, now version "
                f"{updated.get('version', '?')}): " + "; ".join(applied)
            )

    async def delete_onepassword_item(item: str, vault: str) -> str:
        """Delete an item from a vault. PERMANENT."""
        if err := _connect_guard():
            return err
        async with egress_client(timeout=30) as client:
            vlt, full, err = await _resolve_vault_and_item(client, vault, item)
            if err:
                return err
            _resp, err = await safe_request(
                client,
                "delete",
                f"{connect_api}/vaults/{_pid(vlt.get('id'))}/items/"
                f"{_pid(full.get('id'))}",
                _PROVIDER,
                "delete item",
                headers=connect_headers,
            )
            if err:
                return err
            return (
                f"Deleted item '{full.get('title', '?')}' (id "
                f"{full.get('id', '?')}) from vault '{vlt.get('name', '?')}'. "
                "This deletion is permanent - the Connect API has no restore."
            )

    # ══════════════════════════════════════════════════════════════════════
    # 4. CONNECT ACTIVITY LOG
    # ══════════════════════════════════════════════════════════════════════

    async def get_onepassword_activity(limit: int = 25, offset: int = 0) -> str:
        """List the Connect server's API request activity log."""
        if err := _connect_guard():
            return err
        limit_i = _clamp(limit)
        offset_i = _offset(offset)
        async with egress_client(timeout=30) as client:
            resp, err = await safe_request(
                client,
                "get",
                f"{connect_api}/activity",
                _PROVIDER,
                "get API activity",
                headers=connect_headers,
                params={"limit": limit_i, "offset": offset_i},
            )
            if err:
                return err
            entries, jerr = _json_list(resp)
            if jerr:
                return jerr
            # Content-Range: "1-50/1134" carries the collection total.
            total: int | None = None
            headers_obj = getattr(resp, "headers", None) or {}
            content_range = headers_obj.get("Content-Range", "")
            if "/" in content_range:
                try:
                    total = int(content_range.rsplit("/", 1)[1])
                except ValueError:
                    total = None

            def _line(e: dict) -> str:
                actor = e.get("actor") or {}
                resource = e.get("resource") or {}
                bits = [
                    f"- {_iso_to_str(e.get('timestamp'))}",
                    f"{e.get('action', '?')}",
                    f"{e.get('result', '?')}",
                ]
                rtype = _sid(resource.get("type"))
                if rtype:
                    ref = resource.get("item") or resource.get("vault") or {}
                    bits.append(f"{rtype.lower()}={_sid(ref.get('id')) or '?'}")
                if actor.get("account"):
                    bits.append(f"account={actor.get('account')}")
                if actor.get("requestIp"):
                    bits.append(f"ip={actor.get('requestIp')}")
                return " ".join(bits)

            body = format_entity_list(
                [e for e in entries if isinstance(e, dict)],
                _line,
                noun="API request",
                total=total,
                empty_msg="No Connect API activity recorded yet.",
            )
            shown = len(entries)
            has_more = total is not None and offset_i + shown < total
            hint = format_pagination_hint(
                str(offset_i + shown) if has_more or (total is None and shown >= limit_i) else None,
                param_name="offset",
            )
            return body + hint

    # ══════════════════════════════════════════════════════════════════════
    # 5. EVENTS REPORTING API (sign-ins, item usage, audit events)
    # ══════════════════════════════════════════════════════════════════════

    def _events_body(
        since_hours: Any,
        start_time: str,
        end_time: str,
        limit: Any,
        cursor: str,
    ) -> dict[str, Any] | str:
        """Build the ResetCursor / cursor request body (or an error string)."""
        if _sid(cursor):
            return {"cursor": _sid(cursor)}
        body: dict[str, Any] = {
            "limit": _clamp(limit, default=50, maximum=_MAX_EVENT_LIMIT)
        }
        if _sid(start_time):
            body["start_time"] = _sid(start_time)
        else:
            try:
                hours = float(since_hours) if since_hours else 24.0
            except (TypeError, ValueError):
                return "Error: since_hours must be a number of hours."
            hours = max(0.1, min(hours, 24 * 365))
            body["start_time"] = _rfc3339(datetime.now(UTC) - timedelta(hours=hours))
        if _sid(end_time):
            body["end_time"] = _sid(end_time)
        return body

    async def _events_call(
        client: httpx.AsyncClient, path: str, action: str, body: dict[str, Any]
    ) -> tuple[dict, str | None]:
        resp, err = await safe_request(
            client,
            "post",
            f"{events_base}{path}",
            _PROVIDER,
            action,
            headers=events_headers,
            json=body,
        )
        if err:
            return {}, err
        return _json_dict(resp)

    def _events_footer(data: dict, shown: int) -> str:
        cursor_val = _sid(data.get("cursor"))
        if data.get("has_more") and cursor_val:
            return format_pagination_hint(cursor_val, param_name="cursor")
        if shown and cursor_val:
            return (
                "\n[No more events right now - poll again later with "
                f'cursor="{cursor_val}" to resume from this checkpoint.]'
            )
        return ""

    def _user_str(user: Any) -> str:
        if not isinstance(user, dict):
            return "?"
        email = _sid(user.get("email"))
        name = _sid(user.get("name"))
        if email and name:
            return f"{name} <{email}>"
        return email or name or _sid(user.get("uuid")) or "?"

    def _client_str(client_obj: Any) -> str:
        if not isinstance(client_obj, dict):
            return ""
        bits = []
        if client_obj.get("app_name"):
            bits.append(
                f"app={client_obj.get('app_name')} {client_obj.get('app_version', '')}".strip()
            )
        if client_obj.get("os_name"):
            bits.append(f"os={client_obj.get('os_name')} {client_obj.get('os_version', '')}".strip())
        if client_obj.get("ip_address"):
            bits.append(f"ip={client_obj.get('ip_address')}")
        return " ".join(bits)

    def _location_str(location: Any) -> str:
        if not isinstance(location, dict):
            return ""
        bits = [
            _sid(location.get(k))
            for k in ("city", "region", "country")
            if _sid(location.get(k))
        ]
        return f"location={', '.join(bits)}" if bits else ""

    async def list_onepassword_signin_attempts(
        since_hours: float = 24,
        start_time: str = "",
        end_time: str = "",
        category: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> str:
        """List 1Password account sign-in attempts (Events API)."""
        if err := _events_guard():
            return err
        body = _events_body(since_hours, start_time, end_time, limit, cursor)
        if isinstance(body, str):
            return body
        category_f = _sid(category).lower()
        if category_f and category_f not in _SIGNIN_CATEGORIES:
            return (
                f"Error: unknown category '{category_f}'. Valid categories: "
                + ", ".join(_SIGNIN_CATEGORIES)
            )
        async with egress_client(timeout=30) as client:
            data, err = await _events_call(
                client, "/api/v2/signinattempts", "list sign-in attempts", body
            )
            if err:
                return err
            items = [i for i in data.get("items") or [] if isinstance(i, dict)]
            if category_f:
                items = [
                    i for i in items if _sid(i.get("category")).lower() == category_f
                ]

            def _line(e: dict) -> str:
                bits = [
                    f"- {_iso_to_str(e.get('timestamp'))}",
                    f"{e.get('category', '?')}/{e.get('type', '?')}",
                    _user_str(e.get("target_user")),
                ]
                details = e.get("details") or {}
                if isinstance(details, dict) and _sid(details.get("value")):
                    bits.append(f"details={_sid(details.get('value'))}")
                client_s = _client_str(e.get("client"))
                if client_s:
                    bits.append(client_s)
                loc = _location_str(e.get("location"))
                if loc:
                    bits.append(loc)
                return " ".join(bits)

            note = (
                f"\n[Category filter '{category_f}' applied client-side to "
                "this page of events.]"
                if category_f
                else ""
            )
            return (
                format_entity_list(
                    items,
                    _line,
                    noun="sign-in attempt",
                    empty_msg="No sign-in attempts in the window.",
                )
                + note
                + _events_footer(data, len(items))
            )

    async def list_onepassword_item_usages(
        since_hours: float = 24,
        start_time: str = "",
        end_time: str = "",
        action: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> str:
        """List item usage events - who accessed which item (Events API)."""
        if err := _events_guard():
            return err
        body = _events_body(since_hours, start_time, end_time, limit, cursor)
        if isinstance(body, str):
            return body
        action_f = _sid(action).lower()
        async with egress_client(timeout=30) as client:
            data, err = await _events_call(
                client, "/api/v2/itemusages", "list item usages", body
            )
            if err:
                return err
            items = [i for i in data.get("items") or [] if isinstance(i, dict)]
            if action_f:
                items = [i for i in items if _sid(i.get("action")).lower() == action_f]

            def _line(e: dict) -> str:
                bits = [
                    f"- {_iso_to_str(e.get('timestamp'))}",
                    f"action={e.get('action', '?')}",
                    _user_str(e.get("user")),
                    f"item={e.get('item_uuid', '?')}",
                    f"vault={e.get('vault_uuid', '?')}",
                ]
                client_s = _client_str(e.get("client"))
                if client_s:
                    bits.append(client_s)
                return " ".join(bits)

            note = (
                "\n[Items are referenced by UUID - resolve names with "
                "get_onepassword_item when the Connect server is configured.]"
                if items
                else ""
            )
            action_note = (
                f"\n[Action filter '{action_f}' applied client-side to this "
                "page of events.]"
                if action_f
                else ""
            )
            return (
                format_entity_list(
                    items,
                    _line,
                    noun="item usage event",
                    empty_msg="No item usage events in the window.",
                )
                + note
                + action_note
                + _events_footer(data, len(items))
            )

    async def list_onepassword_audit_events(
        since_hours: float = 24,
        start_time: str = "",
        end_time: str = "",
        action: str = "",
        object_type: str = "",
        limit: int = 50,
        cursor: str = "",
    ) -> str:
        """List account audit events - admin and lifecycle actions (Events API)."""
        if err := _events_guard():
            return err
        body = _events_body(since_hours, start_time, end_time, limit, cursor)
        if isinstance(body, str):
            return body
        action_f = _sid(action).lower()
        object_f = _sid(object_type).lower()
        async with egress_client(timeout=30) as client:
            data, err = await _events_call(
                client, "/api/v2/auditevents", "list audit events", body
            )
            if err:
                return err
            items = [i for i in data.get("items") or [] if isinstance(i, dict)]
            if action_f:
                items = [i for i in items if _sid(i.get("action")).lower() == action_f]
            if object_f:
                items = [
                    i for i in items if _sid(i.get("object_type")).lower() == object_f
                ]

            def _line(e: dict) -> str:
                bits = [
                    f"- {_iso_to_str(e.get('timestamp'))}",
                    f"action={e.get('action', '?')}",
                    f"object={e.get('object_type', '?')}:{e.get('object_uuid', '?')}",
                    f"actor={_user_str(e.get('actor_details'))}",
                ]
                aux = e.get("aux_details")
                if isinstance(aux, dict) and (aux.get("email") or aux.get("name")):
                    bits.append(f"aux={_user_str(aux)}")
                if _sid(e.get("aux_info")):
                    bits.append(f"info={_sid(e.get('aux_info'))}")
                session = e.get("session") or {}
                if isinstance(session, dict) and _sid(session.get("ip")):
                    bits.append(f"ip={_sid(session.get('ip'))}")
                return " ".join(bits)

            filter_note = (
                "\n[action/object_type filters applied client-side to this "
                "page of events.]"
                if (action_f or object_f)
                else ""
            )
            return (
                format_entity_list(
                    items,
                    _line,
                    noun="audit event",
                    empty_msg="No audit events in the window.",
                )
                + filter_note
                + _events_footer(data, len(items))
            )

    # ══════════════════════════════════════════════════════════════════════
    # 6. SCIM BRIDGE (users + groups provisioning)
    # ══════════════════════════════════════════════════════════════════════

    async def _scim_get(
        client: httpx.AsyncClient,
        path: str,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> tuple[dict, str | None]:
        resp, err = await safe_request(
            client,
            "get",
            f"{scim_base}{path}",
            _PROVIDER,
            action,
            headers=scim_headers,
            params=params,
        )
        if err:
            return {}, err
        return _json_dict(resp)

    async def _scim_pages(
        client: httpx.AsyncClient,
        path: str,
        action: str,
        *,
        max_pages: int = _MAX_SCIM_PAGES,
    ) -> tuple[list[dict], int, str | None]:
        """Page through a SCIM ListResponse; returns (resources, total, error)."""
        resources: list[dict] = []
        start = 1
        total = 0
        for _page in range(max_pages):
            data, err = await _scim_get(
                client, path, action, params={"startIndex": start, "count": 100}
            )
            if err:
                return [], 0, err
            page_items = [
                r for r in data.get("Resources") or [] if isinstance(r, dict)
            ]
            resources.extend(page_items)
            total = data.get("totalResults") or len(resources)
            start += len(page_items)
            if not page_items or len(resources) >= total:
                break
        return resources, total, None

    def _scim_user_line(user: dict) -> str:
        name = user.get("name") or {}
        display = (
            _sid(name.get("formatted"))
            or f"{_sid(name.get('givenName'))} {_sid(name.get('familyName'))}".strip()
            or _sid(user.get("displayName"))
        )
        email = _sid(user.get("userName"))
        active = "active" if user.get("active", True) else "SUSPENDED"
        return f"- {display or '?'} <{email or '?'}> id={user.get('id', '?')} {active}"

    async def _scim_resolve_user(
        client: httpx.AsyncClient, ref: str
    ) -> tuple[dict | None, str | None]:
        """Resolve a SCIM user by id or email/userName (client-side match)."""
        ref_s = _sid(ref)
        if not ref_s:
            return None, "Error: a user (id or email) is required."
        if "@" not in ref_s:
            data, err = await _scim_get(
                client, f"/Users/{_pid(ref_s)}", "get user"
            )
            if err:
                return None, err
            return data, None
        # 1Password's SCIM filter support is unreliable - resolve by paging
        # and matching client-side.
        users, _total, err = await _scim_pages(client, "/Users", "find user")
        if err:
            return None, err
        lowered = ref_s.lower()
        matched = [
            u
            for u in users
            if _sid(u.get("userName")).lower() == lowered
            or any(
                _sid(e.get("value")).lower() == lowered
                for e in u.get("emails") or []
                if isinstance(e, dict)
            )
        ]
        if not matched:
            return None, (
                f"Error: no user matching '{ref_s}' found. Use "
                "list_onepassword_users to browse the directory."
            )
        if len(matched) > 1:
            ids = ", ".join(_sid(u.get("id")) for u in matched[:10])
            return None, (
                f"Error: '{ref_s}' matches several users ({ids}) - use the id."
            )
        return matched[0], None

    async def _scim_resolve_group(
        client: httpx.AsyncClient, ref: str
    ) -> tuple[dict | None, str | None]:
        """Resolve a SCIM group by id or displayName."""
        ref_s = _sid(ref)
        if not ref_s:
            return None, "Error: a group (id or name) is required."
        groups, _total, err = await _scim_pages(client, "/Groups", "find group")
        if err:
            return None, err
        by_id = [g for g in groups if _sid(g.get("id")) == ref_s]
        if len(by_id) == 1:
            return by_id[0], None
        lowered = ref_s.lower()
        matched = [g for g in groups if _sid(g.get("displayName")).lower() == lowered]
        if not matched:
            return None, (
                f"Error: no group matching '{ref_s}' found. Use "
                "list_onepassword_groups to browse the groups."
            )
        if len(matched) > 1:
            ids = ", ".join(_sid(g.get("id")) for g in matched[:10])
            return None, (
                f"Error: '{ref_s}' matches several groups ({ids}) - use the id."
            )
        return matched[0], None

    async def _scim_patch(
        client: httpx.AsyncClient,
        path: str,
        action: str,
        operations: list[dict[str, Any]],
    ) -> tuple[dict, str | None]:
        resp, err = await safe_request(
            client,
            "patch",
            f"{scim_base}{path}",
            _PROVIDER,
            action,
            headers=scim_headers,
            json={"schemas": [_SCIM_PATCH_SCHEMA], "Operations": operations},
        )
        if err:
            return {}, err
        if getattr(resp, "status_code", 200) == 204:
            return {}, None
        return _json_dict(resp)

    async def list_onepassword_users(
        query: str = "", active_only: bool = False, limit: int = 50, offset: int = 0
    ) -> str:
        """List 1Password account members via the SCIM bridge."""
        if err := _scim_guard():
            return err
        limit_i = _clamp(limit)
        offset_i = _offset(offset)
        async with egress_client(timeout=60) as client:
            users, _total, err = await _scim_pages(client, "/Users", "list users")
            if err:
                return err
            needle = _sid(query).lower()
            if needle:
                def _hay(u: dict) -> str:
                    name = u.get("name") or {}
                    return " ".join(
                        [
                            _sid(u.get("userName")),
                            _sid(name.get("formatted")),
                            _sid(name.get("givenName")),
                            _sid(name.get("familyName")),
                            _sid(u.get("displayName")),
                        ]
                    ).lower()

                users = [u for u in users if needle in _hay(u)]
            if active_only:
                users = [u for u in users if u.get("active", True)]
            total_f = len(users)
            page = users[offset_i : offset_i + limit_i]
            body = format_entity_list(
                page,
                _scim_user_line,
                noun="user",
                total=total_f,
                empty_msg="No users matched.",
            )
            hint = format_pagination_hint(
                str(offset_i + len(page)) if offset_i + len(page) < total_f else None,
                param_name="offset",
            )
            return body + hint

    async def list_onepassword_groups(query: str = "", limit: int = 50) -> str:
        """List 1Password groups synced through the SCIM bridge."""
        if err := _scim_guard():
            return err
        limit_i = _clamp(limit)
        async with egress_client(timeout=60) as client:
            groups, _total, err = await _scim_pages(client, "/Groups", "list groups")
            if err:
                return err
            needle = _sid(query).lower()
            if needle:
                groups = [
                    g for g in groups if needle in _sid(g.get("displayName")).lower()
                ]

            def _line(g: dict) -> str:
                members = g.get("members")
                member_str = (
                    f" members={len(members)}" if isinstance(members, list) else ""
                )
                return (
                    f"- '{g.get('displayName', '?')}' id={g.get('id', '?')}"
                    + member_str
                )

            return format_entity_list(
                groups[:limit_i],
                _line,
                noun="group",
                total=len(groups),
                empty_msg="No groups matched.",
            )

    async def manage_onepassword_provisioning(
        action: str,
        user: str = "",
        group: str = "",
        email: str = "",
        given_name: str = "",
        family_name: str = "",
        name: str = "",
    ) -> str:
        """Consolidated SCIM provisioning tool (user + group lifecycle)."""
        if err := _scim_guard():
            return err
        action_s = _sid(action).lower()
        if action_s not in _PROVISIONING_ACTIONS:
            return (
                f"Error: unknown action '{action_s}'. Valid actions: "
                + ", ".join(sorted(_PROVISIONING_ACTIONS))
            )
        async with egress_client(timeout=60) as client:
            if action_s == "get_user":
                usr, err = await _scim_resolve_user(client, user)
                if err or usr is None:
                    return err or "Error: user not found."
                lines = [_scim_user_line(usr).lstrip("- ")]
                groups = [
                    g for g in usr.get("groups") or [] if isinstance(g, dict)
                ]
                if groups:
                    names = ", ".join(
                        _sid(g.get("display")) or _sid(g.get("value")) for g in groups
                    )
                    lines.append(f"  Groups: {names}")
                if usr.get("meta") and isinstance(usr["meta"], dict):
                    created = usr["meta"].get("created")
                    if created:
                        lines.append(f"  Created: {_iso_to_str(created)}")
                return "\n".join(lines)

            if action_s == "create_user":
                email_s = _sid(email) or _sid(user)
                if not email_s or "@" not in email_s:
                    return "Error: create_user requires an email."
                body: dict[str, Any] = {
                    "schemas": [_SCIM_USER_SCHEMA],
                    "userName": email_s,
                    "emails": [{"value": email_s, "primary": True}],
                    "active": True,
                }
                if _sid(given_name) or _sid(family_name):
                    body["name"] = {
                        "givenName": _sid(given_name),
                        "familyName": _sid(family_name),
                    }
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{scim_base}/Users",
                    _PROVIDER,
                    "create user",
                    headers=scim_headers,
                    json=body,
                )
                if err:
                    return err
                created, jerr = _json_dict(resp)
                if jerr:
                    return jerr
                return (
                    f"Invited/provisioned user {email_s} (id "
                    f"{created.get('id', '?')}). 1Password sends the account "
                    "invitation automatically."
                )

            if action_s == "update_user_name":
                usr, err = await _scim_resolve_user(client, user)
                if err or usr is None:
                    return err or "Error: user not found."
                if not (_sid(given_name) or _sid(family_name)):
                    return (
                        "Error: update_user_name requires given_name and/or "
                        "family_name."
                    )
                existing = usr.get("name") or {}
                new_name = {
                    "givenName": _sid(given_name) or _sid(existing.get("givenName")),
                    "familyName": _sid(family_name)
                    or _sid(existing.get("familyName")),
                }
                _data, err = await _scim_patch(
                    client,
                    f"/Users/{_pid(usr.get('id'))}",
                    "update user name",
                    [{"op": "replace", "value": {"name": new_name}}],
                )
                if err:
                    return err
                return (
                    f"Updated name of {_sid(usr.get('userName'))} to "
                    f"'{new_name['givenName']} {new_name['familyName']}'".strip()
                    + "."
                )

            if action_s in ("deactivate_user", "activate_user"):
                usr, err = await _scim_resolve_user(client, user)
                if err or usr is None:
                    return err or "Error: user not found."
                active = action_s == "activate_user"
                _data, err = await _scim_patch(
                    client,
                    f"/Users/{_pid(usr.get('id'))}",
                    "set user active state",
                    [{"op": "replace", "value": {"active": active}}],
                )
                if err:
                    return err
                if active:
                    return f"Reactivated user {_sid(usr.get('userName'))}."
                return (
                    f"Suspended user {_sid(usr.get('userName'))} - their "
                    "1Password access is deprovisioned (1Password suspends "
                    "rather than deletes accounts)."
                )

            if action_s == "get_group":
                grp, err = await _scim_resolve_group(client, group)
                if err or grp is None:
                    return err or "Error: group not found."
                members = [
                    m for m in grp.get("members") or [] if isinstance(m, dict)
                ]
                lines = [
                    f"Group '{grp.get('displayName', '?')}' id={grp.get('id', '?')} "
                    f"({len(members)} member{'s' if len(members) != 1 else ''})"
                ]
                for m in members[:50]:
                    lines.append(
                        f"  - {_sid(m.get('display')) or '?'} "
                        f"(user id {_sid(m.get('value')) or '?'})"
                    )
                if len(members) > 50:
                    lines.append(f"  ... and {len(members) - 50} more")
                return "\n".join(lines)

            if action_s == "create_group":
                name_s = _sid(name) or _sid(group)
                if not name_s:
                    return "Error: create_group requires a name."
                resp, err = await safe_request(
                    client,
                    "post",
                    f"{scim_base}/Groups",
                    _PROVIDER,
                    "create group",
                    headers=scim_headers,
                    json={"schemas": [_SCIM_GROUP_SCHEMA], "displayName": name_s},
                )
                if err:
                    return err
                created, jerr = _json_dict(resp)
                if jerr:
                    return jerr
                return f"Created group '{name_s}' (id {created.get('id', '?')})."

            if action_s == "rename_group":
                grp, err = await _scim_resolve_group(client, group)
                if err or grp is None:
                    return err or "Error: group not found."
                name_s = _sid(name)
                if not name_s:
                    return "Error: rename_group requires the new name."
                _data, err = await _scim_patch(
                    client,
                    f"/Groups/{_pid(grp.get('id'))}",
                    "rename group",
                    [{"op": "replace", "value": {"displayName": name_s}}],
                )
                if err:
                    return err
                return (
                    f"Renamed group '{grp.get('displayName', '?')}' to "
                    f"'{name_s}'."
                )

            if action_s in ("add_group_member", "remove_group_member"):
                grp, err = await _scim_resolve_group(client, group)
                if err or grp is None:
                    return err or "Error: group not found."
                usr, err = await _scim_resolve_user(client, user)
                if err or usr is None:
                    return err or "Error: user not found."
                user_id = _sid(usr.get("id"))
                if action_s == "add_group_member":
                    ops = [
                        {
                            "op": "add",
                            "path": "members",
                            "value": [{"value": user_id}],
                        }
                    ]
                    verb = "Added"
                    prep = "to"
                else:
                    ops = [
                        {
                            "op": "remove",
                            "path": f'members[value eq "{user_id}"]',
                        }
                    ]
                    verb = "Removed"
                    prep = "from"
                _data, err = await _scim_patch(
                    client,
                    f"/Groups/{_pid(grp.get('id'))}",
                    "update group members",
                    ops,
                )
                if err:
                    return err
                return (
                    f"{verb} {_sid(usr.get('userName'))} {prep} group "
                    f"'{grp.get('displayName', '?')}'."
                )

            # delete_group (destructive)
            grp, err = await _scim_resolve_group(client, group)
            if err or grp is None:
                return err or "Error: group not found."
            _resp, err = await safe_request(
                client,
                "delete",
                f"{scim_base}/Groups/{_pid(grp.get('id'))}",
                _PROVIDER,
                "delete group",
                headers=scim_headers,
            )
            if err:
                return err
            return (
                f"Deleted group '{grp.get('displayName', '?')}' (id "
                f"{grp.get('id', '?')}). This is permanent; members keep "
                "their accounts."
            )

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    # 1. Vaults + items
    executor.register("list_onepassword_vaults", list_onepassword_vaults)
    executor.register("search_onepassword_items", search_onepassword_items)
    executor.register("get_onepassword_item", get_onepassword_item)
    executor.register("get_onepassword_otp", get_onepassword_otp)
    executor.register("reveal_onepassword_field", reveal_onepassword_field)
    # 2. Files
    executor.register("list_onepassword_files", list_onepassword_files)
    executor.register("read_onepassword_file", read_onepassword_file)
    executor.register("download_onepassword_file", download_onepassword_file)
    # 3. Item lifecycle
    executor.register("create_onepassword_item", create_onepassword_item)
    executor.register("update_onepassword_item", update_onepassword_item)
    executor.register("delete_onepassword_item", delete_onepassword_item)
    # 4. Connect activity
    executor.register("get_onepassword_activity", get_onepassword_activity)
    # 5. Events Reporting
    executor.register(
        "list_onepassword_signin_attempts", list_onepassword_signin_attempts
    )
    executor.register("list_onepassword_item_usages", list_onepassword_item_usages)
    executor.register("list_onepassword_audit_events", list_onepassword_audit_events)
    # 6. SCIM provisioning
    executor.register("list_onepassword_users", list_onepassword_users)
    executor.register("list_onepassword_groups", list_onepassword_groups)
    executor.register(
        "manage_onepassword_provisioning", manage_onepassword_provisioning
    )

    _limit_schema = {
        "type": "integer",
        "description": f"Max results to return (max {_MAX_LIMIT})",
        "default": 25,
    }
    _offset_schema = {
        "type": "integer",
        "description": "Offset for pagination",
        "default": 0,
    }
    _vault_schema = {
        "type": "string",
        "description": "Vault name or 26-char vault id",
    }
    _item_schema = {
        "type": "string",
        "description": "Item title or 26-char item id",
    }
    _events_window_props = {
        "since_hours": {
            "type": "number",
            "description": (
                "Look-back window in hours from now (default 24). Ignored "
                "when start_time or cursor is passed."
            ),
            "default": 24,
        },
        "start_time": {
            "type": "string",
            "description": "Window start, RFC 3339 (e.g. 2026-08-01T00:00:00Z)",
            "default": "",
        },
        "end_time": {
            "type": "string",
            "description": "Window end, RFC 3339 (optional)",
            "default": "",
        },
        "limit": {
            "type": "integer",
            "description": f"Events per page (max {_MAX_EVENT_LIMIT})",
            "default": 50,
        },
        "cursor": {
            "type": "string",
            "description": (
                "Continuation cursor from a previous response - resumes the "
                "event stream exactly where it left off"
            ),
            "default": "",
        },
    }

    return [
        # -- 1. Vaults + items (read) --------------------------------------
        ToolDefinition(
            name="list_onepassword_vaults",
            description=(
                "List the 1Password vaults the Connect token can access, with "
                "ids, types, and item counts. Optionally filter by vault name "
                "(case-insensitive substring). Vault ids/names feed every "
                "other item tool."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Filter vaults by name (substring)",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="search_onepassword_items",
            description=(
                "Search 1Password items by title keyword, category, tag, and "
                "favorite flag - in one vault (pass vault) or across all "
                "vaults in scope. Returns item ids and titles for follow-up "
                "calls (get_onepassword_item, get_onepassword_otp). Filters "
                "AND together; newest first. Use offset for pagination."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "keyword": {
                        "type": "string",
                        "description": "Keyword matched against item titles",
                        "default": "",
                    },
                    "vault": {
                        "type": "string",
                        "description": (
                            "Vault name or id (omit to search every vault in "
                            "scope)"
                        ),
                        "default": "",
                    },
                    "category": {
                        "type": "string",
                        "description": "Item category filter",
                        "enum": ["", *_ITEM_CATEGORIES],
                        "default": "",
                    },
                    "tag": {
                        "type": "string",
                        "description": "Exact tag the item must carry",
                        "default": "",
                    },
                    "favorites_only": {
                        "type": "boolean",
                        "description": "Only favorite items",
                        "default": False,
                    },
                    "limit": _limit_schema,
                    "offset": _offset_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_onepassword_item",
            description=(
                "Get the full detail of one 1Password item: fields grouped "
                "by section, URLs, tags, files, and metadata. Concealed "
                "values (passwords, API keys) and OTP secrets are ALWAYS "
                "masked - use reveal_onepassword_field to fetch exactly one "
                "secret, and get_onepassword_otp for the current one-time "
                "password."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                },
                "required": ["item", "vault"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="get_onepassword_otp",
            description=(
                "Get the CURRENT one-time password (TOTP / 2FA code) for a "
                "1Password item, with the remaining validity in seconds. "
                "Uses the server-computed code when available and computes "
                "RFC 6238 locally from the stored secret otherwise. Use "
                "this whenever a login or API flow asks for a 2FA / MFA / "
                "verification code. Pass field_label when the item has "
                "several OTP fields."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                    "field_label": {
                        "type": "string",
                        "description": (
                            "OTP field label or id when the item has more "
                            "than one"
                        ),
                        "default": "",
                    },
                },
                "required": ["item", "vault"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="reveal_onepassword_field",
            description=(
                "Reveal the plaintext value of ONE field of a 1Password item "
                "(a password, API key, or other concealed value). "
                "Least-privilege by design: fetches exactly the field asked "
                "for. The value lands in the conversation - only use it when "
                "the workflow genuinely needs the secret. OTP fields are "
                "refused (the permanent TOTP seed never leaves 1Password) - "
                "use get_onepassword_otp for the current code."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                    "field": {
                        "type": "string",
                        "description": "Field label or field id (e.g. 'password')",
                    },
                },
                "required": ["item", "vault", "field"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 2. Files (read) -------------------------------------------------
        ToolDefinition(
            name="list_onepassword_files",
            description=(
                "List the files attached to a 1Password item (names, sizes, "
                "ids, Connect download URLs). Use read_onepassword_file to read "
                "a text attachment inline, download_onepassword_file to store "
                "any file."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                },
                "required": ["item", "vault"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="read_onepassword_file",
            description=(
                "Read the text content of a file attached to a 1Password "
                "item (config files, recovery codes, certificates...). "
                "Binary files and files over 1 MB are refused; output is "
                "truncated at max_chars."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                    "file": {
                        "type": "string",
                        "description": "File name or file id",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "Max characters to return (max 50000)",
                        "default": 20000,
                    },
                },
                "required": ["item", "vault", "file"],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="download_onepassword_file",
            workspace_ingest=True,
            description=(
                "Store a file attached to a 1Password item - a Document item's "
                "file included, any type up to 100 MB - as a file of the "
                "conversation (download card + file_id to forward, read inline) "
                "with its Connect download URL. The file can hold secrets: "
                "forward it only where the item itself may go. The Connect API "
                "cannot add files to items."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                    "file": {
                        "type": "string",
                        "description": "File name or file id",
                    },
                },
                "required": ["item", "vault", "file"],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 3. Item lifecycle (write) ----------------------------------------
        ToolDefinition(
            name="create_onepassword_item",
            description=(
                "Create a new item in a 1Password vault (LOGIN, PASSWORD, "
                "API_CREDENTIAL, SECURE_NOTE, DATABASE, SERVER, SSH_KEY...). "
                "Supports username/password, server-side strong password "
                "generation (generate_password + password_length), a website "
                "URL, notes, tags, custom fields, and seeding a TOTP secret "
                "(otp_secret: otpauth:// URI or raw base32) so "
                "get_onepassword_otp works immediately."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "vault": _vault_schema,
                    "title": {"type": "string", "description": "Item title"},
                    "category": {
                        "type": "string",
                        "description": "Item category",
                        "enum": list(_ITEM_CATEGORIES),
                        "default": "LOGIN",
                    },
                    "username": {
                        "type": "string",
                        "description": "Username / account identifier",
                        "default": "",
                    },
                    "password": {
                        "type": "string",
                        "description": (
                            "Explicit password value (mutually exclusive "
                            "with generate_password)"
                        ),
                        "default": "",
                    },
                    "generate_password": {
                        "type": "boolean",
                        "description": (
                            "Let 1Password generate a strong password "
                            "server-side"
                        ),
                        "default": False,
                    },
                    "password_length": {
                        "type": "integer",
                        "description": "Generated password length (1-64)",
                        "default": 32,
                    },
                    "url": {
                        "type": "string",
                        "description": "Primary website URL",
                        "default": "",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Free-form notes",
                        "default": "",
                    },
                    "otp_secret": {
                        "type": "string",
                        "description": (
                            "TOTP secret - full otpauth:// URI or raw base32 "
                            "seed"
                        ),
                        "default": "",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tags",
                        "default": "",
                    },
                    "custom_fields": {
                        "type": "array",
                        "description": (
                            "Extra fields: [{label, value, type?, "
                            "concealed?}] - type in STRING/CONCEALED/EMAIL/"
                            "URL/OTP/DATE/MONTH_YEAR"
                        ),
                        "items": {"type": "object"},
                        "default": [],
                    },
                },
                "required": ["vault", "title"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="update_onepassword_item",
            description=(
                "Update an existing 1Password item: rename, replace tags, "
                "toggle favorite, set the primary URL, set field values by "
                "label or id (set_fields), add new fields (add_fields, incl. "
                "type OTP to add a TOTP secret), or remove a field. Only the "
                "passed changes are applied (RFC 6902 patch)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                    "new_title": {
                        "type": "string",
                        "description": "New item title",
                        "default": "",
                    },
                    "set_tags": {
                        "type": "string",
                        "description": (
                            "Comma-separated tags - REPLACES the tag list"
                        ),
                        "default": "",
                    },
                    "favorite": {
                        "type": "boolean",
                        "description": "Set or clear the favorite flag",
                    },
                    "set_url": {
                        "type": "string",
                        "description": "Replace the primary website URL",
                        "default": "",
                    },
                    "set_fields": {
                        "type": "array",
                        "description": (
                            "Field value updates: [{field: label-or-id, "
                            "value}]"
                        ),
                        "items": {"type": "object"},
                        "default": [],
                    },
                    "add_fields": {
                        "type": "array",
                        "description": (
                            "New fields: [{label, value, type?, concealed?}]"
                        ),
                        "items": {"type": "object"},
                        "default": [],
                    },
                    "remove_field": {
                        "type": "string",
                        "description": "Label or id of a field to remove",
                        "default": "",
                    },
                },
                "required": ["item", "vault"],
            },
            source=_SOURCE,
            priority=1,
        ),
        ToolDefinition(
            name="delete_onepassword_item",
            description=(
                "Delete a 1Password item from its vault. PERMANENT and "
                "cannot be undone through the Connect API - double-check the "
                "item and vault before calling."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "item": _item_schema,
                    "vault": _vault_schema,
                },
                "required": ["item", "vault"],
            },
            source=_SOURCE,
            priority=2,
        ),
        # -- 4. Connect activity (read) ---------------------------------------
        ToolDefinition(
            name="get_onepassword_activity",
            description=(
                "List the 1Password Connect server's API activity log: which "
                "access token performed which READ/CREATE/UPDATE/DELETE, on "
                "which vault/item, from which IP, and whether it was allowed "
                "(SUCCESS) or denied (DENY). Use to audit automation access "
                "to secrets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "limit": _limit_schema,
                    "offset": _offset_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 5. Events Reporting (read) ---------------------------------------
        ToolDefinition(
            name="list_onepassword_signin_attempts",
            description=(
                "List 1Password account sign-in attempts (Events Reporting "
                "API, 1Password Business): who signed in (or failed to), "
                "when, from which app/OS/IP/location, and why failures "
                "happened (bad credentials, MFA/TOTP failures, firewall "
                "blocks...). Filter by category client-side. Cursor-based: "
                "pass the returned cursor to poll for newer events."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_events_window_props,
                    "category": {
                        "type": "string",
                        "description": "Filter by attempt category",
                        "enum": ["", *_SIGNIN_CATEGORIES],
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_onepassword_item_usages",
            description=(
                "List 1Password item usage events (Events Reporting API, "
                "1Password Business): who accessed/filled/revealed/exported "
                "which item in shared vaults, from which client and IP. "
                "Items are referenced by UUID - resolve names with "
                "get_onepassword_item. Optional action filter (fill, reveal, "
                "secure-copy, export, share, server-fetch...)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_events_window_props,
                    "action": {
                        "type": "string",
                        "description": (
                            "Filter by usage action (fill, reveal, "
                            "secure-copy, export, share, "
                            "enter-item-edit-mode, server-create, "
                            "server-fetch, server-update, "
                            "select-sso-provider, other)"
                        ),
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_onepassword_audit_events",
            description=(
                "List 1Password account audit events (Events Reporting API, "
                "1Password Business): admin and lifecycle actions - user "
                "joins/suspensions, role and group changes, vault "
                "grants/revocations, device authorizations, SSO changes... "
                "Optional client-side filters on action (join, grant, "
                "revoke, suspend, role, create, delete...) and object_type "
                "(user, group, vault, device, sa...)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    **_events_window_props,
                    "action": {
                        "type": "string",
                        "description": "Filter by audit action",
                        "default": "",
                    },
                    "object_type": {
                        "type": "string",
                        "description": "Filter by object type",
                        "default": "",
                    },
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        # -- 6. SCIM provisioning ----------------------------------------------
        ToolDefinition(
            name="list_onepassword_users",
            description=(
                "List 1Password account members via the SCIM bridge "
                "(1Password Business): names, emails, ids, and "
                "active/suspended state. Optional query matches name and "
                "email; active_only hides suspended users."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Match against name and email",
                        "default": "",
                    },
                    "active_only": {
                        "type": "boolean",
                        "description": "Only active (non-suspended) users",
                        "default": False,
                    },
                    "limit": _limit_schema,
                    "offset": _offset_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="list_onepassword_groups",
            description=(
                "List the 1Password groups managed through the SCIM bridge, "
                "with ids and member counts. Use "
                "manage_onepassword_provisioning (get_group) for a group's "
                "member list."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Match against group names",
                        "default": "",
                    },
                    "limit": _limit_schema,
                },
                "required": [],
            },
            source=_SOURCE,
            priority=0,
        ),
        ToolDefinition(
            name="manage_onepassword_provisioning",
            description=(
                "Manage 1Password users and groups through the SCIM bridge. "
                "Actions: get_user, create_user (email [+ given_name/"
                "family_name] - sends the invite), update_user_name, "
                "deactivate_user (SUSPENDS the account - 1Password's "
                "deprovisioning; there is no user hard-delete), "
                "activate_user (reactivate a suspended account), get_group, "
                "create_group (name), rename_group (group + name), "
                "add_group_member / remove_group_member (group + user), "
                "delete_group (PERMANENT). deactivate_user and delete_group "
                "are disruptive - double-check the target first."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "The provisioning action to perform",
                        "enum": sorted(_PROVISIONING_ACTIONS),
                    },
                    "user": {
                        "type": "string",
                        "description": "User id or email (user actions)",
                        "default": "",
                    },
                    "group": {
                        "type": "string",
                        "description": "Group id or name (group actions)",
                        "default": "",
                    },
                    "email": {
                        "type": "string",
                        "description": "Email for create_user",
                        "default": "",
                    },
                    "given_name": {
                        "type": "string",
                        "description": "Given name (create_user / update_user_name)",
                        "default": "",
                    },
                    "family_name": {
                        "type": "string",
                        "description": "Family name (create_user / update_user_name)",
                        "default": "",
                    },
                    "name": {
                        "type": "string",
                        "description": "Group name (create_group / rename_group)",
                        "default": "",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
            priority=2,
        ),
    ]
