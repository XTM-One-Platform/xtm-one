"""OpenCRQ administration tools: users, tenants, organizations, RBAC.

Users / permissions:
* ``list_opencrq_users`` / ``get_opencrq_user``
* ``get_opencrq_user_permissions`` - effective permissions (+ platform).

Tenants:
* ``list_opencrq_tenant_users`` / ``update_opencrq_tenant_user_profile`` /
  ``remove_opencrq_tenant_user``
* ``create_opencrq_tenant`` / ``delete_opencrq_tenants``

Organizations:
* ``create/update_opencrq_organization``, ``delete_opencrq_organizations``
* ``list_opencrq_sectors`` / ``list_opencrq_countries`` - reference data.

Tenant RBAC:
* ``list_opencrq_groups`` / ``get_opencrq_group`` + group CRUD
* ``manage_opencrq_group_membership`` / ``manage_opencrq_group_roles``
* ``list_opencrq_roles`` / ``get_opencrq_role`` + role CRUD
* ``manage_opencrq_role_capabilities`` / ``list_opencrq_capabilities``

Service accounts / platform RBAC:
* ``list_opencrq_service_accounts`` (read-only - token management is
  deliberately excluded: plaintext secrets must never flow through an LLM)
* ``get_opencrq_platform_rbac`` (read-only introspection)
"""

from __future__ import annotations

import logging
import uuid

from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import (
    TENANT_PROP,
    _clean_ids,
    _client,
    _client_only_hint,
    _grc_link,
    _one_line,
    _quarantined,
    _resolve_tenant,
    _trpc,
)

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencrq"

_REVENUE_BANDS = ["under_10m", "10m_100m", "100m_1b", "1b_10b", "10b_100b", "over_100b"]

_ESTATE_MODES = ["open", "closed"]

# MAX_SCOPE_MAPPING_IDS (organizations/constants.ts), the cap organizations.updateOrganization puts on one save's mapped/unmapped ids.
_MAX_MAPPED_ASSETS = 5000


_SCOPE_PROPS = {
    "reaction_time_min_hours": {
        "type": "number",
        "minimum": 0,
        "description": "Fastest containment time, in hours (0 or more).",
    },
    "reaction_time_likely_hours": {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": "Most likely containment time, in hours (min <= likely <= max).",
    },
    "reaction_time_max_hours": {
        "type": "number",
        "exclusiveMinimum": 0,
        "description": "Slowest containment time, in hours.",
    },
    "estate_mode": {
        "type": "string",
        "enum": _ESTATE_MODES,
        "description": (
            "'open' measures the whole estate, 'closed' only the mapped assets."
        ),
    },
}


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _reaction_time_payload(
    low: float | None, likely: float | None, high: float | None
) -> tuple[dict, str | None]:
    """The three reaction-time keys OpenCRQ takes together, or why they are refused."""
    given = [v for v in (low, likely, high) if v is not None]
    if not given:
        return {}, None
    if len(given) != 3:
        return {}, (
            "reaction_time_min_hours, reaction_time_likely_hours and "
            "reaction_time_max_hours must be provided together."
        )
    if not all(_is_number(v) for v in given):
        return {}, "The reaction times must be numbers of hours."
    if low < 0 or likely <= 0 or high <= 0:
        return {}, (
            "reaction_time_min_hours must be 0 or more; the likely and max "
            "hours must be greater than 0."
        )
    if not low <= likely <= high:
        return {}, (
            "The reaction times must satisfy min <= likely <= max "
            f"(got {low}, {likely}, {high})."
        )
    return {
        "reactionTimeMin": low,
        "reactionTimeLikely": likely,
        "reactionTimeMax": high,
    }, None


def _estate_mode(value: str | None) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    mode = value.strip().lower()
    if mode not in _ESTATE_MODES:
        return None, "estate_mode must be one of: " + ", ".join(_ESTATE_MODES) + "."
    return mode, None


def _mapped_ids(label: str, ids: list[str] | None) -> tuple[list[str], str | None]:
    unique = list(dict.fromkeys(_clean_ids(ids)))
    if len(unique) > _MAX_MAPPED_ASSETS:
        return [], (
            f"{label} holds {len(unique)} assets; a single save maps or unmaps "
            f"at most {_MAX_MAPPED_ASSETS}."
        )
    return unique, None


_MEMBERSHIP_ACTIONS = ["add", "remove"]


def _fmt_permissions(rows: list[dict]) -> list[str]:
    lines = []
    for p in rows[:60]:
        lines.append(
            f"    - {_one_line(p.get('capabilityName'), 120, '?')} via role "
            f"{_one_line(p.get('roleName'), 120, '?')} (group {_one_line(p.get('groupName'), 120, '?')})"
        )
    if len(rows) > 60:
        lines.append(f"    ... and {len(rows) - 60} more.")
    return lines


def register(
    base_url: str, headers: dict[str, str], executor: CallableToolExecutor
) -> list[ToolDefinition]:
    # ── Users & permissions ───────────────────────────────────────────────

    async def list_opencrq_users(search: str = "", tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        users, err = await _trpc(
            base_url, headers, "users.getUsers", "list users", tenant_id=tenant,
        )
        if err:
            return err
        users = users or []
        if search:
            q = search.lower()
            users = [
                u
                for u in users
                if q in (u.get("name", "") or "").lower()
                or q in (u.get("email", "") or "").lower()
            ]
        if not users:
            return "No users matched."
        shown = users[:50]
        lines = []
        for u in shown:
            entry = f"- {_one_line(u.get('name'), 120, '?')} <{_one_line(u.get('email'), 120, '?')}>"
            if u.get("title"):
                entry += f" - {_one_line(u['title'], 120)}"
            entry += f" (id: {u.get('id')})"
            lines.append(entry)
        return _quarantined(f"Users ({len(users)}, showing {len(shown)}):", "\n".join(lines), "users")

    async def get_opencrq_user(user_id: str = "", tenant_id: str = "") -> str:
        if not user_id:
            return "user_id is required."
        # One client for the whole multi-call handler (tenant resolution +
        # user + groups + roles) so the calls share a connection pool.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            user, err = await _trpc(
                base_url, headers, "users.getUser", "get user",
                input={"id": user_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not user:
                return f"User {user_id} not found."
            groups, groups_err = await _trpc(
                base_url, headers, "groups.getUserGroups", "get user groups",
                input={"userId": user_id}, tenant_id=tenant, client=client,
            )
            roles, roles_err = await _trpc(
                base_url, headers, "roles.getUserRoles", "get user roles",
                input={"userId": user_id}, tenant_id=tenant, client=client,
            )
        lines = [f"[User] {_one_line(user.get('name'), 120) or _one_line(user.get('email'), 120, '?')}"]
        lines.append(f"  ID: {user.get('id', 'N/A')}")
        notes: list[str] = []
        link = _grc_link(base_url, tenant, "user", user.get("id"))
        if link:
            lines.append(link)
        lines.append(f"  Email: {_one_line(user.get('email'), 120, '?')}")
        if user.get("title"):
            lines.append(f"  Title: {_one_line(user['title'], 120)}")
        if groups_err:
            notes.append(f"Groups: lookup failed - {groups_err}")
        else:
            names = [
                _one_line((g.get("group") or {}).get("name"), 120, "?") for g in groups or []
            ]
            lines.append(f"  Groups: {', '.join(names) if names else 'none'}")
        if roles_err:
            notes.append(f"Roles: lookup failed - {roles_err}")
        else:
            names = [_one_line(r.get("name"), 120, "?") for r in roles or []]
            lines.append(f"  Roles: {', '.join(names) if names else 'none'}")
        return _quarantined("", "\n".join(lines), "user", *notes)

    async def get_opencrq_user_permissions(
        user_id: str = "", include_platform: bool = False, tenant_id: str = ""
    ) -> str:
        if not user_id:
            return "user_id is required."
        # One client for the whole multi-call handler.
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            perms, err = await _trpc(
                base_url, headers, "capabilities.getUserEffectivePermissions",
                "get user permissions",
                input={"userId": user_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            platform_perms = None
            platform_err = None
            if include_platform:
                platform_perms, platform_err = await _trpc(
                    base_url, headers,
                    "platformCapabilities.getUserPlatformEffectivePermissions",
                    "get user platform permissions",
                    input={"userId": user_id}, tenant_id=tenant, client=client,
                )
        lines: list[str] = []
        notes: list[str] = []
        perms = perms or []
        if perms:
            lines.append(f"  Tenant permissions ({len(perms)}):")
            lines.extend(_fmt_permissions(perms))
        else:
            lines.append("  Tenant permissions: none")
        if include_platform:
            if platform_err:
                notes.append(f"Platform permissions: lookup failed - {platform_err}")
            elif platform_perms:
                lines.append(f"  Platform permissions ({len(platform_perms)}):")
                lines.extend(_fmt_permissions(platform_perms))
            else:
                lines.append("  Platform permissions: none")
        return _quarantined(
            f"Effective permissions of user {user_id}:",
            "\n".join(lines),
            "permissions (role, group and capability names)",
            *notes,
        )

    # ── Tenants ───────────────────────────────────────────────────────────

    async def list_opencrq_tenant_users(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        members, err = await _trpc(
            base_url, headers, "tenants.getTenantUsers", "list tenant users",
            input={"tenantId": tenant}, tenant_id=tenant,
        )
        if err:
            return err
        members = members or []
        if not members:
            return "No users are members of this tenant."
        lines = []
        for m in members[:60]:
            user = m.get("user") or {}
            entry = (
                f"- {_one_line(m.get('tenantName'), 120) or _one_line(user.get('name'), 120, '?')} "
                f"<{_one_line(user.get('email'), 120, '?')}>"
            )
            if m.get("tenantTitle"):
                entry += f" - {_one_line(m['tenantTitle'], 120)}"
            entry += f" (user id: {m.get('userId') or user.get('id')})"
            lines.append(entry)
        return _quarantined(
            f"Tenant {tenant} members ({len(members)}):",
            "\n".join(lines),
            "tenant members",
            f"... and {len(members) - 60} more." if len(members) > 60 else "",
        )

    async def update_opencrq_tenant_user_profile(
        user_id: str = "", name: str = "", title: str = "", tenant_id: str = ""
    ) -> str:
        if not user_id:
            return "user_id is required."
        if not name and not title:
            return "Provide name and/or title to update."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        payload: dict = {"tenantId": tenant, "userId": user_id}
        if name:
            payload["name"] = name
        if title:
            payload["title"] = title
        _, err = await _trpc(
            base_url, headers, "tenants.updateTenantUserProfile",
            "update tenant user profile", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Updated tenant profile of user {user_id}."

    async def remove_opencrq_tenant_user(
        user_id: str = "", tenant_id: str = ""
    ) -> str:
        if not user_id:
            return "user_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "tenants.removeTenantUser",
            "remove tenant user",
            input={"tenantId": tenant, "userId": user_id}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Removed user {user_id} from tenant {tenant}."

    async def create_opencrq_tenant(name: str = "") -> str:
        if not name:
            return "name is required."
        tenant_id = str(uuid.uuid4())
        _, err = await _trpc(
            base_url, headers, "tenants.createTenantsTransaction",
            "create tenant",
            input={"id": tenant_id, "name": name}, mutation=True,
        )
        if err:
            return err
        return f"Created tenant '{name}' (id: {tenant_id})."

    async def delete_opencrq_tenants(
        tenant_ids: list[str] | None = None,
    ) -> str:
        ids = _clean_ids(tenant_ids)
        if not ids:
            return "tenant_ids is required (one or more tenant UUIDs)."
        _, err = await _trpc(
            base_url, headers, "tenants.deleteTenantsTransaction",
            "delete tenants", input=ids, mutation=True,
        )
        if err:
            return err
        return f"Deleted {len(ids)} tenant(s)."

    # ── Organizations & reference data ────────────────────────────────────

    async def create_opencrq_organization(
        name: str = "",
        reaction_time_min_hours: float | None = None,
        reaction_time_likely_hours: float | None = None,
        reaction_time_max_hours: float | None = None,
        estate_mode: str | None = None,
        mapped_asset_ids: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not name:
            return "name is required."
        item: dict = {"id": str(uuid.uuid4()), "name": name}
        reaction, err = _reaction_time_payload(
            reaction_time_min_hours,
            reaction_time_likely_hours,
            reaction_time_max_hours,
        )
        if err:
            return err
        item.update(reaction)
        mode, err = _estate_mode(estate_mode)
        if err:
            return err
        if mode is not None:
            item["estateMode"] = mode
        mapped, err = _mapped_ids("mapped_asset_ids", mapped_asset_ids)
        if err:
            return err
        if mapped:
            item["mappedAssetIds"] = mapped
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        org_id = item["id"]
        _, err = await _trpc(
            base_url, headers, "organizations.createOrganizationsTransaction",
            "create organization",
            input=[item], mutation=True,
            tenant_id=tenant,
        )
        if err:
            return _client_only_hint(err)
        return (
            f"Created organization '{name}' (id: {org_id}). Use "
            "update_opencrq_organization to set sector, country, revenue "
            "band, and employee count."
        )

    async def update_opencrq_organization(
        organization_id: str = "",
        name: str = "",
        description: str | None = None,
        legal_entity_name: str | None = None,
        sector_id: str | None = None,
        country_id: str | None = None,
        revenue_band: str | None = None,
        employee_count: int | str | None = None,
        domain: str | None = None,
        reaction_time_min_hours: float | None = None,
        reaction_time_likely_hours: float | None = None,
        reaction_time_max_hours: float | None = None,
        clear_reaction_time: bool = False,
        estate_mode: str | None = None,
        mapped_asset_ids_added: list[str] | None = None,
        mapped_asset_ids_removed: list[str] | None = None,
        tenant_id: str = "",
    ) -> str:
        if not organization_id:
            return "organization_id is required."
        payload: dict = {"id": organization_id}
        if name:
            payload["name"] = name
        for key, value in (
            ("description", description),
            ("legalEntityName", legal_entity_name),
            ("sectorId", sector_id),
            ("countryId", country_id),
            ("domain", domain),
        ):
            if value is not None:
                payload[key] = value or None
        if revenue_band is not None:
            revenue_band = revenue_band.strip().lower()
            if revenue_band and revenue_band not in _REVENUE_BANDS:
                return "revenue_band must be one of: " + ", ".join(_REVENUE_BANDS) + "."
            payload["revenueBand"] = revenue_band or None
        if employee_count is not None:
            if isinstance(employee_count, str):
                employee_count = employee_count.strip()
            if employee_count == "":
                payload["employeeCount"] = None
            elif isinstance(employee_count, str) and employee_count.isdecimal():
                payload["employeeCount"] = int(employee_count)
            elif (
                isinstance(employee_count, int)
                and not isinstance(employee_count, bool)
                and employee_count >= 0
            ):
                payload["employeeCount"] = employee_count
            else:
                return (
                    "employee_count must be a non-negative integer, or an empty "
                    "string to remove it."
                )
        reaction, err = _reaction_time_payload(
            reaction_time_min_hours,
            reaction_time_likely_hours,
            reaction_time_max_hours,
        )
        if err:
            return err
        if clear_reaction_time:
            if reaction:
                return (
                    "Set the reaction times or clear_reaction_time, not both."
                )
            reaction = {
                "reactionTimeMin": None,
                "reactionTimeLikely": None,
                "reactionTimeMax": None,
            }
        payload.update(reaction)
        mode, err = _estate_mode(estate_mode)
        if err:
            return err
        if mode is not None:
            payload["estateMode"] = mode
        added, err = _mapped_ids("mapped_asset_ids_added", mapped_asset_ids_added)
        if err:
            return err
        removed, err = _mapped_ids(
            "mapped_asset_ids_removed", mapped_asset_ids_removed
        )
        if err:
            return err
        if added or removed:
            payload["mapping"] = {"added": added, "removed": removed}
        if len(payload) == 1:
            return "Provide at least one field to update."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "organizations.updateOrganization",
            "update organization", input=payload, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Updated organization {organization_id}. Read its stored fields back with get_opencrq_organization."

    async def delete_opencrq_organizations(
        organization_ids: list[str] | None = None, tenant_id: str = ""
    ) -> str:
        ids = _clean_ids(organization_ids)
        if not ids:
            return "organization_ids is required (one or more org UUIDs)."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "organizations.deleteOrganizations",
            "delete organizations", input=ids, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted {len(ids)} organization(s)."

    async def list_opencrq_sectors(search: str = "", tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        sectors, err = await _trpc(
            base_url, headers, "sectors.getSectors", "list sectors",
            tenant_id=tenant,
        )
        if err:
            return err
        sectors = sectors or []
        if search:
            q = search.lower()
            sectors = [
                s for s in sectors if q in (s.get("name", "") or "").lower()
            ]
        if not sectors:
            return "No sectors matched."
        shown = sectors[:60]
        return _quarantined(
            f"Sectors ({len(sectors)}, showing {len(shown)}):",
            "\n".join(f"- {_one_line(s.get('name'), 200, '?')} (id: {s.get('id')})" for s in shown),
            "sectors",
        )

    async def list_opencrq_countries(search: str = "", tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        countries, err = await _trpc(
            base_url, headers, "countries.getCountries", "list countries",
            tenant_id=tenant,
        )
        if err:
            return err
        countries = countries or []
        if search:
            q = search.lower()
            countries = [
                c
                for c in countries
                if q in (c.get("name", "") or "").lower()
                or q == (c.get("isoAlpha2", "") or "").lower()
                or q == (c.get("isoAlpha3", "") or "").lower()
            ]
        if not countries:
            return "No countries matched."
        shown = countries[:60]
        return _quarantined(
            f"Countries ({len(countries)}, showing {len(shown)}):",
            "\n".join(
                f"- {_one_line(c.get('name'), 120, '?')} ({_one_line(c.get('isoAlpha2'), 4, '?')}, id: {c.get('id')})"
                for c in shown
            ),
            "countries",
        )

    # ── Tenant RBAC: groups ───────────────────────────────────────────────

    async def list_opencrq_groups(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        groups, err = await _trpc(
            base_url, headers, "groups.getGroups", "list groups",
            tenant_id=tenant,
        )
        if err:
            return err
        groups = groups or []
        if not groups:
            return "No groups found in this tenant."
        return _quarantined(
            f"Groups ({len(groups)}):",
            "\n".join(f"- {_one_line(g.get('name'), 200, '?')} (id: {g.get('id')})" for g in groups[:60]),
            "groups",
        )

    async def get_opencrq_group(group_id: str = "", tenant_id: str = "") -> str:
        if not group_id:
            return "group_id is required."
        # One client for the whole multi-call handler (group + users + roles).
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            group, err = await _trpc(
                base_url, headers, "groups.getGroup", "get group",
                input={"id": group_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not group:
                return f"Group {group_id} not found."
            users, users_err = await _trpc(
                base_url, headers, "groups.getGroupUsers", "get group users",
                input={"groupId": group_id}, tenant_id=tenant, client=client,
            )
            roles, roles_err = await _trpc(
                base_url, headers, "groups.getGroupRoles", "get group roles",
                input={"groupId": group_id}, tenant_id=tenant, client=client,
            )
        lines = [f"[Group] {_one_line(group.get('name'), 200, '?')}"]
        lines.append(f"  ID: {group.get('id', 'N/A')}")
        notes: list[str] = []
        if users_err:
            notes.append(f"Members: lookup failed - {users_err}")
        else:
            members = [
                f"{_one_line((u.get('user') or {}).get('name'), 120, '?')} "
                f"<{_one_line((u.get('user') or {}).get('email'), 120, '?')}>"
                for u in users or []
            ]
            lines.append(
                f"  Members ({len(members)}): "
                + (", ".join(members[:30]) if members else "none")
            )
        if roles_err:
            notes.append(f"Roles: lookup failed - {roles_err}")
        else:
            names = [_one_line((r.get("role") or {}).get("name"), 120, "?") for r in roles or []]
            lines.append(f"  Roles: {', '.join(names) if names else 'none'}")
        return _quarantined("", "\n".join(lines), "group", *notes)

    async def create_opencrq_group(name: str = "", tenant_id: str = "") -> str:
        if not name:
            return "name is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        group_id = str(uuid.uuid4())
        _, err = await _trpc(
            base_url, headers, "groups.createGroup", "create group",
            input={"id": group_id, "name": name}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Created group '{name}' (id: {group_id})."

    async def update_opencrq_group(
        group_id: str = "", name: str = "", tenant_id: str = ""
    ) -> str:
        if not group_id or not name:
            return "group_id and name are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "groups.updateGroup", "update group",
            input={"id": group_id, "name": name}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Renamed group {group_id} to '{name}'."

    async def delete_opencrq_group(group_id: str = "", tenant_id: str = "") -> str:
        if not group_id:
            return "group_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "groups.deleteGroup", "delete group",
            input={"id": group_id}, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted group {group_id}."

    async def manage_opencrq_group_membership(
        action: str = "", group_id: str = "", user_id: str = "", tenant_id: str = ""
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _MEMBERSHIP_ACTIONS:
            return "action must be add or remove."
        if not group_id or not user_id:
            return "group_id and user_id are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        procedure = (
            "groups.addUserToGroup" if action == "add"
            else "groups.removeUserFromGroup"
        )
        _, err = await _trpc(
            base_url, headers, procedure, f"{action} group member",
            input={"groupId": group_id, "userId": user_id}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        verb = "Added" if action == "add" else "Removed"
        prep = "to" if action == "add" else "from"
        return f"{verb} user {user_id} {prep} group {group_id}."

    async def manage_opencrq_group_roles(
        action: str = "", group_id: str = "", role_id: str = "", tenant_id: str = ""
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _MEMBERSHIP_ACTIONS:
            return "action must be add or remove."
        if not group_id or not role_id:
            return "group_id and role_id are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        procedure = (
            "groups.addRoleToGroup" if action == "add"
            else "groups.removeRoleFromGroup"
        )
        _, err = await _trpc(
            base_url, headers, procedure, f"{action} group role",
            input={"groupId": group_id, "roleId": role_id}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        verb = "Added" if action == "add" else "Removed"
        prep = "to" if action == "add" else "from"
        return f"{verb} role {role_id} {prep} group {group_id}."

    # ── Tenant RBAC: roles & capabilities ─────────────────────────────────

    async def list_opencrq_roles(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        roles, err = await _trpc(
            base_url, headers, "roles.getRoles", "list roles", tenant_id=tenant,
        )
        if err:
            return err
        roles = roles or []
        if not roles:
            return "No roles found in this tenant."
        return _quarantined(
            f"Roles ({len(roles)}):",
            "\n".join(f"- {_one_line(r.get('name'), 200, '?')} (id: {r.get('id')})" for r in roles[:60]),
            "roles",
        )

    async def get_opencrq_role(role_id: str = "", tenant_id: str = "") -> str:
        if not role_id:
            return "role_id is required."
        # One client for the whole multi-call handler (role + caps + groups).
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            role, err = await _trpc(
                base_url, headers, "roles.getRole", "get role",
                input={"id": role_id}, tenant_id=tenant, client=client,
            )
            if err:
                return err
            if not role:
                return f"Role {role_id} not found."
            caps, caps_err = await _trpc(
                base_url, headers, "roles.getRoleCapabilities",
                "get role capabilities",
                input={"roleId": role_id}, tenant_id=tenant, client=client,
            )
            groups, groups_err = await _trpc(
                base_url, headers, "roles.getRoleGroups", "get role groups",
                input={"roleId": role_id}, tenant_id=tenant, client=client,
            )
        lines = [f"[Role] {_one_line(role.get('name'), 200, '?')}"]
        lines.append(f"  ID: {role.get('id', 'N/A')}")
        notes: list[str] = []
        if caps_err:
            notes.append(f"Capabilities: lookup failed - {caps_err}")
        else:
            names = [
                _one_line((c.get("capability") or {}).get("name"), 120, "?") for c in caps or []
            ]
            lines.append(
                f"  Capabilities: {', '.join(names) if names else 'none'}"
            )
        if groups_err:
            notes.append(f"Assigned to groups: lookup failed - {groups_err}")
        else:
            names = [
                _one_line((g.get("group") or {}).get("name"), 120, "?") for g in groups or []
            ]
            lines.append(
                f"  Assigned to groups: {', '.join(names) if names else 'none'}"
            )
        return _quarantined("", "\n".join(lines), "role", *notes)

    async def create_opencrq_role(name: str = "", tenant_id: str = "") -> str:
        if not name:
            return "name is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        role_id = str(uuid.uuid4())
        _, err = await _trpc(
            base_url, headers, "roles.createRole", "create role",
            input={"id": role_id, "name": name}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Created role '{name}' (id: {role_id})."

    async def update_opencrq_role(
        role_id: str = "", name: str = "", tenant_id: str = ""
    ) -> str:
        if not role_id or not name:
            return "role_id and name are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "roles.updateRole", "update role",
            input={"id": role_id, "name": name}, mutation=True,
            tenant_id=tenant,
        )
        if err:
            return err
        return f"Renamed role {role_id} to '{name}'."

    async def delete_opencrq_role(role_id: str = "", tenant_id: str = "") -> str:
        if not role_id:
            return "role_id is required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        _, err = await _trpc(
            base_url, headers, "roles.deleteRole", "delete role",
            input={"id": role_id}, mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        return f"Deleted role {role_id}."

    async def manage_opencrq_role_capabilities(
        action: str = "",
        role_id: str = "",
        capability_id: str = "",
        tenant_id: str = "",
    ) -> str:
        action = (action or "").strip().lower()
        if action not in _MEMBERSHIP_ACTIONS:
            return "action must be add or remove."
        if not role_id or not capability_id:
            return "role_id and capability_id are required."
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        procedure = (
            "roles.addCapabilityToRole" if action == "add"
            else "roles.removeCapabilityFromRole"
        )
        _, err = await _trpc(
            base_url, headers, procedure, f"{action} role capability",
            input={"roleId": role_id, "capabilityId": capability_id},
            mutation=True, tenant_id=tenant,
        )
        if err:
            return err
        verb = "Added" if action == "add" else "Removed"
        prep = "to" if action == "add" else "from"
        return f"{verb} capability {capability_id} {prep} role {role_id}."

    async def list_opencrq_capabilities(tenant_id: str = "") -> str:
        tenant, err = await _resolve_tenant(base_url, headers, tenant_id)
        if err:
            return err
        caps, err = await _trpc(
            base_url, headers, "capabilities.getCapabilities",
            "list capabilities", tenant_id=tenant,
        )
        if err:
            return err
        caps = caps or []
        if not caps:
            return "No capabilities found."
        return _quarantined(
            f"Capabilities ({len(caps)}):",
            "\n".join(f"- {_one_line(c.get('name'), 200, '?')} (id: {c.get('id')})" for c in caps[:60]),
            "capabilities",
        )

    # ── Service accounts (read-only) & platform RBAC ──────────────────────

    async def list_opencrq_service_accounts(
        service_account_id: str = "", tenant_id: str = ""
    ) -> str:
        # One client for the whole handler (list or detail multi-call).
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            if service_account_id:
                account, err = await _trpc(
                    base_url, headers, "serviceAccounts.getServiceAccount",
                    "get service account",
                    input={"id": service_account_id}, tenant_id=tenant,
                    client=client,
                )
                if err:
                    return err
                if not account:
                    return f"Service account {service_account_id} not found."
                groups, groups_err = await _trpc(
                    base_url, headers,
                    "serviceAccounts.getServiceAccountGroups",
                    "get service account groups",
                    input={"serviceAccountId": service_account_id},
                    tenant_id=tenant, client=client,
                )
                perms, perms_err = await _trpc(
                    base_url, headers,
                    "capabilities.getServiceAccountEffectivePermissions",
                    "get service account permissions",
                    input={"serviceAccountId": service_account_id},
                    tenant_id=tenant, client=client,
                )
                lines = [f"[ServiceAccount] {_one_line(account.get('name'), 200, '?')}"]
                lines.append(f"  ID: {account.get('id', 'N/A')}")
                notes: list[str] = []
                link = _grc_link(
                    base_url, tenant, "service_account", account.get("id")
                )
                if link:
                    lines.append(link)
                lines.append(
                    f"  Active: {'yes' if account.get('active') else 'no'}"
                )
                if account.get("description"):
                    lines.append(f"  Description: {_one_line(account['description'], 200)}")
                if groups_err:
                    notes.append(f"Groups: lookup failed - {groups_err}")
                else:
                    names = [
                        _one_line((g.get("group") or {}).get("name"), 120, "?")
                        for g in groups or []
                    ]
                    lines.append(
                        f"  Groups: {', '.join(names) if names else 'none'}"
                    )
                if perms_err:
                    notes.append(f"Permissions: lookup failed - {perms_err}")
                elif perms:
                    lines.append(f"  Effective permissions ({len(perms)}):")
                    lines.extend(_fmt_permissions(perms))
                else:
                    lines.append("  Effective permissions: none")
                return _quarantined("", "\n".join(lines), "service account", *notes)
            accounts, err = await _trpc(
                base_url, headers, "serviceAccounts.getServiceAccounts",
                "list service accounts", tenant_id=tenant, client=client,
            )
        if err:
            return err
        accounts = accounts or []
        if not accounts:
            return "No service accounts found in this tenant."
        lines = []
        for a in accounts[:40]:
            lines.append(
                f"- {_one_line(a.get('name'), 200, '?')} "
                f"({'active' if a.get('active') else 'inactive'}, "
                f"id: {a.get('id')})"
            )
        return _quarantined(f"Service accounts ({len(accounts)}):", "\n".join(lines), "service accounts")

    async def get_opencrq_platform_rbac(tenant_id: str = "") -> str:
        # One client for the whole multi-call handler (3 reads).
        async with _client(headers, timeout=30) as client:
            tenant, err = await _resolve_tenant(
                base_url, headers, tenant_id, client=client
            )
            if err:
                return err
            groups, groups_err = await _trpc(
                base_url, headers, "platformGroups.getPlatformGroups",
                "list platform groups", tenant_id=tenant, client=client,
            )
            roles, roles_err = await _trpc(
                base_url, headers, "platformRoles.getPlatformRoles",
                "list platform roles", tenant_id=tenant, client=client,
            )
            caps, caps_err = await _trpc(
                base_url, headers,
                "platformCapabilities.getPlatformCapabilities",
                "list platform capabilities", tenant_id=tenant, client=client,
            )
        lines: list[str] = []
        notes: list[str] = []
        for label, rows, rows_err in (
            ("Platform groups", groups, groups_err),
            ("Platform roles", roles, roles_err),
            ("Platform capabilities", caps, caps_err),
        ):
            if rows_err:
                notes.append(f"{label}: lookup failed - {rows_err}")
                continue
            lines.append(f"  {label} ({len(rows or [])}):")
            for row in (rows or [])[:30]:
                lines.append(f"    - {_one_line(row.get('name'), 200, '?')} (id: {row.get('id')})")
        return _quarantined(
            "Platform-level RBAC (cross-tenant, read-only):",
            "\n".join(lines),
            "platform RBAC (group, role and capability names)",
            *notes,
        )

    executor.register("list_opencrq_users", list_opencrq_users)
    executor.register("get_opencrq_user", get_opencrq_user)
    executor.register("get_opencrq_user_permissions", get_opencrq_user_permissions)
    executor.register("list_opencrq_tenant_users", list_opencrq_tenant_users)
    executor.register(
        "update_opencrq_tenant_user_profile", update_opencrq_tenant_user_profile
    )
    executor.register("remove_opencrq_tenant_user", remove_opencrq_tenant_user)
    executor.register("create_opencrq_tenant", create_opencrq_tenant)
    executor.register("delete_opencrq_tenants", delete_opencrq_tenants)
    executor.register("create_opencrq_organization", create_opencrq_organization)
    executor.register("update_opencrq_organization", update_opencrq_organization)
    executor.register("delete_opencrq_organizations", delete_opencrq_organizations)
    executor.register("list_opencrq_sectors", list_opencrq_sectors)
    executor.register("list_opencrq_countries", list_opencrq_countries)
    executor.register("list_opencrq_groups", list_opencrq_groups)
    executor.register("get_opencrq_group", get_opencrq_group)
    executor.register("create_opencrq_group", create_opencrq_group)
    executor.register("update_opencrq_group", update_opencrq_group)
    executor.register("delete_opencrq_group", delete_opencrq_group)
    executor.register(
        "manage_opencrq_group_membership", manage_opencrq_group_membership
    )
    executor.register("manage_opencrq_group_roles", manage_opencrq_group_roles)
    executor.register("list_opencrq_roles", list_opencrq_roles)
    executor.register("get_opencrq_role", get_opencrq_role)
    executor.register("create_opencrq_role", create_opencrq_role)
    executor.register("update_opencrq_role", update_opencrq_role)
    executor.register("delete_opencrq_role", delete_opencrq_role)
    executor.register(
        "manage_opencrq_role_capabilities", manage_opencrq_role_capabilities
    )
    executor.register("list_opencrq_capabilities", list_opencrq_capabilities)
    executor.register(
        "list_opencrq_service_accounts", list_opencrq_service_accounts
    )
    executor.register("get_opencrq_platform_rbac", get_opencrq_platform_rbac)

    return [
        ToolDefinition(
            name="list_opencrq_users",
            description=(
                "List the OpenCRQ platform users visible to the caller, "
                "with optional text search on name / email."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_user",
            description=(
                "Get an OpenCRQ user's detail including their groups and "
                "roles in the tenant."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User UUID."},
                    **TENANT_PROP,
                },
                "required": ["user_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_user_permissions",
            description=(
                "Get an OpenCRQ user's effective permissions: every "
                "capability with the role and group granting it. Set "
                "include_platform=true to also list platform-level "
                "(cross-tenant) permissions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User UUID."},
                    "include_platform": {
                        "type": "boolean",
                        "default": False,
                    },
                    **TENANT_PROP,
                },
                "required": ["user_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_tenant_users",
            description=(
                "List the members of an OpenCRQ tenant with their "
                "tenant-scoped display names and titles."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_tenant_user_profile",
            description=(
                "Update a user's tenant-scoped profile (display name, "
                "title) in OpenCRQ. Updating another user requires the "
                "Manage Security capability. Confirm with the user before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User UUID."},
                    "name": {"type": "string"},
                    "title": {"type": "string"},
                    **TENANT_PROP,
                },
                "required": ["user_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="remove_opencrq_tenant_user",
            description=(
                "Remove a user from an OpenCRQ tenant (their account is "
                "kept, only the membership is removed). This is a "
                "destructive access change - explicitly confirm the exact "
                "user with the user before removing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "string", "description": "User UUID."},
                    **TENANT_PROP,
                },
                "required": ["user_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_tenant",
            description=(
                "Create a new OpenCRQ tenant (requires the platform-level "
                "Create Tenants capability). Seeds the tenant with default "
                "roles and groups. Confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tenant name."},
                },
                "required": ["name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_tenants",
            description=(
                "Delete one or more OpenCRQ tenants (bulk, requires the "
                "platform-level Create Tenants capability). This is "
                "irreversible and destroys ALL tenant data (organizations, "
                "assessments, risk scenarios, assets) - explicitly confirm "
                "the exact tenants with the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "tenant_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tenant UUIDs to delete.",
                    },
                },
                "required": ["tenant_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_organization",
            description=(
                "Create an organization (risk scope) in an OpenCRQ tenant. "
                "Needs a user session (a service account is refused). "
                "Confirm with the user before creating; use "
                "update_opencrq_organization to set sector, country, revenue "
                "band, and employee count. The reaction times are in hours "
                "and feed the CRQ; give all three or none. Mapping an asset "
                "the caller cannot see is refused."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Organization name."},
                    **_SCOPE_PROPS,
                    "mapped_asset_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Supporting asset UUIDs to map into the scope "
                            f"(at most {_MAX_MAPPED_ASSETS}); they only count "
                            "when estate_mode is 'closed'."
                        ),
                    },
                    **TENANT_PROP,
                },
                "required": ["name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_organization",
            description=(
                "Update an OpenCRQ organization: name, description, legal "
                "entity name, sector, country, revenue band, employee count, "
                "domain, reaction time, estate mode, mapped assets. Fields you "
                "omit are left unchanged; an empty string removes an optional "
                "field. The reaction times are in hours and feed the CRQ: give "
                "all three or none, or clear_reaction_time to remove them. "
                "Mapping an asset the caller cannot see is refused. Confirm "
                "with the user before updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_id": {
                        "type": "string",
                        "description": "Organization UUID.",
                    },
                    "name": {"type": "string"},
                    "description": {
                        "type": "string",
                        "description": "An empty string removes the description.",
                    },
                    "legal_entity_name": {
                        "type": "string",
                        "description": "An empty string removes the legal entity name.",
                    },
                    "sector_id": {
                        "type": "string",
                        "description": (
                            "Sector UUID (list_opencrq_sectors); an empty string "
                            "removes the sector."
                        ),
                    },
                    "country_id": {
                        "type": "string",
                        "description": (
                            "Country UUID (list_opencrq_countries); an empty string "
                            "removes the country."
                        ),
                    },
                    "revenue_band": {
                        "type": "string",
                        "enum": [*_REVENUE_BANDS, ""],
                        "description": "Annual revenue band; an empty string removes it.",
                    },
                    "employee_count": {
                        "type": ["integer", "string"],
                        "minimum": 0,
                        "description": (
                            "Number of employees, an integer or a decimal string; "
                            "an empty string removes it."
                        ),
                    },
                    "domain": {
                        "type": "string",
                        "description": "An empty string removes the domain.",
                    },
                    **_SCOPE_PROPS,
                    "clear_reaction_time": {
                        "type": "boolean",
                        "description": (
                            "True removes the three reaction times; do not "
                            "combine it with them."
                        ),
                    },
                    "mapped_asset_ids_added": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Supporting asset UUIDs to map into the scope "
                            f"(at most {_MAX_MAPPED_ASSETS} per call)."
                        ),
                    },
                    "mapped_asset_ids_removed": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Supporting asset UUIDs to unmap from the scope "
                            f"(at most {_MAX_MAPPED_ASSETS} per call)."
                        ),
                    },
                    **TENANT_PROP,
                },
                "required": ["organization_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_organizations",
            description=(
                "Delete one or more OpenCRQ organizations (bulk). This is "
                "irreversible and removes their assessments, risk "
                "scenarios, and primary assets - explicitly confirm the "
                "exact organizations with the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "organization_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Organization UUIDs to delete.",
                    },
                    **TENANT_PROP,
                },
                "required": ["organization_ids"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_sectors",
            description=(
                "List the sector reference dictionary of OpenCRQ (used "
                "when configuring organizations)."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_countries",
            description=(
                "List the country reference dictionary of OpenCRQ (used "
                "when configuring organizations). Search matches name or "
                "ISO alpha-2/alpha-3 codes."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "search": {"type": "string", "description": "Text filter."},
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_groups",
            description="List the user groups of an OpenCRQ tenant.",
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_group",
            description=(
                "Get an OpenCRQ group's detail including its members and "
                "assigned roles."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "Group UUID."},
                    **TENANT_PROP,
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_group",
            description=(
                "Create a user group in an OpenCRQ tenant. Confirm with "
                "the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Group name."},
                    **TENANT_PROP,
                },
                "required": ["name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_group",
            description=(
                "Rename an OpenCRQ group. Confirm with the user before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "Group UUID."},
                    "name": {"type": "string", "description": "New name."},
                    **TENANT_PROP,
                },
                "required": ["group_id", "name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_group",
            description=(
                "Delete an OpenCRQ group. Members lose the permissions "
                "granted through it - this is irreversible, explicitly "
                "confirm the exact group with the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "group_id": {"type": "string", "description": "Group UUID."},
                    **TENANT_PROP,
                },
                "required": ["group_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_group_membership",
            description=(
                "Add a user to or remove a user from an OpenCRQ group. "
                "This changes the user's permissions - confirm with the "
                "user before applying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _MEMBERSHIP_ACTIONS,
                    },
                    "group_id": {"type": "string", "description": "Group UUID."},
                    "user_id": {"type": "string", "description": "User UUID."},
                    **TENANT_PROP,
                },
                "required": ["action", "group_id", "user_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_group_roles",
            description=(
                "Assign a role to or remove a role from an OpenCRQ group. "
                "This changes every member's permissions - confirm with "
                "the user before applying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _MEMBERSHIP_ACTIONS,
                    },
                    "group_id": {"type": "string", "description": "Group UUID."},
                    "role_id": {"type": "string", "description": "Role UUID."},
                    **TENANT_PROP,
                },
                "required": ["action", "group_id", "role_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_roles",
            description="List the RBAC roles of an OpenCRQ tenant.",
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_role",
            description=(
                "Get an OpenCRQ role's detail including its capabilities "
                "and the groups it is assigned to."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string", "description": "Role UUID."},
                    **TENANT_PROP,
                },
                "required": ["role_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="create_opencrq_role",
            description=(
                "Create an RBAC role in an OpenCRQ tenant (attach "
                "capabilities with manage_opencrq_role_capabilities). "
                "Confirm with the user before creating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Role name."},
                    **TENANT_PROP,
                },
                "required": ["name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="update_opencrq_role",
            description=(
                "Rename an OpenCRQ role. Confirm with the user before "
                "updating."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string", "description": "Role UUID."},
                    "name": {"type": "string", "description": "New name."},
                    **TENANT_PROP,
                },
                "required": ["role_id", "name"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="delete_opencrq_role",
            description=(
                "Delete an OpenCRQ role. Groups holding it lose its "
                "capabilities - this is irreversible, explicitly confirm "
                "the exact role with the user before deleting."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "role_id": {"type": "string", "description": "Role UUID."},
                    **TENANT_PROP,
                },
                "required": ["role_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencrq_role_capabilities",
            description=(
                "Add a capability to or remove a capability from an "
                "OpenCRQ role. This changes permissions for every group "
                "holding the role - confirm with the user before applying."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": _MEMBERSHIP_ACTIONS,
                    },
                    "role_id": {"type": "string", "description": "Role UUID."},
                    "capability_id": {
                        "type": "string",
                        "description": "Capability ID (list_opencrq_capabilities).",
                    },
                    **TENANT_PROP,
                },
                "required": ["action", "role_id", "capability_id"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_capabilities",
            description=(
                "List the tenant-level RBAC capabilities available in "
                "OpenCRQ (attachable to roles)."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencrq_service_accounts",
            description=(
                "List the API service accounts of an OpenCRQ tenant "
                "(read-only). Pass service_account_id for one account's "
                "detail with its groups and effective permissions. Token "
                "management is not available through this integration."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "service_account_id": {
                        "type": "string",
                        "description": "Service account UUID for detail view.",
                    },
                    **TENANT_PROP,
                },
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencrq_platform_rbac",
            description=(
                "Get a read-only overview of OpenCRQ platform-level "
                "(cross-tenant) RBAC: platform groups, platform roles, and "
                "platform capabilities."
            ),
            input_schema={"type": "object", "properties": {**TENANT_PROP}},
            source=_SOURCE,
        ),
    ]
