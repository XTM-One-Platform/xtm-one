"""Coverage for the per-user ("template") binding guardrails.

A per-user binding — "Resolved per user at runtime" in the agent config UI —
must resolve to the acting user's OWN integration, falling back to a genuine
company-wide instance. It must never resolve to a *colleague's* personal
integration: that silently runs the agent against their mailbox / chats / CRM
under the acting user's identity, which is exactly the incident that motivated
this guard.

Two things are pinned here:
  * ownership — another user's personal row is dropped, never preferred;
  * observability — the pick is labelled own vs company-wide fallback so a run
    that used shared credentials is distinguishable in logs and traces.
"""

from __future__ import annotations

import ast
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.services.integration_tools import (
    TEMPLATE_ORIGIN_COMPANY_FALLBACK,
    TEMPLATE_ORIGIN_OWN,
    drop_foreign_personal_integrations,
    is_company_wide_integration,
    is_foreign_personal_integration,
    pick_template_integration,
    resolve_integration_by_scope,
)

ACTING = uuid.uuid4()
COLLEAGUE = uuid.uuid4()


def _integ(
    *,
    user_id: uuid.UUID | None = None,
    is_company_managed: bool = False,
    auth_type: str = "oauth",
    scope_config: dict | None = None,
):
    # ``scope_config=None`` keeps the resolver on the unscoped auto-pick path.
    return SimpleNamespace(
        id=uuid.uuid4(),
        provider="microsoft",
        scope_config=scope_config,
        user_id=user_id,
        is_company_managed=is_company_managed,
        auth_type=auth_type,
    )


# --- classification -----------------------------------------------------------


def test_company_managed_and_platform_managed_are_company_wide():
    assert is_company_wide_integration(_integ(is_company_managed=True))
    assert is_company_wide_integration(_integ(auth_type="platform_managed"))
    assert not is_company_wide_integration(_integ(user_id=ACTING))


def test_colleague_personal_integration_is_foreign():
    """The shape behind the incident: a personal account acting as the shared
    one. Group sharing grants visibility, never permission to run as the
    owner, so it stays foreign regardless of how it was shared."""
    assert is_foreign_personal_integration(_integ(user_id=COLLEAGUE), ACTING)


def test_own_and_company_wide_are_not_foreign():
    assert not is_foreign_personal_integration(_integ(user_id=ACTING), ACTING)
    assert not is_foreign_personal_integration(
        _integ(user_id=COLLEAGUE, is_company_managed=True), ACTING
    )
    # A ``platform_managed`` row mints credentials per acting user, so its owner
    # is irrelevant — this is the clause that used to leak one user's private
    # instance to everybody else.
    assert not is_foreign_personal_integration(
        _integ(user_id=COLLEAGUE, auth_type="platform_managed"), ACTING
    )


def test_no_acting_user_makes_every_personal_row_foreign():
    """Fail closed: with no identity to resolve against, a personal row cannot
    be shown to belong to the caller."""
    assert is_foreign_personal_integration(_integ(user_id=COLLEAGUE), None)


def test_owner_less_legacy_row_is_not_foreign():
    assert not is_foreign_personal_integration(_integ(user_id=None), ACTING)


# --- dropping -----------------------------------------------------------------


def test_drop_keeps_own_and_company_wide_only():
    own = _integ(user_id=ACTING)
    company = _integ(user_id=COLLEAGUE, is_company_managed=True)
    foreign = _integ(user_id=COLLEAGUE)

    kept = drop_foreign_personal_integrations(
        [foreign, own, company], ACTING, provider="microsoft"
    )
    assert kept == [own, company]


# --- picking ------------------------------------------------------------------


def test_pick_prefers_own_over_company_managed():
    company = _integ(user_id=COLLEAGUE, is_company_managed=True)
    own = _integ(user_id=ACTING)

    picked, origin = pick_template_integration([company, own], ACTING)
    assert picked is own
    assert origin == TEMPLATE_ORIGIN_OWN


def test_pick_falls_back_to_company_managed_and_labels_it():
    """The user has no personal instance — the company-wide one stands in, and
    the origin says so instead of being indistinguishable from a personal one."""
    company = _integ(user_id=COLLEAGUE, is_company_managed=True)

    picked, origin = pick_template_integration([company], ACTING)
    assert picked is company
    assert origin == TEMPLATE_ORIGIN_COMPANY_FALLBACK


def test_pick_never_returns_a_colleagues_personal_integration():
    """The regression: with only a colleague's personal instance connected,
    per-user resolution must resolve to NOTHING rather than their account."""
    foreign = _integ(user_id=COLLEAGUE)

    picked, origin = pick_template_integration([foreign], ACTING)
    assert picked is None
    assert origin is None


def test_pick_prefers_company_managed_over_a_colleagues_personal_row():
    """Before the guard, the 'prefer a private connection' tie-break selected
    the colleague's personal row *ahead* of the real company-managed instance."""
    foreign = _integ(user_id=COLLEAGUE)
    company = _integ(user_id=COLLEAGUE, is_company_managed=True)

    picked, _ = pick_template_integration([foreign, company], ACTING)
    assert picked is company


def test_pick_ignores_a_foreign_platform_managed_private_row():
    """``platform_managed`` was an unconditional eligibility clause: another
    user's private row of that auth type was both eligible and preferred."""
    foreign_platform = _integ(user_id=COLLEAGUE, auth_type="platform_managed")
    company = _integ(user_id=COLLEAGUE, is_company_managed=True, auth_type="api_key")

    picked, origin = pick_template_integration([foreign_platform, company], ACTING)
    # Both are company-wide, so the earliest candidate wins — but neither is
    # treated as the acting user's own connection.
    assert picked is foreign_platform
    assert origin == TEMPLATE_ORIGIN_COMPANY_FALLBACK


# --- scope-aware resolver -----------------------------------------------------


@pytest.mark.asyncio
async def test_scope_resolver_drops_foreign_personal_candidates():
    foreign = _integ(user_id=COLLEAGUE)
    own = _integ(user_id=ACTING)

    result = await resolve_integration_by_scope(
        None, "microsoft", [foreign, own], None, acting_user_id=ACTING
    )
    assert result is own


@pytest.mark.asyncio
async def test_scope_resolver_returns_none_when_only_foreign_candidates():
    """A single candidate used to short-circuit ahead of every ownership check,
    so the lone colleague-owned instance was returned as-is."""
    result = await resolve_integration_by_scope(
        None, "microsoft", [_integ(user_id=COLLEAGUE)], None, acting_user_id=ACTING
    )
    assert result is None


@pytest.mark.asyncio
async def test_scope_resolver_requires_the_acting_user_keyword():
    """Omitting the identity used to fall back to identity-blind resolution,
    which binds whatever row comes first - a colleague's account included.
    The omission is now a TypeError, not a silent fallback."""
    with pytest.raises(TypeError, match="acting_user_id"):
        await resolve_integration_by_scope(  # type: ignore[call-arg]
            None, "microsoft", [_integ(user_id=COLLEAGUE)], None
        )


@pytest.mark.asyncio
async def test_scope_resolver_without_identity_is_company_wide_only():
    """An explicit ``None`` means no identity: company-wide rows only."""
    foreign = _integ(user_id=COLLEAGUE)
    company = _integ(user_id=COLLEAGUE, is_company_managed=True)

    assert (
        await resolve_integration_by_scope(
            None, "microsoft", [foreign], None, acting_user_id=None
        )
        is None
    )
    assert (
        await resolve_integration_by_scope(
            None, "microsoft", [foreign, company], None, acting_user_id=None
        )
        is company
    )


@pytest.mark.asyncio
async def test_scope_resolver_without_identity_prefers_no_private_row():
    """With no identity no row is the caller's own. A colleague's private
    ``platform_managed`` row survives the drop (it is company-wide), but the
    "prefer a private connection" tie-break must not pick it ahead of the
    company-managed instance listed first."""
    company = _integ(user_id=COLLEAGUE, is_company_managed=True, auth_type="api_key")
    foreign_platform = _integ(user_id=COLLEAGUE, auth_type="platform_managed")

    result = await resolve_integration_by_scope(
        None, "microsoft", [company, foreign_platform], None, acting_user_id=None
    )
    assert result is company

    picked, origin = pick_template_integration([company, foreign_platform], None)
    assert picked is company
    assert origin == TEMPLATE_ORIGIN_COMPANY_FALLBACK


def test_every_scope_resolver_caller_passes_the_acting_user():
    """Source-level guard: each production call names ``acting_user_id``.

    The keyword has no default, so a call that forgets it only fails when that
    path runs. Parsed, not pattern-matched, so every call form is covered:
    awaited or not, on one line or several, under an import alias or as a
    module attribute. A ``**kwargs`` splat does not count - the identity must
    be visible at the call site.
    """
    resolver = resolve_integration_by_scope.__name__
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    calls: list[str] = []
    for path in app_dir.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if resolver not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        names = {resolver} | {
            alias.asname
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if alias.name == resolver and alias.asname
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (isinstance(func, ast.Name) and func.id in names) or (
                isinstance(func, ast.Attribute) and func.attr == resolver
            ):
                where = f"{path.relative_to(app_dir.parent)}:{node.lineno}"
                calls.append(where)
                assert any(kw.arg == "acting_user_id" for kw in node.keywords), where
    assert len(calls) >= 2, calls
