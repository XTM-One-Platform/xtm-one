"""No code path creates an A2A integration without its settings row.

An A2A integration without its ``a2a_integration_settings`` row belongs to
neither direction: it can neither mint a partner credential nor delegate, and
reads as connected (#11778, the ``create_integration`` tool). Every function
under ``app/`` that constructs an ``Integration`` either runs
``provision_settings`` on it or is listed below with the reason its provider
can never be A2A. A new create path fails here until it does one or the other.
"""

from __future__ import annotations

import ast

from app.a2a.providers import ALL_A2A_PROVIDERS
from tests import app_sources

#: ``(module, function)`` creating an Integration whose provider is never A2A.
_NEVER_A2A = {
    # A bundle's provider is always a ``custom:`` slug (normalize_custom_provider_slug).
    ("api/v1/integrations.py", "import_integration_bundle"),
    # A registered OpenCTI / OpenAEV / OpenCRQ platform's own provider key.
    ("api/v1/platform.py", "register_platform"),
    # BUILTIN_INTEGRATIONS, held to no A2A provider by the test below.
    ("agents/seed.py", "seed_builtin_integrations"),
}


def _imports_integration_model(module: ast.Module) -> bool:
    for node in app_sources.walk(module):
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "app.models.integration"
            and any(alias.name == "Integration" for alias in node.names)
        ):
            return True
    return False


def _creators(module: ast.Module):
    """``(function name, calls provision_settings)`` per outermost function building an Integration."""
    for node in module.body:
        for fn in ast.walk(node):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
            builds = any(isinstance(c.func, ast.Name) and c.func.id == "Integration" for c in calls)
            if not builds:
                continue
            provisions = any(
                (isinstance(c.func, ast.Name) and c.func.id == "provision_settings")
                or (isinstance(c.func, ast.Attribute) and c.func.attr == "provision_settings")
                for c in calls
            )
            yield fn.name, provisions


def test_every_integration_create_path_provisions_a2a_settings_or_is_never_a2a():
    offenders = []
    seen = set()
    for path, module in app_sources.modules():
        if not _imports_integration_model(module):
            continue
        for name, provisions in _creators(module):
            seen.add((path, name))
            if not provisions and (path, name) not in _NEVER_A2A:
                offenders.append(f"{path}:{name}")
    assert not offenders, (
        "These functions create an Integration without app.a2a.providers.provision_settings; "
        "an A2A integration created there has no direction: " + ", ".join(sorted(set(offenders)))
    )
    # The list names live functions only.
    assert not (_NEVER_A2A - seen), sorted(_NEVER_A2A - seen)


def test_the_known_create_paths_are_covered():
    names = {name for path, module in app_sources.modules() for name, ok in _creators(module) if ok}
    # REST create and the agent tool both provision.
    assert {"create_integration", "build_agent_builder_tool"} & names


def test_the_seeded_integrations_are_never_a2a():
    from app.agents.builtin_agents import BUILTIN_INTEGRATIONS

    assert not [d["provider"] for d in BUILTIN_INTEGRATIONS if d["provider"] in ALL_A2A_PROVIDERS]


def test_a_bundle_provider_is_never_an_a2a_key():
    from app.services.integration_bundle_manager import normalize_custom_provider_slug

    for key in ALL_A2A_PROVIDERS:
        assert normalize_custom_provider_slug(key) not in ALL_A2A_PROVIDERS


def test_the_backfill_covers_every_a2a_provider():
    from pathlib import Path

    migration = (
        Path(app_sources.BACKEND_DIR) / "alembic" / "versions" / "a2ah1dirrow01_backfill_a2a_settings_rows.py"
    ).read_text(encoding="utf-8")
    for key in ALL_A2A_PROVIDERS:
        assert f'"{key}"' in migration
