"""``get_opencti_platform_version`` and ``get_opencrq_platform_version`` read the bound platform's version.

OpenAEV had ``manage_openaev_settings`` action ``version``; OpenCTI and OpenCRQ had no version read,
so a run could not tell whether a module upgrade had landed. OpenCTI answers GraphQL
``about { version }``, OpenCRQ its public tRPC ``health`` procedure (``version``).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.opencrq import build_tools as build_opencrq_tools
from app.integrations.providers.opencrq import frameworks as opencrq_frameworks
from app.integrations.providers.opencti import access as opencti_access
from app.integrations.providers.opencti import build_tools as build_opencti_tools
from app.tools.base import CallableToolExecutor, ToolCall


async def _call(executor: CallableToolExecutor, name: str) -> str:
    result = await executor.execute_tool(ToolCall(id="call-v", name=name, arguments={}))
    return str(result.content)


def _opencti(
    monkeypatch: pytest.MonkeyPatch, answer: tuple[dict[str, Any], str | None], sent: list[str]
) -> CallableToolExecutor:
    async def fake_graphql(
        client: Any, base_url: str, headers: Any, query: str, variables: Any = None
    ) -> tuple[dict[str, Any], str | None]:
        sent.append(query)
        return answer

    monkeypatch.setattr(opencti_access, "_graphql", fake_graphql)
    executor = CallableToolExecutor()
    build_opencti_tools("https://octi.local||token", executor)
    return executor


def _opencrq(monkeypatch: pytest.MonkeyPatch, answer: tuple[Any, str | None], sent: list[str]) -> CallableToolExecutor:
    async def fake_trpc(
        base_url: str, headers: Any, procedure: str, action: str, **kwargs: Any
    ) -> tuple[Any, str | None]:
        sent.append(procedure)
        return answer

    monkeypatch.setattr(opencrq_frameworks, "_trpc", fake_trpc)
    executor = CallableToolExecutor()
    build_opencrq_tools("https://crq.local||token", executor)
    return executor


@pytest.mark.asyncio
async def test_opencti_version_reads_about(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    executor = _opencti(monkeypatch, ({"about": {"version": " 6.9.4 "}}, None), sent)
    out = await _call(executor, "get_opencti_platform_version")
    assert out == 'OpenCTI version: {"version": "6.9.4"}'
    assert "about { version }" in sent[-1]


@pytest.mark.asyncio
async def test_opencti_version_missing_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _opencti(monkeypatch, ({"about": {}}, None), [])
    out = await _call(executor, "get_opencti_platform_version")
    assert "could not be read" in out


@pytest.mark.asyncio
async def test_opencti_version_error_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _opencti(monkeypatch, ({}, "Error: OpenCTI refused"), [])
    assert await _call(executor, "get_opencti_platform_version") == "Error: OpenCTI refused"


@pytest.mark.asyncio
async def test_opencrq_version_reads_health(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    executor = _opencrq(monkeypatch, ({"status": "ok", "version": "1.261118.0"}, None), sent)
    out = await _call(executor, "get_opencrq_platform_version")
    assert out == 'OpenCRQ version: {"version": "1.261118.0"}'
    assert sent == ["health"]


@pytest.mark.asyncio
async def test_opencrq_version_missing_is_said(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _opencrq(monkeypatch, ({"status": "ok"}, None), [])
    assert "could not be read" in await _call(executor, "get_opencrq_platform_version")


@pytest.mark.asyncio
async def test_opencrq_version_error_is_passed_through(monkeypatch: pytest.MonkeyPatch) -> None:
    executor = _opencrq(monkeypatch, (None, "Error: OpenCRQ unreachable"), [])
    assert await _call(executor, "get_opencrq_platform_version") == "Error: OpenCRQ unreachable"
