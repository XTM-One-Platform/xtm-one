"""Multi-instance integration bindings: routing + gates.

Two integrations of the SAME provider bound to one agent used to collapse
into a single usable account:

* provider builders register handlers under BASE tool names on the shared
  executor, so the last-built instance silently overwrote the previous one —
  every base-name call executed with the wrong account's credentials;
* the first instance was never instance-labeled (``binding_key != provider``
  is false for the first flattened slot), so the LLM had no way to target a
  specific account;
* allow lists (``integration_allowed_tools``) matched exact names only, so a
  configured tool scope silently dropped every suffixed instance tool — and a
  denied base name did NOT block its suffixed variants.

These tests pin the fixed contract end-to-end.
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

import app.services.integration_tools as integration_tools
from app.models.integration import Integration
from app.services.builtin_tools.filtering import (
    apply_tool_deny_list,
    filter_integration_tools,
    instance_base_tool_name,
    instance_scope_key,
    is_openaev_write_tool,
    narrow_orchestrator_arsenal_tools,
    strip_openaev_write_tools,
)
from app.services.integration_tools import (
    InstanceSlugBudgetError,
    _instance_tool_slug,
    _SafeIntegrationExecutor,
    _suffix_instance_tools,
)
from app.tools.base import ToolCall, ToolDefinition

PROVIDER = "fakeprov"


def _def(
    name: str,
    source: str = f"integration:{PROVIDER}",
    base_name: str | None = None,
) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object", "properties": {}},
        source=source,
        base_name=base_name,
    )


# ── slug generation ──────────────────────────────────────────────────────────


def test_instance_slug_from_name_is_sanitized():
    used: dict[str, set[str]] = {}
    slug = _instance_tool_slug(PROVIDER, "Microsoft - Samuel Hassine", "id-1", used)
    assert slug == "microsoft_samuel_hass"
    assert slug.replace("_", "").isalnum()


def test_instance_slug_unique_for_identical_names():
    used: dict[str, set[str]] = {}
    a = _instance_tool_slug(PROVIDER, "Microsoft", "id-1", used)
    b = _instance_tool_slug(PROVIDER, "Microsoft", "id-2", used)
    assert a != b
    assert b.startswith(a)


def test_instance_slug_fallback_when_name_missing():
    used: dict[str, set[str]] = {}
    integ_id = str(uuid.uuid4())
    a = _instance_tool_slug(PROVIDER, None, integ_id, used)
    b = _instance_tool_slug(PROVIDER, "", str(uuid.uuid4()), used)
    assert a and b and a != b
    assert a.startswith(PROVIDER)


def test_instance_slug_respects_name_length_budget():
    used: dict[str, set[str]] = {}
    slug = _instance_tool_slug(
        PROVIDER, "a very long integration display name", "id-1", used, max_len=10
    )
    assert len(slug) <= 10


def test_instance_slug_counter_never_exceeds_budget():
    # The uniqueness counter must fit INSIDE the budget: an overshoot would
    # produce a >64-char tool name that fails the whole LLM request.
    used: dict[str, set[str]] = {}
    for i in range(12):
        slug = _instance_tool_slug(
            PROVIDER,
            "a very long integration display name",
            f"id-{i}",
            used,
            max_len=10,
        )
        assert len(slug) <= 10, slug
    # All twelve slugs are still distinct despite the re-trimmed stem.
    assert len(used[PROVIDER]) == 12


def test_instance_slug_raises_when_budget_too_small():
    used: dict[str, set[str]] = {}
    with pytest.raises(InstanceSlugBudgetError, match="cannot build an instance slug"):
        _instance_tool_slug(PROVIDER, "Some Name", "id-1", used, max_len=3)
    assert PROVIDER not in used


def test_instance_slug_fallback_is_sanitized_for_custom_provider():
    # A custom provider key carries ':' — invalid in Anthropic/OpenAI tool
    # names — so the id-prefix fallback must be sanitized like the name path.
    used: dict[str, set[str]] = {}
    slug = _instance_tool_slug("custom:my-tool", None, str(uuid.uuid4()), used)
    assert slug
    assert slug.replace("_", "").isalnum()


# ── handler re-routing ───────────────────────────────────────────────────────


def _register_fake_instance(executor, token: str):
    """Simulate a provider builder: register base-name handlers bound to token."""

    async def send_message(text: str = "") -> str:
        return f"sent-with:{token}"

    executor.register("fake_send_message", send_message)
    return [_def("fake_send_message")]


@pytest.mark.asyncio
async def test_suffixed_instances_route_to_their_own_credentials():
    executor = _SafeIntegrationExecutor()
    used: dict[str, set[str]] = {}

    defs_a = _register_fake_instance(executor, "token-A")
    enriched_a = _suffix_instance_tools(
        defs_a,
        executor,
        provider=PROVIDER,
        integ_name="Account A",
        integ_desc=None,
        integration_id="integ-a",
        used_slugs=used,
    )
    # Second instance registers the SAME base names afterwards (the old
    # clobbering scenario).
    defs_b = _register_fake_instance(executor, "token-B")
    enriched_b = _suffix_instance_tools(
        defs_b,
        executor,
        provider=PROVIDER,
        integ_name="Account B",
        integ_desc=None,
        integration_id="integ-b",
        used_slugs=used,
    )

    (def_a,) = enriched_a
    (def_b,) = enriched_b
    assert def_a.name == "fake_send_message__account_a"
    assert def_b.name == "fake_send_message__account_b"
    # The original name is recorded so allow/deny gates match metadata-exact.
    assert def_a.base_name == "fake_send_message"
    assert def_b.base_name == "fake_send_message"
    assert def_a.entity_id == "integ-a"
    assert def_b.entity_id == "integ-b"
    assert def_a.description.startswith("[Account A]")
    assert def_b.description.startswith("[Account B]")

    # Each suffixed tool executes with ITS OWN account's credentials.
    result_a = await executor.execute_tool(ToolCall(id="1", name=def_a.name))
    result_b = await executor.execute_tool(ToolCall(id="2", name=def_b.name))
    assert result_a.content == "sent-with:token-A"
    assert result_b.content == "sent-with:token-B"

    # The base name no longer routes anywhere — no silent
    # "last-built instance wins" fallback.
    stale = await executor.execute_tool(ToolCall(id="3", name="fake_send_message"))
    assert stale.is_error is True


@pytest.mark.asyncio
async def test_suffix_fails_fast_on_overlong_base_name_without_mutation():
    # A base name of 61 chars leaves <4 chars of slug budget under the 64-char
    # ceiling: suffixing must fail fast BEFORE any executor mutation, so the
    # caller can skip this instance instead of emitting invalid tool names.
    long_name = "x" * 61
    executor = _SafeIntegrationExecutor()

    async def handler() -> str:
        return "ok"

    executor.register(long_name, handler)
    with pytest.raises(InstanceSlugBudgetError, match="cannot build an instance slug"):
        _suffix_instance_tools(
            [_def(long_name)],
            executor,
            provider=PROVIDER,
            integ_name="Account A",
            integ_desc=None,
            integration_id="integ-a",
            used_slugs={},
        )
    # The executor was not touched: the base-name handler is still registered.
    result = await executor.execute_tool(ToolCall(id="1", name=long_name))
    assert result.content == "ok"


@pytest.mark.asyncio
async def test_suffixing_drops_definition_without_registered_handler():
    # A definition whose handler was never registered must be DROPPED, not
    # renamed and advertised: the renamed tool would reach the LLM and every
    # call would come back "Unknown tool", feeding the consecutive-error
    # brake. The healthy sibling definition survives untouched.
    executor = _SafeIntegrationExecutor()

    async def send_message() -> str:
        return "ok"

    executor.register("fake_send_message", send_message)
    enriched = _suffix_instance_tools(
        [_def("fake_send_message"), _def("fake_orphan_tool")],
        executor,
        provider=PROVIDER,
        integ_name="Account A",
        integ_desc=None,
        integration_id="integ-a",
        used_slugs={},
    )
    assert [d.name for d in enriched] == ["fake_send_message__account_a"]
    result = await executor.execute_tool(
        ToolCall(id="1", name="fake_send_message__account_a")
    )
    assert result.content == "ok"


def test_instance_label_is_sanitized_and_capped():
    # Integration name/description are user-authored free text prefixed onto
    # every tool description: collapse to a benign charset and hard-cap so a
    # hostile or oversized label cannot flood the model's tool context.
    executor = _SafeIntegrationExecutor()

    async def handler() -> str:
        return "ok"

    executor.register("fake_send_message", handler)
    (enriched,) = _suffix_instance_tools(
        [_def("fake_send_message")],
        executor,
        provider=PROVIDER,
        integ_name="Prod\n<system>! Account",
        integ_desc="d" * 500,
        integration_id="integ-a",
        used_slugs={},
    )
    label = enriched.description.split("]", 1)[0]
    assert "\n" not in label and "<" not in label and "!" not in label
    # name part capped at 80, full label capped at 160 (plus the "[").
    assert len(label) <= 161


# ── end-to-end through _build_integration_tools_inner ────────────────────────


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        return _RowsResult(self._rows)


def _integration_row(integ_id: uuid.UUID, name: str) -> Integration:
    return Integration(
        id=integ_id,
        provider=PROVIDER,
        name=name,
        description=None,
        auth_type="api_key",
    )


@pytest.mark.asyncio
async def test_two_instances_of_one_provider_build_distinct_tools(monkeypatch):
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    rows = [
        _integration_row(id_a, "Filigran Automation"),
        _integration_row(id_b, "Samuel Hassine"),
    ]
    tokens = {str(id_a): "token-A", str(id_b): "token-B"}

    async def fake_get_token(_db, integration_id, _provider, **_kwargs):
        return tokens[str(integration_id)]

    async def fake_parallel_refresh(_db, _rows):
        return {}, set()

    def fake_builder(token, executor, **_kwargs):
        async def send_channel_message(text: str = "") -> str:
            return f"sent-with:{token}"

        executor.register("fake_send_channel_message", send_channel_message)
        return [_def("fake_send_channel_message")]

    monkeypatch.setattr(integration_tools, "_get_token", fake_get_token)
    monkeypatch.setattr(
        integration_tools,
        "_parallel_refresh_expiring_tokens",
        fake_parallel_refresh,
    )
    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, PROVIDER, fake_builder)

    defs, executor = await integration_tools._build_integration_tools_inner(
        _FakeDB(rows),
        {PROVIDER: str(id_a), f"{PROVIDER}#2": str(id_b)},
        {"source": "test"},
    )

    names = sorted(d.name for d in defs)
    assert names == [
        "fake_send_channel_message__filigran_automation",
        "fake_send_channel_message__samuel_hassine",
    ]
    by_name = {d.name: d for d in defs}
    assert by_name[names[0]].entity_id == str(id_a)
    assert by_name[names[1]].entity_id == str(id_b)

    result_a = await executor.execute_tool(ToolCall(id="1", name=names[0]))
    result_b = await executor.execute_tool(ToolCall(id="2", name=names[1]))
    assert result_a.content == "sent-with:token-A"
    assert result_b.content == "sent-with:token-B"

    stale = await executor.execute_tool(
        ToolCall(id="3", name="fake_send_channel_message")
    )
    assert stale.is_error is True


@pytest.mark.asyncio
async def test_single_instance_keeps_base_names(monkeypatch):
    integ_id = uuid.uuid4()
    rows = [_integration_row(integ_id, "Only Account")]

    async def fake_get_token(_db, _integration_id, _provider, **_kwargs):
        return "token-only"

    async def fake_parallel_refresh(_db, _rows):
        return {}, set()

    def fake_builder(token, executor, **_kwargs):
        async def send_channel_message(text: str = "") -> str:
            return f"sent-with:{token}"

        executor.register("fake_send_channel_message", send_channel_message)
        return [_def("fake_send_channel_message")]

    monkeypatch.setattr(integration_tools, "_get_token", fake_get_token)
    monkeypatch.setattr(
        integration_tools,
        "_parallel_refresh_expiring_tokens",
        fake_parallel_refresh,
    )
    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, PROVIDER, fake_builder)

    defs, executor = await integration_tools._build_integration_tools_inner(
        _FakeDB(rows),
        {PROVIDER: str(integ_id)},
        {"source": "test"},
    )

    assert [d.name for d in defs] == ["fake_send_channel_message"]
    result = await executor.execute_tool(
        ToolCall(id="1", name="fake_send_channel_message")
    )
    assert result.content == "sent-with:token-only"


@pytest.mark.asyncio
async def test_stale_binding_does_not_force_multi_instance_naming(monkeypatch):
    # One usable binding plus one STALE binding (integration row deleted) must
    # remain a single-instance provider: the stale reference is skipped by the
    # build loop, so the surviving account keeps its backward-compatible base
    # tool names (an allow list authored against them must not break).
    integ_id = uuid.uuid4()
    stale_id = uuid.uuid4()
    rows = [_integration_row(integ_id, "Only Account")]

    async def fake_get_token(_db, _integration_id, _provider, **_kwargs):
        return "token-only"

    async def fake_parallel_refresh(_db, _rows):
        return {}, set()

    def fake_builder(token, executor, **_kwargs):
        async def send_channel_message(text: str = "") -> str:
            return f"sent-with:{token}"

        executor.register("fake_send_channel_message", send_channel_message)
        return [_def("fake_send_channel_message")]

    monkeypatch.setattr(integration_tools, "_get_token", fake_get_token)
    monkeypatch.setattr(
        integration_tools,
        "_parallel_refresh_expiring_tokens",
        fake_parallel_refresh,
    )
    monkeypatch.setitem(integration_tools.PROVIDER_TOOLS, PROVIDER, fake_builder)

    defs, executor = await integration_tools._build_integration_tools_inner(
        _FakeDB(rows),
        {PROVIDER: str(integ_id), f"{PROVIDER}#2": str(stale_id)},
        {"source": "test"},
    )

    assert [d.name for d in defs] == ["fake_send_channel_message"]
    result = await executor.execute_tool(
        ToolCall(id="1", name="fake_send_channel_message")
    )
    assert result.content == "sent-with:token-only"


# ── gates: allow lists + deny lists must match base names ────────────────────


def test_instance_base_tool_name():
    # Metadata-first: the recorded base wins.
    suffixed = _def("fake_send__account_a", base_name="fake_send")
    assert instance_base_tool_name(suffixed) == "fake_send"
    # A definition without a recorded base was never renamed: its name IS the
    # base — even when the user-authored name legitimately contains ``__``.
    assert instance_base_tool_name(_def("sync__all")) == "sync__all"
    assert instance_base_tool_name(_def("fake_send")) == "fake_send"
    # Bare strings keep the split heuristic (fail-closed OpenAEV gate use).
    assert instance_base_tool_name("fake_send__account_a") == "fake_send"
    assert instance_base_tool_name("fake_send") == "fake_send"


def test_allow_list_with_base_names_keeps_suffixed_instances():
    defs = [
        _def("fake_send__account_a", base_name="fake_send"),
        _def("fake_send__account_b", base_name="fake_send"),
        _def("fake_delete__account_a", base_name="fake_delete"),
    ]
    filtered = filter_integration_tools(defs, {PROVIDER: ["fake_send"]})
    assert sorted(d.name for d in filtered) == [
        "fake_send__account_a",
        "fake_send__account_b",
    ]


def test_allow_list_does_not_widen_for_custom_names_containing_separator():
    # Custom providers accept user-authored tool names verbatim, so a name
    # may legitimately contain ``__``. Without a recorded base_name the name
    # IS the base: an allow list of ["sync"] must NOT admit "sync__all" /
    # "sync__dry_run" through a naming coincidence (an allow list is a
    # scoping boundary and must never grow).
    defs = [_def("sync__all"), _def("sync__dry_run"), _def("sync")]
    filtered = filter_integration_tools(defs, {PROVIDER: ["sync"]})
    assert [d.name for d in filtered] == ["sync"]


def test_allow_list_none_passes_all_instances():
    defs = [
        _def("fake_send__account_a", base_name="fake_send"),
        _def("fake_send__account_b", base_name="fake_send"),
    ]
    assert filter_integration_tools(defs, {PROVIDER: None}) == defs


def _entity_def(name: str, entity_id: str, base_name: str | None = None) -> ToolDefinition:
    return dataclasses.replace(_def(name, base_name=base_name), entity_id=entity_id)


def test_instance_scope_key_restricts_only_that_integration():
    """Every OpenAPI integration shares one provider key but exposes its own
    API: a per-integration key scopes one of them without touching another."""
    pets, erp = str(uuid.uuid4()), str(uuid.uuid4())
    defs = [
        _entity_def("pets_list__prod", pets, base_name="pets_list"),
        _entity_def("pets_delete__prod", pets, base_name="pets_delete"),
        _entity_def("erp_list_orders__erp", erp, base_name="erp_list_orders"),
    ]
    filtered = filter_integration_tools(defs, {instance_scope_key(PROVIDER, pets): ["pets_list"]})
    assert sorted(d.name for d in filtered) == ["erp_list_orders__erp", "pets_list__prod"]
    # An unrestricted instance key changes nothing.
    assert filter_integration_tools(defs, {instance_scope_key(PROVIDER, pets): None}) == defs


def test_provider_and_instance_scopes_both_apply():
    integration_id = str(uuid.uuid4())
    defs = [_entity_def(name, integration_id) for name in ("x", "y", "z")]
    allowed = {PROVIDER: ["x", "y"], instance_scope_key(PROVIDER, integration_id): ["y", "z"]}
    assert [d.name for d in filter_integration_tools(defs, allowed)] == ["y"]


def test_agent_and_assignment_instance_scopes_intersect():
    from app.services.builtin_tools.filtering import merge_tool_allow_lists

    key = instance_scope_key(PROVIDER, str(uuid.uuid4()))
    merged = merge_tool_allow_lists({key: ["x", "y"]}, {key: ["y", "z"]})
    assert sorted(merged[key]) == ["y"]


def test_prune_keeps_instance_scopes_of_bound_integrations_only():
    from app.models.agent import Agent

    bound, unbound = str(uuid.uuid4()), str(uuid.uuid4())
    agent = Agent(
        integration_bindings={PROVIDER: [bound]},
        integration_allowed_tools={
            instance_scope_key(PROVIDER, bound): ["x"],
            instance_scope_key(PROVIDER, unbound): ["y"],
            instance_scope_key("github", bound): ["z"],
            PROVIDER: ["x"],
            "github": ["z"],
        },
    )
    agent.prune_orphaned_integration_scopes()
    assert agent.integration_allowed_tools == {
        instance_scope_key(PROVIDER, bound): ["x"],
        PROVIDER: ["x"],
    }


def test_deny_list_blocks_every_instance_of_a_denied_base_name():
    defs = [
        _def("fake_delete__account_a", base_name="fake_delete"),
        _def("fake_delete__account_b", base_name="fake_delete"),
        _def("fake_send__account_a", base_name="fake_send"),
    ]
    kept = apply_tool_deny_list(defs, ["fake_delete"])
    assert [d.name for d in kept] == ["fake_send__account_a"]


def test_deny_list_base_match_is_metadata_scoped():
    # A tool legitimately containing ``__`` in its own name (MCP or custom —
    # no recorded base_name) must NOT be over-denied by a split heuristic.
    mcp_tool = _def("fake_delete__everything", source="mcp:some-server")
    custom_tool = _def("delete__records")
    kept = apply_tool_deny_list([mcp_tool, custom_tool], ["fake_delete", "delete"])
    assert kept == [mcp_tool, custom_tool]
    # Exact-name denial still applies to any source...
    assert apply_tool_deny_list([mcp_tool], ["fake_delete__everything"]) == []
    # ...and denying the REAL base of such a name blocks its suffixed
    # instances via the recorded base_name (the backstop).
    suffixed = _def("delete__records__account_a", base_name="delete__records")
    assert apply_tool_deny_list([suffixed], ["delete__records"]) == []


# ── OpenAEV advisory gates must see through instance suffixes ────────────────


def _arsenal_def(name: str) -> ToolDefinition:
    return ToolDefinition(
        name=name,
        description="Manage the threat arsenal",
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "get", "create", "update", "bulk_delete"],
                }
            },
        },
        source="integration:openaev",
    )


def test_is_openaev_write_tool_matches_suffixed_variants():
    assert is_openaev_write_tool("set_openaev_run_scope__acme")
    assert is_openaev_write_tool("manage_openaev_asset__acme")
    assert not is_openaev_write_tool("search_openaev_assets__acme")


def test_strip_openaev_write_tools_strips_suffixed_run_scoped_writes():
    # With two OpenAEV platforms bound to one agent, the run-scoped
    # write tools carry an instance suffix — the advisory strip must still
    # withhold them, or a consulted specialist could mutate run state.
    defs = [
        _def("set_openaev_run_scope__acme", source="integration:openaev"),
        _def("search_openaev_assets__acme", source="integration:openaev"),
        _arsenal_def("manage_openaev_threat_arsenal__acme"),
    ]
    kept = strip_openaev_write_tools(defs, allow_arsenal=True)
    names = [d.name for d in kept]
    assert "set_openaev_run_scope__acme" not in names
    assert "search_openaev_assets__acme" in names
    # The suffixed arsenal tool is NARROWED (kept, destructive actions
    # dropped), exactly like its base-name form.
    arsenal = next(
        d for d in kept if d.name == "manage_openaev_threat_arsenal__acme"
    )
    assert "bulk_delete" not in arsenal.input_schema["properties"]["action"]["enum"]


def test_narrow_orchestrator_arsenal_tools_rewrites_suffixed_variant():
    (narrowed,) = narrow_orchestrator_arsenal_tools(
        [_arsenal_def("manage_openaev_threat_arsenal__acme")]
    )
    assert "bulk_delete" not in narrowed.input_schema["properties"]["action"]["enum"]
    assert "create" in narrowed.input_schema["properties"]["action"]["enum"]
