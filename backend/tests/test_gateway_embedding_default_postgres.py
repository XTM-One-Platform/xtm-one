"""The AI Gateway's flagged embedding model fills an EMPTY embedding default, never re-embedding (#1422).

Run against a real database: whether a stored vector would be re-embedded is
read from the ``embeddings`` and ``agent_memory_entries`` rows themselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper
from app.api.v1.platform_settings_helpers import get_platform_settings
from app.api.v1.settings import _ensure_platform_default_model
from app.core.config import settings
from app.core.security import encrypt_value
from app.llm import router
from app.llm.gateway_provider import FiligranGatewayProvider
from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.embedding import Embedding
from app.models.llm_config import LLMProviderConfig
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

GATEWAY = "filigran_gateway"
FLAGGED = "gemini-embedding-001"


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("gwemb", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


class _Flag:
    """Stands in for the gateway listing's ``default_for``; counts the reads."""

    def __init__(self, monkeypatch, model: str | None):
        self.model = model
        self.calls = 0

        async def _marked(provider: FiligranGatewayProvider, kind: str) -> str | None:
            self.calls += 1
            return self.model if kind == "embedding" else None

        monkeypatch.setattr(FiligranGatewayProvider, "marked_default_model", _marked)


class _Native:
    """Stands in for the gateway's embeddings: the model's native size; counts the probes."""

    def __init__(self, monkeypatch, size: int | None = 4):
        self.size = size
        self.calls = 0

        async def _embed(provider: FiligranGatewayProvider, text: str, model: str | None = None) -> list[float]:
            self.calls += 1
            if self.size is None:
                raise RuntimeError("gateway unreachable")
            return [0.1] * self.size

        monkeypatch.setattr(FiligranGatewayProvider, "embed", _embed)


@pytest.fixture(autouse=True)
def native(monkeypatch) -> _Native:
    return _Native(monkeypatch)


@pytest.fixture(autouse=True)
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(settings, "byollm_activated", False)
    router.invalidate_provider_cache()
    with contextlib.suppress(ImportError):
        from app.llm import gateway_embedding_default

        gateway_embedding_default._refused_until.clear()
    yield
    router.invalidate_provider_cache()


async def _add_config(sessions, provider: str) -> LLMProviderConfig:
    key = "https://gateway.example||||static-token" if provider == GATEWAY else "sk-test"
    async with sessions() as db:
        config = LLMProviderConfig(
            user_id=None,
            provider=provider,
            api_key_enc=encrypt_value(key),
            default_model="claude-sonnet-5" if provider == GATEWAY else "gpt-4o",
            available_models=["claude-sonnet-5"] if provider == GATEWAY else ["gpt-4o"],
        )
        db.add(config)
        await db.commit()
        return config


async def _added(sessions, config: LLMProviderConfig, **kwargs) -> None:
    """What adding *config* in Settings runs to fill the empty platform defaults."""
    async with sessions() as db:
        await _ensure_platform_default_model(
            db, config.provider, config.default_model, provider_id=str(config.id), **kwargs
        )
        await db.commit()


async def _chunk(
    sessions, provider: str | None, model: str | None, dims: int, *, stamp_dims: bool = True, provider_id=None
) -> None:
    async with sessions() as db:
        db.add(
            Embedding(
                source_type="document",
                embedding_provider=provider,
                embedding_provider_id=provider_id,
                embedding_model=model,
                embedding_dimensions=dims if stamp_dims else None,
                content="chunk",
                embedding=[0.1] * dims,
            )
        )
        await db.commit()


async def _memory(sessions, model: str | None, dims: int | None, *, vector: bool = True) -> None:
    async with sessions() as db:
        db.add(
            AgentMemoryEntry(
                content="fact",
                embedding=[0.1] * (dims or 3) if vector else None,
                embedding_model=model,
                embedding_dimensions=dims,
            )
        )
        await db.commit()


async def _stored(sessions) -> tuple:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        return ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model, ps.embedding_dimensions


async def _set_dimensions(sessions, dims: int) -> None:
    async with sessions() as db:
        ps = await get_platform_settings(db)
        ps.embedding_dimensions = dims
        await db.commit()


# --- Fill point: the gateway is added ---------------------------------------


@pytest.mark.asyncio
async def test_no_stored_vector_saves_the_flagged_model(monkeypatch, native):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)

        await _added(sessions, gateway)

        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, None)
        assert native.calls == 0  # nothing stored, nothing to compare


@pytest.mark.asyncio
async def test_vectors_of_the_flagged_model_at_one_size_save_it_without_dimensions(monkeypatch, native):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4)
        await _chunk(sessions, GATEWAY, FLAGGED, 4, stamp_dims=False)
        await _memory(sessions, FLAGGED, 4)
        await _memory(sessions, None, None, vector=False)  # no vector stored: nothing to re-embed

        await _added(sessions, gateway)

        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, None)
        assert native.calls == 1  # unset dimensions: the native size is probed


@pytest.mark.asyncio
async def test_stored_dimensions_equal_to_the_vectors_save_it(monkeypatch, native):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4)
        await _set_dimensions(sessions, 4)
        native.size = 6  # the stored setting decides, not the native size

        await _added(sessions, gateway)

        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, 4)
        assert native.calls == 0


@pytest.mark.parametrize(
    "case",
    [
        "other_model",
        "mixed_models",
        "mixed_dimensions",
        "other_provider",
        "unknown_provenance",
        "memory_other_model",
        "memory_other_dimensions",
        "configured_dimensions_differ",
        "native_size_differs",
        "native_size_unknown",
    ],
)
@pytest.mark.asyncio
async def test_vectors_a_switch_would_re_embed_leave_the_default_empty(monkeypatch, native, case):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4)
        if case == "other_model":
            async with sessions() as db:
                await db.execute(Embedding.__table__.delete())
                await db.commit()
            await _chunk(sessions, GATEWAY, "text-embedding-3-large", 4)
        elif case == "mixed_models":
            await _chunk(sessions, GATEWAY, "text-embedding-3-large", 4)
        elif case == "mixed_dimensions":
            await _chunk(sessions, GATEWAY, FLAGGED, 6)
        elif case == "other_provider":
            await _chunk(sessions, "gemini", FLAGGED, 4)
        elif case == "unknown_provenance":
            await _chunk(sessions, None, None, 4)
        elif case == "memory_other_model":
            await _memory(sessions, "text-embedding-3-large", 4)
        elif case == "memory_other_dimensions":
            await _memory(sessions, FLAGGED, 6)
        elif case == "configured_dimensions_differ":
            await _set_dimensions(sessions, 8)
        elif case == "native_size_differs":
            native.size = 6  # unset dimensions embed at 6: the stored 4s would be re-embedded
        elif case == "native_size_unknown":
            native.size = None  # the probe failed

        await _added(sessions, gateway)

        provider, provider_id, model, _dims = await _stored(sessions)
        assert (provider, provider_id, model) == (None, None, None)


@pytest.mark.asyncio
async def test_a_saved_model_is_never_changed_by_a_later_flag(monkeypatch):
    async with _database() as sessions:
        flag = _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _added(sessions, gateway)

        flag.model = "text-embedding-3-large"
        await _added(sessions, gateway)
        async with sessions() as db:
            _provider, model, _id = await router.get_embedding_provider(db)
            await db.commit()

        assert model == FLAGGED
        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, None)


@pytest.mark.asyncio
async def test_an_admin_or_env_choice_is_kept(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model = GATEWAY, gateway.id, "chosen"
            await db.commit()

        await _added(sessions, gateway)

        assert await _stored(sessions) == (GATEWAY, gateway.id, "chosen", None)


@pytest.mark.asyncio
async def test_fill_empty_defaults_false_saves_nothing(monkeypatch):
    """BYOLLM on an administrator-configured install: the empty default is their choice."""
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)

        await _added(sessions, gateway, fill_empty_defaults=False)

        assert await _stored(sessions) == (None, None, None, None)


# --- Retry at the embedding lookup ------------------------------------------


@pytest.mark.asyncio
async def test_gateway_unreachable_when_added_is_retried_at_the_next_lookup(monkeypatch):
    async with _database() as sessions:
        flag = _Flag(monkeypatch, None)  # listing failed: no flag read
        gateway = await _add_config(sessions, GATEWAY)
        await _added(sessions, gateway)
        assert await _stored(sessions) == (None, None, None, None)

        flag.model = FLAGGED
        async with sessions() as db:
            provider, model, config_id = await router.get_embedding_provider(db)
            await db.rollback()  # the caller's transaction is not what keeps it

        assert isinstance(provider, FiligranGatewayProvider)
        assert (model, config_id) == (FLAGGED, gateway.id)
        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, None)


@pytest.mark.asyncio
async def test_lookup_applies_the_same_vector_rules(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, "text-embedding-3-large", 4)

        async with sessions() as db:
            with pytest.raises(router.EmbeddingConfigurationError):
                await router.get_embedding_provider(db)

        assert await _stored(sessions) == (None, None, None, None)


@pytest.mark.asyncio
async def test_a_refused_flag_is_not_rechecked_on_every_lookup(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, "text-embedding-3-large", 4)
        statements: list[str] = []
        async with sessions() as db:
            with pytest.raises(router.EmbeddingConfigurationError):
                await router.get_embedding_provider(db)

        from sqlalchemy import event

        def _record(_conn, _cursor, statement, *_args):
            statements.append(statement)

        engine = sessions.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            async with sessions() as db:
                with pytest.raises(router.EmbeddingConfigurationError):
                    await router.get_embedding_provider(db)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

        assert not [s for s in statements if "embeddings" in s or "agent_memory_entries" in s]


@pytest.mark.asyncio
async def test_lookup_without_a_flag_saves_nothing(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, None)
        await _add_config(sessions, GATEWAY)

        async with sessions() as db:
            with pytest.raises(router.EmbeddingConfigurationError):
                await router.get_embedding_provider(db)

        assert await _stored(sessions) == (None, None, None, None)


@pytest.mark.asyncio
async def test_lookup_on_a_configured_byollm_install_saves_nothing(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        monkeypatch.setattr(settings, "byollm_activated", True)
        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.routing_model = "claude-haiku-5-5"  # an administrator's choice
            await db.commit()

        async with sessions() as db:
            with pytest.raises(router.EmbeddingConfigurationError):
                await router.get_embedding_provider(db)

        assert await _stored(sessions) == (None, None, None, None)


# --- Other providers resolve exactly as today -------------------------------


@pytest.mark.asyncio
async def test_openai_only_install_still_gets_its_built_in_model(monkeypatch):
    async with _database() as sessions:
        flag = _Flag(monkeypatch, FLAGGED)
        openai = await _add_config(sessions, "openai")
        await _chunk(sessions, "gemini", "text-embedding-004", 4)  # no vector check for other providers

        await _added(sessions, openai)

        assert await _stored(sessions) == ("openai", openai.id, "text-embedding-3-large", None)
        assert flag.calls == 0


@pytest.mark.asyncio
async def test_no_gateway_configured_adds_no_call_at_lookup(monkeypatch):
    async with _database() as sessions:
        flag = _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, "openai")  # the default left empty

        async with sessions() as db:
            _provider, model, _id = await router.get_embedding_provider(db)

        assert model == "text-embedding-3-large"
        assert flag.calls == 0
        assert await _stored(sessions) == (None, None, None, None)


@pytest.mark.parametrize("first", ["openai", GATEWAY])
@pytest.mark.asyncio
async def test_openai_and_gateway_keep_the_first_added(monkeypatch, first):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        second = "openai" if first == GATEWAY else GATEWAY
        first_config = await _add_config(sessions, first)
        await _added(sessions, first_config)
        await _added(sessions, await _add_config(sessions, second))

        expected = FLAGGED if first == GATEWAY else "text-embedding-3-large"
        assert await _stored(sessions) == (first, first_config.id, expected, None)


@pytest.mark.asyncio
async def test_lookup_with_openai_and_gateway_keeps_the_implicit_fallback(monkeypatch):
    async with _database() as sessions:
        flag = _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        openai = await _add_config(sessions, "openai")

        async with sessions() as db:
            _provider, model, config_id = await router.get_embedding_provider(db)

        assert (model, config_id) == ("text-embedding-3-large", openai.id)
        assert flag.calls == 0
        assert await _stored(sessions) == (None, None, None, None)


# --- Provider deletion preview ----------------------------------------------


@pytest.mark.asyncio
async def test_deletion_preview_names_the_gateway_default_embeddings_will_use(monkeypatch):
    from app.services.llm_provider_deletion import compute_deletion_impact

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        openai = await _add_config(sessions, "openai")
        await _added(sessions, openai)
        gateway = await _add_config(sessions, GATEWAY)

        async with sessions() as db:
            ps = await get_platform_settings(db)
            impact = await compute_deletion_impact(
                db, await db.get(LLMProviderConfig, openai.id), platform_settings=ps, general_assistant=None
            )

        assert impact.embedding_default.action == "cleared"
        fallback = impact.embedding_default.fallback
        assert fallback is not None
        assert (fallback.provider_id, fallback.model) == (gateway.id, FLAGGED)


@pytest.mark.asyncio
async def test_lookup_never_waits_on_the_callers_own_settings_row_lock(monkeypatch):
    """The save runs in its own session: a lookup inside a transaction holding the row must not hang."""
    import asyncio

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        async with sessions() as db:
            await get_platform_settings(db)  # the row exists, its embedding model empty
            await db.commit()

        async with sessions() as db:
            ps = await get_platform_settings(db)
            ps.routing_model = "claude-haiku-5-5"
            await db.flush()  # the caller now holds the row
            _provider, model, _id = await asyncio.wait_for(router.get_embedding_provider(db), timeout=20)
            await db.rollback()

        assert model == FLAGGED  # used for this call, saved by a later lookup
        assert await _stored(sessions) == (None, None, None, None)


@pytest.mark.asyncio
async def test_a_lookup_losing_the_save_race_uses_the_saved_model(monkeypatch):
    from app.llm import gateway_embedding_default as module

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        async with sessions() as db:
            await get_platform_settings(db)
            await db.commit()
        checked = module.adoptable_gateway_default

        async def _another_worker_saves_meanwhile(*args, **kwargs):
            found = await checked(*args, **kwargs)
            async with sessions() as other:
                ps = await get_platform_settings(other)
                ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model = GATEWAY, gateway.id, "saved"
                await other.commit()
            return found

        monkeypatch.setattr(module, "adoptable_gateway_default", _another_worker_saves_meanwhile)

        async with sessions() as db:
            _provider, model, config_id = await router.get_embedding_provider(db)

        assert (model, config_id) == ("saved", gateway.id)


@pytest.mark.asyncio
async def test_vectors_of_another_model_refuse_without_probing(monkeypatch, native):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, "openai", "text-embedding-3-large", 4)

        await _added(sessions, gateway)

        assert await _stored(sessions) == (None, None, None, None)
        assert native.calls == 0


# --- Review follow-ups ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_lookup_finding_the_row_locked_by_another_worker_still_embeds(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        async with sessions() as db:
            await get_platform_settings(db)
            await db.commit()

        async with sessions() as holder:  # another worker mid-save
            ps = await get_platform_settings(holder)
            await holder.refresh(ps, with_for_update=True)
            async with sessions() as db:
                _provider, model, config_id = await router.get_embedding_provider(db)
            await holder.rollback()

        assert (model, config_id) == (FLAGGED, gateway.id)


@pytest.mark.asyncio
async def test_a_fresh_install_lookup_embeds_before_the_row_is_committed(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)

        async with sessions() as db:  # the lookup creates the settings row, uncommitted
            _provider, model, config_id = await router.get_embedding_provider(db)

        assert (model, config_id) == (FLAGGED, gateway.id)


@pytest.mark.parametrize(("stamped", "saved"), [("this", True), ("other", False)])
@pytest.mark.asyncio
async def test_vectors_are_matched_to_the_gateway_config_not_its_name(monkeypatch, stamped, saved):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        other = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4, provider_id=gateway.id if stamped == "this" else other.id)

        await _added(sessions, gateway)

        assert (await _stored(sessions))[2] == (FLAGGED if saved else None)


@pytest.mark.asyncio
async def test_a_model_less_embedding_only_choice_is_kept(monkeypatch):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        async with sessions() as db:
            tei = LLMProviderConfig(
                user_id=None,
                provider="tei",
                api_key_enc=encrypt_value("none||http://tei.local"),
                default_model="",
                available_models=[],
            )
            db.add(tei)
            await db.flush()
            ps = await get_platform_settings(db)
            ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model = "tei", tei.id, None
            await db.commit()
        gateway = await _add_config(sessions, GATEWAY)

        await _added(sessions, gateway)

        assert await _stored(sessions) == ("tei", tei.id, None, None)


@pytest.mark.asyncio
async def test_deletion_preview_of_todays_implicit_fallback_names_the_gateway(monkeypatch):
    from app.services.llm_provider_deletion import compute_deletion_impact

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        openai = await _add_config(sessions, "openai")  # no default stored: implicit fallback
        gateway = await _add_config(sessions, GATEWAY)

        async with sessions() as db:
            ps = await get_platform_settings(db)
            impact = await compute_deletion_impact(
                db, await db.get(LLMProviderConfig, openai.id), platform_settings=ps, general_assistant=None
            )

        assert impact.embedding_default.fallback_moves
        fallback = impact.embedding_default.fallback
        assert fallback is not None
        assert (fallback.provider_id, fallback.model) == (gateway.id, FLAGGED)


@pytest.mark.asyncio
async def test_empty_knowledge_bases_take_the_saved_model(monkeypatch):
    from app.models.knowledge_base import KnowledgeBase

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        async with sessions() as db:
            empty = KnowledgeBase(name="empty", embedding_model="text-embedding-3-large", chunk_count=0)
            db.add(empty)
            await db.commit()

        await _added(sessions, gateway)

        async with sessions() as db:
            assert (await db.get(KnowledgeBase, empty.id)).embedding_model == FLAGGED


@pytest.mark.asyncio
async def test_a_failed_size_probe_is_retried_soon(monkeypatch, native):
    from app.llm import gateway_embedding_default as module

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4)
        clock = [1000.0]
        monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
        native.size = None
        await _added(sessions, gateway)
        assert await _stored(sessions) == (None, None, None, None)

        native.size = 4
        clock[0] += 60  # well under the 5-minute refusal of mismatched vectors
        await _added(sessions, gateway)

        assert await _stored(sessions) == (GATEWAY, gateway.id, FLAGGED, None)


@pytest.mark.asyncio
async def test_the_size_probe_leaves_the_shared_provider_alone(monkeypatch, native):
    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        gateway = await _add_config(sessions, GATEWAY)
        await _chunk(sessions, GATEWAY, FLAGGED, 4)
        async with sessions() as db:
            shared = await router._cached_provider(await db.get(LLMProviderConfig, gateway.id))
        shared.embedding_dimensions = 1024

        await _added(sessions, gateway)

        assert native.calls == 1
        assert shared.embedding_dimensions == 1024


async def _add_tei(sessions) -> LLMProviderConfig:
    async with sessions() as db:
        tei = LLMProviderConfig(
            user_id=None,
            provider="tei",
            api_key_enc=encrypt_value("none||http://tei.local"),
            default_model="",
            available_models=[],
        )
        db.add(tei)
        await db.commit()
        return tei


async def _choose_tei(db, tei: LLMProviderConfig) -> None:
    ps = await get_platform_settings(db)
    ps.embedding_provider, ps.embedding_provider_id, ps.embedding_model = "tei", tei.id, None
    await db.commit()


@pytest.mark.asyncio
async def test_a_model_less_choice_saved_meanwhile_is_served(monkeypatch):
    from app.llm import gateway_embedding_default as module

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        tei = await _add_tei(sessions)
        async with sessions() as db:
            await get_platform_settings(db)
            await db.commit()
        checked = module.adoptable_gateway_default

        async def _admin_chooses_tei_meanwhile(*args, **kwargs):
            found = await checked(*args, **kwargs)
            async with sessions() as other:
                await _choose_tei(other, tei)
            return found

        monkeypatch.setattr(module, "adoptable_gateway_default", _admin_chooses_tei_meanwhile)

        async with sessions() as db:
            _provider, model, config_id = await router.get_embedding_provider(db)

        assert (model, config_id) == ("", tei.id)
        assert await _stored(sessions) == ("tei", tei.id, None, None)


@pytest.mark.asyncio
async def test_an_admin_choice_committing_under_the_lock_wins(monkeypatch):
    """The lookup waits briefly for the row instead of embedding with the gateway model under a new default."""
    import asyncio

    async with _database() as sessions:
        _Flag(monkeypatch, FLAGGED)
        await _add_config(sessions, GATEWAY)
        tei = await _add_tei(sessions)
        async with sessions() as db:
            await get_platform_settings(db)
            await db.commit()

        async def _admin_saves(holder):
            await asyncio.sleep(0.5)
            await _choose_tei(holder, tei)

        async with sessions() as holder:
            ps = await get_platform_settings(holder)
            await holder.refresh(ps, with_for_update=True)  # the admin's save holds the row
            async with sessions() as db:
                (_provider, model, config_id), _ = await asyncio.gather(
                    router.get_embedding_provider(db), _admin_saves(holder)
                )

        assert (model, config_id) == ("", tei.id)
