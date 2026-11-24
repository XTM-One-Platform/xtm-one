"""List operations on shared state.

``set_shared_state`` takes ``append_json`` / ``remove_json``: a list kept under a
top-level key is changed on the server against the value as stored, and the
row an update reads is locked until the commit, so two runs keeping one list
(an open-PR list) never drop each other's items by rewriting it whole.
"""

import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.services.shared_state as shared_state_service
from app.services.builtin_tools import build_shared_state_tools
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    agent_writer,
)
from app.services.shared_state import apply_list_ops, unset_archive
from app.tools.base import ToolCall

COMPANY_AGENT = agent_writer(is_company_managed=True)


def _state(value: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=None,
        is_company_managed=True,
        workspace_id=None,
        key="open-prs",
        label=None,
        owner_agent_id=None,
        status="active",
        value=value,
        notes=[],
        resolved_at=None,
        created_at=datetime(2026, 11, 1, tzinfo=UTC),
        updated_at=datetime(2026, 11, 1, tzinfo=UTC),
    )


class _Result:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class _Session:
    """Records the statements; the key lookup answers *existing*."""

    def __init__(self, existing, deleted_before_lock=False):
        self._existing = existing
        self._deleted_before_lock = deleted_before_lock
        self.statements: list = []
        self.committed = False
        self.rolled_back = False
        self.locked: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, _model, _ident):
        return None

    async def execute(self, statement):
        self.statements.append(statement)
        if getattr(statement, "_for_update_arg", None) is not None:
            # The post-authorisation lock: the row as stored, or gone.
            if self._deleted_before_lock:
                # A concurrent delete landed between the lookup and the lock:
                # the row is gone for every later read.
                self._deleted_before_lock = False
                self._existing = None
                self.rolled_back = True
                return _Result(None)
            self.locked.append(self._existing)
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


PERSONAL_AGENT = agent_writer(is_company_managed=False)


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


# ── apply_list_ops ─────────────────────────────────────────────────────────


def test_append_creates_the_list_and_skips_items_already_there():
    value = {"prs": [{"repo": "o/r", "number": 1}], "owner": "x"}
    out, changes = apply_list_ops(
        value,
        append={"prs": [{"repo": "o/r", "number": 1}, {"repo": "o/r", "number": 2}], "new": ["a"]},
    )
    assert out["prs"] == [{"repo": "o/r", "number": 1}, {"repo": "o/r", "number": 2}]
    assert out["new"] == ["a"]
    assert out["owner"] == "x"
    assert changes == {"prs": {"appended": 1, "removed": 0}, "new": {"appended": 1, "removed": 0}}
    assert value["prs"] == [{"repo": "o/r", "number": 1}]  # the input is not mutated


def test_remove_by_equality_and_by_object_fields():
    value = {
        "prs": [
            {"repo": "o/r", "number": 1, "state": "green"},
            {"repo": "o/r", "number": 2},
            {"repo": "o/x", "number": 1},
        ],
        "tags": ["a", "b", "a"],
    }
    out, changes = apply_list_ops(
        value, remove={"prs": [{"repo": "o/r", "number": 1}], "tags": ["a"]}
    )
    assert out["prs"] == [{"repo": "o/r", "number": 2}, {"repo": "o/x", "number": 1}]
    assert out["tags"] == ["b"]
    assert changes["prs"]["removed"] == 1
    assert changes["tags"]["removed"] == 2


def test_an_empty_object_match_removes_only_empty_objects():
    out, changes = apply_list_ops({"l": [{}, {"a": 1}]}, remove={"l": [{}]})
    assert out["l"] == [{"a": 1}]
    assert changes["l"]["removed"] == 1


def test_remove_runs_before_append_so_an_item_can_be_replaced():
    value = {"prs": [{"number": 7, "state": "pending"}]}
    out, _ = apply_list_ops(
        value,
        remove={"prs": [{"number": 7}]},
        append={"prs": [{"number": 7, "state": "green"}]},
    )
    assert out["prs"] == [{"number": 7, "state": "green"}]


def test_remove_on_a_missing_key_creates_nothing():
    out, changes = apply_list_ops({"a": 1}, remove={"prs": [{"number": 1}]})
    assert "prs" not in out
    assert changes == {"prs": {"appended": 0, "removed": 0}}


@pytest.mark.parametrize("ops", [{"append": {"owner": ["y"]}}, {"remove": {"owner": ["x"]}}])
@pytest.mark.parametrize("held", ["x", None, {"a": 1}, 3])
def test_a_key_that_is_not_a_list_is_refused(ops, held):
    """JSON null included: a field a caller set to null is never overwritten."""
    with pytest.raises(ValueError, match="not a list"):
        apply_list_ops({"owner": held}, **ops)


@pytest.mark.parametrize("bad", [["x"], {"prs": "x"}, {"": [1]}])
def test_a_badly_shaped_operation_is_refused(bad):
    with pytest.raises(ValueError):
        apply_list_ops({}, append=bad)


# ── upsert_state ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_applies_list_ops_to_the_stored_value_and_answers_the_counts(monkeypatch):
    state = _state({"prs": [{"number": 1}], "owner": "x"})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        value={"owner": "y"},
        append={"prs": [{"number": 2}]},
        remove={"prs": [{"number": 1}]},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"prs": [{"number": 2}], "owner": "y"}
    assert out["list_changes"] == {"prs": {"appended": 1, "removed": 1}}
    assert session.committed is True


@pytest.mark.asyncio
async def test_two_runs_appending_to_one_list_keep_both_items(monkeypatch):
    """Each append lands on the list as stored, never on a copy a run read
    earlier: the second run does not drop the first run's item."""
    state = _state({"prs": [{"number": 1}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    for number in (2, 3):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"prs": [{"number": number}]},
            writer=COMPANY_AGENT,
        )
    assert state.value["prs"] == [{"number": 1}, {"number": 2}, {"number": 3}]


@pytest.mark.asyncio
async def test_upsert_refuses_a_non_list_key_and_writes_nothing(monkeypatch):
    state = _state({"owner": "x"})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="not a list"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            label="changed",
            note="a note",
            append={"owner": ["y"]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"owner": "x"}
    assert state.label is None
    assert state.notes == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_upsert_locks_the_row_once_the_write_is_authorised(monkeypatch):
    state = _state({})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    order: list[str] = []
    original = shared_state_service._gate_write

    async def _gate(db, writer, row, action):
        order.append("authorised" if not session.locked else "authorised-locked")
        return await original(db, writer, row, action)

    monkeypatch.setattr(shared_state_service, "_gate_write", _gate)
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()), key="open-prs", value={"a": 1}, writer=COMPANY_AGENT
    )
    assert session.locked == [state]
    assert order == ["authorised", "authorised-locked"]


@pytest.mark.asyncio
async def test_a_row_deleted_before_the_lock_is_created_again_with_the_write(monkeypatch):
    """A concurrent delete between the read and the FOR UPDATE: the upsert looks
    the key up again and creates the entry, so the write is never lost."""
    state = _state({"prs": [1]})
    session = _Session(state, deleted_before_lock=True)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"prs": [2]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert session.rolled_back is True
    assert out["value"] == {"prs": [2]}
    assert out["list_changes"] == {"prs": {"appended": 1, "removed": 0}}
    assert state.value == {"prs": [1]}  # the deleted row was never written
    assert session.committed is True


class _VanishThenOther(_Session):
    """The first row vanishes before the lock; the next lookup finds *other*."""

    def __init__(self, existing, other):
        super().__init__(existing)
        self._other = other
        self._vanished = False

    async def execute(self, statement):
        self.statements.append(statement)
        if getattr(statement, "_for_update_arg", None) is not None:
            if not self._vanished:
                self._vanished = True
                self._existing = self._other
                return _Result(None)
            self.locked.append(self._existing)
        return _Result(self._existing)


@pytest.mark.asyncio
async def test_a_retry_never_redirects_the_write_to_another_scope(monkeypatch):
    """Own row deleted before the lock, a fleet row of the same key visible:
    the write is refused, the fleet row untouched."""
    owner = uuid.uuid4()
    own = _state({"prs": [1]})
    own.user_id, own.is_company_managed = owner, False
    fleet = _state({"prs": [9]})
    session = _VanishThenOther(own, fleet)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="is another one"):
        await shared_state_service.upsert_state(
            user_id=str(owner),
            key="open-prs",
            append={"prs": [2]},
            writer=COMPANY_AGENT,
        )
    assert fleet.value == {"prs": [9]}
    assert own.value == {"prs": [1]}
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_retry_never_redirects_the_write_to_another_row_of_the_same_scope(monkeypatch):
    """A fleet row vanishes before the lock, another creator's fleet row of the
    same key is visible: the write is refused, that row untouched."""
    first = _state({"prs": [1]})
    first.user_id = uuid.uuid4()
    other = _state({"prs": [9]})
    other.user_id = uuid.uuid4()
    session = _VanishThenOther(first, other)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="is another one"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"prs": [2]},
            writer=COMPANY_AGENT,
            is_company_managed=True,
        )
    assert other.value == {"prs": [9]}
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_retry_never_recreates_the_entry_in_another_scope(monkeypatch):
    """A fleet row deleted before the lock is not recreated as a private one."""
    state = _state({"prs": [1]})
    session = _Session(state, deleted_before_lock=True)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="another scope"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"prs": [2]},
            writer=COMPANY_AGENT,
            is_company_managed=False,
        )
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_retry_reads_the_workspace_tiers_again(monkeypatch):
    """A grant revoked during the delete race is honoured by the retry."""
    state = _state({"prs": [1]})
    session = _Session(state, deleted_before_lock=True)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    reads: list[int] = []

    async def _tiers(db, user_id):
        reads.append(1)
        return [], []

    monkeypatch.setattr(shared_state_service, "_workspace_tiers", _tiers)
    await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"prs": [2]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert len(reads) == 2


@pytest.mark.asyncio
async def test_a_retry_that_finds_a_row_again_never_writes_it(monkeypatch):
    """The row a retry finds after a delete is another physical row, even in
    the same scope: refused, nothing written, no endless re-lock."""
    class _AlwaysDeleted(_Session):
        async def execute(self, statement):
            self.statements.append(statement)
            if getattr(statement, "_for_update_arg", None) is not None:
                return _Result(None)
            return _Result(self._existing)

    state = _state({"prs": [1]})
    session = _AlwaysDeleted(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="is another one"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"prs": [2]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"prs": [1]}
    assert session.committed is False


@pytest.mark.asyncio
async def test_a_refused_write_never_takes_the_lock(monkeypatch):
    """A caller that may only read the row is refused before the FOR UPDATE."""
    state = _state({"prs": []})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(CompanyManagedWriteRefusedError):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            append={"prs": [1]},
            writer=PERSONAL_AGENT,
        )
    assert session.locked == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_upsert_creates_a_new_entry_with_its_list(monkeypatch):
    session = _Session(None)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"prs": [{"number": 1}]},
        writer=COMPANY_AGENT,
        is_company_managed=True,
    )
    assert out["value"] == {"prs": [{"number": 1}]}
    assert out["list_changes"] == {"prs": {"appended": 1, "removed": 0}}


# ── set_shared_state tool ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tool_passes_the_list_ops_and_reads_back_the_counts(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {
            "key": kwargs["key"],
            "value": {"prs": [{"number": 2}, {"number": 3}]},
            "notes": [],
            "list_changes": {"prs": {"appended": 1, "removed": 1}},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="open-prs",
        append_json='{"prs": [{"number": 3}]}',
        remove_json='{"prs": [{"number": 1}]}',
    )
    assert not result.is_error
    assert captured["append"] == {"prs": [{"number": 3}]}
    assert captured["remove"] == {"prs": [{"number": 1}]}
    assert captured["value"] is None
    assert 'list "prs": 1 appended, 1 removed, 2 item(s) now' in result.content


@pytest.mark.asyncio
async def test_tool_archives_what_unset_removes_before_the_source_write(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"entries": [{"key": "a", "long": "L"}]}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"entries": [{"key": "a"}]}, "notes": []}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="watches",
        update_json='{"entries": {"match": {"key": "a"}, "unset": ["long"]}}',
        archive_key="watches-archive",
    )
    assert not result.is_error, result.content
    assert [c["key"].endswith("watches-archive") for c in calls] == [True, False], result.content
    archived = calls[0]["append"]["entries"]
    assert archived[0]["match"] == {"key": "a"} and archived[0]["fields"] == {"long": "L"}
    assert calls[1]["update"] == {"entries": {"match": {"key": "a"}, "unset": ["long"]}}
    assert "Archived 1 item(s)" in result.content


def test_apply_list_ops_reports_the_items_remove_and_keep_last_drop_whole():
    value = {"runs": [{"n": i, "x": f"v{i}"} for i in range(5)]}
    dropped: dict = {}
    out, _ = apply_list_ops(value, remove={"runs": [{"n": 0}]}, keep_last={"runs": 2}, dropped=dropped)
    assert out["runs"] == [{"n": 3, "x": "v3"}, {"n": 4, "x": "v4"}]
    assert dropped == {"runs": [{"n": 0, "x": "v0"}, {"n": 1, "x": "v1"}, {"n": 2, "x": "v2"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("ops", "moved"),
    [
        ({"remove_json": json.dumps({"runs": [{"n": i} for i in range(300)]})}, 300),
        ({"keep_last_json": '{"runs": 200}', "confirm_trim": True}, 300),
    ],
)
async def test_tool_moves_removed_or_trimmed_items_whole_to_the_archive_in_one_call(monkeypatch, ops, moved):
    items = [{"n": i, "text": f"t{i}", "extra": {"k": i}} for i in range(500)]
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"runs": items, "other": 1}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="metrics", archive_key="metrics-archive", **ops)
    assert not result.is_error, result.content
    assert [c["key"].endswith("metrics-archive") for c in calls] == [True, False]
    assert calls[0]["append"] == {"runs": items[:moved]}
    assert calls[1]["expect_dropped"] == {"runs": items[:moved]}
    assert f"Archived {moved} item(s) whole" in result.content


@pytest.mark.asyncio
async def test_upsert_refuses_a_list_that_changed_since_the_archive(monkeypatch):
    state = _state({"runs": [{"n": 1}, {"n": 2}, {"n": 3}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="changed between the archive and the write"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            keep_last={"runs": 1},
            expect_dropped={"runs": [{"n": 1}]},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"runs": [{"n": 1}, {"n": 2}, {"n": 3}]}
    assert session.committed is False


@pytest.mark.asyncio
async def test_upsert_removes_top_level_keys_and_names_them(monkeypatch):
    state = _state({"a": 1, "b": [1], "c": {"x": 1}})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        unset_keys=["a", "c", "missing"],
        writer=COMPANY_AGENT,
    )
    assert state.value == {"b": [1]}
    assert out["unset_keys"] == ["a", "c"]


@pytest.mark.asyncio
async def test_tool_passes_unset_keys_and_reads_back_the_keys_removed(monkeypatch):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"b": 1}, "notes": [], "unset_keys": ["a"]}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="k", unset_keys=["a", "z"])
    assert calls[0]["unset_keys"] == ["a", "z"]
    assert "keys removed: a" in result.content
    bad = await _call(executor, "set_shared_state", key="k", unset_keys="a")
    assert "unset_keys must be a list" in bad.content and len(calls) == 1


@pytest.mark.asyncio
async def test_tool_archives_the_top_level_keys_unset_keys_removes_before_the_write(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"a": {"long": "L"}, "b": [1], "keep": 1}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {"keep": 1}, "notes": [], "unset_keys": ["a", "b"]}

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "set_shared_state", key="gaps", unset_keys=["a", "b", "missing"], archive_key="gaps-archive"
    )
    assert not result.is_error, result.content
    assert [c["key"].endswith("gaps-archive") for c in calls] == [True, False], result.content
    entry = calls[0]["append"]["entries"][0]
    assert entry["fields"] == {"a": {"long": "L"}, "b": [1]}
    assert entry["unset_keys"] == ["a", "b"] and entry["source_key"] == "gaps"
    assert calls[1]["unset_keys"] == ["a", "b", "missing"]
    assert "Archived 2 key(s) (a, b)" in result.content


@pytest.mark.asyncio
async def test_tool_writes_nothing_when_the_unset_keys_archive_is_refused(monkeypatch):
    calls: list[dict] = []

    async def _get(**kwargs):
        return {"key": kwargs["key"], "value": {"a": 1}, "notes": []}

    async def _upsert(**kwargs):
        calls.append(kwargs)
        raise ValueError("too large")

    monkeypatch.setattr(shared_state_service, "get_state", _get)
    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="gaps", unset_keys=["a"], archive_key="gaps-archive")
    assert "Nothing was written" in result.content
    assert len(calls) == 1 and calls[0]["key"].endswith("gaps-archive")


def test_write_at_path_writes_a_scalar_or_a_list_under_the_path():
    assert shared_state_service.write_at_path({"a": {"b": {"x": 1}}}, ["a", "b"], "text") == {"a": {"b": "text"}}
    assert shared_state_service.write_at_path({}, ["a"], [1, 2]) == {"a": [1, 2]}
    with pytest.raises(ValueError):
        shared_state_service.write_at_path({}, [], "text")


@pytest.mark.asyncio
async def test_tool_accepts_a_scalar_value_json_with_a_path_only(monkeypatch):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    ok = await _call(executor, "set_shared_state", key="k", path="a.b", value_json='"text"')
    assert not ok.is_error and calls[0]["value"] == "text"
    refused = await _call(executor, "set_shared_state", key="k", value_json='"text"')
    assert "value_json must be a JSON object" in refused.content and len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update_json", "archive_key", "expected"),
    [
        ('{"entries": {"match": {"key": "a"}, "set": {"x": 1}}}', "other", "this write drops nothing"),
        ('{"entries": {"match": {"key": "a"}, "unset": ["x"]}}', "watches", "another key"),
    ],
)
async def test_tool_refuses_an_archive_without_unset_or_onto_itself(monkeypatch, update_json, archive_key, expected):
    calls: list[dict] = []

    async def _upsert(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor, "set_shared_state", key="watches", update_json=update_json, archive_key=archive_key
    )
    assert expected in result.content
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argument", "raw", "expected"),
    [
        ("append_json", "{not json", "append_json is not valid JSON"),
        ("remove_json", "[1]", "remove_json must be a JSON object"),
        ("append_json", '{"prs": {"number": 1}}', "must map each top-level key to a list"),
        ("append_json", [{"prs": [1]}], "append_json must be a string holding a JSON object"),
        ("remove_json", 3, "remove_json must be a string holding a JSON object"),
        ("remove_json", {"prs": [1]}, "remove_json must be a string holding a JSON object"),
    ],
)
async def test_tool_refuses_bad_list_ops_before_any_write(monkeypatch, argument, raw, expected):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="open-prs", **{argument: raw})
    assert expected in result.content
    assert called is False


@pytest.mark.asyncio
async def test_tool_answers_a_refused_non_list_key(monkeypatch):
    async def _upsert(**kwargs):
        raise ValueError("the value under 'owner' is not a list, so it cannot be appended to or removed from; nothing was written")

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="open-prs", append_json='{"owner": ["y"]}')
    assert result.content.startswith("Error: the value under 'owner' is not a list")


def test_the_tool_schema_declares_the_list_ops():
    defs, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    props = next(d for d in defs if d.name == "set_shared_state").input_schema["properties"]
    assert {"append_json", "remove_json", "keep_last_json", "unique_by_json"} <= set(props)


# ── keep_last ───────────────────────────────────────────────────────


def test_keep_last_trims_the_oldest_items_after_the_append():
    value = {"runs": [1, 2, 3, 4], "owner": "x"}
    out, changes = apply_list_ops(value, append={"runs": [5]}, keep_last={"runs": 3})
    assert out["runs"] == [3, 4, 5]
    assert out["owner"] == "x"
    assert changes == {"runs": {"appended": 1, "removed": 0, "trimmed": 2}}
    assert value["runs"] == [1, 2, 3, 4]  # the input is not mutated


def test_keep_last_under_the_cap_or_on_a_missing_key_changes_nothing():
    out, changes = apply_list_ops({"runs": [1, 2]}, keep_last={"runs": 30, "gone": 5})
    assert out == {"runs": [1, 2]}
    assert changes == {
        "runs": {"appended": 0, "removed": 0, "trimmed": 0},
        "gone": {"appended": 0, "removed": 0, "trimmed": 0},
    }


def test_keep_last_zero_empties_the_list():
    out, changes = apply_list_ops({"runs": [1, 2]}, keep_last={"runs": 0})
    assert out["runs"] == []
    assert changes["runs"]["trimmed"] == 2


@pytest.mark.parametrize("held", ["x", None, {"a": 1}])
def test_keep_last_on_a_key_that_is_not_a_list_is_refused(held):
    with pytest.raises(ValueError, match="not a list"):
        apply_list_ops({"runs": held}, keep_last={"runs": 3})


@pytest.mark.parametrize("bad", [["runs"], {"runs": -1}, {"runs": "3"}, {"runs": True}, {"": 3}])
def test_a_badly_shaped_keep_last_is_refused(bad):
    with pytest.raises(ValueError, match="keep_last"):
        apply_list_ops({"runs": [1]}, keep_last=bad)


@pytest.mark.asyncio
async def test_upsert_trims_the_stored_list_under_the_lock(monkeypatch):
    state = _state({"runs": [{"n": i} for i in range(5)]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="metrics",
        append={"runs": [{"n": 5}]},
        keep_last={"runs": 2},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"runs": [{"n": 4}, {"n": 5}]}
    assert out["list_changes"] == {"runs": {"appended": 1, "removed": 0, "trimmed": 4}}
    assert session.locked == [state]
    assert session.committed is True


@pytest.mark.asyncio
async def test_tool_passes_keep_last_and_reads_back_the_trimmed_count(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {
            "key": kwargs["key"],
            "value": {"runs": [1, 2, 3]},
            "notes": [],
            "list_changes": {"runs": {"appended": 1, "removed": 0, "trimmed": 734}},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="metrics",
        append_json='{"runs": [3]}',
        keep_last_json='{"runs": 3}',
    )
    assert not result.is_error
    assert captured["keep_last"] == {"runs": 3}
    assert 'list "runs": 1 appended, 0 removed, 734 trimmed, 3 item(s) now' in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"runs": -1}', '{"runs": "30"}', '{"runs": [30]}', "[30]"])
async def test_tool_refuses_a_bad_keep_last_before_any_write(monkeypatch, raw):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="metrics", keep_last_json=raw)
    assert result.content.startswith("Error: keep_last_json")
    assert called is False


def test_a_guarded_first_cap_on_a_long_list_is_refused_naming_the_count():
    value = {"runs": list(range(522))}
    with pytest.raises(ValueError, match=r"would drop 323 of 523 item\(s\) while this call appends 1"):
        apply_list_ops(value, append={"runs": [522]}, keep_last={"runs": 200}, guard_trim=True)
    assert len(value["runs"]) == 522  # untouched


def test_an_unguarded_first_cap_trims_the_long_list():
    out, changes = apply_list_ops(
        {"runs": list(range(522))}, append={"runs": [522]}, keep_last={"runs": 200}
    )
    assert out["runs"] == list(range(323, 523))
    assert changes["runs"]["trimmed"] == 323


def test_a_guarded_rolling_cap_trims_what_it_appends():
    out, changes = apply_list_ops(
        {"runs": [1, 2, 3]}, append={"runs": [4, 5]}, keep_last={"runs": 3}, guard_trim=True
    )
    assert out["runs"] == [3, 4, 5]
    assert changes["runs"] == {"appended": 2, "removed": 0, "trimmed": 2}


@pytest.mark.asyncio
@pytest.mark.parametrize(("confirm", "guarded"), [(None, True), (False, True), (True, False)])
async def test_tool_guards_the_trim_unless_confirmed(monkeypatch, confirm, guarded):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "value": {"runs": [1]}, "notes": []}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    extra = {} if confirm is None else {"confirm_trim": confirm}
    result = await _call(
        executor, "set_shared_state", key="metrics", append_json='{"runs": [1]}', keep_last_json='{"runs": 200}', **extra
    )
    assert not result.is_error, result.content
    assert captured["guard_trim"] is guarded


@pytest.mark.asyncio
async def test_upsert_refuses_a_guarded_first_cap_and_writes_nothing(monkeypatch):
    state = _state({"runs": [{"n": i} for i in range(522)]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="would drop 323"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="metrics",
            append={"runs": [{"n": 522}]},
            keep_last={"runs": 200},
            guard_trim=True,
            writer=COMPANY_AGENT,
        )
    assert len(state.value["runs"]) == 522
    assert session.committed is not True


# ── unique_by ───────────────────────────────────────────────────────


def test_unique_by_replaces_the_item_with_the_same_key_in_place():
    value = {"prs": [{"number": 1, "at": "22:42Z"}, {"number": 2, "at": "a"}]}
    out, changes = apply_list_ops(
        value,
        append={"prs": [{"number": 1, "at": "22:42:00Z", "ui": True}, {"number": 3}]},
        unique_by={"prs": "number"},
    )
    assert out["prs"] == [{"number": 1, "at": "22:42:00Z", "ui": True}, {"number": 2, "at": "a"}, {"number": 3}]
    assert changes == {"prs": {"appended": 1, "removed": 0, "replaced": 1}}
    assert value["prs"][0] == {"number": 1, "at": "22:42Z"}  # the input is not mutated


def test_unique_by_collapses_existing_duplicates_of_the_key():
    value = {"prs": [{"number": 8, "at": "x"}, {"number": 9}, {"number": 8, "at": "y"}]}
    out, changes = apply_list_ops(value, append={"prs": [{"number": 8, "at": "z"}]}, unique_by={"prs": "number"})
    assert out["prs"] == [{"number": 8, "at": "z"}, {"number": 9}]
    assert changes == {"prs": {"appended": 0, "removed": 1, "replaced": 1}}


def test_unique_by_an_identical_item_counts_nothing():
    out, changes = apply_list_ops({"prs": [{"number": 1}]}, append={"prs": [{"number": 1}]}, unique_by={"prs": "number"})
    assert out["prs"] == [{"number": 1}]
    assert changes == {"prs": {"appended": 0, "removed": 0, "replaced": 0}}


def test_unique_by_several_fields_and_items_lacking_one_keep_the_equality_rule():
    value = {"prs": [{"repository": "a", "number": 1}, {"repository": "b", "number": 1}, "plain"]}
    out, changes = apply_list_ops(
        value,
        append={"prs": [{"repository": "b", "number": 1, "hold": True}, {"number": 1}, "plain"]},
        unique_by={"prs": ["repository", "number"]},
    )
    assert out["prs"] == [
        {"repository": "a", "number": 1},
        {"repository": "b", "number": 1, "hold": True},
        "plain",
        {"number": 1},
    ]
    assert changes == {"prs": {"appended": 1, "removed": 0, "replaced": 1}}


def test_without_unique_by_a_near_duplicate_is_still_appended():
    out, _ = apply_list_ops({"prs": [{"number": 1, "at": "a"}]}, append={"prs": [{"number": 1, "at": "b"}]})
    assert len(out["prs"]) == 2


@pytest.mark.parametrize("bad", [["prs"], {"prs": ""}, {"prs": []}, {"prs": [1]}, {"": "number"}, {"prs": 3}])
def test_a_badly_shaped_unique_by_is_refused(bad):
    with pytest.raises(ValueError, match="unique_by"):
        apply_list_ops({"prs": []}, append={"prs": [{"number": 1}]}, unique_by=bad)


def test_a_checked_unique_by_passes_the_check_again():
    from app.services.shared_state import _unique_by_shape

    once = _unique_by_shape({"prs": "number", "runs": ["repo", "id"]})
    assert _unique_by_shape(once) == once == {"prs": ("number",), "runs": ("repo", "id")}


@pytest.mark.asyncio
async def test_upsert_replaces_the_keyed_item_under_the_lock(monkeypatch):
    state = _state({"prs": [{"number": 5, "opened_at": "22:42Z"}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"prs": [{"number": 5, "opened_at": "22:43:00Z"}]},
        unique_by={"prs": "number"},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"prs": [{"number": 5, "opened_at": "22:43:00Z"}]}
    assert out["list_changes"] == {"prs": {"appended": 0, "removed": 0, "replaced": 1}}
    assert session.locked == [state]


@pytest.mark.asyncio
async def test_tool_passes_unique_by_and_reads_back_the_replaced_count(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {
            "key": kwargs["key"],
            "value": {"prs": [{"number": 5}]},
            "notes": [],
            "list_changes": {"prs": {"appended": 0, "removed": 2, "replaced": 1}},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="open-prs",
        append_json='{"prs": [{"number": 5}]}',
        unique_by_json='{"prs": "number"}',
    )
    assert not result.is_error
    assert captured["unique_by"] == {"prs": "number"}
    assert 'list "prs": 0 appended, 2 removed, 1 replaced, 1 item(s) now' in result.content


def test_unique_by_reports_what_a_replacement_overwrote() -> None:
    # Another run wrote item 7 (count, note) after this caller read no match.
    value = {"gaps": [{"id": "g7", "count": 3, "note": "x", "state": "queued"}]}
    replaced: dict[str, list[dict[str, object]]] = {}
    out, changes = apply_list_ops(
        value,
        append={"gaps": [{"id": "g7", "state": "open"}, {"id": "g8"}]},
        unique_by={"gaps": "id"},
        replaced_items=replaced,
    )
    assert out["gaps"] == [{"id": "g7", "state": "open"}, {"id": "g8"}]
    assert changes == {"gaps": {"appended": 1, "removed": 0, "replaced": 1}}
    assert replaced == {"gaps": [{"identity": {"id": "g7"}, "lost": ["count", "note"], "changed": ["state"]}]}


def test_an_identical_unique_by_item_records_no_replacement() -> None:
    replaced: dict[str, list[dict[str, object]]] = {}
    apply_list_ops(
        {"prs": [{"number": 1}]}, append={"prs": [{"number": 1}]}, unique_by={"prs": "number"}, replaced_items=replaced
    )
    assert replaced == {}


@pytest.mark.asyncio
async def test_upsert_answers_the_replaced_items(monkeypatch):
    state = _state({"prs": [{"number": 5, "hold": True}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        append={"prs": [{"number": 5}]},
        unique_by={"prs": "number"},
        writer=COMPANY_AGENT,
    )
    assert out["replaced_items"] == {"prs": [{"identity": {"number": 5}, "lost": ["hold"], "changed": []}]}


@pytest.mark.asyncio
async def test_tool_reads_back_the_fields_a_replacement_dropped(monkeypatch):
    async def _upsert(**kwargs):
        return {
            "key": kwargs["key"],
            "value": {"prs": [{"number": 5}]},
            "notes": [],
            "list_changes": {"prs": {"appended": 0, "removed": 0, "replaced": 1}},
            "replaced_items": {"prs": [{"identity": {"number": 5}, "lost": ["hold", "runs"], "changed": []}]},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="open-prs",
        append_json='{"prs": [{"number": 5}]}',
        unique_by_json='{"prs": "number"}',
    )
    assert not result.is_error
    assert 'replaced {"number": 5}: fields dropped hold, runs; fields changed none' in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"prs": 3}', '{"prs": ""}', '{"prs": []}', '["number"]'])
async def test_tool_refuses_a_bad_unique_by_before_any_write(monkeypatch, raw):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="open-prs", unique_by_json=raw)
    assert result.content.startswith("Error: unique_by_json")
    assert called is False


# ── update: a field-level patch on the matched items ──────────────────────


def test_update_increments_sets_and_appends_on_the_matched_item_only():
    value = {
        "gaps": [
            {"slug": "cold-start", "count": 25, "runs": ["a"], "notes": "long" * 100},
            {"slug": "other", "count": 1},
        ]
    }
    out, changes = apply_list_ops(
        value,
        update={
            "gaps": {
                "match": {"slug": "cold-start"},
                "set": {"last_seen": "2026-11-05"},
                "increment": {"count": 1},
                "append": {"runs": ["a", "b"]},
            }
        },
    )
    first, second = out["gaps"]
    assert first["count"] == 26
    assert first["runs"] == ["a", "b"]
    assert first["last_seen"] == "2026-11-05"
    assert first["notes"] == "long" * 100  # the rest of the item is kept
    assert second == {"slug": "other", "count": 1}
    assert changes == {"gaps": {"appended": 0, "removed": 0, "updated": 1}}
    assert value["gaps"][0]["count"] == 25  # the input is not mutated


def test_update_starts_a_missing_counter_at_zero_and_creates_a_missing_list():
    out, _ = apply_list_ops(
        {"gaps": [{"slug": "x", "count": None}]},
        update={"gaps": {"match": {"slug": "x"}, "increment": {"count": 2, "seen": 0.5}, "append": {"runs": ["r"]}}},
    )
    assert out["gaps"] == [{"slug": "x", "count": 2, "seen": 0.5, "runs": ["r"]}]


def test_update_patches_every_matched_item_and_takes_a_list_of_patches():
    out, changes = apply_list_ops(
        {"prs": [{"repo": "a", "n": 1}, {"repo": "a", "n": 2}, {"repo": "b", "n": 3}]},
        update={
            "prs": [
                {"match": {"repo": "a"}, "set": {"state": "green"}},
                {"match": {"n": 3}, "set": {"state": "red"}},
            ]
        },
    )
    assert [p["state"] for p in out["prs"]] == ["green", "green", "red"]
    assert changes["prs"]["updated"] == 3


def test_update_that_changes_nothing_counts_zero():
    out, changes = apply_list_ops(
        {"gaps": [{"slug": "x", "runs": ["r"]}]},
        update={"gaps": {"match": {"slug": "x"}, "append": {"runs": ["r"]}}},
    )
    assert out["gaps"] == [{"slug": "x", "runs": ["r"]}]
    assert changes["gaps"]["updated"] == 0


def test_update_unset_drops_fields_of_the_matched_item_only():
    out, changes = apply_list_ops(
        {"e": [{"key": "a", "long": "x" * 50, "keep": 1}, {"key": "b", "long": "y"}]},
        update={"e": {"match": {"key": "a"}, "unset": ["long", "absent"], "set": {"short": "s"}}},
    )
    assert out["e"] == [{"key": "a", "keep": 1, "short": "s"}, {"key": "b", "long": "y"}]
    assert changes["e"]["updated"] == 1


def test_update_unset_of_an_absent_field_counts_zero():
    out, changes = apply_list_ops(
        {"e": [{"key": "a"}]}, update={"e": {"match": {"key": "a"}, "unset": ["long"]}}
    )
    assert out["e"] == [{"key": "a"}]
    assert changes["e"]["updated"] == 0


@pytest.mark.parametrize("bad", ["long", [""], [1], {"long": 1}])
def test_update_unset_must_be_a_list_of_field_names(bad):
    with pytest.raises(ValueError, match="unset must be a list of field names"):
        apply_list_ops({"e": [{"key": "a"}]}, update={"e": {"match": {"key": "a"}, "unset": bad}})


def test_unset_archive_lists_the_removed_values_with_the_item_identity():
    value = {"e": [{"key": "a", "long": "L", "other": "O"}, {"key": "b", "long": "M"}, {"key": "c"}]}
    archived = unset_archive(
        value,
        {"e": {"match": [{"key": "a"}, {"key": "c"}], "unset": ["long", "other"]}},
        source_key="src",
        at="2026-11-06T00:00:00Z",
    )
    assert archived == [
        {
            "source_key": "src",
            "list": "e",
            "match": {"key": "a"},
            "archived_at": "2026-11-06T00:00:00Z",
            "fields": {"long": "L", "other": "O"},
        }
    ]


def test_unset_archive_keeps_the_item_key_on_a_bulk_match():
    value = {
        "entries": [
            {"key": "w-1", "deploy": "DEPLOYED", "detail": "retest A"},
            {"key": "w-2", "deploy": "DEPLOYED", "detail": "retest B"},
            {"key": "w-3", "deploy": "WATCH", "detail": "retest C"},
        ]
    }
    archived = unset_archive(
        value,
        {"entries": {"match": {"deploy": "DEPLOYED"}, "unset": ["detail"]}},
        source_key="src",
        at="t",
    )
    assert [a["match"] for a in archived] == [
        {"deploy": "DEPLOYED", "key": "w-1"},
        {"deploy": "DEPLOYED", "key": "w-2"},
    ]
    assert [a["fields"] for a in archived] == [{"detail": "retest A"}, {"detail": "retest B"}]


def test_unset_archive_keeps_the_retention_item_key():
    value = {"rows": [{"ref": "r-9", "state": "done", "note": "N"}]}
    archived = unset_archive(
        value,
        {"rows": {"match": {"state": "done"}, "unset": ["note"]}},
        source_key="src",
        at="t",
        item_key="ref",
    )
    assert archived[0]["match"] == {"state": "done", "ref": "r-9"}


def test_unset_archive_is_empty_without_unset_or_list():
    no_unset = {"e": {"match": {"key": "a"}, "set": {"y": 1}}}
    assert unset_archive({"e": [{"key": "a", "x": 1}]}, no_unset, source_key="s", at="t") == []
    unset = {"e": {"match": {"key": "a"}, "unset": ["x"]}}
    assert unset_archive({}, unset, source_key="s", at="t") == []


@pytest.mark.parametrize(
    ("value", "update", "message"),
    [
        ({"gaps": [{"slug": "x"}]}, {"gaps": {"match": {"slug": "y"}, "set": {"a": 1}}}, "no item of the list matches"),
        ({}, {"gaps": {"match": {"slug": "y"}, "set": {"a": 1}}}, "no item of the list matches"),
        ({"gaps": [{"slug": "x", "count": "3"}]}, {"gaps": {"match": {"slug": "x"}, "increment": {"count": 1}}}, "not a number"),
        ({"gaps": [{"slug": "x", "runs": "r"}]}, {"gaps": {"match": {"slug": "x"}, "append": {"runs": ["s"]}}}, "not a list"),
        ({"gaps": {"slug": "x"}}, {"gaps": {"match": {"slug": "x"}, "set": {"a": 1}}}, "not a list"),
    ],
)
def test_update_refuses_and_leaves_the_value_untouched(value, update, message):
    with pytest.raises(ValueError, match=message):
        apply_list_ops(value, update=update)


@pytest.mark.parametrize(
    "bad",
    [
        ["gaps"],
        {"gaps": []},
        {"gaps": "x"},
        {"gaps": {"set": {"a": 1}}},
        {"gaps": {"match": {}, "set": {"a": 1}}},
        {"gaps": {"match": {"slug": "x"}}},
        {"gaps": {"match": {"slug": "x"}, "increment": {"count": "1"}}},
        {"gaps": {"match": {"slug": "x"}, "increment": {"count": True}}},
        {"gaps": {"match": {"slug": "x"}, "append": {"runs": "r"}}},
        {"gaps": {"match": {"slug": "x"}, "set": [1]}},
        {"gaps": {"match": {"slug": "x"}, "delete": {"a": 1}}},
    ],
)
def test_a_badly_shaped_update_is_refused(bad):
    with pytest.raises(ValueError, match="update"):
        apply_list_ops({"gaps": [{"slug": "x"}]}, update=bad)


@pytest.mark.asyncio
async def test_upsert_moves_the_counter_under_the_lock(monkeypatch):
    state = _state({"gaps": [{"slug": "cold-start", "count": 25}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    out = await shared_state_service.upsert_state(
        user_id=str(uuid.uuid4()),
        key="open-prs",
        update={"gaps": {"match": {"slug": "cold-start"}, "increment": {"count": 1}}},
        writer=COMPANY_AGENT,
    )
    assert state.value == {"gaps": [{"slug": "cold-start", "count": 26}]}
    assert out["list_changes"] == {"gaps": {"appended": 0, "removed": 0, "updated": 1}}
    assert session.locked == [state]


@pytest.mark.asyncio
async def test_upsert_refuses_an_update_matching_nothing_and_writes_nothing(monkeypatch):
    state = _state({"gaps": [{"slug": "cold-start", "count": 25}]})
    session = _Session(state)
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    with pytest.raises(ValueError, match="no item of the list matches"):
        await shared_state_service.upsert_state(
            user_id=str(uuid.uuid4()),
            key="open-prs",
            update={"gaps": {"match": {"slug": "typo"}, "increment": {"count": 1}}},
            writer=COMPANY_AGENT,
        )
    assert state.value == {"gaps": [{"slug": "cold-start", "count": 25}]}
    assert session.committed is False


@pytest.mark.asyncio
async def test_tool_passes_update_and_reads_back_the_updated_count(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {
            "key": kwargs["key"],
            "value": {"gaps": [{"slug": "x", "count": 26}]},
            "notes": [],
            "list_changes": {"gaps": {"appended": 0, "removed": 0, "updated": 1}},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="harness-gaps",
        update_json='{"gaps": {"match": {"slug": "x"}, "increment": {"count": 1}}}',
    )
    assert not result.is_error
    assert captured["update"] == {"gaps": {"match": {"slug": "x"}, "increment": {"count": 1}}}
    assert 'list "gaps": 0 appended, 0 removed, 1 updated, 1 item(s) now' in result.content


@pytest.mark.asyncio
async def test_tool_counts_a_list_changed_under_a_write_path(monkeypatch):
    async def _upsert(**kwargs):
        return {
            "key": kwargs["key"],
            "value": {"pools": {"opencti": {"open": [{"number": 1}, {"number": 2}]}}},
            "notes": [],
            "list_changes": {"pools.opencti.open": {"appended": 1, "removed": 0}},
        }

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="pr-pools",
        path="pools.opencti",
        append_json='{"open": [{"number": 2}]}',
    )
    assert not result.is_error
    assert 'list "pools.opencti.open": 1 appended, 0 removed, 2 item(s) now' in result.content


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ['{"gaps": 3}', '{"gaps": []}', '{"gaps": [1]}', '["gaps"]', "{"])
async def test_tool_refuses_a_bad_update_before_any_write(monkeypatch, raw):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(executor, "set_shared_state", key="harness-gaps", update_json=raw)
    assert result.content.startswith("Error: update_json")
    assert called is False


def test_the_tool_schema_declares_update_json():
    definitions, _ = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    props = next(d for d in definitions if d.name == "set_shared_state").input_schema["properties"]
    assert "update_json" in props
    assert "after the removals, before the appends" in props["update_json"]["description"]
    # A list match is any-of for the patch but every object must match (#9801).
    assert "one matching nothing refuses all" in props["update_json"]["description"]


# ── update_json with append_json in one call (#8845) ───────────────────────


_PAIR_UPDATE = '{"gaps": {"match": {"id": "a"}, "increment": {"count": 1}, "append": {"runs": ["r2"]}}}'
_PAIR_APPEND = '{"gaps": [{"id": "b", "count": 1, "runs": ["r2"]}]}'


def test_update_and_append_on_the_same_list_apply_together():
    value = {"gaps": [{"id": "a", "count": 3, "runs": ["r1"]}]}
    out, changes = apply_list_ops(
        value,
        append=json.loads(_PAIR_APPEND),
        update=json.loads(_PAIR_UPDATE),
        unique_by={"gaps": "id"},
    )
    assert out["gaps"] == [
        {"id": "a", "count": 4, "runs": ["r1", "r2"]},
        {"id": "b", "count": 1, "runs": ["r2"]},
    ]
    assert changes["gaps"]["appended"] == 1
    assert changes["gaps"]["updated"] == 1


def test_update_and_append_on_two_lists_of_one_entry_apply_together():
    value = {"gaps": [{"id": "a", "count": 3}], "runs": []}
    out, changes = apply_list_ops(
        value,
        append={"runs": [{"run": "r2"}]},
        update={"gaps": {"match": {"id": "a"}, "increment": {"count": 1}}},
    )
    assert out == {"gaps": [{"id": "a", "count": 4}], "runs": [{"run": "r2"}]}
    assert changes["gaps"]["updated"] == 1
    assert changes["runs"]["appended"] == 1


@pytest.mark.asyncio
async def test_tool_passes_update_and_append_in_one_call(monkeypatch):
    captured: dict = {}

    async def _upsert(**kwargs):
        captured.update(kwargs)
        return {"key": kwargs["key"], "value": {}, "notes": [], "list_changes": {}}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    result = await _call(
        executor,
        "set_shared_state",
        key="harness-gaps",
        update_json=_PAIR_UPDATE,
        append_json=_PAIR_APPEND,
        unique_by_json='{"gaps": "id"}',
    )
    assert not result.is_error
    assert captured["update"] == json.loads(_PAIR_UPDATE)
    assert captured["append"] == json.loads(_PAIR_APPEND)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad", "good"),
    [("append_json", "update_json"), ("update_json", "append_json")],
)
async def test_an_invalid_argument_of_the_pair_is_refused_naming_itself_with_an_excerpt(monkeypatch, bad, good):
    called = False

    async def _upsert(**kwargs):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(shared_state_service, "upsert_state", _upsert)
    _, executor = build_shared_state_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))
    valid = {"append_json": _PAIR_APPEND, "update_json": _PAIR_UPDATE}
    broken = valid[bad].replace('"count": 1', '"count" 1', 1)
    result = await _call(executor, "set_shared_state", key="harness-gaps", **{bad: broken, good: valid[good]})
    assert result.content.startswith(f"Error: {bad} is not valid JSON (Expecting ':' delimiter at char ")
    assert good not in result.content
    assert '"count" >>>1<<<' in result.content
    assert result.content.endswith("Nothing was written.")
    assert called is False


def test_the_json_excerpt_is_bounded_on_a_long_argument():
    from app.services.builtin_tools.shared_state import json_argument_error

    raw = '{"gaps": [' + '"x", ' * 200 + '"y" "z"]}'
    try:
        json.loads(raw)
    except json.JSONDecodeError as exc:
        message = json_argument_error("append_json", raw, exc)
    assert message.startswith("Error: append_json is not valid JSON (Expecting ',' delimiter")
    assert '"y" >>>"<<<z"]}' in message
    assert "..." in message
    assert len(message) < 220
