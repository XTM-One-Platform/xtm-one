"""One shared-state entry per key for a company-managed agent, whatever the run context.

A run of an assignment that hides its runs is born personal
(``run.is_company_managed`` false). The tools used to create a NEW entry in
that run's personal scope, while a chat turn of the same company-managed
agent created a company-managed one: one key, two rows, and the key lookup
(own row first) kept each context on its own copy. The scope of a new entry
now follows the agent, the lookup of a company-managed agent prefers the
company-managed row, and the listing names each entry's scope and owner and
flags two visible entries under one key.
"""

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.company_managed_writes import CompanyManagedWriter, agent_writer
from app.tools.base import CallableToolExecutor, ToolCall, ToolResult

NO_DB = cast(AsyncSession, None)


async def _call(executor: CallableToolExecutor, name: str, /, **arguments: Any) -> ToolResult:
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


def _capture_upserts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _upsert(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": kwargs.get("value") or {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("order", ["chat_first", "run_first", "run_then_one_off"])
async def test_a_company_managed_agent_writes_one_scope_in_chat_and_in_a_hidden_run(
    monkeypatch: pytest.MonkeyPatch, order: str
) -> None:
    calls = _capture_upserts(monkeypatch)
    agent_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    # Chat: agent_chat passes the agent's flag for both arguments.
    _, chat = build_shared_state_tools(NO_DB, user_id, agent_id, is_company_managed=True, company_managed_agent=True)
    # Hidden run: assignment_engine passes run.is_company_managed (false).
    _, run = build_shared_state_tools(NO_DB, user_id, agent_id, is_company_managed=False, company_managed_agent=True)
    # Scheduled one-off (instruction mode): scheduled_work_item_runner._create_run
    # makes a personal run acting for the person who scheduled it; its turn runs
    # through run_agent_chat, the shared-state scope must still follow the agent.
    _, one_off = build_shared_state_tools(
        NO_DB, user_id, agent_id, is_company_managed=False, company_managed_agent=True, run_id=uuid.uuid4()
    )
    first, second = {
        "chat_first": (chat, run),
        "run_first": (run, chat),
        "run_then_one_off": (run, one_off),
    }[order]
    for executor in (first, second):
        result = await _call(executor, "set_shared_state", key="k", value_json='{"n": 1}')
        assert not result.is_error, result.content

    assert [c["is_company_managed"] for c in calls] == [True, True]
    assert {c["user_id"] for c in calls} == {user_id}
    assert {c["key"] for c in calls} == {"k"}


@pytest.mark.asyncio
async def test_a_personal_agent_still_creates_personal_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _capture_upserts(monkeypatch)
    _, executor = build_shared_state_tools(NO_DB, str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="k", value_json='{"n": 1}')
    assert not result.is_error, result.content
    assert calls[0]["is_company_managed"] is False


def _order_by(reader: CompanyManagedWriter | None) -> str:
    lookup = cast(Any, shared_state_service)._visible_state_query
    query = lookup(uuid.uuid4(), "k", None, reader)
    dialect = cast(Any, postgresql).dialect()
    return str(query.compile(dialect=dialect)).split("ORDER BY", 1)[1]


def test_the_lookup_of_a_company_managed_agent_prefers_the_company_managed_row() -> None:
    order_by = _order_by(agent_writer(is_company_managed=True, agent_id=uuid.uuid4()))
    assert order_by.index("is_company_managed") < order_by.index("user_id"), order_by


@pytest.mark.parametrize(
    "reader",
    [None, agent_writer(is_company_managed=False, agent_id=uuid.uuid4())],
    ids=["person", "personal_agent"],
)
def test_other_readers_keep_their_own_row_first(reader: CompanyManagedWriter | None) -> None:
    order_by = _order_by(reader)
    assert "is_company_managed" not in order_by
    assert "user_id" in order_by


@pytest.mark.asyncio
async def test_list_shared_state_shows_scope_owner_and_flags_a_duplicate_key(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = str(uuid.uuid4())
    owner_agent = str(uuid.uuid4())
    workspace = str(uuid.uuid4())
    entry = {"key": "k", "status": "active", "user_id": user_id, "owner_agent_id": owner_agent}

    async def _list(**kwargs: Any) -> list[dict[str, Any]]:
        return [
            {**entry, "is_company_managed": True},
            {**entry, "is_company_managed": False},
            {"key": "w", "status": "active", "is_company_managed": False, "workspace_id": workspace},
        ]

    monkeypatch.setattr(shared_state_service, "list_states", _list)
    _, executor = build_shared_state_tools(NO_DB, user_id, str(uuid.uuid4()), company_managed_agent=True)
    result = await _call(executor, "list_shared_state")
    rows = [line for line in result.content.splitlines() if line.startswith("- `")]
    assert len(rows) == 3
    managed, personal, filed = rows
    assert "duplicate key" in managed and "duplicate key" in personal
    assert "duplicate key" not in filed
    assert f"scope: company-managed, owner user {user_id}" in managed
    assert f"scope: personal, owner user {user_id}" in personal
    assert f"owner agent {owner_agent}" in managed
    assert f"scope: workspace {workspace}" in filed


@pytest.mark.asyncio
async def test_get_shared_state_names_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = str(uuid.uuid4())

    async def _get(**kwargs: Any) -> dict[str, Any]:
        return {
            "key": "k",
            "status": "active",
            "is_company_managed": True,
            "user_id": user_id,
            "value": {"n": 1},
            "notes": [],
        }

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    _, executor = build_shared_state_tools(NO_DB, user_id, str(uuid.uuid4()), company_managed_agent=True)
    result = await _call(executor, "get_shared_state", key="k")
    assert f"scope: company-managed, owner user {user_id}" in result.content
