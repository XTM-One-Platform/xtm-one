"""A run that continues another is never more visible than it.

A follow-up (``POST /assignments/runs/{id}/followup``) carries the result of
the run it follows up, and a chained run the result of the run before it. Both
used to take their visibility from the assignment alone, so a private run of
an assignment whose agent became company-managed since was followed up into a
company-managed run every user could open, holding the private run's result.

``continuing_run_is_company_managed`` (``helpers.py``) is the one rule for
every run that continues another - delegation children, follow-ups, chained
runs, retries: the source run's flag, never more than the new run's own
settings allow. Its other half, ``can_open_continued_run``, keeps a private
continuation from being owned by someone who cannot open its source (a chain
into an assignment run as another user). These tests pin the rules, the
trigger funnel that applies them, and every call site that must go through
them. The route and the chain funnel against PostgreSQL, as non-admins, are
``test_continuing_run_visibility_postgres.py``.
"""

from __future__ import annotations

import ast
import functools
import inspect
import textwrap
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.v1 import helpers
from app.api.v1.helpers import (
    ContinuedRunNotOpenableError,
    can_open_continued_run,
    continuing_run_is_company_managed,
    resolve_child_run_is_company_managed,
)
from app.services import assignment_engine

APP_ROOT = Path(__file__).resolve().parents[1] / "app"
RULE_HELPERS = {"continuing_run_is_company_managed", "resolve_child_run_is_company_managed"}


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source", "ceiling", "expected"),
    [
        # The usual case: the run and its settings agree, the source is mirrored.
        (True, True, True),
        (False, False, False),
        # The widened case: a private source stays private.
        (False, True, False),
        # Its own settings are narrower now: never wider than them either.
        (True, False, False),
    ],
)
def test_the_source_flag_capped_by_the_ceiling(source, ceiling, expected):
    assert continuing_run_is_company_managed(source, fallback=not expected, ceiling=ceiling) is expected


def test_an_unknown_source_falls_back_under_the_same_ceiling():
    assert continuing_run_is_company_managed(None, fallback=True, ceiling=True) is True
    assert continuing_run_is_company_managed(None, fallback=False, ceiling=True) is False
    assert continuing_run_is_company_managed(None, fallback=True, ceiling=False) is False


def test_a_delegation_child_mirrors_its_parent_both_ways():
    """No cap of its own (``ceiling=True``): the parent's flag, whatever the
    fallback says."""
    assert continuing_run_is_company_managed(True, fallback=False, ceiling=True) is True
    assert continuing_run_is_company_managed(False, fallback=True, ceiling=True) is False


def test_every_caller_states_its_ceiling():
    """No default a new caller could inherit without thinking about its cap."""
    with pytest.raises(TypeError):
        continuing_run_is_company_managed(True, fallback=False)


class _FlagSession:
    def __init__(self, flag):
        self._flag = flag

    async def execute(self, *_args, **_kwargs):
        return SimpleNamespace(scalar_one_or_none=lambda: self._flag)


async def test_the_delegation_loader_applies_the_rule(monkeypatch):
    """``resolve_child_run_is_company_managed`` reads the parent's flag and
    hands it to the rule instead of spelling it again."""
    seen: list[tuple] = []

    def _rule(source, *, fallback, ceiling):
        seen.append((source, fallback, ceiling))
        return "the rule's answer"

    monkeypatch.setattr(helpers, "continuing_run_is_company_managed", _rule)
    parent = str(uuid.uuid4())
    assert await resolve_child_run_is_company_managed(_FlagSession(True), parent, fallback=False) == "the rule's answer"
    assert await resolve_child_run_is_company_managed(_FlagSession(None), None, fallback=True) == "the rule's answer"
    assert seen == [(True, False, True), (None, True, True)]


# ---------------------------------------------------------------------------
# The trigger funnel: ``trigger_assignment(continues_run=...)``
# ---------------------------------------------------------------------------


class _CaptureDB:
    """Captures the run ``trigger_assignment`` adds; nothing is persisted."""

    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def execute(self, *_args, **_kwargs):
        # An active user for the identity check, no row for anything else.
        return SimpleNamespace(
            scalar_one_or_none=lambda: None,
            one_or_none=lambda: SimpleNamespace(role="user", is_active=True),
        )

    async def flush(self):
        return None

    async def commit(self):
        return None


@pytest.fixture
def _funnel(monkeypatch):
    async def _no_refusal(*_args, **_kwargs):
        return None

    async def _scheduled(*_args, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", _no_refusal)
    monkeypatch.setattr(assignment_engine, "refuse_if_owner_deactivated", _no_refusal)
    monkeypatch.setattr(assignment_engine, "_schedule_run_execution", _scheduled)


def _assignment(*, company: bool, hide_runs: bool | None = None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        name="Weekly report",
        enabled=True,
        user_id=uuid.uuid4(),
        run_as_user_id=None,
        is_company_managed=company,
        hide_runs=hide_runs,
    )


def _agent():
    return SimpleNamespace(enabled=True, name="Reporter", hide_runs=False)


async def _created_run(assignment, *, source_company: bool | None, clicker=None):
    db = _CaptureDB()
    source = (
        None
        if source_company is None
        # Run by the identity the new run takes, who can open it.
        else SimpleNamespace(id=uuid.uuid4(), is_company_managed=source_company, user_id=clicker or assignment.user_id)
    )
    await assignment_engine.trigger_assignment(
        assignment,
        _agent(),
        payload={},
        db=db,
        triggered_by_user_id=clicker,
        continues_run=source,
    )
    (run,) = db.added
    return run


@pytest.mark.usefixtures("_funnel")
async def test_a_private_run_is_continued_privately_on_a_company_managed_assignment():
    clicker = uuid.uuid4()
    run = await _created_run(_assignment(company=True), source_company=False, clicker=clicker)
    assert run.is_company_managed is False
    # Still the clicker's run: visibility never rewrites the execution identity.
    assert run.user_id == clicker


@pytest.mark.usefixtures("_funnel")
@pytest.mark.parametrize(
    ("assignment", "source_company", "expected"),
    [
        # Unchanged: the run and the assignment agree.
        (_assignment(company=True), True, True),
        (_assignment(company=False), False, False),
        # Unchanged: the assignment is narrower than the run now (made
        # private, or its runs hidden since) - still its visibility, as before.
        (_assignment(company=False), True, False),
        (_assignment(company=True, hide_runs=True), True, False),
        # A run that continues nothing is untouched.
        (_assignment(company=True), None, True),
        (_assignment(company=False), None, False),
    ],
)
async def test_everything_else_is_created_as_before(assignment, source_company, expected):
    run = await _created_run(assignment, source_company=source_company)
    assert run.is_company_managed is expected


@pytest.mark.usefixtures("_funnel")
async def test_the_funnel_asks_the_rule(monkeypatch):
    """Inlining the rule in ``trigger_assignment`` fails here: the run takes
    whatever the helper answers, from the source's flag and the ceiling."""
    seen: list[tuple] = []

    def _rule(source, *, fallback, ceiling):
        seen.append((source, fallback, ceiling))
        return not ceiling

    monkeypatch.setattr(assignment_engine, "continuing_run_is_company_managed", _rule)
    run = await _created_run(_assignment(company=True), source_company=True)
    assert run.is_company_managed is False
    assert seen == [(True, False, True)]


@pytest.mark.usefixtures("_funnel")
async def test_the_funnel_refuses_an_owner_who_cannot_open_the_run_before_writing(monkeypatch):
    """Asked with the identity the run would take, before the overlap lock."""
    asked: list[tuple] = []
    locked: list = []

    async def _cannot_open(db, source, user_id):
        asked.append((source, user_id))
        return False

    async def _lock(*args):
        locked.append(args)

    monkeypatch.setattr(assignment_engine, "can_open_continued_run", _cannot_open)
    monkeypatch.setattr(assignment_engine, "_acquire_trigger_overlap_lock", _lock)
    assignment = _assignment(company=False)
    assignment.run_as_user_id = uuid.uuid4()
    source = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=uuid.uuid4())
    db = _CaptureDB()
    with pytest.raises(ContinuedRunNotOpenableError) as refused:
        await assignment_engine.trigger_assignment(
            assignment, _agent(), payload={}, db=db, skip_if_active=True, continues_run=source
        )
    # A request path answers the reason, with a code a client can branch on.
    assert refused.value.status_code == 403
    assert refused.value.detail["code"] == "continued_run_not_openable"
    assert "cannot open the run it continues" in refused.value.detail["message"]
    assert asked == [(source, assignment.run_as_user_id)]
    assert locked == [] and db.added == []


class _RoleSession:
    """Answers the user lookup (``None`` for no row), then the openable-run
    lookup."""

    def __init__(self, role, openable, *, active=True):
        user = None if role is None else SimpleNamespace(role=role, is_active=active)
        self._answers = [
            SimpleNamespace(one_or_none=lambda: user),
            SimpleNamespace(all=lambda: openable),
        ]
        self.queries = 0

    async def execute(self, *_args, **_kwargs):
        self.queries += 1
        return self._answers.pop(0)


async def test_the_owner_check_reads_the_run_as_its_owner_would():
    source = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=uuid.uuid4())
    # The run's own user opens it, once their account is read active.
    own = _RoleSession("user", [])
    assert await can_open_continued_run(own, source, source.user_id) is True
    assert own.queries == 1
    # No identity at all is no verifiable principal, even for an ownerless run.
    assert await can_open_continued_run(_RoleSession("user", []), source, None) is False
    ownerless = SimpleNamespace(id=uuid.uuid4(), is_company_managed=False, user_id=None)
    nothing_read = _RoleSession("user", [])
    assert await can_open_continued_run(nothing_read, ownerless, None) is False
    assert nothing_read.queries == 0
    # Anyone else: the rule a run names its parent by.
    assert await can_open_continued_run(_RoleSession("user", []), source, uuid.uuid4()) is False
    shown = _RoleSession("admin", [(source.id, uuid.uuid4())])
    assert await can_open_continued_run(shown, source, uuid.uuid4()) is True
    assert shown.queries == 2


def test_the_acting_identity_is_the_engines_own_resolution():
    """An ownerless run acts as its agent's owner: that is who is checked."""
    user, owner = uuid.uuid4(), uuid.uuid4()
    agent = SimpleNamespace(user_id=owner)
    assert assignment_engine._acting_identity(user, agent) == user
    assert assignment_engine._acting_identity(None, agent) == owner
    assert assignment_engine._acting_identity(None, None) is None
    engine = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "effective_user_id = _acting_identity(run.user_id, agent)" in engine


@pytest.mark.parametrize("role", ["user", "admin"])
async def test_a_deactivated_or_missing_identity_opens_nothing(role):
    """Authentication refuses them, so neither the run's own user nor an admin
    opens the run once deactivated, and a user row that is gone opens nothing."""
    source = SimpleNamespace(id=uuid.uuid4(), is_company_managed=True, user_id=uuid.uuid4())
    inactive = _RoleSession(role, [(source.id, uuid.uuid4())], active=False)
    assert await can_open_continued_run(inactive, source, source.user_id) is False
    assert inactive.queries == 1
    assert await can_open_continued_run(_RoleSession(None, []), source, source.user_id) is False


# ---------------------------------------------------------------------------
# Every run that continues another goes through the rule
# ---------------------------------------------------------------------------


def _function_node(func) -> ast.AST:
    return ast.parse(textwrap.dedent(inspect.getsource(func))).body[0]


def _calls_to(tree: ast.AST, *names: str) -> list[ast.Call]:
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in names:
                found.append(node)
    return found


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    return next((kw.value for kw in call.keywords if kw.arg == name), None)


def test_the_follow_up_passes_the_run_it_follows_up():
    from app.api.v1.assignments import followup_run

    (call,) = _calls_to(_function_node(followup_run), "do_trigger")
    value = _keyword(call, "continues_run")
    assert isinstance(value, ast.Name) and value.id == "parent_run"


def test_a_chained_run_passes_the_run_whose_result_it_carries():
    tree = _function_node(assignment_engine.execute_assignment_run)
    (call,) = _calls_to(tree, "trigger_assignment")
    assert _keyword(call, "payload").id == "chain_payload"
    value = _keyword(call, "continues_run")
    assert isinstance(value, ast.Name) and value.id == "run"


def test_the_funnel_caps_the_visibility_with_the_rule():
    tree = _function_node(assignment_engine.trigger_assignment)
    (call,) = _calls_to(tree, "continuing_run_is_company_managed")
    ceiling = _keyword(call, "ceiling")
    assert isinstance(ceiling, ast.Name) and ceiling.id == "run_visible_to_all"
    assert ast.unparse(call.args[0]) == "continues_run.is_company_managed"


def test_the_funnel_asks_about_the_identity_the_run_takes():
    tree = _function_node(assignment_engine.trigger_assignment)
    stored = "_execution_identity(assignment, triggered_by_user_id, agent)"
    (call,) = _calls_to(tree, "can_open_continued_run")
    # Checked as it will act: the stored user, else the agent's owner.
    assert [ast.unparse(arg) for arg in call.args] == ["db", "continues_run", f"_acting_identity({stored}, agent)"]
    (run,) = _calls_to(tree, "AssignmentRun")
    assert ast.unparse(_keyword(run, "user_id")) == stored


def test_a_refused_chain_is_reported_never_dropped_silently():
    """The chain's refusal goes to the owner (``_report_refused_continuation``),
    as a policy decision - never the ERROR of a broken chain."""
    tree = _function_node(assignment_engine.execute_assignment_run)
    (handler,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "ContinuedRunNotOpenableError"
    ]
    (report,) = _calls_to(handler, "_report_refused_continuation")
    assert ast.literal_eval(_keyword(report, "kind")) == "chain"
    assert not _calls_to(handler, "error", "exception")


def test_the_refusal_report_is_logged_audited_and_notified_to_one_user():
    reporter = _function_node(assignment_engine._report_refused_continuation)
    assert _calls_to(reporter, "warning")
    assert _calls_to(reporter, "log_system_audit")
    (notice,) = _calls_to(reporter, "_emit_assignment_notice")
    # Recipient-only, whatever the assignment: never the company channel.
    assert ast.unparse(_keyword(notice, "only_to")) == "assignment.user_id"
    assert _keyword(notice, "company_recipient_id") is None
    # Nothing it writes or logs names the run.
    assert "run" not in {arg.arg for arg in reporter.args.kwonlyargs}


def test_a_retry_asks_whether_the_failed_runs_user_may_still_open_it():
    """A retry is built from the failed run directly, outside the funnel: it
    asks the same helper with the identity it takes, reports a refusal like a
    chain and is not built."""
    tree = _function_node(assignment_engine.execute_assignment_run)
    (retry,) = [call for call in _calls_to(tree, "AssignmentRun") if _keyword(call, "retry_count") is not None]
    assert ast.unparse(_keyword(retry, "user_id")) == "run.user_id"
    (identity,) = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and ast.unparse(node.targets[0]) == "retry_identity"
        and ast.unparse(node.value) == "_acting_identity(run.user_id, agent)"
    ]
    gates = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and any(
            [ast.unparse(arg) for arg in call.args] == ["db", "run", "retry_identity"]
            for call in _calls_to(node.test, "can_open_continued_run")
        )
    ]
    (gate,) = gates
    assert identity.lineno < gate.lineno < retry.lineno
    (report,) = _calls_to(gate, "_report_refused_continuation")
    assert ast.literal_eval(_keyword(report, "kind")) == "retry"
    assert "max_retries = 0" in ast.unparse(gate)


def test_every_continuation_started_after_its_source_ended_checks_its_identity():
    """A run that starts once its source is over - a retry built directly, a
    follow-up or a chain through the funnel - asks ``can_open_continued_run``
    before it is built. Only a live delegation child (it names its running
    parent) is exempt: it runs inside its parent, as the parent's user."""
    unchecked = [
        where
        for where, (function, call) in _continuing_run_constructions().items()
        if _keyword(call, "parent_run_id") is None
        and not any(check.lineno < call.lineno for check in _calls_to(function, "can_open_continued_run"))
    ]
    assert unchecked == []
    funnel = _function_node(assignment_engine.trigger_assignment)
    (check,) = _calls_to(funnel, "can_open_continued_run")
    (run,) = _calls_to(funnel, "AssignmentRun")
    assert check.lineno < run.lineno


def _derived_from_rule(function: ast.AST, value: ast.expr | None) -> bool:
    """*value* is a call to the rule, or a name assigned from one in *function*."""
    if isinstance(value, ast.Await):
        value = value.value
    if isinstance(value, ast.Call):
        return value in _calls_to(value, *RULE_HELPERS)
    if not isinstance(value, ast.Name):
        return False
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        else:
            continue
        names = {t.id for t in targets if isinstance(t, ast.Name)}
        if value.id in names and _derived_from_rule(function, node.value):
            return True
    return False


# How a continuation is recognised. A run continues another when it carries
# that run's id or result: an ``AssignmentRun(...)`` naming a ``parent_run_id``
# (delegation children) or counting a ``retry_count`` (retries), or whose
# ``trigger_payload`` / ``trigger_context`` - and, for a trigger, whose
# ``payload`` / ``trigger_context`` - holds one of ``CONTINUATION_KEYS``
# (follow-ups, chains). Only the dicts that reach the run count, never any
# other dict of the function. A new continuation shape that carries its
# source some other way must add its marker here.
CONTINUATION_KEYS = frozenset({"parent_run_id", "parent_result", "previous_result", "chained_from_run_id"})


def _dicts_reaching(function: ast.AST, value: ast.expr | None) -> list[ast.Dict]:
    """The dict literals *value* is: itself, or what its name is assigned from
    in *function* (``{**base, ...}`` spreads included)."""
    if isinstance(value, ast.Dict):
        found = [value]
        for key, spread in zip(value.keys, value.values, strict=True):
            if key is None:
                found += _dicts_reaching(function, spread)
        return found
    if not isinstance(value, ast.Name):
        return []
    found = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == value.id for t in node.targets):
            found += _dicts_reaching(function, node.value)
    return found


def _carries_a_run(function: ast.AST, call: ast.Call, *fields: str) -> bool:
    return any(
        isinstance(key, ast.Constant) and key.value in CONTINUATION_KEYS
        for field in fields
        for literal in _dicts_reaching(function, _keyword(call, field))
        for key in literal.keys
    )


def _functions(*called: str) -> Iterator[tuple[Path, ast.AST]]:
    """The functions of the app files whose text names one of *called*.

    A call to a name cannot sit in a file that never spells it, so only those
    files are parsed (about a hundred of the app's ~1,800), one at a time: the
    whole app's syntax trees held at once weigh about 17 times its source and
    got the CI runner killed with two xdist workers in this file.
    """
    for path in sorted(APP_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if not any(name in source for name in called):
            continue
        # Breadth-first: an inner function comes after the one holding it.
        for function in ast.walk(ast.parse(source)):
            if isinstance(function, ast.FunctionDef | ast.AsyncFunctionDef):
                yield path, function


@functools.cache
def _continuing_run_constructions() -> dict[str, tuple[ast.AST, ast.Call]]:
    """Every ``AssignmentRun(...)`` in the app that continues another run (see
    ``CONTINUATION_KEYS``). Keyed by location, with the innermost function
    holding it."""
    found = {}
    for path, function in _functions("AssignmentRun"):
        for call in _calls_to(function, "AssignmentRun"):
            parent = _keyword(call, "parent_run_id")
            names_parent = parent is not None and not (isinstance(parent, ast.Constant) and parent.value is None)
            if (
                names_parent
                or _keyword(call, "retry_count") is not None
                or _carries_a_run(function, call, "trigger_payload", "trigger_context")
            ):
                found[f"{path.relative_to(APP_ROOT)}:{call.lineno}"] = (function, call)
    return found


def test_every_run_that_continues_another_takes_its_visibility_from_the_rule():
    constructions = _continuing_run_constructions()
    # The delegation children (sub-agent task, consult, transfer) and the retry.
    assert len(constructions) >= 4, sorted(constructions)
    bypassing = [
        where
        for where, (function, call) in constructions.items()
        if not _derived_from_rule(function, _keyword(call, "is_company_managed"))
    ]
    assert bypassing == []


def test_a_run_built_directly_continues_as_its_source_principal():
    """Outside the funnel, a continuing run never takes an assignment's
    identity: a delegation child runs as its parent's user, a retry as the
    failed run's. An identity read off an assignment belongs in
    ``trigger_assignment``, where ``can_open_continued_run`` checks it."""
    offenders = [
        where
        for where, (_function, call) in _continuing_run_constructions().items()
        if any(
            marker in ast.unparse(_keyword(call, "user_id") or ast.Constant(None))
            for marker in ("run_as_user_id", "assignment.user_id", "agent.user_id")
        )
    ]
    assert offenders == []


def test_every_trigger_carrying_another_run_names_that_run():
    """A trigger whose payload or context carries another run's result or id
    is a run that continues it: it says so to the funnel, which caps its
    visibility and checks its identity."""
    offenders = [
        f"{path.relative_to(APP_ROOT)}:{call.lineno}"
        for path, function in _functions("trigger_assignment", "do_trigger")
        for call in _calls_to(function, "trigger_assignment", "do_trigger")
        if _carries_a_run(function, call, "payload", "trigger_context") and _keyword(call, "continues_run") is None
    ]
    assert offenders == []


def test_the_trigger_detector_sees_both_continuations_it_knows():
    """The detector is not blind: the follow-up and the chain are found."""
    from app.api.v1.assignments import followup_run

    followup = _function_node(followup_run)
    (call,) = _calls_to(followup, "do_trigger")
    assert _carries_a_run(followup, call, "payload", "trigger_context")
    chain = _function_node(assignment_engine.execute_assignment_run)
    (call,) = _calls_to(chain, "trigger_assignment")
    assert _carries_a_run(chain, call, "payload", "trigger_context")
    # An unrelated dict of the same function carrying the keys does not count.
    unrelated = ast.parse("def f():\n    log = {'parent_run_id': 1}\n    trigger_assignment(a, b, payload={})").body[0]
    (call,) = _calls_to(unrelated, "trigger_assignment")
    assert not _carries_a_run(unrelated, call, "payload", "trigger_context")
