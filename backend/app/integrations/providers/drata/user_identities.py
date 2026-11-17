"""Drata Version Control user identity tools (v1 API).

Drata creates one Version Control user identity per account it discovers
through each Version Control connection (GitHub, GitLab, Bitbucket, Azure
Repos, AWS CodeCommit). An identity Drata cannot match to a personnel record
stays "Not linked" with "Write access" and "Merge to default branch" off, which
fails the standard Version Control monitoring tests until someone links it.

These endpoints exist only on the **v1** API (``/user-identities/version-control``),
so the v1 host is derived from the configured v2 base URL, keeping the region.
Every endpoint requires the **Connections: Manage Version Control User
Identities** API key permission; a 403 names it.

Docs: https://developers.drata.com/openapi/reference/v1/tag/User-Identities/
"""

from __future__ import annotations

import re
from typing import Any

from app.core.egress import egress_client
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import _coerce_bool, _coerce_int, _coerce_str, _full_name

#: The API key permission every Version Control user identity endpoint requires.
_VC_IDENTITY_PERMISSION = "Connections: Manage Version Control User Identities"

#: ``clientType`` values the v1 list endpoint accepts.
_VC_CLIENT_TYPES: tuple[str, ...] = (
    "BITBUCKET",
    "GITHUB",
    "GITHUB_ENTERPRISE",
    "GITLAB",
    "GITLAB_ON_PREM",
    "AZURE_REPOS",
    "AZURE_REPOS_ENTRA",
    "AWS_CODECOMMIT",
)

#: Tool-facing setting name -> v1 ``toggleField`` value. The Drata UI labels
#: them "Write access" and "Merge to default branch"; the raw enum values are
#: accepted too.
_VC_TOGGLE_FIELDS: dict[str, str] = {
    "write_access": "VERSION_CONTROL_WRITE_ACCESS",
    "push_production_code_access": "VERSION_CONTROL_PUSH_PROD_CODE_ACCESS",
}

_LINK_STATUSES: tuple[str, ...] = ("linked", "not_linked")

#: Drata v1 pages hold at most 50 identities; fetching more than this many is
#: a sign the caller should narrow by client type or connection instead.
_MAX_IDENTITIES = 1000

#: Longest service-account reason the tool sends. The v1 spec allows 30000
#: characters, but the reason is read back on every listing of the identity.
_MAX_REASON = 1000

#: Control characters (newlines included) in a third-party string: collapsed so
#: one identity stays on one line and cannot fake another one.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


def _clean(value: Any, limit: int = 100) -> str:
    """A string from Drata or the Version Control provider, on one line and capped.

    Usernames, emails, connection aliases and reasons are set by third parties;
    control characters are collapsed to a space and the text is cut at *limit*.
    """
    if value is None:
        return ""
    text = " ".join(_CONTROL_RE.sub(" ", str(value)).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _flag(value: Any) -> bool:
    """Whether an identity flag is on.

    The v1 API reports ``writeAccess`` / ``pushProductionCodeAccess`` /
    ``serviceAccount`` as the timestamp the flag was turned on, ``null`` when
    it is off; a plain boolean is read as is.
    """
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def _yes_no(value: Any) -> str:
    return "yes" if _flag(value) else "no"


def _linked_user(identity: dict[str, Any]) -> dict[str, Any] | None:
    user = identity.get("user")
    return user if isinstance(user, dict) and user.get("id") is not None else None


def _fmt_identity(identity: dict[str, Any]) -> str:
    """One compact line per identity: the raw record nests the whole user and
    connection (with its workspaces), far too large for an agent's context."""
    connection = identity.get("connection") if isinstance(identity.get("connection"), dict) else {}
    conn_parts = [f"connection {_clean(connection.get('id'), 20) or '?'}"]
    account = _clean(connection.get("accountId"))
    if account:
        conn_parts.append(f"account {account}")
    alias = _clean(connection.get("clientAlias"))
    alias_part = f' "{alias}"' if alias else ""
    conn = f"{_clean(connection.get('clientType'), 40) or 'N/A'}{alias_part} ({', '.join(conn_parts)})"

    user = _linked_user(identity)
    linked = (
        f"{_clean(_full_name(user), 120)} <{_clean(user.get('email'), 254) or 'N/A'}> "
        f"(user id {_clean(user['id'], 20)})"
        if user
        else "NOT LINKED"
    )
    has_mfa = identity.get("hasMfa")
    service = _yes_no(identity.get("serviceAccount"))
    reason = _clean(identity.get("serviceAccountReason"), 200)
    if _flag(identity.get("serviceAccount")) and reason:
        service += f" ({reason})"
    return (
        f"[id {_clean(identity.get('id'), 20) or '?'}] {_clean(identity.get('username')) or 'N/A'} — {conn} | "
        f"Provider identity: {_clean(identity.get('identityId')) or 'N/A'} | "
        f"Email: {_clean(identity.get('email'), 254) or 'N/A'} | "
        f"Linked user: {linked} | "
        f"MFA: {'unknown' if has_mfa is None else _yes_no(has_mfa)} | "
        f"Write access: {_yes_no(identity.get('writeAccess'))} | "
        f"Push/merge to default branch: {_yes_no(identity.get('pushProductionCodeAccess'))} | "
        f"Service account: {service} | "
        f"Connected: {_clean(identity.get('connectedAt'), 40) or 'N/A'} | "
        f"Disconnected: {_clean(identity.get('disconnectedAt'), 40) or 'N/A'}"
    )


def _with_identity(result: str, identity: dict[str, Any]) -> str:
    """*result*, plus the identity line when Drata answered with an identity record.

    A body that is not an identity (``{"success": true}``, a partial object)
    would render as ``[id ?] ... NOT LINKED`` right after a successful link,
    telling the agent the opposite of what happened, so it is left out.
    """
    if identity.get("id") is not None:
        return f"{result}\n{_fmt_identity(identity)}"
    return result


def _positive_int(value: Any) -> int | None:
    parsed = _coerce_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata Version Control user identity tools and register their callables."""
    base_url_v1 = _common.to_v1_base_url(base_url)
    identities_url = f"{base_url_v1}/user-identities/version-control"

    async def _put_identity(identity_id: int, suffix: str, action: str, body: dict[str, Any]) -> tuple[dict, str]:
        """PUT one identity sub-resource; ``(identity, "")`` or ``({}, error)``.

        A 2xx body that is not JSON still means Drata applied the change, so it
        reads as an empty identity rather than a tool failure the agent might
        retry.
        """
        async with egress_client(timeout=30) as client:
            resp, err = await _common._request_verbatim(
                client,
                "put",
                f"{identities_url}/{identity_id}/{suffix}",
                action,
                required_permission=_VC_IDENTITY_PERMISSION,
                headers=headers,
                json=body,
            )
        if err:
            return {}, err
        data: Any = {}
        if resp is not None and resp.content:
            try:
                data = resp.json()
            except ValueError:
                data = {}
        return (data if isinstance(data, dict) else {}), ""

    async def list_drata_version_control_identities(
        client_type: str = "",
        connection_id: int = 0,
        link_status: str = "",
        search: str = "",
        limit: int = 0,
    ) -> str:
        """List Version Control user identities (v1 API), one compact line each.

        ``client_type`` narrows to one provider (GITHUB, GITLAB, ...),
        ``connection_id`` to one connection, ``search`` matches the identity
        id, username or email. ``link_status`` keeps only ``linked`` or
        ``not_linked`` identities (filtered after the fetch: the API has no
        such filter). ``limit`` caps the identities fetched (default 500).
        """
        params: dict[str, Any] = {}
        ctype = _coerce_str(client_type).strip().upper()
        if ctype:
            if ctype not in _VC_CLIENT_TYPES:
                return f"Error: client_type must be one of {', '.join(_VC_CLIENT_TYPES)}."
            params["clientType"] = ctype
        if connection_id:
            cid = _positive_int(connection_id)
            if cid is None:
                return "Error: connection_id must be a positive Drata connection ID."
            params["connectionId"] = cid
        status = _coerce_str(link_status).strip().lower()
        if status and status not in _LINK_STATUSES:
            return f"Error: link_status must be one of {', '.join(_LINK_STATUSES)} (or empty for all)."
        query = _coerce_str(search).strip()
        if query:
            params["q"] = query
        max_items = min(_positive_int(limit) or _common._DEFAULT_MAX_ITEMS, _MAX_IDENTITIES)

        async with egress_client(timeout=30) as client:
            items, err, truncated = await _common._paginated_fetch_pages_all(
                client,
                identities_url,
                headers,
                params,
                max_items=max_items,
                action="list version control user identities",
                verbatim_errors=True,
                required_permission=_VC_IDENTITY_PERMISSION,
            )
        if err:
            return err

        fetched = len(items)
        if status == "linked":
            items = [i for i in items if _linked_user(i)]
        elif status == "not_linked":
            items = [i for i in items if not _linked_user(i)]
        label = f"{status.replace('_', ' ')} version control" if status else "version control"
        if items:
            plural = "identity" if len(items) == 1 else "identities"
            result = f"Found {len(items)} {label} {plural}:\n" + "\n".join(_fmt_identity(i) for i in items)
        else:
            result = f"No {label} identities found."
        if status:
            result += f"\n\n({len(items)} of the {fetched} identities fetched are {status.replace('_', ' ')}.)"
        if truncated:
            result += (
                f"\n\n(Only the first {fetched} identities were fetched; the list may be incomplete. Narrow by "
                "client_type, connection_id or search, or raise limit.)"
            )
        return result

    async def link_drata_version_control_identity(identity_id: int, user_id: int) -> str:
        """Link a Version Control identity to a Drata user (v1 API).

        ``identity_id`` is the numeric ``id`` shown by
        ``list_drata_version_control_identities``; ``user_id`` is the Drata
        USER id (``user.id``, from ``list_drata_users``), not a personnel
        record id. A person with identities on several connections needs one
        call per identity.
        """
        iid = _positive_int(identity_id)
        if iid is None:
            return "Error: identity_id (the numeric id from list_drata_version_control_identities) is required."
        uid = _positive_int(user_id)
        if uid is None:
            return "Error: user_id (the numeric Drata user id, not a personnel id) is required."
        identity, err = await _put_identity(iid, "connect", "link version control user identity", {"userId": uid})
        if err:
            return err
        result = f"Version Control identity {iid} linked to Drata user {uid}."
        return _with_identity(result, identity)

    async def toggle_drata_version_control_identity(identity_id: int, setting: str, enabled: bool) -> str:
        """Turn a Version Control identity access flag on or off (v1 API).

        ``setting`` is ``write_access`` ("Write access") or
        ``push_production_code_access`` ("Merge to default branch").
        """
        iid = _positive_int(identity_id)
        if iid is None:
            return "Error: identity_id (the numeric id from list_drata_version_control_identities) is required."
        key = _coerce_str(setting).strip()
        field = _VC_TOGGLE_FIELDS.get(key.lower()) or (key.upper() if key.upper() in _VC_TOGGLE_FIELDS.values() else "")
        if not field:
            return f"Error: setting must be one of {', '.join(_VC_TOGGLE_FIELDS)}."
        value = _coerce_bool(enabled)
        if value is None:
            return "Error: enabled must be true or false."
        identity, err = await _put_identity(
            iid,
            "toggle",
            "update version control user identity toggle",
            {"toggle": value, "toggleField": field},
        )
        if err:
            return err
        result = f"Version Control identity {iid}: {field} turned {'on' if value else 'off'}."
        return _with_identity(result, identity)

    async def set_drata_version_control_service_account(
        identity_id: int, service_account: bool, reason: str = ""
    ) -> str:
        """Mark (or unmark) a Version Control identity as a service account (v1 API).

        ``reason`` is required when marking an identity as a service account;
        the v1 endpoint requires the field either way, so it is sent (empty)
        when unmarking too.
        """
        iid = _positive_int(identity_id)
        if iid is None:
            return "Error: identity_id (the numeric id from list_drata_version_control_identities) is required."
        value = _coerce_bool(service_account)
        if value is None:
            return "Error: service_account must be true or false."
        why = _coerce_str(reason).strip()
        if value and not why:
            return "Error: reason is required when marking an identity as a service account."
        if len(why) > _MAX_REASON:
            return f"Error: reason is longer than {_MAX_REASON} characters ({len(why)}); shorten it."
        identity, err = await _put_identity(
            iid,
            "service-account",
            "update version control user identity service account",
            {"serviceAccount": value, "serviceAccountReason": why},
        )
        if err:
            return err
        result = f"Version Control identity {iid} {'marked' if value else 'no longer marked'} as a service account."
        return _with_identity(result, identity)

    callable_map = {
        "list_drata_version_control_identities": list_drata_version_control_identities,
        "link_drata_version_control_identity": link_drata_version_control_identity,
        "toggle_drata_version_control_identity": toggle_drata_version_control_identity,
        "set_drata_version_control_service_account": set_drata_version_control_service_account,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    identity_id_schema = {
        "type": "integer",
        "description": "The identity's numeric 'id' from list_drata_version_control_identities.",
    }
    return [
        ToolDefinition(
            name="list_drata_version_control_identities",
            description=(
                "List Drata Version Control user identities (one per account per Version Control "
                "connection: GitHub, GitLab, Bitbucket, Azure Repos, AWS CodeCommit) with the linked "
                "Drata user, MFA, Write access, Push/merge to default branch and service-account "
                "state. Use link_status='not_linked' to find the identities that fail the Version "
                "Control monitoring tests. Requires the 'Connections: Manage Version Control User "
                "Identities' API key permission."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "client_type": {
                        "type": "string",
                        "enum": list(_VC_CLIENT_TYPES),
                        "description": "Only identities of this Version Control provider.",
                    },
                    "connection_id": {
                        "type": "integer",
                        "description": "Only identities of this Drata connection (more granular than client_type).",
                    },
                    "link_status": {
                        "type": "string",
                        "enum": list(_LINK_STATUSES),
                        "description": "Keep only linked or not linked identities. Omit for all.",
                    },
                    "search": {
                        "type": "string",
                        "description": "Search by provider identity id, username or email.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            f"Maximum identities fetched from Drata, before the link_status filter "
                            f"(default 500, max {_MAX_IDENTITIES})."
                        ),
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="link_drata_version_control_identity",
            description=(
                "Link a Drata Version Control user identity to a Drata user. user_id is the Drata "
                "USER id (from list_drata_users), not a personnel record id. A person with identities "
                "on several connections needs one call per identity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identity_id": identity_id_schema,
                    "user_id": {
                        "type": "integer",
                        "description": "The Drata user id (user.id) to link the identity to.",
                    },
                },
                "required": ["identity_id", "user_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="toggle_drata_version_control_identity",
            description=(
                "Turn 'Write access' (setting=write_access) or 'Merge to default branch' "
                "(setting=push_production_code_access) on or off for a Drata Version Control user identity."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identity_id": identity_id_schema,
                    "setting": {
                        "type": "string",
                        "enum": list(_VC_TOGGLE_FIELDS),
                        "description": (
                            "write_access = 'Write access'; push_production_code_access = 'Merge to default branch'."
                        ),
                    },
                    "enabled": {"type": "boolean", "description": "true turns the setting on, false off."},
                },
                "required": ["identity_id", "setting", "enabled"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="set_drata_version_control_service_account",
            description=(
                "Mark a Drata Version Control user identity as a service account (a bot or deploy "
                "account, not a person), or unmark it. A reason is required when marking."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "identity_id": identity_id_schema,
                    "service_account": {
                        "type": "boolean",
                        "description": "true marks the identity as a service account, false unmarks it.",
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            f"Why the identity is a service account (required when marking, at most "
                            f"{_MAX_REASON} characters)."
                        ),
                    },
                },
                "required": ["identity_id", "service_account"],
            },
            source="integration:drata",
            priority=2,
        ),
    ]
