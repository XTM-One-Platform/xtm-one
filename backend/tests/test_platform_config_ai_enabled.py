"""``ai_enabled`` on the unauthenticated platform config (US3, T025).

FR-011: the state must be discoverable before authentication so the sign-in
screen can state it, and clients must gate on it rather than on caught errors.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.api.v1 import platform as platform_api

BACKEND = Path(__file__).resolve().parent.parent


def test_every_response_site_sets_ai_enabled():
    """The field has a fail-closed default, so a forgotten branch is silent.

    ``PlatformConfigResponse`` is built at two sites — the XTM One branch and
    the Copilot one — and the licence-wall defect was exactly one branch
    disagreeing with the other. Asserted structurally: a third site added later
    must set it too.
    """
    source = (BACKEND / "app" / "api" / "v1" / "platform.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PlatformConfigResponse"
    ]
    assert len(sites) >= 2, f"expected both response branches, found {len(sites)}"

    missing = [
        site.lineno
        for site in sites
        if not any(kw.arg == "ai_enabled" for kw in site.keywords)
    ]
    assert not missing, (
        f"these PlatformConfigResponse sites omit ai_enabled: {missing}. The "
        "field defaults to False, so the omission reports AI as disabled on a "
        "platform where it is on."
    )


def test_ai_enabled_is_independent_of_the_licence_flag():
    """FR-011 / the contract: neither flag implies the other.

    ``agentic_enabled`` answers "does the licence permit AI",
    ``ai_enabled`` answers "is it switched on here". A licensed platform
    deliberately running without AI is the case clients must handle, so the
    resolution must not read one from the other.
    """
    source = inspect.getsource(platform_api.get_platform_config)
    # ``_for_display`` rather than the gate: a failed read must report the last
    # known value here, not "off". Gating still fails closed.
    assert "ai_enabled = await ai_enabled_for_display(db)" in source, (
        "ai_enabled must come from the AI state, not be derived from the licence"
    )
    # The CODE between the docstring and the mode branch - the docstring
    # legitimately describes both fields side by side.
    body = source[source.index('"""', source.index('"""') + 3) + 3 :]
    resolution = body[: body.index("is_xtm = settings.is_xtm_one")]
    assert "agentic" not in resolution


@pytest.mark.asyncio
async def test_the_payload_reports_ai_disabled_when_the_state_says_so(monkeypatch):
    """Behavioural: an unreadable or disabled state must surface as False.

    Pinned because the fail-closed default makes the wrong answer look like a
    valid one — the payload has to carry the *resolved* state, not the default.
    """
    from app.services import ai_state as ai_state_mod

    assert "ai_enabled" in platform_api.PlatformConfigResponse.model_fields
    assert (
        platform_api.PlatformConfigResponse.model_fields["ai_enabled"].default is False
    ), "the default must fail closed"

    async def _disabled(_db):
        return False

    monkeypatch.setattr(ai_state_mod, "is_ai_enabled", _disabled)
    assert await ai_state_mod.is_ai_enabled(object()) is False


def test_chat_enabled_goes_through_the_one_gate():
    """The payload must not re-derive the chat rule.

    Both response branches computed `chat_enabled` themselves — one from the
    deployment tier, one from the licence — so the AI short-circuit added to
    `is_chat_enabled` never reached the field the frontend gates on: the
    composer stayed live and the WebSocket refused every send. Asserted on the
    source because the two constructions are what drift.
    """
    source = inspect.getsource(platform_api.get_platform_config)
    # Both branches hand the licence answer they just resolved INTO the gate
    # (``licensed=``) rather than re-deriving the chat rule from it.
    assert source.count("chat_enabled = await is_chat_enabled(db, licensed=") == 2
    # Exactly one derivation per branch, passed to both sites.
    assert source.count("chat_enabled=chat_enabled") == 2
    assert "chat_enabled=licensed" not in source
    assert "chat_enabled=edition" not in source
    assert "chat_enabled=license_ok" not in source


def test_the_terms_of_use_resolvers_ask_the_licence_not_the_ai_state():
    """Turning AI off must not waive Terms of Use.

    `is_agentic_ai_enabled` is a refusal for most callers, but these use it
    as an *inclusion* test — false means "no terms to accept" — so the AI
    short-circuit inside it recorded `accepted=True` for users who were never
    shown the terms. They ask `is_agentic_ai_licensed` instead.
    """
    sites = [
        BACKEND / "app" / "api" / "v1" / "auth.py",
        # The per-request gate, and the one with the widest blast radius: this
        # branch *skips* the gate, so AI off would stop asking everyone.
        BACKEND / "app" / "api" / "deps.py",
    ]
    for site in sites:
        text = site.read_text(encoding="utf-8")
        assert "is_agentic_ai_licensed" in text, site.name

    from app.api.v1.platform import is_agentic_ai_licensed

    licensed = inspect.getsource(is_agentic_ai_licensed)
    assert "is_ai_enabled" not in licensed, (
        "is_agentic_ai_licensed must answer the licence question only; folding "
        "the AI state back in reintroduces the waiver"
    )


def test_a_database_blip_does_not_report_the_platform_as_ai_disabled():
    """The gate fails closed; the *report* must not.

    ``/platform/config`` is unauthenticated and polled on every page load, so
    reporting "off" for one transient error made every client render the
    AI-disabled landing, the badge and locked navigation on a healthy platform.
    The last successfully resolved value is reported instead, and only a
    platform that has never resolved one falls back to enabled.
    """
    from app.services import ai_state

    source = inspect.getsource(ai_state.ai_enabled_for_display)
    assert "_last_known" in source
    assert "return True if _last_known is None else _last_known" in source

    # And the gate is untouched — it still answers False when unreadable.
    gate = inspect.getsource(ai_state.is_ai_enabled)
    assert "return False" in gate


def test_both_ai_state_resolvers_read_inside_a_savepoint():
    """The gate had one and the display path did not.

    Postgres aborts the whole transaction on a failed SELECT, so a resolver
    that swallows the error without a savepoint leaves the caller's next
    statement raising InFailedSqlTransaction. On the unauthenticated
    /platform/config that turned "the database blinked" into a 500 — the case
    ``ai_enabled_for_display`` exists to survive.
    """
    from app.services import ai_state

    for fn in (ai_state.is_ai_enabled, ai_state.ai_enabled_for_display):
        source = inspect.getsource(fn)
        assert "_read_override_isolated" in source, fn.__name__

    # And that the isolation is a **Core** savepoint under ``no_autoflush``.
    # ``Session.begin_nested`` is the obvious choice and the wrong one: it calls
    # ``session.flush()``, so every cache miss flushed the caller's pending
    # entities, and a failed read then expired their modified instances
    # (/code-review, 2026-08-28). Asserted because the wrong one still satisfies
    # "a savepoint exists", which is all this test used to check.
    helper = inspect.getsource(ai_state._read_override_isolated)
    assert "connection" in helper and "begin_nested" in helper
    assert "no_autoflush" in helper
    assert "db.begin_nested" not in helper
