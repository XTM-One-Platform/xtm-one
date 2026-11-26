"""Tests for the opensandbox sandbox mode (CUSTOM_TOOL_SANDBOX=opensandbox).

Covers:
- _resolve_sandbox_mode() returning "opensandbox"
- _probe_opensandbox_available(): TCP probe success/failure, caching,
  retry-after-interval, domain with/without explicit port, invalid port
- _select_sandbox(): opensandbox reachable → "opensandbox";
  opensandbox unreachable → RuntimeError with clear message
- get_effective_sandbox(): "opensandbox" and "opensandbox_unavailable" branches
- warn_sandbox_mode_at_startup(): INFO on ready, ERROR on unreachable
- _build_opensandbox_config(): fields sourced from settings
- opensandbox_network_allow_list: comma-separated parsing → NetworkRule list
- execute_tool_auto() dispatcher: routes JS/Python to the opensandbox executors
- execute_js_tool_opensandbox(): happy path, SandboxException on create,
  non-zero exit code, disabled custom tools guard
- execute_python_tool_opensandbox(): same as JS variant
- code_interpreter.execute_python(): probes reachability before delegating;
  raises RuntimeError when opensandbox is unreachable
- Sandbox call quota (spec 001-sandbox-call-quotas): execute_python_opensandbox
  refuses (error-dict shape) when the quota is exhausted, without creating a
  sandbox; proceeds normally when under quota; non-OpenSandbox paths are
  unaffected by an exhausted OpenSandbox quota
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import _OPENSANDBOX_DEFAULT_IMAGES as DEFAULT_IMAGES
from app.core.config import settings
from app.services import _sandbox_selector as ss
from app.services import custom_tool_executor as cte

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_opensandbox_state(monkeypatch):
    """Reset all process-wide probe caches and config between tests."""
    monkeypatch.setattr(cte, "_docker_available", None)
    monkeypatch.setattr(cte, "_docker_probe_failed_at", 0.0)
    monkeypatch.setattr(ss, "_opensandbox_available", None)
    monkeypatch.setattr(ss, "_opensandbox_probe_failed_at", 0.0)
    cte._warn_legacy_env_once.cache_clear()
    monkeypatch.delenv("CUSTOM_TOOL_DOCKER", raising=False)
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    monkeypatch.setattr(settings, "opensandbox_domain", "localhost:8090")
    monkeypatch.setattr(settings, "opensandbox_protocol", "http")
    monkeypatch.setattr(settings, "opensandbox_api_key", "test-key")
    monkeypatch.setattr(settings, "opensandbox_network_allow", "")
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    settings.model_fields_set.discard("custom_tool_sandbox")


@pytest.fixture(autouse=True)
def _quota_store_answers_under_quota(monkeypatch):
    """Every OpenSandbox dispatch runs the combined quota check first.

    The unit suite has no quota store, and in XTM One mode - the suite's
    default - quotas always fail closed: unanswered, the check would
    refuse every dispatch these tests exercise. The tests of the check patch
    ``check_agentic_quota`` themselves.
    """
    from app.services import quota as quota_module

    monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock(return_value=None))


def _set_opensandbox_probe(monkeypatch, available: bool) -> None:
    """Patch the OpenSandbox TCP probe to return a fixed result.

    Patches both the ``ss`` module (used by code_interpreter's lazy import)
    and the ``cte`` module-level binding (used by _select_sandbox /
    get_effective_sandbox / warn_sandbox_mode_at_startup).
    """

    async def fake_probe():
        return available

    monkeypatch.setattr(ss, "_probe_opensandbox_available", fake_probe)
    monkeypatch.setattr(cte, "_probe_opensandbox_available", fake_probe)


def _record_opensandbox_executions(monkeypatch) -> list[str]:
    """Replace the opensandbox JS/Python executors with recording fakes."""
    calls: list[str] = []

    async def fake_js(js_function, inputs, timeout=300, *, user_id=None, quota_user_id=None, db=None):
        calls.append("opensandbox-js")
        return "opensandbox-js-ok"

    async def fake_python(py_function, inputs, timeout=300, *, user_id=None, quota_user_id=None, db=None):
        calls.append("opensandbox-python")
        return "opensandbox-python-ok"

    monkeypatch.setattr(cte, "execute_js_tool_opensandbox", fake_js)
    monkeypatch.setattr(cte, "execute_python_tool_opensandbox", fake_python)
    return calls


# ---------------------------------------------------------------------------
# _resolve_sandbox_mode
# ---------------------------------------------------------------------------


def test_resolve_returns_opensandbox_when_configured(monkeypatch):
    """`custom_tool_sandbox = 'opensandbox'` resolves to the opensandbox mode."""
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    assert cte._resolve_sandbox_mode() == "opensandbox"


def test_resolve_opensandbox_wins_over_legacy_docker_env(monkeypatch):
    """Explicit opensandbox setting overrides the legacy CUSTOM_TOOL_DOCKER=true."""
    monkeypatch.setenv("CUSTOM_TOOL_DOCKER", "true")
    monkeypatch.setattr(settings, "custom_tool_sandbox", "opensandbox")
    assert cte._resolve_sandbox_mode() == "opensandbox"


# ---------------------------------------------------------------------------
# _probe_opensandbox_available — TCP connect behaviour
# ---------------------------------------------------------------------------


async def test_probe_succeeds_when_server_reachable(monkeypatch):
    """Successful TCP connect → probe returns True."""
    mock_writer = MagicMock()  # close() is synchronous in production code
    mock_writer.wait_closed = AsyncMock()

    async def fake_open_connection(host, port):
        return (AsyncMock(), mock_writer)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await ss._probe_opensandbox_available()

    assert result is True
    assert ss._opensandbox_available is True
    mock_writer.close.assert_called_once()


async def test_probe_fails_when_connection_refused(monkeypatch):
    """Connection refused → probe returns False without raising."""

    async def fake_open_connection(host, port):
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    result = await ss._probe_opensandbox_available()

    assert result is False
    assert ss._opensandbox_available is False


async def test_probe_positive_result_cached_for_process_lifetime(monkeypatch):
    """A positive probe result is cached; no second TCP connect is made."""
    connect_count = 0

    async def fake_open_connection(host, port):
        nonlocal connect_count
        connect_count += 1
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        return (AsyncMock(), writer)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    assert await ss._probe_opensandbox_available() is True
    assert await ss._probe_opensandbox_available() is True
    assert connect_count == 1, "Second call must use the cached positive result"


async def test_probe_negative_result_reprobed_after_retry_interval(monkeypatch):
    """A failed probe is retried after OPENSANDBOX_PROBE_RETRY_INTERVAL expires."""
    connect_count = 0
    calls_should_succeed = [False, True]

    async def fake_open_connection(host, port):
        nonlocal connect_count
        connect_count += 1
        if calls_should_succeed[connect_count - 1]:
            writer = MagicMock()
            writer.wait_closed = AsyncMock()
            return (AsyncMock(), writer)
        raise ConnectionRefusedError("refused")

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)

    assert await ss._probe_opensandbox_available() is False
    # Still within the retry window — cached negative returned.
    assert await ss._probe_opensandbox_available() is False
    assert connect_count == 1

    # Simulate the retry interval elapsing.
    monkeypatch.setattr(
        ss,
        "_opensandbox_probe_failed_at",
        ss._opensandbox_probe_failed_at - ss._OPENSANDBOX_PROBE_RETRY_INTERVAL - 1,
    )

    assert await ss._probe_opensandbox_available() is True
    assert connect_count == 2


async def test_probe_uses_port_from_domain_setting(monkeypatch):
    """The port in `opensandbox_domain` (host:port) is used for the TCP connect."""
    connected_to: list[tuple[str, int]] = []

    async def fake_open_connection(host, port):
        connected_to.append((host, port))
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        return (AsyncMock(), writer)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(settings, "opensandbox_domain", "myserver:9999")

    await ss._probe_opensandbox_available()

    assert connected_to == [("myserver", 9999)]


async def test_probe_defaults_port_80_when_no_port_in_domain(monkeypatch):
    """Domain without a port → port 80 for http, 443 for https."""
    connected_to: list[tuple[str, int]] = []

    async def fake_open_connection(host, port):
        connected_to.append((host, port))
        writer = MagicMock()
        writer.wait_closed = AsyncMock()
        return (AsyncMock(), writer)

    monkeypatch.setattr(asyncio, "open_connection", fake_open_connection)
    monkeypatch.setattr(settings, "opensandbox_domain", "myserver")
    monkeypatch.setattr(settings, "opensandbox_protocol", "http")

    await ss._probe_opensandbox_available()

    assert connected_to == [("myserver", 80)]


async def test_probe_returns_false_for_invalid_port(monkeypatch):
    """A non-numeric port in the domain → probe returns False, doesn't raise."""
    monkeypatch.setattr(settings, "opensandbox_domain", "myserver:notaport")

    result = await ss._probe_opensandbox_available()

    assert result is False


async def test_probe_returns_false_when_domain_is_empty(monkeypatch):
    """Empty opensandbox_domain → probe returns False immediately."""
    monkeypatch.setattr(settings, "opensandbox_domain", "")

    result = await ss._probe_opensandbox_available()

    assert result is False


# ---------------------------------------------------------------------------
# _select_sandbox — opensandbox mode
# ---------------------------------------------------------------------------


async def test_select_returns_opensandbox_when_reachable(monkeypatch):
    """`opensandbox` mode + reachable server → _select_sandbox returns 'opensandbox'."""
    _set_opensandbox_probe(monkeypatch, True)

    result = await cte._select_sandbox()

    assert result == "opensandbox"


async def test_select_raises_clearly_when_opensandbox_unreachable(monkeypatch):
    """`opensandbox` mode + unreachable server → RuntimeError with useful message."""
    _set_opensandbox_probe(monkeypatch, False)

    with pytest.raises(RuntimeError, match=r"OpenSandbox.*not reachable"):
        await cte._select_sandbox()


# ---------------------------------------------------------------------------
# get_effective_sandbox — opensandbox branches
# ---------------------------------------------------------------------------


async def test_effective_sandbox_opensandbox_when_reachable(monkeypatch):
    _set_opensandbox_probe(monkeypatch, True)
    assert await cte.get_effective_sandbox() == "opensandbox"


async def test_effective_sandbox_opensandbox_unavailable_when_unreachable(monkeypatch):
    _set_opensandbox_probe(monkeypatch, False)
    assert await cte.get_effective_sandbox() == "opensandbox_unavailable"


# ---------------------------------------------------------------------------
# warn_sandbox_mode_at_startup — opensandbox log messages
# ---------------------------------------------------------------------------


async def test_startup_logs_info_when_opensandbox_ready(monkeypatch, caplog):
    _set_opensandbox_probe(monkeypatch, True)

    with caplog.at_level(logging.INFO, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    assert any("OpenSandbox" in r.getMessage() for r in caplog.records)
    assert all(r.levelno < logging.ERROR for r in caplog.records)


async def test_startup_logs_error_when_opensandbox_unreachable(monkeypatch, caplog):
    _set_opensandbox_probe(monkeypatch, False)

    with caplog.at_level(logging.ERROR, logger="app.services.custom_tool_executor"):
        await cte.warn_sandbox_mode_at_startup()

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("opensandbox" in r.getMessage().lower() for r in error_records)


# ---------------------------------------------------------------------------
# _build_opensandbox_config
# ---------------------------------------------------------------------------


def test_build_opensandbox_config_uses_settings(monkeypatch):
    """ConnectionConfig is built from the opensandbox_* settings."""
    monkeypatch.setattr(settings, "opensandbox_domain", "sandbox.example.com:8090")
    monkeypatch.setattr(settings, "opensandbox_protocol", "https")
    monkeypatch.setattr(settings, "opensandbox_api_key", "secret-key-123")

    config = ss._build_opensandbox_config()

    assert config.domain == "sandbox.example.com:8090"
    assert config.protocol == "https"
    assert config.api_key == "secret-key-123"


# ---------------------------------------------------------------------------
# opensandbox_network_allow_list — egress rule parsing
# ---------------------------------------------------------------------------


def test_network_allow_empty_produces_no_rules(monkeypatch):
    """Empty OPENSANDBOX_NETWORK_ALLOW → no egress NetworkRules are generated."""
    monkeypatch.setattr(settings, "opensandbox_network_allow", "")
    assert not settings.opensandbox_network_allow_list


def test_network_allow_single_target(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_network_allow", "api.example.com")
    assert "api.example.com" in settings.opensandbox_network_allow_list


def test_network_allow_multiple_targets(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_network_allow", "api.example.com,10.0.0.1,*.corp")
    allow_list = settings.opensandbox_network_allow_list
    assert "api.example.com" in allow_list
    assert "10.0.0.1" in allow_list
    assert "*.corp" in allow_list


# ---------------------------------------------------------------------------
# OPENSANDBOX_K8S_POOL_NAMES — per-key pool-name parsing and routing
# ---------------------------------------------------------------------------


def _parse_pool_names(raw: str) -> dict[str, str]:
    """Call the Settings field validator directly, bypassing full construction."""
    from app.core.config import Settings

    return Settings._parse_opensandbox_k8s_pool_names(raw)


def test_pool_names_empty_when_unset():
    assert _parse_pool_names("") == {}
    assert _parse_pool_names(None) == {}


def test_pool_names_parses_multiple_keys():
    parsed = _parse_pool_names("python:my-python-pool,nodejs:my-js-pool")
    assert parsed == {"python": "my-python-pool", "nodejs": "my-js-pool"}


def test_pool_names_accepts_arbitrary_key():
    """Any key is accepted as-is — the mapping isn't hardcoded to python/nodejs."""
    parsed = _parse_pool_names("rust:my-rust-pool")
    assert parsed == {"rust": "my-rust-pool"}


def test_pool_names_bare_value_falls_back_to_cold_start():
    """A bare value with no ':' (the pre-mapping single-pool form) isn't a valid
    entry — it's dropped so every key cold-starts, same as unset."""
    assert _parse_pool_names("my-pool") == {}


def test_pool_names_drops_malformed_entries_and_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        parsed = _parse_pool_names("python:my-python-pool,badentry,nodejs:my-js-pool")
    assert parsed == {"python": "my-python-pool", "nodejs": "my-js-pool"}
    assert "OPENSANDBOX_K8S_POOL_NAMES is not fully valid" in caplog.text
    assert "badentry" in caplog.text


def test_pool_names_valid_content_does_not_log(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        parsed = _parse_pool_names("python:my-python-pool")
    assert parsed == {"python": "my-python-pool"}
    assert "OPENSANDBOX_K8S_POOL_NAMES" not in caplog.text


# ---------------------------------------------------------------------------
# OPENSANDBOX_IMAGES — per-key image parsing, merged over defaults
# ---------------------------------------------------------------------------


def _parse_images(raw) -> dict[str, str]:
    """Call the Settings field validator directly, bypassing full construction."""
    from app.core.config import Settings

    return Settings._parse_opensandbox_images(raw)


def test_default_images_are_the_published_tags():
    assert DEFAULT_IMAGES == {"python": "sandbox-python:0.9", "nodejs": "sandbox-nodejs:0.9"}


def _published_images_checker():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "sandbox" / "check-published-images.py"
    spec = importlib.util.spec_from_file_location("check_published_images", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_published_image_pin_matches_the_inputs_and_the_app_default():
    checker = _published_images_checker()
    manifest = json.loads(checker.MANIFEST.read_text())
    assert checker.check(manifest, None) == []


def test_published_image_pin_names_a_stale_input_and_an_unbumped_version():
    checker = _published_images_checker()
    manifest = json.loads(checker.MANIFEST.read_text())
    stale = {**manifest, "inputs": {**manifest["inputs"], "sandbox-python": "0" * 64}}
    errors = checker.check(stale, None)
    assert len(errors) == 1 and errors[0].startswith("sandbox-python: its build inputs changed")
    base = {**manifest, "inputs": {**manifest["inputs"], "sandbox-nodejs": "0" * 64}}
    errors = checker.check(manifest, base)
    assert errors == [
        f"sandbox-nodejs: build inputs changed against the base without a new version "
        f"(still {manifest['version']}); nothing would be published."
    ]


def test_published_image_pin_holds_the_toolchain_images_apart():
    checker = _published_images_checker()
    manifest = json.loads(checker.MANIFEST.read_text())
    toolchain = manifest["toolchain"]
    stale = {**manifest, "toolchain": {**toolchain, "inputs": {"sandbox-toolchain": "0" * 64}}}
    errors = checker.check(stale, None)
    assert len(errors) == 1
    assert errors[0].startswith("sandbox-toolchain: its build inputs changed")
    assert f"toolchain.version {toolchain['version']}" in errors[0]
    base = {**manifest, "toolchain": {**toolchain, "inputs": {"sandbox-toolchain": "0" * 64}}}
    assert checker.check(manifest, base) == [
        f"sandbox-toolchain: build inputs changed against the base without a new version "
        f"(still {toolchain['version']}); nothing would be published."
    ]
    # A new toolchain version is the publish trigger, the custom-tools pin unchanged.
    assert checker.check({**manifest, "toolchain": {**toolchain, "version": "9.9"}}, base) == []
    # A base manifest from before the toolchain family is not an error.
    legacy = {k: v for k, v in manifest.items() if k != "toolchain"}
    assert checker.check(manifest, legacy) == []


def test_published_image_pin_holds_the_dev_images_and_their_stores():
    checker = _published_images_checker()
    manifest = json.loads(checker.MANIFEST.read_text())
    assert ".xtm-one/stores.py" in checker.DEV_INPUTS
    assert "backend/sandbox/Dockerfile.sandbox.dev" in checker.DEV_INPUTS
    for name in checker.DEV_INPUTS:
        assert (checker.REPO_ROOT / name).is_file(), name
    dev = manifest["dev"]
    stale = {**manifest, "dev": {**dev, "inputs": {"sandbox-dev": "0" * 64}}}
    errors = checker.check(stale, None)
    assert len(errors) == 1
    assert errors[0].startswith("sandbox-dev: its build inputs changed")
    assert f"dev.version {dev['version']}" in errors[0]
    base = {**manifest, "dev": {**dev, "inputs": {"sandbox-dev": "0" * 64}}}
    assert checker.check(manifest, base) == [
        f"sandbox-dev: build inputs changed against the base without a new version "
        f"(still {dev['version']}); nothing would be published."
    ]
    assert checker.check({**manifest, "dev": {**dev, "version": "9.9"}}, base) == []
    legacy = {k: v for k, v in manifest.items() if k != "dev"}
    assert checker.check(manifest, legacy) == []


def test_the_pin_workflow_publishes_a_missing_dev_version():
    from pathlib import Path

    workflow = (Path(__file__).resolve().parents[2] / ".github" / "workflows" / "sandbox-image-pin.yml").read_text()
    assert 'print("dev sandbox-python-dev", m["dev"]["version"])' in workflow


def test_images_default_when_unset():
    assert _parse_images(None) == DEFAULT_IMAGES
    assert _parse_images("") == DEFAULT_IMAGES


def test_images_partial_override_keeps_other_default():
    """Overriding just "python" must not wipe the "nodejs" default."""
    parsed = _parse_images("python:my-python-image:2.0")
    assert parsed == {"python": "my-python-image:2.0", "nodejs": DEFAULT_IMAGES["nodejs"]}


def test_images_value_with_extra_colons_survives_intact():
    """A registry:port-qualified image (multiple ':') is split on the first
    ':' only, so the rest of the value — including further colons — is kept."""
    parsed = _parse_images("python:registry.example.com:5000/python-sandbox:2.0")
    assert parsed["python"] == "registry.example.com:5000/python-sandbox:2.0"


def test_images_accepts_arbitrary_key():
    parsed = _parse_images("rust:my-rust-image:1.0")
    assert parsed["rust"] == "my-rust-image:1.0"
    assert parsed["python"] == DEFAULT_IMAGES["python"]
    assert parsed["nodejs"] == DEFAULT_IMAGES["nodejs"]


def test_images_drops_malformed_entries_and_logs_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.core.config"):
        parsed = _parse_images("python:my-python-image:2.0,badentry")
    assert parsed == {"python": "my-python-image:2.0", "nodejs": DEFAULT_IMAGES["nodejs"]}
    assert "OPENSANDBOX_IMAGES is not fully valid" in caplog.text
    assert "badentry" in caplog.text


async def test_code_interpreter_uses_configured_python_image(monkeypatch):
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(settings, "opensandbox_images", {"python": "my-custom-python:9.9", "nodejs": DEFAULT_IMAGES["nodejs"]})

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    assert mocksandbox.create.call_args.kwargs["image"] == "my-custom-python:9.9"


async def test_js_tool_uses_configured_nodejs_image(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_images", {"python": DEFAULT_IMAGES["python"], "nodejs": "my-custom-js:9.9"})
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hi")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    assert mocksandbox.create.call_args.kwargs["image"] == "my-custom-js:9.9"


async def test_js_tool_raises_without_calling_create_when_image_missing(monkeypatch):
    """A key missing from OPENSANDBOX_IMAGES is a config error, not a cold
    start — it must fail loudly before ever touching Sandbox.create."""
    monkeypatch.setattr(settings, "opensandbox_images", {"python": DEFAULT_IMAGES["python"]})

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=r"No OpenSandbox image configured for key 'nodejs'"):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    mocksandbox.create.assert_not_awaited()


async def test_js_tool_routes_to_nodejs_pool_ref(monkeypatch):
    """execute_js_tool_opensandbox binds extensions.poolRef to the nodejs pool only."""
    monkeypatch.setattr(
        settings, "opensandbox_k8s_pool_names", {"python": "my-python-pool", "nodejs": "my-js-pool"}
    )
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hi")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    assert mocksandbox.create.call_args.kwargs["extensions"] == {"poolRef": "my-js-pool"}


async def test_python_tool_routes_to_python_pool_ref(monkeypatch):
    """execute_python_tool_opensandbox binds extensions.poolRef to the python pool only."""
    monkeypatch.setattr(
        settings, "opensandbox_k8s_pool_names", {"python": "my-python-pool", "nodejs": "my-js-pool"}
    )
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hi")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("print('hi')", {})

    assert mocksandbox.create.call_args.kwargs["extensions"] == {"poolRef": "my-python-pool"}


async def test_custom_tool_extensions_none_when_no_pool_configured(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {})
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hi")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    assert mocksandbox.create.call_args.kwargs["extensions"] is None


# ---------------------------------------------------------------------------
# execute_tool_auto dispatcher — opensandbox routing
# ---------------------------------------------------------------------------


async def test_execute_tool_auto_routes_js_to_opensandbox(monkeypatch):
    """`execute_tool_auto` routes JavaScript to `execute_js_tool_opensandbox`."""
    _set_opensandbox_probe(monkeypatch, True)
    calls = _record_opensandbox_executions(monkeypatch)

    result = await cte.execute_tool_auto("console.log('hi');", {}, language="javascript")

    assert result == "opensandbox-js-ok"
    assert calls == ["opensandbox-js"]


async def test_execute_tool_auto_routes_python_to_opensandbox(monkeypatch):
    """`execute_tool_auto` routes Python to `execute_python_tool_opensandbox`."""
    _set_opensandbox_probe(monkeypatch, True)
    calls = _record_opensandbox_executions(monkeypatch)

    result = await cte.execute_tool_auto("print('hi')", {}, language="python")

    assert result == "opensandbox-python-ok"
    assert calls == ["opensandbox-python"]


async def test_execute_tool_auto_raises_when_opensandbox_unreachable(monkeypatch):
    """Unreachable server with opensandbox mode → clear RuntimeError, no fallback."""
    _set_opensandbox_probe(monkeypatch, False)
    calls = _record_opensandbox_executions(monkeypatch)

    with pytest.raises(RuntimeError, match=r"OpenSandbox.*not reachable"):
        await cte.execute_tool_auto("console.log('hi');", {})

    assert calls == [], "No executor must run when opensandbox is required but missing"


# ---------------------------------------------------------------------------
# execute_js_tool_opensandbox — unit tests with mocked Sandbox
# ---------------------------------------------------------------------------


def _fake_metrics(**overrides) -> SimpleNamespace:
    """A SandboxMetrics-shaped fake for sandbox.get_metrics() (spec 003-sandbox-call-logging)."""
    defaults = {
        "cpu_used_percentage": 12.5,
        "cpu_count": 1.0,
        "memory_used_in_mib": 64.0,
        "memory_total_in_mib": 128.0,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_sandbox_mock(
    exit_code: int = 0,
    stdout: str = "ok",
    stderr: str = "",
    execution_time_in_millis: int = 842,
    metrics: SimpleNamespace | None = None,
) -> MagicMock:
    """Build a minimal Sandbox async-context-manager mock."""

    def log_line(text: str) -> MagicMock:
        return MagicMock(text=text)

    result = MagicMock()
    result.exit_code = exit_code
    result.logs.stdout = [log_line(stdout)] if stdout else []
    result.logs.stderr = [log_line(stderr)] if stderr else []
    result.complete = SimpleNamespace(execution_time_in_millis=execution_time_in_millis)

    sandbox = AsyncMock()
    sandbox.__aenter__ = AsyncMock(return_value=sandbox)
    sandbox.__aexit__ = AsyncMock(return_value=False)
    sandbox.files.write_file = AsyncMock()
    sandbox.commands.run = AsyncMock(return_value=result)
    sandbox.get_metrics = AsyncMock(return_value=metrics or _fake_metrics())
    return sandbox


async def test_execute_js_opensandbox_happy_path(monkeypatch):
    """Successful JS execution returns the sandbox stdout, sized per settings."""
    monkeypatch.setattr(settings, "sandbox_cpu", "2")
    monkeypatch.setattr(settings, "sandbox_ram", "256Mi")
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    assert result == "hello from js"
    sandbox.files.write_file.assert_awaited_once()
    call_args = sandbox.files.write_file.call_args
    assert call_args[0][0] == "/tmp/_xtm_tool.js"  # noqa: S108
    sandbox.commands.run.assert_awaited_once_with("node /tmp/_xtm_tool.js")
    assert mocksandbox.create.call_args.kwargs["resource"] == {"cpu": "2", "memory": "256Mi"}


async def test_custom_tool_sandboxes_read_the_per_key_resource_list(monkeypatch):
    """SANDBOX_CPU / SANDBOX_RAM in the per-key form size each custom-tool sandbox by its own key."""
    monkeypatch.setattr(settings, "sandbox_cpu", "1,nodejs:500m,python-dev:2")
    monkeypatch.setattr(settings, "sandbox_ram", "128Mi,python:512Mi,python-dev:2Gi")
    sandbox = _make_sandbox_mock(exit_code=0, stdout="ok")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})
        js_resource = mocksandbox.create.call_args.kwargs["resource"]
        await cte.execute_python_tool_opensandbox("print('hi')", {})
        py_resource = mocksandbox.create.call_args.kwargs["resource"]

    assert js_resource == {"cpu": "500m", "memory": "128Mi"}
    assert py_resource == {"cpu": "1", "memory": "512Mi"}


async def test_execute_js_opensandbox_nonzero_exit_raises(monkeypatch):
    """Non-zero exit code from the sandbox command → RuntimeError."""
    sandbox = _make_sandbox_mock(exit_code=1, stderr="SyntaxError: bad token")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match=r"Tool execution failed"):
            await cte.execute_js_tool_opensandbox("bad js", {})


async def test_execute_js_opensandbox_sandbox_exception_on_create(monkeypatch):
    """SandboxException during Sandbox.create → RuntimeError with message."""
    from opensandbox.exceptions.sandbox import SandboxException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        with pytest.raises(RuntimeError, match=r"Failed to create sandbox"):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})


async def test_execute_js_opensandbox_disabled_tools_guard(monkeypatch):
    """DISABLE_CUSTOM_TOOLS=true raises before touching the sandbox."""
    monkeypatch.setattr(settings, "disable_custom_tools", True)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=r"disabled"):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    mocksandbox.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# execute_python_tool_opensandbox — unit tests with mocked Sandbox
# ---------------------------------------------------------------------------


async def test_execute_python_opensandbox_happy_path(monkeypatch):
    """Successful Python execution returns the sandbox stdout, sized per settings."""
    monkeypatch.setattr(settings, "sandbox_cpu", "2")
    monkeypatch.setattr(settings, "sandbox_ram", "256Mi")
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from python")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await cte.execute_python_tool_opensandbox("print('hi')", {})

    assert result == "hello from python"
    call_args = sandbox.files.write_file.call_args
    assert call_args[0][0] == "/tmp/_xtm_tool.py"  # noqa: S108
    sandbox.commands.run.assert_awaited_once_with("python /tmp/_xtm_tool.py")
    assert mocksandbox.create.call_args.kwargs["resource"] == {"cpu": "2", "memory": "256Mi"}


async def test_execute_python_opensandbox_nonzero_exit_raises(monkeypatch):
    """Non-zero exit code from the Python sandbox → RuntimeError."""
    sandbox = _make_sandbox_mock(exit_code=1, stderr="NameError: name 'x' is not defined")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match=r"Tool execution failed"):
            await cte.execute_python_tool_opensandbox("x + 1", {})


async def test_execute_python_opensandbox_sandbox_exception_on_create(monkeypatch):
    """SandboxException during Sandbox.create → RuntimeError."""
    from opensandbox.exceptions.sandbox import SandboxException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("quota exceeded"))
        with pytest.raises(RuntimeError, match=r"Failed to create sandbox"):
            await cte.execute_python_tool_opensandbox("print('hi')", {})


async def test_execute_python_opensandbox_disabled_tools_guard(monkeypatch):
    """DISABLE_CUSTOM_TOOLS=true raises before touching the sandbox."""
    monkeypatch.setattr(settings, "disable_custom_tools", True)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=r"disabled"):
            await cte.execute_python_tool_opensandbox("print('hi')", {})

    mocksandbox.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Custom-tool opensandbox executors — sandbox call quota (001-sandbox-call-quotas)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# code_interpreter.execute_python — opensandbox availability gate
# ---------------------------------------------------------------------------


async def test_code_interpreter_raises_when_opensandbox_unreachable(monkeypatch):
    """execute_python_opensandbox() returns an error dict when the server is down."""
    from app.services import code_interpreter as ci

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    _set_opensandbox_probe(monkeypatch, False)

    result = await ci.execute_python_opensandbox("print('hi')")

    assert result["error"]
    assert "not reachable" in result["error"]
    assert result["stdout"] == ""
    assert result["images"] == []


async def test_code_interpreter_delegates_when_opensandbox_reachable(monkeypatch):
    """execute_python() delegates to execute_python_opensandbox when server is up."""
    from app.services import code_interpreter as ci

    monkeypatch.setattr(settings, "disable_code_interpreter", False)

    called_with: list[str] = []

    async def fake_execute_opensandbox(code, *, timeout, user_id=None, quota_user_id=None, db=None):
        called_with.append(code)
        return {"stdout": "ok", "stderr": "", "error": None, "images": [], "output_files": []}

    monkeypatch.setattr(ci, "execute_python_opensandbox", fake_execute_opensandbox)

    result = await ci.execute_python("print('hi')")

    assert called_with == ["print('hi')"]
    assert result["stdout"] == "ok"


# ---------------------------------------------------------------------------
# Conversation files mounted under attachments/
# ---------------------------------------------------------------------------

_MOUNTS = [("data-0f3c9a1b.csv", b"a,b\n1,2\n"), ("index.json", b"[]")]
_REMOTE = "/tmp/"  # noqa: S108 - the sandbox container's working directory, not a host path
_ATTACH = f"{_REMOTE}attachments"


async def test_execute_python_forwards_the_mounts_to_opensandbox(monkeypatch):
    from app.services import code_interpreter as ci

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    captured: list = []

    async def fake_execute_opensandbox(code, *, timeout, user_id=None, quota_user_id=None, db=None, input_files=None):
        captured.append(input_files)
        return {"stdout": "ok", "stderr": "", "error": None, "images": [], "output_files": [], "mounted": []}

    monkeypatch.setattr(ci, "execute_python_opensandbox", fake_execute_opensandbox)
    await ci.execute_python("open('attachments/index.json')", input_files=_MOUNTS)
    assert captured == [_MOUNTS]


async def test_opensandbox_creates_the_mount_directory_before_writing_into_it(monkeypatch):
    """The files were written under ``/tmp/attachments``
    without creating it, and the run relied on a shell ``cd``. The directory
    is created through the SDK first, and the working directory is the SDK's
    run option - the command itself is unchanged."""
    from opensandbox.models.execd import RunCommandOpts

    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    order: list[str] = []
    sandbox.files.create_directories = AsyncMock(side_effect=lambda entries: order.append("mkdir " + entries[0].path))
    sandbox.files.write_file = AsyncMock(side_effect=lambda path, data: order.append(path))

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_MOUNTS)

    mkdir_at = order.index(f"mkdir {_ATTACH}")
    assert order.index(f"{_ATTACH}/data-0f3c9a1b.csv") > mkdir_at
    assert order.index(f"{_ATTACH}/index.json") > mkdir_at
    run = sandbox.commands.run.call_args
    assert run.args[0] == f"python {_REMOTE}_sandbox_wrapper.py {_REMOTE}user_code.py"
    assert "cd " not in run.args[0]
    assert isinstance(run.kwargs["opts"], RunCommandOpts)
    assert run.kwargs["opts"].working_directory == _REMOTE
    assert result["mounted"] == ["data-0f3c9a1b.csv", "index.json"]


async def test_opensandbox_reports_only_the_mounts_that_landed(monkeypatch):
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock()

    async def _write(path, data):
        if path.endswith("index.json"):
            raise RuntimeError("disk full")

    sandbox.files.write_file = AsyncMock(side_effect=_write)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("x = 1", input_files=_MOUNTS)
    assert result["mounted"] == ["data-0f3c9a1b.csv"]

    # A directory the sandbox refuses: nothing is mounted, the code still runs.
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock(side_effect=RuntimeError("denied"))
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("x = 1", input_files=_MOUNTS)
    assert result["mounted"] == []
    sandbox.commands.run.assert_awaited_once()
    written = [c.args[0] for c in sandbox.files.write_file.call_args_list]
    assert not any("/attachments/" in path for path in written)


async def test_opensandbox_never_collects_a_mounted_file_as_an_output(monkeypatch):
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock()
    after = [
        SimpleNamespace(path=f"{_ATTACH}/data-0f3c9a1b.csv", entry_type="file", size=8),
        SimpleNamespace(path=f"{_ATTACH}/index.json", entry_type="file", size=2),
        SimpleNamespace(path=f"{_REMOTE}out.csv", entry_type="file", size=4),
    ]
    # The "before" listing failed to see the mounts: the exclusion must not rely on it.
    sandbox.files.list_directory = AsyncMock(side_effect=[[], after])
    sandbox.files.read_bytes = AsyncMock(return_value=b"x,y\n")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("open('out.csv', 'w')", input_files=_MOUNTS)
    assert [f["filename"] for f in result["output_files"]] == ["out.csv"]


async def test_a_refused_opensandbox_run_downloads_nothing_and_mounts_nothing(monkeypatch):
    """The unreachable / quota / creation early
    returns carried no ``mounted`` key, so the tool announced every planned
    file as mounted next to the error - and the files had been downloaded
    before any gate ran. The loader now runs past the gates, and every
    return says what landed."""
    from app.services import code_interpreter as ci

    loads: list[str] = []

    async def _loader():
        loads.append("download")
        return _MOUNTS

    _set_opensandbox_probe(monkeypatch, False)
    unreachable = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_loader)
    assert unreachable["mounted"] == [] and "not reachable" in unreachable["error"]

    _set_opensandbox_probe(monkeypatch, True)
    from app.services import quota as quota_module

    monkeypatch.setattr(
        quota_module,
        "enforce_combined_sandbox_quota",
        AsyncMock(side_effect=quota_module.QuotaExceededError("sandbox", 5, 5, "monthly")),
    )
    spent = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_loader)
    assert spent["mounted"] == [] and spent["error"] == quota_module.QUOTA_EXCEEDED_MESSAGE

    monkeypatch.setattr(quota_module, "enforce_combined_sandbox_quota", AsyncMock(return_value=None))
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=RuntimeError("no capacity"))
        failed = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_loader)
    assert failed["mounted"] == [] and "Failed to create sandbox" in failed["error"]
    assert loads == [], "no gate passed, nothing downloaded"


async def test_the_opensandbox_loader_runs_once_the_sandbox_exists(monkeypatch):
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock()

    async def _loader():
        return _MOUNTS

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_loader)
    assert result["mounted"] == ["data-0f3c9a1b.csv", "index.json"]

    async def _broken():
        raise RuntimeError("storage down")

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock()
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("open('attachments/index.json')", input_files=_broken)
    assert result["mounted"] == [], "a loader that fails mounts nothing - the run still runs"
    sandbox.commands.run.assert_awaited_once()


async def test_opensandbox_without_mounts_creates_no_directory(monkeypatch):
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    sandbox.files.create_directories = AsyncMock()
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("print('hi')")
    sandbox.files.create_directories.assert_not_awaited()
    assert result["mounted"] == []


# ---------------------------------------------------------------------------
# Sandbox usage quota tracking (spec 002-sandbox-quota-usage-tracking)
# ---------------------------------------------------------------------------


def _make_ci_sandbox_mock(
    exit_code: int = 0,
    stdout: str = "ok",
    stderr: str = "",
    execution_time_in_millis: int = 842,
    metrics: SimpleNamespace | None = None,
) -> MagicMock:
    """Build a code-interpreter Sandbox async-context-manager mock."""

    def log_line(text: str) -> MagicMock:
        return MagicMock(text=text)

    result = MagicMock()
    result.exit_code = exit_code
    result.logs.stdout = [log_line(stdout)] if stdout else []
    result.logs.stderr = [log_line(stderr)] if stderr else []
    result.complete = SimpleNamespace(execution_time_in_millis=execution_time_in_millis)

    sandbox = AsyncMock()
    sandbox.__aenter__ = AsyncMock(return_value=sandbox)
    sandbox.__aexit__ = AsyncMock(return_value=False)
    sandbox.files.write_file = AsyncMock()
    sandbox.files.list_directory = AsyncMock(return_value=[])
    sandbox.commands.run = AsyncMock(return_value=result)
    sandbox.get_metrics = AsyncMock(return_value=metrics or _fake_metrics())
    return sandbox


async def test_execute_python_opensandbox_increments_usage_on_success(monkeypatch):
    """Successful run calls increment_sandbox_usage(user_id) exactly once, and
    sizes the sandbox from settings.sandbox_cpu/sandbox_ram."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(settings, "sandbox_cpu", "2")
    monkeypatch.setattr(settings, "sandbox_ram", "256Mi")
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=user_id)

    increment_mock.assert_awaited_once_with(user_id, db=None)
    assert mocksandbox.create.call_args.kwargs["resource"] == {"cpu": "2", "memory": "256Mi"}


async def test_execute_python_opensandbox_increments_usage_on_raw_stdout_fallback(monkeypatch):
    """Raw (non-JSON) stdout success path also counts as a successful call."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout="not json")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=user_id)

    increment_mock.assert_awaited_once_with(user_id, db=None)


async def test_code_interpreter_routes_to_python_pool_ref(monkeypatch):
    """code_interpreter's execute_python_opensandbox binds extensions.poolRef to
    the python pool only, even when a nodejs pool is also configured."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))
    monkeypatch.setattr(
        settings, "opensandbox_k8s_pool_names", {"python": "my-python-pool", "nodejs": "my-js-pool"}
    )

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    assert mocksandbox.create.call_args.kwargs["extensions"] == {"poolRef": "my-python-pool"}


async def test_execute_python_opensandbox_still_increments_usage_when_user_id_none(monkeypatch):
    """No user_id -> increment_sandbox_usage is still called (with None) so
    its global counter — the platform-wide cap's only remaining gate for
    unattributed calls — still gets written."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout="ok")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')")

    increment_mock.assert_awaited_once_with(None, db=None)


async def test_execute_python_opensandbox_skips_usage_when_unreachable(monkeypatch):
    """OpenSandbox unreachable -> increment_sandbox_usage never called."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, False)
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    increment_mock.assert_not_awaited()


async def test_execute_python_opensandbox_skips_usage_on_hard_exit_no_output(monkeypatch):
    """Nonzero exit with no stdout -> counted as failure, no increment."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    sandbox = _make_ci_sandbox_mock(exit_code=1, stdout="", stderr="boom")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    increment_mock.assert_not_awaited()


async def test_execute_js_opensandbox_increments_usage_on_success(monkeypatch):
    """Successful JS run calls increment_sandbox_usage(user_id) exactly once."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {}, user_id=user_id)

    increment_mock.assert_awaited_once_with(user_id, db=None)


async def test_execute_js_opensandbox_still_increments_usage_when_user_id_none(monkeypatch):
    """No user_id -> increment_sandbox_usage is still called (with None) so
    its global counter — the platform-wide cap's only remaining gate for
    unattributed calls — still gets written."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    increment_mock.assert_awaited_once_with(None, db=None)


async def test_execute_js_opensandbox_skips_usage_on_nonzero_exit(monkeypatch):
    """RuntimeError-raising nonzero exit -> no usage increment."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=1, stderr="SyntaxError: bad token")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match=r"Tool execution failed"):
            await cte.execute_js_tool_opensandbox("bad js", {}, user_id=user_id)

    increment_mock.assert_not_awaited()


async def test_execute_js_opensandbox_skips_usage_on_sandbox_creation_failure(monkeypatch):
    """SandboxException on create -> no usage increment."""
    from opensandbox.exceptions.sandbox import SandboxException

    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        with pytest.raises(RuntimeError, match=r"Failed to create sandbox"):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {}, user_id=user_id)

    increment_mock.assert_not_awaited()


async def test_execute_python_tool_opensandbox_increments_usage_on_success(monkeypatch):
    """Successful Python tool run calls increment_sandbox_usage(user_id) exactly once."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from python")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("print('hi')", {}, user_id=user_id)

    increment_mock.assert_awaited_once_with(user_id, db=None)


async def test_execute_python_tool_opensandbox_still_increments_usage_when_user_id_none(monkeypatch):
    """No user_id -> increment_sandbox_usage is still called (with None) so
    its global counter — the platform-wide cap's only remaining gate for
    unattributed calls — still gets written."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from python")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("print('hi')", {})

    increment_mock.assert_awaited_once_with(None, db=None)


async def test_execute_python_tool_opensandbox_skips_usage_on_nonzero_exit(monkeypatch):
    """Nonzero exit -> no usage increment."""
    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    sandbox = _make_sandbox_mock(exit_code=1, stderr="NameError")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError, match=r"Tool execution failed"):
            await cte.execute_python_tool_opensandbox("print('x')", {}, user_id=user_id)

    increment_mock.assert_not_awaited()


async def test_execute_python_tool_opensandbox_skips_usage_on_sandbox_creation_failure(monkeypatch):
    """SandboxException on create -> no usage increment."""
    from opensandbox.exceptions.sandbox import SandboxException

    from app.services import quota as quota_module

    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        with pytest.raises(RuntimeError, match=r"Failed to create sandbox"):
            await cte.execute_python_tool_opensandbox("print('hi')", {}, user_id=user_id)

    increment_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# disable_sandbox_quota_usage — end-to-end through the call site (US2)
# ---------------------------------------------------------------------------


async def test_execute_python_opensandbox_respects_disable_flag_end_to_end(monkeypatch):
    """With disable_sandbox_quota_usage=True, a full successful call through
    execute_python_opensandbox must not touch the sandbox usage Redis key —
    not just increment_sandbox_usage() in isolation (Foundational already
    covers that), but the real wiring from the call site through to Redis."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    monkeypatch.setattr(settings, "disable_sandbox_quota_usage", True)
    _set_opensandbox_probe(monkeypatch, True)

    fake_redis = MagicMock()
    fake_redis.incr = AsyncMock(
        side_effect=AssertionError("disable_sandbox_quota_usage=True must prevent any Redis INCR for sandbox usage")
    )
    monkeypatch.setattr(quota_module, "redis_client", AsyncMock(return_value=fake_redis))

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout="ok")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("print('hi')", user_id=user_id)

    assert result["error"] is None
    fake_redis.incr.assert_not_awaited()


# ---------------------------------------------------------------------------
# Structured per-call sandbox logging (spec 003-sandbox-call-logging)
# ---------------------------------------------------------------------------

_SANDBOX_CALL_LOGGER = "app.services.sandbox_call_logging"


def _sandbox_call_records(caplog):
    return [r for r in caplog.records if r.name == _SANDBOX_CALL_LOGGER and r.getMessage() == "sandbox_call"]


def _field(record: logging.LogRecord, key: str, default=None):
    return getattr(record, key, default)


def _assert_no_resource_or_tte_fields(record: logging.LogRecord) -> None:
    for key in ("cpu_used_percentage", "cpu_count", "memory_used_in_mib", "memory_total_in_mib", "tte_ms"):
        assert not hasattr(record, key)


def _assert_resource_and_tte_fields_present(record: logging.LogRecord, execution_time_in_millis: int = 842) -> None:
    metrics = _fake_metrics()
    assert _field(record, "cpu_used_percentage") == metrics.cpu_used_percentage
    assert _field(record, "cpu_count") == metrics.cpu_count
    assert _field(record, "memory_used_in_mib") == metrics.memory_used_in_mib
    assert _field(record, "memory_total_in_mib") == metrics.memory_total_in_mib
    assert _field(record, "tte_ms") == execution_time_in_millis


async def test_execute_python_opensandbox_logs_success_record(monkeypatch, caplog):
    """Successful code-interpreter run emits one sandbox_call record with all fields (US1)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=user_id)

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.levelno == logging.INFO
    assert _field(record, "usage_type") == "code_interpreter"
    assert _field(record, "outcome") == "success"
    assert _field(record, "user_id") == str(user_id)
    assert _field(record, "egress_default_action") == "deny"
    assert _field(record, "egress_allow_list") == []
    _assert_resource_and_tte_fields_present(record)


async def test_execute_python_opensandbox_logs_success_record_without_user_id(monkeypatch, caplog):
    """FR-002a: user_id key is omitted (not null) when the call has no user context."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout="ok")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')")

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert not hasattr(records[0], "user_id")


async def test_execute_js_opensandbox_logs_success_record(monkeypatch, caplog):
    """Successful JS tool run emits one sandbox_call record (US1)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {}, user_id=user_id)

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "usage_type") == "custom_tool_js"
    assert _field(record, "outcome") == "success"
    assert _field(record, "user_id") == str(user_id)
    _assert_resource_and_tte_fields_present(record)


async def test_execute_js_opensandbox_logs_success_record_without_user_id(monkeypatch, caplog):
    """FR-002a: user_id key omitted when the call has no user context."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    records = _sandbox_call_records(caplog)
    assert not hasattr(records[0], "user_id")


async def test_execute_python_tool_opensandbox_logs_success_record(monkeypatch, caplog):
    """Successful Python tool run emits one sandbox_call record (US1)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from python")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("print('hi')", {}, user_id=user_id)

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "usage_type") == "custom_tool_python"
    assert _field(record, "outcome") == "success"
    assert _field(record, "user_id") == str(user_id)
    _assert_resource_and_tte_fields_present(record)


async def test_execute_python_tool_opensandbox_logs_success_record_without_user_id(monkeypatch, caplog):
    """FR-002a: user_id key omitted when the call has no user context."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from python")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("print('hi')", {})

    records = _sandbox_call_records(caplog)
    assert not hasattr(records[0], "user_id")


# --- Failure-path logging (US2) ---------------------------------------------


async def test_execute_python_opensandbox_logs_ready_timeout_failure(monkeypatch, caplog):
    """SandboxReadyTimeoutException during create -> failure_reason=sandbox_ready_timeout, no resource/TTE fields."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxReadyTimeoutException

    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxReadyTimeoutException("sandbox never became ready"))
        await ci.execute_python_opensandbox("print('hi')")

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "sandbox_ready_timeout"
    _assert_no_resource_or_tte_fields(record)


async def test_execute_python_opensandbox_logs_sandbox_create_failed(monkeypatch, caplog):
    """Other SandboxException during create -> failure_reason=sandbox_create_failed."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxException

    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        await ci.execute_python_opensandbox("print('hi')")

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "sandbox_create_failed"
    _assert_no_resource_or_tte_fields(record)


async def test_execute_python_opensandbox_logs_sandbox_create_error(monkeypatch, caplog):
    """Generic Exception during create -> failure_reason=sandbox_create_error."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=ValueError("boom"))
        await ci.execute_python_opensandbox("print('hi')")

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "sandbox_create_error"
    _assert_no_resource_or_tte_fields(record)


async def test_execute_python_opensandbox_logs_nonzero_exit_with_resource_fields(monkeypatch, caplog):
    """nonzero_exit -> resource/TTE fields ARE present (sandbox reached completion,
    /speckit-analyze finding I1)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    sandbox = _make_ci_sandbox_mock(exit_code=1, stdout="", stderr="boom")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')")

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "nonzero_exit"
    _assert_resource_and_tte_fields_present(record)


async def test_execute_python_opensandbox_logs_nonzero_exit_with_stdout_as_failure(monkeypatch, caplog):
    """/code-review finding: a non-zero exit code that still produced stdout
    was previously misreported as outcome="success" (the wrapper's own
    JSON-parsed-vs-raw-fallback return shapes only distinguish stdout
    presence for usage-metering purposes, spec 002-sandbox-quota-usage-tracking
    FR-002 — but sandbox_call's outcome must reflect the actual exit code,
    per spec FR-006, regardless of whether stdout happened to be produced)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    increment_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", increment_mock)

    sandbox = _make_ci_sandbox_mock(exit_code=1, stdout="partial output before crash", stderr="boom")
    user_id = uuid.uuid4()

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await ci.execute_python_opensandbox("print('hi'); import sys; sys.exit(1)", user_id=user_id)

    # Unchanged return-value/usage-metering behavior (SC-004 / FR-002 of the
    # sibling quota spec): the sandbox ran to completion, so usage is still
    # recorded and the raw stdout is still returned to the caller.
    increment_mock.assert_awaited_once()
    assert result["stdout"] == "partial output before crash"

    # But the sandbox_call record must say failure, not success.
    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "nonzero_exit"
    _assert_resource_and_tte_fields_present(record)


async def test_execute_python_opensandbox_logs_execution_error_and_reraises(monkeypatch, caplog):
    """An exception from commands.run() itself (previously unhandled) still
    emits one execution_error record and still propagates unchanged (SC-004)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from app.services import code_interpreter as ci

    _set_opensandbox_probe(monkeypatch, True)

    sandbox = _make_ci_sandbox_mock(exit_code=0)
    original_exc = RuntimeError("execd connection dropped")
    sandbox.commands.run = AsyncMock(side_effect=original_exc)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError) as excinfo:
            await ci.execute_python_opensandbox("print('hi')")

    assert excinfo.value is original_exc
    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "execution_error"
    # An exception from commands.run() itself means the sandbox's connection
    # is presumed unhealthy, so no metrics fetch is attempted (/code-review
    # finding: it previously was, adding up to 3s of latency to an
    # already-failing call) — no resource/TTE fields at all, same as the
    # sandbox_create_* failure branches.
    _assert_no_resource_or_tte_fields(record)


async def test_execute_js_opensandbox_logs_ready_timeout_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxReadyTimeoutException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxReadyTimeoutException("never ready"))
        with pytest.raises(RuntimeError):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_ready_timeout"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_js_opensandbox_logs_sandbox_create_failed(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        with pytest.raises(RuntimeError):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_create_failed"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_js_opensandbox_logs_sandbox_create_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(RuntimeError):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_create_error"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_js_opensandbox_logs_nonzero_exit_with_resource_fields(monkeypatch, caplog):
    """nonzero_exit -> resource/TTE fields present (sandbox reached completion)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=1, stderr="SyntaxError: bad token")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError):
            await cte.execute_js_tool_opensandbox("bad js", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "failure_reason") == "nonzero_exit"
    _assert_resource_and_tte_fields_present(record)


async def test_execute_js_opensandbox_logs_execution_error_and_reraises(monkeypatch, caplog):
    """An exception from commands.run() itself (previously unhandled) still
    emits one execution_error record and still propagates unchanged (SC-004)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0)
    original_exc = RuntimeError("execd connection dropped")
    sandbox.commands.run = AsyncMock(side_effect=original_exc)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError) as excinfo:
            await cte.execute_js_tool_opensandbox("console.log('hi');", {})

    assert excinfo.value is original_exc
    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "execution_error"
    # No metrics fetch is attempted for execution_error (/code-review
    # finding: it previously was, adding latency to an already-failing
    # call) — no resource/TTE fields, same as the sandbox_create_* branches.
    _assert_no_resource_or_tte_fields(record)


async def test_execute_python_tool_opensandbox_logs_ready_timeout_failure(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxReadyTimeoutException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxReadyTimeoutException("never ready"))
        with pytest.raises(RuntimeError):
            await cte.execute_python_tool_opensandbox("print('hi')", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_ready_timeout"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_python_tool_opensandbox_logs_sandbox_create_failed(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    from opensandbox.exceptions.sandbox import SandboxException

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=SandboxException("image not found"))
        with pytest.raises(RuntimeError):
            await cte.execute_python_tool_opensandbox("print('hi')", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_create_failed"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_python_tool_opensandbox_logs_sandbox_create_error(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=ValueError("boom"))
        with pytest.raises(RuntimeError):
            await cte.execute_python_tool_opensandbox("print('hi')", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    assert _field(records[0], "failure_reason") == "sandbox_create_error"
    _assert_no_resource_or_tte_fields(records[0])


async def test_execute_python_tool_opensandbox_logs_nonzero_exit_with_resource_fields(monkeypatch, caplog):
    """nonzero_exit -> resource/TTE fields present (sandbox reached completion)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=1, stderr="NameError")

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError):
            await cte.execute_python_tool_opensandbox("print('x')", {})

    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "failure_reason") == "nonzero_exit"
    _assert_resource_and_tte_fields_present(record)


async def test_execute_python_tool_opensandbox_logs_execution_error_and_reraises(monkeypatch, caplog):
    """An exception from commands.run() itself (previously unhandled) still
    emits one execution_error record and still propagates unchanged (SC-004)."""
    caplog.set_level(logging.INFO, logger=_SANDBOX_CALL_LOGGER)
    sandbox = _make_sandbox_mock(exit_code=0)
    original_exc = RuntimeError("execd connection dropped")
    sandbox.commands.run = AsyncMock(side_effect=original_exc)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        with pytest.raises(RuntimeError) as excinfo:
            await cte.execute_python_tool_opensandbox("print('hi')", {})

    assert excinfo.value is original_exc
    records = _sandbox_call_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert _field(record, "outcome") == "failure"
    assert _field(record, "failure_reason") == "execution_error"
    # No metrics fetch is attempted for execution_error (/code-review
    # finding: it previously was, adding latency to an already-failing
    # call) — no resource/TTE fields, same as the sandbox_create_* branches.
    _assert_no_resource_or_tte_fields(record)


# ---------------------------------------------------------------------------
# Combined agentic+sandbox quota check (spec 002-sandbox-quota-usage-tracking
# FR-015/FR-018/FR-019) — the sole quota gate remaining at sandbox dispatch
# time now that spec 001's platform-wide check has been removed.
# ---------------------------------------------------------------------------


async def test_execute_python_opensandbox_refuses_on_combined_quota_exceeded(monkeypatch):
    """Combined-quota exceeded -> error-dict shape, using QUOTA_EXCEEDED_MESSAGE;
    Sandbox.create never called."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)

    exc = quota_module.QuotaExceededError("agentic", 5, 5, "monthly")
    monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock(side_effect=exc))

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        result = await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    mocksandbox.create.assert_not_awaited()
    assert result["error"] == quota_module.QUOTA_EXCEEDED_MESSAGE
    assert result["stdout"] == ""


async def test_execute_python_opensandbox_still_runs_combined_check_when_user_id_none(monkeypatch):
    """No user_id -> the combined check still runs (with None) so the
    platform-wide hard cap inside it — the only remaining gate for
    unattributed calls — still applies; only the per-user limit it can't
    resolve without a user is skipped."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    combined_check = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "check_agentic_quota", combined_check)

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')")  # no user_id

    combined_check.assert_awaited_once()
    assert combined_check.await_args.args[1] is None
    mocksandbox.create.assert_awaited_once()


async def test_execute_python_opensandbox_combined_check_proceeds_when_under_limit(monkeypatch):
    """Under the combined limit -> dispatch proceeds normally, exactly as if
    the new check didn't exist."""
    from app.services import code_interpreter as ci
    from app.services import quota as quota_module

    _set_opensandbox_probe(monkeypatch, True)
    monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock(return_value=None))

    sandbox = _make_ci_sandbox_mock(exit_code=0, stdout='{"stdout": "hi", "error": null}')
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await ci.execute_python_opensandbox("print('hi')", user_id=uuid.uuid4())

    mocksandbox.create.assert_awaited_once()


async def test_execute_js_opensandbox_raises_runtimeerror_on_combined_quota_exceeded(monkeypatch):
    """Combined-quota exceeded -> RuntimeError (not a dict — this function
    returns str)."""
    from app.services import quota as quota_module

    exc = quota_module.QuotaExceededError("agentic", 5, 5, "monthly")
    monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock(side_effect=exc))

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=r"agentic execution quota"):
            await cte.execute_js_tool_opensandbox("console.log('hi');", {}, user_id=uuid.uuid4())

    mocksandbox.create.assert_not_awaited()


async def test_execute_js_opensandbox_still_runs_combined_check_when_user_id_none(monkeypatch):
    """No user_id -> the combined check still runs (with None) so the
    platform-wide hard cap inside it still applies."""
    from app.services import quota as quota_module

    combined_check = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "check_agentic_quota", combined_check)

    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await cte.execute_js_tool_opensandbox("console.log('hi');", {})  # no user_id

    combined_check.assert_awaited_once()
    assert combined_check.await_args.args[1] is None
    assert result == "hello from js"


async def test_execute_python_tool_opensandbox_raises_runtimeerror_on_combined_quota_exceeded(monkeypatch):
    from app.services import quota as quota_module

    exc = quota_module.QuotaExceededError("agentic", 5, 5, "monthly")
    monkeypatch.setattr(quota_module, "check_agentic_quota", AsyncMock(side_effect=exc))

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=r"agentic execution quota"):
            await cte.execute_python_tool_opensandbox("def run(): return 1", {}, user_id=uuid.uuid4())

    mocksandbox.create.assert_not_awaited()


async def test_execute_python_tool_opensandbox_still_runs_combined_check_when_user_id_none(monkeypatch):
    """No user_id -> the combined check still runs (with None) so the
    platform-wide hard cap inside it still applies."""
    from app.services import quota as quota_module

    combined_check = AsyncMock(return_value=None)
    monkeypatch.setattr(quota_module, "check_agentic_quota", combined_check)

    sandbox = _make_sandbox_mock(exit_code=0, stdout="42")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await cte.execute_python_tool_opensandbox("def run(): return 1", {})  # no user_id

    combined_check.assert_awaited_once()
    assert combined_check.await_args.args[1] is None


async def test_execute_js_opensandbox_combined_check_uses_dedicated_session_not_shared_db(monkeypatch):
    """The new check must open its own dedicated session via async_session()
    rather than reusing this function's shared `db` parameter — the same
    concurrency hazard increment_sandbox_usage already had to solve
    (research.md §11.4). Two concurrent calls sharing one `shared_db` that
    raises on any use prove neither call ever touches it."""
    import app.core.database as database
    from app.services import quota as quota_module

    # increment_sandbox_usage (called later, on the success path) also opens
    # its own dedicated session for period resolution when given `db` — a
    # pre-existing, unrelated behavior. No-op it so this test isolates only
    # the NEW combined check's session usage.
    monkeypatch.setattr(quota_module, "increment_sandbox_usage", AsyncMock(return_value=None))

    class _SharedDb:
        async def execute(self, *_a, **_kw):
            raise AssertionError("must not use the caller's shared db")

    class _DedicatedSession:
        def __init__(self, registry):
            registry.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    created_sessions: list[_DedicatedSession] = []

    def _session_factory():
        return _DedicatedSession(created_sessions)

    monkeypatch.setattr(database, "async_session", _session_factory)

    seen_dbs = []

    async def _fake_check_agentic_quota(db, _user_id):
        seen_dbs.append(db)

    monkeypatch.setattr(quota_module, "check_agentic_quota", _fake_check_agentic_quota)

    shared_db = _SharedDb()
    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        await asyncio.gather(
            cte.execute_js_tool_opensandbox(
                "console.log('hi');", {}, user_id=uuid.uuid4(), db=shared_db
            ),
            cte.execute_js_tool_opensandbox(
                "console.log('hi');", {}, user_id=uuid.uuid4(), db=shared_db
            ),
        )

    assert len(created_sessions) == 2
    assert all(db is not shared_db for db in seen_dbs)
    assert set(seen_dbs) == set(created_sessions)


async def test_sandbox_dispatch_combined_check_reads_quota_fail_mode(monkeypatch):
    """Unreachable quota backend: QUOTA_FAIL_MODE=open lets dispatch proceed
    in Copilot mode; QUOTA_FAIL_MODE=closed refuses (spec FR-019), and so
    does XTM One mode whatever the variable says."""
    from app.services import quota as quota_module

    monkeypatch.setattr(
        quota_module,
        "check_agentic_quota",
        AsyncMock(side_effect=RuntimeError("quota backend unreachable")),
    )

    sandbox = _make_sandbox_mock(exit_code=0, stdout="hello from js")

    # open (Copilot mode's default) -> proceeds
    monkeypatch.setattr(settings, "platform_mode", "copilot")
    monkeypatch.setattr(settings, "quota_fail_mode", "open")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=sandbox)
        result = await cte.execute_js_tool_opensandbox(
            "console.log('hi');", {}, user_id=uuid.uuid4()
        )
    assert result == "hello from js"

    # closed -> refuses
    monkeypatch.setattr(settings, "quota_fail_mode", "closed")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=quota_module.QUOTA_UNAVAILABLE_MESSAGE):
            await cte.execute_js_tool_opensandbox(
                "console.log('hi');", {}, user_id=uuid.uuid4()
            )
    mocksandbox.create.assert_not_awaited()

    # XTM One mode, open -> refuses all the same
    monkeypatch.setattr(settings, "platform_mode", "xtm_one")
    monkeypatch.setattr(settings, "quota_fail_mode", "open")
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock()
        with pytest.raises(RuntimeError, match=quota_module.QUOTA_UNAVAILABLE_MESSAGE):
            await cte.execute_js_tool_opensandbox(
                "console.log('hi');", {}, user_id=uuid.uuid4()
            )
    mocksandbox.create.assert_not_awaited()


# ---------------------------------------------------------------------------
# Egress-sidecar cold-start retry
# ---------------------------------------------------------------------------

_EGRESS_FAILURE = "Create sandbox failed: Egress sidecar container failed to start."


def _create_kwargs(**overrides) -> dict:
    return {
        "image_key": "python",
        "timeout_seconds": 30,
        "kind_label": "Python",
        "usage_type": "custom_tool_python",
        "user_id": None,
        "egress_allow_list": frozenset(),
        "resource": {"cpu": "1", "memory": "512Mi"},
        **overrides,
    }


def _sidecar_failure():
    """The OpenSandbox server's answer when the egress sidecar did not start (HTTP 500)."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    return SandboxApiException(_EGRESS_FAILURE, status_code=500)


@pytest.fixture
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(ss, "_EGRESS_SIDECAR_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ss, "_CREATE_TIMEOUT_RETRY_DELAY_SECONDS", 0)


@pytest.mark.parametrize(
    ("message", "structured"),
    [
        (_EGRESS_FAILURE, None),
        # The server's sentence only in the structured error the SDK parsed from the body.
        ("Create sandbox failed: HTTP 500", "Egress sidecar container failed to start."),
    ],
)
async def test_create_opensandbox_retries_once_on_an_egress_sidecar_failure(_no_retry_delay, message, structured):
    """A lost sidecar startup race is retried, and a success is not a failure."""
    from opensandbox.exceptions.sandbox import SandboxApiException, SandboxError

    error = SandboxError("CONTAINER_START_FAILED", structured) if structured else None
    sandbox = MagicMock()
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(
            side_effect=[SandboxApiException(message, status_code=500, error=error), sandbox]
        )
        result = await ss._create_opensandbox(**_create_kwargs())

    assert result is sandbox
    assert mocksandbox.create.await_count == 2
    logged.assert_not_awaited()


@pytest.mark.parametrize(
    ("allow_list", "expected_targets"),
    [
        (frozenset(), None),
        (frozenset({"api.example.com", "10.0.0.1"}), ["10.0.0.1", "api.example.com"]),
    ],
)
async def test_create_opensandbox_retries_under_the_same_egress_policy(_no_retry_delay, allow_list, expected_targets):
    """The retry sends the identical default-deny policy: the sidecar is never skipped, the allow-list never widened."""
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=[_sidecar_failure(), MagicMock()])
        await ss._create_opensandbox(**_create_kwargs(egress_allow_list=allow_list))

    policies = [call.kwargs["network_policy"] for call in mocksandbox.create.await_args_list]
    assert len(policies) == 2
    assert policies[0] == policies[1]
    for policy in policies:
        assert policy.default_action == "deny"
        assert (None if policy.egress is None else sorted(rule.target for rule in policy.egress)) == expected_targets
        assert all(rule.action == "allow" for rule in policy.egress or [])


async def test_create_opensandbox_reports_a_persistent_sidecar_failure_as_a_platform_problem(_no_retry_delay):
    """Still failing after the retry: one failure logged, a platform-level error raised."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=_sidecar_failure())
        with pytest.raises(ss.SandboxUnavailableError, match=r"not a defect of the tool") as info:
            await ss._create_opensandbox(**_create_kwargs())

    assert mocksandbox.create.await_count == 1 + ss._EGRESS_SIDECAR_RETRIES
    logged.assert_awaited_once()
    message = str(info.value)
    assert "Egress sidecar container failed to start" in message
    # The server says the same for a host that can never start the sidecar
    # (upstream OpenSandbox #1168): one more try, then the agent escalates.
    assert "retry the call once" in message
    assert "administrator" in message
    assert isinstance(info.value.__cause__, SandboxApiException)


@pytest.mark.parametrize(
    ("message", "status_code"),
    [
        ("Create sandbox failed: image not found", 400),
        # Naming egress is not enough: a bad rule is permanent, not a cold start.
        ("Create sandbox failed: invalid egress rule '*.bad'", 400),
        ("Create sandbox failed: egress configuration error", 400),
        # A request the server refused is permanent, whatever it says about the sidecar.
        (_EGRESS_FAILURE, 400),
        ("Create sandbox failed: networkPolicy refused: the egress sidecar cannot start under runtime gvisor", 400),
        # Names the sidecar and "start" without being its start failure.
        ("Create sandbox failed: egress sidecar startup is disabled by configuration", 500),
    ],
)
async def test_create_opensandbox_does_not_retry_other_api_errors(_no_retry_delay, message, status_code):
    from opensandbox.exceptions.sandbox import SandboxApiException

    error = SandboxApiException(message, status_code=status_code)
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=error)
        with pytest.raises(SandboxApiException) as info:
            await ss._create_opensandbox(**_create_kwargs())

    assert info.value is error
    assert mocksandbox.create.await_count == 1
    logged.assert_awaited_once()


async def test_create_opensandbox_does_not_retry_a_ready_timeout(_no_retry_delay):
    """Only the API's sidecar failure is retried, not a (long) readiness timeout."""
    from opensandbox.exceptions.sandbox import SandboxReadyTimeoutException

    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=SandboxReadyTimeoutException("egress never became ready"))
        with pytest.raises(SandboxReadyTimeoutException):
            await ss._create_opensandbox(**_create_kwargs())

    assert mocksandbox.create.await_count == 1


# ---------------------------------------------------------------------------
# Server-side pod-ready timeout: HTTP 504 on create
# ---------------------------------------------------------------------------


def _create_timeout():
    """The OpenSandbox server's answer when the pod was not ready in sandbox_create_timeout_seconds."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    return SandboxApiException("Create sandbox failed: HTTP 504", status_code=504)


async def test_create_opensandbox_retries_once_on_a_create_timeout(_no_retry_delay):
    """A cold pod that missed the server's ready timeout gets one more attempt under the same policy."""
    sandbox = MagicMock()
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=[_create_timeout(), sandbox])
        result = await ss._create_opensandbox(**_create_kwargs(image_key="python"))

    assert result is sandbox
    assert mocksandbox.create.await_count == 2
    first, second = (call.kwargs for call in mocksandbox.create.await_args_list)
    assert first == second
    logged.assert_not_awaited()


async def test_create_opensandbox_reports_a_persistent_create_timeout_with_its_stage(_no_retry_delay):
    """Still 504 after the retry: one failure logged, a staged platform error naming the setting."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=[_create_timeout(), _create_timeout()])
        with pytest.raises(ss.SandboxUnavailableError) as info:
            await ss._create_opensandbox(**_create_kwargs(image_key="python"))

    assert mocksandbox.create.await_count == 1 + ss._CREATE_TIMEOUT_RETRIES
    logged.assert_awaited_once()
    message = str(info.value)
    assert message.startswith("stage create:")
    assert "image key 'python'" in message
    assert "2 attempts" in message
    assert "sandbox_create_timeout_seconds" in message
    assert "its size is not the cause" in message
    assert "HTTP 504" in message
    assert "60 s by default" not in message
    assert isinstance(info.value.__cause__, SandboxApiException)


async def test_create_opensandbox_timeout_carries_the_server_reason_and_each_attempt(_no_retry_delay):
    """The server's K8S_POD_READY_TIMEOUT body (last pod state, seconds) reaches the agent."""
    from opensandbox.exceptions.sandbox import SandboxApiException

    body = (
        b'{"detail": {"code": "K8S_POD_READY_TIMEOUT", "message": "Timeout waiting for sandbox sb-1 to be '
        b'Running with IP. Elapsed: 180.2s, Last state: Pending"}}'
    )
    error = SandboxApiException("Create sandbox failed: HTTP 504", status_code=504)
    error.response_body = body
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=[error, error])
        with pytest.raises(ss.SandboxUnavailableError) as info:
            await ss._create_opensandbox(**_create_kwargs(image_key="python"))

    message = str(info.value)
    assert "2 attempts of " in message
    assert "K8S_POD_READY_TIMEOUT: Timeout waiting for sandbox sb-1" in message
    assert "Last state: Pending" in message
    assert "warm buffer" in message


@pytest.mark.parametrize(
    ("error_message", "body", "expected"),
    [
        ("parsed by the SDK", None, "parsed by the SDK"),
        (None, b'{"code": "X", "message": "top level"}', "X: top level"),
        (None, b"gateway said no", "gateway said no"),
        (None, None, ""),
    ],
)
def test_server_detail_reads_the_sdk_error_or_the_raw_body(error_message, body, expected):
    from opensandbox.exceptions.sandbox import SandboxApiException, SandboxError

    error = SandboxError(SandboxError.UNEXPECTED_RESPONSE, error_message) if error_message else None
    exc = SandboxApiException("Create sandbox failed: HTTP 504", status_code=504, error=error)
    exc.response_body = body
    assert ss._server_detail(exc) == expected


async def test_create_opensandbox_records_each_attempt_in_the_create_histogram(_no_retry_delay):
    """xtmone_sandbox_create_seconds counts a timeout then an ok, labelled by image key and Pool binding."""
    from prometheus_client import REGISTRY

    def _count(outcome: str) -> float:
        labels = {"image_key": "python", "outcome": outcome, "pooled": "false"}
        return REGISTRY.get_sample_value("xtmone_sandbox_create_seconds_count", labels) or 0.0

    before_timeout, before_ok = _count("timeout"), _count("ok")
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=[_create_timeout(), MagicMock()])
        await ss._create_opensandbox(**_create_kwargs(image_key="python"))

    assert _count("timeout") == before_timeout + 1
    assert _count("ok") == before_ok + 1


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_create_opensandbox_does_not_treat_other_5xx_as_a_create_timeout(_no_retry_delay, status_code):
    from opensandbox.exceptions.sandbox import SandboxApiException

    error = SandboxApiException(f"Create sandbox failed: HTTP {status_code}", status_code=status_code)
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=error)
        with pytest.raises(SandboxApiException):
            await ss._create_opensandbox(**_create_kwargs())

    assert mocksandbox.create.await_count == 1


async def test_create_opensandbox_spreads_the_retries_of_calls_that_failed_together(monkeypatch):
    """Calls that lost the race together do not all retry at the same instant."""
    delays: list[float] = []

    async def _fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    calls = 20
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(
            side_effect=[answer for _ in range(calls) for answer in (_sidecar_failure(), MagicMock())]
        )
        for _ in range(calls):
            await ss._create_opensandbox(**_create_kwargs())

    base = ss._EGRESS_SIDECAR_RETRY_DELAY_SECONDS
    assert len(delays) == calls
    assert all(base <= delay <= 2 * base for delay in delays)
    assert len(set(delays)) > 1


# ---------------------------------------------------------------------------
# Native thread-pool caps: OpenBLAS sizes its pool on the host's CPU
# count, so on a many-core host `import numpy` exceeded the sandbox pids_limit.
# ---------------------------------------------------------------------------


def _caps(threads: str) -> dict[str, str]:
    return {"OMP_NUM_THREADS": threads, "OPENBLAS_NUM_THREADS": threads, "MKL_NUM_THREADS": threads}


@pytest.mark.parametrize(
    ("cpu", "threads"),
    [
        ("1", "1"),
        ("2", "2"),
        ("2.5", "3"),
        ("500m", "1"),
        ("4000m", "4"),
        (" 8 ", "8"),
        ("0", "1"),
        ("", "1"),
        (None, "1"),
        ("not-a-number", "1"),
        ("nan", "1"),
        ("inf", "1"),
        ("-2", "1"),
    ],
)
async def test_create_opensandbox_caps_native_thread_pools_at_the_cpu_allocation(cpu, threads):
    """Every container gets the caps from its own CPU request, never from the host's core count."""
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=MagicMock())
        await ss._create_opensandbox(**_create_kwargs(resource={"cpu": cpu, "memory": "128Mi"}))

    assert mocksandbox.create.await_count == 1
    assert mocksandbox.create.await_args.kwargs["env"] == _caps(threads)


async def test_create_opensandbox_keeps_the_callers_env_over_the_thread_caps():
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(return_value=MagicMock())
        await ss._create_opensandbox(
            **_create_kwargs(
                resource={"cpu": "2", "memory": "128Mi"},
                env={"MPLBACKEND": "Agg", "OMP_NUM_THREADS": "1"},
            )
        )

    assert mocksandbox.create.await_args.kwargs["env"] == {**_caps("2"), "MPLBACKEND": "Agg", "OMP_NUM_THREADS": "1"}


async def test_create_opensandbox_retries_with_the_same_thread_caps(_no_retry_delay):
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=[_sidecar_failure(), MagicMock()])
        await ss._create_opensandbox(**_create_kwargs(env={"MPLBACKEND": "Agg"}))

    envs = [call.kwargs["env"] for call in mocksandbox.create.await_args_list]
    assert envs == [{**_caps("1"), "MPLBACKEND": "Agg"}] * 2


@pytest.mark.parametrize(
    ("executor", "code"),
    [
        ("execute_python_tool_opensandbox", "print('hi')"),
        ("execute_js_tool_opensandbox", "console.log('hi');"),
    ],
)
async def test_custom_tools_tell_the_agent_the_platform_failed_not_the_tool(_no_retry_delay, executor, code):
    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=_sidecar_failure())
        with pytest.raises(
            RuntimeError,
            match=r"Failed to create sandbox: the sandbox infrastructure could not start",
        ):
            await getattr(cte, executor)(code, {})


@pytest.mark.parametrize("language", ["python", "javascript"])
async def test_opensandbox_mode_never_falls_back_when_the_sidecar_cannot_start(monkeypatch, _no_retry_delay, language):
    """No sidecar, no run: never the Docker or subprocess sandbox, which cannot apply the egress policy."""
    _set_opensandbox_probe(monkeypatch, True)
    fallbacks = {
        name: AsyncMock(return_value="ran without the egress policy")
        for name in ("execute_python_tool", "execute_python_tool_docker", "execute_js_tool", "execute_js_tool_docker")
    }
    for name, fallback in fallbacks.items():
        monkeypatch.setattr(cte, name, fallback)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=_sidecar_failure())
        with pytest.raises(RuntimeError, match=r"Failed to create sandbox: the sandbox infrastructure"):
            await cte.execute_tool_auto("x = 1", {}, language=language)

    assert mocksandbox.create.await_count == 1 + ss._EGRESS_SIDECAR_RETRIES
    for fallback in fallbacks.values():
        fallback.assert_not_awaited()


async def test_code_interpreter_never_runs_the_code_locally_when_the_sidecar_cannot_start(
    monkeypatch, _no_retry_delay
):
    """The code interpreter reports the platform failure and never runs the code in the local subprocess."""
    from app.services import code_interpreter as ci

    monkeypatch.setattr(settings, "disable_code_interpreter", False)
    _set_opensandbox_probe(monkeypatch, True)
    local_run = AsyncMock(side_effect=AssertionError("the code ran outside the egress policy"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", local_run)

    with patch("opensandbox.Sandbox") as mocksandbox:
        mocksandbox.create = AsyncMock(side_effect=_sidecar_failure())
        result = await ci.execute_python("print('hi')")

    assert result["error"].startswith("Failed to create sandbox: the sandbox infrastructure could not start")
    assert result["stdout"] == ""
    assert mocksandbox.create.await_count == 1 + ss._EGRESS_SIDECAR_RETRIES
    local_run.assert_not_awaited()
