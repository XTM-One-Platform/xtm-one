"""``a2a_sdk`` is imported by exactly two modules, and its types stay inside.

A static guard, because the alternative is a docstring nobody checks.
``app/a2a/protocol.py`` states the rule and this file is what enforces it —
the same pairing as ``test_migration_predicate_matches_binds_a2a``, where a
claim in a comment is pinned against the thing it describes.

The rule is not "one importer" but "one shaper, and no SDK type escapes
``app/a2a``". Two modules import the SDK for genuinely different reasons:
``protocol`` shapes versions and states, ``client.sdk`` speaks to partners.
A third would need a reason neither covers.
"""

from __future__ import annotations

import ast
import pathlib

from tests import app_sources

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

#: The complete list. Adding to it is a deliberate act that should come with a
#: reason in the new module's docstring, which is why this test names them
#: rather than counting them.
PERMITTED = {"app/a2a/protocol.py", "app/a2a/client/sdk.py"}


def _imports_a2a_sdk(path: pathlib.Path) -> bool:
    tree = app_sources.tree(path)
    for node in app_sources.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "a2a" or a.name.startswith("a2a.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "a2a" or mod.startswith("a2a."):
                return True
    return False


def test_only_the_permitted_modules_import_the_sdk():
    offenders = set()
    for path in app_sources.python_files(APP):
        rel = path.relative_to(APP.parent).as_posix()
        if _imports_a2a_sdk(path):
            offenders.add(rel)
    unexpected = offenders - PERMITTED
    assert not unexpected, (
        f"{sorted(unexpected)} import a2a_sdk outside the containment "
        "boundary. Either route through app.a2a.protocol (shaping) or "
        "app.a2a.client.sdk (partner I/O), or add the module to PERMITTED "
        "with a reason in its docstring."
    )


def test_both_permitted_modules_actually_import_it():
    # A permitted entry that no longer imports the SDK is a stale exemption,
    # and a stale exemption is how the next module quietly joins the list.
    for rel in PERMITTED:
        path = APP.parent / rel
        assert path.exists(), f"{rel} is listed as permitted but does not exist"
        assert _imports_a2a_sdk(path), (
            f"{rel} no longer imports a2a_sdk — remove it from PERMITTED"
        )


def test_the_protocol_docstring_names_the_other_importer():
    # The rule and its enforcement must agree. A docstring still claiming
    # "the ONLY module" beside a second importer is the kind of drift that
    # makes a reader trust neither.
    from app.a2a import protocol

    assert "app.a2a.client.sdk" in (protocol.__doc__ or "")


def test_partner_card_is_a_platform_type():
    # The invariant that actually matters: what leaves the boundary is ours.
    from app.a2a.client.sdk import PartnerCard

    assert PartnerCard.__module__.startswith("app.a2a.")
