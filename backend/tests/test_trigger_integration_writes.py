"""A trigger may only name an integration its writer can see, and a
copy leaves out the work-item owner agents its copier cannot see.

An assignment's ``trigger_config.integration_id`` names the integration the
event poller reads events from, with that integration's credentials. A save
that sets or changes it must name one the writer can see (three-tier, admins
see all); the id already stored is never re-judged, so an unrelated edit
always saves. Import, duplicate and template instantiation never refuse: they
leave the integration out of the copied trigger and say so where the path can.
The agent tools take ``agent_tool_admin_bypass``, REST the admin role.
The agent's ``work_item_owner_agent_ids`` follow the same added-ids rule on
agent create / update, and a duplicate filters them like its other bindings.

These pin what is decided before any lookup and how each path reports it; the
visibility predicate itself runs against PostgreSQL in
``test_trigger_integration_writes_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.api.v1 import agent_import
from app.api.v1 import agents as agents_api
from app.api.v1 import binding_access as rule
from app.core.config import settings
from app.models.agent import Agent
from app.models.assignment import EVENT_MATCHING_EXACT, EVENT_MATCHING_KEY
from app.services import agent_bindings
from app.tools.base import ToolCall

_USER = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
_ADMIN = SimpleNamespace(id=uuid.uuid4(), is_admin=True)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def scalars(self):
        return _Result([row[0] for row in self._rows])


class _LookupDB:
    """Answers each id lookup with ``allowed`` (the writer belongs to no
    group); counts the id lookups."""

    def __init__(self, allowed=()):
        self._allowed = [(uuid.UUID(str(a)),) for a in allowed]
        self.lookups = 0
        self.added: list = []
        self.commits = 0

    async def execute(self, stmt):
        if stmt.get_final_froms()[0].name == "group_members":
            return _Result([])
        self.lookups += 1
        return _Result(self._allowed)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, instance):
        self.added.append(instance)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        return None


class _BypassAwareDB(_LookupDB):
    """Answers ``allowed`` to every lookup, plus ``admin_only`` to a lookup
    that takes the admin bypass (no per-user clause)."""

    def __init__(self, allowed=(), admin_only=()):
        super().__init__(allowed)
        self._admin_only = [(uuid.UUID(str(a)),) for a in admin_only]

    async def execute(self, stmt):
        if stmt.get_final_froms()[0].name == "group_members":
            return _Result([])
        self.lookups += 1
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if ".user_id" in sql:
            return _Result(self._allowed)
        return _Result(self._allowed + self._admin_only)


class _Scalar:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _TemplateDB(_BypassAwareDB):
    """A builder session that finds ``template`` and no clashing slug."""

    def __init__(self, template, **kwargs):
        super().__init__(**kwargs)
        self._template = template

    async def execute(self, stmt):
        table = stmt.get_final_froms()[0].name
        if table == "agent_templates":
            return _Scalar(self._template)
        if table == "agents":
            return _Scalar(None)
        return await super().execute(stmt)

    async def flush(self):
        return None


def _trigger(integration_id=None, **extra):
    config = {"provider": "microsoft", "event": "new_email", **extra}
    if integration_id is not None:
        config["integration_id"] = integration_id
    return config


def _created_trigger(integration_id=None):
    return {**_trigger(integration_id), EVENT_MATCHING_KEY: EVENT_MATCHING_EXACT}


# ---------------------------------------------------------------------------
# The rule (binding_access)
# ---------------------------------------------------------------------------


def test_a_trigger_names_its_integration_by_id():
    ref = str(uuid.uuid4())

    assert rule.trigger_integration_ref(_trigger(ref)) == ref
    assert rule.trigger_integration_ref(_trigger()) is None
    assert rule.trigger_integration_ref(_trigger(None)) is None
    assert rule.trigger_integration_ref({"integration_id": ""}) is None
    assert rule.trigger_integration_ref({"integration_id": "   "}) is None
    assert rule.trigger_integration_ref(None) is None
    assert rule.trigger_integration_ref(["not", "a", "config"]) is None
    assert rule.trigger_integration_ref({"integration_id": 7}) == 7


async def test_worked_on_main_the_stored_integration_is_never_re_judged():
    """An edit that keeps the trigger's integration - in any spelling - saves
    without a lookup, even when its writer cannot see it."""
    stored = str(uuid.uuid4())
    db = _LookupDB()

    for value in (_trigger(stored), _trigger(stored.upper()), _trigger(stored, filter={"x": 1})):
        assert not await rule.trigger_integration_refused(
            db, user=_USER, value=value, baseline=_trigger(stored)
        )
    assert db.lookups == 0


async def test_worked_on_main_a_trigger_naming_no_integration_is_never_refused():
    db = _LookupDB()

    for value in (_trigger(), _trigger(None), {"cron": "0 9 * * *"}, None, {}):
        assert not await rule.trigger_integration_refused(
            db, user=_USER, value=value, baseline=_trigger(str(uuid.uuid4()))
        )
    assert db.lookups == 0


async def test_a_new_or_changed_integration_must_be_one_the_writer_sees():
    visible, hidden = str(uuid.uuid4()), str(uuid.uuid4())

    assert not await rule.trigger_integration_refused(
        _LookupDB(allowed=[visible]), user=_USER, value=_trigger(visible)
    )
    assert await rule.trigger_integration_refused(
        _LookupDB(allowed=[visible]), user=_USER, value=_trigger(hidden)
    )
    changed = _LookupDB(allowed=[visible])
    assert await rule.trigger_integration_refused(
        changed, user=_USER, value=_trigger(hidden), baseline=_trigger(visible)
    )
    assert changed.lookups == 1


async def test_an_id_naming_no_integration_is_refused_like_a_hidden_one():
    db = _LookupDB()

    for bad in ("not-a-uuid", 7, {"id": "x"}):
        assert await rule.trigger_integration_refused(db, user=_USER, value=_trigger(bad))
    assert db.lookups == 0, "a malformed id is refused before any lookup"
    # A malformed value already stored is kept, like any stored value.
    assert not await rule.trigger_integration_refused(
        db, user=_USER, value=_trigger("legacy"), baseline=_trigger("legacy")
    )


async def test_a_refused_trigger_is_one_400_that_names_nothing():
    hidden = str(uuid.uuid4())
    with pytest.raises(HTTPException) as exc:
        await rule.assert_trigger_integration_allowed(
            _LookupDB(), user=_USER, value=_trigger(hidden), user_group_ids=[]
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == rule.TRIGGER_INTEGRATION_REFUSED_DETAIL
    assert hidden not in exc.value.detail


def test_the_refusals_read_like_the_other_binding_refusals():
    assert rule.TRIGGER_INTEGRATION_REFUSED_DETAIL == (
        "The integration in trigger_config was not found or is not accessible."
    )
    assert rule.WORK_ITEM_OWNERS_REFUSED_DETAIL == (
        "One or more work-item owner agents were not found or are not accessible."
    )


def test_leaving_the_integration_out_keeps_the_rest_of_the_trigger():
    config = _trigger(str(uuid.uuid4()), filter={"channels": ["C1"]})

    assert rule.without_trigger_integration(config) == {
        "provider": "microsoft",
        "event": "new_email",
        "filter": {"channels": ["C1"]},
    }


async def test_the_trigger_rule_takes_the_admin_bypass_its_caller_passes():
    """REST passes nothing (the admin role); an agent tool in strict mode
    passes ``is_admin=False`` and the admin is held to their three tiers."""
    hidden = str(uuid.uuid4())

    assert not await rule.trigger_integration_refused(
        _BypassAwareDB(admin_only=[hidden]), user=_ADMIN, value=_trigger(hidden)
    )
    assert await rule.trigger_integration_refused(
        _BypassAwareDB(admin_only=[hidden]), user=_ADMIN, value=_trigger(hidden), is_admin=False
    )
    with pytest.raises(HTTPException) as exc:
        await rule.assert_trigger_integration_allowed(
            _BypassAwareDB(admin_only=[hidden]),
            user=_ADMIN,
            value=_trigger(hidden),
            is_admin=False,
        )
    assert exc.value.detail == rule.TRIGGER_INTEGRATION_REFUSED_DETAIL


async def test_worked_on_main_a_rest_admin_names_any_integration_in_strict_mode(monkeypatch):
    """Strict admin access is about agent tools: a REST admin write keeps the
    admin bypass whatever it says."""
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    hidden = str(uuid.uuid4())

    await rule.assert_trigger_integration_allowed(
        _BypassAwareDB(admin_only=[hidden]), user=_ADMIN, value=_trigger(hidden)
    )


async def test_a_copied_trigger_keeps_only_an_integration_its_writer_sees():
    visible, hidden = str(uuid.uuid4()), str(uuid.uuid4())
    copy = rule.trigger_config_for_copy
    unpinned = {"provider": "microsoft", "event": "new_email", "filter": {"x": 1}}

    assert await copy(
        _BypassAwareDB(allowed=[visible]), user=_USER, trigger_config=_trigger(visible)
    ) == (_trigger(visible), False)
    assert await copy(
        _BypassAwareDB(allowed=[visible]),
        user=_USER,
        trigger_config=_trigger(hidden, filter={"x": 1}),
    ) == (unpinned, True)
    assert await copy(
        _BypassAwareDB(admin_only=[hidden]), user=_ADMIN, trigger_config=_trigger(hidden)
    ) == (_trigger(hidden), False)
    assert await copy(
        _BypassAwareDB(admin_only=[hidden]),
        user=_ADMIN,
        trigger_config=_trigger(hidden, filter={"x": 1}),
        is_admin=False,
    ) == (unpinned, True)


async def test_worked_on_main_a_copied_trigger_naming_no_integration_is_untouched():
    db = _BypassAwareDB()

    for config in (_trigger(), _trigger(None), {"cron": "0 9 * * *"}, {}, None, "not a config"):
        assert await rule.trigger_config_for_copy(db, user=_USER, trigger_config=config) == (
            config,
            False,
        )
    assert db.lookups == 0


async def test_an_added_work_item_owner_must_be_an_agent_the_writer_sees():
    stored, visible, hidden = (str(uuid.uuid4()) for _ in range(3))

    kept = _LookupDB()
    await rule.assert_work_item_owner_additions_allowed(
        kept, user=_USER, value=[stored.upper()], baseline=[stored]
    )
    await rule.assert_work_item_owner_additions_allowed(kept, user=_USER, value=None)
    await rule.assert_work_item_owner_additions_allowed(kept, user=_USER, value=[])
    assert kept.lookups == 0, "a stored roster entry is never re-judged"

    await rule.assert_work_item_owner_additions_allowed(
        _LookupDB(allowed=[visible]), user=_USER, value=[stored, visible], baseline=[stored]
    )
    with pytest.raises(HTTPException) as exc:
        await rule.assert_work_item_owner_additions_allowed(
            _LookupDB(allowed=[visible]), user=_USER, value=[visible, hidden], baseline=[]
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == rule.WORK_ITEM_OWNERS_REFUSED_DETAIL
    assert hidden not in exc.value.detail


# ---------------------------------------------------------------------------
# Import: an integration the importer cannot see is left out, with a warning
# ---------------------------------------------------------------------------


def test_the_import_reads_every_trigger_integration_the_file_names():
    one, two = uuid.uuid4(), uuid.uuid4()
    assignments = [
        {"trigger_config": _trigger(str(one))},
        {"trigger_config": _trigger(str(two).upper())},
        {"trigger_config": _trigger("not-a-uuid")},
        {"trigger_config": _trigger()},
        {"trigger_config": None},
        "not an assignment",
    ]

    assert agent_import._referenced_trigger_integration_ids(assignments) == {one, two}
    assert agent_import._referenced_trigger_integration_ids(None) == set()


def test_the_import_keeps_a_visible_trigger_integration_and_drops_the_rest():
    visible, hidden = str(uuid.uuid4()), str(uuid.uuid4())
    keep = agent_import._importable_trigger_config

    assert keep(_trigger(visible), {visible}) == (_trigger(visible), False)
    assert keep(_trigger(visible.upper()), {visible}) == (_trigger(visible.upper()), False)
    assert keep(_trigger(), {visible}) == (_trigger(), False)
    for dropped in (hidden, "not-a-uuid", 7):
        config, removed = keep(_trigger(dropped, filter={"x": 1}), {visible})
        assert removed is True
        assert config == {"provider": "microsoft", "event": "new_email", "filter": {"x": 1}}


def test_the_import_warning_names_the_assignment_and_what_to_do():
    warning = agent_import._dropped_trigger_integration_warning("Mail triage")

    assert "Mail triage" in warning
    assert "trigger integration" in warning
    assert "Pick one of your integrations" in warning


# ---------------------------------------------------------------------------
# Duplicate: the warning sentences and the work-item owner filter
# ---------------------------------------------------------------------------


def test_the_duplicate_counts_what_it_leaves_out_of_triggers_and_rosters():
    assert agents_api._copy_warnings(
        {"work_item_owner_agent_ids": 2, "assignment_trigger_integration": 1}
    ) == [
        "2 work-item owner agents of the original agent were not copied: you cannot use them.",
        "1 assignment trigger integration of the original agent was not copied: you cannot use it.",
    ]


def test_the_duplicate_filters_the_work_item_owners_like_its_other_bindings():
    kept, dropped = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(work_item_owner_agent_ids=[kept, dropped])

    counts = agent_bindings.retain_agent_bindings(agent, {"work_item_owner_agent_ids": {kept}})

    assert agent.work_item_owner_agent_ids == [kept]
    assert counts == {"work_item_owner_agent_ids": 1}
    assert agent_bindings.BINDING_MODELS["work_item_owner_agent_ids"] is Agent


# ---------------------------------------------------------------------------
# The agent-builder tools answer the REST refusal and save nothing
# ---------------------------------------------------------------------------


def _builder(monkeypatch, db, *, assignment=None, caller=_USER):
    agent = SimpleNamespace(
        id=uuid.uuid4(), name="Mail desk", is_company_managed=False, user_id=caller.id
    )

    async def _writable_agent(_db, _uid, _agent_uuid, **_gates):
        return agent, None

    async def _writable_assignment(_db, _uid, _assignment_uuid, **_gates):
        return assignment, agent, None

    async def _caller(_db, _uid):
        return caller

    monkeypatch.setattr(database, "async_session", lambda: db)
    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _writable_agent)
    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_assignment_for_builder", _writable_assignment
    )
    monkeypatch.setattr(builtin_tools_module, "_load_caller_user_for_builder", _caller)
    _, executor = builtin_tools_module.build_agent_builder_tool(SimpleNamespace(), str(caller.id))
    return executor, agent


async def _call(executor, tool, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )


async def test_the_builder_create_tool_refuses_a_hidden_trigger_integration(monkeypatch):
    db = _LookupDB(allowed=[])
    executor, agent = _builder(monkeypatch, db)

    result = await _call(
        executor,
        "create_assignment",
        agent_id=str(agent.id),
        name="Mail triage",
        prompt="Triage",
        trigger_type="integration_event",
        trigger_config=json.dumps(_trigger(str(uuid.uuid4()))),
    )

    assert rule.TRIGGER_INTEGRATION_REFUSED_DETAIL in str(result.content)
    assert db.added == [] and db.commits == 0


async def test_worked_on_main_the_builder_create_tool_saves_a_visible_trigger_integration(
    monkeypatch,
):
    visible = str(uuid.uuid4())
    db = _LookupDB(allowed=[visible])
    executor, agent = _builder(monkeypatch, db)

    result = await _call(
        executor,
        "create_assignment",
        agent_id=str(agent.id),
        name="Mail triage",
        prompt="Triage",
        trigger_type="integration_event",
        trigger_config=json.dumps(_trigger(visible)),
    )

    assert not result.is_error, result.content
    assert [wf.trigger_config["integration_id"] for wf in db.added] == [visible]
    assert db.commits == 1


def _stored_assignment(integration_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Mail triage",
        enabled=True,
        trigger_type="integration_event",
        trigger_config=_trigger(integration_id),
        agent_id=uuid.uuid4(),
    )


async def test_the_builder_update_tool_refuses_a_new_hidden_trigger_integration(monkeypatch):
    stored = str(uuid.uuid4())
    assignment = _stored_assignment(stored)
    db = _LookupDB(allowed=[])
    executor, _agent = _builder(monkeypatch, db, assignment=assignment)

    result = await _call(
        executor,
        "update_assignment",
        assignment_id=str(assignment.id),
        name="Renamed",
        trigger_config=json.dumps(_trigger(str(uuid.uuid4()))),
    )

    assert rule.TRIGGER_INTEGRATION_REFUSED_DETAIL in str(result.content)
    assert "Nothing was saved" in str(result.content)
    assert assignment.trigger_config == _trigger(stored)
    assert db.commits == 0


async def test_worked_on_main_the_builder_update_tool_keeps_a_stored_integration(monkeypatch):
    """The stored integration re-sent (in another spelling) with a new filter
    saves without a lookup, whether or not the caller can see it."""
    stored = str(uuid.uuid4())
    assignment = _stored_assignment(stored)
    db = _LookupDB(allowed=[])
    executor, _agent = _builder(monkeypatch, db, assignment=assignment)

    result = await _call(
        executor,
        "update_assignment",
        assignment_id=str(assignment.id),
        trigger_config=json.dumps(_trigger(stored.upper(), filter={"channels": ["C1"]})),
    )

    assert "Nothing was saved" not in str(result.content), result.content
    assert assignment.trigger_config["integration_id"] == stored.upper()
    assert db.lookups == 0


async def test_strict_admin_access_holds_an_admins_builder_writes_to_their_tiers(monkeypatch):
    """``AGENT_TOOLS_STRICT_ADMIN_ACCESS``: an admin's agent names only an
    integration the admin sees through their three tiers."""
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    hidden = str(uuid.uuid4())
    created_db = _BypassAwareDB(admin_only=[hidden])
    executor, agent = _builder(monkeypatch, created_db, caller=_ADMIN)

    created = await _call(
        executor,
        "create_assignment",
        agent_id=str(agent.id),
        name="Mail triage",
        prompt="Triage",
        trigger_type="integration_event",
        trigger_config=json.dumps(_trigger(hidden)),
    )
    stored = str(uuid.uuid4())
    assignment = _stored_assignment(stored)
    updated_db = _BypassAwareDB(admin_only=[hidden])
    executor, _agent = _builder(monkeypatch, updated_db, assignment=assignment, caller=_ADMIN)
    updated = await _call(
        executor,
        "update_assignment",
        assignment_id=str(assignment.id),
        trigger_config=json.dumps(_trigger(hidden)),
    )

    assert rule.TRIGGER_INTEGRATION_REFUSED_DETAIL in str(created.content)
    assert created_db.added == [] and created_db.commits == 0
    assert rule.TRIGGER_INTEGRATION_REFUSED_DETAIL in str(updated.content)
    assert "Nothing was saved" in str(updated.content)
    assert assignment.trigger_config == _trigger(stored)
    assert updated_db.commits == 0


async def test_worked_on_main_an_admins_builder_names_any_integration_by_default(monkeypatch):
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", False)
    hidden = str(uuid.uuid4())
    created_db = _BypassAwareDB(admin_only=[hidden])
    executor, agent = _builder(monkeypatch, created_db, caller=_ADMIN)

    created = await _call(
        executor,
        "create_assignment",
        agent_id=str(agent.id),
        name="Mail triage",
        prompt="Triage",
        trigger_type="integration_event",
        trigger_config=json.dumps(_trigger(hidden)),
    )
    assignment = _stored_assignment(str(uuid.uuid4()))
    executor, _agent = _builder(
        monkeypatch, _BypassAwareDB(admin_only=[hidden]), assignment=assignment, caller=_ADMIN
    )
    updated = await _call(
        executor,
        "update_assignment",
        assignment_id=str(assignment.id),
        trigger_config=json.dumps(_trigger(hidden)),
    )

    assert not created.is_error, created.content
    assert [wf.trigger_config["integration_id"] for wf in created_db.added] == [hidden]
    assert "Nothing was saved" not in str(updated.content), updated.content
    assert assignment.trigger_config["integration_id"] == hidden


# ---------------------------------------------------------------------------
# Template instantiation: a template trigger keeps only what its user sees
# ---------------------------------------------------------------------------


def _template(*integration_ids):
    return SimpleNamespace(
        slug="mail-desk",
        name="Mail desk",
        default_persona="",
        default_skills=[],
        default_assignments=[
            {
                "name": f"Triage {index}",
                "trigger_type": "integration_event",
                "trigger_config": _trigger(integration_id),
            }
            for index, integration_id in enumerate(integration_ids)
        ],
    )


async def _create_from_template(monkeypatch, db, caller):
    executor, _agent = _builder(monkeypatch, db, caller=caller)
    result = await _call(executor, "create_agent", name="Desk", template_slug="mail-desk")
    triggers = [row.trigger_config for row in db.added if not isinstance(row, Agent)]
    return result, triggers


async def test_the_builder_template_leaves_out_an_integration_its_user_cannot_see(monkeypatch):
    visible, hidden = str(uuid.uuid4()), str(uuid.uuid4())
    db = _TemplateDB(_template(visible, hidden, None), allowed=[visible], admin_only=[hidden])

    result, triggers = await _create_from_template(monkeypatch, db, _USER)

    assert not result.is_error, result.content
    assert triggers == [
        _created_trigger(visible),
        _created_trigger(),
        _created_trigger(),
    ]
    assert (
        "1 assignment trigger integration of the template was not copied: you cannot use it."
        in str(result.content)
    )
    assert hidden not in str(result.content)


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
async def test_an_admins_builder_template_takes_the_agent_tools_admin_bypass(monkeypatch, strict):
    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", strict)
    hidden = str(uuid.uuid4())
    db = _TemplateDB(_template(hidden), admin_only=[hidden])

    result, triggers = await _create_from_template(monkeypatch, db, _ADMIN)

    assert not result.is_error, result.content
    if strict:
        assert triggers == [_created_trigger()]
        assert "was not copied" in str(result.content)
    else:
        assert triggers == [_created_trigger(hidden)]
        assert "not copied" not in str(result.content)
