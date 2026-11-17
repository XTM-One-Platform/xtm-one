# SPDX-License-Identifier: Apache-2.0

"""Doppel integration - digital risk protection / social engineering defense.

Full-fledged Doppel (doppel.com) integration covering the brand-protection
loop as a best-in-class building block for an agentic security team:

  - Alerts (the core work queue): list with queue/product/date/tag filters,
    get full detail (audit logs incl. queue-movement justification, AI
    summary, screenshot), create an alert for a suspicious URL / phone
    number, update an alert (triage queue state incl. takedown requests,
    entity state, comments, tag add/remove), manage evidence files on an
    alert (upload a chat-attached file or agent-authored text, delete by
    file name), and submit referrer logs for threat discovery
  - URL scanning: submit a URL for phishing-suspiciousness analysis and poll
    the async verdict (low/medium/high)
  - Brands: list the organization's active brands (resolve brand IDs/names),
    archive a brand
  - Protected assets: list/create/delete the URLs, handles, and identifiers
    the organization monitors (protected assets suppress alerts for
    known-good content)
  - Human Risk Management (HRM): report on phishing-simulation campaigns and
    the per-user engagement events they generate (email opens, link clicks,
    data submissions, reports)

Authentication supports both Doppel API generations:

  - V1 (static keys): ``x-api-key`` (the organization API key) plus the
    optional ``x-user-api-key`` (user-specific key - the request then runs
    with that user's RBAC role, the mode Doppel recommends for V1) and
    ``x-organization-code`` (only needed when the user belongs to multiple
    organizations). Calls target ``{base}/v1/...``.
  - V2 (OAuth 2.0 client credentials, GA 2026-07-13): a Client ID + Client
    Secret created from the Version 2 tab of the Doppel Vision API Settings
    page are exchanged at ``POST {base}/oauth/token`` (audience
    ``doppel-external``) for a bearer access token (~24h lifetime, cached
    via the shared app-token cache with expiry skew). Calls then send
    ``Authorization: Bearer ...`` and target ``{base}/v2/...`` - V2 is a
    drop-in path/auth swap with identical request/response shapes.

When both a Client ID and Client Secret are configured the provider runs in
V2 mode; otherwise it falls back to the V1 static keys. The compound
``base_url||api_key||user_api_key||organization_code||client_id||client_secret``
token is assembled by ``integration_tools._get_token``; the base URL
defaults to the fixed SaaS host ``https://api.doppel.com``. Errors use the
uniform ``{"message": ...}`` envelope in both versions.
"""

from __future__ import annotations

import base64
from typing import Any
from urllib.parse import quote, urlencode

from app.core.egress import egress_client
from app.integrations.file_delivery import (
    PersistFileFn,
    deliver_file,
    link_lines,
    uploaded_file_answer,
    with_extension,
)
from app.integrations.http import (
    app_token_cache_key,
    format_entity_list,
    format_pagination_hint,
    get_cached_app_token,
    parse_compound_token,
    safe_request,
    set_cached_app_token,
)
from app.integrations.providers.social_media import fetch_public_media
from app.tools.base import CallableToolExecutor, FileContext, ToolDefinition, ToolResult

_PROVIDER = "Doppel"
_DEFAULT_TIMEOUT = 30
_DEFAULT_PAGE_SIZE = 30
_MAX_PAGE_SIZE = 200

DOPPEL_DEFAULT_BASE_URL = "https://api.doppel.com"

# V2 OAuth client-credentials exchange (POST {base}/oauth/token). The
# audience is a fixed vendor constant from the official migration guide.
_V2_TOKEN_AUDIENCE = "doppel-external"  # noqa: S105 - OAuth audience, not a secret
# Conservative fallback when the token endpoint omits expires_in (the
# documented lifetime is 24h; the shared cache applies its own expiry skew).
_V2_TOKEN_FALLBACK_TTL = 3600

# Enums from the official V1 OpenAPI spec (doppel.readme.io).
_QUEUE_STATES: tuple[str, ...] = (
    "doppel_review",
    "needs_confirmation",
    "actioned",
    "taken_down",
    "monitoring",
    "archived",
)
_ENTITY_STATES: tuple[str, ...] = (
    "active",
    "down",
    "parked",
    "suspicious",
    "unclassified",
    "unrelated",
    "related",
    "unknown",
)
_PRODUCTS: tuple[str, ...] = (
    "domains",
    "social_media",
    "mobile_apps",
    "ecommerce",
    "crypto",
    "email",
    "paid_ads",
    "telco",
    "darkweb",
)
_SORT_TYPES: tuple[str, ...] = ("date_sourced", "date_last_actioned")
_HRM_CAMPAIGN_SORT_FIELDS: tuple[str, ...] = ("launch_date", "created_at", "name")
_TAG_ACTIONS: tuple[str, ...] = ("add", "remove")
_BRAND_TYPES: tuple[str, ...] = ("organization_brand", "personal_brand")

# limit/offset pagination bounds for the brands / protected-assets / HRM
# resources (the alerts resource uses page/page_size instead).
_DEFAULT_LIMIT = 100
_MAX_LIMIT = 1000

# The create-protected-asset body accepts 1-100 brand UUIDs (OpenAPI
# ``minItems: 1, maxItems: 100``).
_MAX_BRAND_IDS = 100

# Alert evidence file management (PUT /alert with file_action + files).
_FILE_ACTIONS: tuple[str, ...] = ("upload", "delete")
# Documented upload cap: "Maximum file size is 50MB" (base64-decoded bytes).
_FILE_MAX_BYTES = 50 * 1024 * 1024
# Supported file extensions from the official V1/V2 OpenAPI spec (files with
# no extension are also accepted, so the guard only fires on a KNOWN-bad
# extension - an unknown one is left to the API's own validation).
_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".csv",
        ".docx",
        ".eml",
        ".exe",
        ".gif",
        ".html",
        ".jfif",
        ".jpg",
        ".jpeg",
        ".js",
        ".m4a",
        ".mov",
        ".mp3",
        ".mp4",
        ".msg",
        ".odt",
        ".pdf",
        ".png",
        ".pptx",
        ".rtf",
        ".snagx",
        ".tsv",
        ".txt",
        ".wav",
        ".webp",
        ".xlsx",
        ".zip",
    }
)

# Platforms accepted by the protected-assets list filter (from the official
# V1 OpenAPI spec; shown as "Type" in the Doppel dashboard).
_ASSET_PLATFORMS: tuple[str, ...] = (
    "domains",
    "twitter",
    "facebook",
    "instagram",
    "youtube",
    "tiktok",
    "linkedin",
    "reddit",
    "github",
    "telegram",
    "discord",
    "pinterest",
    "snapchat",
    "whatsapp",
    "medium",
    "tumblr",
    "twitch",
    "spotify",
    "threads",
    "bluesky",
    "truth_social",
    "weibo",
    "wechat",
    "apple_app_store",
    "google_play_store",
    "amazon",
    "ebay",
    "alibaba",
    "etsy",
    "walmart",
    "shopee",
    "facebook_marketplace",
    "coinbase",
    "binance",
    "opensea",
    "email",
    "darkweb",
    "telco",
    "google_ads",
    "facebook_ads",
    "bing_ads",
)


# ---------------------------------------------------------------------------
# Auth + URL helpers (shared with integration_testing.py)
# ---------------------------------------------------------------------------


def doppel_api_base(base_url: str) -> str:
    """Return the Doppel API host base URL (no trailing slash, no ``/v1``).

    The value is stripped first: a base URL pasted with a trailing
    newline/space would otherwise yield an invalid host. A trailing ``/v1``
    or ``/v2`` path segment (the docs present the versioned bases
    ``https://api.doppel.com/v1`` and ``.../v2``) is trimmed so a pasted URL
    can never produce a doubled ``/v1/v1/...`` path. Falls back to the fixed
    SaaS host when empty. Endpoints are appended as ``/v1/...`` or
    ``/v2/...`` depending on the active auth mode.
    """
    cleaned = (base_url or "").strip().rstrip("/")
    if cleaned.lower().endswith(("/v1", "/v2")):
        cleaned = cleaned[:-3].rstrip("/")
    return cleaned or DOPPEL_DEFAULT_BASE_URL


def build_doppel_headers(
    api_key: str,
    user_api_key: str = "",
    organization_code: str = "",
) -> dict[str, str]:
    """Build Doppel request headers (``x-api-key`` + optional user/org keys).

    The credentials are stripped: a key pasted with a trailing newline/space
    would otherwise produce header values that always fail (or that httpx
    rejects as illegal). ``x-user-api-key`` and ``x-organization-code`` are
    only sent when configured - the org-wide auth mode omits them.
    """
    headers = {
        "x-api-key": (api_key or "").strip(),
        "Accept": "application/json",
    }
    user_key = (user_api_key or "").strip()
    if user_key:
        headers["x-user-api-key"] = user_key
    org_code = (organization_code or "").strip()
    if org_code:
        headers["x-organization-code"] = org_code
    return headers


async def request_doppel_v2_token(
    base_url: str, client_id: str, client_secret: str
) -> tuple[str | None, int, str | None]:
    """Exchange V2 client credentials for a short-lived bearer access token.

    Returns ``(access_token, expires_in_seconds, None)`` on success or
    ``(None, 0, error_message)`` on failure. The token endpoint lives at the
    host root (``POST {base}/oauth/token`` - no ``/v1`` or ``/v2`` prefix)
    and takes no prior authentication.
    """
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not (client_id and client_secret):
        return None, 0, "Error: Doppel V2 client ID / client secret missing."
    url = f"{doppel_api_base(base_url)}/oauth/token"
    async with egress_client(timeout=20) as client:
        resp, err = await safe_request(
            client,
            "post",
            url,
            _PROVIDER,
            "mint V2 access token",
            json={
                "client_id": client_id,
                "client_secret": client_secret,
                "audience": _V2_TOKEN_AUDIENCE,
                "grant_type": "client_credentials",
            },
        )
        if err:
            return None, 0, err
        try:
            data = resp.json() if resp is not None else {}
        except Exception:
            data = {}
        token = data.get("access_token") if isinstance(data, dict) else None
        if not token or not isinstance(token, str):
            return None, 0, (
                "Error: Doppel token endpoint returned no access_token. "
                "Check the V2 Client ID and Client Secret."
            )
        expires_in = data.get("expires_in")
        ttl = (
            expires_in
            if isinstance(expires_in, int) and expires_in > 0
            else _V2_TOKEN_FALLBACK_TTL
        )
        return token, ttl, None


async def validate_doppel_v2_connection(
    base_url: str, client_id: str, client_secret: str
) -> tuple[bool, str]:
    """Validate V2 client credentials: token exchange + a 1-record alerts read.

    Used by the connectivity test so the UI "Test Connection" button shares
    the runtime token-exchange path (including its error guidance).
    """
    token, _ttl, err = await request_doppel_v2_token(
        base_url, client_id, client_secret
    )
    if err or not token:
        return False, err or "Error: could not obtain a Doppel V2 access token."
    url = f"{doppel_api_base(base_url)}/v2/alerts?page_size=1"
    async with egress_client(timeout=20) as client:
        _resp, probe_err = await safe_request(
            client,
            "get",
            url,
            _PROVIDER,
            "test V2 connectivity",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        if probe_err:
            return False, probe_err
    return True, "Doppel is connected (API V2, OAuth client credentials)."


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _trunc(text: Any, max_len: int = 300) -> str:
    if not text:
        return ""
    text = str(text)
    return text[:max_len] + ("..." if len(text) > max_len else "")


def _dt(value: Any) -> str:
    """Trim an ISO timestamp to second precision for display."""
    return str(value or "")[:19]


def _clamp_page_size(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_PAGE_SIZE
    return max(1, min(n, _MAX_PAGE_SIZE))


def _clamp_page(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _clamp_limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_LIMIT
    return max(1, min(n, _MAX_LIMIT))


def _clamp_offset(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _seg(value: Any) -> str:
    """Percent-encode an LLM-provided value used as a URL path segment.

    IDs are interpolated into endpoint paths; a value containing ``/``,
    ``?``, or ``#`` would otherwise rewrite the request path.
    """
    return quote(str(value or "").strip(), safe="")


def _data_rows(resp: Any) -> list[dict]:
    """Return the ``data`` list from a ``{"data": [...], "count": N}`` envelope."""
    if isinstance(resp, dict):
        data = resp.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
    return []


def _data_count(resp: Any) -> int | None:
    if isinstance(resp, dict):
        count = resp.get("count")
        if isinstance(count, int):
            return count
    return None


def _offset_hint(
    rows: list, limit: int, offset: int, total: int | None = None
) -> str:
    """Offset pagination hint when more results remain.

    When the envelope's ``count`` total is available it is authoritative:
    hint only while ``offset + len(rows) < count`` (a full page that is also
    the last page gets no hint). Otherwise fall back to the full-page
    heuristic.
    """
    if total is not None:
        if offset + len(rows) < total:
            return format_pagination_hint(
                str(offset + len(rows)), param_name="offset"
            )
        return ""
    if len(rows) >= limit:
        return format_pagination_hint(str(offset + limit), param_name="offset")
    return ""


def _queue_justification(entry: dict) -> str:
    """Extract ``metadata.queue_movement.justification`` from an audit entry.

    Present on queue-movement audit-log entries since the 2026-05-27 API
    update - it explains WHY an alert was moved between queues, which is
    exactly the context an agent needs to report on triage decisions.
    """
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return ""
    movement = metadata.get("queue_movement")
    if not isinstance(movement, dict):
        return ""
    return str(movement.get("justification") or "")


def _alert_tags(alert: dict) -> str:
    tags = alert.get("tags")
    if not isinstance(tags, list):
        return ""
    names = [
        t.get("name", "") if isinstance(t, dict) else str(t) for t in tags if t
    ]
    return ", ".join(n for n in names if n)


def _format_alert_row(alert: dict) -> str:
    """One alert as a compact multi-line summary for list output."""
    lines = [
        f"  {alert.get('id', '?')} {_trunc(alert.get('entity'), 120) or '?'}",
        (
            f"    Queue: {alert.get('queue_state', 'N/A')} | "
            f"Entity state: {alert.get('entity_state', 'N/A')} | "
            f"Severity: {alert.get('severity', 'N/A')} | "
            f"Score: {alert.get('score', 'N/A')}"
        ),
        (
            f"    Brand: {alert.get('brand', 'N/A')} | "
            f"Product: {alert.get('product', 'N/A')} | "
            f"Platform: {alert.get('platform', 'N/A')} | "
            f"Created: {_dt(alert.get('created_at'))}"
        ),
    ]
    tags = _alert_tags(alert)
    if tags:
        lines.append(f"    Tags: {tags}")
    if alert.get("doppel_link"):
        lines.append(f"    Link: {alert['doppel_link']}")
    return "\n".join(lines)


def _format_alert_detail(alert: dict) -> str:
    """Full alert detail (GET /alert and PUT /alert responses)."""
    lines = [
        f"Alert: {alert.get('id', '?')}",
        f"Entity: {alert.get('entity', 'N/A')}",
        f"Brand: {alert.get('brand', 'N/A')}",
        (
            f"Queue state: {alert.get('queue_state', 'N/A')} | "
            f"Entity state: {alert.get('entity_state', 'N/A')} | "
            f"Severity: {alert.get('severity', 'N/A')} | "
            f"Score: {alert.get('score', 'N/A')}"
        ),
        (
            f"Product: {alert.get('product', 'N/A')} | "
            f"Platform: {alert.get('platform', 'N/A')} | "
            f"Source: {alert.get('source', 'N/A')}"
        ),
        (
            f"Uploaded by: {alert.get('uploaded_by', 'N/A')} | "
            f"Assignee: {alert.get('assignee') or 'None'}"
        ),
        (
            f"Created: {_dt(alert.get('created_at'))} | "
            f"Last activity: "
            f"{_dt(alert.get('last_activity') or alert.get('last_activity_timestamp'))}"
        ),
    ]
    tags = _alert_tags(alert)
    if tags:
        lines.append(f"Tags: {tags}")
    if alert.get("alert_summary"):
        lines.append(f"AI summary: {_trunc(alert['alert_summary'], 600)}")
    if alert.get("notes"):
        lines.append(f"Latest note: {_trunc(alert['notes'], 400)}")
    entity_content = alert.get("entity_content")
    if isinstance(entity_content, dict) and entity_content:
        lines.append("Entity content:")
        for key, value in list(entity_content.items())[:5]:
            if isinstance(value, dict):
                fields = ", ".join(
                    f"{k}={v}" for k, v in list(value.items())[:8] if v
                )
                lines.append(f"  {key}: {_trunc(fields, 300)}")
            else:
                lines.append(f"  {key}: {_trunc(value, 200)}")
    audit_logs = alert.get("audit_logs")
    if isinstance(audit_logs, list) and audit_logs:
        lines.append(f"Audit log ({len(audit_logs)} entries, most recent last):")
        for entry in audit_logs[-5:]:
            if isinstance(entry, dict):
                lines.append(
                    f"  {_dt(entry.get('timestamp'))} {entry.get('type', '?')} "
                    f"-> {entry.get('value', '?')} "
                    f"(by {entry.get('changed_by') or 'Doppel'})"
                )
                justification = _queue_justification(entry)
                if justification:
                    lines.append(
                        f"    Justification: {_trunc(justification, 300)}"
                    )
    if alert.get("screenshot_url"):
        lines.append(
            f"Screenshot URL (signed, expires in 1 hour): {alert['screenshot_url']}\n"
            "  download_doppel_alert_screenshot stores it as a file."
        )
    if alert.get("doppel_link"):
        lines.append(f"Doppel Vision link: {alert['doppel_link']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool builder
# ---------------------------------------------------------------------------


def build_tools(
    token: str,
    executor: CallableToolExecutor,
    persist_file_fn: PersistFileFn | None = None,
    **_kwargs: Any,
) -> list[ToolDefinition]:
    """Build Doppel tools.

    ``token`` is the compound
    ``base_url||api_key||user_api_key||org_code||client_id||client_secret``
    assembled by ``integration_tools._get_token`` from the integration's
    stored credentials. When BOTH V2 client credentials are present the
    provider runs in V2 mode (bearer auth + ``/v2/`` paths); otherwise the
    V1 static keys apply (``x-api-key`` + optional user key / org code and
    ``/v1/`` paths). base_url defaults to the SaaS host. ``persist_file_fn``
    (injected by ``integration_tools``) stores an alert screenshot as a file
    of the run.
    """
    base_url, api_key, user_api_key, organization_code, client_id, client_secret = (
        parse_compound_token(token, 6, greedy_last=True)
    )
    api = doppel_api_base(base_url)
    api_key = (api_key or "").strip()
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    use_v2 = bool(client_id and client_secret)
    api_version = "v2" if use_v2 else "v1"
    v1_headers = build_doppel_headers(api_key, user_api_key, organization_code)
    token_cache_key = app_token_cache_key(
        "doppel", api, client_id, client_secret
    )

    async def _headers() -> tuple[dict[str, str] | None, str | None]:
        """Resolve the per-request auth headers for the active API version."""
        if not use_v2:
            return v1_headers, None
        cached = get_cached_app_token(token_cache_key)
        if cached:
            access = cached
        else:
            access, ttl, err = await request_doppel_v2_token(
                api, client_id, client_secret
            )
            if err or not access:
                return None, err or "Error: could not obtain a Doppel V2 token."
            set_cached_app_token(token_cache_key, access, ttl)
        return {
            "Authorization": f"Bearer {access}",
            "Accept": "application/json",
        }, None

    async def _api(
        method: str,
        path: str,
        action: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        expected_statuses: set[int] | None = None,
    ) -> tuple[Any, str | None, int]:
        """Authenticated request helper returning ``(json, error, status)``.

        ``path`` is version-less (e.g. ``/alerts``); the active API version
        prefix (``/v1`` or ``/v2``) is applied here.
        """
        if not use_v2 and not api_key:
            return (
                None,
                (
                    "Error: Doppel credentials are not configured. Edit the "
                    "integration and set either the organization API key "
                    "(V1) or a V2 Client ID + Client Secret."
                ),
                0,
            )
        headers, auth_err = await _headers()
        if auth_err or headers is None:
            return None, auth_err or "Error: Doppel authentication failed.", 0
        url = f"{api}/{api_version}/{path.lstrip('/')}"
        if params:
            # Drop None/empty values so optional filters don't leak.
            clean = {k: v for k, v in params.items() if v not in (None, "")}
            if clean:
                url = f"{url}?{urlencode(clean, doseq=True)}"
        async with egress_client(timeout=_DEFAULT_TIMEOUT) as client:
            resp, err = await safe_request(
                client,
                method,
                url,
                _PROVIDER,
                action,
                headers=headers,
                json=json_body,
                expected_statuses=expected_statuses,
            )
            status = resp.status_code if resp is not None else 0
            if err and (
                resp is None
                or expected_statuses is None
                or status not in expected_statuses
            ):
                return None, err, status
            if resp is None or resp.status_code == 204 or not resp.content:
                return {}, None, status
            try:
                return resp.json(), None, status
            except Exception:
                return {}, None, status

    # ══════════════════════════════════════════════════════════════════════
    # ALERTS
    # ══════════════════════════════════════════════════════════════════════

    async def list_doppel_alerts(
        search_key: str = "",
        queue_state: str = "",
        product: str = "",
        created_before: str = "",
        created_after: str = "",
        last_activity_since: str = "",
        tags: str = "",
        sort_type: str = "",
        sort_order: str = "",
        page: int = 0,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> str:
        """List Doppel alerts with filtering, sorting, and pagination."""
        if queue_state and queue_state not in _QUEUE_STATES:
            return (
                f"Error: queue_state must be one of "
                f"{', '.join(_QUEUE_STATES)} (got '{queue_state}')."
            )
        if product and product not in _PRODUCTS:
            return (
                f"Error: product must be one of {', '.join(_PRODUCTS)} "
                f"(got '{product}')."
            )
        if sort_type and sort_type not in _SORT_TYPES:
            return (
                f"Error: sort_type must be one of {', '.join(_SORT_TYPES)} "
                f"(got '{sort_type}')."
            )
        if sort_order and sort_order not in ("asc", "desc"):
            return (
                f"Error: sort_order must be 'asc' or 'desc' "
                f"(got '{sort_order}')."
            )
        # The API requires sort_type and sort_order together.
        if bool(sort_type) != bool(sort_order):
            return (
                "Error: sort_type and sort_order must be provided together "
                "(or both omitted for the default date_sourced descending)."
            )
        params: dict[str, Any] = {
            "search_key": search_key,
            "queue_state": queue_state,
            "product": product,
            "created_before": created_before,
            "created_after": created_after,
            "last_activity_timestamp": last_activity_since,
            "tags": tags,
            "sort_type": sort_type,
            "sort_order": sort_order,
            "page": _clamp_page(page),
            "page_size": _clamp_page_size(page_size),
        }
        resp, err, _ = await _api("get", "/alerts", "list alerts", params=params)
        if err:
            return err
        alerts = resp.get("alerts") if isinstance(resp, dict) else None
        metadata = resp.get("metadata") if isinstance(resp, dict) else None
        metadata = metadata if isinstance(metadata, dict) else {}
        body = format_entity_list(
            alerts if isinstance(alerts, list) else [],
            _format_alert_row,
            noun="alert",
            total=metadata.get("count"),
        )
        current_page = metadata.get("page", params["page"])
        total_pages = metadata.get("total_pages")
        if (
            isinstance(current_page, int)
            and isinstance(total_pages, int)
            # Pages are zero-indexed: the last page is total_pages - 1.
            and current_page < total_pages - 1
        ):
            body += format_pagination_hint(str(current_page + 1), param_name="page")
        return body

    async def get_doppel_alert(alert_id: str = "", entity: str = "") -> str:
        """Get full detail of one Doppel alert by ID or entity value."""
        # Strip first so a whitespace-only value counts as missing in the
        # XOR guard and never reaches the API as an invalid identifier.
        alert_id = (alert_id or "").strip()
        entity = (entity or "").strip()
        if bool(alert_id) == bool(entity):
            return (
                "Error: provide exactly one of 'alert_id' (format ORG-1234) "
                "or 'entity' (the alerted URL, phone number, or email)."
            )
        params = {"id": alert_id, "entity": entity}
        resp, err, status = await _api(
            "get",
            "/alert",
            "get alert",
            params=params,
            # 404 = no alert for this id/entity - report it as a lookup
            # miss, not a generic HTTP error.
            expected_statuses={404},
        )
        if err:
            return err
        if status == 404 or not isinstance(resp, dict) or not resp:
            return f"Alert {alert_id or entity} not found."
        return _format_alert_detail(resp)

    async def download_doppel_alert_screenshot(
        alert_id: str = "", entity: str = ""
    ) -> str | ToolResult:
        """Store the screenshot of a Doppel alert as a file of the run."""
        alert_id = (alert_id or "").strip()
        entity = (entity or "").strip()
        if bool(alert_id) == bool(entity):
            return (
                "Error: provide exactly one of 'alert_id' (format ORG-1234) "
                "or 'entity' (the alerted URL, phone number, or email)."
            )
        resp, err, status = await _api(
            "get",
            "/alert",
            "get alert",
            params={"id": alert_id, "entity": entity},
            expected_statuses={404},
        )
        if err:
            return err
        if status == 404 or not isinstance(resp, dict) or not resp:
            return f"Alert {alert_id or entity} not found."
        from app.services.builtin_tools.image_assets import sniff_raster_type

        shot = str(resp.get("screenshot_url") or "")
        if not shot:
            return f"Alert {resp.get('id') or alert_id or entity} has no screenshot."
        web_link = ("Web URL", resp.get("doppel_link"))
        links = [web_link, ("Screenshot URL (signed, expires in 1 hour)", shot)]
        # The screenshot URL is signed: fetched without the Doppel credentials,
        # every hop - redirects included - under the outbound URL policy.
        fetched = await fetch_public_media(
            shot, source=_PROVIDER, max_bytes=_FILE_MAX_BYTES, what="screenshot"
        )
        if isinstance(fetched, str):
            return "\n".join([fetched, *link_lines([web_link])])
        content_type = sniff_raster_type(fetched.data)
        if content_type is None:
            return "\n".join(
                [
                    "Error: the screenshot URL answered something other than a "
                    "PNG, JPEG, GIF or WebP image - nothing was stored.",
                    *link_lines(links),
                ]
            )
        identifier = str(resp.get("id") or alert_id or "alert")
        return await deliver_file(
            data=fetched.data,
            filename=with_extension(f"doppel-{identifier}-screenshot", content_type),
            content_type=content_type,
            source=_PROVIDER,
            persist_file_fn=persist_file_fn,
            links=links,
        )

    # NOTE: "required" tool parameters still default to "" so a tool call
    # that omits them reaches the friendly validation guard instead of
    # raising TypeError inside the executor (the input_schema "required"
    # list is what tells the LLM they are mandatory).
    async def create_doppel_alert(
        entity: str = "",
        brand: str = "",
        tags: str = "",
        source: str = "",
    ) -> str:
        """Create a Doppel alert for a suspicious URL or phone number."""
        if not (entity or "").strip():
            return "Error: 'entity' (a URL or phone number) is required."
        body: dict[str, Any] = {"entity": entity.strip()}
        brand = (brand or "").strip()
        if brand:
            body["brand"] = brand
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        if tag_list:
            body["tags"] = tag_list
        source = (source or "").strip()
        if source:
            body["source"] = source
        resp, err, status = await _api(
            "post",
            "/alert",
            f"create alert for {entity}",
            json_body=body,
            # 200 = alert already exists (returned, not an error); 409 = it
            # exists under a DIFFERENT brand - surface the API's own message.
            expected_statuses={409},
        )
        if err:
            return err
        resp = resp if isinstance(resp, dict) else {}
        alert_id = resp.get("id", "?")
        link = resp.get("doppel_link", "")
        message = resp.get("message", "")
        if status == 409:
            return (
                f"Alert for {entity} already exists with a different brand "
                f"(id: {alert_id})."
                + (f" {message}" if message else "")
            )
        if status == 200:
            return (
                f"Alert for {entity} already exists (id: {alert_id})."
                + (f" Link: {link}" if link else "")
                + (f" {message}" if message else "")
            )
        return (
            f"Alert created for {entity} (id: {alert_id}). It will now go "
            f"through Doppel's triage workflow."
            + (f" Link: {link}" if link else "")
        )

    async def update_doppel_alert(
        alert_id: str = "",
        entity: str = "",
        queue_state: str = "",
        entity_state: str = "",
        comment: str = "",
        tag_action: str = "",
        tag_name: str = "",
    ) -> str:
        """Update a Doppel alert (queue state, entity state, comment, tags)."""
        # Strip first so whitespace-only values count as missing in the XOR
        # guard and never leak into query params or the request body.
        alert_id = (alert_id or "").strip()
        entity = (entity or "").strip()
        queue_state = (queue_state or "").strip()
        entity_state = (entity_state or "").strip()
        comment = (comment or "").strip()
        tag_action = (tag_action or "").strip()
        tag_name = (tag_name or "").strip()
        if bool(alert_id) == bool(entity):
            return (
                "Error: provide exactly one of 'alert_id' (format ORG-1234) "
                "or 'entity' (the alerted URL, phone number, or email)."
            )
        if queue_state and queue_state not in _QUEUE_STATES:
            return (
                f"Error: queue_state must be one of "
                f"{', '.join(_QUEUE_STATES)} (got '{queue_state}')."
            )
        if entity_state and entity_state not in _ENTITY_STATES:
            return (
                f"Error: entity_state must be one of "
                f"{', '.join(_ENTITY_STATES)} (got '{entity_state}')."
            )
        if tag_action and tag_action not in _TAG_ACTIONS:
            return (
                f"Error: tag_action must be one of {', '.join(_TAG_ACTIONS)} "
                f"(got '{tag_action}')."
            )
        # The API requires tag_action and tag_name together.
        if bool(tag_action) != bool(tag_name):
            return (
                "Error: tag_action and tag_name must be provided together."
            )
        body: dict[str, Any] = {}
        if queue_state:
            body["queue_state"] = queue_state
        if entity_state:
            body["entity_state"] = entity_state
        if comment:
            body["comment"] = comment
        if tag_action:
            body["tag_action"] = tag_action
            body["tag_name"] = tag_name
        if not body:
            return (
                "Error: provide at least one field to update (queue_state, "
                "entity_state, comment, or tag_action + tag_name)."
            )
        params = {"id": alert_id, "entity": entity}
        resp, err, _ = await _api(
            "put",
            "/alert",
            f"update alert {alert_id or entity}",
            params=params,
            json_body=body,
        )
        if err:
            return err
        if isinstance(resp, dict) and resp.get("id"):
            return "Alert updated.\n\n" + _format_alert_detail(resp)
        return f"Alert {alert_id or entity} updated."

    async def manage_doppel_alert_files(
        alert_id: str = "",
        entity: str = "",
        action: str = "",
        file_name: str = "",
        file_id: str = "",
        content_text: str = "",
        file_context: FileContext | None = None,
    ) -> str:
        """Upload or delete an evidence file on a Doppel alert."""
        alert_id = (alert_id or "").strip()
        entity = (entity or "").strip()
        action = (action or "").strip()
        file_name = (file_name or "").strip()
        file_id = (file_id or "").strip()
        if bool(alert_id) == bool(entity):
            return (
                "Error: provide exactly one of 'alert_id' (format ORG-1234) "
                "or 'entity' (the alerted URL, phone number, or email)."
            )
        if action not in _FILE_ACTIONS:
            return (
                f"Error: action must be one of {', '.join(_FILE_ACTIONS)} "
                f"(got '{action}')."
            )
        if action == "delete":
            if not file_name:
                return (
                    "Error: 'file_name' (the uploaded file to delete, as "
                    "shown on the alert) is required for action=delete."
                )
            file_entry: dict[str, Any] = {"file_name": file_name}
        else:
            # Upload: bytes come from a chat-attached file (file_id) OR from
            # agent-authored text (content_text) - exactly one source.
            content_text = content_text or ""
            if bool(file_id) == bool(content_text.strip()):
                return (
                    "Error: for action=upload provide exactly one of "
                    "'file_id' (a file attached in this conversation) or "
                    "'content_text' (text evidence authored by you)."
                )
            if file_id:
                if file_context is None:
                    return "Error: no files are available in this conversation."
                try:
                    resolved = await file_context.resolve_upload(file_id)
                except Exception as exc:  # pragma: no cover - defensive
                    return (
                        f"Error: could not read attached file "
                        f"{_trunc(file_id, 120)}: {_trunc(exc, 200)}"
                    )
                if not resolved:
                    return (
                        f"Error: file '{_trunc(file_id, 120)}' is not "
                        f"available in this conversation."
                    )
                data, attached_name, _mime = resolved
                file_name = file_name or (attached_name or "").strip()
            else:
                data = content_text.encode("utf-8")
            if not file_name:
                return "Error: 'file_name' is required for action=upload."
            # The file is named by its id: its stored name may be a third
            # party's text, which reaches the model only in the block.
            subject = f"file '{_trunc(file_id, 120)}'" if file_id else "the evidence file"
            if len(data) > _FILE_MAX_BYTES:
                return (
                    f"Error: {subject} is {len(data)} bytes "
                    f"- Doppel accepts uploads up to "
                    f"{_FILE_MAX_BYTES // 1024 // 1024} MB."
                )
            # Fail fast on a KNOWN-bad extension (files with no extension are
            # accepted by the API, so only guard when one is present).
            if "." in file_name:
                ext = "." + file_name.rsplit(".", 1)[-1].lower()
                if ext not in _FILE_EXTENSIONS:
                    return uploaded_file_answer(
                        f"Error: the extension of {subject} (its name is below) is "
                        f"not supported by Doppel. Supported: "
                        f"{', '.join(sorted(_FILE_EXTENSIONS))} (or no "
                        f"extension).",
                        target=_PROVIDER,
                        filename=file_name,
                    )
            file_entry = {
                "file_name": file_name,
                "file_to_upload": base64.b64encode(data).decode("ascii"),
            }
        params = {"id": alert_id, "entity": entity}
        body = {"file_action": action, "files": [file_entry]}
        resp, err, _ = await _api(
            "put",
            "/alert",
            f"{action} file on alert {alert_id or entity}",
            params=params,
            json_body=body,
        )
        if err:
            return err
        details: list[str] = []
        file_results = resp.get("files") if isinstance(resp, dict) else None
        if isinstance(file_results, list) and file_results:
            for r in file_results:
                if isinstance(r, dict):
                    detail = " | ".join(
                        f"{k}: {_trunc(v, 200)}"
                        for k, v in list(r.items())[:6]
                        if v not in (None, "")
                    )
                    if detail:
                        details.append(detail)
        if action == "upload":
            # Doppel's per-file results echo the name: they go in the block too.
            return uploaded_file_answer(
                f"Uploaded {subject} to alert {alert_id or entity}.",
                target=_PROVIDER,
                filename=file_name,
                details=details,
            )
        lines = [
            f"File '{file_name}' deleted from alert {alert_id or entity}.",
            *(f"  {detail}" for detail in details),
        ]
        return "\n".join(lines)

    async def submit_doppel_referrer_log(
        referrer_url: str = "",
        destination_url: str = "",
    ) -> str:
        """Submit a referrer log to Doppel for threat analysis."""
        if not (referrer_url or "").strip():
            return "Error: 'referrer_url' is required."
        body: dict[str, Any] = {"referrer_url": referrer_url.strip()}
        destination_url = (destination_url or "").strip()
        if destination_url:
            body["destination_url"] = destination_url
        resp, err, _ = await _api(
            "post",
            "/alert/referrer",
            f"submit referrer log for {referrer_url}",
            json_body=body,
        )
        if err:
            return err
        message = resp.get("message") if isinstance(resp, dict) else None
        return message or (
            f"Referrer log for {referrer_url} accepted for processing. "
            f"Doppel will analyze the URL as part of referrer log processing "
            f"(an alert is not guaranteed)."
        )

    # ══════════════════════════════════════════════════════════════════════
    # URL SCANNING
    # ══════════════════════════════════════════════════════════════════════

    async def scan_doppel_url(url: str = "", associated_urls: str = "") -> str:
        """Submit a URL to Doppel for phishing-suspiciousness analysis."""
        if not (url or "").strip():
            return "Error: 'url' is required."
        body: dict[str, Any] = {"url": url.strip()}
        assoc = [u.strip() for u in (associated_urls or "").split(",") if u.strip()]
        if assoc:
            body["associated_urls"] = assoc
        resp, err, _ = await _api(
            "post", "/scan", f"submit scan for {url}", json_body=body
        )
        if err:
            return err
        scan_id = resp.get("scan_id") if isinstance(resp, dict) else None
        if scan_id:
            return (
                f"Scan submitted for {url} (scan_id: {scan_id}). Poll "
                f"get_doppel_scan_result with this scan_id for the verdict "
                f"(analysis usually takes a short while)."
            )
        return f"Scan submitted for {url}, but no scan_id was returned."

    async def get_doppel_scan_result(scan_id: str = "") -> str:
        """Get the suspiciousness verdict of a previously submitted scan."""
        if not (scan_id or "").strip():
            return "Error: 'scan_id' is required (returned by scan_doppel_url)."
        resp, err, status = await _api(
            "get",
            "/scan/result",
            f"get scan result {scan_id}",
            params={"scan_id": scan_id.strip()},
            # 404 = no scan with this ID - report it as a lookup miss, not a
            # generic HTTP error.
            expected_statuses={404},
        )
        if err:
            return err
        if status == 404:
            return (
                f"Scan {scan_id} not found. Check the scan_id returned by "
                f"scan_doppel_url."
            )
        resp = resp if isinstance(resp, dict) else {}
        status = resp.get("status", "UNKNOWN")
        lines = [
            f"Scan {resp.get('scan_id', scan_id)}: {status}",
            f"URL: {resp.get('url', 'N/A')}",
        ]
        results = resp.get("results")
        if status == "PENDING":
            lines.append(
                "The scan is still running - poll get_doppel_scan_result "
                "again in a moment."
            )
        elif isinstance(results, list) and results:
            for r in results:
                if not isinstance(r, dict):
                    continue
                lines.append(
                    f"  Suspiciousness: {r.get('suspiciousness', 'N/A')} | "
                    f"Category: {r.get('category', 'N/A')} | "
                    f"Created: {_dt(r.get('created_at'))}"
                )
                scan_data = r.get("scan_data")
                if isinstance(scan_data, dict) and scan_data:
                    fields = ", ".join(
                        f"{k}={v}" for k, v in list(scan_data.items())[:8] if v
                    )
                    lines.append(f"    Data: {_trunc(fields, 300)}")
        else:
            lines.append("No results returned.")
        return "\n".join(lines)

    # ══════════════════════════════════════════════════════════════════════
    # BRANDS
    # ══════════════════════════════════════════════════════════════════════

    async def list_doppel_brands(
        brand_type: str = "",
        name: str = "",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List the organization's active Doppel brands."""
        if brand_type and brand_type not in _BRAND_TYPES:
            return (
                f"Error: brand_type must be one of {', '.join(_BRAND_TYPES)} "
                f"(got '{brand_type}')."
            )
        params: dict[str, Any] = {
            "brand_type": brand_type,
            "name": name,
            "limit": _clamp_limit(limit),
            "offset": _clamp_offset(offset),
        }
        resp, err, _ = await _api("get", "/brands", "list brands", params=params)
        if err:
            return err

        def _fmt(b: dict) -> str:
            return (
                f"  {b.get('name', 'Unnamed')} [{b.get('brand_type', 'N/A')}]\n"
                f"    id: {b.get('id', '?')} | "
                f"Created: {_dt(b.get('created_at'))} | "
                f"Updated: {_dt(b.get('updated_at'))}"
            )

        rows = _data_rows(resp)
        count = _data_count(resp)
        body = format_entity_list(rows, _fmt, noun="brand", total=count)
        return body + _offset_hint(
            rows, params["limit"], params["offset"], total=count
        )

    async def delete_doppel_brand(brand_id: str = "") -> str:
        """Delete (archive) a Doppel brand by its UUID."""
        if not (brand_id or "").strip():
            return "Error: 'brand_id' (the brand UUID) is required."
        resp, err, _ = await _api(
            "delete",
            f"/brands/{_seg(brand_id)}",
            f"delete brand {brand_id}",
        )
        if err:
            return err
        deleted = resp.get("id") if isinstance(resp, dict) else None
        return (
            f"Brand {deleted or brand_id} archived. Related sourcing queries, "
            f"trademarks, customer assets, and designated takedown assets "
            f"were disabled."
        )

    # ══════════════════════════════════════════════════════════════════════
    # PROTECTED ASSETS
    # ══════════════════════════════════════════════════════════════════════

    def _format_asset(a: dict) -> str:
        brand_ids = a.get("brand_ids")
        brands = (
            ", ".join(str(b) for b in brand_ids)
            if isinstance(brand_ids, list)
            else "N/A"
        )
        lines = [
            f"  {_trunc(a.get('asset_value'), 120) or '?'} "
            f"[{a.get('platform', 'N/A')}]",
            (
                f"    id: {a.get('id', '?')} | "
                f"Created: {_dt(a.get('created_at'))} | "
                f"Updated by: {a.get('updated_by') or 'unknown'}"
            ),
            f"    Brands: {_trunc(brands, 200)}",
        ]
        return "\n".join(lines)

    async def list_doppel_protected_assets(
        platform_name: str = "",
        brand_ids: str = "",
        brand_names: str = "",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List the organization's Doppel protected assets."""
        if platform_name and platform_name not in _ASSET_PLATFORMS:
            return (
                f"Error: platform_name must be one of "
                f"{', '.join(_ASSET_PLATFORMS)} (got '{platform_name}')."
            )
        params: dict[str, Any] = {
            "platform_name": platform_name,
            "brand_ids": brand_ids,
            "brand_names": brand_names,
            "limit": _clamp_limit(limit),
            "offset": _clamp_offset(offset),
        }
        resp, err, _ = await _api(
            "get", "/protected-assets", "list protected assets", params=params
        )
        if err:
            return err
        rows = _data_rows(resp)
        count = _data_count(resp)
        body = format_entity_list(
            rows, _format_asset, noun="protected asset", total=count
        )
        return body + _offset_hint(
            rows, params["limit"], params["offset"], total=count
        )

    async def create_doppel_protected_asset(
        asset_value: str = "",
        brand_ids: str = "",
    ) -> str:
        """Create a Doppel protected asset for one or more brands."""
        if not (asset_value or "").strip():
            return "Error: 'asset_value' (a URL, handle, or identifier) is required."
        ids = [b.strip() for b in (brand_ids or "").split(",") if b.strip()]
        if not ids:
            return (
                "Error: 'brand_ids' (comma-separated brand UUIDs from "
                "list_doppel_brands) is required."
            )
        if len(ids) > _MAX_BRAND_IDS:
            return (
                f"Error: 'brand_ids' accepts at most {_MAX_BRAND_IDS} brand "
                f"UUIDs (got {len(ids)})."
            )
        resp, err, _ = await _api(
            "post",
            "/protected-asset",
            f"create protected asset {asset_value}",
            json_body={"asset_value": asset_value.strip(), "brand_ids": ids},
        )
        if err:
            return err
        resp = resp if isinstance(resp, dict) else {}
        data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
        asset_id = data.get("id", "?")
        platform = data.get("platform", "N/A")
        return (
            f"Protected asset created: {asset_value} (id: {asset_id}, "
            f"platform: {platform}). Alerts matching this asset will now be "
            f"suppressed."
        )

    async def delete_doppel_protected_asset(asset_id: str = "") -> str:
        """Delete a Doppel protected asset by its UUID."""
        if not (asset_id or "").strip():
            return "Error: 'asset_id' (the protected asset UUID) is required."
        _resp, err, _ = await _api(
            "delete",
            f"/protected-asset/{_seg(asset_id)}",
            f"delete protected asset {asset_id}",
        )
        if err:
            return err
        return (
            f"Protected asset {asset_id} deleted. It will no longer suppress "
            f"alerts for matching content."
        )

    # ══════════════════════════════════════════════════════════════════════
    # HUMAN RISK MANAGEMENT (phishing simulations)
    # ══════════════════════════════════════════════════════════════════════

    def _format_campaign(c: dict) -> str:
        return (
            f"  {c.get('name', 'Unnamed')} [{c.get('status', 'N/A')}]\n"
            f"    id: {c.get('id', '?')} | "
            f"Type: {c.get('campaign_type', 'N/A')} | "
            f"Test: {c.get('is_test', 'N/A')} | "
            f"Launch: {_dt(c.get('launch_date'))} | "
            f"End: {_dt(c.get('end_date'))}"
        )

    async def list_doppel_hrm_campaigns(
        status: str = "",
        campaign_type: str = "",
        launch_date_after: str = "",
        launch_date_before: str = "",
        is_test: bool = False,
        sort_by: str = "created_at",
        sort_order: str = "desc",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List Doppel HRM phishing-simulation campaigns."""
        if sort_by and sort_by not in _HRM_CAMPAIGN_SORT_FIELDS:
            return (
                f"Error: sort_by must be one of "
                f"{', '.join(_HRM_CAMPAIGN_SORT_FIELDS)} (got '{sort_by}')."
            )
        if sort_order and sort_order not in ("asc", "desc"):
            return (
                f"Error: sort_order must be 'asc' or 'desc' "
                f"(got '{sort_order}')."
            )
        params: dict[str, Any] = {
            "status": status,
            "campaign_type": campaign_type,
            "launch_date_after": launch_date_after,
            "launch_date_before": launch_date_before,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "limit": _clamp_limit(limit),
            "offset": _clamp_offset(offset),
        }
        if is_test:
            params["is_test"] = "true"
        resp, err, _ = await _api(
            "get", "/hrm/campaigns", "list HRM campaigns", params=params
        )
        if err:
            return err
        rows = _data_rows(resp)
        count = _data_count(resp)
        body = format_entity_list(
            rows, _format_campaign, noun="campaign", total=count
        )
        return body + _offset_hint(
            rows, params["limit"], params["offset"], total=count
        )

    async def get_doppel_hrm_campaign(campaign_id: str = "") -> str:
        """Get one Doppel HRM phishing-simulation campaign by ID."""
        if not (campaign_id or "").strip():
            return "Error: 'campaign_id' is required."
        resp, err, status = await _api(
            "get",
            f"/hrm/campaigns/{_seg(campaign_id)}",
            f"get HRM campaign {campaign_id}",
            # 404 = no campaign with this ID - report it as a lookup miss,
            # not a generic HTTP error.
            expected_statuses={404},
        )
        if err:
            return err
        if status == 404 or not isinstance(resp, dict) or not resp:
            return f"Campaign {campaign_id} not found."
        lines = [
            f"Campaign: {resp.get('name', 'Unnamed')}",
            f"ID: {resp.get('id', '?')}",
            (
                f"Status: {resp.get('status', 'N/A')} | "
                f"Type: {resp.get('campaign_type', 'N/A')} | "
                f"Test campaign: {resp.get('is_test', 'N/A')}"
            ),
            (
                f"Launch: {_dt(resp.get('launch_date'))} | "
                f"End: {_dt(resp.get('end_date'))} | "
                f"Created: {_dt(resp.get('created_at'))}"
            ),
        ]
        return "\n".join(lines)

    async def list_doppel_hrm_events(
        campaign_id: str = "",
        action: str = "",
        sim_user_id: str = "",
        department: str = "",
        manager_email: str = "",
        country: str = "",
        date_after: str = "",
        date_before: str = "",
        sort_order: str = "desc",
        limit: int = _DEFAULT_LIMIT,
        offset: int = 0,
    ) -> str:
        """List Doppel HRM phishing-simulation user events."""
        if sort_order and sort_order not in ("asc", "desc"):
            return (
                f"Error: sort_order must be 'asc' or 'desc' "
                f"(got '{sort_order}')."
            )
        params: dict[str, Any] = {
            "campaign_id": campaign_id,
            "action": action,
            "sim_user_id": sim_user_id,
            "department": department,
            "manager_email": manager_email,
            "country": country,
            "date_after": date_after,
            "date_before": date_before,
            "sort_order": sort_order,
            "limit": _clamp_limit(limit),
            "offset": _clamp_offset(offset),
        }
        resp, err, _ = await _api(
            "get", "/hrm/events", "list HRM events", params=params
        )
        if err:
            return err

        def _fmt(e: dict) -> str:
            user = (
                f"{e.get('user_first_name', '')} {e.get('user_last_name', '')}"
            ).strip() or e.get("user_email", "?")
            lines = [
                f"  {e.get('action', '?')} by {user} "
                f"({e.get('user_email', 'N/A')})",
                (
                    f"    id: {e.get('id', '?')} | "
                    f"Campaign: {e.get('campaign_id', 'N/A')} | "
                    f"At: {_dt(e.get('created_at'))}"
                ),
            ]
            context = " | ".join(
                part
                for part in (
                    f"Dept: {e['department']}" if e.get("department") else "",
                    f"Country: {e['country']}" if e.get("country") else "",
                    f"Device: {e['device_type']}" if e.get("device_type") else "",
                    f"Manager: {e['manager_email']}"
                    if e.get("manager_email")
                    else "",
                )
                if part
            )
            if context:
                lines.append(f"    {context}")
            return "\n".join(lines)

        rows = _data_rows(resp)
        count = _data_count(resp)
        body = format_entity_list(rows, _fmt, noun="event", total=count)
        return body + _offset_hint(
            rows, params["limit"], params["offset"], total=count
        )

    # ══════════════════════════════════════════════════════════════════════
    # REGISTER ALL TOOLS
    # ══════════════════════════════════════════════════════════════════════

    executor.register("list_doppel_alerts", list_doppel_alerts)
    executor.register("get_doppel_alert", get_doppel_alert)
    executor.register("download_doppel_alert_screenshot", download_doppel_alert_screenshot)
    executor.register("create_doppel_alert", create_doppel_alert)
    executor.register("update_doppel_alert", update_doppel_alert)
    executor.register("manage_doppel_alert_files", manage_doppel_alert_files)
    executor.register("submit_doppel_referrer_log", submit_doppel_referrer_log)
    executor.register("scan_doppel_url", scan_doppel_url)
    executor.register("get_doppel_scan_result", get_doppel_scan_result)
    executor.register("list_doppel_brands", list_doppel_brands)
    executor.register("delete_doppel_brand", delete_doppel_brand)
    executor.register(
        "list_doppel_protected_assets", list_doppel_protected_assets
    )
    executor.register(
        "create_doppel_protected_asset", create_doppel_protected_asset
    )
    executor.register(
        "delete_doppel_protected_asset", delete_doppel_protected_asset
    )
    executor.register("list_doppel_hrm_campaigns", list_doppel_hrm_campaigns)
    executor.register("get_doppel_hrm_campaign", get_doppel_hrm_campaign)
    executor.register("list_doppel_hrm_events", list_doppel_hrm_events)

    src = "integration:doppel"
    return [
        # ── Alerts ────────────────────────────────────────────────────────
        ToolDefinition(
            name="list_doppel_alerts",
            description=(
                "List Doppel digital-risk-protection alerts (phishing "
                "domains, fake social accounts, rogue mobile apps, scam ads, "
                "telco smishing, dark-web mentions). Filter by queue_state, "
                "product category, creation/activity dates, tags, or search "
                "by URL (search_key); sort by date_sourced or "
                "date_last_actioned. Returns each alert's ID (ORG-1234), "
                "entity, queue/entity state, severity, score, brand, "
                "product, platform, tags, and Doppel Vision link. Use the "
                "returned ID with get_doppel_alert or update_doppel_alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search_key": {
                        "type": "string",
                        "description": "Search alerts by URL (only URL search is supported)",
                    },
                    "queue_state": {
                        "type": "string",
                        "enum": list(_QUEUE_STATES),
                        "description": (
                            "Workflow queue filter (taken_down = Resolved in "
                            "Doppel Vision)"
                        ),
                    },
                    "product": {
                        "type": "string",
                        "enum": list(_PRODUCTS),
                        "description": "Product category filter",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "ISO 8601 date-time, e.g. 2026-01-05T13:45:30",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "ISO 8601 date-time",
                    },
                    "last_activity_since": {
                        "type": "string",
                        "description": (
                            "Only alerts with activity at or after this ISO "
                            "8601 date-time"
                        ),
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated tag names to filter by",
                    },
                    "sort_type": {
                        "type": "string",
                        "enum": list(_SORT_TYPES),
                        "description": (
                            "Sort field; must be provided together with "
                            "sort_order (default: date_sourced)"
                        ),
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": (
                            "Sort direction; must be provided together with "
                            "sort_type (default: desc)"
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "default": 0,
                        "description": "Zero-indexed page number",
                    },
                    "page_size": {
                        "type": "integer",
                        "default": _DEFAULT_PAGE_SIZE,
                        "description": f"Alerts per page (max {_MAX_PAGE_SIZE})",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_doppel_alert",
            description=(
                "Get full detail of one Doppel alert by its ID (format "
                "ORG-1234) OR by entity value (the alerted URL, phone "
                "number, or email) - provide exactly one. Returns queue and "
                "entity state, severity/score, brand, product/platform, "
                "tags, the AI-generated alert summary, entity content "
                "(domain WHOIS-style data, social profile info, ...), the "
                "audit log trail, the signed screenshot URL (1 hour), and the "
                "Doppel Vision deep link."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert ID, e.g. ACM-1234 (mutually exclusive with entity)",
                    },
                    "entity": {
                        "type": "string",
                        "description": (
                            "The alerted URL, phone number, or email "
                            "(mutually exclusive with alert_id)"
                        ),
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="download_doppel_alert_screenshot",
            workspace_ingest=True,
            description=(
                "Store the screenshot of a Doppel alert (by ID or entity - "
                "exactly one) as a file of the conversation: a download card, a "
                "file_id to forward (e.g. as takedown evidence) and the image on "
                "the result so you can see it, with the alert's Doppel Vision "
                "link and the signed screenshot URL. Doppel's API lists no other "
                "alert files: evidence is added with manage_doppel_alert_files."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert ID, e.g. ACM-1234 (mutually exclusive with entity)",
                    },
                    "entity": {
                        "type": "string",
                        "description": "The alerted URL, phone number, or email (mutually exclusive with alert_id)",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_doppel_alert",
            description=(
                "Create a Doppel Brand Protection alert for a suspicious "
                "entity (a URL or a phone number) so it enters Doppel's "
                "triage workflow. If an alert for the entity already exists "
                "it is returned instead of duplicated (or flagged when it "
                "belongs to a different brand). Fails for protected assets "
                "(e.g. google.com). brand must match an active brand in the "
                "organization (case-insensitive); tags must already exist "
                "(unknown tags are silently ignored); source must match a "
                "configured external source's API value."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "URL or phone number to alert on",
                    },
                    "brand": {
                        "type": "string",
                        "description": "Brand name to associate (must be an active brand)",
                    },
                    "tags": {
                        "type": "string",
                        "description": "Comma-separated existing tag names",
                    },
                    "source": {
                        "type": "string",
                        "description": (
                            "Custom API source identifier configured in "
                            "Doppel Vision organization settings"
                        ),
                    },
                },
                "required": ["entity"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="update_doppel_alert",
            description=(
                "Update a Doppel alert identified by alert_id (ORG-1234) OR "
                "entity - provide exactly one, plus at least one change. Set "
                "queue_state to drive the takedown workflow: 'actioned' "
                "requests enforcement/takedown, 'taken_down' marks the "
                "threat resolved, 'archived' dismisses it, 'monitoring' "
                "keeps watching without action. Set entity_state (active/"
                "down/parked/...), add a comment, or add/remove a tag "
                "(tag_action + tag_name together). Returns the updated "
                "alert detail."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert ID, e.g. ACM-1234 (mutually exclusive with entity)",
                    },
                    "entity": {
                        "type": "string",
                        "description": (
                            "The alerted URL, phone number, or email "
                            "(mutually exclusive with alert_id)"
                        ),
                    },
                    "queue_state": {
                        "type": "string",
                        "enum": list(_QUEUE_STATES),
                        "description": (
                            "New workflow queue (actioned = request "
                            "takedown, taken_down = resolved, archived = "
                            "dismissed)"
                        ),
                    },
                    "entity_state": {
                        "type": "string",
                        "enum": list(_ENTITY_STATES),
                        "description": "New live status of the tracked entity",
                    },
                    "comment": {
                        "type": "string",
                        "description": "Comment to add to the alert",
                    },
                    "tag_action": {
                        "type": "string",
                        "enum": list(_TAG_ACTIONS),
                        "description": "Provide together with tag_name",
                    },
                    "tag_name": {
                        "type": "string",
                        "description": "Tag to add or remove (with tag_action)",
                    },
                },
                "required": [],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="manage_doppel_alert_files",
            description=(
                "Upload or delete an evidence file on a Doppel alert "
                "(identified by alert_id ORG-1234 OR entity - provide "
                "exactly one). action='upload' attaches evidence: pass "
                "file_id to forward a file the user attached in this "
                "conversation byte-for-byte (screenshots, .eml samples, "
                "PDFs), or content_text for text evidence you authored "
                "(then file_name with a supported extension like .txt is "
                "required). action='delete' removes a previously uploaded "
                "file by its file_name. Max 50 MB per file. For Telco "
                "alerts an uploaded image may become the alert's canonical "
                "screenshot (deleting the file does not remove that "
                "screenshot copy)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "Alert ID, e.g. ACM-1234 (mutually exclusive with entity)",
                    },
                    "entity": {
                        "type": "string",
                        "description": (
                            "The alerted URL, phone number, or email "
                            "(mutually exclusive with alert_id)"
                        ),
                    },
                    "action": {
                        "type": "string",
                        "enum": list(_FILE_ACTIONS),
                        "description": "upload = attach a file, delete = remove one",
                    },
                    "file_name": {
                        "type": "string",
                        "description": (
                            "File name incl. extension (e.g. evidence.txt). "
                            "Required for delete and for content_text "
                            "uploads; defaults to the attachment's own name "
                            "for file_id uploads"
                        ),
                    },
                    "file_id": {
                        "type": "string",
                        "description": (
                            "ID of a file attached in this conversation to "
                            "upload byte-for-byte (mutually exclusive with "
                            "content_text)"
                        ),
                    },
                    "content_text": {
                        "type": "string",
                        "description": (
                            "Text evidence to upload as a file (mutually "
                            "exclusive with file_id)"
                        ),
                    },
                },
                "required": ["action"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="submit_doppel_referrer_log",
            description=(
                "Submit a referrer log to Doppel for threat-discovery "
                "processing: referrer_url is the URL that directed visitors "
                "to your site (a potential phishing page linking to you); "
                "optional destination_url is where the visitor landed (it is "
                "auto-added as a protected asset if unknown). Doppel "
                "analyzes the referrer as part of its referrer-log pipeline "
                "- submission does not guarantee an alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "referrer_url": {
                        "type": "string",
                        "description": (
                            "The referrer URL to analyze (must not be one of "
                            "your protected assets)"
                        ),
                    },
                    "destination_url": {
                        "type": "string",
                        "description": "Where the visitor navigated to (optional)",
                    },
                },
                "required": ["referrer_url"],
            },
            source=src,
            priority=1,
        ),
        # ── URL scanning ──────────────────────────────────────────────────
        ToolDefinition(
            name="scan_doppel_url",
            description=(
                "Submit a URL to Doppel for phishing-suspiciousness "
                "analysis. Doppel categorizes the URL as low, medium, or "
                "high suspiciousness (a high verdict can create an alert in "
                "Doppel for further triage). Returns a scan_id - the "
                "analysis is asynchronous, poll get_doppel_scan_result with "
                "it. Use this to vet a suspicious link BEFORE deciding to "
                "create_doppel_alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL to scan",
                    },
                    "associated_urls": {
                        "type": "string",
                        "description": (
                            "Comma-separated URLs associated with the main "
                            "URL (optional)"
                        ),
                    },
                },
                "required": ["url"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="get_doppel_scan_result",
            description=(
                "Get the verdict of a URL scan previously submitted with "
                "scan_doppel_url. Returns the scan status (PENDING while "
                "the analysis runs, COMPLETED when done) and, once "
                "complete, the suspiciousness rating (low/medium/high/na) "
                "per scan category with any additional scan data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "scan_id": {
                        "type": "string",
                        "description": "The scan UUID returned by scan_doppel_url",
                    },
                },
                "required": ["scan_id"],
            },
            source=src,
            priority=0,
        ),
        # ── Brands ────────────────────────────────────────────────────────
        ToolDefinition(
            name="list_doppel_brands",
            description=(
                "List the organization's active Doppel brands (archived "
                "brands are excluded). Filter by brand_type "
                "(organization_brand or personal_brand) and/or search by "
                "name (case-insensitive contains). Returns each brand's "
                "UUID, name, and type - use the UUID with "
                "create_doppel_protected_asset or delete_doppel_brand, and "
                "the name with create_doppel_alert."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "brand_type": {
                        "type": "string",
                        "enum": list(_BRAND_TYPES),
                        "description": "Filter by brand type",
                    },
                    "name": {
                        "type": "string",
                        "description": "Search by brand name (case-insensitive contains)",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LIMIT,
                        "description": f"Max results (up to {_MAX_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Results to skip for pagination",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="delete_doppel_brand",
            description=(
                "Delete (archive) a Doppel brand by its UUID (from "
                "list_doppel_brands). This soft-deletes the brand and "
                "disables its related sourcing queries, trademarks, "
                "customer assets, and designated takedown assets - Doppel "
                "stops monitoring for this brand. Destructive: only use "
                "when the user explicitly asks to remove a brand."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "brand_id": {
                        "type": "string",
                        "description": "UUID of the brand to archive",
                    },
                },
                "required": ["brand_id"],
            },
            source=src,
            priority=2,
        ),
        # ── Protected assets ──────────────────────────────────────────────
        ToolDefinition(
            name="list_doppel_protected_assets",
            description=(
                "List the organization's Doppel protected assets - the "
                "URLs, social handles, and identifiers the organization "
                "owns and monitors (matching content is suppressed from "
                "alerting). Filter by platform (shown as 'Type' in the "
                "Doppel dashboard) and/or brand IDs or brand names "
                "(comma-separated, max 100). Returns each asset's UUID, "
                "value, platform, and associated brand IDs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "platform_name": {
                        "type": "string",
                        "enum": list(_ASSET_PLATFORMS),
                        "description": "Filter by platform",
                    },
                    "brand_ids": {
                        "type": "string",
                        "description": "Comma-separated brand UUIDs (max 100)",
                    },
                    "brand_names": {
                        "type": "string",
                        "description": (
                            "Comma-separated brand names (case-insensitive, "
                            "max 100)"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LIMIT,
                        "description": f"Max results (up to {_MAX_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Results to skip for pagination",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="create_doppel_protected_asset",
            description=(
                "Create a Doppel protected asset - register a URL, handle, "
                "or identifier the organization OWNS so Doppel stops "
                "raising alerts for it (e.g. your official LinkedIn page "
                "or login domain). Requires 1-100 brand UUIDs from "
                "list_doppel_brands. The asset must be unique in the "
                "organization. Note: Doppel rate-limits this endpoint to "
                "about 5 requests per minute."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_value": {
                        "type": "string",
                        "description": (
                            "The asset value, e.g. "
                            "https://linkedin.com/company/example"
                        ),
                    },
                    "brand_ids": {
                        "type": "string",
                        "description": (
                            "Comma-separated brand UUIDs to associate (1-100)"
                        ),
                    },
                },
                "required": ["asset_value", "brand_ids"],
            },
            source=src,
            priority=1,
        ),
        ToolDefinition(
            name="delete_doppel_protected_asset",
            description=(
                "Delete a Doppel protected asset by its UUID (from "
                "list_doppel_protected_assets). The asset stops suppressing "
                "alerts for matching content, so Doppel may start alerting "
                "on it again. Destructive: only use when the user "
                "explicitly asks to remove a protected asset."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "UUID of the protected asset to delete",
                    },
                },
                "required": ["asset_id"],
            },
            source=src,
            priority=2,
        ),
        # ── Human Risk Management ─────────────────────────────────────────
        ToolDefinition(
            name="list_doppel_hrm_campaigns",
            description=(
                "List Doppel Human Risk Management (HRM) phishing-simulation "
                "campaigns. Filter by status (e.g. pending, in_progress, "
                "finished), campaign_type (e.g. simulation), or launch-date "
                "range; test campaigns are excluded unless is_test is true. "
                "Returns each campaign's ID, name, status, type, and "
                "launch/end dates - use the ID with get_doppel_hrm_campaign "
                "or list_doppel_hrm_events for the per-user engagement "
                "breakdown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "e.g. pending, in_progress, finished",
                    },
                    "campaign_type": {
                        "type": "string",
                        "description": "e.g. simulation",
                    },
                    "launch_date_after": {
                        "type": "string",
                        "description": "ISO 8601 date",
                    },
                    "launch_date_before": {
                        "type": "string",
                        "description": "ISO 8601 date",
                    },
                    "is_test": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include test campaigns",
                    },
                    "sort_by": {
                        "type": "string",
                        "enum": list(_HRM_CAMPAIGN_SORT_FIELDS),
                        "default": "created_at",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LIMIT,
                        "description": f"Max results (up to {_MAX_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Results to skip for pagination",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="get_doppel_hrm_campaign",
            description=(
                "Get one Doppel HRM phishing-simulation campaign by ID: "
                "name, status, type, test flag, and launch/end/created "
                "dates."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "Campaign ID from list_doppel_hrm_campaigns",
                    },
                },
                "required": ["campaign_id"],
            },
            source=src,
            priority=0,
        ),
        ToolDefinition(
            name="list_doppel_hrm_events",
            description=(
                "List Doppel HRM phishing-simulation user events - how "
                "employees interacted with simulated phishing (action "
                "values include email_opened, link_visited, data_submitted, "
                "email_reported). Each event includes joined user context "
                "(name, email, department, manager, country, device). "
                "Filter by campaign_id, action, sim_user_id, department, "
                "manager_email, country, or a date range. Use for security "
                "awareness reporting, e.g. who submitted data or which "
                "departments report the most."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "campaign_id": {
                        "type": "string",
                        "description": "Filter by campaign ID",
                    },
                    "action": {
                        "type": "string",
                        "description": (
                            "Event type, e.g. email_opened, link_visited, "
                            "data_submitted, email_reported"
                        ),
                    },
                    "sim_user_id": {
                        "type": "string",
                        "description": "Filter by simulation user ID",
                    },
                    "department": {
                        "type": "string",
                        "description": "Filter by user department",
                    },
                    "manager_email": {
                        "type": "string",
                        "description": "Filter by the user's manager email",
                    },
                    "country": {
                        "type": "string",
                        "description": "Filter by user country",
                    },
                    "date_after": {
                        "type": "string",
                        "description": "ISO 8601 date",
                    },
                    "date_before": {
                        "type": "string",
                        "description": "ISO 8601 date",
                    },
                    "sort_order": {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "default": "desc",
                    },
                    "limit": {
                        "type": "integer",
                        "default": _DEFAULT_LIMIT,
                        "description": f"Max results (up to {_MAX_LIMIT})",
                    },
                    "offset": {
                        "type": "integer",
                        "default": 0,
                        "description": "Results to skip for pagination",
                    },
                },
                "required": [],
            },
            source=src,
            priority=0,
        ),
    ]
