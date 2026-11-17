"""No layer under the API reaches into a private helper of the API layer.

``app/api/`` serves HTTP and calls the services, the channels and the tools;
they never depend on its internals. A helper both need lives in a service
module (or in the API's shared ``api/v1/helpers.py``) under a public name,
and the API imports it from there. ``app/main.py``, the composition root that
wires the API in, is not a layer.
"""

from __future__ import annotations

import ast

from tests.app_sources import modules, walk

_COMPOSITION_ROOT = frozenset({"main.py"})


def _private_api_imports() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for rel, module in modules():
        if rel.startswith("api/") or rel in _COMPOSITION_ROOT:
            continue
        for node in walk(module):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[:2] == ["app", "api"]:
                found.update(
                    (rel, node.module, alias.name)
                    for alias in node.names
                    if alias.name.startswith("_")
                )
    return found


def test_no_private_api_import_outside_the_api_layer():
    found = sorted(_private_api_imports())
    assert not found, (
        "A module outside app/api imports a private helper of the API layer. "
        "Move the helper to a service module under a public name and have the "
        "API import it from there:\n" + "\n".join(f"  {f}: from {m} import {n}" for f, m, n in found)
    )
