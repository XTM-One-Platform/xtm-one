"""Regression tests for the branding-pass import surface.

These tests guard against two specific regressions that almost slipped
into production during the XTM One branding refactor:

1. ``DEFAULT_SYSTEM_PROMPT`` was removed from
   :mod:`app.models.general_assistant_config` so that the system prompt
   is re-evaluated at row-creation time (and reflects the current
   branding resolver).  Several modules still imported the removed name
   lazily — ``settings.py`` and ``channels/router.py``. The first request
   that touched those lazy imports raised ``ImportError`` in production.
   These assertions guarantee that either the constant still exists OR all
   callers have migrated to the callable ``default_system_prompt()`` so a stale
   ``DEFAULT_SYSTEM_PROMPT`` import can never resurface.

2. :func:`app.api.v1.platform_settings_helpers.get_platform_settings`
   used to initialise a brand-new ``PlatformSettings`` row from a hardcoded
   mode-default dict (``{"xtm_one": "XTM One", "copilot":
   "Filigran Copilot"}``).  That silently froze the deployment-mode default
   into the database. This test pins the contract that a fresh singleton row
   leaves ``platform_name`` unset so the central branding resolver keeps
   authority.
"""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from tests import app_sources


def test_default_system_prompt_callable_is_exported_and_callable():
    """The callable replacement for the removed module-level constant
    must still exist and resolve to a non-empty string mentioning the
    current product name (resolved via ``get_cached_branding``)."""
    from app.models.general_assistant_config import default_system_prompt
    from app.services.branding import get_cached_branding

    prompt = default_system_prompt()
    assert isinstance(prompt, str) and prompt
    assert get_cached_branding().name in prompt


def test_no_stale_default_system_prompt_import_remains():
    """Walk every Python file under ``app/`` looking for an import of
    the removed ``DEFAULT_SYSTEM_PROMPT`` symbol from
    :mod:`app.models.general_assistant_config`.  Static AST-based
    scanning catches both the eager ``from … import …`` form and the
    lazy ``from … import …`` inside a function body, so adding a new
    consumer with the wrong import path lands as a test failure
    instead of a 500 on first request."""
    app_root = Path(__file__).resolve().parent.parent / "app"
    target_module_suffix = "models.general_assistant_config"
    offenders: list[str] = []

    for py in app_sources.python_files(app_root):
        try:
            tree = app_sources.tree(py)
        except SyntaxError:
            continue
        for node in app_sources.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            mod = node.module or ""
            if not mod.endswith(target_module_suffix):
                continue
            for alias in node.names:
                if alias.name == "DEFAULT_SYSTEM_PROMPT":
                    offenders.append(f"{py.relative_to(app_root)}:{node.lineno}")

    assert not offenders, (
        "Stale ``DEFAULT_SYSTEM_PROMPT`` imports detected. The constant "
        "was removed in favour of the callable ``default_system_prompt()``; "
        "update these call sites:\n  " + "\n  ".join(offenders)
    )


def test_settings_and_router_modules_import_cleanly():
    """End-to-end regression: importing the two modules that previously
    referenced the stale constant must not raise ``ImportError``.  A
    bare ``import`` is enough because the lazy ``from … import …``
    lives inside function bodies — but if anyone re-introduces the
    removed name at module top level, this assertion fails fast."""
    importlib.import_module("app.api.v1.settings")
    importlib.import_module("app.channels.router")


@pytest.mark.asyncio
async def test_get_platform_settings_creates_row_with_null_platform_name(
    monkeypatch,
):
    """A freshly-created ``PlatformSettings`` row must leave
    ``platform_name = None`` so the mode-aware runtime default
    (resolved at read time via ``get_cached_branding``) is used.

    Persisting a snapshot of the current effective name would freeze
    the brand in the DB, masking later ``PLATFORM_MODE`` /
    ``PLATFORM_NAME`` changes — the explicit ``NULL`` sentinel is the
    contract that lets the resolver keep authority.
    """
    from app.api.v1 import platform_settings_helpers

    captured: list[object] = []

    class _FakeDB:
        async def execute(self, _statement):
            class _R:
                @staticmethod
                def scalar_one_or_none() -> None:
                    return None

            return _R()

        def add(self, obj):
            captured.append(obj)

        async def flush(self) -> None:
            return None

        def begin_nested(self):
            # ``get_platform_settings`` wraps its INSERT in a SAVEPOINT —
            # return a no-op async context manager standing in for
            # ``AsyncSessionTransaction``.
            class _Savepoint:
                async def __aenter__(self):
                    return self

                async def __aexit__(self, *exc_info):
                    return False

            return _Savepoint()

    ps = await platform_settings_helpers.get_platform_settings(_FakeDB())
    assert ps is captured[0]
    assert ps.platform_name is None
