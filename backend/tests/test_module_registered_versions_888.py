"""Module version tools name the version XTM One registered (#888).

The CTEM Command Center chips show ``PlatformRegistration.platform_version``; a
module's own live read (OpenCTI ``about``, OpenAEV ``/api/settings/version``,
OpenCRQ ``health``) can lack the ``-dev.<sha>`` suffix or lag the heartbeat. A
retest that read the live answer judged carry on the wrong build and said "the
commit went backwards". The version tools now name the registered version as the
source and the live answer as a fallback, and a disagreement names both without
inferring a direction.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.module_versions import RegisteredVersion, build_commit, version_answer
from app.integrations.providers.opencrq import build_tools as build_opencrq_tools
from app.integrations.providers.opencrq import frameworks as opencrq_frameworks
from app.integrations.providers.opencti import access as opencti_access
from app.integrations.providers.opencti import build_tools as build_opencti_tools
from app.tools.base import CallableToolExecutor, ToolCall

HEARTBEAT = datetime(2026, 11, 21, 12, 50, tzinfo=UTC)


def _registered(version: str) -> RegisteredVersion:
    return RegisteredVersion(
        platform_identifier="opencti",
        version=version,
        status="active",
        registered_at=HEARTBEAT,
        last_heartbeat_at=HEARTBEAT,
    )


def test_build_commit_reads_the_dev_suffix_only() -> None:
    assert build_commit("7.261119.0-dev.d736806") == "d736806"
    assert build_commit("3.261118.0") is None
    assert build_commit("") is None
    assert build_commit(None) is None


def test_disagreement_names_both_and_infers_no_direction() -> None:
    out = version_answer(
        "OpenAEV",
        'OpenAEV version: {"version": "3.261118.0"}',
        "3.261118.0",
        _registered("3.261118.0-dev.b214d9d"),
    )
    assert 'registered "3.261118.0-dev.b214d9d"' in out
    assert 'live answer "3.261118.0" (a fallback)' in out
    assert "The two sources disagree" in out
    assert "commit b214d9d" in out
    assert "tells no upgrade or downgrade" in out
    assert "backwards" not in out


def test_agreement_is_said_and_the_live_answer_is_a_fallback() -> None:
    out = version_answer(
        "OpenCTI",
        'OpenCTI version: {"version": "v7.261119.0-dev.d736806"}',
        "v7.261119.0-dev.d736806",
        _registered("7.261119.0-dev.d736806"),
    )
    assert "agrees with the registered version" in out
    assert "disagree" not in out


def test_no_registration_labels_the_live_answer_as_the_only_source() -> None:
    out = version_answer("OpenCTI", 'OpenCTI version: {"version": "6.9.4"}', "6.9.4", None)
    assert "only source" in out
    assert "fallback" in out


def test_a_failed_live_read_still_gives_the_registered_version() -> None:
    out = version_answer("OpenCTI", "Error: OpenCTI refused", "", _registered("7.261119.0-dev.d736806"))
    assert out.startswith("Error: OpenCTI refused")
    assert '"7.261119.0-dev.d736806"' in out
    assert "the registered version is the only source" in out


async def _call(executor: CallableToolExecutor, name: str) -> str:
    result = await executor.execute_tool(ToolCall(id="call-v", name=name, arguments={}))
    return str(result.content)


@pytest.mark.asyncio
async def test_opencti_tool_names_the_registered_version(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_graphql(client: Any, base_url: str, headers: Any, query: str, variables: Any = None):
        return {"about": {"version": "7.261119.0"}}, None

    async def registered() -> RegisteredVersion:
        return _registered("7.261119.0-dev.d736806")

    monkeypatch.setattr(opencti_access, "_graphql", fake_graphql)
    executor = CallableToolExecutor()
    build_opencti_tools("https://octi.local||token", executor, registered_version_fn=registered)
    out = await _call(executor, "get_opencti_platform_version")
    assert out.startswith('OpenCTI version: {"version": "7.261119.0"}')
    assert 'registered "7.261119.0-dev.d736806"' in out
    assert "The two sources disagree" in out


@pytest.mark.asyncio
async def test_opencrq_tool_keeps_the_live_value_in_its_quarantine(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_trpc(base_url: str, headers: Any, procedure: str, action: str, **kwargs: Any):
        return {"status": "ok", "version": "0.261120.0-dev.80b87cb"}, None

    async def registered() -> RegisteredVersion:
        return _registered("0.261121.0-dev.dd18c51")

    monkeypatch.setattr(opencrq_frameworks, "_trpc", fake_trpc)
    executor = CallableToolExecutor()
    build_opencrq_tools("https://crq.local||token", executor, registered_version_fn=registered)
    out = await _call(executor, "get_opencrq_platform_version")
    end = out.index("<<END-UNTRUSTED-")
    assert out.count("0.261120.0-dev.80b87cb") == 1
    assert out.index("0.261120.0-dev.80b87cb") < end
    assert 'registered "0.261121.0-dev.dd18c51"' in out[end:]
    assert "live answer above (a fallback)" in out[end:]


@pytest.mark.asyncio
async def test_openaev_tool_names_the_registered_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.providers.openaev import _common as openaev_common
    from app.integrations.providers.openaev import build_tools as build_openaev_tools

    class _Resp:
        status_code = 200
        headers: dict[str, str] = {}
        text = "3.261118.0"
        content = b"3.261118.0"

        def json(self) -> Any:
            return {}

    async def transport(client: Any, method: str, url: str, provider: str, action: str, **kwargs: Any):
        return _Resp(), None

    async def registered() -> RegisteredVersion:
        return _registered("3.261118.0-dev.b214d9d")

    monkeypatch.setattr(openaev_common, "safe_request", transport)
    executor = CallableToolExecutor()
    build_openaev_tools("https://aev.local||token", executor, registered_version_fn=registered)
    result = await executor.execute_tool(
        ToolCall(id="call-v", name="manage_openaev_settings", arguments={"action": "version"})
    )
    out = str(result.content)
    assert 'registered "3.261118.0-dev.b214d9d"' in out
    assert 'live answer "3.261118.0" (a fallback)' in out


@pytest.mark.parametrize("module", ["opencti", "openaev", "opencrq"])
def test_every_module_builder_declares_the_reader(module: str) -> None:
    import importlib
    import inspect

    # integration_tools injects the reader only into a builder declaring it (_declares).
    builder = importlib.import_module(f"app.integrations.providers.{module}").build_tools
    assert "registered_version_fn" in inspect.signature(builder).parameters


@pytest.mark.asyncio
async def test_without_a_reader_the_tools_answer_as_before(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_graphql(client: Any, base_url: str, headers: Any, query: str, variables: Any = None):
        return {"about": {"version": "6.9.4"}}, None

    monkeypatch.setattr(opencti_access, "_graphql", fake_graphql)
    executor = CallableToolExecutor()
    build_opencti_tools("https://octi.local||token", executor)
    assert await _call(executor, "get_opencti_platform_version") == 'OpenCTI version: {"version": "6.9.4"}'
