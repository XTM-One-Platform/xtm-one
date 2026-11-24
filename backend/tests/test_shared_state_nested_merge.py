"""Nested merge and path-scoped writes on shared state.

``set_shared_state`` merges ``value_json`` recursively (a nested object merges
key by key, a list or a scalar replaces) and takes ``path`` to write one
sub-key alone, against the value as stored under the row lock: two runs that
each keep a sibling sub-key of one object (``slice_state.P1`` and
``slice_state.P2``) never wipe each other, whatever the order of their writes.
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.company_managed_writes import agent_writer
from app.services.shared_state import deep_merge, parse_value_path, write_at_path
from app.tools.base import ToolCall

COMPANY_AGENT = agent_writer(is_company_managed=True)


def _state(value: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        workspace_id=None,
        key="harness-perf",
        label=None,
        owner_agent_id=None,
        status="active",
        value=value,
        notes=[],
        resolved_at=None,
        created_at=datetime(2026, 11, 2, tzinfo=UTC),
        updated_at=datetime(2026, 11, 2, tzinfo=UTC),
    )


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _Session:
    """The key lookup and the lock answer the one row as stored."""

    def __init__(self, existing):
        self._existing = existing
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None

    async def execute(self, _statement):
        return _Result(self._existing)

    def add(self, obj):
        self._existing = obj

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    async def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


# ── pure helpers ───────────────────────────────────────────────────────────


def test_deep_merge_merges_objects_and_replaces_lists_and_scalars():
    base = {"slice_state": {"P1": {"pr": 1, "tags": ["a"]}}, "owner": "x", "l": [1]}
    patch = {"slice_state": {"P1": {"tags": ["b"]}, "P2": {"pr": 2}}, "l": [2], "owner": None}
    out = deep_merge(base, patch)
    assert out == {
        "slice_state": {"P1": {"pr": 1, "tags": ["b"]}, "P2": {"pr": 2}},
        "owner": None,
        "l": [2],
    }
    assert base["slice_state"] == {"P1": {"pr": 1, "tags": ["a"]}}  # not mutated


def test_deep_merge_replaces_a_scalar_with_an_object_and_back():
    assert deep_merge({"a": 1}, {"a": {"b": 2}}) == {"a": {"b": 2}}
    assert deep_merge({"a": {"b": 2}}, {"a": 3}) == {"a": 3}


@pytest.mark.parametrize(
    ("raw", "parts"),
    [(None, []), ("", []), ("  ", []), ("slice_state", ["slice_state"]), ("slice_state.P2", ["slice_state", "P2"])],
)
def test_parse_value_path(raw, parts):
    assert parse_value_path(raw) == parts


@pytest.mark.parametrize("bad", ["a..b", ".a", "a.", "a. .b"])
def test_parse_value_path_refuses_empty_segments(bad):
    with pytest.raises(ValueError, match="dot-separated"):
        parse_value_path(bad)


def test_write_at_path_creates_missing_objects_and_keeps_siblings():
    value = {"slice_state": {"P1": {"pr": 1}}, "owner": "x"}
    out = write_at_path(value, ["slice_state", "P2"], {"pr": 2})
    assert out == {"slice_state": {"P1": {"pr": 1}, "P2": {"pr": 2}}, "owner": "x"}
    assert write_at_path({}, ["a", "b", "c"], {"d": 1}) == {"a": {"b": {"c": {"d": 1}}}}
    assert value == {"slice_state": {"P1": {"pr": 1}}, "owner": "x"}  # not mutated


def test_write_at_path_merges_or_replaces_the_target_alone():
    value = {"slice_state": {"P1": {"pr": 1, "state": "open"}, "P2": {"pr": 2}}}
    merged = write_at_path(value, ["slice_state", "P1"], {"state": "merged"})
    assert merged["slice_state"] == {"P1": {"pr": 1, "state": "merged"}, "P2": {"pr": 2}}
    replaced = write_at_path(value, ["slice_state", "P1"], {"state": "merged"}, replace=True)
    assert replaced["slice_state"] == {"P1": {"state": "merged"}, "P2": {"pr": 2}}


def test_write_at_path_refuses_to_go_through_a_non_object():
    with pytest.raises(ValueError, match="not an object"):
        write_at_path({"slice_state": [1]}, ["slice_state", "P1"], {"pr": 1})


# ── upsert_state: two writers of sibling sub-keys ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("P1", "P2"), ("P2", "P1")])
async def test_two_writers_of_sibling_sub_keys_both_survive_a_deep_merge(monkeypatch, order):
    state = _state({"slice_state": {"P0": {"pr": 0}}, "program": "perf"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    for slice_name in order:
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-perf",
            value={"slice_state": {slice_name: {"pr": slice_name}}},
            deep=True,
            writer=COMPANY_AGENT,
        )
    assert state.value == {
        "slice_state": {"P0": {"pr": 0}, "P1": {"pr": "P1"}, "P2": {"pr": "P2"}},
        "program": "perf",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("P1", "P2"), ("P2", "P1")])
async def test_two_writers_of_sibling_paths_both_survive(monkeypatch, order):
    state = _state({"program": "perf"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    for slice_name in order:
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-perf",
            value={"pr": slice_name, "state": "open"},
            path=f"slice_state.{slice_name}",
            writer=COMPANY_AGENT,
        )
    assert state.value == {
        "program": "perf",
        "slice_state": {"P1": {"pr": "P1", "state": "open"}, "P2": {"pr": "P2", "state": "open"}},
    }


@pytest.mark.asyncio
async def test_the_shallow_merge_stays_the_default_of_the_service(monkeypatch):
    """Callers that never asked for ``deep`` (the REST API) keep their contract."""
    state = _state({"slice_state": {"P1": {"pr": 1}}})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-perf",
        value={"slice_state": {"P2": {"pr": 2}}},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"slice_state": {"P2": {"pr": 2}}}


@pytest.mark.asyncio
async def test_replace_still_overwrites_the_whole_value(monkeypatch):
    state = _state({"slice_state": {"P1": {"pr": 1}}, "program": "perf"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-perf",
        value={"slice_state": {"P2": {"pr": 2}}},
        deep=True,
        replace_value=True,
        writer=COMPANY_AGENT,
    )
    assert state.value == {"slice_state": {"P2": {"pr": 2}}}


@pytest.mark.asyncio
async def test_replace_with_a_path_overwrites_that_sub_key_alone(monkeypatch):
    state = _state({"slice_state": {"P1": {"pr": 1, "old": True}, "P2": {"pr": 2}}})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-perf",
        value={"pr": 11},
        path="slice_state.P1",
        replace_value=True,
        writer=COMPANY_AGENT,
    )
    assert state.value == {"slice_state": {"P1": {"pr": 11}, "P2": {"pr": 2}}}


@pytest.mark.asyncio
async def test_a_path_through_a_non_object_writes_nothing(monkeypatch):
    state = _state({"slice_state": "flat"})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="not an object"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-perf",
            value={"pr": 1},
            path="slice_state.P1",
            writer=COMPANY_AGENT,
        )
    assert state.value == {"slice_state": "flat"}
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_path_without_a_value_is_refused(monkeypatch):
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(None))
    with pytest.raises(ValueError, match="path needs a value"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="harness-perf",
            path="slice_state.P1",
            writer=COMPANY_AGENT,
        )


@pytest.mark.asyncio
async def test_a_new_entry_is_created_with_its_path(monkeypatch):
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(None))
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="harness-perf",
        value={"pr": 1},
        path="slice_state.P1",
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"] == {"slice_state": {"P1": {"pr": 1}}}


# ── set_shared_state tool ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_merges_deep_and_passes_the_path(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "value": {"slice_state": {"P2": {"pr": 2}}}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "set_shared_state", key="harness-perf", path="slice_state.P2", value_json='{"pr": 2}'
    )
    assert not result.is_error
    assert captured["deep"] is True
    assert captured["path"] == "slice_state.P2"
    assert captured["value"] == {"pr": 2}


@pytest.mark.asyncio
async def test_tool_without_a_path_merges_deep_with_no_path(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    await _call(executor, "set_shared_state", key="harness-perf", value_json='{"a": {"b": 1}}')
    assert captured["deep"] is True
    assert captured["path"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"path": "slice_state.P1"}, "path needs value_json"),
        ({"path": ["slice_state"], "value_json": "{}"}, "path must be a dot-separated string"),
    ],
)
async def test_tool_refuses_a_bad_path_before_any_write(monkeypatch, arguments, expected):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="harness-perf", **arguments)
    assert expected in result.content
    assert called is False


def test_the_tool_description_names_the_recursive_merge_and_the_path():
    defs, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    tool = next(d for d in defs if d.name == "set_shared_state")
    assert "merged recursively" in tool.description
    assert "path" in tool.description
    assert "path" in tool.input_schema["properties"]


# ── list changes under a path (gap shared-state-path-write-lost-update) ─────


@pytest.mark.asyncio
async def test_list_changes_under_a_path_keep_the_siblings_and_the_other_lists(monkeypatch):
    state = _state(
        {
            "pools": {
                "connectors": {"open": [{"n": 8424}, {"n": 8287}], "merged_by_team": [{"n": 1}], "size": 10},
                "opencti": {"open": [{"n": 5}]},
            }
        }
    )
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="pr-pools",
        path="pools.connectors",
        remove={"open": [{"n": 8287}]},
        append={"merged_by_team": [{"n": 8287}]},
        writer=COMPANY_AGENT,
    )
    assert state.value == {
        "pools": {
            "connectors": {"open": [{"n": 8424}], "merged_by_team": [{"n": 1}, {"n": 8287}], "size": 10},
            "opencti": {"open": [{"n": 5}]},
        }
    }


@pytest.mark.asyncio
async def test_two_list_writes_under_one_path_both_land(monkeypatch):
    state = _state({"pools": {"connectors": {"open": [{"n": 1}, {"n": 2}], "merged": []}}})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    for n in (1, 2):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="pr-pools",
            path="pools.connectors",
            remove={"open": [{"n": n}]},
            append={"merged": [{"n": n}]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"pools": {"connectors": {"open": [], "merged": [{"n": 1}, {"n": 2}]}}}


@pytest.mark.asyncio
async def test_list_changes_under_a_path_create_the_object(monkeypatch):
    state = _state({"pools": {}})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session(state))
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="pr-pools",
        path="pools.openaev",
        append={"open": [{"n": 7}]},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"pools": {"openaev": {"open": [{"n": 7}]}}}


@pytest.mark.asyncio
async def test_list_changes_under_a_non_object_write_nothing(monkeypatch):
    state = _state({"pools": {"connectors": [1, 2]}})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="not an object"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="pr-pools",
            path="pools.connectors",
            append={"open": [{"n": 7}]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"pools": {"connectors": [1, 2]}}
    assert session.committed is False


@pytest.mark.asyncio
async def test_tool_passes_list_changes_with_a_path_and_no_value(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="pr-pools",
        path="pools.connectors",
        remove_json='{"open": [{"n": 8287}]}',
    )
    assert not result.is_error
    assert captured["path"] == "pools.connectors"
    assert captured["remove"] == {"open": [{"n": 8287}]}
    assert captured["value"] is None


@pytest.mark.asyncio
async def test_tool_archives_what_path_list_changes_drop_before_the_write(monkeypatch):
    calls: list[dict] = []
    stored = {"pools": {"connectors": {"open": [{"n": 1}, {"n": 2}, {"n": 3}]}}, "runs": [{"n": 9}]}

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": stored, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="pr-pools",
        path="pools.connectors",
        remove_json='{"open": [{"n": 1}]}',
        keep_last_json='{"open": 1}',
        confirm_trim=True,
        archive_key="pr-pools:archive",
    )
    assert not result.is_error, result.content
    assert [c["key"].endswith("pr-pools:archive") for c in calls] == [True, False]
    assert calls[0]["append"] == {"open": [{"n": 1}, {"n": 2}]}
    assert calls[1]["path"] == "pools.connectors"
    assert calls[1]["expect_dropped"] == {"open": [{"n": 1}, {"n": 2}]}
    assert "Archived 2 item(s) whole" in result.content


@pytest.mark.asyncio
async def test_tool_archives_what_a_path_unset_removes(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"pools": {"c": {"open": [{"n": 1, "long": "L"}]}}}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="pr-pools",
        path="pools.c",
        update_json='{"open": {"match": {"n": 1}, "unset": ["long"]}}',
        archive_key="pr-pools:archive",
    )
    assert not result.is_error, result.content
    archived = calls[0]["append"]["entries"][0]
    assert archived["fields"] == {"long": "L"} and archived["source_key"] == "pr-pools#pools.c"


@pytest.mark.asyncio
async def test_upsert_refuses_a_path_list_that_changed_since_the_archive(monkeypatch):
    state = _state({"pools": {"c": {"open": [{"n": 1}, {"n": 2}]}}})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="changed between the archive and the write"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="pr-pools",
            path="pools.c",
            remove={"open": [{"n": 2}]},
            expect_dropped={"open": [{"n": 1}]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"pools": {"c": {"open": [{"n": 1}, {"n": 2}]}}}
    assert session.committed is False
