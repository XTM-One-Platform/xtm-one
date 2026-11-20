"""Built-in tools take an unambiguous id prefix of 8+ hexadecimal characters.

``builtin_tools/id_prefix.py`` parses the prefix and resolves it among the
objects the caller may see; the work-item, agent-builder, knowledge and
browser-profile tools call it before their own read.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

import app.services.work_item as wi_service
from app.models.agent_skill import AgentSkill
from app.services.builtin_tools import build_work_item_tools
from app.services.builtin_tools import id_prefix as idp
from app.tools.base import ToolCall

FULL = "48b4e177-1c2d-4e5f-8a9b-0c1d2e3f4a5b"
OTHER = "48b4e177-ffff-4e5f-8a9b-0c1d2e3f4a5b"


async def _call(executor, name: str, **arguments):
    return await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments))


# ── parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("48b4e177", "48b4e177"),
        ("48B4E177", "48b4e177"),
        (" 48b4e177 ", "48b4e177"),
        ("48b4e1771c2d", "48b4e177-1c2d"),
        ("48b4e177-1c2d", "48b4e177-1c2d"),
        ("48b4e177-1c", "48b4e177-1c"),
    ],
)
def test_an_id_prefix_is_read_as_the_dashed_start_of_a_uuid(value, expected):
    assert idp.id_prefix(value) == expected


@pytest.mark.parametrize(
    "value",
    ["48b4e17", "48b4e17g", "not-an-id", "", FULL, FULL.upper(), "48b-4e177", None, 48_000_000, b"48b4e177"],
)
def test_short_non_hexadecimal_and_full_ids_are_not_prefixes(value):
    assert idp.id_prefix(value) is None


# ── resolution ───────────────────────────────────────────────────────────────


class _Db:
    """Answers each query with *rows* and keeps the statements it ran."""

    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        rows = self.rows
        return SimpleNamespace(all=lambda: rows)


@pytest.mark.asyncio
async def test_one_visible_match_stands_for_the_full_id():
    db = _Db([(uuid.UUID(FULL), "Release watch")])
    visible = AgentSkill.is_company_managed.is_(True)
    value, refusal = await idp.resolve_id_prefix(db, AgentSkill, "48b4e177", [visible], noun="skills")
    assert (value, refusal) == (FULL, None)
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    # The id is matched by its start as a UUID range the primary key's index
    # serves (never a scan of its text), inside the tool's own visibility clause.
    assert "BETWEEN" in sql
    assert "CAST" not in sql
    assert "is_company_managed" in sql


@pytest.mark.asyncio
async def test_no_match_keeps_the_value_so_the_tool_answers_not_found():
    value, refusal = await idp.resolve_id_prefix(_Db([]), AgentSkill, "48b4e177", noun="skills")
    assert (value, refusal) == ("48b4e177", None)


@pytest.mark.asyncio
async def test_two_matches_are_refused_with_both_candidates_named():
    db = _Db([(uuid.UUID(FULL), "Release watch"), (uuid.UUID(OTHER), "Deploy watch")])
    value, refusal = await idp.resolve_id_prefix(db, AgentSkill, "48b4e177", noun="skills")
    assert value == "48b4e177"
    assert refusal is not None
    assert FULL in refusal and OTHER in refusal
    assert "Release watch" in refusal and "Deploy watch" in refusal
    assert "full id" in refusal


@pytest.mark.asyncio
async def test_a_full_uuid_and_a_seven_character_prefix_read_nothing():
    db = _Db([(uuid.UUID(FULL), "x")])
    assert await idp.resolve_id_prefix(db, AgentSkill, FULL, noun="skills") == (FULL, None)
    assert await idp.resolve_id_prefix(db, AgentSkill, "48b4e17", noun="skills") == ("48b4e17", None)
    assert db.statements == []


def test_more_candidates_than_shown_say_so():
    many = [(uuid.uuid4(), f"n{i}") for i in range(idp.MAX_CANDIDATES + 1)]
    text = idp.ambiguous_text("48b4e177", "agents", many)
    assert "and more" in text
    assert f"more than {idp.MAX_CANDIDATES}" in text


# ── work item tools ──────────────────────────────────────────────────────────


def _item(item_id: str = FULL) -> dict:
    return {"id": item_id, "title": "Watch the deploy", "status": "open", "priority": "medium", "description": ""}


def _tools():
    return build_work_item_tools(SimpleNamespace(), str(uuid.uuid4()), str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_get_work_item_answers_the_one_item_a_prefix_starts(monkeypatch):
    asked: dict = {}

    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        asked["prefix"], asked["reader"] = prefix, reader
        return [_item()]

    async def _get(work_item_id, *, user_id, reader=None):
        asked["id"] = work_item_id
        return None

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    await _call(executor, "get_work_item", work_item_id="48b4e177")
    assert asked["prefix"] == "48b4e177"
    # Looked up as the agent reads, then read by the full id.
    assert asked["reader"] is not None
    assert asked["id"] == FULL


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["get_work_item", "update_work_item", "complete_work_item", "claim_work_item"])
async def test_work_item_tools_refuse_a_prefix_of_two_items(monkeypatch, tool):
    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        return [_item(FULL), {**_item(OTHER), "title": "Another"}]

    async def _never(*args, **kwargs):
        raise AssertionError("an ambiguous prefix must not reach the item")

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    for name in ("get_work_item", "update_work_item", "complete_work_item", "claim_work_item"):
        monkeypatch.setattr(wi_service, name, _never)
    _defs, executor = _tools()
    result = await _call(executor, tool, work_item_id="48b4e177")
    assert FULL in result.content and OTHER in result.content
    assert "Another" in result.content


@pytest.mark.asyncio
async def test_a_full_work_item_id_never_looks_up_a_prefix(monkeypatch):
    async def _by_prefix(*args, **kwargs):
        raise AssertionError("a full id is taken as it is")

    seen: dict = {}

    async def _get(work_item_id, *, user_id, reader=None):
        seen["id"] = work_item_id
        return None

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id=FULL)
    assert seen["id"] == FULL
    assert f"Work item `{FULL}` not found or not accessible." in result.content


@pytest.mark.asyncio
async def test_a_seven_character_work_item_prefix_is_not_resolved(monkeypatch):
    async def _by_prefix(*args, **kwargs):
        raise AssertionError("7 characters are not a prefix")

    async def _get(work_item_id, *, user_id, reader=None):
        return None

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id="48b4e17")
    assert "not found or not accessible" in result.content


# ── one platform tool: get_skill ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_skill_refuses_a_prefix_of_two_skills(monkeypatch):
    from app.api.v1 import helpers
    from app.core import database
    from app.services.builtin_tools.agent_builder import build_agent_builder_tool

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    async def _groups(db, uid):
        return []

    async def _ambiguous(db, model, value, clauses=(), *, noun, label=None):
        assert model is AgentSkill and noun == "skills"
        assert clauses, "the skill visibility clause is applied"
        return value, idp.ambiguous_text(value, noun, [(FULL, "One"), (OTHER, "Two")])

    monkeypatch.setattr(database, "async_session", lambda: _Session())
    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(idp, "resolve_id_prefix", _ambiguous)
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_skill", skill_id="48b4e177")
    assert FULL in result.content and OTHER in result.content


class _NoSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arg", "entity"),
    [
        ("get_assignment", "assignment_id", "Assignment"),
        ("get_skill", "skill_id", "Skill"),
        ("update_skill", "skill_id", "Skill"),
        ("get_agent", "agent_id", "Agent"),
        ("list_assignments", "agent_id", "Agent"),
    ],
)
async def test_a_prefix_that_starts_no_visible_object_is_answered_not_found(monkeypatch, tool, arg, entity):
    from app.api.v1 import helpers
    from app.core import database
    from app.services.builtin_tools import agent_builder

    async def _groups(db, uid):
        return []

    async def _caller(db, uid):
        return None

    async def _none(db, model, value, clauses=(), *, noun, label=None):
        return value, None

    monkeypatch.setattr(database, "async_session", lambda: _NoSession())
    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(agent_builder, "_load_caller_user_for_builder", _caller)
    monkeypatch.setattr(idp, "resolve_id_prefix", _none)
    _, executor = agent_builder.build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, tool, **{arg: "9196660a"})
    # A dead or invisible object, said as such: never a malformed argument.
    assert f"{entity} `9196660a` not found or you don't have access" in result.content
    assert "must be a valid UUID" not in result.content


@pytest.mark.asyncio
async def test_list_assignments_refuses_a_prefix_of_two_agents(monkeypatch):
    from app.api.v1 import helpers
    from app.core import database
    from app.models.agent import Agent
    from app.services.builtin_tools import agent_builder

    async def _groups(db, uid):
        return []

    async def _ambiguous(db, model, value, clauses=(), *, noun, label=None):
        assert model is Agent and noun == "agents"
        assert clauses, "the agent visibility clause is applied"
        return value, idp.ambiguous_text(value, noun, [(FULL, "One"), (OTHER, "Two")])

    monkeypatch.setattr(database, "async_session", lambda: _NoSession())
    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(idp, "resolve_id_prefix", _ambiguous)
    _, executor = agent_builder.build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "list_assignments", agent_id="48b4e177")
    # Read as an id prefix, never refused as a malformed UUID.
    assert FULL in result.content and OTHER in result.content
    assert "must be a valid UUID" not in result.content


@pytest.mark.asyncio
async def test_get_skill_still_refuses_a_value_that_is_no_id():
    from app.services.builtin_tools.agent_builder import build_agent_builder_tool

    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "get_skill", skill_id="48b4e17")
    assert "skill_id must be a valid UUID" in result.content
    result = await _call(executor, "get_assignment", assignment_id="9196660z")
    assert "assignment_id must be a valid UUID" in result.content
    result = await _call(executor, "list_assignments", agent_id="9196660z")
    assert "agent_id must be a valid UUID" in result.content


# ── one knowledge tool ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_document_prefix_is_looked_up_inside_the_knowledge_bases_in_scope(monkeypatch):
    from app.services.builtin_tools import kb as kb_tools

    kb_id = str(uuid.uuid4())
    seen: dict = {}

    async def _ambiguous(db, model, value, clauses=(), *, noun, label=None):
        seen["clauses"] = clauses
        return value, idp.ambiguous_text(value, noun, [(FULL, "a.md"), (OTHER, "b.md")])

    monkeypatch.setattr(idp, "resolve_id_prefix", _ambiguous)
    doc, kb, error = await kb_tools._fetch_kb_document(_Db([]), [kb_id], document_id="48b4e177", title=None)
    assert doc is None and kb is None
    assert error is not None and "a.md" in error and "b.md" in error
    sql = str(seen["clauses"][0].compile(dialect=postgresql.dialect()))
    assert "knowledge_base_id" in sql


@pytest.mark.asyncio
async def test_a_document_prefix_that_starts_no_document_is_answered_not_found(monkeypatch):
    from app.services.builtin_tools import kb as kb_tools

    async def _none(db, model, value, clauses=(), *, noun, label=None):
        return value, None

    monkeypatch.setattr(idp, "resolve_id_prefix", _none)
    doc, kb, error = await kb_tools._fetch_kb_document(_Db([]), [str(uuid.uuid4())], document_id="9196660a", title=None)
    assert doc is None and kb is None
    assert error is not None and "`9196660a` not found" in error
    assert "expected a UUID" not in error
    # Seven characters stay a malformed argument.
    _, _, error = await kb_tools._fetch_kb_document(_Db([]), [str(uuid.uuid4())], document_id="9196660", title=None)
    assert error is not None and "not a valid document id" in error


@pytest.mark.asyncio
async def test_a_work_item_prefix_that_starts_no_item_is_answered_not_found(monkeypatch):
    async def _by_prefix(prefix, *, user_id, reader=None, limit=6):
        return []

    async def _get(work_item_id, *, user_id, reader=None):
        return None

    monkeypatch.setattr(wi_service, "work_items_by_id_prefix", _by_prefix)
    monkeypatch.setattr(wi_service, "get_work_item", _get)
    _defs, executor = _tools()
    result = await _call(executor, "get_work_item", work_item_id="9196660a")
    assert "not found or not accessible" in result.content


# ── one video studio tool: the browser profiles ──────────────────────────────


@pytest.mark.asyncio
async def test_a_profile_prefix_resolves_among_the_usable_profiles(monkeypatch):
    from app.api.v1 import helpers
    from app.services.browser_profiles import profiles

    async def _groups(db, uid):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    db = _Db([(uuid.UUID(FULL), "Filigran Copilot")])
    value, refusal = await profiles.resolve_profile_id_prefix(db, "48b4e177", uuid.uuid4())
    assert (value, refusal) == (FULL, None)
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_a_full_profile_id_reads_nothing_more():
    from app.services.browser_profiles import profiles

    db = _Db([])
    assert await profiles.resolve_profile_id_prefix(db, FULL, uuid.uuid4()) == (FULL, None)
    assert db.statements == []


# ── video studio: the video ids ──────────────────────────────────────────────


def _viewer():
    return SimpleNamespace(id=uuid.uuid4(), role="user")


@pytest.mark.asyncio
async def test_a_video_prefix_resolves_among_the_videos_the_user_can_open(monkeypatch):
    from app.api.v1 import helpers
    from app.services.video_studio import projects

    async def _groups(db, uid):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    db = _Db([(uuid.UUID(FULL), "Launch film")])
    assert await projects.resolve_video_id_prefix(db, "48b4e177", _viewer()) == (FULL, None)
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_an_ambiguous_video_prefix_is_refused_naming_the_candidates(monkeypatch):
    from app.api.v1 import helpers
    from app.services.video_studio import projects

    async def _groups(db, uid):
        return []

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    db = _Db([(uuid.UUID(FULL), "Launch film"), (uuid.UUID(OTHER), "Teaser")])
    value, refusal = await projects.resolve_video_id_prefix(db, "48b4e177", _viewer())
    assert value == "48b4e177"
    assert refusal is not None and FULL in refusal and OTHER in refusal and "videos" in refusal


@pytest.mark.asyncio
async def test_a_full_video_id_reads_nothing_more():
    from app.services.video_studio import projects

    db = _Db([])
    assert await projects.resolve_video_id_prefix(db, FULL, _viewer()) == (FULL, None)
    assert await projects.resolve_video_id_prefix(db, "48b4e17", _viewer()) == ("48b4e17", None)
    assert db.statements == []


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _video_tools(monkeypatch, resolved):
    from app.services.builtin_tools import video_studio as vs
    from app.services.video_studio import projects

    async def _user(db, uid):
        return _viewer()

    async def _resolve(db, value, user):
        return resolved(value)

    monkeypatch.setattr(vs, "async_session", _Session)
    monkeypatch.setattr(vs, "_user", _user)
    monkeypatch.setattr(projects, "resolve_video_id_prefix", _resolve)
    _defs, executor = vs.build_video_studio_tools(user_id=str(uuid.uuid4()))
    return executor


@pytest.mark.asyncio
async def test_every_video_tool_taking_a_video_id_is_wrapped(monkeypatch):
    executor = _video_tools(monkeypatch, lambda value: (FULL, None))
    for name in ("get_video", "edit_video", "render_video", "delete_video", "share_video", "render_video_scene"):
        assert getattr(executor.handler(name), "__wrapped__", None) is not None, name
    assert getattr(executor.handler("list_videos"), "__wrapped__", None) is None


@pytest.mark.asyncio
async def test_get_video_with_a_prefix_reaches_the_tool_with_the_full_id(monkeypatch):
    from app.services.builtin_tools import video_studio as vs

    executor = _video_tools(monkeypatch, lambda value: (FULL, None))
    seen: list = []

    async def _probe(video_id: str = "", scenes=None, fields=None):
        seen.append(video_id)
        return vs._result("ok")

    executor.register("get_video", vs._with_video_id_prefix(_probe, str(uuid.uuid4())))
    result = await _call(executor, "get_video", video_id="48b4e177")
    assert not result.is_error
    assert seen == [FULL]
    seen.clear()
    await _call(executor, "get_video", video_id=OTHER)
    assert seen == [OTHER]


@pytest.mark.asyncio
async def test_get_video_with_an_ambiguous_prefix_is_refused_before_the_tool_runs(monkeypatch):
    from app.services.builtin_tools import video_studio as vs

    refusal = idp.ambiguous_text("48b4e177", "videos", [(FULL, "Launch film"), (OTHER, "Teaser")])
    executor = _video_tools(monkeypatch, lambda value: (value, refusal))

    async def _probe(video_id: str = "", scenes=None, fields=None):
        raise AssertionError("an ambiguous prefix must not reach the tool")

    executor.register("get_video", vs._with_video_id_prefix(_probe, str(uuid.uuid4())))
    result = await _call(executor, "get_video", video_id="48b4e177")
    assert result.is_error
    assert FULL in result.content and OTHER in result.content
