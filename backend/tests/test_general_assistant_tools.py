"""The General Assistant's built-in tools and handover targets.

Settings > AI and models > General Assistant offered 8 of the 15 suites ``general_chat`` wires,
hid two a new install seeds ON, listed every enabled agent for handover and
offered a handover type the General Assistant does not implement. Every picker
now reads ONE registry (``BUILTIN_TOOL_GROUPS`` in
``app/services/builtin_tool_groups.py``, served to ``useBuiltinTools()``), pinned here against the runtime, and the settings API refuses what the runtime
cannot honour.
"""

import json
import re
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks, HTTPException

from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.api.v1.settings import (
    _refuse_handover_types_the_ga_cannot_run,
    _refuse_suites_the_ga_cannot_run,
    _refuse_unreachable_ga_handover_targets,
    update_general_assistant_config,
)
from app.models.general_assistant_config import (
    DEFAULT_GA_BUILTIN_TOOL_IDS,
    GENERAL_ASSISTANT_BUILTIN_GROUP_IDS,
    GENERAL_ASSISTANT_BUILTIN_TOOL_IDS,
    GENERAL_ASSISTANT_HANDOVER_TYPES,
    general_assistant_handover_type,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
FRONTEND_SRC = BACKEND_ROOT.parent / "frontend" / "src"

#: Every screen that lets someone pick built-in suites, or lists them.
REGISTRY_READERS = (
    "pages/settings/GeneralAssistantSection.tsx",
    "pages/Profile.tsx",
    "pages/agent-detail/config/ToolingSection.tsx",
    "pages/chat/SessionToolPicker.tsx",
    "pages/chat-panel/useSessionTools.ts",
    "pages/CustomTools.tsx",
    "pages/custom-tool-detail/BuiltinToolDetail.tsx",
)

#: Backend built-in ids no picker offers.
NOT_OFFERED_BY_A_PICKER: set[str] = set()


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _general_chat_suites() -> set[str]:
    """Every suite ``general_chat`` builds tools for (``"x" in ga_builtin_tool_ids``)."""
    source = _read(BACKEND_ROOT / "app" / "services" / "general_chat.py")
    return set(re.findall(r'"(\w+)" in ga_builtin_tool_ids', source))


def _registry() -> dict[str, bool]:
    """``BUILTIN_TOOL_GROUPS`` ids mapped to whether the group is agent-only."""
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    return {group.id: group.agent_keyed for group in BUILTIN_TOOL_GROUPS}


# ---------------------------------------------------------------------------
# One registry, pinned against the runtime
# ---------------------------------------------------------------------------


def test_general_chat_wires_exactly_the_general_assistant_suites():
    assert _general_chat_suites() == set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS)


def test_the_registry_offers_the_general_assistant_exactly_the_suites_it_runs():
    """The bug itself: the settings screen offered 8 of these 15."""
    from app.services.builtin_tool_aliases import alias_group

    offered = {tool_id for tool_id, agent_only in _registry().items() if not agent_only}
    assert offered == set(GENERAL_ASSISTANT_BUILTIN_GROUP_IDS)
    # Every runtime id is a group or a [DEPRECATED] id of one.
    assert {alias_group(t) or t for t in GENERAL_ASSISTANT_BUILTIN_TOOL_IDS} == offered


def test_the_registry_offers_agents_every_selectable_suite_and_nothing_else():
    """``generate_report`` was wired everywhere and offered by no picker.

    Both directions: a suite the platform gains is offered (or named here as
    one no picker offers), and the registry never offers one the platform lacks.
    """
    from app.services.builtin_tool_aliases import is_former_builtin_id
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog
    from app.services.builtin_tools.agent_builder import _VALID_BUILTIN_TOOL_IDS

    registry = set(_registry())
    # [DEPRECATED] ids stay accepted but are not cards: their group is.
    current = {tool_id for tool_id in _VALID_BUILTIN_TOOL_IDS if not is_former_builtin_id(tool_id)}
    assert current <= registry, current - registry
    catalog = set(build_builtin_tool_catalog(include_disabled=True))
    assert registry <= catalog, registry - catalog
    assert catalog - registry == NOT_OFFERED_BY_A_PICKER, catalog - registry


@pytest.mark.parametrize("locale", ["en", "fr", "es", "ja"])
def test_every_registry_string_is_translated(locale):
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    catalog = json.loads(_read(FRONTEND_SRC / "i18n" / "lang" / f"{locale}.json"))
    missing = []
    for group in BUILTIN_TOOL_GROUPS:
        for field in ("name", "tagline", "detail", "category"):
            if field == "name" and group.named_after_platform:
                continue  # titled with the platform's own name
            key = getattr(group, field)
            if key and key not in catalog:
                missing.append((group.id, field, key[:60]))
    assert missing == []


def test_a_new_general_assistant_seeds_only_suites_it_runs():
    assert set(DEFAULT_GA_BUILTIN_TOOL_IDS) <= set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS)


def test_the_frontend_offers_the_handover_types_the_backend_accepts():
    constants = _read(FRONTEND_SRC / "lib" / "constants.ts")
    match = re.search(r"export const GA_HANDOVER_TYPES = \[([^\]]*)\]", constants)
    assert match, "GA_HANDOVER_TYPES was not found where this test reads it"
    assert tuple(re.findall(r'"(\w+)"', match.group(1))) == GENERAL_ASSISTANT_HANDOVER_TYPES


@pytest.mark.parametrize("relative", REGISTRY_READERS)
def test_every_picker_reads_the_registry(relative):
    """Eight hand-maintained copies drifted apart; a copy must not come back."""
    source = _read(FRONTEND_SRC / relative)
    assert "useBuiltinTools" in source
    assert "stix_bundle_validator" not in source
    assert '"Image Generation"' not in source


def test_enable_all_never_drops_a_suite_already_on():
    """It REPLACED the session list, so it turned presentations off."""
    source = _read(FRONTEND_SRC / "pages" / "chat-panel" / "useSessionTools.ts")
    assert "setBuiltinIds((prev) => [...new Set([...prev, ...offered])]);" in source


# ---------------------------------------------------------------------------
# Handover types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stored", "runs_as"),
    [
        ("delegate", "delegate"),
        ("consult", "consult"),
        ("transfer", "transfer"),
        # Announced with the delegate instructions, with no delegate tool.
        ("mandatory_transfer", "delegate"),
        ("bogus", "delegate"),
        (None, "delegate"),
    ],
)
def test_a_stored_handover_type_runs_as_one_the_general_assistant_implements(stored, runs_as):
    assert general_assistant_handover_type(stored) == runs_as


def test_general_chat_reads_the_stored_handover_types_through_the_helper():
    source = _read(BACKEND_ROOT / "app" / "services" / "general_chat.py")
    assert "general_assistant_handover_type(handover_type)" in source


def test_a_handover_type_the_general_assistant_cannot_run_is_refused():
    with pytest.raises(HTTPException) as exc:
        _refuse_handover_types_the_ga_cannot_run({"a": "consult", "b": "mandatory_transfer"}, {})
    assert exc.value.status_code == 422
    assert "mandatory_transfer" in exc.value.detail


def test_every_handover_type_the_general_assistant_runs_is_accepted():
    _refuse_handover_types_the_ga_cannot_run(
        {f"agent-{index}": handover_type for index, handover_type in enumerate(GENERAL_ASSISTANT_HANDOVER_TYPES)},
        None,
    )


def test_a_legacy_handover_type_sent_back_unchanged_is_kept():
    """A client saving the whole map sends back a stored mandatory_transfer; it runs as delegate."""
    _refuse_handover_types_the_ga_cannot_run(
        {"a": "mandatory_transfer", "b": "consult"}, {"a": "mandatory_transfer"}
    )


def test_changing_a_handover_type_to_one_the_general_assistant_cannot_run_is_refused():
    with pytest.raises(HTTPException) as exc:
        _refuse_handover_types_the_ga_cannot_run({"a": "mandatory_transfer"}, {"a": "consult"})
    assert exc.value.status_code == 422


# ---------------------------------------------------------------------------
# Built-in suites
# ---------------------------------------------------------------------------


def test_adding_a_suite_the_general_assistant_cannot_run_is_refused():
    with pytest.raises(HTTPException) as exc:
        _refuse_suites_the_ga_cannot_run(["generate_file", "work_items"], ["generate_file"])
    assert exc.value.status_code == 422
    assert exc.value.detail.startswith("The General Assistant cannot run work_items.")


def test_a_suite_saved_before_the_gate_is_kept():
    """Only what a write adds is checked: an unrelated edit never fails on it."""
    _refuse_suites_the_ga_cannot_run(["generate_file", "work_items"], ["work_items"])


def test_every_suite_the_general_assistant_runs_can_be_added():
    _refuse_suites_the_ga_cannot_run(list(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS), None)


# ---------------------------------------------------------------------------
# Handover targets
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, value):
        self._value = value

    def all(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


class _FlushReachedError(Exception):
    """The handler's first write: every gate has run by then."""


class _Session:
    def __init__(self, *results):
        self._results = list(results)
        self.statements: list = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)

    async def flush(self):
        raise _FlushReachedError


def _agent(
    agent_id: uuid.UUID,
    name: str,
    *,
    company_managed: bool,
    enabled: bool = True,
    access_restricted: bool | None = False,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=agent_id,
        name=name,
        enabled=enabled,
        is_company_managed=company_managed,
        access_restricted=access_restricted,
    )


@pytest.mark.asyncio
async def test_adding_a_private_handover_target_is_refused():
    private = uuid.uuid4()
    db = _Session(_Result([_agent(private, "My private agent", company_managed=False)]))

    with pytest.raises(HTTPException) as exc:
        await _refuse_unreachable_ga_handover_targets(db, [str(private)], [])

    assert exc.value.status_code == 422
    assert '"My private agent" is not company-managed' in exc.value.detail


@pytest.mark.asyncio
async def test_adding_a_company_managed_target_restricted_to_some_people_is_refused():
    """``access_restricted`` withdraws "visible to every user": most users would not reach it."""
    restricted = uuid.uuid4()
    db = _Session(_Result([_agent(restricted, "Legal", company_managed=True, access_restricted=True)]))

    with pytest.raises(HTTPException) as exc:
        await _refuse_unreachable_ga_handover_targets(db, [str(restricted)], [])

    assert exc.value.status_code == 422
    assert '"Legal" is restricted to the people granted access' in exc.value.detail


@pytest.mark.asyncio
async def test_adding_a_disabled_handover_target_is_refused():
    disabled = uuid.uuid4()
    db = _Session(_Result([_agent(disabled, "Paused", company_managed=True, enabled=False)]))

    with pytest.raises(HTTPException) as exc:
        await _refuse_unreachable_ga_handover_targets(db, [str(disabled)], [])

    assert exc.value.status_code == 422
    assert '"Paused" is disabled' in exc.value.detail


@pytest.mark.asyncio
async def test_adding_a_company_managed_handover_target_is_accepted():
    """``access_restricted`` NULL reads as unrestricted, as ``user_visible_filter`` reads it."""
    shared = uuid.uuid4()
    db = _Session(_Result([_agent(shared, "Support", company_managed=True, access_restricted=None)]))

    added = await _refuse_unreachable_ga_handover_targets(db, [str(shared)], [])

    assert added == [str(shared)]
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_a_handover_target_saved_before_the_gate_is_kept_without_a_lookup():
    legacy = str(uuid.uuid4())
    db = _Session()

    assert await _refuse_unreachable_ga_handover_targets(db, [legacy], [legacy]) == []

    assert db.statements == []


@pytest.mark.asyncio
async def test_an_unknown_handover_target_is_refused():
    with pytest.raises(HTTPException) as exc:
        await _refuse_unreachable_ga_handover_targets(_Session(_Result([])), [str(uuid.uuid4())], [])
    assert exc.value.status_code == 422
    assert "was not found" in exc.value.detail


@pytest.mark.asyncio
async def test_a_malformed_handover_target_is_refused_without_a_lookup():
    db = _Session()
    with pytest.raises(HTTPException) as exc:
        await _refuse_unreachable_ga_handover_targets(db, ["not-a-uuid"], [])
    assert exc.value.status_code == 422
    assert "Invalid handover agent id" in exc.value.detail
    assert db.statements == []


# ---------------------------------------------------------------------------
# Through the real endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def platform_settings(monkeypatch):
    async def _fake_platform_settings(_db):
        return SimpleNamespace(
            embedding_provider=None,
            embedding_provider_id=None,
            embedding_model=None,
            embedding_dimensions=None,
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_platform_settings,
    )


async def _save_general_assistant(db: _Session, body: GeneralAssistantConfigRequest):
    return await update_general_assistant_config(
        body=body,
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        db=db,
    )


def test_only_an_admin_may_change_the_general_assistant():
    from app.api.deps import require_admin
    from app.api.v1.settings import router

    route = next(
        route
        for route in router.routes
        if route.path == "/general-assistant" and "PUT" in route.methods
    )
    assert any(dependency.call is require_admin for dependency in route.dependant.dependencies)


@pytest.mark.asyncio
async def test_the_settings_endpoint_refuses_a_private_handover_target(platform_settings):
    """The gate is wired into ``PUT /settings/general-assistant``, not only defined."""
    private = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[], handover_types={}, builtin_tool_ids=[])
    db = _Session(
        _Result(config),
        _Result([_agent(private, "Mine", company_managed=False)]),
    )

    with pytest.raises(HTTPException) as exc:
        await _save_general_assistant(db, GeneralAssistantConfigRequest(handover_agent_ids=[str(private)]))

    assert exc.value.status_code == 422
    assert '"Mine" is not company-managed' in exc.value.detail
    assert config.handover_agent_ids == []


@pytest.mark.asyncio
async def test_a_target_added_without_a_type_to_an_explicit_map_is_recorded_as_delegate(platform_settings):
    """In an explicit map an untyped target runs as delegate: it is written so."""
    existing = str(uuid.uuid4())
    added = uuid.uuid4()
    config = SimpleNamespace(
        handover_agent_ids=[existing], handover_types={existing: "consult"}, builtin_tool_ids=[]
    )
    db = _Session(_Result(config), _Result([_agent(added, "Support", company_managed=True)]))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db, GeneralAssistantConfigRequest(handover_agent_ids=[existing, str(added)])
        )

    assert config.handover_agent_ids == [existing, str(added)]
    assert config.handover_types == {existing: "consult", str(added): "delegate"}


@pytest.mark.asyncio
async def test_a_target_added_to_a_legacy_configuration_keeps_every_handover_tool(platform_settings):
    """An empty map gives the General Assistant every handover tool for every
    target. Recording the new target as delegate would narrow the targets saved
    before it to delegate too, so the map stays empty, as it did at the baseline."""
    legacy = str(uuid.uuid4())
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[legacy], handover_types={}, builtin_tool_ids=[])
    db = _Session(_Result(config), _Result([_agent(added, "Support", company_managed=True)]))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db, GeneralAssistantConfigRequest(handover_agent_ids=[legacy, str(added)])
        )

    assert config.handover_agent_ids == [legacy, str(added)]
    assert config.handover_types == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_types", [None, {}])
async def test_a_first_target_added_without_a_type_is_recorded_as_delegate(platform_settings, stored_types):
    """No stored target runs every type, so nothing is narrowed."""
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[], handover_types=stored_types, builtin_tool_ids=[])
    db = _Session(_Result(config), _Result([_agent(added, "Support", company_managed=True)]))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(db, GeneralAssistantConfigRequest(handover_agent_ids=[str(added)]))

    assert config.handover_agent_ids == [str(added)]
    assert config.handover_types == {str(added): "delegate"}


@pytest.mark.asyncio
async def test_targets_replacing_every_legacy_target_are_recorded_as_delegate(platform_settings):
    """The legacy targets are all removed, so recording the new ones narrows nothing."""
    legacy = str(uuid.uuid4())
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[legacy], handover_types={}, builtin_tool_ids=[])
    db = _Session(_Result(config), _Result([_agent(added, "Support", company_managed=True)]))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(db, GeneralAssistantConfigRequest(handover_agent_ids=[str(added)]))

    assert config.handover_agent_ids == [str(added)]
    assert config.handover_types == {str(added): "delegate"}


@pytest.mark.asyncio
async def test_a_legacy_configuration_saved_back_from_the_screen_keeps_every_handover_tool(platform_settings):
    """The settings screen saves the whole form: an unrelated edit sends the
    targets back with the empty map it read, which stays empty."""
    legacy = str(uuid.uuid4())
    config = SimpleNamespace(handover_agent_ids=[legacy], handover_types={}, builtin_tool_ids=[])
    db = _Session(_Result(config))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db,
            GeneralAssistantConfigRequest(handover_agent_ids=[legacy], handover_types={}),
        )

    assert config.handover_types == {}


@pytest.mark.asyncio
async def test_the_first_type_picked_on_the_screen_is_saved_with_a_type_for_every_target(platform_settings):
    """Picking a type for one target of a legacy configuration types every
    target, the others as delegate, a target saved before the gates included.
    No target is new and every type is one the General Assistant runs, so the
    save goes through as sent, without a target lookup."""
    picked = str(uuid.uuid4())
    other = str(uuid.uuid4())
    saved_before_the_gates = str(uuid.uuid4())
    targets = [picked, other, saved_before_the_gates]
    typed = {picked: "consult", other: "delegate", saved_before_the_gates: "delegate"}
    config = SimpleNamespace(handover_agent_ids=list(targets), handover_types={}, builtin_tool_ids=[])
    db = _Session(_Result(config))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db, GeneralAssistantConfigRequest(handover_agent_ids=targets, handover_types=typed)
        )

    assert config.handover_types == typed
    assert len(db.statements) == 1


@pytest.mark.asyncio
async def test_a_type_sent_with_a_new_target_is_kept(platform_settings):
    added = uuid.uuid4()
    config = SimpleNamespace(handover_agent_ids=[], handover_types={}, builtin_tool_ids=[])
    db = _Session(_Result(config), _Result([_agent(added, "Support", company_managed=True)]))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db,
            GeneralAssistantConfigRequest(
                handover_agent_ids=[str(added)], handover_types={str(added): "consult"}
            ),
        )

    assert config.handover_types == {str(added): "consult"}


@pytest.mark.asyncio
async def test_a_legacy_configuration_saves_unchanged(platform_settings):
    """A private target and a mandatory transfer stored before the gates keep saving."""
    legacy = str(uuid.uuid4())
    config = SimpleNamespace(
        handover_agent_ids=[legacy],
        handover_types={legacy: "mandatory_transfer"},
        builtin_tool_ids=["work_items", "generate_file"],
    )
    db = _Session(_Result(config))

    with pytest.raises(_FlushReachedError):
        await _save_general_assistant(
            db,
            GeneralAssistantConfigRequest(
                builtin_tool_ids=["work_items", "generate_file"],
                handover_agent_ids=[legacy],
                handover_types={legacy: "mandatory_transfer"},
            ),
        )

    assert config.handover_types == {legacy: "mandatory_transfer"}


# ---------------------------------------------------------------------------
# Chat session overrides: a suite that acts as the agent needs its configurator
# ---------------------------------------------------------------------------


def _chat_agent(*, carries: list[str], company_managed: bool, owner=None, **shares) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        builtin_tool_ids=carries,
        is_company_managed=company_managed,
        user_id=owner,
        shared_user_ids=shares.get("shared_user_ids"),
        shared_user_permissions=shares.get("shared_user_permissions"),
        shared_group_ids=shares.get("shared_group_ids"),
        shared_group_permissions=shares.get("shared_group_permissions"),
    )


def _chat_user(*, admin: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role="admin" if admin else "user")


@pytest.fixture
def no_groups(monkeypatch):
    calls: list = []

    async def _fake_groups(_db, user_id):
        calls.append(user_id)
        return []

    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", _fake_groups)
    return calls


def test_the_agent_keyed_additions_of_a_session_are_the_ones_configuring_the_agent_decides():
    from app.services.builtin_tools import agent_keyed_session_additions

    assert agent_keyed_session_additions(
        ["web_fetch", "inspect_my_traces", "work_items", "generate_report"], ["web_fetch"]
    ) == {"work_items"}
    # The trace suites, and the builder that implies them, are decided on every
    # turn by who may read the agent's traces (``_turn_trace_tool_ids``).
    assert agent_keyed_session_additions(["agent_builder", "inspect_agent_traces"], []) == set()
    # What the agent already carries is not an addition, and removing is free.
    assert agent_keyed_session_additions(["shared_state"], ["shared_state"]) == set()
    assert agent_keyed_session_additions([], ["work_items"]) == set()


def test_the_general_assistant_runs_no_suite_that_acts_as_an_agent():
    from app.services.builtin_tools import AGENT_KEYED_BUILTIN_TOOL_IDS

    assert set(GENERAL_ASSISTANT_BUILTIN_TOOL_IDS).isdisjoint(AGENT_KEYED_BUILTIN_TOOL_IDS)


def test_the_registry_marks_agent_only_exactly_the_suites_that_act_as_the_agent():
    """``workset`` is one of them: an assignment or a chat wires it from the agent's own list."""
    from app.services.builtin_tool_aliases import alias_group
    from app.services.builtin_tools import AGENT_KEYED_BUILTIN_TOOL_IDS

    agent_only = {tool_id for tool_id, is_agent_only in _registry().items() if is_agent_only}
    # The backend set also names coordination's [DEPRECATED] ids.
    assert agent_only == {alias_group(t) or t for t in AGENT_KEYED_BUILTIN_TOOL_IDS} - NOT_OFFERED_BY_A_PICKER


@pytest.mark.asyncio
async def test_a_session_cannot_add_a_suite_that_acts_as_an_agent_its_user_does_not_configure(no_groups):
    """A shared agent's work queue or shared state is part of configuring it; its traces - every
    user's conversations with it - are refused by the turn's trace rule, not by the bound."""
    from app.services.agent_chat import _bound_chat_session_builtin_tool_ids, _turn_trace_tool_ids

    agent = _chat_agent(carries=["web_fetch"], company_managed=True)
    requested = ["web_fetch", "inspect_my_traces", "shared_state", "agent_builder", "generate_report"]

    kept = await _bound_chat_session_builtin_tool_ids(object(), _chat_user(), agent, requested)

    assert kept == ["web_fetch", "inspect_my_traces", "agent_builder", "generate_report"]
    assert await _turn_trace_tool_ids(object(), _chat_user(), agent, kept, user_group_ids=[]) == set()


@pytest.mark.asyncio
async def test_a_session_keeps_the_agent_keyed_suites_the_agent_carries_without_a_lookup(no_groups):
    from app.services.agent_chat import _bound_chat_session_builtin_tool_ids

    agent = _chat_agent(carries=["agent_builder", "work_items", "web_fetch"], company_managed=True)

    kept = await _bound_chat_session_builtin_tool_ids(object(), _chat_user(), agent, ["agent_builder", "work_items"])

    assert kept == ["agent_builder", "work_items"]
    assert no_groups == []


@pytest.mark.parametrize(
    ("admin", "company_managed", "owned", "grant"),
    [
        (True, True, False, None),  # an admin configures a company-managed agent
        (False, False, True, None),  # the owner configures their own agent
        (False, False, False, "config"),  # so does a config grantee
    ],
)
@pytest.mark.asyncio
async def test_whoever_may_configure_the_agent_may_add_any_suite(no_groups, admin, company_managed, owned, grant):
    from app.services.agent_chat import _bound_chat_session_builtin_tool_ids

    user = _chat_user(admin=admin)
    agent = _chat_agent(
        carries=[],
        company_managed=company_managed,
        owner=user.id if owned else uuid.uuid4(),
        shared_user_ids=[str(user.id)] if grant else None,
        shared_user_permissions={str(user.id): grant} if grant else None,
    )
    requested = ["work_items", "shared_state", "workset", "agent_builder"]

    assert await _bound_chat_session_builtin_tool_ids(object(), user, agent, requested) == requested


@pytest.mark.asyncio
async def test_a_read_only_grantee_and_an_anonymous_caller_cannot_add_one(no_groups):
    from app.services.agent_chat import _bound_chat_session_builtin_tool_ids

    user = _chat_user()
    agent = _chat_agent(
        carries=[],
        company_managed=False,
        owner=uuid.uuid4(),
        shared_user_ids=[str(user.id)],
        shared_user_permissions={str(user.id): "read_only"},
    )
    for caller in (user, None):
        kept = await _bound_chat_session_builtin_tool_ids(object(), caller, agent, ["work_items", "web_fetch"])
        assert kept == ["web_fetch"]


TRACE_SUITES = {"inspect_my_traces", "inspect_agent_traces"}


@pytest.mark.asyncio
async def test_the_builder_an_agent_carries_reads_its_traces_only_for_whoever_may_read_its_chats(no_groups):
    """The CTEM Assistant carries ``agent_builder`` and is every user's default agent in XTM One
    mode. The trace suites the builder implies read every user's conversations with it
    (``_own_trace_reader``), so they follow the rule for its chats, never every user chatting."""
    from app.services.agent_chat import _turn_trace_tool_ids

    agent = _chat_agent(carries=["agent_builder"], company_managed=True)

    async def suites(user, turn):
        return await _turn_trace_tool_ids(object(), user, agent, turn, user_group_ids=[])

    assert await suites(_chat_user(), ["agent_builder"]) == set()
    assert await suites(None, ["agent_builder"]) == set()
    assert await suites(_chat_user(admin=True), ["agent_builder"]) == TRACE_SUITES
    owner = _chat_user()
    own_agent = _chat_agent(carries=["agent_builder"], company_managed=False, owner=owner.id)
    assert await _turn_trace_tool_ids(object(), owner, own_agent, ["agent_builder"], user_group_ids=[]) == TRACE_SUITES
    # A suite a session adds is held to the same rule, and so is one the agent carries itself.
    assert await suites(_chat_user(), ["inspect_my_traces"]) == set()
    carrying = _chat_agent(carries=["agent_builder", "inspect_my_traces"], company_managed=True)
    assert await _turn_trace_tool_ids(
        object(), _chat_user(), carrying, ["agent_builder", "inspect_my_traces"], user_group_ids=[]
    ) == set()
    assert no_groups == []


@pytest.mark.parametrize(
    ("tier", "may_read"), [("config_with_chats", True), ("config", False), ("read_only", False)]
)
@pytest.mark.asyncio
async def test_a_grantee_reads_a_shared_agents_traces_only_with_chat_access(no_groups, tier, may_read):
    """A trace holds the conversation it records, so ``config`` - configuring the agent - is not
    enough: only ``config_with_chats``, whether the grant names the user or reaches them through
    one of their groups. The groups are the ones the turn already read: no other query."""
    from app.services.agent_chat import _turn_trace_tool_ids
    from app.services.builtin_tools.traces import may_read_agent_traces

    grantee = _chat_user()
    group_id = str(uuid.uuid4())
    direct = _chat_agent(
        carries=["agent_builder"],
        company_managed=False,
        owner=uuid.uuid4(),
        shared_user_ids=[str(grantee.id)],
        shared_user_permissions={str(grantee.id): tier},
    )
    through_group = _chat_agent(
        carries=["agent_builder"],
        company_managed=False,
        owner=uuid.uuid4(),
        shared_group_ids=[group_id],
        shared_group_permissions={group_id: tier},
    )
    expected = TRACE_SUITES if may_read else set()

    assert await may_read_agent_traces(object(), grantee, direct, []) is may_read
    assert await may_read_agent_traces(object(), grantee, through_group, [group_id]) is may_read
    assert await may_read_agent_traces(object(), grantee, through_group, []) is False
    for agent, groups in ((direct, []), (through_group, [group_id])):
        turn = ["agent_builder", "inspect_my_traces"]
        assert await _turn_trace_tool_ids(object(), grantee, agent, turn, user_group_ids=groups) == expected
    assert no_groups == []


def test_the_turn_wires_every_trace_suite_through_the_one_rule():
    agent_chat = _read(BACKEND_ROOT / "app" / "services" / "agent_chat.py")
    run_body = agent_chat.split("async def run_agent_chat(", 1)[1]
    assert run_body.count("build_trace_tools(") == 0
    assert re.search(
        r"_add_trace_tools\(\s*composite,\s*db,\s*user_id,\s*agent,\s*await _turn_trace_tool_ids\(\s*"
        r"db, user_record, agent, agent_builtin_tool_ids, user_group_ids=_user_group_ids",
        run_body,
    )


def test_the_trace_tools_come_with_their_payload_fetch_and_nothing_else():
    from app.services.agent_chat import _add_trace_tools
    from app.services.builtin_tools.traces import TRACE_PAYLOAD_TOOL_NAME
    from app.tools.base import CompositeToolExecutor

    agent = _chat_agent(carries=[], company_managed=True)
    agent.trace_supervision_target_agent_ids = None

    def wired(suites):
        composite = CompositeToolExecutor()
        _add_trace_tools(composite, None, str(uuid.uuid4()), agent, suites)
        return set(composite.tool_names)

    assert wired(set()) == set()
    assert wired({"inspect_my_traces"}) == {"inspect_my_traces", TRACE_PAYLOAD_TOOL_NAME}
    assert wired(TRACE_SUITES) == TRACE_SUITES | {TRACE_PAYLOAD_TOOL_NAME}


def test_every_chat_session_is_bounded_and_only_a_flow_extends_the_agent():
    """Flows compose their session lists from the capability nodes their owner wired, which
    extend an agent by design; every other caller hands in a chat selection."""
    agent_chat = _read(BACKEND_ROOT / "app" / "services" / "agent_chat.py")
    assert "session_tools_from_flow: bool = False" in agent_chat
    assert "if not session_tools_from_flow:" in agent_chat
    assert "await _bound_chat_session_builtin_tool_ids(" in agent_chat

    services = BACKEND_ROOT / "app"
    callers = [
        path
        for path in services.rglob("*.py")
        if "session_tools_from_flow=True" in _read(path)
    ]
    assert callers == [services / "services" / "flow_runner.py"]
