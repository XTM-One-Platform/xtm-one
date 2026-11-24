"""OpenCTI access management tools - users, groups, roles, capabilities, profile.

Consolidated, action-based tools over the core schema
(``config/schema/opencti.graphql``, ``src/domain/user.js`` / ``group.js``):

* ``manage_opencti_user``        - full CRUD + group/organization membership
* ``manage_opencti_group``       - full CRUD + role assignment
* ``manage_opencti_role``        - full CRUD + capability assignment
* ``list_opencti_capabilities``  - read the capability catalog (ids for roles)
* ``manage_opencti_profile``     - read/update the authenticated user (me/meEdit)

These are platform administration operations (not STIX knowledge) - not
draft-gated. Most require SETTINGS_SETACCESSES on the API token.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app.core.module_versions import RegisteredVersionFn, version_answer
from app.tools.base import CallableToolExecutor, ToolDefinition

from ._common import _client, _edges, _graphql

logger = logging.getLogger(__name__)

_SOURCE = "integration:opencti"
_REL_MEMBER_OF = "member-of"
_REL_HAS_ROLE = "has-role"
_REL_HAS_CAPABILITY = "has-capability"


def _unapplied(action: str, fields: dict[str, object]) -> str | None:
    """An error naming every field passed that *action* does not apply, else None.

    A field the tool accepts but the action ignores (or an argument the tool does not
    declare at all) must never end in a silent "Updated": the caller believes it was
    written. Values that are empty (``""``, ``None``, ``[]``, ``False``) count as not passed.
    """
    names = sorted(k for k, v in fields.items() if not (v is None or v is False or v in ("", [], {})))
    if not names:
        return None
    return (
        f"Error: action '{action}' does not apply {', '.join(names)}; nothing was "
        "written. Remove the field(s) or use the action that applies them."
    )


# The fields each action of manage_opencti_user / manage_opencti_group reads. Any
# other field passed is refused by name on EVERY action, reads and relation actions
# included: a list, get or delete must never answer as if an extra field applied.
_USER_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"user_id"}),
    "create": frozenset(
        {
            "name",
            "user_email",
            "password",
            "firstname",
            "lastname",
            "description",
            "account_status",
            "group_ids",
            "max_confidence",
            "confidence_overrides",
        }
    ),
    "update": frozenset(
        {
            "user_id",
            "name",
            "user_email",
            "firstname",
            "lastname",
            "description",
            "account_status",
            "max_confidence",
            "confidence_overrides",
            "inherit_confidence",
        }
    ),
    "delete": frozenset({"user_id"}),
    "add_group": frozenset({"user_id", "group_id"}),
    "remove_group": frozenset({"user_id", "group_id"}),
    "add_organization": frozenset({"user_id", "organization_id"}),
    "remove_organization": frozenset({"user_id", "organization_id"}),
}
_GROUP_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"group_id"}),
    "create": frozenset({"name", "description", "max_confidence", "confidence_overrides"}),
    "update": frozenset({"group_id", "name", "description", "max_confidence", "confidence_overrides"}),
    "delete": frozenset({"group_id"}),
    "add_role": frozenset({"group_id", "role_id"}),
    "remove_role": frozenset({"group_id", "role_id"}),
}
_ROLE_APPLIES: dict[str, frozenset[str]] = {
    "list": frozenset({"search", "limit"}),
    "get": frozenset({"role_id"}),
    "create": frozenset({"name", "description"}),
    "update": frozenset({"role_id", "name", "description"}),
    "delete": frozenset({"role_id"}),
    "add_capability": frozenset({"role_id", "capability_id"}),
    "remove_capability": frozenset({"role_id", "capability_id"}),
}
_ROLE_DEFAULT_LIMIT = 50
_PROFILE_APPLIES: dict[str, frozenset[str]] = {
    "get": frozenset(),
    "update": frozenset(
        {
            "name",
            "user_email",
            "firstname",
            "lastname",
            "description",
            "theme",
            "language",
            "unit_system",
            "new_password",
            "current_password",
        }
    ),
}
_DEFAULT_LIMIT = 20


def _refuse_unapplied(
    action: str,
    applies: dict[str, frozenset[str]],
    fields: dict[str, object],
    extra: dict[str, object],
) -> str | None:
    """The refusal of :func:`_unapplied` for every field *action* does not read.

    *fields* are the declared arguments as passed (``limit`` only when it differs
    from its default), *extra* the undeclared ones. An unknown action is left to the
    tool's own answer.
    """
    allowed = applies.get(action)
    if allowed is None:
        return None
    return _unapplied(action, {**{k: v for k, v in fields.items() if k not in allowed}, **extra})


def _confidence_level(
    max_confidence: int | None, overrides: list[dict[str, Any]] | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a confidence level ``{max_confidence, overrides}`` (OpenCTI
    ConfidenceLevelInput); answers (level, error)."""
    if max_confidence is not None and (
        isinstance(max_confidence, bool) or not isinstance(max_confidence, int) or not 0 <= max_confidence <= 100
    ):
        return None, "Error: max_confidence must be an integer from 0 to 100; nothing was written."
    clean: list[dict[str, Any]] = []
    for item in overrides or []:
        entity_type = item.get("entity_type") if isinstance(item, dict) else None
        level = item.get("max_confidence") if isinstance(item, dict) else None
        if (
            not isinstance(entity_type, str)
            or not entity_type.strip()
            or isinstance(level, bool)
            or not isinstance(level, int)
            or not 0 <= level <= 100
        ):
            return None, (
                "Error: each confidence override needs entity_type (e.g. Report) and "
                "max_confidence 0-100; nothing was written."
            )
        clean.append({"entity_type": entity_type.strip(), "max_confidence": level})
    return {"max_confidence": max_confidence, "overrides": clean}, None


def _format_level(level: dict[str, Any] | None) -> str:
    """``50 (overrides: Report 80)`` for a stored confidence level."""
    if not level:
        return "none"
    text = str(level.get("max_confidence")) if level.get("max_confidence") is not None else "unset"
    overrides = [f"{o.get('entity_type')} {o.get('max_confidence')}" for o in (level.get("overrides") or [])]
    return f"{text} (overrides: {', '.join(overrides)})" if overrides else text


#: Members a group ``get`` names; past it the answer says how many more exist.
_GROUP_MEMBERS_SHOWN = 200


def _group_member_lines(group: dict[str, Any]) -> list[str]:
    """``members: N`` then one line per member: name, id, email, service account or not.

    The count is OpenCTI's ``globalCount`` when it answers one, so a group larger
    than the page says how many members are left unnamed instead of a short count.
    """
    connection = group.get("members") or {}
    members = [(e or {}).get("node") or {} for e in connection.get("edges") or []]
    page = connection.get("pageInfo") or {}
    total = page.get("globalCount")
    total = total if isinstance(total, int) and total >= len(members) else len(members)
    lines = [f"  members: {total}"]
    for m in members:
        kind = "service account" if m.get("user_service_account") else "user"
        email = f", {m['user_email']}" if m.get("user_email") else ""
        lines.append(f"    - {m.get('name')} (id: {m.get('id')}{email}, {kind})")
    if total > len(members) or page.get("hasNextPage"):
        lines.append(
            f"    ... {max(total - len(members), 0) or 'more'} not shown "
            f"(first {len(members)} listed; the user 'get' action lists a user's groups)"
        )
    return lines


def _format_source(source: dict[str, Any] | None) -> str:
    obj = (source or {}).get("object") or {}
    kind = (source or {}).get("type") or obj.get("entity_type") or "?"
    if obj.get("name"):
        return f"{kind} '{obj.get('name')}' (id: {obj.get('id')})"
    return kind


_LEVEL_FIELDS = "max_confidence overrides { entity_type max_confidence }"
_OVERRIDES_SCHEMA = {
    "type": "array",
    "description": (
        "Per entity type max confidence (create/update), e.g. "
        "[{entity_type: Report, max_confidence: 80}]; replaces the stored list, [] clears it."
    ),
    "items": {
        "type": "object",
        "properties": {
            "entity_type": {"type": "string"},
            "max_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["entity_type", "max_confidence"],
    },
}
_EFFECTIVE_FIELDS = (
    "max_confidence overrides { entity_type max_confidence }"
    " source { type object { ... on User { entity_type id name }"
    " ... on Group { entity_type id name } } }"
)


def _edits(pairs: list[tuple[str, object]]) -> list[dict]:
    """Build an [EditInput] list from (key, value) pairs, skipping None values."""
    out: list[dict] = []
    for key, value in pairs:
        if value is None:
            continue
        out.append({"key": key, "value": value if isinstance(value, list) else [value]})
    return out


def register(
    base_url: str,
    headers: dict[str, str],
    executor: CallableToolExecutor,
    *,
    registered_version_fn: RegisteredVersionFn | None = None,
) -> list[ToolDefinition]:
    async def _gql(query: str, variables: dict | None = None) -> tuple[dict, str | None]:
        async with _client(headers, timeout=30) as client:
            return await _graphql(client, base_url, headers, query, variables)

    # ── Users ──────────────────────────────────────────────────────────
    async def manage_opencti_user(
        action: str,
        user_id: str = "",
        name: str = "",
        user_email: str = "",
        password: str = "",
        firstname: str = "",
        lastname: str = "",
        description: str = "",
        account_status: str = "",
        group_ids: list[str] | None = None,
        group_id: str = "",
        organization_id: str = "",
        search: str = "",
        limit: int = 20,
        max_confidence: int | None = None,
        confidence_overrides: list[dict[str, Any]] | None = None,
        inherit_confidence: bool = False,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _USER_APPLIES,
            {
                "user_id": user_id,
                "name": name,
                "user_email": user_email,
                "password": password,
                "firstname": firstname,
                "lastname": lastname,
                "description": description,
                "account_status": account_status,
                "group_ids": group_ids,
                "group_id": group_id,
                "organization_id": organization_id,
                "search": search,
                "limit": None if limit == _DEFAULT_LIMIT else limit,
                "max_confidence": max_confidence,
                "confidence_overrides": confidence_overrides,
                "inherit_confidence": inherit_confidence,
            },
            extra,
        )
        if refused:
            return refused

        async def _read_user_confidence() -> tuple[dict[str, Any] | None, str | None]:
            query = (
                "query UserConfidence($id: String!) {"
                f"  user(id: $id) {{ id name user_confidence_level {{ {_LEVEL_FIELDS} }}"
                f"    effective_confidence_level {{ {_EFFECTIVE_FIELDS} }} }} }}"
            )
            data, err = await _gql(query, {"id": user_id})
            if err:
                return None, err
            return data.get("user"), None

        def _confidence_lines(u: dict[str, Any]) -> list[str]:
            own = u.get("user_confidence_level")
            eff = u.get("effective_confidence_level") or {}
            lines = [
                "  user_confidence_level: " + (_format_level(own) if own else "inherit (from groups)"),
                f"  effective_confidence_level: {_format_level(eff) if eff else 'none'}"
                + (f", source: {_format_source(eff.get('source'))}" if eff else ""),
            ]
            return lines

        if action == "list":
            query = (
                "query Users($first: Int, $search: String) {"
                "  users(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name user_email"
                "    account_status external } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "users")
            if not nodes:
                return "No users found."
            lines = [f"Users ({len(nodes)}):"]
            for n in nodes:
                ext = " [external]" if n.get("external") else ""
                lines.append(
                    f"- {n.get('name')} <{n.get('user_email')}> "
                    f"[{n.get('account_status', '?')}]{ext} (id: {n.get('id')})"
                )
            return "\n".join(lines)

        if action == "get":
            if not user_id:
                return "user_id is required for action 'get'."
            query = (
                "query User($id: String!) {"
                "  user(id: $id) { id name user_email firstname lastname"
                "    account_status external"
                "    roles { id name } groups { edges { node { id name } } }"
                f"    user_confidence_level {{ {_LEVEL_FIELDS} }}"
                f"    effective_confidence_level {{ {_EFFECTIVE_FIELDS} }}"
                "    objectOrganization { edges { node { id name } } } } }"
            )
            data, err = await _gql(query, {"id": user_id})
            if err:
                return err
            u = data.get("user")
            if not u:
                return f"User {user_id} not found."
            roles = ", ".join(r.get("name", "") for r in (u.get("roles") or [])) or "none"
            groups = ", ".join(
                e.get("name", "") for e in _edges(u, "groups")
            ) or "none"
            orgs = ", ".join(
                e.get("name", "") for e in _edges(u, "objectOrganization")
            ) or "none"
            return "\n".join(
                [
                    f"{u.get('name')} <{u.get('user_email')}> (id: {u.get('id')})",
                    f"  name: {u.get('firstname') or ''} {u.get('lastname') or ''}".rstrip(),
                    f"  account_status: {u.get('account_status')}",
                    f"  roles: {roles}",
                    f"  groups: {groups}",
                    f"  organizations: {orgs}",
                    *_confidence_lines(u),
                ]
            )

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            inp: dict = {"name": name}
            if max_confidence is not None or confidence_overrides:
                level, level_err = _confidence_level(max_confidence, confidence_overrides)
                if level_err:
                    return level_err
                inp["user_confidence_level"] = level
            if user_email:
                inp["user_email"] = user_email
            if password:
                inp["password"] = password
            if firstname:
                inp["firstname"] = firstname
            if lastname:
                inp["lastname"] = lastname
            if description:
                inp["description"] = description
            if account_status:
                inp["account_status"] = account_status
            if group_ids:
                inp["groups"] = group_ids
            query = (
                "mutation UserAdd($input: UserAddInput!) {"
                "  userAdd(input: $input) { id name user_email } }"
            )
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            u = data.get("userAdd") or {}
            return f"Created user '{u.get('name')}' <{u.get('user_email')}> (id: {u.get('id')})."

        if action == "update":
            if not user_id:
                return "user_id is required for action 'update'."
            sets_confidence = max_confidence is not None or confidence_overrides is not None
            if inherit_confidence and sets_confidence:
                return (
                    "Error: inherit_confidence clears the user's own confidence level; "
                    "do not pass max_confidence or confidence_overrides with it. "
                    "Nothing was written."
                )
            pairs: list[tuple[str, object]] = [
                ("name", name or None),
                ("user_email", user_email or None),
                ("firstname", firstname or None),
                ("lastname", lastname or None),
                ("description", description or None),
                ("account_status", account_status or None),
            ]
            edits = _edits(pairs)
            if sets_confidence:
                overrides = confidence_overrides
                if overrides is None:
                    # Keep the overrides already stored: only max_confidence changes.
                    current, read_err = await _read_user_confidence()
                    if read_err:
                        return read_err
                    if current is None:
                        return f"User {user_id} not found."
                    overrides = (current.get("user_confidence_level") or {}).get("overrides") or []
                level, level_err = _confidence_level(max_confidence, overrides)
                if level_err:
                    return level_err
                edits.append({"key": "user_confidence_level", "value": [level]})
            if inherit_confidence:
                # OpenCTI clears the field on a [null] value (user-test.ts).
                edits.append({"key": "user_confidence_level", "value": [None]})
            if not edits:
                return "Provide at least one field to update."
            query = (
                "mutation UserPatch($id: ID!, $input: [EditInput]!) {"
                "  userEdit(id: $id) { fieldPatch(input: $input) { id name } } }"
            )
            data, err = await _gql(query, {"id": user_id, "input": edits})
            if err:
                return err
            changed = sorted({e["key"] for e in edits})
            lines = [f"Updated user {user_id} ({', '.join(changed)})."]
            if sets_confidence or inherit_confidence:
                stored, read_err = await _read_user_confidence()
                if read_err:
                    return f"{lines[0]} Read back failed: {read_err}"
                if stored:
                    lines.append("Read back from OpenCTI:")
                    lines.extend(_confidence_lines(stored))
            return "\n".join(lines)

        if action == "delete":
            if not user_id:
                return "user_id is required for action 'delete'."
            query = "mutation UserDelete($id: ID!) { userEdit(id: $id) { delete } }"
            _, err = await _gql(query, {"id": user_id})
            if err:
                return err
            return f"Deleted user {user_id}."

        if action in ("add_group", "remove_group"):
            if not user_id or not group_id:
                return f"user_id and group_id are required for action '{action}'."
            if action == "add_group":
                query = (
                    "mutation AddGroup($id: ID!, $input: InternalRelationshipAddInput!) {"
                    "  userEdit(id: $id) { relationAdd(input: $input) { id } } }"
                )
                _, err = await _gql(
                    query,
                    {
                        "id": user_id,
                        "input": {"relationship_type": _REL_MEMBER_OF, "toId": group_id},
                    },
                )
                if err:
                    return err
                return f"Added user {user_id} to group {group_id}."
            query = (
                "mutation RemoveGroup($id: ID!, $toId: StixRef!, $rt: String!) {"
                "  userEdit(id: $id) { relationDelete(toId: $toId, relationship_type: $rt)"
                "    { id } } }"
            )
            _, err = await _gql(
                query, {"id": user_id, "toId": group_id, "rt": _REL_MEMBER_OF}
            )
            if err:
                return err
            return f"Removed user {user_id} from group {group_id}."

        if action in ("add_organization", "remove_organization"):
            if not user_id or not organization_id:
                return f"user_id and organization_id are required for action '{action}'."
            field = "organizationAdd" if action == "add_organization" else "organizationDelete"
            query = (
                f"mutation OrgEdit($id: ID!, $orgId: ID!) {{"
                f"  userEdit(id: $id) {{ {field}(organizationId: $orgId) {{ id }} }} }}"
            )
            _, err = await _gql(query, {"id": user_id, "orgId": organization_id})
            if err:
                return err
            verb = "Added to" if action == "add_organization" else "Removed from"
            return f"{verb} organization {organization_id} for user {user_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, add_group, remove_group, add_organization, remove_organization."
        )

    # ── Groups ─────────────────────────────────────────────────────────
    async def manage_opencti_group(
        action: str,
        group_id: str = "",
        name: str = "",
        description: str = "",
        max_confidence: int | None = None,
        role_id: str = "",
        search: str = "",
        limit: int = 20,
        confidence_overrides: list[dict[str, Any]] | None = None,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _GROUP_APPLIES,
            {
                "group_id": group_id,
                "name": name,
                "description": description,
                "max_confidence": max_confidence,
                "role_id": role_id,
                "search": search,
                "limit": None if limit == _DEFAULT_LIMIT else limit,
                "confidence_overrides": confidence_overrides,
            },
            extra,
        )
        if refused:
            return refused

        async def _read_group_confidence() -> tuple[dict[str, Any] | None, str | None]:
            query = (
                "query GroupConfidence($id: String!) {"
                f"  group(id: $id) {{ id name group_confidence_level {{ {_LEVEL_FIELDS} }} }} }}"
            )
            data, err = await _gql(query, {"id": group_id})
            if err:
                return None, err
            return data.get("group"), None

        if action == "list":
            query = (
                "query Groups($first: Int, $search: String) {"
                "  groups(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name description } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "groups")
            if not nodes:
                return "No groups found."
            lines = [f"Groups ({len(nodes)}):"]
            for n in nodes:
                lines.append(f"- {n.get('name')} (id: {n.get('id')})")
                if n.get("description"):
                    lines.append(f"    {n['description']}")
            return "\n".join(lines)

        if action == "get":
            if not group_id:
                return "group_id is required for action 'get'."
            query = (
                "query Group($id: String!) {"
                "  group(id: $id) { id name description default_assignation"
                f"    auto_new_marking group_confidence_level {{ {_LEVEL_FIELDS} }}"
                "    roles { edges { node { id name } } }"
                f"    members(first: {_GROUP_MEMBERS_SHOWN}) {{ edges {{ node {{"
                "      id name user_email user_service_account } }"
                "      pageInfo { hasNextPage globalCount } } } }"
            )
            data, err = await _gql(query, {"id": group_id})
            if err:
                return err
            g = data.get("group")
            if not g:
                return f"Group {group_id} not found."
            roles = ", ".join(
                e.get("name", "") for e in _edges(g, "roles")
            ) or "none"
            return "\n".join(
                [
                    f"{g.get('name')} (id: {g.get('id')})",
                    f"  description: {g.get('description') or '-'}",
                    f"  default_assignation: {g.get('default_assignation')}",
                    f"  auto_new_marking: {g.get('auto_new_marking')}",
                    f"  group_confidence_level: {_format_level(g.get('group_confidence_level'))}",
                    f"  roles: {roles}",
                    *_group_member_lines(g),
                ]
            )

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            level, level_err = _confidence_level(
                100 if max_confidence is None else max_confidence, confidence_overrides
            )
            if level_err:
                return level_err
            query = (
                "mutation GroupAdd($input: GroupAddInput!) {"
                f"  groupAdd(input: $input) {{ id name group_confidence_level {{ {_LEVEL_FIELDS} }} }} }}"
            )
            inp = {
                "name": name,
                "description": description or None,
                "group_confidence_level": level,
            }
            data, err = await _gql(query, {"input": inp})
            if err:
                return err
            g = data.get("groupAdd") or {}
            return (
                f"Created group '{g.get('name')}' (id: {g.get('id')}), "
                f"group_confidence_level: {_format_level(g.get('group_confidence_level'))}."
            )

        if action == "update":
            if not group_id:
                return "group_id is required for action 'update'."
            edits = _edits(
                [("name", name or None), ("description", description or None)]
            )
            sets_confidence = max_confidence is not None or confidence_overrides is not None
            if sets_confidence:
                current, read_err = await _read_group_confidence()
                if read_err:
                    return read_err
                if current is None:
                    return f"Group {group_id} not found."
                stored = current.get("group_confidence_level") or {}
                level, level_err = _confidence_level(
                    stored.get("max_confidence") if max_confidence is None else max_confidence,
                    (stored.get("overrides") or []) if confidence_overrides is None else confidence_overrides,
                )
                if level_err:
                    return level_err
                if level and level.get("max_confidence") is None:
                    return (
                        "Error: the group has no max_confidence stored; pass max_confidence "
                        "with the overrides. Nothing was written."
                    )
                edits.append({"key": "group_confidence_level", "value": [level]})
            if not edits:
                return "Provide at least one of name, description, max_confidence or confidence_overrides to update."
            query = (
                "mutation GroupPatch($id: ID!, $input: [EditInput]!) {"
                "  groupEdit(id: $id) { fieldPatch(input: $input) { id name } } }"
            )
            _, err = await _gql(query, {"id": group_id, "input": edits})
            if err:
                return err
            changed = sorted({e["key"] for e in edits})
            message = f"Updated group {group_id} ({', '.join(changed)})."
            if sets_confidence:
                stored_group, read_err = await _read_group_confidence()
                if read_err:
                    return f"{message} Read back failed: {read_err}"
                if stored_group:
                    message += (
                        " Read back from OpenCTI: group_confidence_level "
                        f"{_format_level(stored_group.get('group_confidence_level'))}."
                    )
            return message

        if action == "delete":
            if not group_id:
                return "group_id is required for action 'delete'."
            query = "mutation GroupDelete($id: ID!) { groupEdit(id: $id) { delete } }"
            _, err = await _gql(query, {"id": group_id})
            if err:
                return err
            return f"Deleted group {group_id}."

        if action in ("add_role", "remove_role"):
            if not group_id or not role_id:
                return f"group_id and role_id are required for action '{action}'."
            if action == "add_role":
                query = (
                    "mutation AddRole($id: ID!, $input: InternalRelationshipAddInput!) {"
                    "  groupEdit(id: $id) { relationAdd(input: $input) { id } } }"
                )
                _, err = await _gql(
                    query,
                    {
                        "id": group_id,
                        "input": {"relationship_type": _REL_HAS_ROLE, "toId": role_id},
                    },
                )
                if err:
                    return err
                return f"Assigned role {role_id} to group {group_id}."
            query = (
                "mutation RemoveRole($id: ID!, $toId: StixRef!, $rt: String!) {"
                "  groupEdit(id: $id) { relationDelete(toId: $toId,"
                "    relationship_type: $rt) { id } } }"
            )
            _, err = await _gql(
                query, {"id": group_id, "toId": role_id, "rt": _REL_HAS_ROLE}
            )
            if err:
                return err
            return f"Removed role {role_id} from group {group_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, add_role, remove_role."
        )

    # ── Roles ──────────────────────────────────────────────────────────
    async def manage_opencti_role(
        action: str,
        role_id: str = "",
        name: str = "",
        description: str = "",
        capability_id: str = "",
        search: str = "",
        limit: int = _ROLE_DEFAULT_LIMIT,
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        # Defaults count as not passed: a field is refused only when the caller set it.
        refused = _refuse_unapplied(
            action,
            _ROLE_APPLIES,
            {
                "role_id": role_id,
                "name": name,
                "description": description,
                "capability_id": capability_id,
                "search": search,
                "limit": None if limit == _ROLE_DEFAULT_LIMIT else limit,
            },
            dict(extra),
        )
        if refused:
            return refused

        if action == "list":
            query = (
                "query Roles($first: Int, $search: String) {"
                "  roles(first: $first, search: $search, orderBy: name,"
                "    orderMode: asc) { edges { node { id name description } } } }"
            )
            data, err = await _gql(
                query, {"first": min(max(limit, 1), 200), "search": search or None}
            )
            if err:
                return err
            nodes = _edges(data, "roles")
            if not nodes:
                return "No roles found."
            lines = [f"Roles ({len(nodes)}):"]
            for n in nodes:
                lines.append(f"- {n.get('name')} (id: {n.get('id')})")
                if n.get("description"):
                    lines.append(f"    {n['description']}")
            return "\n".join(lines)

        if action == "get":
            if not role_id:
                return "role_id is required for action 'get'."
            query = (
                "query Role($id: String!) {"
                "  role(id: $id) { id name description"
                "    capabilities { id name } } }"
            )
            data, err = await _gql(query, {"id": role_id})
            if err:
                return err
            r = data.get("role")
            if not r:
                return f"Role {role_id} not found."
            caps = [c.get("name", "") for c in (r.get("capabilities") or [])]
            lines = [
                f"{r.get('name')} (id: {r.get('id')})",
                f"  description: {r.get('description') or '-'}",
                f"  capabilities ({len(caps)}): {', '.join(caps) if caps else 'none'}",
            ]
            return "\n".join(lines)

        if action == "create":
            if not name:
                return "name is required for action 'create'."
            query = (
                "mutation RoleAdd($input: RoleAddInput!) {"
                "  roleAdd(input: $input) { id name } }"
            )
            data, err = await _gql(
                query, {"input": {"name": name, "description": description or None}}
            )
            if err:
                return err
            r = data.get("roleAdd") or {}
            return f"Created role '{r.get('name')}' (id: {r.get('id')})."

        if action == "update":
            if not role_id:
                return "role_id is required for action 'update'."
            edits = _edits(
                [("name", name or None), ("description", description or None)]
            )
            if not edits:
                return "Provide name and/or description to update."
            query = (
                "mutation RolePatch($id: ID!, $input: [EditInput]!) {"
                "  roleEdit(id: $id) { fieldPatch(input: $input) { id name } } }"
            )
            _, err = await _gql(query, {"id": role_id, "input": edits})
            if err:
                return err
            return f"Updated role {role_id}."

        if action == "delete":
            if not role_id:
                return "role_id is required for action 'delete'."
            query = "mutation RoleDelete($id: ID!) { roleEdit(id: $id) { delete } }"
            _, err = await _gql(query, {"id": role_id})
            if err:
                return err
            return f"Deleted role {role_id}."

        if action in ("add_capability", "remove_capability"):
            if not role_id or not capability_id:
                return f"role_id and capability_id are required for action '{action}'."
            if action == "add_capability":
                query = (
                    "mutation AddCap($id: ID!, $input: InternalRelationshipAddInput!) {"
                    "  roleEdit(id: $id) { relationAdd(input: $input) { id } } }"
                )
                _, err = await _gql(
                    query,
                    {
                        "id": role_id,
                        "input": {
                            "relationship_type": _REL_HAS_CAPABILITY,
                            "toId": capability_id,
                        },
                    },
                )
                if err:
                    return err
                return f"Added capability {capability_id} to role {role_id}."
            query = (
                "mutation RemoveCap($id: ID!, $toId: StixRef!, $rt: String!) {"
                "  roleEdit(id: $id) { relationDelete(toId: $toId,"
                "    relationship_type: $rt) { id } } }"
            )
            _, err = await _gql(
                query, {"id": role_id, "toId": capability_id, "rt": _REL_HAS_CAPABILITY}
            )
            if err:
                return err
            return f"Removed capability {capability_id} from role {role_id}."

        return (
            f"Unknown action '{action}'. Valid actions: list, get, create, update, "
            "delete, add_capability, remove_capability."
        )

    async def list_opencti_capabilities() -> str:
        query = (
            "query Capabilities {"
            "  capabilities(first: 500) { edges { node { id name description"
            "    attribute_order } } } }"
        )
        data, err = await _gql(query)
        if err:
            return err
        nodes = _edges(data, "capabilities")
        if not nodes:
            return "No capabilities found."
        nodes.sort(key=lambda n: n.get("attribute_order") or 0)
        lines = [f"Capabilities ({len(nodes)}):"]
        for n in nodes:
            lines.append(f"- {n.get('name')} (id: {n.get('id')})")
        return "\n".join(lines)

    # ── Profile (me) ───────────────────────────────────────────────────
    async def manage_opencti_profile(
        action: str,
        name: str = "",
        user_email: str = "",
        firstname: str = "",
        lastname: str = "",
        description: str = "",
        theme: str = "",
        language: str = "",
        unit_system: str = "",
        new_password: str = "",
        current_password: str = "",
        **extra: object,
    ) -> str:
        action = (action or "").strip().lower()
        refused = _refuse_unapplied(
            action,
            _PROFILE_APPLIES,
            {
                "name": name,
                "user_email": user_email,
                "firstname": firstname,
                "lastname": lastname,
                "description": description,
                "theme": theme,
                "language": language,
                "unit_system": unit_system,
                "new_password": new_password,
                "current_password": current_password,
            },
            dict(extra),
        )
        if refused:
            return refused

        if action == "get":
            query = (
                "query Me { me { id name user_email firstname lastname theme"
                "  language unit_system account_status"
                "  groups { edges { node { name } } } } }"
            )
            data, err = await _gql(query)
            if err:
                return err
            me = data.get("me") or {}
            groups = ", ".join(
                e.get("name", "") for e in _edges(me, "groups")
            ) or "none"
            return "\n".join(
                [
                    f"{me.get('name')} <{me.get('user_email')}> (id: {me.get('id')})",
                    f"  full name: {me.get('firstname') or ''} {me.get('lastname') or ''}".rstrip(),
                    f"  theme: {me.get('theme') or 'default'}",
                    f"  language: {me.get('language') or 'auto'}",
                    f"  unit_system: {me.get('unit_system') or 'auto'}",
                    f"  account_status: {me.get('account_status')}",
                    f"  groups: {groups}",
                ]
            )

        if action == "update":
            # meEdit only accepts keys in OpenCTI's ME_USER_MODIFIABLE_ATTRIBUTES
            # allowlist (src/domain/user.js), which lists `user_name` (NOT `name`)
            # for the display name and throws ForbiddenAccess for any other key.
            # The admin userEdit path uses `name`; meEdit intentionally uses
            # `user_name` - do not "normalise" this to `name`.
            edits = _edits(
                [
                    ("user_name", name or None),
                    ("user_email", user_email or None),
                    ("firstname", firstname or None),
                    ("lastname", lastname or None),
                    ("description", description or None),
                    ("theme", theme or None),
                    ("language", language or None),
                    ("unit_system", unit_system or None),
                ]
            )
            if new_password:
                if not current_password:
                    return (
                        "current_password is required to change your password "
                        "(OpenCTI verifies it before applying the new one)."
                    )
                edits.append({"key": "password", "value": [new_password]})
            if not edits:
                return "Provide at least one field to update."
            query = (
                "mutation MeEdit($input: [EditInput]!, $password: String) {"
                "  meEdit(input: $input, password: $password) { id name } }"
            )
            variables = {"input": edits}
            if new_password:
                variables["password"] = current_password
            data, err = await _gql(query, variables)
            if err:
                return err
            me = data.get("meEdit") or {}
            return f"Updated your profile (name: {me.get('name')})."

        return f"Unknown action '{action}'. Valid actions: get, update."

    executor.register("manage_opencti_user", manage_opencti_user)
    executor.register("manage_opencti_group", manage_opencti_group)
    executor.register("manage_opencti_role", manage_opencti_role)

    async def get_opencti_platform_version() -> str:
        data, err = await _gql("query PlatformVersion { about { version } }")
        about = data.get("about") if isinstance(data, dict) and not err else None
        version = about.get("version") if isinstance(about, dict) else None
        if err:
            answer, version = err, ""
        elif not isinstance(version, str) or not version.strip():
            answer, version = "OpenCTI answered about { version } without a version: the platform version could not be read.", ""
        else:
            version = version.strip()[:200]
            answer = f"OpenCTI version: {json.dumps({'version': version})}"
        if registered_version_fn is None:
            return answer
        return version_answer("OpenCTI", answer, version, await registered_version_fn())

    executor.register("list_opencti_capabilities", list_opencti_capabilities)
    executor.register("manage_opencti_profile", manage_opencti_profile)
    executor.register("get_opencti_platform_version", get_opencti_platform_version)

    return [
        ToolDefinition(
            name="manage_opencti_user",
            description=(
                "Manage OpenCTI users (requires SETTINGS_SETACCESSES). Actions: "
                "'list' (search), 'get' (user_id), 'create' (name + optional "
                "user_email/password/firstname/lastname/description/account_status/"
                "group_ids/max_confidence/confidence_overrides), 'update' (user_id + "
                "fields; max_confidence and/or confidence_overrides set the user's own "
                "confidence level, inherit_confidence=true clears it so the user "
                "inherits from its groups; the answer reads the stored and effective "
                "level back), 'delete' (user_id), "
                "'add_group'/'remove_group' (user_id + group_id), "
                "'add_organization'/'remove_organization' (user_id + organization_id). "
                "'get' shows the user's own confidence level (or inherit) and the "
                "effective one with its source. A field the action does not apply is "
                "refused with nothing written."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete",
                            "add_group", "remove_group",
                            "add_organization", "remove_organization",
                        ],
                    },
                    "user_id": {"type": "string", "description": "User ID."},
                    "name": {"type": "string", "description": "Display name (create/update)."},
                    "user_email": {"type": "string", "description": "Email (create/update)."},
                    "password": {"type": "string", "description": "Initial password (create)."},
                    "firstname": {"type": "string"},
                    "lastname": {"type": "string"},
                    "description": {"type": "string"},
                    "account_status": {"type": "string", "description": "e.g. Active, Inactive (create/update)."},
                    "group_ids": {"type": "array", "items": {"type": "string"}, "description": "Group IDs to assign at creation."},
                    "group_id": {"type": "string", "description": "Group ID for add_group/remove_group."},
                    "organization_id": {"type": "string", "description": "Organization ID for add/remove_organization."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                    "max_confidence": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "User's own max confidence level (create/update); overrides the group level.",
                    },
                    "confidence_overrides": _OVERRIDES_SCHEMA,
                    "inherit_confidence": {
                        "type": "boolean",
                        "description": "update: clear the user's own confidence level so it inherits from its groups.",
                    },
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_group",
            description=(
                "Manage OpenCTI groups (requires SETTINGS_SETACCESSES). Actions: "
                "'list' (search), 'get' (group_id), 'create' (name, optional "
                "description, max_confidence 0-100 default 100, confidence_overrides), "
                "'update' (group_id + any of name/description/max_confidence/"
                "confidence_overrides; the answer reads the stored level back), "
                "'delete' (group_id), 'add_role'/'remove_role' (group_id + role_id). "
                "'get' shows the group's max confidence, overrides and members "
                "(name, id, email, service account). A field the "
                "action does not apply is refused with nothing written."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete",
                            "add_role", "remove_role",
                        ],
                    },
                    "group_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "max_confidence": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Group max confidence level (create, default 100; update).",
                    },
                    "confidence_overrides": _OVERRIDES_SCHEMA,
                    "role_id": {"type": "string", "description": "Role ID for add_role/remove_role."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_role",
            description=(
                "Manage OpenCTI roles (requires SETTINGS_SETACCESSES). Actions: "
                "'list' (search), 'get' (role_id - shows assigned capabilities), "
                "'create' (name, optional description), 'update' (role_id + "
                "name/description), 'delete' (role_id), 'add_capability'/"
                "'remove_capability' (role_id + capability_id from "
                "list_opencti_capabilities). A field the action does not apply is "
                "refused with nothing written."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "list", "get", "create", "update", "delete",
                            "add_capability", "remove_capability",
                        ],
                    },
                    "role_id": {"type": "string"},
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "capability_id": {"type": "string", "description": "Capability ID for add/remove_capability."},
                    "search": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="list_opencti_capabilities",
            description=(
                "List all OpenCTI capabilities (permissions) with their IDs. Use the "
                "returned id values with manage_opencti_role add_capability/"
                "remove_capability."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
        ToolDefinition(
            name="manage_opencti_profile",
            description=(
                "Read or update the authenticated user's own OpenCTI profile. "
                "Actions: 'get' (returns name, email, theme, language, unit_system, "
                "groups) and 'update' (any of name, user_email, firstname, lastname, "
                "description, theme, language, unit_system). To change the password, "
                "set new_password AND current_password (OpenCTI verifies the current "
                "password first). A field the action does not apply is refused with "
                "nothing written."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["get", "update"]},
                    "name": {"type": "string"},
                    "user_email": {"type": "string"},
                    "firstname": {"type": "string"},
                    "lastname": {"type": "string"},
                    "description": {"type": "string"},
                    "theme": {"type": "string", "description": "e.g. dark, light."},
                    "language": {"type": "string", "description": "e.g. en-us, fr-fr, auto."},
                    "unit_system": {"type": "string", "description": "auto, Metric, or Imperial."},
                    "new_password": {"type": "string", "description": "New password (requires current_password)."},
                    "current_password": {"type": "string", "description": "Current password (required to change password)."},
                },
                "required": ["action"],
            },
            source=_SOURCE,
        ),
        ToolDefinition(
            name="get_opencti_platform_version",
            description=(
                "Read the version the bound OpenCTI platform runs (GraphQL about). "
                "Read-only; compare it with a release to tell whether an upgrade landed."
            ),
            input_schema={"type": "object", "properties": {}, "required": []},
            source=_SOURCE,
        ),
    ]
