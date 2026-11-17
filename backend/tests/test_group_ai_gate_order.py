"""A route that asks an AI gate has authenticated its caller by then.

``deps.refuse_if_group_ai_disabled`` reads the person ``get_current_user_ungated``
bound for the request. FastAPI resolves dependencies in declaration order, so a
gate declared BEFORE the user dependency sees nobody and would let a member of an
AI-disabled group through, silently. This walks every route of the real app: a
route carrying both must resolve the user first. A route with a gate and no user
dependency at all is public by design (widget, webhooks) and listed here, so a
new one is a decision, not an accident.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from app.api import deps
from app.main import application as app

GATES = {deps.require_ai_enabled, deps.require_agentic_ai, deps.require_chat_enabled}
AUTHENTICATORS = {deps.get_current_user_ungated}

#: Routes asking an AI gate with no authenticated user: the platform switch alone
#: decides for them. Each answers for someone with no account.
PUBLIC_ROUTES: set[tuple[str, str]] = set()


def _flatten(dependant) -> list:
    """The callables of a dependant, in the order FastAPI resolves them."""
    order: list = []
    for sub in dependant.dependencies:
        order.extend(_flatten(sub))
        order.append(sub.call)
    return order


def _routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            yield route, _flatten(route.dependant)


def test_a_gate_is_never_resolved_before_the_user():
    early = []
    for route, order in _routes():
        gates = [i for i, call in enumerate(order) if call in GATES]
        users = [i for i, call in enumerate(order) if call in AUTHENTICATORS]
        if gates and users and min(gates) < min(users):
            early.append(f"{sorted(route.methods)} {route.path}")
    assert early == []


def test_a_gated_route_without_a_user_is_a_known_public_one():
    unauthenticated = {
        (",".join(sorted(route.methods)), route.path)
        for route, order in _routes()
        if any(call in GATES for call in order) and not any(call in AUTHENTICATORS for call in order)
    }
    assert unauthenticated == PUBLIC_ROUTES, sorted(unauthenticated - PUBLIC_ROUTES)
