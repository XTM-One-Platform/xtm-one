"""No function or class in the outbound client is written but never called.

SC-014. The failure it catches is a requirement shipped as a function with no
call site — documented, unit-tested, and executing on zero real requests while
reading as assurance. Run as a test rather than an audit, because an audit
passes once.

Deliberately crude: it counts identifier occurrences rather than resolving
calls, so it under-reports a function reached only through ``getattr``. A
precise version needs a call graph; this one catches forgetting to wire
something up.
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]

#: Every module this feature added, plus the shipped one it extended most.
SWEPT = [
    "app/a2a/client/sdk.py",
    "app/a2a/client/failures.py",
    "app/a2a/client/destinations.py",
    "app/a2a/client/verification.py",
    "app/a2a/client/credentials.py",
    "app/a2a/client/credential_state.py",
    "app/a2a/client/registration.py",
    "app/a2a/client/probe.py",
    "app/a2a/client/breaker.py",
    "app/a2a/client/delegation.py",
    "app/a2a/client/tool.py",
    "app/a2a/client/sweep.py",
    "app/integrations/providers/google_a2a.py",
    "app/api/v1/a2a_client.py",
    "app/a2a/client/callbacks.py",
    "app/api/v1/a2a_callbacks.py",
    "app/a2a/providers.py",
]

# There is deliberately no exemption list: an empty exemption set plus a
# trivially-passing guard is the unreachable-but-tested machinery this file
# exists to find.


@pytest.fixture(scope="module")
def haystack():
    """How often every identifier appears in app/, in tests/, and as a *definition*.

    ``app`` and ``tests`` are counted SEPARATELY, and that separation is the
    whole point of this fixture. Summing them makes a function called only
    from its own unit test indistinguishable from a wired one — the exact
    case this sweep exists to catch: implemented, documented, unit-tested,
    never called.

    Counted once into Counters rather than regex-scanned per name: the naive
    version re-scanned several megabytes for each of 75 names and took a
    minute, which is how a useful test becomes one people skip.
    """
    import collections

    app_uses: collections.Counter[str] = collections.Counter()
    test_uses: collections.Counter[str] = collections.Counter()
    # Definitions are counted PER ROOT, because the app count is subtracted
    # from the app count. Counting a same-named helper defined in tests/
    # against app/'s uses would hide a real call site.
    app_defs: collections.Counter[str] = collections.Counter()
    word = re.compile(r"\b\w+\b")
    definition = re.compile(r"(?:async )?def (\w+)|class (\w+)")
    for root, uses in (("app", app_uses), ("tests", test_uses)):
        for path in (BACKEND / root).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            uses.update(word.findall(text))
            if root != "app":
                continue
            for a, b in definition.findall(text):
                app_defs[a or b] += 1
    return app_uses, test_uses, app_defs


def _referenced(name: str, haystack) -> bool:
    """True when *name* is used from app/ somewhere other than its definition.

    A reference from tests/ deliberately does NOT count. A function only its
    own test calls is precisely the thing being swept for, and crediting the
    test with keeping it alive would let the sweep pass while a feature's
    main path is unreachable.
    """
    app_uses, _test_uses, app_defs = haystack
    return app_uses[name] - app_defs[name] > 0


def _test_only(name: str, haystack) -> bool:
    """True when tests/ reference *name* but app/ never does.

    Worth distinguishing in the failure message: an unreferenced function is
    someone's forgotten draft, whereas a test-only one has a passing test
    that reads as proof the feature works.
    """
    _app_uses, test_uses, _app_defs = haystack
    return test_uses[name] > 0


def _top_level(rel: str):
    return ast.parse((BACKEND / rel).read_text(encoding="utf-8")).body


def _is_route_handler(node) -> bool:
    """Whether a decorator wires this function up, making its name irrelevant.

    A ``@router.get(...)`` handler is registered by the decorator and is never
    called by name from anywhere, so a name-based sweep reads every route as
    dead. Excluded on the DECORATOR, not on the filename: a helper sitting in
    a routes module is swept like anything else, which is where an unwired one
    would actually hide.
    """
    for dec in getattr(node, "decorator_list", []):
        target = dec.func if isinstance(dec, ast.Call) else dec
        if (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and target.value.id == "router"
        ):
            return True
    return False


@pytest.mark.parametrize("rel", SWEPT)
def test_every_function_has_a_call_site(rel, haystack):
    orphans = []
    for node in _top_level(rel):
        names = []
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names = [] if _is_route_handler(node) else [node.name]
        elif isinstance(node, ast.ClassDef):
            names = [
                s.name
                for s in node.body
                if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)
                and not s.name.startswith("__")
            ]
        orphans += [n for n in names if not _referenced(n, haystack)]
    tested_but_unwired = [n for n in orphans if _test_only(n, haystack)]
    assert not orphans, (
        f"{rel} defines {orphans} with no call site in app/. "
        "Wire it up or delete it — a tested function that nothing calls reads "
        "as assurance and provides none."
        + (
            f" {tested_but_unwired} ARE referenced from tests/, which is the "
            "worse case: those have passing tests standing in for a path no "
            "run can reach."
            if tested_but_unwired
            else ""
        )
    )


@pytest.mark.parametrize("rel", SWEPT)
def test_every_class_is_referenced(rel, haystack):
    orphans = [
        node.name
        for node in _top_level(rel)
        if isinstance(node, ast.ClassDef) and not _referenced(node.name, haystack)
    ]
    assert not orphans, f"{rel} defines unreferenced class(es): {orphans}"


def test_the_sweep_covers_every_module_the_feature_added():
    # A module missing from SWEPT is a module this test silently ignores,
    # which would make it the assurance-without-substance it exists to
    # prevent. Checked against the directory rather than a list, so a new
    # module joins the sweep by existing.
    present = {
        f"app/a2a/client/{p.name}"
        for p in (BACKEND / "app/a2a/client").glob("*.py")
        if p.name != "__init__.py"
    }
    assert present <= set(SWEPT), f"not swept: {sorted(present - set(SWEPT))}"
