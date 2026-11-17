"""A custom tool may take any name, and never runs unattended by it.

The HITL whitelist is keyed by bare name, so a custom tool named after an
exempt built-in (``code_interpreter``) could have inherited that exemption and
run unattended with whatever ``{{var_id.*}}`` injected into its body. That is
closed at the gate, not by reserving names: a shipped entry reaches product
tools only (``approval.whitelist_reaches``), and a custom tool registers as
``custom:<id>``. So every name that was valid before keeps working - a tool
created on main, an imported bundle, a catalogue clone, an agent copy.

A standing approval is keyed by bare name too, so the same holds for it: a user's
"Yes, always" on an integration tool must not run a custom tool that copied its
name and schema (``approval.standing_approval_covers``).
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest

from app.models.custom_tool import CustomTool
from app.services import hitl_whitelist
from app.services.hitl_whitelist_default import SHIPPED_DEFAULT_WHITELIST
from app.tools.approval import REACH_PRODUCT, evaluate_gate, fingerprint
from app.tools.base import HumanInputRequestedError


@pytest.mark.parametrize("name", ["code_interpreter", "list_agents", "wait"])
def test_a_whitelisted_name_is_a_valid_custom_tool_name(name):
    assert name in SHIPPED_DEFAULT_WHITELIST, f"{name} is no longer whitelisted"
    tool = CustomTool(user_id=uuid.uuid4(), name=name)
    tool.name = "summarize_ticket"
    tool.name = name
    assert tool.name == name


@pytest.mark.parametrize("reach", ["recorded", None])
@pytest.mark.parametrize("name", ["code_interpreter", "list_agents", "wait"])
def test_a_custom_tool_on_a_whitelisted_name_still_gates(name, reach):
    """With the reach the resolver records, and with none at all."""
    whitelist_reach = (
        {n: frozenset({REACH_PRODUCT}) for n in SHIPPED_DEFAULT_WHITELIST}
        if reach == "recorded"
        else None
    )
    tool_id = str(uuid.uuid4())
    decision = evaluate_gate(
        name,
        hitl_enabled=True,
        whitelist=SHIPPED_DEFAULT_WHITELIST,
        active_exceptions={},
        current_fingerprint="fp",
        tool_source=f"custom:{tool_id}",
        tool_entity_id=tool_id,
        whitelist_reach=whitelist_reach,
    )
    assert decision.action == "pause"
    # The built-in of the same name keeps its exemption.
    builtin = evaluate_gate(
        name,
        hitl_enabled=True,
        whitelist=SHIPPED_DEFAULT_WHITELIST,
        active_exceptions={},
        current_fingerprint="fp",
        tool_source="builtin",
        whitelist_reach=whitelist_reach,
    )
    assert builtin.action == "execute"


# --- an old bundle, end to end ----------------------------------------------

_OLD_BUNDLE = {
    "version": 3,
    "export_type": "agent",
    "agent": {"name": "Pre-approval agent", "persona": "You compute."},
    "custom_tools": [{"name": "code_interpreter", "js_function": "return 1;"}],
}


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["import", "import-file"])
async def test_an_old_bundle_with_a_whitelisted_tool_name_imports_and_still_gates(
    route, monkeypatch
):
    from sqlalchemy import select

    from app.services.builtin_tools.loaders import load_custom_tools
    from app.tools.base import CompositeToolExecutor
    from tests.test_agent_import_null_fields_postgres import _ROUTES, _user
    from tests.test_provisioning_postgres import _temp_database

    async with _temp_database("imp_ctname") as sessionmaker:
        user = await _user(sessionmaker)

        await _ROUTES[route](sessionmaker, user, copy.deepcopy(_OLD_BUNDLE))

        async with sessionmaker() as db:
            tool = (await db.execute(select(CustomTool))).scalar_one()
            assert tool.name == "code_interpreter"

            composite = CompositeToolExecutor()
            await load_custom_tools(db, [tool.id], composite, user_id=str(user.id))
            definition = composite.get_tool_definition("code_interpreter")
            assert definition is not None
            assert definition.source == f"custom:{tool.id}"

            async def _whitelist(_db, *, reach=None):
                for name in SHIPPED_DEFAULT_WHITELIST:
                    reach.setdefault(name, set()).add(REACH_PRODUCT)
                return SHIPPED_DEFAULT_WHITELIST

            async def _nothing(*_a, **_k):
                return frozenset()

            async def _no_suppression(*_a, **_k):
                return None

            async def _exceptions(_db, _uid, **_k):
                return {}

            monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist)
            monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
            monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
            monkeypatch.setattr(
                hitl_whitelist, "resolve_gate_suppression", _no_suppression
            )
            monkeypatch.setattr(
                "app.services.tool_approval_service.list_active_exceptions",
                _exceptions,
            )
            gate = await hitl_whitelist.build_approval_gate(
                db, composite, user_id=user.id, hitl_enabled=True
            )

        with pytest.raises(HumanInputRequestedError):
            gate(SimpleNamespace(id="c1", name="code_interpreter"))


# --- a standing approval is the tool's, not the name's -----------------------

_SCHEMA = {"type": "object", "properties": {"to": {"type": "string"}}}
_FP = fingerprint(_SCHEMA)


def _standing(source, *, granted_to, sources="recorded", name="send_email"):
    return evaluate_gate(
        name,
        hitl_enabled=True,
        whitelist=frozenset(),
        active_exceptions={name: _FP},
        current_fingerprint=_FP,
        tool_source=source,
        exception_sources={name: granted_to} if sources == "recorded" else None,
    )


def test_an_approval_for_an_integration_tool_does_not_run_a_custom_tool_of_that_name():
    theirs = _standing("custom:t9", granted_to="integration:gmail")
    assert theirs.action == "pause"
    # Left alone: gating one tool must not revoke what was given to another.
    assert theirs.invalidated_preference is False
    # The tool it was granted to keeps it, as does a product tool by name.
    assert _standing("integration:gmail", granted_to="integration:gmail").action == "execute"


def test_an_approval_covers_the_custom_tool_it_was_granted_to_and_no_other():
    assert _standing("custom:t9", granted_to="custom:t9").action == "execute"
    assert _standing("custom:t9", granted_to="custom:t1").action == "pause"


@pytest.mark.parametrize("granted_to", ["custom", "", "builtin"])
def test_an_approval_that_names_no_custom_tool_covers_none(granted_to):
    """A grant from before the source carried the tool's id, or a bare label."""
    assert _standing("custom:t9", granted_to=granted_to).action == "pause"
    assert _standing("custom", granted_to="custom").action == "pause"


def test_a_caller_that_resolved_no_sources_covers_no_custom_tool():
    assert _standing("custom:t9", granted_to="custom:t9", sources=None).action == "pause"
    # ...and leaves every other source as it was.
    assert (
        _standing("integration:gmail", granted_to="integration:gmail", sources=None).action
        == "execute"
    )


class _Composite:
    def __init__(self, definition):
        self._definition = definition

    def get_tool_definition(self, _name):
        return self._definition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("granted_to", "outcome"),
    [("integration:gmail", "pause"), ("custom:t9", "execute")],
)
async def test_the_chat_gate_reads_the_source_an_approval_was_granted_to(
    monkeypatch, granted_to, outcome
):
    from app.tools.base import ToolDefinition

    async def _nothing(*_a, **_k):
        return frozenset()

    async def _whitelist(_db, *, reach=None):
        return frozenset()

    async def _exceptions(_db, _uid, *, sources=None, entity_ids=None):
        sources["send_email"] = granted_to
        return {"send_email": _FP}

    monkeypatch.setattr(hitl_whitelist, "resolve_whitelist", _whitelist)
    monkeypatch.setattr(hitl_whitelist, "resolve_vetoed", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_exempt_sources", _nothing)
    monkeypatch.setattr(hitl_whitelist, "resolve_gate_suppression", _nothing)
    monkeypatch.setattr(
        "app.services.tool_approval_service.list_active_exceptions", _exceptions
    )
    definition = ToolDefinition(
        name="send_email",
        description="",
        input_schema=_SCHEMA,
        source="custom:t9",
        entity_id="t9",
    )
    gate = await hitl_whitelist.build_approval_gate(
        object(), _Composite(definition), user_id=uuid.uuid4(), hitl_enabled=True
    )

    call = SimpleNamespace(id="c1", name="send_email")
    if outcome == "pause":
        with pytest.raises(HumanInputRequestedError):
            gate(call)
    else:
        gate(call)


@pytest.mark.asyncio
async def test_a_grant_from_the_tools_list_names_the_custom_tool(monkeypatch):
    """Recorded as ``custom:<id>``; a bare ``custom`` would cover no tool."""
    from sqlalchemy import select

    from app.api.v1 import helpers
    from app.api.v1 import tool_approvals as api

    tool_id = uuid.uuid4()

    async def _groups(*_a, **_k):
        return set()

    monkeypatch.setattr(helpers, "get_user_group_ids", _groups)
    monkeypatch.setattr(
        helpers, "user_visible_query", lambda *_a, **_k: select(CustomTool)
    )

    class _DB:
        async def execute(self, _stmt):
            row = SimpleNamespace(id=tool_id, input_schema=_SCHEMA)
            return SimpleNamespace(first=lambda: row)

    schema, source, entity_id = await api._resolve_schema(
        _DB(), "acme_send_report", user_id=uuid.uuid4(), is_admin=False
    )

    assert schema == _SCHEMA
    assert source == f"custom:{tool_id}"
    assert entity_id == str(tool_id)
