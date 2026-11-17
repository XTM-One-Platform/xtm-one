"""Generic integration-extension seams — provider lifecycle hooks and the
standalone contributed-node registry. Pure unit tests, no DB."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.integrations.lifecycle import (
    dispatch_on_connect,
    dispatch_on_disconnect,
    register_lifecycle,
)
from app.integrations.node_registry import (
    get_registered_node_kind,
    node_kind_required_provider,
    register_node_kind,
    registered_node_kinds,
)


def _demo_kind(kind: str) -> dict:
    return {
        "kind": kind,
        "category": "capabilities",
        "label": "Demo",
        "description": "test-only node",
        "icon": "Boxes",
    }


# ── Contributed-node registry ────────────────────────────────────────────────


def test_gated_kind_surfaces_only_when_provider_connected():
    kind = "capability.__seamtest_gated"
    register_node_kind(_demo_kind(kind), requires_provider="demoprov")

    assert node_kind_required_provider(kind) == "demoprov"
    kinds = lambda conn: {d["kind"] for d in registered_node_kinds(conn)}  # noqa: E731
    assert kind not in kinds(None)
    assert kind not in kinds(set())
    assert kind not in kinds({"other"})
    assert kind in kinds({"demoprov"})


def test_gated_kind_still_resolvable_when_disconnected():
    # A saved flow referencing the node must resolve its ports regardless, so
    # the definition lookup is connectivity-independent.
    kind = "capability.__seamtest_resolve"
    register_node_kind(_demo_kind(kind), requires_provider="demoprov")
    assert get_registered_node_kind(kind) is not None
    assert get_registered_node_kind("capability.__never_registered") is None


def test_ungated_kind_always_available():
    kind = "capability.__seamtest_ungated"
    register_node_kind(_demo_kind(kind))
    assert node_kind_required_provider(kind) is None
    assert kind in {d["kind"] for d in registered_node_kinds(set())}


def test_register_is_idempotent_by_kind():
    kind = "capability.__seamtest_idem"
    register_node_kind(_demo_kind(kind), requires_provider="a")
    register_node_kind(_demo_kind(kind), requires_provider="b")
    assert node_kind_required_provider(kind) == "b"
    assert sum(1 for d in registered_node_kinds({"b"}) if d["kind"] == kind) == 1


def test_register_rejects_definition_without_kind():
    with pytest.raises(ValueError):
        register_node_kind({"label": "no kind here"})


# ── Lifecycle hook dispatch ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_fires_registered_hooks():
    seen: list[str] = []

    async def on_connect(_db, integration):
        seen.append(f"connect:{integration.provider}")

    async def on_disconnect(_db, integration):
        seen.append(f"disconnect:{integration.provider}")

    register_lifecycle("seamprov", on_connect=on_connect, on_disconnect=on_disconnect)
    integ = SimpleNamespace(provider="seamprov")
    await dispatch_on_connect(None, integ)
    await dispatch_on_disconnect(None, integ)
    assert seen == ["connect:seamprov", "disconnect:seamprov"]


@pytest.mark.asyncio
async def test_dispatch_is_noop_for_provider_without_hooks():
    # Every existing integration takes this path — it must be silent.
    integ = SimpleNamespace(provider="ghost")
    await dispatch_on_connect(None, integ)
    await dispatch_on_disconnect(None, integ)


@pytest.mark.asyncio
async def test_failing_hook_never_breaks_the_connect():
    async def boom(_db, _integration):
        raise RuntimeError("repo unreachable")

    register_lifecycle("boomprov", on_connect=boom)
    await dispatch_on_connect(None, SimpleNamespace(provider="boomprov"))
