"""Microsoft To Do: the ``todo_task_changed`` event and the task tools an agent owns a list with.

- ``update_microsoft_task`` ``status`` and ``append_note`` (a dated line, the body kept).
- Task listings show status, created and modified times; ``list_microsoft_tasks`` ``status``.
- Every tool write is recorded, so the writing assignment (or, ``exclude_self``, every
  assignment) is not triggered by it.
- The poller diffs the kept lists by ``lastModifiedDateTime`` and tells created,
  updated and completed apart; the filter keeps or drops lists and change kinds.
- The subscription plan names each list by id, else the trigger is polled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.providers import microsoft as ms
from app.integrations.providers.microsoft import build_tools
from app.integrations.providers.microsoft.tasks import appended_body, task_line
from app.services import agent_posts, event_poller
from app.services.graph_event_subscriptions import parse_todo_resource, subscription_plan
from app.services.integration_event_filters import FILTER_SIGNALS_KEY, event_filter_reason, todo_list_matches
from app.tools.base import CallableToolExecutor, ToolCall

LIST_ID = "AAMkADY0" + "x" * 60


class _Resp:
    def __init__(self, payload: Any, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> Any:
        return self._payload


class _Recorder:
    def __init__(self, routes: dict[tuple[str, str], Any]) -> None:
        self.routes = routes
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, client, method, url, provider, op, **kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        payload = self.routes.get((method, url.rsplit("/v1.0", 1)[-1]), {"value": []})
        return _Resp(payload), None


class _MemoryRedis:
    """The strings and hashes these paths use, in memory (values as bytes, like Redis)."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    @staticmethod
    def _b(value: Any) -> bytes:
        return value if isinstance(value, bytes) else str(value).encode()

    async def get(self, key):
        value = self.data.get(key)
        return value if isinstance(value, bytes) else None

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = self._b(value)
        return True

    async def delete(self, *keys):
        return sum(1 for k in keys if self.data.pop(k, None) is not None)

    async def expire(self, key, seconds):
        return key in self.data

    async def hget(self, key, field):
        return (self.data.get(key) or {}).get(str(field))

    async def hset(self, key, field=None, value=None, mapping=None):
        table = self.data.setdefault(key, {})
        items = dict(mapping or {})
        if field is not None:
            items[field] = value
        for f, v in items.items():
            table[str(f)] = self._b(v)
        return len(items)

    async def hdel(self, key, *fields):
        table = self.data.get(key) or {}
        return sum(1 for f in fields if table.pop(str(f), None) is not None)


@pytest.fixture
def redis(monkeypatch):
    server = _MemoryRedis()

    async def _client():
        return server

    monkeypatch.setattr(agent_posts, "redis_client", _client)
    return server


def _executor(monkeypatch, routes):
    recorder = _Recorder(routes)
    monkeypatch.setattr(ms._common, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def test_task_line_shows_status_and_times():
    line = task_line(
        {
            "id": "t1",
            "title": "Call the bank",
            "status": "inProgress",
            "createdDateTime": "2026-11-07T06:00:00.0000000Z",
            "lastModifiedDateTime": "2026-11-07T06:05:00.0000000Z",
        }
    )
    assert "Status: inProgress" in line
    assert "Created: 2026-11-07T06:00:00" in line
    assert "Modified: 2026-11-07T06:05:00" in line
    assert line.endswith("ID: t1")


def test_appended_body_keeps_the_notes_and_dates_the_line():
    now = datetime(2026, 11, 7, 6, 30, tzinfo=UTC)
    assert appended_body("First line\n", "Waiting for the invoice", now=now) == (
        "First line\n[2026-11-07 06:30 UTC] Waiting for the invoice"
    )
    assert appended_body("", "Started", now=now) == "[2026-11-07 06:30 UTC] Started"


@pytest.mark.asyncio
async def test_update_task_sets_status_and_appends_a_note(monkeypatch, redis):
    path = "/me/todo/lists/L1/tasks/T1"
    executor, recorder = _executor(
        monkeypatch,
        {
            ("get", path): {"id": "T1", "body": {"content": "Old notes"}},
            ("patch", path): {"id": "T1", "title": "Pay", "status": "inProgress", "lastModifiedDateTime": "M1"},
        },
    )
    result = await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_microsoft_task",
            arguments={"list_id": "L1", "task_id": "T1", "status": "inProgress", "append_note": "Missing: the IBAN"},
        )
    )
    assert not result.is_error, result.content
    assert "Status: inProgress" in result.content
    patch = next(c for c in recorder.calls if c["method"] == "patch")
    assert patch["json"]["status"] == "inProgress"
    content = patch["json"]["body"]["content"]
    assert content.startswith("Old notes\n[") and content.endswith("] Missing: the IBAN")
    # The write is recorded (outside a run: the platform wrote it).
    assert await agent_posts.todo_writer(redis, "T1", "M1") == agent_posts.TODO_WRITER_PLATFORM


@pytest.mark.asyncio
async def test_update_task_refuses_an_unknown_status_and_body_with_append_note(monkeypatch, redis):
    executor, recorder = _executor(monkeypatch, {})
    bad = await executor.execute_tool(
        ToolCall(id="c1", name="update_microsoft_task", arguments={"list_id": "L", "task_id": "T", "status": "done"})
    )
    assert "status must be one of" in bad.content
    both = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="update_microsoft_task",
            arguments={"list_id": "L", "task_id": "T", "body": "x", "append_note": "y"},
        )
    )
    assert "not both" in both.content
    assert not recorder.calls


@pytest.mark.asyncio
async def test_list_tasks_by_status(monkeypatch, redis):
    executor, recorder = _executor(monkeypatch, {})
    await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_tasks", arguments={"list_id": "L1", "status": "inProgress"})
    )
    call = next(c for c in recorder.calls if "/tasks" in c["url"])
    assert call["params"]["$filter"] == "status eq 'inProgress'"


@pytest.mark.asyncio
async def test_a_run_write_is_its_own_only(redis):
    token = agent_posts.set_posting_assignment("wf-a")
    try:
        await agent_posts.record_todo_write({"id": "T1", "lastModifiedDateTime": "M1"})
    finally:
        agent_posts.reset_posting_assignment(token)
    assert await agent_posts.is_own_todo_write(redis, "T1", "M1", "wf-a")
    assert not await agent_posts.is_own_todo_write(redis, "T1", "M1", "wf-b")
    assert await agent_posts.is_own_todo_write(redis, "T1", "M1", "wf-b", exclude_self=True)
    # A person's later edit is nobody's write.
    assert not await agent_posts.is_own_todo_write(redis, "T1", "M2", "wf-a", exclude_self=True)


# ---------------------------------------------------------------------------
# Filter
# ---------------------------------------------------------------------------


def test_lists_and_exclude_lists_match_names_ids_and_well_known_lists():
    keep_tasks = {"lists": ["Tasks"], "exclude_lists": ["Flagged Emails"]}
    assert todo_list_matches("id1", "To Do", "defaultList", keep_tasks)
    assert not todo_list_matches("id2", "Courses", "none", keep_tasks)
    assert not todo_list_matches("id3", "Courrier marqué", "flaggedEmails", {"exclude_lists": ["flagged emails"]})
    assert todo_list_matches("id2", "Courses", "none", {"lists": ["ID2"]})
    assert todo_list_matches("id2", "Courses", "none", {})


def test_event_filter_reason_reads_lists_and_change_kinds():
    payload = {"list_id": "id1", "list_name": "Tasks", "change_kind": "updated", FILTER_SIGNALS_KEY: {}}
    assert event_filter_reason("todo_task_changed", payload, {"lists": ["Tasks"]}) is None
    assert event_filter_reason("todo_task_changed", payload, {"change_kinds": ["created"]}) == (
        "change kind is not in change_kinds"
    )
    assert event_filter_reason("todo_task_changed", payload, {"exclude_lists": ["tasks"]}) == (
        "task list is excluded or not in lists"
    )


# ---------------------------------------------------------------------------
# Subscription plan
# ---------------------------------------------------------------------------


def test_subscription_plan_names_each_list_by_id_else_polls():
    plan = subscription_plan({"event": "todo_task_changed", "filter": {"lists": [LIST_ID]}})
    assert plan.resources == ((f"me/todo/lists/{LIST_ID}/tasks", "created,updated,deleted"),)
    by_name = subscription_plan({"event": "todo_task_changed", "filter": {"lists": ["Tasks"]}})
    assert by_name.resources == () and "polled every minute" in (by_name.reason or "")


def test_parse_todo_resource_both_forms():
    assert parse_todo_resource("Users/u1/todo/lists('L1')/tasks('T1')") == ("L1", "T1")
    assert parse_todo_resource("me/todo/lists/L1/tasks/T1") == ("L1", "T1")
    assert parse_todo_resource("me/events/E1") is None


# ---------------------------------------------------------------------------
# Poller
# ---------------------------------------------------------------------------


class _Http:
    def __init__(self, lists, tasks_by_list):
        self.lists = lists
        self.tasks_by_list = tasks_by_list
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append((url, params or {}))
        if url.endswith("/me/todo/lists"):
            return _Resp({"value": self.lists})
        list_id = url.rsplit("/lists/", 1)[1].split("/", 1)[0]
        return _Resp({"value": self.tasks_by_list.get(list_id, [])})


def _wf(filter_config: dict):
    return SimpleNamespace(
        id="wf-todo",
        trigger_config={"provider": "microsoft", "event": "todo_task_changed", "filter": filter_config},
    )


@pytest.mark.asyncio
async def test_poll_seeds_from_now_then_tells_created_updated_completed(redis):
    lists = [
        {"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"},
        {"id": "L2", "displayName": "Flagged Emails", "wellknownListName": "flaggedEmails"},
    ]
    wf = _wf({"lists": ["Tasks"], "exclude_lists": ["Flagged Emails"]})
    http = _Http(lists, {})
    assert await event_poller._poll_microsoft_todo_changes("tok", wf, redis, http) == []
    assert await redis.get("event_cursor:wf-todo")

    await redis.set("event_cursor:wf-todo", "2026-11-07T06:00:00Z")
    new_task = {
        "id": "T1",
        "title": "Renew the domain",
        "status": "notStarted",
        "createdDateTime": "2026-11-07T06:10:00.0000000Z",
        "lastModifiedDateTime": "2026-11-07T06:10:01.0000000Z",
    }
    old_task = {
        "id": "T2",
        "title": "Old",
        "status": "notStarted",
        "createdDateTime": "2026-10-01T00:00:00Z",
        "lastModifiedDateTime": "2026-11-07T06:11:00Z",
    }
    http = _Http(lists, {"L1": [new_task, old_task], "L2": [{**new_task, "id": "F1"}]})
    events = await event_poller._poll_microsoft_todo_changes("tok", wf, redis, http)
    assert [(e["task_id"], e["change_kind"]) for e in events] == [("T1", "created"), ("T2", "updated")]
    assert events[0]["list_name"] == "Tasks" and events[0]["event"] == "todo_task_changed"
    assert not any("/lists/L2/" in url for url, _ in http.calls), "an excluded list is never read"
    assert (await redis.get("event_cursor:wf-todo")).decode() == "2026-11-07T06:11:00Z"

    done = {**new_task, "status": "completed", "lastModifiedDateTime": "2026-11-07T06:20:00Z"}
    http = _Http(lists, {"L1": [done]})
    events = await event_poller._poll_microsoft_todo_changes("tok", wf, redis, http)
    assert [(e["task_id"], e["change_kind"]) for e in events] == [("T1", "completed")]


@pytest.mark.asyncio
async def test_poll_skips_the_platform_own_writes_with_exclude_self(redis):
    lists = [{"id": "L1", "displayName": "Tasks", "wellknownListName": "defaultList"}]
    await redis.set("event_cursor:wf-todo", "2026-11-07T06:00:00Z")
    await agent_posts.record_todo_write({"id": "T9", "lastModifiedDateTime": "2026-11-07T06:30:00Z"})
    task = {
        "id": "T9",
        "title": "Created by the agent",
        "status": "notStarted",
        "createdDateTime": "2026-11-07T06:30:00Z",
        "lastModifiedDateTime": "2026-11-07T06:30:00Z",
    }
    http = _Http(lists, {"L1": [task]})
    assert await event_poller._poll_microsoft_todo_changes("tok", _wf({"exclude_self": True}), redis, http) == []

    await redis.set("event_cursor:wf-todo", "2026-11-07T06:00:00Z")
    await redis.delete("event_todo_state:wf-todo")
    events = await event_poller._poll_microsoft_todo_changes("tok", _wf({}), redis, _Http(lists, {"L1": [task]}))
    assert [e["task_id"] for e in events] == ["T9"], "without exclude_self, another writer's task triggers"
