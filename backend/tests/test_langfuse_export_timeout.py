"""Regression: the OTLP span export timeout must come from the Langfuse
constructor, not from OTEL env vars.

Production showed the span exporter timing out at ~5 s
(``read timeout=4.999997854232788``) on transient TLS handshake latency to the
Langfuse endpoint, silently dropping whole span batches.  That 5 s was the
``timeout`` kwarg our own init passed to ``Langfuse(...)`` — the SDK wires it
straight into ``OTLPSpanExporter(timeout=...)``, which short-circuits the
exporter's ``OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`` env lookup entirely.

These tests pin the fix:

- the ``Langfuse`` constructor gets the tolerant export timeout (>= 30 s);
- the init auth check stays short via per-request options so a dead host can
  never hold the init lock or a worker thread for the full export timeout;
- the admin test-connection endpoint uses the same constructor timeout,
  because the SDK's resource manager is a per-public-key singleton that
  ``shutdown()`` does not evict — whichever construction runs first pins the
  exporter timeout for the whole process.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# Aliased so pytest does not collect the imported route handler (its name
# starts with ``test_``) as a test item of this module.
from app.api.v1.settings import test_langfuse_connection as _test_connection_route
from app.services import langfuse_tracing as lt


class _FakeProjects:
    def __init__(self, captured: dict, data: list | None):
        self._captured = captured
        self._data = data

    def get(self, *, request_options=None):
        self._captured["request_options"] = request_options
        return SimpleNamespace(data=self._data)


def _make_fake_langfuse(captured: dict, project_data: list | None):
    class _FakeLangfuse:
        def __init__(self, **kwargs):
            captured["init_kwargs"] = kwargs
            self.api = SimpleNamespace(projects=_FakeProjects(captured, project_data))

        def shutdown(self):
            captured["shutdown"] = True

    return _FakeLangfuse


def test_export_timeout_is_tolerant_of_slow_tls_handshakes():
    """The exporter deadline must be >= 30 s and strictly
    larger than the short auth-check timeout it replaced."""
    assert lt.LANGFUSE_EXPORT_TIMEOUT >= 30
    assert lt.LANGFUSE_EXPORT_TIMEOUT > lt._LANGFUSE_AUTH_TIMEOUT


def test_blocking_init_passes_export_timeout_to_constructor(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(
        "langfuse.Langfuse", _make_fake_langfuse(captured, project_data=[object()])
    )

    client = lt._blocking_init_and_auth("pk", "sk", "https://langfuse.example")

    assert captured["init_kwargs"]["timeout"] == lt.LANGFUSE_EXPORT_TIMEOUT
    assert client is not None


def test_blocking_init_bounds_auth_check_with_short_per_request_timeout(monkeypatch):
    """The tolerant constructor timeout must NOT stretch the auth check: the
    ``asyncio.wait_for`` budget in ``get_langfuse_client`` is sized to
    ``_LANGFUSE_AUTH_TIMEOUT``, so the blocking ``projects.get()`` must be
    bounded by the same value via per-request options."""
    captured: dict = {}
    monkeypatch.setattr(
        "langfuse.Langfuse", _make_fake_langfuse(captured, project_data=[object()])
    )

    lt._blocking_init_and_auth("pk", "sk", "https://langfuse.example")

    assert captured["request_options"] == {
        "timeout_in_seconds": lt._LANGFUSE_AUTH_TIMEOUT
    }


def test_blocking_init_leaves_client_running_when_no_project(monkeypatch):
    """A failed check never shuts the client down: its resource manager is the
    SDK's process-wide one for the key, which the next attempt reuses
    (``test_langfuse_failed_init_threads``)."""
    captured: dict = {}
    monkeypatch.setattr(
        "langfuse.Langfuse", _make_fake_langfuse(captured, project_data=[])
    )

    with pytest.raises(lt._LangfuseAuthError):
        lt._blocking_init_and_auth("pk", "sk", "https://langfuse.example")

    assert "shutdown" not in captured


class _Result:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _MockSession:
    def __init__(self, value):
        self._value = value

    async def execute(self, _statement):
        return _Result(self._value)


@pytest.mark.asyncio
async def test_admin_test_connection_uses_same_constructor_timeout(monkeypatch):
    """The throwaway test-connection client must be constructed with the same
    export timeout as the real client: the SDK's resource manager is a
    per-public-key singleton that ``shutdown()`` does not evict, so whichever
    ``Langfuse(...)`` construction runs first pins the OTLP span exporter
    timeout for the whole process."""
    captured: dict = {}
    monkeypatch.setattr(
        "langfuse.Langfuse", _make_fake_langfuse(captured, project_data=[object()])
    )
    monkeypatch.setattr("app.core.security.decrypt_value", lambda value: value)
    monkeypatch.setattr("app.core.security.decrypt_stored_value", lambda value: value)

    async def _noop_reset() -> None:
        captured["reset"] = True

    monkeypatch.setattr(lt, "areset_langfuse_client", _noop_reset)

    config = SimpleNamespace(
        enabled=True,
        public_key_enc="pk",
        secret_key_enc="sk",
        host="https://langfuse.example",
    )
    user = SimpleNamespace(email="admin@example.com")

    result = await _test_connection_route(user=user, db=_MockSession(config))

    assert result["status"] == "success"
    assert captured["init_kwargs"]["timeout"] == lt.LANGFUSE_EXPORT_TIMEOUT
    # The auth probe itself must stay bounded per-request, not ride the
    # tolerant constructor timeout.
    assert captured["request_options"] == {"timeout_in_seconds": 10}
    assert captured.get("reset") is True
    # Never shut down: the resource manager is the process-wide one for the key.
    assert "shutdown" not in captured
