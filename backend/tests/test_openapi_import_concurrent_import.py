"""The openapi_import package is safe to import from several threads at once.

A turn of the Assistant died before its first generation with
"deadlock detected by _ModuleLock('app.services.openapi_import.schema')":
importlib holds a submodule's lock while it imports the parent package, and
the package ``__init__`` imported its submodules eagerly, so a thread importing
``openapi_import.schema`` directly and a thread importing the package each
held the lock the other waited for. The package now re-exports lazily.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
PACKAGE = BACKEND / "app" / "services" / "openapi_import"


def _eager_submodule_imports(tree: ast.Module) -> list[str]:
    """Top-level imports of the package's own submodules outside ``TYPE_CHECKING``."""
    found = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.services.openapi_import."):
            found.append(node.module or "")
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith("app.services.openapi_import."))
    return found


def test_package_init_imports_no_submodule_eagerly():
    tree = ast.parse((PACKAGE / "__init__.py").read_text(encoding="utf-8"))
    assert _eager_submodule_imports(tree) == []


def test_every_public_name_resolves():
    import app.services.openapi_import as pkg

    for name in pkg.__all__:
        assert getattr(pkg, name) is not None, name
    assert set(pkg.__all__) == set(pkg._EXPORTS)


def test_an_unknown_name_is_an_attribute_error():
    import app.services.openapi_import as pkg

    assert not hasattr(pkg, "not_an_export")


def test_concurrent_first_imports_on_a_fresh_interpreter_do_not_deadlock():
    script = textwrap.dedent(
        f"""
        import importlib, sys, threading
        sys.path.insert(0, {str(BACKEND)!r})
        targets = [
            "app.services.openapi_import.schema",
            "app.services.openapi_import",
            "app.services.openapi_import.security",
            "app.services.openapi_import.service",
            "app.services.openapi_import.operations",
            "app.services.openapi_import.discovery",
        ] * 4
        barrier = threading.Barrier(len(targets))
        errors = []

        def run(name):
            barrier.wait()
            try:
                importlib.import_module(name)
            except Exception as exc:
                errors.append(f"{{name}}: {{type(exc).__name__}}: {{exc}}")

        threads = [threading.Thread(target=run, args=(t,)) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        print("ERRORS", errors)
        sys.exit(1 if errors or any(t.is_alive() for t in threads) else 0)
        """
    )
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)  # noqa: S603
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
