"""Tests for Langfuse env-var seeding helpers in main.py."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import Settings
from app.main import _seed_langfuse_from_env, _seed_langfuse_otel_exporter_defaults
from app.services import langfuse_tracing


@pytest.mark.asyncio
async def test_seed_langfuse_skipped_when_no_env_vars():
    """When LANGFUSE_PUBLIC_KEY or LANGFUSE_SECRET_KEY are empty, seeding is a no-op."""
    mock_settings = Settings(
        langfuse_public_key="",
        langfuse_secret_key="",
        secret_key="test",
    )
    with patch("app.main.settings", mock_settings):
        await _seed_langfuse_from_env()
    # No DB call should have been made — function returns early


@pytest.mark.asyncio
async def test_seed_langfuse_skipped_when_partial_env_vars():
    """When only one of the keys is set, seeding is a no-op."""
    mock_settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="",
        secret_key="test",
    )
    with patch("app.main.settings", mock_settings):
        await _seed_langfuse_from_env()


@pytest.mark.asyncio
async def test_seed_langfuse_creates_config_when_none_exists():
    """When both keys are set and no DB row exists, a new config is created."""
    mock_settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        langfuse_host="https://my-langfuse.example.com",
        secret_key="test",
    )

    # Mock the scalar result
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    added_objects = []
    mock_db.add = lambda obj: added_objects.append(obj)

    with (
        patch("app.main.settings", mock_settings),
        patch("app.core.database.async_session", return_value=mock_session_cm),
        patch("app.services.langfuse_tracing.reset_langfuse_client"),
    ):
        await _seed_langfuse_from_env()

    assert len(added_objects) == 1
    config = added_objects[0]
    assert config.enabled is True
    assert config.host == "https://my-langfuse.example.com"
    assert config.public_key_enc  # encrypted, not empty
    assert config.secret_key_enc  # encrypted, not empty


@pytest.mark.asyncio
async def test_seed_langfuse_updates_existing_config():
    """When both keys are set and a DB row exists, it is overwritten."""
    mock_settings = Settings(
        langfuse_public_key="pk-lf-new",
        langfuse_secret_key="sk-lf-new",
        langfuse_host="",
        secret_key="test",
    )

    # Existing config
    existing = MagicMock()
    existing.enabled = False
    existing.host = "https://old.example.com"
    existing.public_key_enc = "old-enc"
    existing.secret_key_enc = "old-enc"

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = existing

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.main.settings", mock_settings),
        patch("app.core.database.async_session", return_value=mock_session_cm),
        patch("app.services.langfuse_tracing.reset_langfuse_client"),
    ):
        await _seed_langfuse_from_env()

    assert existing.enabled is True
    # Host should default to LANGFUSE_DEFAULT_HOST when env var is empty
    assert existing.host == "https://cloud.langfuse.com"
    assert existing.public_key_enc != "old-enc"
    assert existing.secret_key_enc != "old-enc"


@pytest.mark.asyncio
async def test_seed_langfuse_default_host():
    """When LANGFUSE_HOST is not set, it defaults to cloud.langfuse.com."""
    mock_settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        secret_key="test",
    )

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(return_value=mock_result)
    mock_db.flush = AsyncMock()
    mock_db.commit = AsyncMock()

    added_objects = []
    mock_db.add = lambda obj: added_objects.append(obj)

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.main.settings", mock_settings),
        patch("app.core.database.async_session", return_value=mock_session_cm),
        patch("app.services.langfuse_tracing.reset_langfuse_client"),
    ):
        await _seed_langfuse_from_env()

    assert len(added_objects) == 1
    assert added_objects[0].host == "https://cloud.langfuse.com"


@pytest.mark.asyncio
async def test_seed_langfuse_db_error_does_not_crash():
    """A DB error during seeding should be logged, not raised."""
    mock_settings = Settings(
        langfuse_public_key="pk-lf-test",
        langfuse_secret_key="sk-lf-test",
        secret_key="test",
    )

    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=Exception("DB gone"))

    mock_session_cm = AsyncMock()
    mock_session_cm.__aenter__ = AsyncMock(return_value=mock_db)
    mock_session_cm.__aexit__ = AsyncMock(return_value=False)

    with (
        patch("app.main.settings", mock_settings),
        patch("app.core.database.async_session", return_value=mock_session_cm),
    ):
        await _seed_langfuse_from_env()  # should not raise


def test_seed_langfuse_otel_exporter_defaults_when_missing(monkeypatch):
    """Startup seeding sets OTEL BSP defaults when env vars are unset."""
    monkeypatch.delenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", raising=False)
    monkeypatch.delenv("OTEL_BSP_MAX_QUEUE_SIZE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_COMPRESSION", raising=False)

    _seed_langfuse_otel_exporter_defaults()

    assert (
        os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"]
        == langfuse_tracing._DEFAULT_OTEL_BSP_MAX_EXPORT_BATCH_SIZE
    )
    assert (
        os.environ["OTEL_BSP_MAX_QUEUE_SIZE"]
        == langfuse_tracing._DEFAULT_OTEL_BSP_MAX_QUEUE_SIZE
    )
    assert (
        os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"]
        == langfuse_tracing._DEFAULT_OTEL_EXPORTER_OTLP_TRACES_COMPRESSION
    )


def test_seed_langfuse_otel_exporter_defaults_preserve_overrides(monkeypatch):
    """Startup seeding must not overwrite explicit operator-provided values."""
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "16")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "1024")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "none")

    _seed_langfuse_otel_exporter_defaults()

    assert os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"] == "16"
    assert os.environ["OTEL_BSP_MAX_QUEUE_SIZE"] == "1024"
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] == "none"
