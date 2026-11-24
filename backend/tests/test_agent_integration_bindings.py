"""Who may use an agent's bound integrations is decided once.

``integration_tools.resolve_agent_integration_bindings`` resolves an agent's
bindings for the person a run acts for; ``prebuilt_tools_refusal`` keeps a run
from using tools built elsewhere from any other resolution. The tripwire at the
end lists every caller of ``build_integration_tools`` with why its bindings
are allowed, so a new one is reviewed before it ships.
"""

from __future__ import annotations

import ast
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import true

from app.services import integration_credentials, integration_tools
from tests import app_sources

_OWNER = uuid.uuid4()
_COLLEAGUE = uuid.uuid4()
_OWN_ID = str(uuid.uuid4())
_LENT_ID = str(uuid.uuid4())


def _agent(**overrides) -> SimpleNamespace:
    values = {
        "id": uuid.uuid4(),
        "name": "Shared agent",
        "user_id": _OWNER,
        "integration_bindings": {"github": [_OWN_ID], "jira": [_LENT_ID]},
        "integration_template_bindings": [],
        "integration_lending": {"integration_ids": [_LENT_ID]},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    """Answers each ``execute`` with the next queued rows."""

    def __init__(self, *results):
        self._results = list(results)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Rows(self._results.pop(0) if self._results else [])


@pytest.fixture
def guard(monkeypatch):
    """The orphan drop and the visibility guard, recorded; the guard drops github."""
    calls: dict[str, dict] = {}

    async def drop_orphaned(_db, _agent, bindings, origins, *, withheld=None):
        calls["orphans"] = {"bindings": dict(bindings)}
        return []

    async def shadow(_db, bindings, origins, **kwargs):
        calls["shadow"] = {"bindings": dict(bindings), **kwargs}
        bindings.pop("github", None)
        origins.pop("github", None)
        kwargs["withheld"].append({"provider": "github", "reason": "invisible"})
        return ["github"]

    monkeypatch.setattr(integration_credentials, "drop_orphaned_bindings", drop_orphaned)
    monkeypatch.setattr(integration_tools, "shadow_invisible_bindings", shadow)
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=["g1"]))
    monkeypatch.setattr(integration_tools, "template_candidate_filter", AsyncMock(return_value=true()))
    return calls


async def test_a_non_owner_gets_the_guard_with_what_the_agent_lends(guard):
    resolved = await integration_tools.resolve_agent_integration_bindings(_Db(), _agent(), str(_COLLEAGUE))

    shadow = guard["shadow"]
    assert shadow["acting_user_id"] == str(_COLLEAGUE)
    assert shadow["user_group_ids"] == ["g1"]
    assert shadow["lent_ids"] == {_LENT_ID}
    assert shadow["lent_owner_id"] == _OWNER
    assert resolved.bindings == {"jira": _LENT_ID}
    # Nothing replaced github: the run says it was withheld.
    assert resolved.withheld == [{"provider": "github", "reason": "invisible"}]


async def test_a_withheld_provider_falls_back_to_the_person_s_own_integration(guard, monkeypatch):
    own = SimpleNamespace(id=uuid.uuid4(), provider="github")
    monkeypatch.setattr(
        integration_tools, "resolve_template_integration", AsyncMock(return_value=(own, "template_own"))
    )

    resolved = await integration_tools.resolve_agent_integration_bindings(_Db([own]), _agent(), str(_COLLEAGUE))

    assert resolved.bindings == {"jira": _LENT_ID, "github": str(own.id)}
    assert resolved.origins["github"] == "template_own"
    assert resolved.withheld == []


@pytest.mark.parametrize("acting", [str(_OWNER), _OWNER.hex.upper()], ids=["canonical", "hex-upper"])
async def test_the_owner_s_own_run_keeps_every_binding(guard, acting):
    resolved = await integration_tools.resolve_agent_integration_bindings(_Db(), _agent(), acting)

    assert "shadow" not in guard
    assert resolved.bindings == {"github": _OWN_ID, "jira": _LENT_ID}


@pytest.mark.parametrize("acting", [None, "anonymous", "not-a-uuid"])
async def test_a_run_with_no_person_is_the_owner_s_and_resolves_no_template(guard, acting):
    db = _Db()

    resolved = await integration_tools.resolve_agent_integration_bindings(
        db, _agent(integration_template_bindings=["slack"]), acting
    )

    assert "shadow" not in guard
    assert "orphans" in guard
    assert db.statements == []
    assert resolved.bindings == {"github": _OWN_ID, "jira": _LENT_ID}


async def test_templates_resolve_in_one_query_through_the_candidate_filter(guard, monkeypatch):
    rows = [SimpleNamespace(id=uuid.uuid4(), provider=p) for p in ("slack", "teams")]
    monkeypatch.setattr(
        integration_tools,
        "resolve_template_integration",
        AsyncMock(side_effect=lambda _db, _p, candidates, *_a, **_k: (candidates[0], "template_own")),
    )
    db = _Db(rows)

    resolved = await integration_tools.resolve_agent_integration_bindings(
        db,
        _agent(user_id=_COLLEAGUE, integration_template_bindings=["slack", "teams"]),
        str(_COLLEAGUE),
        company_automation=True,
    )

    assert len(db.statements) == 1
    integration_tools.template_candidate_filter.assert_awaited_once_with(db, _COLLEAGUE, company_automation=True)
    assert resolved.bindings["slack"] == str(rows[0].id)
    assert resolved.bindings["teams"] == str(rows[1].id)


# --- prebuilt_tools_refusal ---------------------------------------------------


def _prebuilt(bindings, acting_user_id):
    return SimpleNamespace(bindings=bindings, acting_user_id=acting_user_id)


@pytest.mark.parametrize(
    ("prebuilt", "acting", "effective", "refused"),
    [
        # Built for the person from what the turn resolved: used.
        (_prebuilt({"jira": _LENT_ID}, str(_COLLEAGUE)), str(_COLLEAGUE), {"jira": _LENT_ID}, False),
        # Holds the owner's integration the turn withheld: refused.
        (
            _prebuilt({"github": _OWN_ID, "jira": _LENT_ID}, str(_COLLEAGUE)),
            str(_COLLEAGUE),
            {"jira": _LENT_ID},
            True,
        ),
        # Built for someone else: refused.
        (_prebuilt({"jira": _LENT_ID}, str(_OWNER)), str(_COLLEAGUE), {"jira": _LENT_ID}, True),
        # Says nothing of what it was built from: refused.
        (([], None), str(_COLLEAGUE), {}, True),
        # A run with no person is the owner's: tools built for the owner or no one.
        (_prebuilt({"github": _OWN_ID}, str(_OWNER)), None, {}, False),
        (_prebuilt({"github": _OWN_ID}, None), None, {}, False),
        (_prebuilt({"github": _OWN_ID}, str(_COLLEAGUE)), None, {}, True),
    ],
    ids=["resolved", "withheld", "other-person", "undeclared", "owner-run", "no-one", "owner-run-other"],
)
def test_prebuilt_tools_are_used_only_as_the_turn_resolved(prebuilt, acting, effective, refused):
    refusal = integration_tools.prebuilt_tools_refusal(
        prebuilt, agent=_agent(), acting_user_id=acting, effective_bindings=effective
    )

    assert (refusal is not None) is refused


# --- ToolBindings -------------------------------------------------------------


async def test_the_builder_refuses_bindings_that_do_not_say_why():
    with pytest.raises(TypeError, match="ToolBindings"):
        await integration_tools.build_integration_tools(_Db(), {"github": [_OWN_ID]})


def test_tool_bindings_need_a_basis_and_a_reason():
    with pytest.raises(ValueError):
        integration_tools.ToolBindings({}, basis=integration_tools.BindingBasis.UNCHECKED, reason=" ")
    bound = integration_tools.ToolBindings(
        {"github": [_OWN_ID]}, basis=integration_tools.BindingBasis.CALLER_CHECKED, reason="test"
    )
    # A read-only mapping: what the builder and its fakes read.
    assert dict(bound) == {"github": [_OWN_ID]}


def test_resolved_bindings_say_they_were_resolved():
    resolved = integration_tools.AgentIntegrationBindings({"jira": _LENT_ID}, {}, [])

    bound = resolved.tool_bindings

    assert bound.basis is integration_tools.BindingBasis.RESOLVED
    assert dict(bound) == {"jira": _LENT_ID}


# --- Tripwire -----------------------------------------------------------------

#: Every module that builds integration tools, and on which ground
#: (``BindingBasis``) the bindings it hands over may act for the person a run
#: serves. ``RESOLVED`` comes from ``resolve_agent_integration_bindings`` (the
#: chat turn also takes the session selection its resolver checked);
#: ``UNCHECKED`` marks the known gaps still to close. A new module, or a new
#: basis in one, is reviewed here before it ships.
_BASES = {
    # The turn's resolution; the pipeline fallback's pick; its pinned ref, unchecked.
    "services/agent_chat.py": {"RESOLVED", "CALLER_CHECKED", "UNCHECKED"},
    "services/assignment_engine.py": {"UNCHECKED"},
    # The Integration node, unchecked; the call-tool node, the caller's own.
    "services/flow_runner.py": {"UNCHECKED", "CALLER_CHECKED"},
    "services/scheduled_work_item_runner.py": {"CALLER_CHECKED"},
    "services/general_chat.py": {"CALLER_CHECKED", "ADMIN_CONFIGURED"},
    "api/v1/public_chat.py": {"OWNER_RUN"},
    "api/v1/integrations.py": {"CALLER_CHECKED"},
    "api/v1/channel_bots.py": {"CALLER_CHECKED"},
    "api/v1/flows.py": {"CALLER_CHECKED"},
    "api/v1/mcp_platform.py": {"CALLER_CHECKED"},
    "integrations/providers/openapi.py": {"CALLER_CHECKED"},
    # Platform Health's module reads, as the "Run checks as" user it checked.
    "services/builtin_tools/health_modules.py": {"CALLER_CHECKED"},
    # AgentIntegrationBindings.tool_bindings
    "services/integration_tools.py": {"RESOLVED"},
}

#: The modules building from ``AgentIntegrationBindings.tool_bindings``.
_FROM_RESOLVER = {"services/evaluation_engine.py"}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)


def _calls(module: ast.Module, name: str) -> list[ast.Call]:
    return [node for node in app_sources.walk(module) if isinstance(node, ast.Call) and _call_name(node) == name]


def _bases(call: ast.Call) -> set[str]:
    basis = next(keyword.value for keyword in call.keywords if keyword.arg == "basis")
    return {
        node.attr
        for node in ast.walk(basis)
        if isinstance(node, ast.Attribute) and getattr(node.value, "id", None) == "BindingBasis"
    }


def test_every_module_building_integration_tools_says_on_what_ground():
    found: dict[str, set[str]] = {}
    for path, module in app_sources.modules():
        for call in _calls(module, "ToolBindings"):
            found.setdefault(path, set()).update(_bases(call))

    assert found == _BASES


def test_every_build_names_its_bindings_where_it_builds():
    """The bindings are a ``ToolBindings(...)`` call or a resolution's
    ``tool_bindings`` in the build call itself, so the ground they are used on
    is read at the call."""
    loose = []
    from_resolver = set()
    for path, module in app_sources.modules():
        if path == "services/integration_tools.py":
            continue
        for call in _calls(module, "build_integration_tools"):
            argument = call.args[1] if len(call.args) > 1 else None
            if isinstance(argument, ast.Attribute) and argument.attr == "tool_bindings":
                from_resolver.add(path)
            elif not (isinstance(argument, ast.Call) and _call_name(argument) == "ToolBindings"):
                loose.append(f"{path}:{call.lineno}")

    assert loose == []
    assert from_resolver == _FROM_RESOLVER


def test_only_the_resolver_and_the_chat_turn_claim_a_resolution():
    claiming = {path for path, bases in _BASES.items() if "RESOLVED" in bases}
    assert claiming == {"services/integration_tools.py", "services/agent_chat.py"}


def test_only_an_evaluation_hands_a_turn_prebuilt_tools_and_the_turn_checks_them():
    passing = sorted(
        path
        for path, module in app_sources.modules()
        if any(
            keyword.arg == "prebuilt_integration_tools"
            for call in _calls(module, "run_agent_chat")
            for keyword in call.keywords
        )
    )
    assert passing == ["services/evaluation_engine.py"]
    chat = dict(app_sources.modules())["services/agent_chat.py"]
    assert _calls(chat, "prebuilt_tools_refusal")
    assert _calls(chat, "resolve_agent_integration_bindings")
