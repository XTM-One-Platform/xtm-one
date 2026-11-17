"""Tests for the admin-configured explicit fallback model in FailoverLLMProvider.

When ``PlatformSettings.fallback_model`` (``DEFAULT_FALLBACK_MODEL``) is set,
the provider that owns that model is tried FIRST on failover with that exact
model — before the generic per-provider chain (which uses each provider's own
default model). These tests pin that contract:

1. The explicit fallback model is tried first, with its exact model id.
2. The explicit fallback's provider is not re-tried with its default model.
3. With no explicit fallback configured, behaviour is unchanged (model=None).

``resolve_configured_fallback`` is the shared resolution behind this setting:
the failover chain reacts to a **provider error**, while the agent loop's
per-run budget switches on **cost exhaustion**, and the two must never
disagree about which model the platform falls back to.
"""

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from app.llm import router as router_module
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.llm.router import FailoverLLMProvider


class _AlwaysFailPrimary(BaseLLMProvider):
    provider_name = "anthropic"

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        raise RuntimeError("overloaded_error: primary rate limited")

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> AsyncIterator[str]:
        if False:
            yield ""
        raise RuntimeError("overloaded_error: primary rate limited")

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


class _RecordingFallback(BaseLLMProvider):
    provider_name = "openai"
    default_model = "gpt-4o"
    max_output_tokens = 16_384

    def __init__(self, name: str = "openai") -> None:
        self.provider_name = name
        self.chat_calls: list[dict] = []
        self.stream_calls: list[dict] = []

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        self.chat_calls.append({"model": model, "max_tokens": max_tokens})
        return ChatResponse(content="ok", model=model or self.default_model, usage={})

    async def chat_stream(
        self, messages, model=None, temperature=0.7, max_tokens=4096, **kwargs
    ) -> AsyncIterator[str]:
        self.stream_calls.append({"model": model, "max_tokens": max_tokens})
        yield "ok"

    async def embed(self, text, model=None):
        return []

    async def list_models(self):
        return []


@pytest.mark.asyncio
async def test_explicit_fallback_model_tried_first(monkeypatch):
    explicit = _RecordingFallback(name="openai")
    fake_config = SimpleNamespace(id="cfg-openai", provider="openai")

    # ``_iter_fallback_targets`` builds the explicit provider via
    # ``_get_cached_provider`` — return the recording provider for it.
    monkeypatch.setattr(
        router_module, "_get_cached_provider", lambda cfg: explicit
    )

    failover = FailoverLLMProvider(
        primary=_AlwaysFailPrimary(),
        fallback_configs=[],
        fallback_model="gpt-4o-mini",
        fallback_model_config=fake_config,
    )

    response = await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="claude-sonnet-4", max_tokens=4096
    )

    assert response.content == "ok"
    # Explicit model id passed through (not None), clamped to the provider limit.
    assert explicit.chat_calls == [{"model": "gpt-4o-mini", "max_tokens": 4096}]


@pytest.mark.asyncio
async def test_explicit_fallback_model_tried_first_streaming(monkeypatch):
    """``chat_stream()`` must honour the explicit fallback model too — it shares
    ``_iter_fallback_targets`` with ``chat()``, so this guards against the
    streaming path regressing while the non-streaming path keeps working."""
    explicit = _RecordingFallback(name="openai")
    fake_config = SimpleNamespace(id="cfg-openai", provider="openai")

    monkeypatch.setattr(router_module, "_get_cached_provider", lambda cfg: explicit)

    failover = FailoverLLMProvider(
        primary=_AlwaysFailPrimary(),
        fallback_configs=[],
        fallback_model="gpt-4o-mini",
        fallback_model_config=fake_config,
    )

    chunks = [
        chunk
        async for chunk in failover.chat_stream(
            [ChatMessage(role="user", content="Hello")],
            model="claude-sonnet-4",
            max_tokens=4096,
        )
    ]

    assert "".join(chunks) == "ok"
    # The explicit model id is passed through on the streaming failover path too.
    assert explicit.stream_calls == [{"model": "gpt-4o-mini", "max_tokens": 4096}]


@pytest.mark.asyncio
async def test_explicit_fallback_skips_same_provider_default(monkeypatch):
    """When the explicit fallback's provider also appears in the generic chain,
    it must not be retried a second time with its default model."""
    explicit = _RecordingFallback(name="openai")
    fake_config = SimpleNamespace(id="cfg-openai", provider="openai")

    # Both the explicit fallback config and the generic chain entry resolve to
    # the same provider instance.
    monkeypatch.setattr(
        router_module, "_get_cached_provider", lambda cfg: explicit
    )

    failover = FailoverLLMProvider(
        primary=_AlwaysFailPrimary(),
        fallback_configs=[SimpleNamespace(id="cfg-openai", provider="openai")],
        fallback_model="gpt-4o-mini",
        fallback_model_config=fake_config,
    )

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="claude-sonnet-4", max_tokens=4096
    )

    # Exactly one attempt — the explicit model — not a second default-model retry.
    assert explicit.chat_calls == [{"model": "gpt-4o-mini", "max_tokens": 4096}]


@pytest.mark.asyncio
async def test_no_explicit_fallback_uses_provider_default():
    """Without a configured fallback model, the generic chain passes model=None."""
    explicit = _RecordingFallback(name="openai")
    failover = FailoverLLMProvider(primary=_AlwaysFailPrimary(), fallbacks=[explicit])

    await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="claude-sonnet-4", max_tokens=4096
    )

    assert explicit.chat_calls == [{"model": None, "max_tokens": 4096}]


@pytest.mark.asyncio
async def test_explicit_fallback_then_generic_chain(monkeypatch):
    """The explicit fallback is tried first; if it fails, the generic chain
    (a different provider, default model) is tried next."""
    explicit = _RecordingFallback(name="openai")
    generic = _RecordingFallback(name="gemini")

    async def _explicit_fail(messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        explicit.chat_calls.append({"model": model, "max_tokens": max_tokens})
        raise RuntimeError("explicit fallback unavailable")

    explicit.chat = _explicit_fail  # type: ignore[method-assign]

    fake_config = SimpleNamespace(id="cfg-openai", provider="openai")

    def _resolve(cfg):
        # Explicit config → explicit provider; generic chain config → generic.
        if getattr(cfg, "provider", None) == "openai":
            return explicit
        return generic

    monkeypatch.setattr(router_module, "_get_cached_provider", _resolve)

    failover = FailoverLLMProvider(
        primary=_AlwaysFailPrimary(),
        fallback_configs=[SimpleNamespace(id="cfg-gemini", provider="gemini")],
        fallback_model="gpt-4o-mini",
        fallback_model_config=fake_config,
    )

    response = await failover.chat(
        [ChatMessage(role="user", content="Hello")], model="claude-sonnet-4", max_tokens=4096
    )

    assert response.content == "ok"
    assert explicit.chat_calls == [{"model": "gpt-4o-mini", "max_tokens": 4096}]
    # Generic chain entry tried with its own default model (None).
    assert generic.chat_calls == [{"model": None, "max_tokens": 4096}]


def test_fallback_model_acceptance_uses_owner_resolution():
    """The fallback model may be pinned WITHOUT being in any provider's
    ``available_models``: seeding (``main._seed_llm_providers_from_env``) and the
    settings API both gate on ``_resolve_model_owner_config`` — the SAME
    resolution the runtime failover uses — so a model is accepted whenever it
    resolves to a configured provider (explicit membership OR model-name prefix
    inference), and rejected only when no configured provider owns it.
    """
    from app.llm.router import _resolve_model_owner_config

    configs = [
        SimpleNamespace(provider="openai", available_models=["gpt-4o", "gpt-4o-mini"]),
        SimpleNamespace(provider="anthropic", available_models=["claude-sonnet-4"]),
    ]
    # Explicit membership is accepted.
    assert _resolve_model_owner_config(configs, "gpt-4o-mini") is not None
    # Not listed, but its prefix maps to a CONFIGURED provider (anthropic) ->
    # accepted. This is the "pin claude-opus-4-8 as fallback without exposing it
    # in the selectable model list" case.
    opus = _resolve_model_owner_config(configs, "claude-opus-4-8")
    assert opus is not None and opus.provider == "anthropic"
    # Prefix maps to a provider that is NOT configured -> rejected.
    assert _resolve_model_owner_config(configs, "gemini-2.5-pro") is None
    # No recognizable prefix and not listed -> rejected.
    assert _resolve_model_owner_config(configs, "totally-unknown-model") is None
    # Empty / missing inputs -> rejected.
    assert _resolve_model_owner_config(configs, "") is None
    assert _resolve_model_owner_config(configs, None) is None
    assert _resolve_model_owner_config([], "gpt-4o") is None


def test_settings_api_rejects_non_chat_fallback_model(monkeypatch):
    """The Settings API write path must reject a non-chat fallback model.

    The owner-resolution gate alone accepts ``gpt-3.5-turbo-instruct`` (it is a
    catalog member / matches the ``gpt-`` prefix), but every failover attempt
    to a completion-only model 404s on ``/v1/chat/completions`` — persisting it
    makes the entire redundancy chain non-functional. The write path must
    refuse it with a 422 before it ever reaches ``PlatformSettings``.
    """
    import uuid as _uuid

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.deps import require_admin
    from app.api.v1 import settings as settings_api
    from app.core.database import get_db

    class _ScalarOneResult:
        def __init__(self, value):
            self._value = value

        def scalar_one_or_none(self):
            return self._value

    class _SeqDB:
        def __init__(self, results):
            self._results = list(results)

        async def execute(self, stmt, *args, **kwargs):
            # The provider config lock returns nothing the endpoint reads.
            if getattr(stmt, "_for_update_arg", None) is not None or "pg_advisory" in str(stmt):
                return _ScalarOneResult(None)
            return self._results.pop(0)

        async def refresh(self, _obj):
            pass

    ga_config = SimpleNamespace()
    platform_settings = SimpleNamespace(
        agent_default_provider=None,
        agent_default_provider_id=None,
        embedding_provider=None,
        embedding_provider_id=None,
        embedding_model=None,
        embedding_dimensions=None,
        image_provider=None,
        image_provider_id=None,
    )
    fake_db = _SeqDB(
        [_ScalarOneResult(ga_config), _ScalarOneResult(platform_settings)]
    )

    # Neutralize env locks so the test is independent of local DEFAULT_* vars.
    monkeypatch.setattr(settings_api, "_env_managed_ga_fields", set)

    app = FastAPI()
    app.include_router(settings_api.router, prefix="/api/v1/settings")

    async def _get_db():
        yield fake_db

    fake_admin = SimpleNamespace(id=_uuid.uuid4(), is_admin=True, full_name="Admin")
    app.dependency_overrides[require_admin] = lambda: fake_admin
    app.dependency_overrides[get_db] = _get_db

    client = TestClient(app)
    resp = client.put(
        "/api/v1/settings/general-assistant",
        json={"fallback_model": "gpt-3.5-turbo-instruct"},
    )

    assert resp.status_code == 422, resp.text
    assert "not a chat-capable model" in resp.json()["detail"]


def test_env_seed_guards_fallback_model_chat_capability():
    """``DEFAULT_FALLBACK_MODEL`` env seeding must check chat-capability BEFORE
    persisting: the env value is re-applied on every boot, so a non-chat model
    that slipped past the owner check would keep the failover chain broken
    forever. Source-level regression pin, mirroring the seeding-path
    conventions asserted in ``test_settings_llm_providers.py`` (the backend
    suite has no real-DB startup harness).
    """
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    guard_idx = source.find("is_chat_capable_model(fallback_default_model)")
    assert guard_idx > 0, "chat-capability guard missing from fallback seeding"
    assign_idx = source.find("ps.fallback_model = fallback_default_model")
    assert assign_idx > 0, "fallback model assignment not found"
    assert guard_idx < assign_idx, (
        "the chat-capability guard must run before the fallback model is "
        "persisted to PlatformSettings"
    )


def test_resolve_owner_config_deterministic_across_duplicate_provider_rows():
    """The data model allows several configs for the same provider (no unique
    constraint on ``provider`` — e.g. an env-seeded row plus a UI-created one),
    and callers load them via an unordered ``select(LLMProviderConfig)``. Owner
    resolution must therefore be deterministic: the most recently created
    config wins, regardless of input/query row order, so the explicit fallback
    model can't bind to an arbitrary provider config / API key.
    """
    from datetime import UTC, datetime

    from app.llm.router import _resolve_model_owner_config

    older = SimpleNamespace(
        provider="anthropic",
        available_models=["claude-sonnet-4"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        id="older",
    )
    newer = SimpleNamespace(
        provider="anthropic",
        available_models=["claude-sonnet-4"],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        id="newer",
    )
    # ``claude-opus-4-8`` is in neither ``available_models`` -> prefix inference
    # to anthropic. Both row orderings must resolve to the newer config.
    for ordering in ([older, newer], [newer, older]):
        owner = _resolve_model_owner_config(ordering, "claude-opus-4-8")
        assert owner is newer

    # Same determinism for the explicit-membership branch when the model is
    # listed by duplicate same-provider rows.
    older.available_models = ["claude-opus-4-8"]
    newer.available_models = ["claude-opus-4-8"]
    for ordering in ([older, newer], [newer, older]):
        assert _resolve_model_owner_config(ordering, "claude-opus-4-8") is newer


# ── The shared fallback-model resolution ─────────────────────────


def _configs():
    return [
        SimpleNamespace(
            id=1, provider="openai", available_models=["gpt-4o", "gpt-4o-mini"],
            created_at=None,
        ),
        SimpleNamespace(
            id=2, provider="anthropic", available_models=["claude-opus-4-8"],
            created_at=None,
        ),
    ]


def test_resolve_configured_fallback_returns_model_and_owner():
    """One resolution, two consumers: the failover chain and the budget-driven
    downgrade must agree on which model the platform falls back to."""
    from app.llm.router import resolve_configured_fallback

    model, owner = resolve_configured_fallback(
        _configs(),
        SimpleNamespace(fallback_model="gpt-4o-mini"),
        current_model="claude-opus-4-8",
    )
    assert model == "gpt-4o-mini"
    assert owner is not None and owner.provider == "openai"


def test_resolve_configured_fallback_skips_the_model_already_in_use():
    """A same-model fallback is pointless — for a provider error and for a cost
    downgrade alike."""
    from app.llm.router import resolve_configured_fallback

    assert resolve_configured_fallback(
        _configs(),
        SimpleNamespace(fallback_model="gpt-4o-mini"),
        current_model="gpt-4o-mini",
    ) == (None, None)


def test_resolve_configured_fallback_rejects_unusable_values(monkeypatch):
    """Nothing configured, a non-chat model, and a model no configured provider
    owns all resolve to "no fallback"."""
    from app.llm import router as r

    monkeypatch.setattr(r, "_warned_fallback_model", set())
    assert r.resolve_configured_fallback(
        _configs(), SimpleNamespace(fallback_model=None), current_model="gpt-4o"
    ) == (None, None)
    assert r.resolve_configured_fallback(
        _configs(),
        SimpleNamespace(fallback_model="gpt-3.5-turbo-instruct"),
        current_model="gpt-4o",
    ) == (None, None)
    assert r.resolve_configured_fallback(
        _configs(),
        SimpleNamespace(fallback_model="gemini-2.5-flash"),
        current_model="gpt-4o",
    ) == (None, None)


@pytest.mark.asyncio
async def test_resolve_fallback_model_provider_never_raises(monkeypatch):
    """A downgrade that cannot be prepared must degrade to "no downgrade", not
    fail the run."""
    from app.llm import router as r

    async def _boom(*a, **kw):
        raise RuntimeError("db is down")

    monkeypatch.setattr(r, "resolve_configured_fallback", _boom)
    assert await r.resolve_fallback_model_provider(
        SimpleNamespace(execute=_boom), current_model="gpt-4o"
    ) is None


def test_resolve_configured_fallback_prefers_the_stored_reference():
    """When two configs advertise the fallback model, the recency winner
    is not necessarily the one the admin picked. The stored reference decides,
    so failover uses the API key the admin chose."""
    from datetime import UTC, datetime

    from app.llm.router import resolve_configured_fallback

    older = SimpleNamespace(
        id="older",
        provider="openai",
        available_models=["gpt-4o-mini"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    newer = SimpleNamespace(
        id="newer",
        provider="openai",
        available_models=["gpt-4o-mini"],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    ps = SimpleNamespace(fallback_model="gpt-4o-mini", fallback_provider_id="older")

    for ordering in ([older, newer], [newer, older]):
        model, owner = resolve_configured_fallback(
            ordering, ps, current_model="claude-opus-4-8"
        )
        assert model == "gpt-4o-mini"
        assert owner is older  # the reference, not the recency winner


def test_resolve_configured_fallback_ignores_a_dangling_reference():
    """A reference to a deleted config falls through to the model-owner lookup
    rather than dropping the fallback entirely."""
    from app.llm.router import resolve_configured_fallback

    ps = SimpleNamespace(fallback_model="gpt-4o-mini", fallback_provider_id="deleted")
    model, owner = resolve_configured_fallback(
        _configs(), ps, current_model="claude-opus-4-8"
    )
    assert model == "gpt-4o-mini"
    assert owner is not None and owner.provider == "openai"


def test_resolve_configured_fallback_ignores_a_reference_that_cannot_serve():
    """Same safety net as the router: a reference that does not advertise the
    fallback model would 404, so the model owner wins."""
    from app.llm.router import resolve_configured_fallback

    configs = _configs()  # id=1 openai owns gpt-4o-mini, id=2 anthropic does not
    ps = SimpleNamespace(fallback_model="gpt-4o-mini", fallback_provider_id=2)
    model, owner = resolve_configured_fallback(
        configs, ps, current_model="claude-opus-4-8"
    )
    assert model == "gpt-4o-mini"
    assert owner is not None and owner.provider == "openai"


def test_resolve_configured_fallback_honours_a_prefix_inferred_reference():
    """The write path accepts a fallback model that is in no catalog, resolved by
    name prefix alone (``settings.py`` / the env seeder both say so). A reference
    saved alongside such a pin must still be honoured, or it is ignored for
    exactly the setup the API documents as supported."""
    from app.llm.router import resolve_configured_fallback

    gateway = SimpleNamespace(
        id="gateway", provider="anthropic", available_models=[], created_at=None
    )
    direct = SimpleNamespace(
        id="direct",
        provider="anthropic",
        available_models=["claude-sonnet-4-6"],
        created_at=None,
    )
    ps = SimpleNamespace(
        fallback_model="claude-opus-4-8", fallback_provider_id="gateway"
    )

    model, owner = resolve_configured_fallback(
        [gateway, direct], ps, current_model="gpt-4o"
    )
    assert model == "claude-opus-4-8"
    assert owner is gateway  # not the catalog-carrying row


def test_resolve_configured_fallback_logs_a_reference_it_cannot_use(monkeypatch, caplog):
    """The no-FK comment on the column promises a dangling reference is logged;
    silently falling through leaves a flipped failover key undebuggable."""
    from app.llm import router as r

    monkeypatch.setattr(r, "_warned_fallback_model", set())
    caplog.set_level("WARNING", logger="app.llm.router")

    ps = SimpleNamespace(fallback_model="gpt-4o-mini", fallback_provider_id="deleted")
    model, owner = r.resolve_configured_fallback(
        _configs(), ps, current_model="claude-opus-4-8"
    )
    assert model == "gpt-4o-mini"
    assert owner is not None and owner.provider == "openai"
    assert "cannot serve" in caplog.text
    assert "deleted" in caplog.text


def test_env_pinned_model_owner_prefers_an_env_seeded_row():
    """An env-pinned model should route through env-seeded credentials; pairing
    it with a UI row's API key would be surprising. Falls back to any configured
    provider when no env-seeded row owns the model."""
    from datetime import UTC, datetime

    from app.main import _env_pinned_model_owner

    ui_row = SimpleNamespace(
        id="ui",
        provider="anthropic",
        available_models=["claude-sonnet-4-6"],
        created_at=datetime(2026, 6, 1, tzinfo=UTC),  # newest → recency winner
        is_from_env=False,
    )
    env_row = SimpleNamespace(
        id="env",
        provider="anthropic",
        available_models=["claude-sonnet-4-6"],
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        is_from_env=True,
    )

    assert _env_pinned_model_owner([ui_row, env_row], "claude-sonnet-4-6") is env_row
    assert _env_pinned_model_owner([ui_row], "claude-sonnet-4-6") is ui_row
    assert _env_pinned_model_owner([ui_row], "gpt-5-mini") is None


def test_env_seed_pins_the_reference_for_the_models_it_owns():
    """Clearing the companion reference is only half of owning it: without an
    assignment the env-pinned model keeps newest-config-first resolution, so a
    third config advertising it flips the credentials, on
    the env path. And ``fallback_provider_id`` is env-locked, so an admin who
    tries to pin it gets a 403. Source-level pin: the
    backend suite has no real-DB startup harness."""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    for field, owner in (
        ("ps.fallback_provider_id", "fb_owner"),
        ("ps.routing_provider_id", "routing_owner"),
    ):
        assert f"{field} = {owner}" in source or f"{field} = {owner}.id" in source, (
            f"{field} is never pinned to the resolved owner"
        )
    assert "_env_pinned_model_owner" in source


def test_env_seed_writes_the_fallback_reference_only_where_it_writes_the_model():
    """Clearing ``fallback_provider_id`` up front strips the admin's pin off a
    model the rejection branches keep: ``_env_managed_ga_fields`` locks both
    fields on the env var being non-empty, valid or not, so a stale
    ``DEFAULT_FALLBACK_MODEL`` would un-pin a kept model on every boot with the
    403 blocking any repoint. The pair has to move
    together, the way the General Assistant block already does it."""
    import ast
    import inspect
    import textwrap

    from app import main as app_main

    tree = ast.parse(
        textwrap.dedent(inspect.getsource(app_main._seed_llm_providers_from_env))
    )

    # Which statement list each assignment lives in, keyed by id() of the list.
    blocks: dict[str, set[int]] = {"fallback_model": set(), "fallback_provider_id": set()}
    for node in ast.walk(tree):
        for body in (
            getattr(node, "body", None),
            getattr(node, "orelse", None),
            getattr(node, "finalbody", None),
        ):
            if not isinstance(body, list):
                continue
            for stmt in body:
                if not isinstance(stmt, ast.Assign):
                    continue
                for target in stmt.targets:
                    if isinstance(target, ast.Attribute) and target.attr in blocks:
                        blocks[target.attr].add(id(body))

    assert blocks["fallback_model"], "the env seed never assigns ps.fallback_model"
    assert blocks["fallback_provider_id"] == blocks["fallback_model"], (
        "ps.fallback_provider_id is assigned in a branch that does not also "
        "assign ps.fallback_model — a rejected env model would drop the "
        "reference of the stored model it leaves in place"
    )
