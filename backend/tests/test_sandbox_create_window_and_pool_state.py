"""A background create retries a pod-ready 504 until its window, and the pool state is read (#8574)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import _sandbox_selector as ss


@pytest.fixture(autouse=True)
def _no_retry_delay(monkeypatch):
    monkeypatch.setattr(ss, "_EGRESS_SIDECAR_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(ss, "_CREATE_TIMEOUT_RETRY_DELAY_SECONDS", 0)


def _create_timeout():
    from opensandbox.exceptions.sandbox import SandboxApiException

    return SandboxApiException("Create sandbox failed: HTTP 504", status_code=504)


def _kwargs(**extra):
    return {
        "image_key": "python",
        "timeout_seconds": 60,
        "kind_label": "sandbox-command",
        "usage_type": "sandbox_command",
        "user_id": None,
        "egress_allow_list": [],
        "resource": {"cpu": "1", "memory": "256Mi"},
        **extra,
    }


async def test_a_create_with_a_window_keeps_retrying_a_504_until_the_pod_is_ready():
    sandbox = MagicMock()
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=[_create_timeout()] * 5 + [sandbox])
        result = await ss._create_opensandbox(**_kwargs(retry_timeouts_for=900.0))
    assert result is sandbox
    assert mocksandbox.create.await_count == 6
    logged.assert_not_awaited()


async def test_a_create_with_a_window_fails_once_the_window_is_spent(monkeypatch):
    # A fake clock on the module only (the event loop keeps the real one): 100 s per reading.
    clock = iter(range(0, 1_000_000, 100))
    monkeypatch.setattr(ss, "time", SimpleNamespace(monotonic=lambda: float(next(clock))))
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()) as logged,
    ):
        mocksandbox.create = AsyncMock(side_effect=_create_timeout())
        with pytest.raises(ss.SandboxUnavailableError) as info:
            await ss._create_opensandbox(**_kwargs(retry_timeouts_for=600.0))
    assert 1 < mocksandbox.create.await_count < 10
    logged.assert_awaited_once()
    assert "stage create" in str(info.value)


async def test_a_create_without_a_window_keeps_the_single_retry():
    with (
        patch("opensandbox.Sandbox") as mocksandbox,
        patch("app.services.sandbox_call_logging.log_sandbox_create_failure", new=AsyncMock()),
    ):
        mocksandbox.create = AsyncMock(side_effect=_create_timeout())
        with pytest.raises(ss.SandboxUnavailableError):
            await ss._create_opensandbox(**_kwargs())
    assert mocksandbox.create.await_count == 1 + ss._CREATE_TIMEOUT_RETRIES


def test_the_retry_pause_grows_and_is_capped():
    assert ss._create_retry_delay(1) == 0
    with patch.object(ss, "_CREATE_TIMEOUT_RETRY_DELAY_SECONDS", 2.0):
        assert 2.0 <= ss._create_retry_delay(1) <= 4.0
        assert 8.0 <= ss._create_retry_delay(3) <= 16.0
        assert ss._create_retry_delay(20) <= 2 * ss._CREATE_TIMEOUT_RETRY_DELAY_MAX_SECONDS


def _info(image: str, state: str, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(image=SimpleNamespace(image=image), status=SimpleNamespace(state=state, reason=reason, message=""))


class _Manager:
    def __init__(self, pages):
        self.pages = pages
        self.filters = []

    async def list_sandbox_infos(self, filter):
        self.filters.append(filter)
        infos, has_next = self.pages[len(self.filters) - 1]
        return SimpleNamespace(sandbox_infos=infos, pagination=SimpleNamespace(has_next_page=has_next))


async def test_the_pool_state_counts_the_pods_per_image_key(monkeypatch):
    monkeypatch.setattr(settings, "opensandbox_images", {"python": "img/py:1", "python-dev": "img/pydev:1"})
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {})
    manager = _Manager(
        [
            ([_info("img/py:1", "Running"), _info("img/pydev:1", "Pending", "ImagePulling")], True),
            ([_info("img/pydev:1", "Pending"), _info("img/pydev:1", "Running"), _info("x:1", "Terminated")], False),
        ]
    )
    state = await ss.sandbox_pool_state(manager=manager)
    assert state == {
        "other": {"ready": 0, "creating": 0, "pulling": 0, "other": 1},
        "python": {"ready": 1, "creating": 0, "pulling": 0, "other": 0},
        "python-dev": {"ready": 1, "creating": 1, "pulling": 1, "other": 0},
    }
    assert [f.page for f in manager.filters] == [1, 2]
    narrowed = await ss.sandbox_pool_state("nodejs", manager=_Manager([([_info("img/py:1", "Running")], False)]))
    assert narrowed == {"nodejs": {"ready": 0, "creating": 0, "pulling": 0, "other": 0}}
    assert "0 running, 0 creating, 0 pulling" in ss.pool_state_words("nodejs", narrowed)


async def test_the_starting_answer_carries_the_pool_state(monkeypatch):
    from app.services.sandbox_workspace import commands

    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {})

    async def pool_state(image_key):
        return {image_key: {"ready": 0, "creating": 2, "pulling": 1, "other": 0}}

    words = await commands._pool_words(SimpleNamespace(pool_state=pool_state), "python-dev")
    assert words == "Sandbox pods for image key 'python-dev' on the server now: 0 running, 2 creating, 1 pulling the image."

    async def broken(_image_key):
        raise RuntimeError("down")

    assert await commands._pool_words(SimpleNamespace(pool_state=broken), "python") == ""
    assert await commands._pool_words(SimpleNamespace(), "python") == ""


async def test_the_pool_state_fails_open():
    class Broken:
        async def list_sandbox_infos(self, _filter):
            raise RuntimeError("connection refused")

    assert await ss.sandbox_pool_state(manager=Broken()) is None
    assert ss.pool_state_words("python", None) == ""


def test_a_pooled_key_never_reads_a_false_zero(monkeypatch):
    # #9383: the server lists a pooled sandbox with image "unknown" and never
    # lists the Pool's warm pods, so a count for a pooled key is always 0.
    monkeypatch.setattr(settings, "opensandbox_k8s_pool_names", {"python": "copilot-python-sandbox-pool"})
    state = {"python": {"ready": 0, "creating": 0, "pulling": 0, "other": 0}}
    words = ss.pool_state_words("python", state)
    assert "0 running" not in words
    assert "'copilot-python-sandbox-pool'" in words and "not observable" in words
    assert ss.pool_state_words("python", None) == ""
    # An unpooled key keeps its counts.
    assert ss.pool_state_words("nodejs", {"nodejs": {"ready": 1, "creating": 0, "pulling": 0, "other": 0}}).startswith(
        "Sandbox pods for image key 'nodejs' on the server now: 1 running"
    )
