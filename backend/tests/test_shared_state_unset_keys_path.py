"""``unset_keys`` is relative to ``path``, and a large unset needs a copy (#2449).

With ``path``, the list changes of ``set_shared_state`` act on the object under
the path; ``unset_keys`` used to act on the top level, so an unset meant for a
nested key deleted the top-level key of the same name, silently. These tests
pin the path-relative unset, its archive, and the guard on a large unarchived
unset; the top-level unset without a path is unchanged.
"""

import uuid
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.builtin_tools.shared_state import UNSET_KEYS_GUARD_CHARS
from app.services.company_managed_writes import agent_writer
from app.services.shared_state import apply_unset_keys
from app.tools.base import ToolCall
from tests.test_shared_state_list_ops import _Session, _state

COMPANY_AGENT = agent_writer(is_company_managed=True)


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


# ── apply_unset_keys ────────────────────────────────────────────────────────


def test_unset_under_a_path_removes_the_nested_key_and_keeps_the_top_level_one():
    removed: list[str] = []
    out = apply_unset_keys({"a": {"b": 1, "c": 2}, "b": 9}, ["b"], ["a"], removed)
    assert out == {"a": {"c": 2}, "b": 9}
    assert removed == ["a.b"]


def test_unset_without_a_path_removes_top_level_keys_as_before():
    removed: list[str] = []
    out = apply_unset_keys({"a": {"b": 1}, "b": 9, "c": 3}, ["b", "missing"], [], removed)
    assert out == {"a": {"b": 1}, "c": 3}
    assert removed == ["b"]


def test_unset_of_missing_keys_or_a_path_to_no_object_changes_nothing():
    removed: list[str] = []
    assert apply_unset_keys({"a": {"c": 2}}, ["b"], ["a"], removed) is None
    assert apply_unset_keys({"a": 1}, ["b"], ["a"], removed) is None
    assert apply_unset_keys({"x": 1}, ["b"], ["a", "z"], removed) is None
    assert removed == []


def test_a_large_guarded_unset_is_refused_and_a_small_one_passes():
    value = {"runs_reviewed": {"runs": ["x" * 40] * 60}, "small": 1}
    with pytest.raises(ValueError, match="nothing was written"):
        apply_unset_keys(value, ["runs_reviewed"], [], [], guard_chars=UNSET_KEYS_GUARD_CHARS)
    assert "runs_reviewed" in value
    removed: list[str] = []
    out = apply_unset_keys(value, ["small"], [], removed, guard_chars=UNSET_KEYS_GUARD_CHARS)
    assert out == {"runs_reviewed": value["runs_reviewed"]} and removed == ["small"]


# ── upsert_state ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_unsets_under_the_path_and_names_the_dotted_key(monkeypatch):
    state = _state({"a": {"b": 1, "c": 2}, "b": 9})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()), key="open-prs", path="a", unset_keys=["b"], writer=COMPANY_AGENT
    )
    assert state.value == {"a": {"c": 2}, "b": 9}
    assert out["unset_keys"] == ["a.b"]


@pytest.mark.asyncio
async def test_upsert_reproduction_of_the_issue_keeps_the_fifty_records(monkeypatch):
    records = [{"run": i} for i in range(50)]
    state = _state({"runs_reviewed": {"runs": records, "runs_reviewed": [{"run": 99}]}})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        path="runs_reviewed",
        unset_keys=["runs_reviewed"],
        writer=COMPANY_AGENT,
    )
    assert state.value == {"runs_reviewed": {"runs": records}}


@pytest.mark.asyncio
async def test_upsert_refuses_a_guarded_large_unset_and_writes_nothing(monkeypatch):
    big = {"runs": ["x" * 50] * 60}
    state = _state({"big": big, "keep": 1})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="nothing was written"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            unset_keys=["big"],
            unset_guard_chars=UNSET_KEYS_GUARD_CHARS,
            writer=COMPANY_AGENT,
        )
    assert state.value == {"big": big, "keep": 1}
    assert session.committed is False


# ── set_shared_state ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_accepts_path_with_unset_keys_alone_and_passes_both(monkeypatch):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"a": {"c": 2}, "b": 9}, "notes": [], "unset_keys": ["a.b"]}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="k", path="a", unset_keys=["b"])
    assert not result.is_error, result.content
    assert calls[0]["path"] == "a" and calls[0]["unset_keys"] == ["b"]
    assert calls[0]["unset_guard_chars"] == UNSET_KEYS_GUARD_CHARS
    assert "keys removed: a.b" in result.content


@pytest.mark.asyncio
async def test_tool_lifts_the_guard_on_confirm_trim_or_an_archive(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"a": 1}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": [], "unset_keys": ["a"]}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    await _call(executor, "set_shared_state", key="k", unset_keys=["a"], confirm_trim=True)
    await _call(executor, "set_shared_state", key="k", unset_keys=["a"], archive_key="k-archive")
    source_writes = [c for c in calls if not c["key"].endswith("k-archive")]
    assert [c["unset_guard_chars"] for c in source_writes] == [None, None]


@pytest.mark.asyncio
async def test_tool_answers_the_guard_refusal_with_nothing_written(monkeypatch):
    async def _upsert(**kwargs):
        removed: list[str] = []
        apply_unset_keys(
            {"big": "x" * 3000}, kwargs["unset_keys"], [], removed, guard_chars=kwargs["unset_guard_chars"]
        )
        raise AssertionError("the guard did not refuse")

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="k", unset_keys=["big"])
    assert "nothing was written" in result.content
    assert "archive_key" in result.content


@pytest.mark.asyncio
async def test_tool_archives_the_keys_under_the_path_not_the_top_level_ones(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"a": {"b": "nested", "c": 2}, "b": "top"}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"a": {"c": 2}, "b": "top"}, "notes": [], "unset_keys": ["a.b"]}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "set_shared_state", key="gaps", path="a", unset_keys=["b"], archive_key="gaps-archive"
    )
    assert not result.is_error, result.content
    entry = calls[0]["append"]["entries"][0]
    assert entry["fields"] == {"b": "nested"} and entry["path"] == "a"
    assert calls[1]["path"] == "a" and calls[1]["unset_keys"] == ["b"]


def test_the_tool_schema_says_unset_keys_and_list_ops_are_relative_to_path():
    tools, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    schema = next(t for t in tools if t.name == "set_shared_state").input_schema["properties"]
    assert "unset_keys apply under it" in schema["path"]["description"]
    assert "under path" in schema["unset_keys"]["description"]
    assert "unset_keys" in schema["confirm_trim"]["description"]
