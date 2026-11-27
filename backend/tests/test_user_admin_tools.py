"""The ``user_admin`` built-in group (#250): definitions, gates and argument checks.

The cascade itself runs against PostgreSQL in ``test_user_admin_tools_postgres.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services.builtin_tools import (
    USER_ADMIN_GROUP,
    build_user_admin_tools,
    offers_user_admin,
    refused_admin_bound_ids,
)
from app.services.builtin_tools import user_admin as module
from app.services.builtin_tools.filtering import AGENT_KEYED_BUILTIN_TOOL_IDS

_ADMIN_ID = "00000000-0000-0000-0000-0000000000a1"
_TOOLS = {
    "list_platform_users",
    "get_platform_user",
    "preview_user_deactivation",
    "deactivate_platform_users",
    "reactivate_platform_user",
    "list_platform_groups",
    "get_platform_group",
    "add_group_members",
    "remove_group_members",
    "list_deactivation_review",
    "resolve_deactivation_review_item",
}


async def _call(executor: Any, name: str, **args: Any) -> str:
    handler = executor.handler(name)
    assert handler is not None
    answer = await handler(**args)
    assert isinstance(answer, str)
    return answer


def test_the_group_builds_every_tool_stamped_with_its_source() -> None:
    defs, executor = build_user_admin_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    assert {d.name for d in defs} == _TOOLS
    assert all(d.source == f"builtin:{USER_ADMIN_GROUP}" for d in defs)
    assert all(executor.handler(name) is not None for name in _TOOLS)
    deactivate = next(d for d in defs if d.name == "deactivate_platform_users")
    assert deactivate.input_schema["required"] == ["users", "reason"]


def test_the_group_is_admin_bound_and_agent_keyed() -> None:
    assert USER_ADMIN_GROUP in AGENT_KEYED_BUILTIN_TOOL_IDS
    assert refused_admin_bound_ids([USER_ADMIN_GROUP], caller_is_admin=False) == [USER_ADMIN_GROUP]
    assert refused_admin_bound_ids([USER_ADMIN_GROUP], caller_is_admin=True) == []


@pytest.mark.parametrize(
    ("ids", "acting", "admin", "expected"),
    [
        ([USER_ADMIN_GROUP], _ADMIN_ID, True, True),
        ([USER_ADMIN_GROUP], _ADMIN_ID, False, False),
        ([USER_ADMIN_GROUP], None, True, False),
        (["web"], _ADMIN_ID, True, False),
    ],
)
def test_a_turn_wires_the_group_for_an_administrator_alone(
    ids: list[str], acting: str | None, admin: bool, expected: bool
) -> None:
    assert offers_user_admin(ids, acting_user_id=acting, acting_is_admin=admin) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(_TOOLS))
async def test_every_tool_refuses_a_caller_who_is_not_an_administrator(
    tool: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core import database

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *_exc: object) -> bool:
            return False

        async def get(self, *_args: object, **_kwargs: object) -> None:  # pragma: no cover - never reached
            raise AssertionError("the database was read for a non-administrator")

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    _, executor = build_user_admin_tools(user_id=_ADMIN_ID, caller_is_admin=False)
    args = {
        "users": ["someone@test.local"],
        "reason": "left",
        "user": "someone@test.local",
        "group_id": _ADMIN_ID,
        "deactivation_id": _ADMIN_ID,
        "key": "k",
        "resolution": "keep",
    }
    assert await _call(executor, tool, **args) == module.REFUSAL


@pytest.mark.asyncio
async def test_a_deactivation_needs_a_reason_and_a_bounded_batch() -> None:
    _, executor = build_user_admin_tools(user_id=_ADMIN_ID, caller_is_admin=True)
    name = "deactivate_platform_users"
    assert (await _call(executor, name, users=["a@test.local"], reason="  ")).startswith("Refused: a reason")
    assert (await _call(executor, name, users=[], reason="left")).startswith("Refused: give 1 to 100")
    too_many = [f"u{i}@test.local" for i in range(module.MAX_BATCH + 1)]
    assert (await _call(executor, name, users=too_many, reason="left")).startswith("Refused: give 1 to 100")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"status": "gone"}, "status must be one of"),
        ({"source": "ldap"}, "source must be one of"),
    ],
)
def test_an_unknown_filter_value_is_refused(kwargs: dict[str, str], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        module.user_filter_clauses(**kwargs)


def test_the_filters_build_one_clause_per_criterion() -> None:
    clauses = module.user_filter_clauses(
        status="inactive", source="sso", role="admin", search="ann", created_from="2026-01-01T00:00:00Z"
    )
    # inactive: is_active false + not deprovisioned; sso: one clause, models.user.provisioning_source_clause.
    assert len(clauses) == 6
