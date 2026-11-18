# SPDX-License-Identifier: Apache-2.0

"""SCIM 2.0 (RFC 7643 / RFC 7644) resource shapes, filters and PATCH, for
``api/v1/scim.py``.

Pure functions: nothing here reads or writes the database. The route module
resolves the account or group, asks this module what a request means, and
applies it; every deactivation and reactivation goes through
``services/user_deactivation.py``.

Microsoft Entra compatibility (its documented request shapes):

- a PATCH ``replace`` with no ``path`` carries an object of attributes, its keys
  plain (``active``), dotted (``name.givenName``) or schema-qualified
  (``urn:...:enterprise:2.0:User:department``);
- booleans may come as the strings ``"True"`` / ``"False"``;
- operation names may be capitalised (``Replace``, ``Add``);
- group membership is removed with ``path`` ``members[value eq "<id>"]``, or
  ``members`` with a ``value`` list.

Attributes XTM One has no field for (phone numbers, addresses, preferred
language...) are accepted and ignored, so a provisioning run never stops on them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

SCIM_CONTENT_TYPE = "application/scim+json"

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"
SPC_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"
RESOURCE_TYPE_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:ResourceType"
SCHEMA_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Schema"

#: Page size when the caller names none, and the most one page answers.
DEFAULT_COUNT = 100
MAX_COUNT = 200


class ScimError(Exception):
    """A refusal in the SCIM error shape (RFC 7644 section 3.12)."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type

    def body(self) -> dict[str, Any]:
        out: dict[str, Any] = {"schemas": [ERROR_SCHEMA], "status": str(self.status), "detail": self.detail}
        if self.scim_type:
            out["scimType"] = self.scim_type
        return out


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


def as_bool(value: Any) -> bool:
    """A SCIM boolean, Entra's ``"True"`` / ``"False"`` strings included."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    raise ScimError(400, f"Not a boolean: {value!r}", "invalidValue")


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScimError(400, f"Not a string: {value!r}", "invalidValue")
    return value.strip() or None


# ---------------------------------------------------------------------------
# Filters: ``attr eq "value"`` clauses joined by ``and``
# ---------------------------------------------------------------------------

_CLAUSE = re.compile(
    r"""^\s*(?P<attr>[A-Za-z][\w.:]*)(?:\[\s*value\s+eq\s+"(?P<inner>(?:[^"\\]|\\.)*)"\s*\])?"""
    r"""\s*(?:(?P<op>eq)\s+"(?P<value>(?:[^"\\]|\\.)*)")?\s*$""",
    re.IGNORECASE,
)
_AND = re.compile(r"\s+and\s+", re.IGNORECASE)


def parse_filter(raw: str | None, allowed: set[str]) -> dict[str, str]:
    """``{attribute (lower case): value}`` for a filter of ``eq`` clauses.

    ``members[value eq "x"]`` reads as ``{"members.value": "x"}``. Any other
    operator or attribute is refused with ``invalidFilter``: answering a
    filter half-understood would list accounts the caller did not ask for.
    """
    if raw is None or not raw.strip():
        return {}
    out: dict[str, str] = {}
    for clause in _AND.split(raw.strip()):
        match = _CLAUSE.match(clause)
        if match is None:
            raise ScimError(400, f"Unsupported filter: {clause!r}", "invalidFilter")
        attr = match.group("attr").lower()
        if match.group("inner") is not None and match.group("op") is None:
            attr, value = f"{attr}.value", match.group("inner")
        elif match.group("op") is not None and match.group("inner") is None:
            value = match.group("value")
        else:
            raise ScimError(400, f"Unsupported filter: {clause!r}", "invalidFilter")
        if attr not in allowed:
            raise ScimError(400, f"Unsupported filter attribute: {attr!r}", "invalidFilter")
        out[attr] = value.replace('\\"', '"').replace("\\\\", "\\")
    return out


def page(start_index: int | None, count: int | None) -> tuple[int, int]:
    """``(offset, limit)`` for SCIM's 1-based ``startIndex`` and ``count``."""
    start = max(1, start_index or 1)
    limit = DEFAULT_COUNT if count is None else max(0, min(count, MAX_COUNT))
    return start - 1, limit


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


@dataclass
class UserChanges:
    """What a POST, PUT or PATCH asks of an account. ``None``: unchanged."""

    user_name: str | None = None
    external_id: str | None = None
    external_id_set: bool = False
    full_name: str | None = None
    given_name: str | None = None
    family_name: str | None = None
    title: str | None = None
    title_set: bool = False
    department: str | None = None
    department_set: bool = False
    active: bool | None = None

    def display_name(self, current: str | None = None) -> str | None:
        """The account name these changes ask for; ``current`` is the stored one.

        ``displayName`` / ``name.formatted`` win. Both name parts form a name. A
        single part (Entra PATCHes only the attribute that changed) never replaces
        a stored name: it keeps ``current``, and names the account only when there
        is no stored name yet (a create).
        """
        if self.full_name:
            return self.full_name
        if self.given_name and self.family_name:
            return f"{self.given_name} {self.family_name}"
        if current:
            return current
        return self.given_name or self.family_name


def _primary_email(emails: Any) -> str | None:
    if not isinstance(emails, list):
        return None
    chosen = None
    for entry in emails:
        if not isinstance(entry, dict) or not entry.get("value"):
            continue
        if entry.get("primary") in (True, "true", "True"):
            return _text(entry["value"])
        if chosen is None or str(entry.get("type", "")).lower() == "work":
            chosen = _text(entry["value"])
    return chosen


def _apply_user_attribute(changes: UserChanges, key: str, value: Any) -> None:
    """One attribute of a user, by its SCIM name (any case, dotted or URN-qualified)."""
    lowered = key.strip().lower()
    enterprise = ENTERPRISE_SCHEMA.lower() + ":"
    if lowered.startswith(enterprise):
        lowered = "enterprise." + lowered[len(enterprise) :]
    elif lowered.startswith(USER_SCHEMA.lower() + ":"):
        lowered = lowered[len(USER_SCHEMA) + 1 :]
    if lowered == ENTERPRISE_SCHEMA.lower() and isinstance(value, dict):
        for sub, sub_value in value.items():
            _apply_user_attribute(changes, f"enterprise.{sub}", sub_value)
        return
    if lowered == "name" and isinstance(value, dict):
        for sub, sub_value in value.items():
            _apply_user_attribute(changes, f"name.{sub}", sub_value)
        return
    if lowered == "username":
        changes.user_name = _text(value)
    elif lowered == "externalid":
        changes.external_id, changes.external_id_set = _text(value), True
    elif lowered in ("displayname", "name.formatted"):
        changes.full_name = _text(value) or changes.full_name
    elif lowered == "name.givenname":
        changes.given_name = _text(value)
    elif lowered == "name.familyname":
        changes.family_name = _text(value)
    elif lowered == "title":
        changes.title, changes.title_set = _text(value), True
    elif lowered == "enterprise.department":
        changes.department, changes.department_set = _text(value), True
    elif lowered == "active":
        changes.active = as_bool(value)
    elif lowered == "emails":
        email = _primary_email(value)
        if email and changes.user_name is None:
            changes.user_name = email
    # Anything else (phone numbers, addresses, the manager...) has no field here.


def user_changes_from_resource(body: dict[str, Any]) -> UserChanges:
    """The changes a POST or PUT body asks for (a whole resource)."""
    if not isinstance(body, dict):
        raise ScimError(400, "The body must be a JSON object", "invalidSyntax")
    changes = UserChanges()
    for key, value in body.items():
        if key in ("schemas", "id", "meta", "groups", "password"):
            continue
        _apply_user_attribute(changes, key, value)
    return changes


_MEMBER_PATH = re.compile(r'^members\[\s*value\s+eq\s+"(?P<id>[^"]+)"\s*\]$', re.IGNORECASE)


def _operations(body: Any) -> list[dict[str, Any]]:
    if not isinstance(body, dict) or not isinstance(body.get("Operations"), list):
        raise ScimError(400, "A PatchOp carries an 'Operations' list", "invalidSyntax")
    ops: list[dict[str, Any]] = []
    for op in body["Operations"]:
        if not isinstance(op, dict) or not isinstance(op.get("op"), str):
            raise ScimError(400, "Every operation names its 'op'", "invalidSyntax")
        name = op["op"].strip().lower()
        if name not in ("add", "replace", "remove"):
            raise ScimError(400, f"Unsupported operation: {op['op']!r}", "invalidSyntax")
        ops.append({"op": name, "path": op.get("path"), "value": op.get("value")})
    return ops


def user_changes_from_patch(body: Any) -> UserChanges:
    """The changes a PATCH asks of an account: ``add`` and ``replace`` set,
    ``remove`` clears (only what can be empty: title, department, externalId)."""
    changes = UserChanges()
    for op in _operations(body):
        path, value = op["path"], op["value"]
        if op["op"] == "remove":
            if not isinstance(path, str):
                raise ScimError(400, "A remove names its 'path'", "noTarget")
            _apply_user_attribute(changes, path, None)
            continue
        if path is None:
            if not isinstance(value, dict):
                raise ScimError(400, "An operation without 'path' carries an object", "invalidValue")
            for key, sub_value in value.items():
                _apply_user_attribute(changes, key, sub_value)
            continue
        if not isinstance(path, str):
            raise ScimError(400, "'path' must be a string", "invalidPath")
        if re.match(r"^emails(\[.*\])?(\.value)?$", path.strip(), re.IGNORECASE) and not isinstance(value, list):
            value = [{"value": value, "primary": True}] if isinstance(value, str) else value
            path = "emails"
        _apply_user_attribute(changes, path, value)
    return changes


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def user_resource(user: Any, base_url: str) -> dict[str, Any]:
    """The SCIM User of an account (``base_url``: ``.../api/v1/scim/v2``)."""
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA, ENTERPRISE_SCHEMA],
        "id": str(user.id),
        "userName": user.email,
        "active": bool(user.is_active),
        "displayName": user.full_name,
        "name": {"formatted": user.full_name},
        "emails": [{"value": user.email, "type": "work", "primary": True}],
        "meta": {
            "resourceType": "User",
            "created": _iso(user.created_at),
            "lastModified": _iso(user.updated_at or user.created_at),
            "location": f"{base_url}/Users/{user.id}",
        },
    }
    if user.scim_external_id:
        resource["externalId"] = user.scim_external_id
    if user.title:
        resource["title"] = user.title
    if user.directory_department:
        resource[ENTERPRISE_SCHEMA] = {"department": user.directory_department}
    return resource


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@dataclass
class GroupChanges:
    """What a PATCH asks of a group."""

    display_name: str | None = None
    external_id: str | None = None
    external_id_set: bool = False
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    #: Not None: the members become exactly this list.
    replace: list[str] | None = None


def _member_ids(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    ids: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("value"):
            ids.append(str(item["value"]))
        elif isinstance(item, str) and item:
            ids.append(item)
        else:
            raise ScimError(400, f"Not a member: {item!r}", "invalidValue")
    return ids


def group_members_from_resource(body: dict[str, Any]) -> list[str]:
    return _member_ids(body.get("members"))


def group_changes_from_patch(body: Any) -> GroupChanges:
    changes = GroupChanges()
    for op in _operations(body):
        path, value = op["path"], op["value"]
        lowered = path.strip().lower() if isinstance(path, str) else None
        member = _MEMBER_PATH.match(path.strip()) if isinstance(path, str) else None
        if op["op"] == "remove":
            if member is not None:
                changes.remove.append(member.group("id"))
            elif lowered == "members":
                if value is None:
                    changes.replace = []
                else:
                    changes.remove.extend(_member_ids(value))
            elif lowered == "externalid":
                changes.external_id, changes.external_id_set = None, True
            else:
                raise ScimError(400, f"Cannot remove {path!r}", "noTarget")
            continue
        if lowered is None:
            if not isinstance(value, dict):
                raise ScimError(400, "An operation without 'path' carries an object", "invalidValue")
            items = list(value.items())
        else:
            items = [(path, value)]
        for key, sub_value in items:
            k = key.strip().lower()
            if k == "displayname":
                changes.display_name = _text(sub_value)
            elif k == "externalid":
                changes.external_id, changes.external_id_set = _text(sub_value), True
            elif k == "members":
                if op["op"] == "replace":
                    changes.replace = _member_ids(sub_value)
                else:
                    changes.add.extend(_member_ids(sub_value))
            # Anything else has no field on a group.
    return changes


def group_resource(group: Any, members: list[tuple[str, str]] | None, base_url: str) -> dict[str, Any]:
    """The SCIM Group of a group; ``members`` ``[(user id, display)]`` or None to leave them out."""
    resource: dict[str, Any] = {
        "schemas": [GROUP_SCHEMA],
        "id": str(group.id),
        "displayName": group.name,
        "meta": {
            "resourceType": "Group",
            "created": _iso(group.created_at),
            "lastModified": _iso(group.updated_at or group.created_at),
            "location": f"{base_url}/Groups/{group.id}",
        },
    }
    if group.scim_external_id:
        resource["externalId"] = group.scim_external_id
    if members is not None:
        resource["members"] = [
            {"value": uid, "display": display, "$ref": f"{base_url}/Users/{uid}"} for uid, display in members
        ]
    return resource


def list_response(resources: list[dict[str, Any]], total: int, start_index: int) -> dict[str, Any]:
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def service_provider_config(base_url: str) -> dict[str, Any]:
    return {
        "schemas": [SPC_SCHEMA],
        "documentationUri": "https://docs.xtm.one",
        "patch": {"supported": True},
        "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
        "filter": {"supported": True, "maxResults": MAX_COUNT},
        "changePassword": {"supported": False},
        "sort": {"supported": False},
        "etag": {"supported": False},
        "authenticationSchemes": [
            {
                "type": "oauthbearertoken",
                "name": "Bearer token",
                "description": "A SCIM token generated by an administrator in Settings.",
                "primary": True,
            }
        ],
        "meta": {"resourceType": "ServiceProviderConfig", "location": f"{base_url}/ServiceProviderConfig"},
    }


def resource_types(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "schemas": [RESOURCE_TYPE_SCHEMA],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": USER_SCHEMA,
            "schemaExtensions": [{"schema": ENTERPRISE_SCHEMA, "required": False}],
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/User"},
        },
        {
            "schemas": [RESOURCE_TYPE_SCHEMA],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": GROUP_SCHEMA,
            "meta": {"resourceType": "ResourceType", "location": f"{base_url}/ResourceTypes/Group"},
        },
    ]


def _attribute(name: str, kind: str = "string", **extra: Any) -> dict[str, Any]:
    out = {
        "name": name,
        "type": kind,
        "multiValued": False,
        "required": False,
        "caseExact": False,
        "mutability": "readWrite",
        "returned": "default",
        "uniqueness": "none",
    }
    out.update(extra)
    return out


def schemas(base_url: str) -> list[dict[str, Any]]:
    user_attributes = [
        _attribute("userName", required=True, uniqueness="server"),
        _attribute("externalId"),
        _attribute("displayName"),
        _attribute(
            "name",
            "complex",
            subAttributes=[_attribute("formatted"), _attribute("givenName"), _attribute("familyName")],
        ),
        _attribute("title"),
        _attribute("active", "boolean"),
        _attribute(
            "emails",
            "complex",
            multiValued=True,
            subAttributes=[_attribute("value"), _attribute("type"), _attribute("primary", "boolean")],
        ),
    ]
    group_attributes = [
        _attribute("displayName", required=True, uniqueness="server"),
        _attribute("externalId"),
        _attribute(
            "members",
            "complex",
            multiValued=True,
            subAttributes=[_attribute("value", mutability="immutable"), _attribute("display", mutability="readOnly")],
        ),
    ]
    entries = (
        (USER_SCHEMA, "User", user_attributes),
        (GROUP_SCHEMA, "Group", group_attributes),
        (ENTERPRISE_SCHEMA, "EnterpriseUser", [_attribute("department")]),
    )
    return [
        {
            "schemas": [SCHEMA_SCHEMA],
            "id": schema_id,
            "name": name,
            "attributes": attributes,
            "meta": {"resourceType": "Schema", "location": f"{base_url}/Schemas/{schema_id}"},
        }
        for schema_id, name, attributes in entries
    ]
