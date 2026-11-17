"""Drata access tools - User accounts and roles."""

from __future__ import annotations

from typing import Any

from app.core.egress import egress_client
from app.integrations.http import format_entity_list
from app.tools.base import CallableToolExecutor, ToolDefinition

from . import _common
from ._common import (
    _PROVIDER,
    _empty,
    _full_name,
    _user_ref,
)


def register(
    base_url: str,
    headers: dict[str, str],
    upload_headers: dict[str, str],
    executor: CallableToolExecutor,
) -> list[ToolDefinition]:
    """Build the Drata access tools and register their callables."""

    def _fmt_user(u: dict[str, Any]) -> str:
        return (
            f"[{u.get('id')}] {_full_name(u)} <{u.get('email', 'N/A')}> "
            f"— Job: {u.get('jobTitle', 'N/A')} | "
            f"Roles: {', '.join(u.get('roles') or []) or 'None'}"
        )

    async def list_drata_users(email: str = "", first_name: str = "", last_name: str = "") -> str:
        """List Drata users.

        ``email`` resolves a single account by exact address (Drata's
        ``GET /users`` has no email filter — only ``firstName`` / ``lastName``
        prefix filters — so an email is looked up directly via the documented
        ``users/email:<address>`` path form). ``first_name`` / ``last_name``
        are server-side prefix filters.
        """
        # Strip first so a whitespace-only email falls through to the list
        # path instead of issuing a malformed ``/users/email:`` (empty) lookup.
        email = (email or "").strip()
        async with egress_client(timeout=30) as client:
            if email:
                # ``email`` is interpolated into the request path, so normalize
                # and validate it through the shared guard (returns the
                # documented ``email:<address>`` ref, or "" for anything that
                # could inject ``/`` / ``?`` / ``#`` / reserved chars).
                ref = _user_ref(email)
                if not ref:
                    return (
                        f"Error: '{email}' is not a valid email address "
                        "(expected e.g. user@example.com)."
                    )
                resp, err = await _common.safe_request(
                    client,
                    "get",
                    f"{base_url}/users/{ref}",
                    _PROVIDER,
                    "get user by email",
                    headers=headers,
                )
                if err:
                    return err
                return format_entity_list([resp.json()], _fmt_user, noun="user")

            params: dict[str, Any] = {}
            if first_name:
                params["firstName"] = first_name
            if last_name:
                params["lastName"] = last_name
            items, err, truncated = await _common._paginated_fetch_all(client, f"{base_url}/users", headers, params)
            if err:
                return err
            result = format_entity_list(items, _fmt_user, noun="user")
            if truncated:
                result += (
                    f"\n\n(Showing the first {len(items)} users; more exist. "
                    "Narrow the search with first_name / last_name, or look up a "
                    "specific person with the email argument.)"
                )
            return result

    async def get_drata_user(user_id: int) -> str:
        """Get details for a specific Drata user."""
        async with egress_client(timeout=30) as client:
            resp, err = await _common.safe_request(
                client,
                "get",
                f"{base_url}/users/{user_id}",
                _PROVIDER,
                "get user",
                headers=headers,
            )
            if err:
                return err
            u = resp.json()
            lines = [
                f"User: {_full_name(u)} (ID: {u.get('id')})",
                f"Email: {u.get('email', 'N/A')}",
                f"Job Title: {u.get('jobTitle', 'N/A')}",
                f"Roles: {', '.join(u.get('roles') or []) or 'None'}",
                f"Drata Terms Agreed: {u.get('drataTermsAgreedAt', 'N/A')}",
                f"Created: {u.get('createdAt', 'N/A')}",
                f"Updated: {u.get('updatedAt', 'N/A')}",
            ]
            return "\n".join(lines)

    async def list_drata_roles() -> str:
        """List available Drata user roles."""
        async with egress_client(timeout=30) as client:
            items, err = await _common._paginated_fetch(client, f"{base_url}/roles", headers, {})
            if err:
                return err
            return format_entity_list(
                items,
                lambda r: f"[{r.get('id')}] {r.get('name', 'Unnamed')} — Description: {r.get('description', 'N/A')}",
                noun="role",
            )

    callable_map = {
        "list_drata_users": list_drata_users,
        "get_drata_user": get_drata_user,
        "list_drata_roles": list_drata_roles,
    }
    for _name, _fn in callable_map.items():
        executor.register(_name, _fn)

    return [
        ToolDefinition(
            name="list_drata_users",
            description=(
                "List Drata user accounts. Pass email to resolve one account by "
                "exact address; use first_name / last_name for server-side prefix "
                "filters."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "email": {
                        "type": "string",
                        "description": "Exact email — returns that single user account",
                    },
                    "first_name": {
                        "type": "string",
                        "description": "Filter by first-name prefix (e.g. 'Jo')",
                    },
                    "last_name": {
                        "type": "string",
                        "description": "Filter by last-name prefix (e.g. 'Sm')",
                    },
                },
                "required": [],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="get_drata_user",
            description="Get details for a specific Drata user including roles and job title.",
            input_schema={
                "type": "object",
                "properties": {
                    "user_id": {"type": "integer", "description": "Drata user ID"},
                },
                "required": ["user_id"],
            },
            source="integration:drata",
            priority=2,
        ),
        ToolDefinition(
            name="list_drata_roles",
            description="List available Drata user roles.",
            input_schema=_empty,
            source="integration:drata",
            priority=2,
        ),
    ]
