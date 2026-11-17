"""Server-side Terms of Use acceptance gate.

Terms of Use are accepted ONCE for the whole platform (by an admin), not
per-user. The gate lives in ``app.api.deps.enforce_terms_of_use`` and is layered
onto ``get_current_user``, so every authenticated endpoint inherits it without
any per-router wiring. These tests pin the exact conditions under which it
fires, because each one is a deliberate product decision a refactor could
silently invert:

* it fires only when the env config is on AND the Agentic AI license is active
  (CE deployments are never gated);
* acceptance is read from the ``PlatformSettings`` singleton, not the user — one
  platform acceptance clears the gate for everyone;
* it never fires for machine credentials (``fcp-`` API keys, cross-platform
  JWTs), so a ToU bump can't take every customer's automations offline;
* a blank ``TERMS_OF_USE_VERSION`` deactivates it rather than locking everyone
  behind a version nobody can match.

Mirrors the repo's fake-session convention (``test_auth_me_group_ids.py``):
a mini FastAPI app plus dependency overrides — the backend suite has no
real-DB harness.
"""

import uuid

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.api import deps
from app.core.config import settings
from app.core.database import get_db

CURRENT = "2099-01-01"


def _fake_user(*, kind: str = "session_jwt", is_admin: bool = False, email: str = "u@example.com"):
    """A minimal stand-in for the ORM ``User`` the gate reads off.

    The gate no longer reads any acceptance field from the user — only
    the credential kind — so acceptance lives on the platform stub below.
    """
    return type(
        "_User",
        (),
        {
            "id": uuid.uuid4(),
            "email": email,
            "auth_credential_kind": kind,
            "is_admin": is_admin,
        },
    )()


def _patch_platform_accepted(monkeypatch, accepted_version):
    """Stub the read-only singleton peek the gate reads acceptance from.

    Also resets the process-local positive cache in ``deps`` so a previous
    test's confirmed acceptance cannot leak into this one.
    """
    import app.api.v1.platform_settings_helpers as ps_helpers

    deps.reset_terms_of_use_cache()

    ps = type(
        "_PS",
        (),
        {"tou_accepted_version": accepted_version, "tou_accepted_at": None},
    )()

    async def _peek(_db):
        return ps

    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _peek)


def _make_client(monkeypatch, user, *, agentic: bool = True, version: str = CURRENT,
                 enabled: bool = True, platform_accepted: str | None = None,
                 exempt: str = ""):
    monkeypatch.setattr(settings, "terms_of_use_enabled", enabled)
    monkeypatch.setattr(settings, "terms_of_use_version", version)
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", exempt)
    _patch_platform_accepted(monkeypatch, platform_accepted)

    async def _agentic(_db):
        return agentic

    # ``enforce_terms_of_use`` imports this lazily from app.api.v1.platform, so
    # patch it at the source module.
    import app.api.v1.platform as platform_api

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _agentic)

    app = FastAPI()

    @app.get("/protected")
    async def _protected(_u=Depends(deps.get_current_user)):
        return {"ok": True}

    async def _get_db():
        yield object()

    app.dependency_overrides[deps.get_current_user_ungated] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    return TestClient(app)


def test_pending_platform_blocks_every_user(monkeypatch):
    client = _make_client(monkeypatch, _fake_user(), platform_accepted=None)
    resp = client.get("/protected")
    assert resp.status_code == 403
    # The frontend keys off this exact prefix (lib/api.ts) to tell the ToU 403
    # apart from every other 403.
    assert resp.json()["detail"].startswith("Terms of Use acceptance required")


def test_the_refusal_carries_a_code_beside_its_readable_detail(monkeypatch):
    """``code`` for clients that branch; ``detail`` stays the string ``lib/api.ts`` sniffs."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    from app.main import _log_http_exception

    client = _make_client(monkeypatch, _fake_user(), platform_accepted=None)
    client.app.add_exception_handler(StarletteHTTPException, _log_http_exception)
    resp = client.get("/protected")
    assert resp.status_code == 403
    assert resp.json() == {"detail": deps.TERMS_OF_USE_REQUIRED_DETAIL, "code": "terms_required"}


def test_accepted_current_version_passes(monkeypatch):
    client = _make_client(monkeypatch, _fake_user(), platform_accepted=CURRENT)
    assert client.get("/protected").status_code == 200


def test_confirmed_acceptance_is_cached_for_the_process(monkeypatch):
    """After one confirmed read, the gate stops hitting the DB entirely.

    The positive cache in ``deps.platform_tou_accepted_version`` is what keeps
    the accepted-platform hot path free of a per-request singleton SELECT —
    the same "the common case costs nothing" property the per-user gate had.
    """
    client = _make_client(monkeypatch, _fake_user(), platform_accepted=CURRENT)
    assert client.get("/protected").status_code == 200

    import app.api.v1.platform_settings_helpers as ps_helpers

    async def _boom(_db):  # pragma: no cover - must not run
        raise AssertionError("singleton must not be re-read once accepted")

    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _boom)
    assert client.get("/protected").status_code == 200


def test_stale_accepted_version_is_blocked(monkeypatch):
    """A published version bump re-opens the gate until the platform re-accepts."""
    client = _make_client(monkeypatch, _fake_user(), platform_accepted="2026-07-14")
    assert client.get("/protected").status_code == 403


def test_disabled_by_config_passes(monkeypatch):
    """``TERMS_OF_USE_ENABLED=false`` — the self-hosted escape hatch."""
    client = _make_client(monkeypatch, _fake_user(), enabled=False, platform_accepted=None)
    assert client.get("/protected").status_code == 200


def test_blank_version_fails_open(monkeypatch):
    """A misconfigured version must not lock every user out permanently."""
    client = _make_client(monkeypatch, _fake_user(), version="   ", platform_accepted=None)
    assert client.get("/protected").status_code == 200


def test_community_edition_is_not_gated(monkeypatch):
    """The gate is only on AI-activated XTM One, not CE."""
    client = _make_client(monkeypatch, _fake_user(), agentic=False, platform_accepted=None)
    assert client.get("/protected").status_code == 200


@pytest.mark.parametrize("kind", ["api_key", "platform_jwt"])
def test_machine_credentials_are_not_gated(monkeypatch, kind):
    """Only interactive session JWTs are gated.

    ``fcp-`` API keys and cross-platform JWTs have nowhere to render a consent
    screen; gating them would turn a routine ToU revision into an automation
    outage across every install.
    """
    client = _make_client(monkeypatch, _fake_user(kind=kind), platform_accepted=None)
    assert client.get("/protected").status_code == 200


def test_gate_skips_license_lookup_when_platform_accepted(monkeypatch):
    """The hot path must not hit the license lookup once the platform accepted.

    ``is_agentic_ai_licensed`` runs a query; reaching it on every authenticated
    request for the (overwhelmingly common) already-accepted platform would add
    a round-trip to the whole API. The singleton read settles it first.
    """
    calls: list[object] = []

    async def _agentic(db):
        calls.append(db)
        return True

    import app.api.v1.platform as platform_api

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _agentic)
    monkeypatch.setattr(settings, "terms_of_use_enabled", True)
    monkeypatch.setattr(settings, "terms_of_use_version", CURRENT)
    _patch_platform_accepted(monkeypatch, CURRENT)

    app = FastAPI()

    @app.get("/protected")
    async def _protected(_u=Depends(deps.get_current_user)):
        return {"ok": True}

    async def _get_db():
        yield object()

    app.dependency_overrides[deps.get_current_user_ungated] = lambda: _fake_user()
    app.dependency_overrides[get_db] = _get_db

    assert TestClient(app).get("/protected").status_code == 200
    assert calls == []


# ---------------------------------------------------------------------------
# Web chat
#
# The web chat's send stream outlives a ToU bump only for the turn it runs:
# every send, steer, approval and events stream is a request through the gate.
# ---------------------------------------------------------------------------


def _depends_on(dependant, call) -> bool:
    return any(d.call is call or _depends_on(d, call) for d in dependant.dependencies)


def test_every_web_chat_route_goes_through_the_gate():
    from fastapi.routing import APIRoute

    from app.api.v1 import chat_web_stream

    routes = [r for r in chat_web_stream.router.routes if isinstance(r, APIRoute)]
    assert routes
    ungated = [r.path for r in routes if not _depends_on(r.dependant, deps.get_current_user)]
    assert ungated == []


def test_pending_platform_blocks_a_web_chat_send(monkeypatch):
    from app.api.v1 import chat_web_stream

    client = _make_client(monkeypatch, _fake_user(), platform_accepted=None)
    client.app.include_router(chat_web_stream.router, prefix="/api/v1/web")
    resp = client.post(
        "/api/v1/web/chat/messages", json={"content": "hello", "conversation_id": str(uuid.uuid4())}
    )
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("Terms of Use acceptance required")


# ---------------------------------------------------------------------------
# Telemetry gauge
#
# Exercises ``_refresh_terms_of_use`` for real — a single platform-wide
# accepted/pending gauge, not the old per-user counters.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("platform_accepted", "expected"),
    [(CURRENT, 1), ("2026-07-14", 0), (None, 0)],
)
async def test_telemetry_reports_platform_acceptance(monkeypatch, platform_accepted, expected):
    from app.services import telemetry

    monkeypatch.setattr(settings, "terms_of_use_enabled", True)
    monkeypatch.setattr(settings, "terms_of_use_version", CURRENT)
    _patch_platform_accepted(monkeypatch, platform_accepted)

    captured: dict[str, int] = {}
    monkeypatch.setattr(telemetry, "_apply", lambda attr, value: captured.__setitem__(attr, value))

    await telemetry._refresh_terms_of_use(object())

    assert captured == {"terms_accepted": expected}


async def test_telemetry_reports_zero_when_singleton_row_is_missing(monkeypatch):
    """A fresh install without the singleton row is simply "not accepted".

    ``_refresh_terms_of_use`` must use the read-only peek — reporting 0, not
    creating the row as a side effect of a telemetry refresh.
    """
    from app.services import telemetry

    monkeypatch.setattr(settings, "terms_of_use_enabled", True)
    monkeypatch.setattr(settings, "terms_of_use_version", CURRENT)

    import app.api.v1.platform_settings_helpers as ps_helpers

    async def _peek(_db):
        return None

    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _peek)

    captured: dict[str, int] = {}
    monkeypatch.setattr(telemetry, "_apply", lambda attr, value: captured.__setitem__(attr, value))

    await telemetry._refresh_terms_of_use(object())

    assert captured == {"terms_accepted": 0}


async def test_telemetry_gauge_is_zero_when_gate_inactive(monkeypatch):
    """An install that never turns the gate on reports 0, with no settings read."""
    from app.services import telemetry

    monkeypatch.setattr(settings, "terms_of_use_enabled", False)

    import app.api.v1.platform_settings_helpers as ps_helpers

    async def _unexpected(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("no settings read while the gate is inactive")

    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _unexpected)

    captured: dict[str, int] = {}
    monkeypatch.setattr(telemetry, "_apply", lambda attr, value: captured.__setitem__(attr, value))

    await telemetry._refresh_terms_of_use(object())

    assert captured == {"terms_accepted": 0}


# ---------------------------------------------------------------------------
# TERMS_OF_USE_EXEMPT_EMAILS
#
# The vendor accounts operating a SaaS instance sign in without accepting the
# terms on the customer's behalf. Every gate takes the same decision; the
# platform stays unaccepted for everyone else.
# ---------------------------------------------------------------------------

EXEMPT = " Ops@Vendor.example , second@vendor.example,, "


def test_exempt_email_set_is_trimmed_lowercased_and_drops_blanks(monkeypatch):
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", EXEMPT)
    assert settings.terms_of_use_exempt_email_set == frozenset(
        {"ops@vendor.example", "second@vendor.example"}
    )


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        ("ops@vendor.example", True),
        ("OPS@VENDOR.EXAMPLE", True),
        ("  second@vendor.example ", True),
        ("other@vendor.example", False),  # exact address, never a domain
        ("ops@vendor.example.evil", False),
        ("", False),
        (None, False),
    ],
)
def test_exempt_matches_exact_addresses_only(monkeypatch, email, expected):
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", EXEMPT)
    assert settings.terms_of_use_exempt(email) is expected


def test_nothing_is_exempt_by_default(monkeypatch):
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", "")
    assert settings.terms_of_use_exempt_email_set == frozenset()
    assert settings.terms_of_use_exempt("u@example.com") is False


def test_exempt_user_passes_a_pending_platform(monkeypatch):
    client = _make_client(
        monkeypatch, _fake_user(email="ops@vendor.example"), platform_accepted=None, exempt=EXEMPT
    )
    assert client.get("/protected").status_code == 200


def test_exemption_does_not_open_the_platform_for_others(monkeypatch):
    client = _make_client(monkeypatch, _fake_user(), platform_accepted=None, exempt=EXEMPT)
    resp = client.get("/protected")
    assert resp.status_code == 403
    assert resp.json()["detail"].startswith("Terms of Use acceptance required")


async def test_exempt_user_skips_the_acceptance_and_license_reads(monkeypatch):
    """The exemption is a config-only decision: no singleton or licence query."""
    monkeypatch.setattr(settings, "terms_of_use_enabled", True)
    monkeypatch.setattr(settings, "terms_of_use_version", CURRENT)
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", EXEMPT)

    import app.api.v1.platform as platform_api
    import app.api.v1.platform_settings_helpers as ps_helpers

    deps.reset_terms_of_use_cache()

    async def _boom(_db):  # pragma: no cover - must not run
        raise AssertionError("an exempt user needs no DB read")

    monkeypatch.setattr(ps_helpers, "peek_platform_settings", _boom)
    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _boom)

    assert await deps.enforce_terms_of_use(object(), _fake_user(email="OPS@vendor.example")) is None


@pytest.mark.parametrize(
    ("email", "required", "accepted"),
    [("ops@vendor.example", False, True), ("u@example.com", True, False)],
)
async def test_auth_me_terms_state_applies_the_exemption(monkeypatch, email, required, accepted):
    """``/auth/me`` must not render the consent screen the API would not enforce."""
    from app.api.v1.auth import _build_terms_state

    monkeypatch.setattr(settings, "terms_of_use_enabled", True)
    monkeypatch.setattr(settings, "terms_of_use_version", CURRENT)
    monkeypatch.setattr(settings, "terms_of_use_exempt_emails", EXEMPT)
    _patch_platform_accepted(monkeypatch, None)

    import app.api.v1.platform as platform_api

    async def _agentic(_db):
        return True

    monkeypatch.setattr(platform_api, "is_agentic_ai_licensed", _agentic)

    state = await _build_terms_state(object(), _fake_user(email=email, is_admin=True))
    assert (state.required, state.accepted) == (required, accepted)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
