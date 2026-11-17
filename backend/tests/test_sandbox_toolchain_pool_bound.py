"""A toolchain sandbox is always created through its Pool: the Pool's label is its registry egress.

The toolchain Pools (helm-xtm-one ``toolchain: true``) idle at ``bufferMin: 0``,
so ``pool_starved`` is true on every cold create. The #9503 fallback then
dropped the poolRef and the pod came from the server's own template
(``component: sandbox``), under the namespace deny-all: no DNS, so a JVM
check could not resolve Maven Central (gap sandbox-jvm-maven-offline).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import _sandbox_selector as ss
from app.services import sandbox_pool_reaper as reaper

POOL = "copilot-toolchain-jvm-sandbox-pool"


@pytest.fixture(autouse=True)
def _pooled(monkeypatch):
    monkeypatch.setattr(ss, "_CREATE_TIMEOUT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ss, "_EGRESS_SIDECAR_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {"toolchain-jvm": POOL, "python": "py-pool"})
    monkeypatch.setattr(
        settings,
        "opensandbox_images",
        {**settings.opensandbox_images, "toolchain-jvm": "img/jvm:1", "python": "img/py:1"},
    )


def _timeout():
    from opensandbox.exceptions.sandbox import SandboxApiException

    return SandboxApiException("Create sandbox failed: HTTP 504", status_code=504)


def _kwargs(**extra):
    return {
        "image_key": "toolchain-jvm",
        "timeout_seconds": 60,
        "kind_label": "sandbox-command",
        "usage_type": "sandbox_command",
        "user_id": None,
        "egress_allow_list": [],
        "resource": {"cpu": "4", "memory": "8Gi"},
        **extra,
    }


@pytest.mark.parametrize(
    ("key", "bound"),
    [
        ("toolchain-jvm", True),
        ("toolchain-node", True),
        ("toolchain-python", True),
        ("toolchain-rust", True),
        ("toolchain-dbt", True),
        ("python", False),
        ("nodejs", False),
        ("python-dev", False),
        ("nodejs-dev", False),
    ],
)
def test_only_the_toolchain_keys_are_pool_bound(key, bound):
    assert ss.is_pool_bound(key) is bound


async def test_a_starved_toolchain_pool_keeps_its_pool_ref():
    sandbox = MagicMock()
    with (
        patch.object(reaper, "pool_starved", new=AsyncMock(return_value=True)) as starved,
        patch.object(reaper, "request_reap", new=AsyncMock()),
        patch("opensandbox.Sandbox") as mocksandbox,
    ):
        mocksandbox.create = AsyncMock(return_value=sandbox)
        assert await ss._create_opensandbox(**_kwargs()) is sandbox
    # The starvation read is not even made: the Pool is the only route to the registries.
    starved.assert_not_awaited()
    assert mocksandbox.create.await_count == 1
    assert mocksandbox.create.await_args.kwargs["extensions"] == {"poolRef": POOL}


async def test_a_toolchain_create_that_times_out_retries_on_its_pool_never_direct():
    sandbox = MagicMock()
    with (
        patch.object(reaper, "pool_starved", new=AsyncMock(return_value=False)),
        patch.object(reaper, "request_reap", new=AsyncMock()),
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=[_timeout(), sandbox])
        assert await ss._create_opensandbox(**_kwargs()) is sandbox
    calls = [call.kwargs["extensions"] for call in mocksandbox.create.await_args_list]
    assert calls == [{"poolRef": POOL}, {"poolRef": POOL}]


async def test_a_toolchain_create_that_keeps_timing_out_fails_without_a_direct_attempt():
    with (
        patch.object(reaper, "pool_starved", new=AsyncMock(return_value=False)),
        patch.object(reaper, "request_reap", new=AsyncMock()),
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=_timeout())
        with pytest.raises(ss.SandboxUnavailableError):
            await ss._create_opensandbox(**_kwargs())
    calls = [call.kwargs["extensions"] for call in mocksandbox.create.await_args_list]
    assert calls and all(call == {"poolRef": POOL} for call in calls)


async def test_a_starved_pool_of_another_key_still_falls_back_direct():
    sandbox = MagicMock()
    with (
        patch.object(reaper, "pool_starved", new=AsyncMock(return_value=True)),
        patch.object(reaper, "request_reap", new=AsyncMock()),
        patch("opensandbox.Sandbox") as mocksandbox,
    ):
        mocksandbox.create = AsyncMock(return_value=sandbox)
        assert await ss._create_opensandbox(**_kwargs(image_key="python")) is sandbox
    assert mocksandbox.create.await_args.kwargs["extensions"] is None
