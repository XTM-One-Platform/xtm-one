"""Static guards over the evaluation code: the run ownership rule, the metric suite
encapsulation, and evaluation tests building their databases with the shared helper.
"""

from __future__ import annotations

import ast
import pathlib
import re
from functools import cache
from pathlib import Path

import pytest

from tests.evaluation_helpers import APP_DIR

# ── The run ownership rule ──
# One helper module does not stop an inline predicate being written elsewhere, so these
# tests parse the modules and refuse a violation.


# The one module allowed to express the rule.
_HOME = "app/services/evaluation/run_ownership.py"

_FIELD = "owner_execution_id"
_MODEL = "EvaluationRun"


@cache
def _guarded_modules() -> tuple[str, ...]:
    """Every module that mentions the field, bar the one allowed to decide on it.

    Derived, not listed: a hand-written list misses a predicate written in any
    other module and the suite stays green. Mentioning the field is the
    whole criterion; a module that only reads or writes it passes the parse.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in (root / "app").rglob("*.py")
            if path.relative_to(root).as_posix() != _HOME
            and _FIELD in path.read_text(encoding="utf-8")
        )
    )

# Methods that arrange a column without deciding anything about it. Everything
# else SQLAlchemy exposes is treated as a decision.
#
# Listing the exclusions rather than the comparators makes the guard fail
# closed: a synonym such as ``.isnot`` (the legacy spelling of ``.is_not``,
# live in SQLAlchemy 2.0) is caught by default. A new arranging-only
# method from an upgrade is reported as a decision until it is added here.
_ARRANGES_ONLY = frozenset(
    {
        "asc",
        "desc",
        "distinct",
        "collate",
        "label",
        "nulls_first",
        "nullsfirst",
        "nulls_last",
        "nullslast",
    }
)


@cache
def _deciding_comparators() -> frozenset[str]:
    """Every SQLAlchemy column method that expresses a decision.

    Cached at the definition: it is constant for the process and is called for
    every candidate node of the AST walk.
    """
    from sqlalchemy.sql.operators import ColumnOperators

    return frozenset(
        name
        for name in dir(ColumnOperators)
        if not name.startswith("_") and name not in _ARRANGES_ONLY
    )


def _refers_to_model(node: ast.AST, aliases: frozenset[str]) -> bool:
    """Is *node* a reference to the mapped class, however it is spelled?

    Shared by both guards, so a spelling one of them knows is never a way past
    the other.
    """
    if isinstance(node, ast.Name):
        return node.id in aliases
    # ``evaluation.EvaluationRun`` -- the module imported rather than the class.
    # Matched on the real name only: a module attribute cannot be renamed, so
    # ``obj.Row`` is some other object's field.
    return isinstance(node, ast.Attribute) and node.attr == _MODEL


def _names_bound_to_the_model(tree: ast.AST) -> frozenset[str]:
    """Every name this module can reach ``EvaluationRun`` through.

    One collector for both guards, so an alias one of them resolves is never a
    way past the other. Covers the import spellings, direct rebinding and
    ``aliased()``, iterated to a fixpoint so a chain (``A = EvaluationRun``
    then ``B = A``) resolves too.

    Name-based, with no scope analysis: a lowercase alias shadowed by an
    instance parameter would have that instance's reads reported. That is a
    false failure, which someone investigates, on a shape nothing in ``app/``
    uses.
    """
    names = {_MODEL}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names if a.name == _MODEL)

    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    grew = True
    while grew:
        grew = False
        for node in assigns:
            value = node.value
            frozen = frozenset(names)
            # ``_refers_to_model`` rather than a Name check:
            # ``Run = evaluation.EvaluationRun`` has an ``Attribute`` value, and
            # must still register ``Run`` as an alias.
            reaches = _refers_to_model(value, frozen) or (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "aliased"
                and any(_refers_to_model(a, frozen) for a in value.args)
            )
            if not reaches:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    grew = True
    return frozenset(names)


def _predicate_uses(tree: ast.AST) -> list[tuple[int, str]]:
    """Every place ``owner_execution_id`` is used to DECIDE something.

    Reading the column — ``select(EvaluationRun.id, EvaluationRun.owner_execution_id)``
    — is fine and stays allowed; it reports, it does not decide. Writing it is
    fine too. What must not appear outside the helper is a COMPARISON: ``==``,
    ``!=``, or any comparator method under either of its spellings.
    """
    found: list[tuple[int, str]] = []

    # Names bound to the mapped class, so a lowercase alias is not a way out:
    # ``run_model = EvaluationRun`` and ``run_alias = aliased(EvaluationRun)``
    # break the uppercase convention below.
    aliases = _names_bound_to_the_model(tree)

    def is_field(node: ast.AST) -> bool:
        """The COLUMN, not the same-named attribute on a loaded row.

        ``EvaluationRun.owner_execution_id == x`` decides which rows an
        execution may act on. ``run.owner_execution_id != y`` reads a field off
        a row already in hand and decides nothing about which rows.

        Keyed on the receiver being a class by naming convention, so a second
        mapped class is covered without an edit. Names bound to the model,
        directly or through ``aliased()``, are resolved above and treated as
        the class.
        """
        if not (isinstance(node, ast.Attribute) and node.attr == _FIELD):
            return False

        def looks_like_a_class(receiver: ast.AST) -> bool:
            """Uppercase by convention -- covers a mapped class never named here.

            Applied to both spellings, so ``OtherModel.owner_execution_id`` and
            ``models.OtherModel.owner_execution_id`` are treated alike.
            """
            if isinstance(receiver, ast.Name):
                return receiver.id[:1].isupper()
            return (
                isinstance(receiver, ast.Attribute)
                and receiver.attr[:1].isupper()
            )

        return _refers_to_model(node.value, aliases) or looks_like_a_class(
            node.value
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and (
            is_field(node.left) or any(is_field(c) for c in node.comparators)
        ):
            found.append((node.lineno, "comparison"))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in _deciding_comparators()
            and is_field(node.func.value)
        ):
            found.append((node.lineno, f".{node.func.attr}()"))
    return found


@pytest.mark.parametrize("relpath", _guarded_modules())
def test_no_module_writes_its_own_ownership_predicate(relpath: str) -> None:
    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relpath).read_text(encoding="utf-8"))
    offenders = _predicate_uses(tree)
    assert offenders == [], (
        f"{relpath} decides run ownership inline at "
        f"{', '.join(f'line {ln} ({kind})' for ln, kind in offenders)}.\n\n"
        "Use app/services/evaluation/run_ownership.py instead, and state which "
        "case you are in rather than assembling the predicate:\n"
        "  runs_this_execution_may_touch(execution_id, repeating_is_harmless=...)\n"
        "  runs_owned_by_nobody()\n"
        "  runs_owned_by_another(execution_id)\n\n"
        "If none of them fits, the new case belongs IN that module with its "
        "reasoning, next to the others — which is the point. Ten predicates "
        "accumulated here because each was written where it was needed."
    )


# Statuses an execution drives. A run created in one of these is dispatchable,
# so the row must name the execution that owns it; a run created terminal --
# the import path rebuilds history -- never will have one and must not be
# required to invent it.
_ACTIVE_STATUSES = frozenset({"pending", "running"})


def _unstamped_active_constructions(tree: ast.AST) -> list[int]:
    """Lines constructing an ACTIVE ``EvaluationRun`` without stamping its owner.

    The write side of the rule. The read guard cannot see a missing
    assignment, so a trigger path that forgets the stamp would go unnoticed
    until a cleanup silently skips its runs.

    What counts as active:

    * **No ``status`` kwarg** -- active: the column defaults to ``"pending"``.
    * **A literal ``"pending"``/``"running"``** -- active.
    * **A literal terminal status** -- skipped.
    * **A computed status** -- skipped. ``import_evaluation`` builds rows through
      ``_importable_run_status(...)``, which yields only terminal statuses.

    Fail-closed residue: a construction that leaves ``status`` out and assigns
    a terminal one afterwards is reported; naming the status in the
    constructor fixes it.

    Granularity: a stamping function covers every construction in it, because
    the trigger builds runs in one loop and stamps them in another, which
    cannot be paired statically. What this catches is a path with no stamp at
    all.
    """
    # Names this module can build the model under, so ``import ... as Run`` is
    # inspected too.
    ctor_names = _names_bound_to_the_model(tree)

    def is_none(value: ast.AST | None) -> bool:
        return isinstance(value, ast.Constant) and value.value is None

    def creates_active_run(node: ast.AST) -> bool:
        if not (
            isinstance(node, ast.Call)
            and _refers_to_model(node.func, ctor_names)
        ):
            return False
        status = next(
            (k.value for k in node.keywords if k.arg == "status"), None
        )
        if status is None:
            # No ``status`` kwarg: the column defaults to ``"pending"``, so the
            # row is dispatchable and must be owned. Unlike a computed
            # status, nothing here names a status the guard declines to judge.
            return True
        return isinstance(status, ast.Constant) and status.value in _ACTIVE_STATUSES

    def stamps_inline(node: ast.Call) -> bool:
        """An owner passed to the constructor, and not an explicit ``None``.

        ``EvaluationRun(status="pending", owner_execution_id=None)`` names the
        field and still creates an unowned active row.
        """
        for keyword in node.keywords:
            if keyword.arg == _FIELD:
                return not is_none(keyword.value)
        return False

    def is_stamping_assignment(node: ast.AST) -> bool:
        """``x.owner_execution_id = <something other than None>``.

        ``run.owner_execution_id = None`` clears the owner and does not count
        as stamping the row.
        """
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            return False
        if value is None or is_none(value):
            return False
        return any(
            isinstance(t, ast.Attribute) and t.attr == _FIELD for t in targets
        )

    # Collected as "which constructions a stamping function covers", then
    # checked over the whole tree, so a module-level construction (no enclosing
    # function) is still looked at.
    covered: set[int] = set()
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if not any(is_stamping_assignment(n) for n in ast.walk(fn)):
            continue
        covered.update(
            n.lineno for n in ast.walk(fn) if creates_active_run(n)
        )

    def explicitly_unowned(node: ast.Call) -> bool:
        """``owner_execution_id=None`` written out on an active construction.

        Reported whatever else the enclosing function does: the ``covered``
        compromise does not apply to a row that says it has no owner.
        """
        return any(
            keyword.arg == _FIELD and is_none(keyword.value)
            for keyword in node.keywords
        )

    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if creates_active_run(node)
        and (
            explicitly_unowned(node)
            or (node.lineno not in covered and not stamps_inline(node))
        )
    )


@cache
def _modules_constructing_runs() -> tuple[str, ...]:
    """Every module that builds an ``EvaluationRun``.

    Derived independently of the read guard's set: a trigger path that forgets
    the stamp does not mention the field at all.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    return tuple(
        sorted(
            path.relative_to(root).as_posix()
            for path in (root / "app").rglob("*.py")
            # Keyed on the class name, not on ``EvaluationRun(``: a module
            # building it under an import alias still names it on the import
            # line.
            if _MODEL in path.read_text(encoding="utf-8")
        )
    )


@pytest.mark.parametrize("relpath", _modules_constructing_runs())
def test_an_active_run_is_never_created_without_its_owner(relpath: str) -> None:
    """An active run is always created with its owning execution stamped.

    The read rule stays true only if every write keeps it true.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / relpath).read_text(encoding="utf-8"))
    unstamped = _unstamped_active_constructions(tree)
    assert unstamped == [], (
        f"{relpath} creates a dispatchable EvaluationRun without naming the "
        f"execution that owns it, at line(s) {unstamped}.\n\n"
        "An active run with no owner is matched by no strict ownership "
        "predicate, so every cleanup skips it and it sits in progress forever. "
        "Stamp it in the same transaction as the status, or create the "
        "run terminal if nothing will drive it."
    )


def test_the_guarded_set_is_derived_and_covers_what_was_listed() -> None:
    """Guards the derivation: an empty parametrize list runs zero tests, greenly.

    A walk that finds no modules (a moved root, a renamed field) would turn the
    guard off with no failure. The three modules the rule was written for
    are asserted by name, since a count would pass on unrelated modules.
    """
    guarded = _guarded_modules()

    assert guarded, (
        "the ownership guard derived NO modules, so its parametrized test "
        f"collects nothing and passes without looking. Either {_FIELD!r} was "
        "renamed, or the walk no longer reaches app/."
    )
    for inherited in (
        "app/services/evaluation_engine.py",
        "app/services/evaluation_continuation.py",
        "app/services/orchestration_adapters/evaluation.py",
    ):
        assert inherited in guarded, (
            f"{inherited} was guarded by hand before the set was derived and "
            f"is no longer covered. Derived set: {list(guarded)}"
        )
    assert _HOME not in guarded, (
        f"{_HOME} is the module allowed to express the rule; guarding it would "
        "fail on its own predicates"
    )

    # The write guard is derived too, and an empty set is just as silent there.
    constructing = _modules_constructing_runs()
    assert constructing, (
        f"no module under app/ constructs a {_MODEL}, so the write-side guard "
        "collects nothing and passes without looking"
    )
    for builder in ("app/api/v1/evaluations.py", "app/services/autonomous.py"):
        assert builder in constructing, (
            f"{builder} builds runs and is no longer scanned for missing "
            f"ownership stamps. Derived set: {list(constructing)}"
        )


def test_the_helper_module_is_where_the_rule_actually_lives() -> None:
    """Guards the guard: the test above passes trivially if the rule moved out.

    Without this, deleting the comparisons from ``run_ownership.py`` — by
    re-inlining them somewhere unguarded, or by the module being emptied —
    leaves every assertion above green while the rule is gone.
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    tree = ast.parse((root / _HOME).read_text(encoding="utf-8"))
    assert _predicate_uses(tree), (
        f"{_HOME} no longer compares {_FIELD} at all. Either the rule moved "
        "somewhere unguarded, or these tests are now asserting nothing."
    )


def test_the_guard_covers_sqlalchemy_legacy_comparator_spellings() -> None:
    """Pins the synonyms, so "simplifying" the derivation loses coverage loudly.

    ``.isnot`` / ``.is_not`` and ``.notin_`` / ``.not_in`` are the same operator
    under two spellings, both live in SQLAlchemy 2.0 and producing identical SQL.
    """
    deciding = _deciding_comparators()
    for pair in (("is_not", "isnot"), ("not_in", "notin_")):
        for spelling in pair:
            assert spelling in deciding, (
                f"{spelling!r} is a comparator SQLAlchemy accepts, so an inline "
                "ownership predicate could be written with it and slip past "
                "the guard"
            )
    assert "asc" not in deciding, (
        "ordering a column is not a decision; flagging it would make the guard "
        "noisy enough to be worked around"
    )


# ── Metric suite encapsulation ──
# ``app/services/evaluation/metric_package/`` is the sole importer of the metric suite;
# engines, routes and serializers speak only the platform-neutral surface. The suite's own
# modules import each other by ``app.evaluation_metrics``, so the scan excludes the suite
# itself. A static scan: it fails fast without importing anything.


_ALLOWED_PREFIX = APP_DIR / "services" / "evaluation" / "metric_package"
# The metric suite. Its internal imports are self-references, not seam breaches.
_SUITE_DIR = APP_DIR / "evaluation_metrics"

# Matches ``import app.evaluation_metrics`` and ``from app.evaluation_metrics[.x]
# import`` at the start of a (possibly indented) line. Not a comment, not a longer
# identifier like ``app.evaluation_metrics_helper``, and not a mention of the
# ``evaluation_metrics`` table, which shares the name.
_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+app\.evaluation_metrics(?:\.|\s|$)"
    r"|from\s+app\.evaluation_metrics(?:\.|\s))"
)


def _imports_the_suite(path: Path) -> bool:
    text = path.read_text(encoding="utf-8")
    return any(_IMPORT_RE.match(line) for line in text.splitlines())


def _offending_files() -> list[str]:
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if _ALLOWED_PREFIX in path.parents or _SUITE_DIR in path.parents:
            continue
        if _imports_the_suite(path):
            offenders.append(str(path.relative_to(APP_DIR.parent)))
    return offenders


def test_only_metric_package_imports_the_metric_suite():
    offenders = _offending_files()
    assert not offenders, (
        "the metric suite must only be imported inside "
        "app/services/evaluation/metric_package/. Offending files: "
        + ", ".join(sorted(offenders))
    )


def test_metric_package_actually_imports_the_suite():
    # Keeps the scan from passing vacuously if the seam moves or is renamed.
    hits = [path for path in _ALLOWED_PREFIX.rglob("*.py") if _imports_the_suite(path)]
    assert hits, "expected the metric_package adapter to import the metric suite"


def test_the_suite_sits_where_the_guard_expects_it():
    # If the suite moved, the exclusion would refer to nothing and the guard
    # would keep passing.
    assert (_SUITE_DIR / "__init__.py").is_file(), (
        f"the metric suite is not at {_SUITE_DIR}; this guard's "
        "self-import exclusion no longer refers to anything"
    )


# ── Evaluation tests build their databases with the shared helper ──
# ``tests/scratch_database.py`` creates a test database, binds the app's session factories
# to it and drops it. An engine that only creates or drops databases
# (``isolation_level="AUTOCOMMIT"``) is not a session engine.


_TESTS = Path(__file__).resolve().parent

#: The evaluation suites: datasets, evaluations, their runs and the reuse of predictions.
#: A new evaluation suite named otherwise must be added here, or the guard does not see it.
_EVALUATION_TEST = re.compile(r"^test_.*(evaluation|dataset).*\.py$|^test_(run_tags|prediction_reuse)\.py$")

#: Files still building their own engine, each with its reason.
_NOT_YET_MOVED: set[str] = set()


def _session_engines(source: str) -> list[int]:
    """Lines of the ``create_async_engine`` calls in *source* that build a session engine.

    The function is recognised under its own name and under the one an import
    gives it (``from ... import create_async_engine as make_engine``).
    """
    tree = ast.parse(source)
    names = {"create_async_engine"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name == "create_async_engine" and alias.asname
    }
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = (isinstance(node.func, ast.Name) and node.func.id in names) or (
            isinstance(node.func, ast.Attribute) and node.func.attr == "create_async_engine"
        )
        if not called:
            continue
        admin = any(
            keyword.arg == "isolation_level" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "AUTOCOMMIT"
            for keyword in node.keywords
        )
        if not admin:
            lines.append(node.lineno)
    return sorted(lines)


def _evaluation_test_files() -> list[Path]:
    return sorted(path for path in _TESTS.rglob("test_*.py") if _EVALUATION_TEST.match(path.name))


def test_the_scan_finds_a_session_engine_and_leaves_an_admin_one():
    """Positive control: a scan that matches nothing would pass on any file."""
    source = (
        "from sqlalchemy.ext.asyncio import create_async_engine\n"
        "from sqlalchemy.ext.asyncio import create_async_engine as make_engine\n"
        "import sqlalchemy.ext.asyncio as sa\n"
        "session = create_async_engine(url)\n"
        "aliased = make_engine(url)\n"
        "other = sa.create_async_engine(url, json_serializer=dumps)\n"
        'admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")\n'
    )

    assert _session_engines(source) == [4, 5, 6]


def test_the_evaluation_suites_are_found():
    names = {path.name for path in _evaluation_test_files()}

    assert {"test_datasets.py", "test_evaluations.py", "test_run_tags.py", "test_prediction_reuse.py"} <= names
    # The floor sits well below the real count, so merging suites does not trip
    # it, while a pattern that stops matching them does.
    assert len(names) >= 30, "the evaluation test files are no longer matched"


def test_evaluation_tests_build_their_databases_with_the_shared_helper():
    own_engines = {
        path.name: lines
        for path in _evaluation_test_files()
        if path.name not in _NOT_YET_MOVED and (lines := _session_engines(path.read_text(encoding="utf-8")))
    }

    assert own_engines == {}, (
        "build the test database with tests/scratch_database.py (scratch_database), "
        f"not an engine of the file's own: {own_engines}"
    )


def test_a_file_waiting_to_move_still_needs_its_exception():
    """An exception whose file no longer builds its own engine is stale: drop it."""
    for name in _NOT_YET_MOVED:
        assert _session_engines((_TESTS / name).read_text(encoding="utf-8")), f"{name} builds no engine of its own any more"
