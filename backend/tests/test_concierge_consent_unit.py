"""Platform Concierge level 1 (#8254), without a database: the consent snapshot.
The predicate and the SQL filter against PostgreSQL: ``test_concierge_consent_postgres.py``.
"""

from __future__ import annotations

import uuid

import pytest

from app.services.health.consent import ConsentScope

# ── The consent snapshot ──────────────────────────────────────────────────

A, B, FLEET = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
F = uuid.uuid4()
ALICE, BOB = uuid.uuid4(), uuid.uuid4()


def _scope(**over) -> ConsentScope:
    base = {
        "active": True,
        "agent_ids": None,
        "flow_ids": None,
        "fleet_agent_ids": frozenset({FLEET}),
        "content_agent_ids": frozenset({A}),
        "content_flow_ids": frozenset({F}),
        "opted_out_user_ids": frozenset({BOB}),
    }
    return ConsentScope(**{**base, **over})


@pytest.mark.parametrize(
    ("kwargs", "metrics", "content"),
    [
        ({"agent_id": A}, True, True),
        ({"agent_id": A, "user_ids": [ALICE, None]}, True, True),
        ({"agent_id": A, "user_ids": [BOB]}, True, False),
        ({"agent_id": B}, True, False),
        ({"agent_id": FLEET}, False, False),
        ({"flow_id": F}, True, True),
        ({"agent_id": A, "flow_id": uuid.uuid4()}, True, False),
        ({}, False, False),
        ({"agent_id": "garbage"}, False, False),
    ],
)
def test_the_snapshot_applies_the_rule(kwargs: dict, metrics: bool, content: bool) -> None:
    scope = _scope()
    users = kwargs.pop("user_ids", ())
    assert scope.allows_metrics(**kwargs) is metrics
    assert scope.allows_content(**kwargs, user_ids=users) is content


def test_a_selected_scope_and_an_inactive_one() -> None:
    selected = _scope(agent_ids=frozenset({B}), flow_ids=frozenset())
    assert selected.allows_metrics(agent_id=B) is True
    assert selected.allows_metrics(agent_id=A) is False
    assert selected.allows_content(agent_id=A) is False
    assert selected.allows_metrics(flow_id=F) is False
    inactive = _scope(active=False)
    assert inactive.allows_metrics(agent_id=A) is False
    assert inactive.allows_content(agent_id=A) is False


def test_scope_all_reads_enrolled_targets_only(monkeypatch) -> None:
    """Platform Health S1: scope "all" is every ENROLLED target, never every agent.

    An upgrade with hundreds of agents and no opt-in supervises nothing; an
    agent an owner already opted in keeps its enrolment.
    """
    import asyncio
    from types import SimpleNamespace

    from app.services.health import consent

    async def _settings(_db):
        return SimpleNamespace(enabled=True, scope_mode="all", scope_agent_ids=[], scope_flow_ids=[])

    # Query order in load_consent_scope: fleet, enrolled agents, enrolled flows, opted-out users.
    answers = iter([[FLEET], [A], [F], [BOB]])

    class _Db:
        async def execute(self, _stmt):
            rows = next(answers)
            return SimpleNamespace(scalars=lambda: iter(rows))

    monkeypatch.setattr(consent, "_active_settings", _settings)
    scope = asyncio.run(consent.load_consent_scope(_Db()))
    assert scope.agent_ids == frozenset({A})
    assert scope.flow_ids == frozenset({F})
    assert scope.allows_metrics(agent_id=A) is True
    assert scope.allows_metrics(agent_id=B) is False, "a non-enrolled agent is never supervised"
    assert scope.allows_metrics(flow_id=uuid.uuid4()) is False



def test_every_registered_handler_runs_once_in_order_and_a_failure_propagates(monkeypatch) -> None:
    import asyncio

    from app.services.health import consent

    monkeypatch.setattr(consent, "_HANDLERS", [])
    seen: list[tuple[str, consent.Revocation]] = []

    async def first(db, revocation) -> None:
        seen.append(("first", revocation))

    async def second(db, revocation) -> None:
        seen.append(("second", revocation))

    assert consent.register_revocation_handler(first) is first
    consent.register_revocation_handler(first)
    consent.register_revocation_handler(second)
    revocation = consent.Revocation(kind="user_opt_out", user_id=ALICE)
    asyncio.run(consent.apply_revocation(None, revocation))
    assert seen == [("first", revocation), ("second", revocation)]

    async def broken(db, revocation) -> None:
        raise RuntimeError("datasets unreachable")

    consent.register_revocation_handler(broken)
    with pytest.raises(RuntimeError):
        asyncio.run(consent.apply_revocation(None, revocation))


def test_start_up_registers_every_revocation_handler_in_a_fresh_process() -> None:
    """The production registry, not a swapped one: the handler modules are
    imported lazily elsewhere, so a process that revokes first would miss them."""
    import json
    import subprocess
    import sys

    from tests.app_sources import BACKEND_DIR

    script = (
        "import json\n"
        "from app.services.health import consent\n"
        "names = lambda: sorted(h.__module__ + '.' + h.__name__ for h in consent._HANDLERS)\n"
        "before = names()\n"
        "consent.register_revocation_handlers()\n"
        "print(json.dumps([before, names()]))\n"
    )
    done = subprocess.run(  # noqa: S603 - our own interpreter, a fixed script
        [sys.executable, "-c", script], cwd=BACKEND_DIR, capture_output=True, text=True, check=True, timeout=120
    )
    out = done.stdout.strip().splitlines()[-1]

    assert json.loads(out) == [
        [],
        [
            "app.services.health.datasets.withdraw_for_revocation",
            "app.services.health.memory_history.purge_on_revocation",
        ],
    ]


@pytest.mark.parametrize(("module", "function"), [("main.py", "lifespan"), ("worker.py", "startup")])
def test_the_api_and_the_worker_register_the_handlers_at_start_up(module, function) -> None:
    import ast

    from tests.app_sources import APP_DIR, tree

    (body,) = [
        node
        for node in ast.walk(tree(APP_DIR / module))
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name == function
    ]
    called = {node.func.id for node in ast.walk(body) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    assert "register_revocation_handlers" in called
