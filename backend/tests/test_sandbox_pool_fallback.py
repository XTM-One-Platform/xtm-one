"""A broken Pool never blocks a sandbox create: one direct create under the same policy (#9503)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import _sandbox_selector as ss
from app.services import sandbox_pool_reaper as reaper
from app.services.sandbox_workspace.kube import KubeError

NOW = datetime(2026, 11, 7, 6, 0, tzinfo=UTC)
POOL = "copilot-python-sandbox-pool"


@pytest.fixture(autouse=True)
def _pooled(monkeypatch):
    monkeypatch.setattr(ss, "_EGRESS_SIDECAR_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ss, "_CREATE_TIMEOUT_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {"python": POOL})
    monkeypatch.setattr(settings, "opensandbox_images", {**settings.opensandbox_images, "python": "img/py:1"})


def _timeout():
    from opensandbox.exceptions.sandbox import SandboxApiException

    return SandboxApiException("Create sandbox failed: HTTP 504", status_code=504)


def _kwargs(**extra):
    return {
        "image_key": "python",
        "timeout_seconds": 60,
        "kind_label": "code-interpreter",
        "usage_type": "code_interpreter",
        "user_id": None,
        "egress_allow_list": ["pypi.org"],
        "resource": {"cpu": "1", "memory": "256Mi"},
        **extra,
    }


def _starved(value: bool):
    return patch.object(reaper, "pool_starved", new=AsyncMock(return_value=value))


async def test_a_pooled_create_that_times_out_falls_back_once_to_a_direct_create():
    sandbox = MagicMock()
    with (
        _starved(False),
        patch.object(reaper, "request_reap", new=AsyncMock()) as reap,
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=[_timeout(), sandbox])
        result = await ss._create_opensandbox(**_kwargs())
    assert result is sandbox
    pooled, direct = (call.kwargs for call in mocksandbox.create.await_args_list)
    assert pooled["extensions"] == {"poolRef": POOL}
    assert direct["extensions"] is None
    # Everything but the Pool binding is identical: image, resources, env, the default-deny policy.
    assert {k: v for k, v in pooled.items() if k != "extensions"} == {
        k: v for k, v in direct.items() if k != "extensions"
    }
    assert direct["network_policy"].default_action == "deny"
    reap.assert_awaited_once()
    logged.assert_not_awaited()


async def test_the_fallback_happens_once_per_call():
    with (
        _starved(False),
        patch.object(reaper, "request_reap", new=AsyncMock()),
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=_timeout())
        with pytest.raises(ss.SandboxUnavailableError):
            await ss._create_opensandbox(**_kwargs())
    calls = [call.kwargs["extensions"] for call in mocksandbox.create.await_args_list]
    # One pooled attempt, then the direct one (the fallback is the call's 504 retry), never back to the Pool.
    assert calls == [{"poolRef": POOL}, None]


async def test_a_starved_pool_is_skipped_before_the_first_attempt():
    sandbox = MagicMock()
    with (
        _starved(True),
        patch.object(reaper, "request_reap", new=AsyncMock()) as reap,
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(return_value=sandbox)
        assert await ss._create_opensandbox(**_kwargs()) is sandbox
    assert mocksandbox.create.await_count == 1
    assert mocksandbox.create.await_args.kwargs["extensions"] is None
    assert mocksandbox.create.await_args.kwargs["network_policy"].default_action == "deny"
    reap.assert_awaited_once()


async def test_an_unpooled_key_never_reads_the_pool():
    with (
        patch.object(reaper, "pool_starved", new=AsyncMock(return_value=True)) as starved,
        patch("opensandbox.Sandbox") as mocksandbox,
    ):
        mocksandbox.create = AsyncMock(return_value=MagicMock())
        await ss._create_opensandbox(**_kwargs(image_key="nodejs"))
    starved.assert_not_awaited()


# --- pool_starved -----------------------------------------------------------


def _pod(phase: str, *, age: timedelta = timedelta(seconds=30), waiting: str | None = None) -> dict:
    status: dict = {"phase": phase}
    if waiting:
        status["initContainerStatuses"] = [{"name": "init", "state": {"waiting": {"reason": waiting}}}]
    created = (NOW - age).isoformat().replace("+00:00", "Z")
    return {
        "metadata": {"name": f"p-{phase}", "labels": {reaper.POOL_LABEL: POOL}, "creationTimestamp": created},
        "status": status,
    }


class _Kube:
    def __init__(self, pool: dict | None, pods: list[dict], *, error: KubeError | None = None):
        self.pool, self.pods, self.error = pool, pods, error
        self.selectors: list[str] = []

    async def get_custom(self, group, version, plural, name):
        if self.error:
            raise self.error
        assert (group, version, plural, name) == (reaper.GROUP, reaper.VERSION, reaper.POOLS, POOL)
        return self.pool

    async def list_pods(self, selector):
        self.selectors.append(selector)
        return self.pods


@pytest.fixture
def _reaper_on(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_k8s_namespace", "sandbox")


@pytest.mark.usefixtures("_reaper_on")
@pytest.mark.parametrize(
    ("pool", "pods", "starved"),
    [
        # The incident: 3 dead pods fill the buffer, 0 available, nothing creating.
        ({"status": {"total": 3, "available": 0, "allocated": 0}}, [_pod("Failed"), _pod("Failed"), _pod("Succeeded")], True),
        # Every pod allocated and none on its way.
        ({"status": {"total": 2, "available": 0, "allocated": 2}}, [_pod("Running"), _pod("Running")], True),
        # A pod is warming up: the pooled create waits for it.
        ({"status": {"total": 1, "available": 0}}, [_pod("Pending")], False),
        # A Pending pod stuck past the threshold does not count as on its way.
        ({"status": {"available": 0}}, [_pod("Pending", age=timedelta(hours=1))], True),
        ({"status": {"available": 2}}, [], False),
        (None, [], False),
        ({"status": {"available": "n/a"}}, [], False),
    ],
)
async def test_pool_starved(pool, pods, starved):
    kube = _Kube(pool, pods)
    assert await reaper.pool_starved(POOL, client=kube, now=NOW) is starved


@pytest.mark.usefixtures("_reaper_on")
async def test_pool_starved_fails_open():
    kube = _Kube({"status": {}}, [], error=KubeError("GET pools: 403 forbidden", 403))
    assert await reaper.pool_starved(POOL, client=kube, now=NOW) is False


async def test_pool_starved_is_off_without_the_namespace(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_k8s_namespace", "")
    kube = _Kube({"status": {"available": 0}}, [])
    assert await reaper.pool_starved(POOL, client=kube, now=NOW) is False
    assert kube.selectors == []
