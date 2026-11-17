"""An agent can run on the platform routing model.

``Agent.llm_model_source = "routing"`` makes the agent ask for
``PlatformSettings.routing_model`` / ``routing_provider_id`` instead of its own
fields. These tests pin the selection itself, the untrusted-input normaliser,
the agent chat call site (the hub every chat, consult, background task,
channel and A2A path goes through), and that no call site reads the agent's
model columns behind the helper's back.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.llm.agent_model import (
    AgentModelSelection,
    agent_model_selection,
    normalize_llm_model_source,
)

_ROUTING_PID = uuid.UUID("00000000-0000-0000-0000-0000000005a5")


def _patch_routing(monkeypatch, model, provider_id):
    async def _routing(_db):
        return model, provider_id

    monkeypatch.setattr("app.services.flow_llm.routing_model", _routing)


def _agent(**overrides) -> SimpleNamespace:
    defaults = dict(
        llm_provider="openai",
        llm_provider_id=uuid.UUID("00000000-0000-0000-0000-0000000000a1"),
        llm_model="gpt-5",
        llm_model_source=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# The selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_without_a_source_asks_for_its_own_model(monkeypatch):
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)
    agent = _agent()

    assert await agent_model_selection(None, agent) == AgentModelSelection(
        "openai", "gpt-5", agent.llm_provider_id
    )


@pytest.mark.asyncio
async def test_a_routing_agent_asks_for_the_routing_model_and_its_config(monkeypatch):
    """The routing model wins over the agent's own (stale) fields, and the
    provider name is left to the router: the routing config reference names it."""
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)

    selection = await agent_model_selection(None, _agent(llm_model_source="routing"))

    assert selection == AgentModelSelection(None, "small-model", _ROUTING_PID)


@pytest.mark.asyncio
async def test_a_routing_agent_with_no_routing_model_overrides_nothing(monkeypatch):
    """No routing model configured: the agent falls through to the usual tiers,
    and says so, rather than reviving its own stale fields."""
    _patch_routing(monkeypatch, None, None)

    selection = await agent_model_selection(None, _agent(llm_model_source="routing"))

    assert selection == AgentModelSelection(None, None, None)


@pytest.mark.asyncio
async def test_an_agent_with_empty_fields_selects_nothing(monkeypatch):
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)

    selection = await agent_model_selection(
        None, _agent(llm_provider="", llm_provider_id=None, llm_model="")
    )

    assert selection == AgentModelSelection(None, None, None)


@pytest.mark.asyncio
async def test_a_row_without_the_column_reads_as_the_agent_model(monkeypatch):
    """A partial row (``select(Agent.id, Agent.llm_model)``) or an older double
    carries no source: it is the agent's own model."""
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)

    selection = await agent_model_selection(None, SimpleNamespace(llm_model="gpt-5"))

    assert selection == AgentModelSelection(None, "gpt-5", None)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("routing", "routing"),
        (None, None),
        ("", None),
        ("ROUTING", None),
        ("routing ", None),
        ("agent", None),
        ("x" * 200, None),
        (1, None),
        ({}, None),
        (["routing"], None),
    ],
)
def test_an_imported_source_is_normalised(raw, expected):
    """A bundle is untrusted: anything but a known source reads as none."""
    assert normalize_llm_model_source(raw) == expected


# ---------------------------------------------------------------------------
# The agent chat call site
# ---------------------------------------------------------------------------


class _StopError(Exception):
    """Raised by the router spy: the test only needs the call's arguments."""


@pytest.mark.asyncio
async def test_agent_chat_resolves_a_routing_agent_on_the_routing_model(monkeypatch):
    """``run_agent_chat`` hands the routing model and its config to the router,
    and does not reuse the caller's prebuilt default LLM for it."""
    import app.services.agent_chat as agent_chat_mod

    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)
    get_llm = AsyncMock(side_effect=_StopError())
    agent = SimpleNamespace(
        id=uuid.UUID("00000000-0000-0000-0000-00000000dddd"),
        name="Judge",
        slug="judge",
        is_builtin=False,
        skip_llm=False,
        prepare_tool_ref=None,
        finalize_tool_ref=None,
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        llm_model_source="routing",
    )

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.services.quota.refund_agentic_usage", new=AsyncMock()),
        patch("app.llm.router.get_llm_for_user", new=get_llm),
        pytest.raises(_StopError),
    ):
        await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            prebuilt_llm=(SimpleNamespace(provider_name="default"), "default-model"),
        )

    get_llm.assert_awaited_once()
    kwargs = get_llm.await_args.kwargs
    assert kwargs["model_override"] == "small-model"
    assert kwargs["provider_id_override"] == _ROUTING_PID
    assert kwargs["provider_override"] is None


# ---------------------------------------------------------------------------
# One reader of the agent's model columns
# ---------------------------------------------------------------------------

_MODEL_COLUMNS = {"llm_model", "llm_provider", "llm_provider_id"}
_OVERRIDE_KWARGS = {"model_override", "provider_override", "provider_id_override"}


def _column_read(node: ast.AST) -> str | None:
    """``x.llm_model`` or ``getattr(x, "llm_model", ...)`` -> the column name."""
    if isinstance(node, ast.Attribute) and node.attr in _MODEL_COLUMNS:
        return node.attr
    if (
        isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "getattr"
        and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
        and node.args[1].value in _MODEL_COLUMNS
    ):
        return node.args[1].value
    return None


def test_no_router_call_reads_an_agent_model_column_directly():
    """Every ``get_llm_for_user`` override comes from ``agent_model_selection``.

    A call site passing ``agent.llm_model`` itself would run a routing agent on
    its stale own model (or the platform default) on that one path - the
    "every path, one rule" failure. Structural, over the parsed call
    arguments, not the text.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    offenders: list[str] = []
    for path in sorted(app_dir.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name != "get_llm_for_user":
                continue
            for keyword in node.keywords:
                if keyword.arg not in _OVERRIDE_KWARGS:
                    continue
                # ``x.llm_model or y`` and ``a if c else x.llm_model`` hide it too.
                for sub in ast.walk(keyword.value):
                    column = _column_read(sub)
                    if column:
                        offenders.append(
                            f"{path.relative_to(app_dir)}:{node.lineno} {keyword.arg}=...{column}"
                        )
    assert offenders == []


# ---------------------------------------------------------------------------
# The agent builder tool, like the REST update
# ---------------------------------------------------------------------------


async def _builder_update(monkeypatch, agent, **arguments):
    from app.core import database
    from app.services.builtin_tools import build_agent_builder_tool
    from tests.test_agent_builder_crud_tools import (
        _call,
        _session_factory,
        _stub_writable_agent,
    )

    _stub_writable_agent(monkeypatch, agent=agent)
    created: list = []
    monkeypatch.setattr(database, "async_session", _session_factory(created))
    _, executor = build_agent_builder_tool(SimpleNamespace(), str(uuid.uuid4()))
    result = await _call(executor, "update_agent", agent_id=str(agent.id), **arguments)
    return result, created[0].commits


@pytest.mark.asyncio
async def test_the_builder_puts_an_agent_on_the_routing_model(monkeypatch):
    from app.models.agent import Agent

    agent = Agent(
        id=uuid.uuid4(), name="Judge", llm_provider="openai", llm_model="gpt-5"
    )

    result, commits = await _builder_update(monkeypatch, agent, llm_model_source="routing")

    assert not result.is_error
    assert agent.llm_model_source == "routing"
    assert (agent.llm_provider, agent.llm_provider_id, agent.llm_model) == (None, None, None)
    assert commits == 1


@pytest.mark.asyncio
async def test_the_builder_returns_an_agent_to_its_own_model(monkeypatch):
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Judge", llm_model_source="routing")

    result, commits = await _builder_update(monkeypatch, agent, llm_model_source="none")

    assert not result.is_error
    assert agent.llm_model_source is None
    assert commits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["ROUTER", "small", "agent"])
async def test_the_builder_refuses_an_unknown_model_source(monkeypatch, value):
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Judge", llm_model="gpt-5")

    result, commits = await _builder_update(
        monkeypatch, agent, llm_model_source=value, name="Renamed"
    )

    assert result.content.startswith("Invalid llm_model_source")
    assert agent.llm_model_source is None
    assert agent.name == "Judge"
    assert commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "own",
    [
        {"llm_model": "gpt-5"},
        {"llm_provider": "openai"},
        {"llm_provider_id": "00000000-0000-0000-0000-0000000000a1"},
        {"llm_model": "gpt-5", "llm_provider": "openai"},
    ],
)
async def test_the_builder_refuses_the_routing_model_with_a_model_of_its_own(
    monkeypatch, own
):
    """The model would be dropped once written: refused, nothing written."""
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Judge", llm_provider="anthropic", llm_model="claude")

    result, commits = await _builder_update(
        monkeypatch, agent, llm_model_source="routing", name="Renamed", **own
    )

    assert result.content.startswith("llm_model_source 'routing' runs the agent")
    assert f"leave {', '.join(own)} empty" in result.content
    assert agent.llm_model_source is None
    assert (agent.name, agent.llm_provider, agent.llm_model) == ("Judge", "anthropic", "claude")
    assert commits == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cleared", ["none", "clear", " NONE "])
async def test_the_builder_takes_the_routing_model_with_cleared_model_fields(
    monkeypatch, cleared
):
    """Clearing a field agrees with the routing source: not refused."""
    from app.models.agent import Agent

    agent = Agent(id=uuid.uuid4(), name="Judge", llm_provider="openai", llm_model="gpt-5")

    result, commits = await _builder_update(
        monkeypatch,
        agent,
        llm_model_source="routing",
        llm_model=cleared,
        llm_provider=cleared,
        llm_provider_id=cleared,
    )

    assert not result.is_error
    assert agent.llm_model_source == "routing"
    assert (agent.llm_provider, agent.llm_provider_id, agent.llm_model) == (None, None, None)
    assert commits == 1


# ---------------------------------------------------------------------------
# The REST bodies (create and update): a 422, not a silent discard
# ---------------------------------------------------------------------------

_OWN_MODEL_BODIES = [
    {"llm_model": "gpt-5"},
    {"llm_provider": "openai"},
    {"llm_provider_id": "00000000-0000-0000-0000-0000000000a1"},
    {"llm_provider": "openai", "llm_provider_id": "p1", "llm_model": "gpt-5"},
]


def _bodies():
    from app.api.v1.schemas.agent import CreateAgentRequest, UpdateAgentRequest

    return {
        "create": lambda **kw: CreateAgentRequest(name="Judge", **kw),
        "update": lambda **kw: UpdateAgentRequest(**kw),
    }


@pytest.mark.parametrize("route", ["create", "update"])
@pytest.mark.parametrize("own", _OWN_MODEL_BODIES)
def test_a_body_refuses_the_routing_model_with_a_model_of_its_own(route, own):
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc_info:
        _bodies()[route](llm_model_source="routing", **own)

    errors = exc_info.value.errors()
    assert len(errors) == 1
    assert errors[0]["type"] == "value_error"
    assert f"leave {', '.join(own)} empty" in errors[0]["msg"]


@pytest.mark.parametrize("route", ["create", "update"])
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"llm_model": None, "llm_provider": None, "llm_provider_id": None},
        {"llm_model": "", "llm_provider": "", "llm_provider_id": ""},
    ],
)
def test_a_body_takes_the_routing_model_with_empty_model_fields(route, fields):
    """What the agent page sends: the source with the model fields nulled."""
    body = _bodies()[route](llm_model_source="routing", **fields)

    assert body.llm_model_source == "routing"


@pytest.mark.parametrize("route", ["create", "update"])
def test_a_body_takes_a_model_of_its_own_without_the_routing_source(route):
    body = _bodies()[route](llm_model_source=None, llm_model="gpt-5", llm_provider="openai")

    assert (body.llm_model_source, body.llm_model) == (None, "gpt-5")


# ---------------------------------------------------------------------------
# The activation gate runs on the agent's selection
# ---------------------------------------------------------------------------


async def _gate_router_kwargs(monkeypatch, agent) -> dict:
    from unittest.mock import MagicMock

    from app.services import assignment_engine

    llm = MagicMock()
    llm.chat = AsyncMock(return_value=SimpleNamespace(content="YES", usage=None))
    get_llm = AsyncMock(return_value=(llm, "resolved-model"))
    monkeypatch.setattr("app.llm.router.get_llm_for_user", get_llm)
    monkeypatch.setattr(
        "app.services.langfuse_tracing.start_activation_gate_generation",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr("app.services.langfuse_tracing.end_generation", lambda *_a, **_k: None)

    answer = await assignment_engine._run_activation_gate(
        MagicMock(),
        agent=agent,
        user_id="user-1",
        rendered_activation="Should I run?",
        parent_trace_ctx=None,
    )

    assert answer == "YES"
    get_llm.assert_awaited_once()
    return get_llm.await_args.kwargs


@pytest.mark.asyncio
async def test_the_activation_gate_of_a_routing_agent_runs_on_the_routing_model(monkeypatch):
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)

    kwargs = await _gate_router_kwargs(
        monkeypatch,
        _agent(llm_provider=None, llm_provider_id=None, llm_model=None, llm_model_source="routing"),
    )

    assert kwargs == {
        "provider_override": None,
        "model_override": "small-model",
        "provider_id_override": _ROUTING_PID,
    }


@pytest.mark.asyncio
async def test_the_activation_gate_keeps_the_agent_provider_config_pin(monkeypatch):
    _patch_routing(monkeypatch, "small-model", _ROUTING_PID)
    agent = _agent()

    kwargs = await _gate_router_kwargs(monkeypatch, agent)

    assert kwargs == {
        "provider_override": "openai",
        "model_override": "gpt-5",
        "provider_id_override": agent.llm_provider_id,
    }
