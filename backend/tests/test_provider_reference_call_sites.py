"""The administered model choices forward their provider-config reference.

A model id is not unique across provider configs, so a surface that passes only
the model string lets the router resolve the owner newest-config-first. These
tests pin the reference reaching ``get_llm_for_user`` for the two surfaces whose
reference is new — the General Assistant's own chat model and the routing model —
plus the routing tuple that carries it into flow nodes.

They also pin the no-reference case, which must keep passing ``None`` so
deployments without a duplicated model id resolve exactly as before (FR-006).
"""

import uuid
from types import SimpleNamespace

import pytest


class _Result:
    def __init__(self, obj):
        self._obj = obj

    def scalar_one_or_none(self):
        return self._obj


@pytest.mark.asyncio
async def test_routing_model_returns_the_configured_reference(monkeypatch):
    from app.services import flow_llm

    reference = uuid.uuid4()

    async def _ps(_db):
        return SimpleNamespace(
            routing_model="gpt-4.1-mini", routing_provider_id=reference
        )

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _ps
    )
    assert await flow_llm.routing_model(None) == ("gpt-4.1-mini", reference)


@pytest.mark.asyncio
async def test_routing_model_without_a_model_carries_no_reference(monkeypatch):
    """No routing model means no routing route, so the reference is moot — and a
    legacy settings row has no attribute at all."""
    from app.services import flow_llm

    async def _unset(_db):
        return SimpleNamespace(routing_model=None, routing_provider_id=uuid.uuid4())

    async def _legacy(_db):
        return SimpleNamespace(routing_model="gpt-4.1-mini")

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _unset
    )
    assert await flow_llm.routing_model(None) == (None, None)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _legacy
    )
    assert await flow_llm.routing_model(None) == ("gpt-4.1-mini", None)


@pytest.mark.asyncio
async def test_decision_model_returns_the_configured_reference(monkeypatch):
    from app.services import flow_llm

    reference = uuid.uuid4()

    async def _ps(_db):
        return SimpleNamespace(decision_model="clef-flash", decision_provider_id=reference)

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _ps
    )
    assert await flow_llm.decision_model(None) == ("clef-flash", reference)


@pytest.mark.asyncio
async def test_decision_model_without_a_model_carries_no_reference(monkeypatch):
    from app.services import flow_llm

    async def _unset(_db):
        return SimpleNamespace(decision_model=None, decision_provider_id=uuid.uuid4())

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _unset
    )
    assert await flow_llm.decision_model(None) == (None, None)


@pytest.mark.asyncio
async def test_resolve_llm_uses_the_routing_reference_only_when_the_node_has_none(
    monkeypatch,
):
    """A node that pins its own model owns the route; the platform routing
    reference fills in only when the node pins nothing."""
    from app.services import flow_llm

    seen: list[dict] = []

    async def _get_llm(_db, _user, *, model_override=None, provider_id_override=None):
        seen.append({"model": model_override, "provider_id": provider_id_override})
        return SimpleNamespace(provider_name="stub"), model_override

    monkeypatch.setattr("app.llm.router.get_llm_for_user", _get_llm)

    node_id, routing_id = uuid.uuid4(), uuid.uuid4()

    await flow_llm.resolve_llm(
        None,
        flow_llm.LlmSettings(),
        user_id=None,
        fallback_model="gpt-4.1-mini",
        fallback_provider_id=routing_id,
    )
    assert seen[-1] == {"model": "gpt-4.1-mini", "provider_id": routing_id}

    await flow_llm.resolve_llm(
        None,
        flow_llm.LlmSettings(model="gpt-5", provider_id=node_id),
        user_id=None,
        fallback_model="gpt-4.1-mini",
        fallback_provider_id=routing_id,
    )
    assert seen[-1] == {"model": "gpt-5", "provider_id": node_id}

    # A node that pins a model but no config keeps resolving that model by
    # ownership — the routing reference belongs to the routing model, not to it.
    await flow_llm.resolve_llm(
        None,
        flow_llm.LlmSettings(model="gpt-5"),
        user_id=None,
        fallback_model="gpt-4.1-mini",
        fallback_provider_id=routing_id,
    )
    assert seen[-1] == {"model": "gpt-5", "provider_id": None}


@pytest.mark.asyncio
async def test_routing_evaluator_forwards_the_routing_reference(monkeypatch):
    from app.services import routing_evaluator

    reference = uuid.uuid4()
    seen: list[dict] = []

    async def _ps(_db):
        return SimpleNamespace(
            routing_model="gpt-4.1-mini", routing_provider_id=reference
        )

    async def _get_llm(_db, _user, *, model_override=None, provider_id_override=None):
        seen.append({"model": model_override, "provider_id": provider_id_override})
        raise RuntimeError("stop after resolution")

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _ps
    )
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _get_llm)

    # Two targets, or the evaluator short-circuits without consulting an LLM.
    first, second = uuid.uuid4(), uuid.uuid4()
    await routing_evaluator.evaluate_routing_conditions(
        None,
        agent_output="done",
        mandatory_targets=[
            SimpleNamespace(id=first, name="a", slug="a"),
            SimpleNamespace(id=second, name="b", slug="b"),
        ],
        conditions={str(first): "mentions a", str(second): "mentions b"},
        user_id=None,
    )
    assert seen == [{"model": "gpt-4.1-mini", "provider_id": reference}]


@pytest.mark.asyncio
async def test_routing_evaluator_sends_no_reference_when_no_routing_model(monkeypatch):
    """FR-006: with nothing configured the call is byte-identical to today's."""
    from app.services import routing_evaluator

    seen: list[dict] = []

    async def _ps(_db):
        return SimpleNamespace(routing_model=None, routing_provider_id=uuid.uuid4())

    async def _get_llm(_db, _user, *, model_override=None, provider_id_override=None):
        seen.append({"model": model_override, "provider_id": provider_id_override})
        raise RuntimeError("stop after resolution")

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _ps
    )
    monkeypatch.setattr("app.llm.router.get_llm_for_user", _get_llm)

    # Two targets, or the evaluator short-circuits without consulting an LLM.
    first, second = uuid.uuid4(), uuid.uuid4()
    await routing_evaluator.evaluate_routing_conditions(
        None,
        agent_output="done",
        mandatory_targets=[
            SimpleNamespace(id=first, name="a", slug="a"),
            SimpleNamespace(id=second, name="b", slug="b"),
        ],
        conditions={str(first): "mentions a", str(second): "mentions b"},
        user_id=None,
    )
    assert seen == [{"model": None, "provider_id": None}]


# ── The read path: the three new references round-trip through settings ───────


@pytest.mark.asyncio
async def test_ga_config_serializes_the_three_new_references():
    """The API hands the pickers back what was stored, so a saved choice can be
    re-selected rather than re-guessed."""
    from app.api.v1.settings import _build_ga_response

    ga_ref, routing_ref, fallback_ref = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    config = SimpleNamespace(
        linked_agent_id=None,
        llm_provider="litellm",
        llm_provider_id=ga_ref,
        llm_model="claude-sonnet-4-6",
        max_iterations=25,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        channel_config={},
    )
    ps = SimpleNamespace(
        mcp_exposed_agent_ids=[],
        routing_model="gpt-4.1-mini",
        routing_provider_id=routing_ref,
        fallback_model="gpt-4o",
        fallback_provider_id=fallback_ref,
    )

    out = _build_ga_response(config, ps)
    assert out.llm_provider_id == str(ga_ref)
    assert out.routing_provider_id == str(routing_ref)
    assert out.fallback_provider_id == str(fallback_ref)


@pytest.mark.asyncio
async def test_ga_config_serializes_none_for_rows_without_references():
    """FR-006: a row saved before this feature reports no reference, and the
    serializer must not invent one."""
    from app.api.v1.settings import _build_ga_response

    config = SimpleNamespace(
        linked_agent_id=None,
        llm_provider="anthropic",
        llm_model="claude-sonnet-4-6",
        max_iterations=25,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        channel_config={},
    )

    out = _build_ga_response(config, None)
    assert out.llm_provider_id is None
    assert out.routing_provider_id is None
    assert out.fallback_provider_id is None


# ── Cleanup: a wiped model choice must not leave its reference behind ─────────


@pytest.mark.asyncio
async def test_deleting_a_provider_clears_the_new_references(monkeypatch):
    """A surviving reference outranks the model owner, so a cleanup that nulls
    the model but keeps the reference would leave the GA and the routing/failover
    models pointed at the deleted config's replacement."""
    from app.api.v1 import settings as settings_api

    deleted_id = uuid.uuid4()
    ga = SimpleNamespace(
        llm_provider="litellm",
        llm_provider_id=deleted_id,
        llm_model="claude-sonnet-4-6",
    )
    ps = SimpleNamespace(
        agent_default_provider=None,
        agent_default_provider_id=None,
        agent_default_model=None,
        embedding_provider=None,
        embedding_provider_id=None,
        embedding_model=None,
        image_provider=None,
        image_provider_id=None,
        image_model=None,
        routing_model="gpt-4.1-mini",
        routing_provider_id=deleted_id,
        fallback_model="gpt-4o",
        fallback_provider_id=deleted_id,
    )

    class _Db:
        async def execute(self, _stmt):
            return SimpleNamespace(
                scalar_one_or_none=lambda: ga,
                scalars=lambda: SimpleNamespace(all=lambda: []),
                all=lambda: [],
                rowcount=0,
            )

        async def flush(self):
            return None

    async def _get_ps(_db):
        return ps

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _get_ps
    )

    await settings_api._cleanup_orphaned_model_references(
        _Db(),
        "litellm",
        removed_models={"claude-sonnet-4-6", "gpt-4.1-mini", "gpt-4o"},
        provider_deleted=True,
        provider_id=deleted_id,
    )

    assert ga.llm_provider_id is None
    assert ga.llm_model is None
    assert ps.routing_provider_id is None and ps.routing_model is None
    assert ps.fallback_provider_id is None and ps.fallback_model is None


@pytest.mark.asyncio
async def test_available_models_deduplicates_within_a_config(monkeypatch):
    """`available_models` has no uniqueness constraint — the update endpoint
    stores the request verbatim — so one config can list a model twice. The
    pickers use (provider_id, model) as their option value and React key, so a
    repeat would reinstate the duplicate-key symptom."""
    from app.llm import router

    cfg_id = uuid.uuid4()

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    id=cfg_id,
                    provider="openai",
                    available_models=["gpt-4o", "gpt-4o-mini", "gpt-4o"],
                )
            ]

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    models = await router.get_available_models(_Db())
    assert [m["model"] for m in models] == ["gpt-4o", "gpt-4o-mini"]
    keys = [(m["provider_id"], m["model"]) for m in models]
    assert len(keys) == len(set(keys))


# ── The write path: a PUT persists each reference, and omission leaves it be ──


class _GAWriteDb:
    """Just enough session for ``update_general_assistant_config``.

    One result object serves both queries the endpoint makes: the GA row via
    ``scalar_one_or_none`` and the provider configs via ``scalars().all()`` —
    the latter is what the fallback-model owner gate reads.
    """

    def __init__(self, config, provider_configs=()):
        self._config = config
        self._providers = list(provider_configs)
        self.flushed = False
        self.committed = False

    async def execute(self, _statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: self._config,
            scalars=lambda: SimpleNamespace(all=lambda: self._providers),
            all=lambda: self._providers,
        )

    def add(self, _obj):
        pass

    async def refresh(self, _obj):
        pass

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


def _ga_row():
    return SimpleNamespace(
        llm_provider=None,
        llm_provider_id=None,
        llm_model=None,
        linked_agent_id=None,
        max_iterations=25,
        system_prompt="p",
        knowledge_base_ids=[],
        mcp_server_ids=[],
        custom_tool_ids=[],
        builtin_tool_ids=[],
        integration_bindings={},
        handover_agent_ids=[],
        handover_types={},
        available_channel_types=["web"],
        channel_config={},
    )


def _ps_row():
    return SimpleNamespace(
        agent_default_provider=None,
        agent_default_provider_id=None,
        agent_default_model=None,
        default_reasoning_effort=None,
        embedding_provider=None,
        embedding_provider_id=None,
        embedding_model=None,
        embedding_dimensions=None,
        image_provider=None,
        image_provider_id=None,
        image_model=None,
        routing_model=None,
        routing_provider_id=None,
        fallback_model=None,
        fallback_provider_id=None,
        decision_model=None,
        decision_provider_id=None,
        mcp_exposed_agent_ids=[],
        platform_mcp_enabled=False,
        xtm_mcp_enabled=False,
    )


def _owning_config(*models):
    """A provider config that satisfies the write path's owner gates."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        available_models=list(models),
        created_at=None,
    )


async def _put_ga_config(monkeypatch, body, ga, ps, provider_configs=()):
    """Drive the GA config PUT with a fake session."""
    from fastapi import BackgroundTasks

    from app.api.v1 import settings as settings_api

    async def _get_ps(_db):
        return ps

    async def _no_model_error(_db, _model):
        return None

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings", _get_ps
    )
    monkeypatch.setattr(
        "app.llm.router.validate_selectable_chat_model", _no_model_error
    )
    monkeypatch.setattr(settings_api, "log_audit", _no_audit)

    db = _GAWriteDb(ga, provider_configs)
    return await settings_api.update_general_assistant_config(
        body=body,
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        user=SimpleNamespace(id=uuid.uuid4(), email="admin@example.test"),
        db=db,
    )


@pytest.mark.asyncio
async def test_put_persists_the_three_new_references(monkeypatch):
    """FR-001: a saved choice records the config the admin selected. Without the
    write path these columns stay NULL and resolution falls back to the recency
    guess — the whole defect."""
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest

    ga_ref, routing_ref, fallback_ref = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    ga, ps = _ga_row(), _ps_row()

    body = GeneralAssistantConfigRequest(
        llm_provider="litellm",
        llm_provider_id=str(ga_ref),
        llm_model="claude-sonnet-4-6",
        routing_model="gpt-4.1-mini",
        routing_provider_id=str(routing_ref),
        fallback_model="gpt-4o",
        fallback_provider_id=str(fallback_ref),
    )
    out = await _put_ga_config(
        monkeypatch, body, ga, ps, provider_configs=[_owning_config("gpt-4o")]
    )

    assert ga.llm_provider_id == ga_ref
    assert ps.routing_provider_id == routing_ref
    assert ps.fallback_provider_id == fallback_ref
    assert out.llm_provider_id == str(ga_ref)


@pytest.mark.asyncio
async def test_put_omitting_a_reference_leaves_the_stored_value(monkeypatch):
    """A payload that does not mention a reference must not clear it — the
    pickers send one field group at a time, so a blanket overwrite would wipe the
    references the other sections saved."""
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest

    kept_ga, kept_routing = uuid.uuid4(), uuid.uuid4()
    ga, ps = _ga_row(), _ps_row()
    ga.llm_provider_id = kept_ga
    ps.routing_provider_id = kept_routing

    # Mentions only max_iterations.
    await _put_ga_config(
        monkeypatch, GeneralAssistantConfigRequest(max_iterations=30), ga, ps
    )

    assert ga.llm_provider_id == kept_ga
    assert ps.routing_provider_id == kept_routing


@pytest.mark.asyncio
async def test_put_can_clear_a_reference_explicitly(monkeypatch):
    """Sending it as null is how a picker says "no provider pinned", which must
    reach the column — otherwise a reference can never be unset."""
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest

    ga, ps = _ga_row(), _ps_row()
    ga.llm_provider_id = uuid.uuid4()
    ps.fallback_provider_id = uuid.uuid4()

    await _put_ga_config(
        monkeypatch,
        GeneralAssistantConfigRequest(llm_provider_id=None, fallback_provider_id=None),
        ga,
        ps,
    )

    assert ga.llm_provider_id is None
    assert ps.fallback_provider_id is None


# ── The decision model: a decision-serving provider, named with it ────


class _GAWriteDbWithConfigs(_GAWriteDb):
    async def get(self, _model, ident):
        return next((c for c in self._providers if c.id == ident), None)


async def _put_decision(monkeypatch, ps, provider_configs, **fields):
    from app.api.v1.schemas.settings import GeneralAssistantConfigRequest

    async def _no_problem(*_a, **_k):
        return None

    monkeypatch.setattr(
        "app.api.v1.settings.provider_reference_problem", _no_problem
    )
    from fastapi import BackgroundTasks

    from app.api.v1 import settings as settings_api

    async def _get_ps(_db):
        return ps

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.api.v1.platform_settings_helpers.get_platform_settings", _get_ps)
    monkeypatch.setattr(settings_api, "log_audit", _no_audit)
    return await settings_api.update_general_assistant_config(
        body=GeneralAssistantConfigRequest(**fields),
        request=SimpleNamespace(),
        background_tasks=BackgroundTasks(),
        user=SimpleNamespace(id=uuid.uuid4(), email="admin@example.test"),
        db=_GAWriteDbWithConfigs(_ga_row(), provider_configs),
    )


def _provider_config(provider):
    return SimpleNamespace(id=uuid.uuid4(), provider=provider, available_models=[], created_at=None)


@pytest.mark.asyncio
async def test_put_stores_a_decision_model_with_its_provider(monkeypatch):
    ps, ollama = _ps_row(), _provider_config("ollama")
    out = await _put_decision(
        monkeypatch, ps, [ollama], decision_model="clef-flash", decision_provider_id=str(ollama.id)
    )
    assert (ps.decision_model, ps.decision_provider_id) == ("clef-flash", ollama.id)
    assert (out.decision_model, out.decision_provider_id) == ("clef-flash", str(ollama.id))


@pytest.mark.asyncio
async def test_put_refuses_a_decision_model_without_its_provider(monkeypatch):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await _put_decision(monkeypatch, _ps_row(), [], decision_model="clef-flash")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_put_refuses_a_provider_that_serves_no_decision_model(monkeypatch):
    from fastapi import HTTPException

    anthropic = _provider_config("anthropic")
    with pytest.raises(HTTPException) as exc:
        await _put_decision(
            monkeypatch, _ps_row(), [anthropic],
            decision_model="clef-flash", decision_provider_id=str(anthropic.id),
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_put_clears_the_decision_model_and_its_provider(monkeypatch):
    ps = _ps_row()
    ps.decision_model, ps.decision_provider_id = "clef-flash", uuid.uuid4()
    await _put_decision(monkeypatch, ps, [], decision_model="")
    assert (ps.decision_model, ps.decision_provider_id) == (None, None)


@pytest.mark.asyncio
async def test_available_decision_models_are_listed_from_the_callers_usable_providers(monkeypatch):
    # Like the other capabilities, but only the providers the caller may use:
    # a restricted provider is neither listed nor asked.
    from app.api.v1 import settings as settings_api

    asked = []

    async def _for_capability(_db, capability, _user, *, usable_only=False, listings=None):
        asked.append((capability, usable_only))
        listings["cfg-id"] = "failed"
        return []

    monkeypatch.setattr(settings_api, "_models_for_capability", _for_capability)
    response = await settings_api.list_available_decision_models(user=SimpleNamespace(), db=None)
    assert response.models == []
    assert [(item.provider_id, item.status) for item in response.listings] == [("cfg-id", "failed")]
    assert asked == [("decision", True)]


# ── Overlap is visible when it is created (US3) ───────────────────────────────


def _cfg(model_list, cfg_id=None):
    return SimpleNamespace(
        id=cfg_id or uuid.uuid4(),
        provider="anthropic",
        display_name=None,
        default_model=None,
        available_models=list(model_list),
        context_windows=None,
        max_output_tokens=None,
        is_from_env=False,
        created_at=None,
    )


def test_model_overlaps_is_empty_without_a_shared_model():
    from app.api.v1.settings import _model_overlaps, build_model_owner_index

    a = _cfg(["claude-sonnet-4-6"])
    b = _cfg(["gpt-4o"])
    assert _model_overlaps(a, build_model_owner_index([a, b])) == []


def test_model_overlaps_names_every_config_sharing_a_model():
    """FR-010: the warning has to say which other config it collides with, or an
    admin cannot tell which pair is ambiguous."""
    from app.api.v1.settings import _model_overlaps, build_model_owner_index

    a = _cfg(["claude-sonnet-4-6", "claude-opus-4-8"])
    b = _cfg(["claude-sonnet-4-6"])
    c = _cfg(["claude-sonnet-4-6"])

    overlaps = _model_overlaps(a, build_model_owner_index([a, b, c]))
    assert [o.model for o in overlaps] == ["claude-sonnet-4-6"]
    assert sorted(overlaps[0].provider_ids) == sorted([str(b.id), str(c.id)])


def test_model_overlaps_reports_nothing_without_the_other_configs():
    """A single-config serialization has nothing to compare against, so it must
    report no overlap rather than implying there is none."""
    from app.api.v1.settings import _model_overlaps, build_model_owner_index

    a = _cfg(["claude-sonnet-4-6"])
    assert _model_overlaps(a) == []
    assert _model_overlaps(a, build_model_owner_index([a])) == []


@pytest.mark.asyncio
async def test_listing_providers_reports_overlap_both_ways():
    """Both configs must carry the warning — an admin looking at either one
    should see it, not only whichever was saved second."""
    from app.api.v1.settings import list_llm_providers

    a = _cfg(["claude-sonnet-4-6"])
    b = _cfg(["claude-sonnet-4-6", "gpt-4o"])

    class _Db:
        async def execute(self, _statement):
            return SimpleNamespace(
                scalars=lambda: SimpleNamespace(all=lambda: [a, b])
            )

    out = await list_llm_providers(user=SimpleNamespace(is_admin=True), db=_Db())
    assert [o.model_overlaps[0].model for o in out] == [
        "claude-sonnet-4-6",
        "claude-sonnet-4-6",
    ]
    assert out[0].model_overlaps[0].provider_ids == [str(b.id)]
    assert out[1].model_overlaps[0].provider_ids == [str(a.id)]


def test_model_overlaps_counts_a_repeated_model_once():
    """`available_models` is an unconstrained JSON list, so a config can list a
    model twice. Iterating it raw reported one overlap per repeat and inflated
    the badge count."""
    from app.api.v1.settings import _model_overlaps, build_model_owner_index

    a = _cfg(["claude-sonnet-4-6", "claude-sonnet-4-6"])
    b = _cfg(["claude-sonnet-4-6"])

    overlaps = _model_overlaps(a, build_model_owner_index([a, b]))
    assert [o.model for o in overlaps] == ["claude-sonnet-4-6"]
    assert overlaps[0].provider_ids == [str(b.id)]


def test_cleanup_keeps_a_choice_pinned_to_a_surviving_config():
    """Deleting one config must not wipe a choice pinned to another config of the
    same provider — the stored reference is decisive, the name is not, because
    several configs share it."""
    from app.services.llm_provider_deletion import reference_points_at_deleted

    deleted, survivor = uuid.uuid4(), uuid.uuid4()

    # Pinned to the survivor: same provider name, different config.
    assert not reference_points_at_deleted(
        "anthropic", survivor, "anthropic", deleted
    )
    # Pinned to the config being deleted.
    assert reference_points_at_deleted("anthropic", deleted, "anthropic", deleted)
    # No reference stored (an older choice): the name is all there is.
    assert reference_points_at_deleted("anthropic", None, "anthropic", deleted)
    assert not reference_points_at_deleted("openai", None, "anthropic", deleted)
