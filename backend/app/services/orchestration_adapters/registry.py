# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Module-level registry mapping ``adapter`` discriminators to instances."""

from __future__ import annotations

import threading

from app.services.orchestration_adapters.base import OrchestrationAdapter

_ADAPTERS: dict[str, OrchestrationAdapter] = {}
_LOCK = threading.RLock()


class AdapterNotFoundError(KeyError):
    """Raised when an execution references an adapter that is not registered.

    Subclasses ``KeyError`` so existing ``except KeyError`` handlers keep
    working, while the reconciler can pattern-match the distinct type to route
    "adapter missing, leave for later" vs. a genuine lookup bug.
    """


class AdapterAlreadyRegisteredError(RuntimeError):
    """Raised when two DIFFERENT adapter instances claim the same name."""


def register_adapter(adapter: OrchestrationAdapter) -> None:
    """Register an adapter under its ``name``.

    Re-registering the SAME instance is idempotent (safe for hot reload / repeat
    imports). Registering a DIFFERENT instance under a used name raises.
    """
    if not adapter.name:
        raise ValueError("OrchestrationAdapter.name must be a non-empty string")
    with _LOCK:
        existing = _ADAPTERS.get(adapter.name)
        if existing is not None and existing is not adapter:
            raise AdapterAlreadyRegisteredError(
                f"Adapter {adapter.name!r} is already registered to a different instance"
            )
        _ADAPTERS[adapter.name] = adapter


def get_adapter(name: str) -> OrchestrationAdapter:
    """Return the registered adapter for ``name`` or raise ``AdapterNotFoundError``."""
    with _LOCK:
        try:
            return _ADAPTERS[name]
        except KeyError:
            raise AdapterNotFoundError(name) from None


def list_adapters() -> list[str]:
    """Return a sorted snapshot of registered adapter names."""
    with _LOCK:
        return sorted(_ADAPTERS)


def registered_adapters() -> list[OrchestrationAdapter]:
    """Return a snapshot of registered adapter instances."""
    with _LOCK:
        return list(_ADAPTERS.values())


def _reset_registry_for_tests() -> None:
    """Clear the registry — test-only helper."""
    with _LOCK:
        _ADAPTERS.clear()
