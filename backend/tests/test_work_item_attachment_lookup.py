"""A file a sibling work item attaches, read by its id or an id prefix.

A brief cites the files of another item ("Generator gen.py: bd68673c");
``read_file_content`` resolves such an id against the attachments of the work
items the agent may read, without opening that item first. One file resolves,
several are refused with the candidates, none (or one the agent may not read)
keeps the usual not found.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.services.work_item as wi_service
import app.services.work_item_files as wi_files
from app.services import coordination_scope

AGENT = str(uuid.uuid4())
USER = str(uuid.uuid4())


def _row(name: str, row_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=row_id or uuid.uuid4(),
        filename=name,
        content_type="text/x-python",
        size=42,
        s3_object_name=f"runs/x/{name}",
        source="code_interpreter",
        description=None,
    )


class _Scope:
    """A coordination scope admitting the items whose id it lists."""

    def __init__(self, admitted: set[str] | None = None) -> None:
        self.admitted = admitted

    def admits(self, item) -> bool:
        return self.admitted is None or item["id"] in self.admitted


@pytest.fixture
def world(monkeypatch):
    """Visible items, stored rows, the agent's scope and admin flag, as the test sets them."""
    state = SimpleNamespace(items=[], rows={}, scope=_Scope(), admin=False, prefixes=[])

    async def _attaching(prefix, *, user_id, reader=None):
        state.prefixes.append(prefix)
        assert user_id == USER
        assert reader is not None and str(reader.agent_id) == AGENT
        return [i for i in state.items if any(str(f).startswith(prefix) for f in i["attachment_file_ids"])]

    async def _stored(ids):
        return {i: state.rows[i] for i in ids if i in state.rows}

    async def _resolve_scope(agent_id, user_id, requested):
        return state.scope

    async def _is_admin(user_id):
        return state.admin

    monkeypatch.setattr(wi_service, "work_items_attaching", _attaching)
    monkeypatch.setattr(wi_files, "_stored_rows", _stored)
    monkeypatch.setattr(coordination_scope, "resolve_coordination_scope", _resolve_scope)
    monkeypatch.setattr(coordination_scope, "user_is_admin", _is_admin)
    return state


def _attach(state, *rows) -> dict:
    item = {"id": str(uuid.uuid4()), "attachment_file_ids": [str(r.id) for r in rows]}
    state.items.append(item)
    for r in rows:
        state.rows[r.id] = r
    return item


async def _lookup(file_id):
    return await wi_files.resolve_readable_attachment(file_id, user_id=USER, agent_id=AGENT)


async def test_a_prefix_of_another_items_attachment_resolves(world):
    gen = _row("gen.py")
    _attach(world, gen)
    ref, refusal = await _lookup(str(gen.id)[:8])
    assert refusal is None
    assert ref is not None and ref.file_id == str(gen.id) and ref.filename == "gen.py"


async def test_the_full_id_and_the_marker_resolve_too(world):
    gen = _row("gen.py")
    _attach(world, gen)
    for spelled in (str(gen.id), f"[[FILE:{gen.id}]]", f"file-{gen.id}"):
        ref, refusal = await _lookup(spelled)
        assert refusal is None and ref is not None and ref.file_id == str(gen.id), spelled


async def test_an_ambiguous_prefix_is_refused_with_the_candidates(world):
    first = _row("a.py", uuid.UUID("bd68673c-0000-4000-8000-000000000001"))
    second = _row("b.py", uuid.UUID("bd68673c-0000-4000-8000-000000000002"))
    _attach(world, first)
    _attach(world, second)
    ref, refusal = await _lookup("bd68673c")
    assert ref is None
    assert refusal is not None and "a.py" in refusal and "b.py" in refusal
    assert str(first.id) in refusal and str(second.id) in refusal


async def test_the_same_file_on_two_items_is_one_file(world):
    gen = _row("gen.py")
    _attach(world, gen)
    _attach(world, gen)
    ref, refusal = await _lookup(str(gen.id)[:8])
    assert refusal is None and ref is not None and ref.file_id == str(gen.id)


async def test_a_prefix_that_matches_nothing_is_not_found(world):
    _attach(world, _row("gen.py", uuid.UUID("11111111-0000-4000-8000-000000000001")))
    assert await _lookup("22222222") == (None, None)


async def test_an_attachment_whose_row_is_gone_is_not_found(world):
    gone = _row("gone.py")
    item = _attach(world, gone)
    world.rows.clear()
    assert item["attachment_file_ids"]
    assert await _lookup(str(gone.id)[:8]) == (None, None)


async def test_an_item_outside_the_agents_fleet_grants_nothing(world):
    gen = _row("gen.py")
    _attach(world, gen)
    world.scope = _Scope(admitted=set())
    assert await _lookup(str(gen.id)[:8]) == (None, None)


async def test_an_administrator_reads_past_the_fleet_as_get_work_item_does(world):
    gen = _row("gen.py")
    _attach(world, gen)
    world.scope = _Scope(admitted=set())
    world.admin = True
    ref, _ = await _lookup(str(gen.id)[:8])
    assert ref is not None and ref.file_id == str(gen.id)


@pytest.mark.parametrize("spelled", ["", "gen.py", "bd68", "not-an-id-at-all", None])
async def test_a_filename_or_a_short_prefix_reads_nothing(world, spelled):
    assert await _lookup(spelled) == (None, None)
    assert world.prefixes == []


async def test_no_agent_or_no_user_reads_nothing(world):
    gen = _row("gen.py")
    _attach(world, gen)
    assert await wi_files.resolve_readable_attachment(str(gen.id), user_id=USER, agent_id=None) == (None, None)
    assert await wi_files.resolve_readable_attachment(str(gen.id), user_id=None, agent_id=AGENT) == (None, None)
    assert world.prefixes == []


async def test_a_failed_lookup_reads_as_not_found(world, monkeypatch):
    async def _boom(prefix, *, user_id, reader=None):
        raise RuntimeError("database down")

    monkeypatch.setattr(wi_service, "work_items_attaching", _boom)
    assert await _lookup("bd68673c") == (None, None)


async def test_read_file_content_reads_a_sibling_items_file_by_prefix(world, monkeypatch):
    from app.services.builtin_tools import _shared as shared_tools
    from app.services.builtin_tools import build_generate_file_tool
    from app.tools.base import FileContext, ToolCall

    gen = _row("gen.py")
    _attach(world, gen)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    async def _bytes(self, file_id):
        return b"print('hello')\n" if file_id == str(gen.id) else None

    monkeypatch.setattr(shared_tools, "load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr("app.services.builtin_tools.media._load_scoped_file_ref", _not_scoped)
    monkeypatch.setattr(FileContext, "read_bytes", _bytes)

    _defs, executor = build_generate_file_tool(
        None, USER, None, assignment_run_id=str(uuid.uuid4()), agent_id=AGENT, agent_company_managed=True
    )
    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(id="c1", name="read_file_content", arguments={"file_id": str(gen.id)[:8]})
    )
    assert not result.is_error, result.content
    assert "print('hello')" in result.content
    assert executor.file_context.get(str(gen.id)) is not None


async def test_read_file_content_without_an_agent_keeps_not_found(world, monkeypatch):
    from app.services.builtin_tools import build_generate_file_tool
    from app.tools.base import FileContext, ToolCall

    gen = _row("gen.py")
    _attach(world, gen)

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    monkeypatch.setattr("app.services.builtin_tools.media._load_scoped_file_ref", _not_scoped)
    _defs, executor = build_generate_file_tool(None, USER, None, assignment_run_id=str(uuid.uuid4()))
    executor.file_context = FileContext()
    result = await executor.execute_tool(
        ToolCall(id="c1", name="read_file_content", arguments={"file_id": str(gen.id)[:8]})
    )
    assert result.is_error
    assert "is attached to this conversation" in result.content
    assert world.prefixes == []


async def test_read_file_content_answers_the_ambiguity(world, monkeypatch):
    from app.services.builtin_tools import build_generate_file_tool
    from app.tools.base import FileContext, ToolCall

    _attach(world, _row("a.py", uuid.UUID("bd68673c-0000-4000-8000-000000000001")))
    _attach(world, _row("b.py", uuid.UUID("bd68673c-0000-4000-8000-000000000002")))

    async def _not_scoped(file_id, *, conversation_id, assignment_run_id):
        return None

    monkeypatch.setattr("app.services.builtin_tools.media._load_scoped_file_ref", _not_scoped)
    _defs, executor = build_generate_file_tool(None, USER, None, agent_id=AGENT)
    executor.file_context = FileContext()
    result = await executor.execute_tool(ToolCall(id="c1", name="read_file_content", arguments={"file_id": "bd68673c"}))
    assert result.is_error
    assert "a.py" in result.content and "b.py" in result.content
