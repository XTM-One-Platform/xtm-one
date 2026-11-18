"""Regression tests for the non-blocking embedding-config save.

Changing the platform embedding provider / model / dimensions used to run the
bulk ``DELETE FROM embeddings`` + per-document re-queue *inline* inside the
``PUT /settings/general-assistant`` request. A fix stopped that delete from
being cancelled by the 30s ``statement_timeout``, but it still blocked the HTTP
response — on a large KB corpus the delete over a pgvector/HNSW index runs for
minutes, so the AI Models "Save" button appeared to "load forever".

The fix hands the heavy KB reset to a FastAPI background task (with its own
session), so the Save returns immediately. The small embedding-config write
stays on the request path and is committed atomically with the audit log by the
``get_db`` dependency. The autonomous ``recover_stale_documents`` sweep is the
safety net.

These tests pin:

1. An embedding-config change schedules ``_run_embedding_reset_background`` as a
   background task and does NOT run the heavy reset inline.
2. A save that does NOT change embedding config schedules no reset.
3. ``_run_embedding_reset_background`` purges + re-queues with its own session.
"""

import uuid
from types import SimpleNamespace

import pytest
from fastapi import BackgroundTasks

import app.api.v1.settings as settings_module
from app.api.v1.schemas.settings import GeneralAssistantConfigRequest
from app.core.config import settings as app_settings


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MockSession:
    """Minimal async session: GA-config lookup + no-op flush/commit."""

    def __init__(self, results):
        self._results = list(results)
        self.commits = 0
        self.flushes = 0

    async def execute(self, statement):
        # The provider config lock returns nothing the endpoint reads.
        if getattr(statement, "_for_update_arg", None) is not None or "pg_advisory" in str(
            statement
        ):
            return _Result(None)
        return self._results.pop(0)

    async def refresh(self, _obj):
        pass

    async def flush(self):
        self.flushes += 1

    async def commit(self):
        self.commits += 1


def _clear_env_locks(monkeypatch):
    for attr in (
        "default_model",
        "default_reasoning_effort",
        "default_routing_model",
        "default_embedding_provider",
        "default_embedding_model",
        "default_image_provider",
        "default_image_model",
    ):
        monkeypatch.setattr(app_settings, attr, "")
    monkeypatch.setattr(app_settings, "default_embedding_dimensions", None)


def _common_patches(monkeypatch, *, ps):
    async def _fake_load_ps(_db):
        return ps

    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        _fake_load_ps,
    )

    inline_calls: list = []

    async def _fake_reset(_db, *, target_embedding_model):
        inline_calls.append(target_embedding_model)
        return 0, []

    monkeypatch.setattr(settings_module, "_reset_kb_embeddings_for_new_model", _fake_reset)

    async def _fake_log_audit(*_a, **_kw):
        return None

    monkeypatch.setattr(settings_module, "log_audit", _fake_log_audit)
    monkeypatch.setattr(settings_module, "_build_ga_response", lambda *_a, **_kw: {"ok": True})
    return inline_calls


def _make_ps():
    # An already-configured embedding target; ``embedding_provider_id`` is set so
    # the endpoint's provider/uuid alignment loop never issues an extra query.
    return SimpleNamespace(
        agent_default_provider=None,
        agent_default_provider_id=None,
        agent_default_model=None,
        embedding_provider="openai",
        embedding_provider_id="00000000-0000-0000-0000-000000000001",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
        image_provider=None,
        image_provider_id=None,
        image_model=None,
        routing_model=None,
        mcp_exposed_agent_ids=[],
        enterprise_license=None,
    )


@pytest.mark.asyncio
async def test_embedding_change_schedules_background_reset(monkeypatch):
    _clear_env_locks(monkeypatch)
    ps = _make_ps()
    inline_calls = _common_patches(monkeypatch, ps=ps)

    config_row = SimpleNamespace(llm_model="gpt-4.1")
    db = _MockSession([_Result(config_row)])
    bg = BackgroundTasks()

    # Switch to a different embedding model -> embedding_config_changed=True.
    body = GeneralAssistantConfigRequest(embedding_model="text-embedding-3-large")

    await settings_module.update_general_assistant_config(
        body=body,
        request=SimpleNamespace(),
        background_tasks=bg,
        user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        db=db,
    )

    # The heavy reset must NOT run inline on the request path...
    assert inline_calls == []
    # ...it must be scheduled as a background task instead.
    assert len(bg.tasks) == 1
    task = bg.tasks[0]
    assert task.func is settings_module._run_embedding_reset_background
    assert task.args == ("text-embedding-3-large",)


@pytest.mark.asyncio
async def test_no_embedding_change_schedules_nothing(monkeypatch):
    _clear_env_locks(monkeypatch)
    ps = _make_ps()
    inline_calls = _common_patches(monkeypatch, ps=ps)

    config_row = SimpleNamespace(llm_model="gpt-4.1")
    db = _MockSession([_Result(config_row)])
    bg = BackgroundTasks()

    # Only touch a non-embedding field; embedding signature is unchanged.
    body = GeneralAssistantConfigRequest(max_iterations=12)

    await settings_module.update_general_assistant_config(
        body=body,
        request=SimpleNamespace(),
        background_tasks=bg,
        user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        db=db,
    )

    assert inline_calls == []
    assert bg.tasks == []


@pytest.mark.asyncio
async def test_background_reset_purges_and_requeues(monkeypatch):
    """``_run_embedding_reset_background`` runs the reset in its own session and
    enqueues the re-queued documents after commit."""
    committed = {"count": 0}

    class _BgSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def commit(self):
            committed["count"] += 1

    def _session_factory():
        return _BgSession()

    monkeypatch.setattr("app.core.database.async_session", _session_factory)

    reset_args: list = []

    async def _fake_reset(_db, *, target_embedding_model):
        reset_args.append(target_embedding_model)
        return 9, ["doc-a", "doc-b"]

    monkeypatch.setattr(settings_module, "_reset_kb_embeddings_for_new_model", _fake_reset)

    enqueued: list = []
    queued_for: list = []

    async def _fake_enqueue(doc_ids):
        from app.llm.provider_access import acting_user_id

        enqueued.extend(doc_ids)
        queued_for.append(acting_user_id())

    monkeypatch.setattr("app.services.kb_helpers.enqueue_documents", _fake_enqueue)

    # The admin whose Save started the reset is bound to the request; the
    # re-embedding of every knowledge base must not be attributed to them, or
    # their LLM provider exclusions would apply to everyone's documents.
    from app.llm.provider_access import bind_acting_user

    bind_acting_user(uuid.uuid4())
    await settings_module._run_embedding_reset_background("text-embedding-3-large")

    assert reset_args == ["text-embedding-3-large"]
    assert committed["count"] == 1
    assert enqueued == ["doc-a", "doc-b"]
    assert queued_for == [None]


@pytest.mark.asyncio
async def test_background_reset_swallows_errors(monkeypatch):
    """A failure in the background reset must not raise — the autonomous
    stale-document sweep is the safety net."""

    def _boom_factory():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("app.core.database.async_session", _boom_factory)

    # Must not raise.
    await settings_module._run_embedding_reset_background("text-embedding-3-large")


async def _save(monkeypatch, body, **ps_fields):
    """Run the GA save; return (bg tasks, audit details, retry_provider_failures_now calls)."""
    _clear_env_locks(monkeypatch)
    ps = _make_ps()
    for key, value in ps_fields.items():
        setattr(ps, key, value)
    _common_patches(monkeypatch, ps=ps)
    audits: list = []
    retried: list = []

    async def _capture_audit(*_a, **kw):
        audits.append(kw.get("details"))

    async def _no_reference_check(*_a, **_kw):
        return None

    async def _record_retry(db):
        retried.append(db)
        return 0

    monkeypatch.setattr(settings_module, "log_audit", _capture_audit)
    monkeypatch.setattr(settings_module, "_refuse_missing_ga_provider_references", _no_reference_check)
    monkeypatch.setattr(settings_module, "retry_provider_failures_now", _record_retry)
    bg = BackgroundTasks()
    await settings_module.update_general_assistant_config(
        body=body,
        request=SimpleNamespace(),
        background_tasks=bg,
        user=SimpleNamespace(id="admin", email="admin@filigran.io"),
        db=_MockSession([_Result(SimpleNamespace(llm_model="gpt-4.1"))]),
    )
    return bg.tasks, audits[0], len(retried)


_OTHER_CONFIG = "00000000-0000-0000-0000-000000000002"


@pytest.mark.asyncio
async def test_same_model_on_another_provider_keeps_the_vectors(monkeypatch):
    # OpenAI direct -> AI Gateway, same model and dimensions: the stored vectors
    # are still valid; only the documents that failed on the provider retry.
    tasks, audit, retried = await _save(
        monkeypatch,
        GeneralAssistantConfigRequest(
            embedding_provider="filigran_gateway",
            embedding_provider_id=_OTHER_CONFIG,
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
    )

    assert tasks == []
    assert retried == 1
    assert audit["embedding_config_changed"] is True
    assert audit["kb_reset_scheduled"] is False


@pytest.mark.asyncio
async def test_another_config_of_the_same_provider_type_keeps_the_vectors(monkeypatch):
    tasks, audit, retried = await _save(
        monkeypatch, GeneralAssistantConfigRequest(embedding_provider_id=_OTHER_CONFIG)
    )

    assert tasks == []
    assert retried == 1
    assert audit["kb_reset_scheduled"] is False


@pytest.mark.asyncio
async def test_naming_the_config_of_a_legacy_row_retries_nothing(monkeypatch):
    tasks, audit, retried = await _save(
        monkeypatch,
        GeneralAssistantConfigRequest(embedding_provider_id=_OTHER_CONFIG),
        embedding_provider_id=None,
    )

    assert tasks == []
    assert retried == 0
    assert audit["embedding_config_changed"] is True
    assert audit["kb_reset_scheduled"] is False


@pytest.mark.asyncio
async def test_unchanged_embedding_settings_schedule_and_retry_nothing(monkeypatch):
    tasks, audit, retried = await _save(
        monkeypatch,
        GeneralAssistantConfigRequest(
            embedding_provider="openai",
            embedding_provider_id="00000000-0000-0000-0000-000000000001",
            embedding_model="text-embedding-3-small",
            embedding_dimensions=1536,
        ),
        embedding_provider_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
    )

    assert tasks == []
    assert retried == 0
    assert audit["embedding_config_changed"] is False
    assert audit["kb_reset_scheduled"] is False


@pytest.mark.asyncio
async def test_provider_switch_without_a_model_schedules_reset(monkeypatch):
    # No model set: each provider embeds with its own default model.
    tasks, audit, retried = await _save(
        monkeypatch,
        GeneralAssistantConfigRequest(embedding_provider="gemini", embedding_provider_id=_OTHER_CONFIG),
        embedding_model=None,
    )

    assert [t.func for t in tasks] == [settings_module._run_embedding_reset_background]
    assert retried == 0
    assert audit["kb_reset_scheduled"] is True


@pytest.mark.asyncio
async def test_dimensions_change_schedules_reset(monkeypatch):
    tasks, audit, retried = await _save(monkeypatch, GeneralAssistantConfigRequest(embedding_dimensions=512))

    assert [t.func for t in tasks] == [settings_module._run_embedding_reset_background]
    assert retried == 0
    assert audit["kb_reset_scheduled"] is True


@pytest.mark.asyncio
async def test_model_change_on_another_provider_schedules_reset(monkeypatch):
    tasks, audit, retried = await _save(
        monkeypatch,
        GeneralAssistantConfigRequest(
            embedding_provider="filigran_gateway",
            embedding_provider_id=_OTHER_CONFIG,
            embedding_model="text-embedding-3-large",
        ),
    )

    assert [t.args for t in tasks] == [("text-embedding-3-large",)]
    assert retried == 0
    assert audit["kb_reset_scheduled"] is True
