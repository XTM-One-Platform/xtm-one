import asyncio
import time
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1.schemas.settings import LLMProviderRequest, LLMProviderUpdateRequest
from app.api.v1.settings import (
    _ensure_platform_default_model,
    _list_models_by_capability,
    _serialize_platform_settings,
    create_llm_provider,
    delete_llm_provider,
    list_llm_providers,
    update_llm_provider,
)
from app.core.config import settings as app_settings
from app.services.llm_provider_management import ensure_llm_provider_management_allowed


class _MockSQLAlchemyResult:
    def __init__(self, configs):
        self._configs = configs

    def scalars(self):
        return self

    def all(self):
        return self._configs


class _MockAsyncSession:
    def __init__(self, execute_results):
        self._execute_results = list(execute_results)

    async def execute(self, _statement):
        return self._execute_results.pop(0)


class _GAResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _SeedingSession:
    """Async session stub for ``_ensure_platform_default_model``: returns a
    fixed GA-config row from ``execute`` and records ``add`` / ``flush``."""

    def __init__(self, ga_row):
        self._ga_row = ga_row
        self.added: list = []
        self.flushed = False

    async def execute(self, _statement):
        return _GAResult(self._ga_row)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flushed = True


def _seeding_ps():
    return SimpleNamespace(
        agent_default_provider=None,
        agent_default_provider_id=None,
        agent_default_model=None,
        embedding_provider=None,
        embedding_provider_id=None,
        embedding_model=None,
        image_provider=None,
        image_provider_id=None,
        image_model=None,
    )


@pytest.mark.asyncio
async def test_ensure_platform_default_seeds_first_chat_model_regardless_of_order(
    monkeypatch,
):
    """``_ensure_platform_default_model`` must pick the first CHAT-CAPABLE model
    from ``available_models`` for ``agent_default_model`` — not the literal first
    entry. A provider that lists an image model first but a chat model later
    (``["gpt-image-1", "gpt-4o"]``) must still seed ``gpt-4o`` as the agent
    default rather than leaving it unset.
    """
    ps = _seeding_ps()

    async def _fake_load_ps(_db):
        return ps

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    db = _SeedingSession(ga_row=None)

    await _ensure_platform_default_model(
        db,
        "openai",
        "gpt-image-1",  # literal first model is non-chat
        provider_id=None,
        available_models=["gpt-image-1", "gpt-4o"],
    )

    assert ps.agent_default_model == "gpt-4o"
    assert ps.agent_default_provider == "openai"


@pytest.mark.asyncio
async def test_ensure_platform_default_skips_chat_default_for_non_chat_only_provider(
    monkeypatch,
):
    """An image/embedding-only provider (no chat model anywhere in its catalog)
    must leave ``agent_default_model`` unset — seeding a non-chat model as the
    chat default is the root cause. The image/embedding section
    defaults are still seeded independently (keyed on the provider).
    """
    ps = _seeding_ps()

    async def _fake_load_ps(_db):
        return ps

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    db = _SeedingSession(ga_row=None)

    await _ensure_platform_default_model(
        db,
        "openai",
        "gpt-image-1",
        provider_id=None,
        available_models=["gpt-image-1", "text-embedding-3-large"],
    )

    assert ps.agent_default_model is None
    # Image / embedding section defaults are still populated (independent of
    # the chat model — they are keyed on the provider).
    assert ps.image_model == "gpt-image-1"
    assert ps.embedding_model == "text-embedding-3-large"


@pytest.mark.asyncio
async def test_ensure_platform_default_leaves_empty_defaults_when_told_to(
    monkeypatch,
):
    """``fill_empty_defaults=False`` is what the BYOLLM boot seed passes on an
    install an administrator configured: an empty agent, embedding or image
    default is their choice and stays empty."""
    ps = _seeding_ps()

    async def _fake_load_ps(_db):
        return ps

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    db = _SeedingSession(ga_row=None)

    await _ensure_platform_default_model(
        db,
        "openai",
        "gpt-4o",
        provider_id=None,
        available_models=["gpt-4o", "gpt-image-1"],
        fill_empty_defaults=False,
    )

    assert ps == _seeding_ps()
    assert db.flushed is True


@pytest.mark.parametrize(
    ("provider", "default_model", "available_models", "expected"),
    [
        (
            "openai",
            "gpt-image-1",
            ["gpt-image-1", "gpt-4o"],
            {
                "agent_default_model": "gpt-4o",
                "embedding_model": "text-embedding-3-large",
                "image_model": "gpt-image-1",
            },
        ),
        (
            "anthropic",
            "claude-sonnet-5",
            None,
            {
                "agent_default_model": "claude-sonnet-5",
                "embedding_model": None,
                "image_model": None,
            },
        ),
        (
            "tei",
            "bge-m3",
            ["bge-m3"],
            {"agent_default_model": None, "embedding_model": "bge-m3", "image_model": None},
        ),
    ],
)
def test_platform_filled_models_names_what_the_platform_writes(
    provider, default_model, available_models, expected
):
    """The values the BYOLLM boot seed does not count as an administrator's:
    the first chat model of the catalog, the provider's built-in embedding and
    image models, and an embedding-only endpoint's own model."""
    from app.api.v1.settings import platform_filled_models

    config = SimpleNamespace(
        provider=provider,
        default_model=default_model,
        available_models=available_models,
    )

    assert platform_filled_models(config) == expected


@pytest.mark.asyncio
async def test_list_llm_providers_coerces_null_is_from_env_to_false():
    config = SimpleNamespace(
        id=uuid.uuid4(),
        provider="openai",
        display_name=None,
        default_model="gpt-5-mini",
        available_models=["gpt-5-mini"],
        context_windows=None,
        max_output_tokens=None,
        is_from_env=None,
    )
    db = _MockAsyncSession([_MockSQLAlchemyResult([config])])

    result = await list_llm_providers(user=SimpleNamespace(is_admin=True), db=db)

    assert len(result) == 1
    assert result[0].is_from_env is False


class _FakeTEIProviderNoDiscovery:
    default_model = "tei-embedding"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def list_embedding_models(self):
        return []


class _FakeOpenAIProviderNoDiscovery:
    default_model = "text-embedding-3-large"

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def list_embedding_models(self):
        return []


@pytest.mark.asyncio
async def test_list_models_by_capability_keeps_tei_selectable_without_discovery(
    monkeypatch,
):
    tei_id = uuid.uuid4()
    tei_config = SimpleNamespace(
        id=tei_id,
        provider="tei",
        api_key_enc="enc",
        default_model=None,
    )
    db = _MockAsyncSession([_MockSQLAlchemyResult([tei_config])])

    monkeypatch.setattr("app.api.v1.settings.decrypt_value", lambda _v: "none||https://embed.example.com")
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _v: "none||https://embed.example.com")
    monkeypatch.setattr(
        "app.api.v1.settings.PROVIDER_MAP",
        {"tei": _FakeTEIProviderNoDiscovery},
    )

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    models = await _list_models_by_capability(db, "embedding", user)

    assert len(models) == 1
    assert models[0].provider == "tei"
    assert models[0].provider_id == str(tei_id)
    assert models[0].model == "tei-embedding"


@pytest.mark.asyncio
async def test_list_models_by_capability_does_not_add_fallback_for_non_tei_without_discovery(
    monkeypatch,
):
    openai_id = uuid.uuid4()
    openai_config = SimpleNamespace(
        id=openai_id,
        provider="openai",
        api_key_enc="enc",
        default_model=None,
    )
    db = _MockAsyncSession([_MockSQLAlchemyResult([openai_config])])

    monkeypatch.setattr("app.api.v1.settings.decrypt_value", lambda _v: "test")
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda _v: "test")
    monkeypatch.setattr(
        "app.api.v1.settings.PROVIDER_MAP",
        {"openai": _FakeOpenAIProviderNoDiscovery},
    )

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    models = await _list_models_by_capability(db, "embedding", user)

    assert models == []


class _SlowDecisionProvider:
    def __init__(self, name: str, delay: float):
        self.name = name
        self.delay = delay

    async def decision_listing(self):
        await asyncio.sleep(self.delay)
        return [f"clm-{self.name}"], True


def _decision_configs(monkeypatch, delays: dict[str, float]):
    configs = [SimpleNamespace(id=uuid.uuid4(), provider="ollama", api_key_enc=name, default_model=None) for name in delays]

    async def _provider_for_config(cfg):
        return _SlowDecisionProvider(cfg.api_key_enc, delays[cfg.api_key_enc])

    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    monkeypatch.setattr("app.api.v1.settings.PROVIDER_MAP", {"ollama": _SlowDecisionProvider})
    return configs


@pytest.mark.asyncio
async def test_list_models_by_capability_asks_the_providers_concurrently(monkeypatch):
    # Concurrency is proven structurally, never by wall-clock time (a loaded
    # xdist worker broke a 0.4 s threshold): every provider waits on a
    # barrier with one party per provider, so only concurrent calls release
    # it. A sequential implementation leaves the first call waiting until the
    # listing deadline, which drops its models and fails the assertion below.
    monkeypatch.setattr("app.api.v1.settings.DECISION_LISTING_TIMEOUT_SECONDS", 5)
    first, second = _decision_configs(monkeypatch, {"first": 0.0, "second": 0.0})
    barrier = asyncio.Barrier(2)

    class _BarrierDecisionProvider(_SlowDecisionProvider):
        async def decision_listing(self):
            await barrier.wait()
            return [f"clm-{self.name}"], True

    async def _provider_for_config(cfg):
        return _BarrierDecisionProvider(cfg.api_key_enc, 0.0)

    monkeypatch.setattr("app.api.v1.settings.provider_for_config", _provider_for_config)
    db = _MockAsyncSession([_MockSQLAlchemyResult([first, second])])

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    models = await _list_models_by_capability(db, "decision", user)

    assert barrier.n_waiting == 0, "a provider is still waiting: the listings were not asked concurrently"
    assert [(m.model, m.provider_id) for m in models] == [
        ("clm-first", str(first.id)),
        ("clm-second", str(second.id)),
    ]


@pytest.mark.asyncio
async def test_a_decision_listing_that_does_not_answer_ends_at_its_deadline(monkeypatch):
    monkeypatch.setattr("app.api.v1.settings.DECISION_LISTING_TIMEOUT_SECONDS", 0.2)
    quick, hung = _decision_configs(monkeypatch, {"quick": 0.0, "hung": 60.0})
    db = _MockAsyncSession([_MockSQLAlchemyResult([quick, hung])])

    user = SimpleNamespace(id=uuid.uuid4(), email="admin@example.com")
    started = time.monotonic()
    models = await _list_models_by_capability(db, "decision", user)

    assert time.monotonic() - started < 1
    assert [m.model for m in models] == ["clm-quick"]


@pytest.mark.asyncio
async def test_create_llm_provider_blocked_when_models_env_only(monkeypatch):
    # Use the 3-arg ``monkeypatch.setattr(obj, name, value)`` form rather
    # than the string-import form: ``settings`` is an instance of the
    # ``Settings`` class, not a module, so the 2-arg
    # ``"app.core.config.settings.models_env_providing_only"`` path
    # depends on pytest's recursive attribute-resolution fallback (it
    # tries ``import_module("app.core.config.settings")``, fails, then
    # walks back to ``import_module("app.core.config")`` and traverses
    # ``.settings.<attr>``). That fallback works today but is not part
    # of pytest's documented API — the 3-arg form is the unambiguous
    # contract for patching attributes on an object instance.
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    body = LLMProviderRequest(
        provider="openai",
        api_key="sk-test",
        default_model="gpt-4.1-mini",
        available_models=["gpt-4.1-mini"],
    )

    with pytest.raises(HTTPException) as exc:
        await create_llm_provider(
            body=body,
            request=SimpleNamespace(),
            user=SimpleNamespace(id=uuid.uuid4()),
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 403
    assert "MODELS_ENV_PROVIDING_ONLY=true" in str(exc.value.detail)


def test_gateway_provider_addable_without_any_feature_flag(monkeypatch):
    monkeypatch.setattr(app_settings, "enabled_features", "")

    from app.api.v1.settings import _ensure_new_provider_allowed

    _ensure_new_provider_allowed("filigran_gateway")


@pytest.mark.asyncio
async def test_update_llm_provider_blocked_when_models_env_only(monkeypatch):
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    with pytest.raises(HTTPException) as exc:
        await update_llm_provider(
            provider_id=str(uuid.uuid4()),
            body=SimpleNamespace(),
            request=SimpleNamespace(),
            user=SimpleNamespace(id=uuid.uuid4()),
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 403
    assert "MODELS_ENV_PROVIDING_ONLY=true" in str(exc.value.detail)


class _ScalarSession:
    """Async session stub for ``update_llm_provider``: hands back one row from
    ``execute().scalar_one_or_none()`` and records ``flush`` and ``commit``."""

    def __init__(self, row):
        self._row = row
        self.flushed = False
        self.committed = False

    async def execute(self, _statement):
        return _GAResult(self._row)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


def _env_provider_row(**overrides):
    row = SimpleNamespace(
        id=uuid.uuid4(),
        provider="anthropic",
        display_name=None,
        default_model="claude-sonnet-5",
        available_models=["claude-sonnet-5", "claude-haiku-4-5"],
        context_windows=None,
        max_output_tokens=None,
        is_from_env=True,
        env_models=["claude-sonnet-5", "claude-haiku-4-5"],
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def _neutralize_update_side_effects(monkeypatch):
    """Stub the post-mutation plumbing of ``update_llm_provider`` so the
    guard tests exercise only the env-lock decision."""
    import app.api.v1.settings as settings_api

    async def _noop(*_args, **_kwargs):
        return None

    async def _noop_counts(*_args, **_kwargs):
        return {"agents": 0, "users": 0, "ga_fields": 0}

    monkeypatch.setattr(settings_api, "_cleanup_orphaned_model_references", _noop_counts)
    monkeypatch.setattr(settings_api, "_ensure_platform_default_model", _noop)
    monkeypatch.setattr(settings_api, "log_audit", _noop)
    monkeypatch.setattr("app.llm.router.invalidate_provider_cache", lambda: None)


@pytest.mark.asyncio
async def test_update_env_provider_models_blocked_unless_editable(monkeypatch):
    """Without ``LLM_PROVIDER_MODELS_EDITABLE`` the boot seed re-pins the
    catalog on the next restart, so the API must refuse the edit up front and
    name the way out instead of accepting a change that silently
    reverts."""
    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", False)
    _neutralize_update_side_effects(monkeypatch)

    row = _env_provider_row()
    with pytest.raises(HTTPException) as exc:
        await update_llm_provider(
            provider_id=str(row.id),
            body=LLMProviderUpdateRequest(available_models=["claude-sonnet-5"]),
            request=SimpleNamespace(),
            user=SimpleNamespace(id=uuid.uuid4()),
            db=_ScalarSession(row),
        )

    assert exc.value.status_code == 403
    detail = str(exc.value.detail)
    assert "LLM_PROVIDER_MODELS_EDITABLE=true" in detail
    assert "LLM_PROVIDER_ANTHROPIC_MODELS" in detail
    # The row must be untouched.
    assert row.available_models == ["claude-sonnet-5", "claude-haiku-4-5"]


@pytest.mark.asyncio
async def test_update_env_provider_models_allowed_when_editable(monkeypatch):
    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", True)
    _neutralize_update_side_effects(monkeypatch)

    row = _env_provider_row()
    db = _ScalarSession(row)
    result = await update_llm_provider(
        provider_id=str(row.id),
        body=LLMProviderUpdateRequest(available_models=["claude-sonnet-5"]),
        request=SimpleNamespace(),
        user=SimpleNamespace(id=uuid.uuid4()),
        db=db,
    )

    assert db.flushed is True
    assert row.available_models == ["claude-sonnet-5"]
    assert result.is_from_env is True
    assert result.models_env_locked is False


@pytest.mark.asyncio
async def test_update_env_provider_rename_not_subject_to_the_catalog_lock(monkeypatch):
    """The seed never touches ``display_name`` / ``context_windows`` /
    ``max_output_tokens``, so those edits stick across restarts and must stay
    allowed on a locked env-seeded provider."""
    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", False)
    _neutralize_update_side_effects(monkeypatch)

    row = _env_provider_row()
    result = await update_llm_provider(
        provider_id=str(row.id),
        body=LLMProviderUpdateRequest(display_name="Claude (prod)"),
        request=SimpleNamespace(),
        user=SimpleNamespace(id=uuid.uuid4()),
        db=_ScalarSession(row),
    )

    assert row.display_name == "Claude (prod)"
    assert result.models_env_locked is True


@pytest.mark.asyncio
async def test_update_ui_provider_models_never_env_locked(monkeypatch):
    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", False)
    _neutralize_update_side_effects(monkeypatch)

    row = _env_provider_row(is_from_env=False, env_models=None)
    result = await update_llm_provider(
        provider_id=str(row.id),
        body=LLMProviderUpdateRequest(available_models=["claude-haiku-4-5"]),
        request=SimpleNamespace(),
        user=SimpleNamespace(id=uuid.uuid4()),
        db=_ScalarSession(row),
    )

    assert row.available_models == ["claude-haiku-4-5"]
    assert result.models_env_locked is False


@pytest.mark.asyncio
async def test_list_llm_providers_reports_models_env_locked(monkeypatch):
    """``models_env_locked`` (not ``is_from_env``) is what the Settings card
    gates the model editor on: env-seeded + flag off -> locked, env-seeded +
    flag on -> editable, UI-created -> never locked."""
    monkeypatch.setattr(app_settings, "llm_provider_models_editable", False)
    env_row = _env_provider_row()
    ui_row = _env_provider_row(is_from_env=False, env_models=None)

    result = await list_llm_providers(
        user=SimpleNamespace(is_admin=True),
        db=_MockAsyncSession([_MockSQLAlchemyResult([env_row, ui_row])]),
    )
    assert [r.models_env_locked for r in result] == [True, False]
    assert [r.is_from_env for r in result] == [True, False]

    monkeypatch.setattr(app_settings, "llm_provider_models_editable", True)
    result = await list_llm_providers(
        user=SimpleNamespace(is_admin=True),
        db=_MockAsyncSession([_MockSQLAlchemyResult([env_row, ui_row])]),
    )
    assert [r.models_env_locked for r in result] == [False, False]
    # The credential is still env-managed either way.
    assert result[0].is_from_env is True


@pytest.mark.asyncio
async def test_delete_llm_provider_blocked_when_models_env_only(monkeypatch):
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    with pytest.raises(HTTPException) as exc:
        await delete_llm_provider(
            provider_id=str(uuid.uuid4()),
            request=SimpleNamespace(),
            user=SimpleNamespace(id=uuid.uuid4()),
            db=SimpleNamespace(),
        )

    assert exc.value.status_code == 403
    assert "MODELS_ENV_PROVIDING_ONLY=true" in str(exc.value.detail)


def test_management_guard_detail_message_covers_all_mutation_paths(monkeypatch):
    """The shared env-only guard is reused for create / update / delete /
    rename. Pin its detail string so a future regression that drops one
    of the verbs (or reverts to "creation is disabled") trips here —
    misleading 403 messages on PUT / DELETE were the original Copilot
    complaint (the previous helper name was
    ``_ensure_llm_provider_creation_allowed`` and the message said
    "provider creation is disabled", which was wrong on DELETE)."""
    monkeypatch.setattr(app_settings, "models_env_providing_only", True)

    with pytest.raises(HTTPException) as exc:
        ensure_llm_provider_management_allowed()

    detail = str(exc.value.detail)
    assert exc.value.status_code == 403
    # All four mutation verbs must appear so the message stays accurate
    # for create / update / delete / rename paths.
    for verb in ("add", "edit", "rename", "delete"):
        assert verb in detail, (
            f"detail must mention {verb!r} so the 403 stays honest on "
            f"the corresponding endpoint; got {detail!r}"
        )
    assert "MODELS_ENV_PROVIDING_ONLY=true" in detail
    # The reverse: when env-only mode is off the guard must be a no-op
    # (otherwise normal admin flow breaks).
    monkeypatch.setattr(app_settings, "models_env_providing_only", False)
    ensure_llm_provider_management_allowed()  # must not raise


def test_serialize_platform_settings_carries_env_provider_flags():
    """``has_env_providers`` and ``models_env_providing_only`` must live
    inside ``_serialize_platform_settings`` itself (not be added ad-hoc
    at the GET endpoint), so the PUT /platform response carries the same
    shape. Without this, ``useUpdatePlatformSettings`` would briefly
    cache a payload where ``models_env_providing_only`` is undefined
    after any branding save and the UI would flicker into 'env-only
    OFF' state until the GET refetch landed."""
    ps = SimpleNamespace(
        id=uuid.uuid4(),
        platform_name="X",
        platform_logo=None,
        platform_logo_url=None,
        platform_description="",
        platform_login_message=None,
        platform_theme_color=None,
        platform_favicon=None,
        platform_accent_color=None,
        platform_background_color=None,
        platform_og_image=None,
        enterprise_license=None,
        ai_disabled=None,
    )
    app_settings = SimpleNamespace(
        enterprise_license="",
        models_env_providing_only=True,
        byollm_activated=False,
        provider_management_env_only=True,
    )

    payload = _serialize_platform_settings(ps, app_settings)

    assert payload["models_env_providing_only"] is True
    assert "has_env_providers" in payload, (
        "shape must include `has_env_providers` so PUT /platform "
        "response and GET /platform response stay consistent"
    )
    assert isinstance(payload["has_env_providers"], bool)


def test_serialize_platform_settings_env_only_off_by_default():
    """Inverse of the test above: when env-only mode is disabled, the
    serialiser must still emit the key (set to False) so the frontend
    can rely on a stable shape and avoid `?? false` fallbacks polluting
    every read site."""
    ps = SimpleNamespace(
        id=uuid.uuid4(),
        platform_name="X",
        platform_logo=None,
        platform_logo_url=None,
        platform_description="",
        platform_login_message=None,
        platform_theme_color=None,
        platform_favicon=None,
        platform_accent_color=None,
        platform_background_color=None,
        platform_og_image=None,
        enterprise_license=None,
        ai_disabled=None,
    )
    app_settings = SimpleNamespace(
        enterprise_license="",
        models_env_providing_only=False,
        byollm_activated=False,
        provider_management_env_only=False,
    )

    payload = _serialize_platform_settings(ps, app_settings)

    assert payload["models_env_providing_only"] is False


def test_default_model_first_pass_filters_to_env_seeded_rows():
    """Regression: the ``DEFAULT_MODEL`` env-seeded FIRST-PASS lookup in
    ``_seed_llm_providers_from_env`` MUST filter on ``user_id.is_(None)``
    so the resolved ``agent_default_provider_id`` points at the env-
    seeded row, not at a UI-created row with the same provider type.

    Companion to ``test_default_model_fallback_does_not_filter_by_user_id``:
    the FALLBACK block must NOT filter (so UI providers are visible),
    but the FIRST-PASS block (which only matches against env-seeded
    providers' ``MODELS`` lists) MUST filter — otherwise a deployment
    with both an env-seeded ``openai`` row AND a UI-created ``openai``
    row would resolve the platform default's ``agent_default_provider_id``
    to whichever row was most recently created (usually the UI one),
    silently routing the platform default away from the env-seeded
    provider whose ``MODELS`` list we just matched against.

    Source-inspection: scans the block between ``# First pass:`` and
    ``# Fallback:`` and asserts the row-lookup carries
    ``user_id.is_(None)``. A future refactor that drops the filter
    trips here.
    """
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    first_idx = source.find("# First pass:")
    assert first_idx > 0, "First-pass comment marker not found"
    fallback_idx = source.find("# Fallback:", first_idx)
    assert fallback_idx > 0, "Fallback marker not found after first pass"
    first_pass_block = source[first_idx:fallback_idx]

    code_only = "\n".join(
        line
        for line in first_pass_block.splitlines()
        if not line.lstrip().startswith("#")
    )

    assert "user_id.is_(None)" in code_only, (
        "DEFAULT_MODEL env-seeded first-pass MUST filter "
        "LLMProviderConfig on `user_id.is_(None)` — otherwise "
        "agent_default_provider_id can resolve to a UI-created row "
        "with the same provider type as the env-seeded provider, "
        "silently routing the platform default to the wrong provider_id "
        "and potentially the wrong API key."
    )


def test_embedding_image_provider_id_lookups_filter_to_env_seeded_rows():
    """Companion regression to the agent_default first-pass guard above:
    the embedding and image provider_id lookups inside
    ``_seed_llm_providers_from_env`` (the ones that set
    ``ps.embedding_provider_id`` and ``ps.image_provider_id``) follow
    the SAME env-seeded-precedence rule. If a deployment has both an
    env-seeded ``openai`` row and a UI-configured ``openai`` row, the
    env-seeded boot path must bind the embedding / image provider_id to
    the env-seeded row, not to whichever happened to be created most
    recently.

    Source-inspection: extracts the function source and asserts that
    the ``embedding_provider_id`` and ``image_provider_id`` lookups
    each include ``user_id.is_(None)`` in their WHERE clause. The
    fallback block stays unfiltered — only the env-seeded write paths
    are pinned here.
    """
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    # The embedding and image provider_id lookups are both anchored on
    # a ``ps.<purpose>_provider_id = cfg.id`` assignment immediately
    # following the WHERE clause. Walk forward from each anchor and
    # confirm the preceding WHERE clause carries the env-scope filter.
    for anchor in ("ps.embedding_provider_id = cfg.id", "ps.image_provider_id = cfg.id"):
        anchor_idx = source.find(anchor)
        assert anchor_idx > 0, f"Anchor not found: {anchor!r}"
        # The lookup query precedes the anchor; the relevant ``where``
        # block ends with a closing ``)`` followed by ``.order_by``.
        # Walk back from the anchor by a generous 1500 chars (the
        # blocks are ~25 lines each) and confirm the filter is present
        # in that slice.
        lookback = source[max(0, anchor_idx - 1500) : anchor_idx]
        # Strip comments so the explanatory block above the filter
        # doesn't satisfy the assertion.
        code_only = "\n".join(
            line for line in lookback.splitlines() if not line.lstrip().startswith("#")
        )
        assert "user_id.is_(None)" in code_only, (
            f"The provider_id lookup that feeds {anchor!r} MUST filter "
            f"LLMProviderConfig on `user_id.is_(None)` — env-seeded boot "
            f"must always bind the platform's *_provider_id to the env-"
            f"seeded row, not to a same-provider-type UI-created row."
        )


def test_default_model_fallback_does_not_filter_by_user_id():
    """Regression: the ``DEFAULT_MODEL`` fallback in
    ``_seed_llm_providers_from_env`` (``backend/app/main.py``) must query
    ``LLMProviderConfig`` WITHOUT filtering on ``user_id IS NULL``.

    UI-created providers set ``user_id`` to the admin who configured
    them as an audit trail (see ``create_llm_provider`` in
    ``api/v1/settings.py``), so a ``user_id IS NULL`` filter would
    silently skip every UI-created provider — and the fallback would
    fail to apply ``DEFAULT_MODEL`` on "DB-only / UI-configured
    providers" deployments, which is the exact use case the fallback
    was added to support.

    Source-inspection test rather than a behaviour test: the surrounding
    boot helper is 200+ lines of DB-coupled startup logic exercised
    end-to-end by ``test_bootstrap.py`` on every CI run. This test
    pins the SQL contract of the one block that previously broke so
    a regression that re-introduces ``user_id.is_(None)`` (or that
    reverts to iterating ``existing_by_name``) trips here instead of
    silently failing in production for env-only deployments."""
    import inspect

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    # Bound the fallback block: from the "# Fallback:" comment that
    # introduces the DEFAULT_MODEL fallback down to the next sibling
    # statement (``if routing_default_model:``).
    fallback_idx = source.find("# Fallback:")
    assert fallback_idx > 0, "Fallback comment marker not found"
    # Matched without the colon: the condition carries the BYOLLM seed-once
    # gate (``and _env_may_seed(...)``) and wraps across lines.
    routing_idx = source.find("if routing_default_model", fallback_idx)
    fallback_block = source[
        fallback_idx : routing_idx if routing_idx > 0 else len(source)
    ]

    # Strip Python comments before pattern-matching so the explanatory
    # comment block that documents *why* the fallback no longer uses
    # ``existing_by_name`` does not trip the assertion.
    code_only_lines = [
        line
        for line in fallback_block.splitlines()
        if not line.lstrip().startswith("#")
    ]
    code_only = "\n".join(code_only_lines)

    assert "user_id.is_(None)" not in code_only, (
        "DEFAULT_MODEL fallback in _seed_llm_providers_from_env must NOT "
        "filter LLMProviderConfig on `user_id.is_(None)` — UI-created "
        "providers carry `user_id != None` and would be silently skipped, "
        "breaking the 'DB-only / UI-configured providers' case the "
        "fallback was added to support."
    )
    assert "existing_by_name" not in code_only, (
        "DEFAULT_MODEL fallback must NOT iterate `existing_by_name` — "
        "that dict is built with `user_id.is_(None)` (env-seeded providers "
        "only) and silently drops UI-created providers. Query "
        "LLMProviderConfig directly so all provider rows are considered."
    )
    assert "order_by" in code_only, (
        "DEFAULT_MODEL fallback must ORDER BY when querying the table "
        "directly so the resolution is deterministic across boots when "
        "multiple providers carry the model in their available_models."
    )


def test_embedding_dimensions_seeding_is_unconditional():
    """Regression: ``_seed_llm_providers_from_env`` must apply
    ``DEFAULT_EMBEDDING_DIMENSIONS`` unconditionally (env always wins on every
    restart), mirroring the provider/model seeding just above it and the image
    block below.

    Embedding env vars now seed AND lock like every other ``DEFAULT_*`` setting
    (the Embedding control is read-only when set — see ``_env_managed_ga_fields``
    in ``api/v1/settings.py``), so the previous fresh-config guard
    (``and not ps.embedding_dimensions``) MUST be gone — otherwise env would not
    win on restart and the lock would be a phantom lock.

    Source-inspection test (the surrounding boot helper is DB-coupled and
    exercised end-to-end by ``test_bootstrap.py`` on every CI run): pins the
    unconditional seeding so a regression that re-introduces the guard trips
    here instead of silently breaking the env-wins contract.
    """
    import inspect
    import re

    from app import main as app_main

    source = inspect.getsource(app_main._seed_llm_providers_from_env)

    # Locate the assignment with a regex so the test is robust to whitespace /
    # line-wrapping changes from a formatter (an exact fixed-whitespace string
    # match would break on a no-op reformat).
    anchor_match = re.search(
        r"ps\.embedding_dimensions\s*=\s*settings\.default_embedding_dimensions",
        source,
    )
    assert anchor_match, "embedding_dimensions seeding assignment not found"
    anchor_idx = anchor_match.start()

    # The guarding ``if`` precedes the assignment; walk back a generous slice
    # and confirm the fresh-config guard is ABSENT (ignoring comment lines so
    # the explanatory NOTE above the guard cannot trip the assertion).
    lookback = source[max(0, anchor_idx - 800) : anchor_idx]
    code_only = "\n".join(
        line for line in lookback.splitlines() if not line.lstrip().startswith("#")
    )
    assert "not ps.embedding_dimensions" not in code_only, (
        "DEFAULT_EMBEDDING_DIMENSIONS must be seeded unconditionally (env wins "
        "on every restart) now that the embedding defaults are env-managed and "
        "UI-locked — the fresh-config guard (`and not ps.embedding_dimensions`) "
        "must NOT be present."
    )


def _gateway_config(credential: str, provider: str = "filigran_gateway"):
    from app.core.security import encrypt_value

    return SimpleNamespace(
        id=uuid.uuid4(),
        provider=provider,
        display_name=None,
        default_model=None,
        available_models=[],
        context_windows=None,
        max_output_tokens=None,
        is_from_env=False,
        api_key_enc=encrypt_value(credential),
        source_integration_id=None,
    )


def _serialize(config, **kwargs):
    from app.api.v1.settings import _serialize_llm_provider_response

    return _serialize_llm_provider_response(config, **kwargs)


def test_gateway_url_is_served_without_the_token(monkeypatch):
    monkeypatch.setattr("app.api.v1.settings.is_restricted", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.models_env_locked", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.vault_status", lambda *_args: None)

    config = _gateway_config("https://gw.example.com/v1||anthropic||s3cret")
    response = _serialize(config)

    assert response.gateway_url == "https://gw.example.com"
    assert "s3cret" not in response.model_dump_json()


def test_gateway_url_is_admin_only_and_gateway_only(monkeypatch):
    monkeypatch.setattr("app.api.v1.settings.is_restricted", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.models_env_locked", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.vault_status", lambda *_args: None)

    gateway = _gateway_config("https://gw.example.com")
    assert _serialize(gateway, with_audience=False).gateway_url is None
    other = _gateway_config("https://gw.example.com", provider="custom_openai")
    assert _serialize(other).gateway_url is None


def test_gateway_url_is_none_for_a_vault_reference(monkeypatch):
    monkeypatch.setattr("app.api.v1.settings.is_restricted", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.models_env_locked", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.vault_status", lambda *_args: None)

    config = _gateway_config("vault:some/reference")
    assert _serialize(config).gateway_url is None


def test_a_provider_says_whether_it_lists_its_decision_models(monkeypatch):
    """Settings asks for a typed decision model id from OpenRouter, which cannot list, never from a gateway."""
    monkeypatch.setattr("app.api.v1.settings.is_restricted", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.models_env_locked", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.vault_status", lambda *_args: None)

    assert _serialize(_gateway_config("https://gw.example.com")).lists_decision_models
    openrouter = _gateway_config("https://gw.example.com", provider="openrouter")
    assert not _serialize(openrouter).lists_decision_models


@pytest.mark.parametrize(
    ("stored", "expected"),
    [
        ("https://user:pw@gw.example.com:8443/v1?key=k#f||||tok", "https://gw.example.com:8443/v1"),
        ("http://[::1]:8080/", "http://[::1]:8080"),
        ("https://gw.example.com/base/v1", "https://gw.example.com/base"),
        ("gw.example.com", None),
        ("https://gw.example.com:abc", None),
        ("https://gw.example.com:99999", None),
        ("http://[::1", None),
    ],
)
def test_gateway_url_drops_userinfo_query_and_token(monkeypatch, stored, expected):
    monkeypatch.setattr("app.api.v1.settings.is_restricted", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.models_env_locked", lambda _c: False)
    monkeypatch.setattr("app.api.v1.settings.vault_status", lambda *_args: None)

    assert _serialize(_gateway_config(stored)).gateway_url == expected


class _RejectingGateway:
    """A gateway that echoes the credential it rejected, as some do."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    async def list_models_grouped(self):
        raise RuntimeError(f"401 Invalid token: {self.api_key.split('||')[-1]}")


async def _resolve_gateway(monkeypatch, api_key: str):
    from app.api.v1.schemas.settings import ResolveModelsRequest
    from app.api.v1.settings import PROVIDER_MAP, resolve_models_with_key

    monkeypatch.setitem(PROVIDER_MAP, "filigran_gateway", _RejectingGateway)
    with pytest.raises(HTTPException) as exc:
        await resolve_models_with_key(
            body=ResolveModelsRequest(provider="filigran_gateway", api_key=api_key),
            user=SimpleNamespace(id=uuid.uuid4(), email="a@example.com"),
            _ai_gate=None,
        )
    assert exc.value.status_code == 400
    return str(exc.value.detail)


@pytest.mark.asyncio
async def test_resolve_error_hides_the_gateway_token_and_its_jwks_hint(monkeypatch):
    detail = await _resolve_gateway(monkeypatch, "https://gw.example.com||||s3cret")

    assert "s3cret" not in detail
    assert "***" in detail
    assert "/xtm/auth/jwks" not in detail


@pytest.mark.asyncio
async def test_resolve_error_without_a_token_keeps_the_jwks_hint(monkeypatch):
    detail = await _resolve_gateway(monkeypatch, "https://gw.example.com")

    assert "/xtm/auth/jwks" in detail


async def _list_models_of(monkeypatch, provider: str):
    from app.api.v1.settings import list_provider_models
    from app.llm.gateway_provider import FiligranGatewayProvider, GatewayModel

    config = SimpleNamespace(id=uuid.uuid4(), provider=provider)

    async def execute(*_a, **_k):
        return _Result(config)

    db = SimpleNamespace(execute=execute)

    async def reveal(*_a, **_k):
        return "https://gw.example.com"

    async def meta(_self):
        return [
            GatewayModel(id="claude-sonnet", owner="anthropic", types=("chat",)),
            GatewayModel(id="text-embedding-3", owner="openai", types=("embedding",)),
            GatewayModel(id="dall-e-3", owner="openai", types=("image",)),
        ]

    monkeypatch.setattr("app.api.v1.settings.reveal_field", reveal)
    monkeypatch.setattr(FiligranGatewayProvider, "_models_with_meta", meta)
    return await list_provider_models(
        provider_id=str(config.id),
        user=SimpleNamespace(id=uuid.uuid4(), email="a@example.com"),
        db=db,
        _ai_gate=None,
    )


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


@pytest.mark.asyncio
async def test_gateway_models_also_report_every_offered_id(monkeypatch):
    response = await _list_models_of(monkeypatch, "filigran_gateway")

    assert response.models == ["claude-sonnet"]
    assert response.offered_models == ["claude-sonnet", "dall-e-3", "text-embedding-3"]


@pytest.mark.asyncio
async def test_other_providers_report_no_offered_ids(monkeypatch):
    from app.api.v1.settings import PROVIDER_MAP

    class _Chat:
        def __init__(self, api_key):
            pass

        async def list_models(self):
            return ["gpt-x"]

    monkeypatch.setitem(PROVIDER_MAP, "openai", _Chat)
    response = await _list_models_of(monkeypatch, "openai")

    assert response.models == ["gpt-x"]
    assert response.offered_models is None
