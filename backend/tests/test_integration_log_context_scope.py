# SPDX-License-Identifier: Apache-2.0

"""#2197: the integration log fields a token resolution binds never outlive it.

On main they stayed bound on the task: later unrelated lines (the run's own, another
product's read errors) were printed with the last resolved integration's name.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
import structlog

from app.services import integration_tools


@pytest.fixture(autouse=True)
def _clean_context() -> Iterator[None]:
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _binding_resolver(name: str) -> Callable[..., Awaitable[str]]:
    async def _resolve(*_args: Any, **_kwargs: Any) -> str:
        structlog.contextvars.bind_contextvars(
            integration_name=name,
            integration_owner_user_id="owner-1",
            integration_auth_type="api_key",
            integration_is_company_managed=True,
        )
        return "token"

    return _resolve


async def test_a_token_read_leaves_no_integration_field_on_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_tools, "_resolve_token", _binding_resolver("OpenAEV - CTEM"))
    structlog.contextvars.bind_contextvars(run_id="r1")

    assert await integration_tools._get_token(None, "id-1", "openaev") == "token"  # type: ignore[arg-type]

    assert structlog.contextvars.get_contextvars() == {"run_id": "r1"}


async def test_the_callers_own_integration_fields_are_put_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_tools, "_resolve_token", _binding_resolver("OpenCRQ"))
    structlog.contextvars.bind_contextvars(integration_name="OpenCTI - CTEM", integration_provider="opencti")

    await integration_tools._get_token(None, "id-2", "opencrq")  # type: ignore[arg-type]

    assert structlog.contextvars.get_contextvars() == {
        "integration_name": "OpenCTI - CTEM",
        "integration_provider": "opencti",
    }


async def test_a_failed_read_leaves_no_integration_field_either(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fails(*_args: Any, **_kwargs: Any) -> str:
        structlog.contextvars.bind_contextvars(integration_name="Jira")
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(integration_tools, "_resolve_token", _fails)

    with pytest.raises(RuntimeError):
        await integration_tools._get_token(None, "id-3", "jira")  # type: ignore[arg-type]

    assert structlog.contextvars.get_contextvars() == {}


async def test_the_build_loop_keeps_the_fields_for_its_own_block(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(integration_tools, "_resolve_token", _binding_resolver("GitHub"))

    await integration_tools._get_token(None, "id-4", "github", keep_log_context=True)  # type: ignore[arg-type]

    assert structlog.contextvars.get_contextvars()["integration_name"] == "GitHub"
