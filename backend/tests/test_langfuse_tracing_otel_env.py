"""Tests for OpenTelemetry env defaults in langfuse_tracing."""

import os

from app.services import langfuse_tracing


def _clear_seeded_env(monkeypatch):
    """Remove every env var the seed helper may write or read.

    The helper setdefaults the BSP knobs and conditionally seeds the
    traces-scoped compression var; clearing all of them (plus the OTLP timeout
    vars the helper must NOT touch) keeps each test hermetic regardless of
    execution order or ambient CI environment.
    """
    monkeypatch.delenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", raising=False)
    monkeypatch.delenv("OTEL_BSP_MAX_QUEUE_SIZE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_COMPRESSION", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_TIMEOUT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT", raising=False)


def test_seed_otel_exporter_limits_sets_defaults_when_missing(monkeypatch):
    """Set conservative OTEL batch defaults when vars are unset."""
    _clear_seeded_env(monkeypatch)

    langfuse_tracing._seed_otel_exporter_limits_from_env()

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


def test_seed_otel_exporter_limits_keeps_explicit_overrides(monkeypatch):
    """Do not overwrite user-provided OTEL tuning values."""
    _clear_seeded_env(monkeypatch)
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "13")
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "77")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_TRACES_COMPRESSION", "none")

    langfuse_tracing._seed_otel_exporter_limits_from_env()

    assert os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"] == "13"
    assert os.environ["OTEL_BSP_MAX_QUEUE_SIZE"] == "77"
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] == "none"


def test_seed_otel_exporter_limits_default_compression_is_gzip(monkeypatch):
    """Default traces compression must be gzip to shrink OTLP export payloads."""
    _clear_seeded_env(monkeypatch)

    langfuse_tracing._seed_otel_exporter_limits_from_env()

    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_COMPRESSION"] == "gzip"
    # The generic knob must stay untouched so the anonymous-telemetry
    # metric exporter keeps its own (uncompressed) default.
    assert "OTEL_EXPORTER_OTLP_COMPRESSION" not in os.environ


def test_seed_otel_exporter_limits_respects_generic_compression_var(monkeypatch):
    """An operator-set generic OTEL_EXPORTER_OTLP_COMPRESSION must win.

    The traces-scoped variable takes precedence over the generic one in the
    OTLP exporter's env lookup, so seeding it while the operator set only the
    generic knob would silently override their explicit choice.
    """
    _clear_seeded_env(monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_COMPRESSION", "none")

    langfuse_tracing._seed_otel_exporter_limits_from_env()

    assert "OTEL_EXPORTER_OTLP_TRACES_COMPRESSION" not in os.environ
    assert os.environ["OTEL_EXPORTER_OTLP_COMPRESSION"] == "none"


def test_seed_otel_exporter_limits_default_batch_size_is_conservative(monkeypatch):
    """Default batch size must be conservative enough to avoid 413 errors."""
    _clear_seeded_env(monkeypatch)

    langfuse_tracing._seed_otel_exporter_limits_from_env()

    # Batch size must be <= 32 to stay well under nginx 1 MB body-size limits
    # even for large LLM spans with prompt/completion payloads.
    assert int(os.environ["OTEL_BSP_MAX_EXPORT_BATCH_SIZE"]) <= 32


def test_seed_helper_does_not_seed_otlp_timeout_env(monkeypatch):
    """The OTLP export timeout must NOT be seeded via env vars.

    The Langfuse SDK constructs ``OTLPSpanExporter(timeout=...)`` with an
    explicit kwarg, so ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`` /
    ``OTEL_EXPORTER_OTLP_TIMEOUT`` are never consulted for the span exporter —
    seeding them would be dead config suggesting the timeout is handled when it
    is not.  (They would also be misread: opentelemetry-python interprets these
    env vars in SECONDS, not the spec's milliseconds.)  The export deadline is
    controlled by the ``timeout`` kwarg on the ``Langfuse`` constructor instead
    (see ``test_langfuse_export_timeout.py``).
    """
    _clear_seeded_env(monkeypatch)

    langfuse_tracing._seed_otel_exporter_limits_from_env()

    assert "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT" not in os.environ
    assert "OTEL_EXPORTER_OTLP_TIMEOUT" not in os.environ
