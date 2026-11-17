"""A2A is offered exactly while agentic chat is.

No predicate of its own: the routes take ``require_chat_enabled`` and every
other path asks ``is_chat_enabled``. Pinned here: every route asks it, the
unwatched outbound paths (sweep, tool wiring, pre-flight) stop without it, the
probes report it, teardown does not wait for it, and the tier catalog agrees.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

import app.api.v1.platform as platform_mod
from app.api import deps
from tests.a2a_chat_gate import stub_chat_gate


class TestEveryRouteAsksIt:
    def test_the_partner_facing_routes(self):
        from app.api.v1 import a2a_server

        for name in ("list_partner_agents", "get_agent_card", "a2a_jsonrpc"):
            src = inspect.getsource(getattr(a2a_server, name))
            assert "await require_chat_enabled(db)" in src, name
            # Behind the per-IP limiter, which is this surface's only control
            # on unauthenticated load: a flood must be throttled before it
            # pays the tier lookup.
            assert src.index("_enforce_preauth_rate_limit") < src.index(
                "await require_chat_enabled(db)"
            ), name

    def test_the_task_surface_states_ai_off_first(self):
        # The gate would refuse too, but the explicit refusal comes first so
        # the reason is the more actionable one.
        from app.api.v1 import a2a_server

        src = inspect.getsource(a2a_server.a2a_jsonrpc)
        assert src.index("refuse_ai_disabled()") < src.index(
            "await require_chat_enabled(db)"
        )

    def test_the_internal_routers_declare_it_once(self):
        from app.api.v1 import a2a_admin, a2a_client

        for router in (a2a_admin.router, a2a_client.router):
            assert any(
                d.dependency is deps.require_chat_enabled for d in router.dependencies
            ), router.prefix

    def test_the_integration_routes(self):
        # The shared integrations router cannot carry the gate for everyone,
        # so the four A2A endpoints on it await it themselves.
        from app.api.v1 import integrations

        for name in (
            "create_integration",
            "update_integration",
            "regenerate_a2a_credential",
            "get_a2a_federation_activity",
        ):
            assert "await require_chat_enabled(db)" in inspect.getsource(
                getattr(integrations, name)
            ), name
        # The catalog is deliberately NOT one of them: it lists A2A like any
        # other provider and gates nothing. The routes above are what refuse.
        assert "is_chat_enabled" not in inspect.getsource(
            integrations.list_provider_catalog
        )

    def test_the_chat_licence_is_the_one_licence(self):
        """A2A follows chat, and chat follows THE licence: ``is_chat_licensed``
        must be ``is_agentic_ai_licensed`` under another name, not a second
        rule that could drift (the retired tier catalog used to pin this)."""
        source = inspect.getsource(platform_mod.is_chat_licensed)
        assert "return await is_agentic_ai_licensed(db)" in source


class TestTheUnwatchedOutboundPathsStop:
    """Sweep, tool wiring and pre-flight fire with no operator watching, which
    is exactly where a missing gate would leak outbound requests silently."""

    @pytest.mark.asyncio
    async def test_the_sweep_does_nothing(self, monkeypatch):
        from app.a2a.client.sweep import sweep_once

        stub_chat_gate(monkeypatch, enabled=False)

        class _Poisoned:
            async def execute(self, _stmt):
                raise AssertionError("A2A not offered — the sweep must not touch the database")

        assert await sweep_once(_Poisoned()) == 0

    @pytest.mark.asyncio
    async def test_the_tool_is_not_offered(self, monkeypatch):
        # An OUTBOUND row, so the direction check passes and the gate is the
        # one thing between the binding and the tool.
        from app.a2a import providers
        from app.services import integration_tools

        stub_chat_gate(monkeypatch, enabled=False)

        async def _outbound(_db, _id):
            return SimpleNamespace(direction="outbound")

        monkeypatch.setattr(providers, "get_settings", _outbound)
        assert (
            await integration_tools._build_a2a_delegation_tool(
                None, "oauth_a2a", str(uuid.uuid4())
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_the_preflight_refuses_before_reading_the_registration(
        self, monkeypatch
    ):
        from app.a2a.client import delegation

        stub_chat_gate(monkeypatch, enabled=False)
        # No target and no usable registration: if the gate were not the first
        # check, one of those would be the refusal reported instead.
        with pytest.raises(delegation.PreflightRefusedError) as caught:
            await delegation.preflight(object(), SimpleNamespace(), None)
        assert platform_mod.CHAT_UNAVAILABLE_DETAIL in str(caught.value)

    def test_teardown_cancel_is_deliberately_not_gated(self):
        # The one outbound path that keeps working: cancelling a task this
        # deployment started while A2A was offered. Leaving the partner
        # working toward an answer nobody will give is the worse outcome, and
        # the message carries nothing but the task id.
        from app.a2a.client import delegation

        assert "is_chat_enabled" not in inspect.getsource(delegation.cancel)

    def test_the_re_send_of_a_cancel_a_crash_interrupted_is_teardown_too(self):
        # The same cancel, sent by the client sweep because the process that
        # decided it was killed first. Gated, it would let a crash
        # decide whether the partner is ever told.
        from app.a2a.client import sweep

        assert "is_chat_enabled" not in inspect.getsource(sweep.reissue_owed_cancels)


class TestTheProbesReportIt:
    def test_all_four_probes_ask_it(self):
        from app.a2a.client import probe
        from app.integrations.providers import entra_a2a, google_a2a, oauth_a2a

        for fn in (
            probe.outbound_probe,
            entra_a2a.check_connectivity,
            google_a2a.check_connectivity,
            oauth_a2a.check_connectivity,
        ):
            src = inspect.getsource(fn)
            assert "is_chat_enabled(ctx.db)" in src, fn.__module__
            assert "CHAT_UNAVAILABLE_DETAIL" in src, fn.__module__
