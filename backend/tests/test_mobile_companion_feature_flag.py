"""The mobile companion stays behind ``MOBILE_COMPANION``, off by default.

Like ``VIDEO_STUDIO`` the flag is an enforcement boundary: off, the companion's
routes answer 404 before authentication and are not documented, nothing is
pushed to a phone, the PIN lock never refuses a request, and the ``phone`` tool
is never built nor offered. An agent that already carries the tool id keeps it:
the flag decides what runs, not what is stored.

The frontend half is pinned by source assertions, as in
``test_video_studio_feature_flag.py``.

See ``dev-docs/mobile-companion-feature-flag.md``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from app.core.feature_flags import KNOWN_FEATURE_FLAGS, is_feature_enabled

# Imported at collection, before any fixture can turn the flag on: ``app.main``
# reads ``MOBILE_COMPANION`` once, at import, for ``include_in_schema``. A lazy
# import inside a test made the documented routes depend on which test of the
# xdist worker imported the app first (``mobile_companion_enabled`` on: the
# companion stayed documented for the whole worker; #10677).
from app.main import application
from app.services import app_lock, mobile_push
from app.services.builtin_tools import agent_builder

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"

FLAG = "MOBILE_COMPANION"

requires_flag_off = pytest.mark.skipif(
    is_feature_enabled(FLAG),
    reason=f"ENABLED_FEATURES has {FLAG} on; these assert the flag-off shape",
)

#: One route of each companion router, read and written.
COMPANION_ROUTES = (
    ("GET", "/api/v1/mobile-devices"),
    ("GET", "/api/v1/mobile-devices/vapid-public-key"),
    ("POST", "/api/v1/mobile-devices"),
    ("DELETE", f"/api/v1/mobile-devices/{uuid.UUID(int=7)}"),
    ("POST", "/api/v1/mobile/push-actions"),
    ("GET", "/api/v1/app-lock"),
    ("POST", "/api/v1/app-lock/session"),
    ("POST", "/api/v1/app-lock/unlock"),
    ("PUT", "/api/v1/app-lock/pin"),
)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _has(source: str, snippet: str) -> bool:
    return _squash(snippet) in _squash(source)


def _read_backend(*parts: str) -> str:
    return BACKEND.joinpath(*parts).read_text(encoding="utf-8")


def _read_frontend(*parts: str) -> str:
    return FRONTEND.joinpath(*parts).read_text(encoding="utf-8")


def _line(source: str, needle: str) -> str:
    return next(line for line in source.splitlines() if needle in line)


def test_the_flag_is_in_both_registries():
    assert FLAG in KNOWN_FEATURE_FLAGS
    assert f'"{FLAG}",' in _read_frontend("src", "lib", "utils.ts")
    assert agent_builder._FEATURE_GATED_BUILTIN_TOOL_IDS["phone"] == FLAG


async def _answers(user) -> dict[tuple[str, str], int]:
    """The status of every companion route, signed in as ``user`` (``None``: no session)."""
    from app.api import deps
    from app.main import application

    if user is not None:
        application.dependency_overrides[deps.get_current_user] = lambda: user
        application.dependency_overrides[deps.get_current_user_ungated] = lambda: user
    try:
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return {
                (method, path): (await client.request(method, path, json={})).status_code
                for method, path in COMPANION_ROUTES
            }
    finally:
        application.dependency_overrides.pop(deps.get_current_user, None)
        application.dependency_overrides.pop(deps.get_current_user_ungated, None)


@requires_flag_off
@pytest.mark.parametrize("is_admin", [False, True], ids=["non-admin", "admin"])
async def test_off_every_companion_route_answers_404_to_anyone(is_admin):
    """The gate runs before authentication: no session, a non-admin and an
    administrator all get the answer of a route that does not exist."""
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, role="admin" if is_admin else "user")
    for who in (None, user):
        answers = await _answers(who)
        assert answers == dict.fromkeys(COMPANION_ROUTES, 404), answers


@requires_flag_off
async def test_off_the_404_names_no_flag_and_no_route_is_published():
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/api/v1/mobile-devices")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    # ``include_in_schema`` is read once, at startup, with the flag off here.
    leaked = [
        p
        for p in application.openapi()["paths"]
        if p.startswith(("/api/v1/mobile-devices", "/api/v1/mobile/", "/api/v1/app-lock"))
    ]
    assert leaked == []


@pytest.mark.usefixtures("mobile_companion_enabled")
async def test_on_the_routes_are_mounted_and_ask_for_a_session():
    answers = await _answers(None)
    assert 404 not in answers.values(), answers
    assert answers[("GET", "/api/v1/mobile-devices")] == 401
    assert answers[("GET", "/api/v1/app-lock")] == 401


# ── The PIN lock: never locks anyone out while the companion is off ───────────

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class _Db:
    def __init__(self, row):
        self.row = row

    async def get(self, _model, _sid):
        return self.row


def _companion_user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.UUID(int=1), web_session=SimpleNamespace(claims={"sid": "s"}))


def _row(*, ended: bool = False, unlocked_until=None) -> SimpleNamespace:
    return SimpleNamespace(user_id=uuid.UUID(int=1), unlocked_until=unlocked_until, ended=ended, failed_attempts=0)


@requires_flag_off
@pytest.mark.parametrize("required", [False, True], ids=["pin-chosen", "pin-required"])
async def test_off_a_locked_phone_session_is_not_refused(monkeypatch, required):
    async def _load_pin(_db, _user_id):
        return SimpleNamespace(enabled=True, pin_hash="x")

    monkeypatch.setattr(app_lock, "load_pin", _load_pin)
    monkeypatch.setattr(app_lock, "pin_required", lambda: required)
    expired = NOW - timedelta(minutes=1)
    assert await app_lock.enforce(_Db(_row(unlocked_until=expired)), _companion_user(), now=NOW) is None


@requires_flag_off
async def test_off_a_session_its_pin_ended_stays_ended():
    """Too many wrong PINs signed the session out: turning the flag off does
    not give it back (the person signs in again)."""
    with pytest.raises(HTTPException) as refused:
        await app_lock.enforce(_Db(_row(ended=True)), _companion_user(), now=NOW)
    assert (refused.value.status_code, refused.value.detail) == (401, {"code": app_lock.ENDED})


@pytest.mark.usefixtures("mobile_companion_enabled")
async def test_on_the_same_session_is_locked(monkeypatch):
    async def _load_pin(_db, _user_id):
        return SimpleNamespace(enabled=True, pin_hash="x")

    monkeypatch.setattr(app_lock, "load_pin", _load_pin)
    monkeypatch.setattr(app_lock, "pin_required", lambda: False)
    with pytest.raises(HTTPException) as refused:
        await app_lock.enforce(_Db(_row()), _companion_user(), now=NOW)
    assert refused.value.status_code == 423


# ── Phone pushes: none while the companion is off ─────────────────────────────


class _NoDb:
    """Any query fails the test: off, nothing is read."""

    @property
    def sync_session(self):
        raise AssertionError("queue_push touched the session")

    async def execute(self, *_args, **_kwargs):
        raise AssertionError("a phone was looked up")

    async def get(self, *_args, **_kwargs):
        raise AssertionError("a notification was read")


@requires_flag_off
async def test_off_nothing_is_queued_delivered_or_sent():
    db = _NoDb()
    mobile_push.queue_push(db, user_id=uuid.uuid4(), ref_kind="activity", ref_id=uuid.uuid4())  # type: ignore[arg-type]
    delivered = await mobile_push.deliver_queued_push(
        db,  # type: ignore[arg-type]
        user_id=str(uuid.uuid4()),
        ref_kind="activity",
        ref_id=str(uuid.uuid4()),
    )
    assert delivered is None
    counts = await mobile_push.push_item_to_user(db, uuid.uuid4(), SimpleNamespace(kind="run"))  # type: ignore[arg-type]
    assert counts == {"sent": 0, "skipped": 0, "gone": 0, "refused": 0, "failed": 0}


# ── The ``phone`` tool ────────────────────────────────────────────────────────


@requires_flag_off
async def test_off_the_catalog_withholds_the_tool_but_the_registry_keeps_it():
    from app.api.v1.settings import get_builtin_tool_catalog
    from app.services.builtin_tool_catalog import build_builtin_tool_catalog

    assert "phone" in build_builtin_tool_catalog(include_disabled=True)
    assert "phone" not in await get_builtin_tool_catalog(_user=None)
    assert _has(
        _read_backend("app", "api", "v1", "settings.py"),
        'if not is_feature_enabled("MOBILE_COMPANION"): catalog.pop("phone", None)',
    )


@requires_flag_off
def test_off_the_agent_builder_does_not_name_the_tool_but_still_accepts_it():
    assert "phone" not in agent_builder._advertised_builtin_tool_ids()
    assert "phone" in agent_builder._VALID_BUILTIN_TOOL_IDS


def test_the_tool_is_only_built_and_listed_with_the_flag_on():
    for module, gate in (
        ("agent_chat.py", 'if "phone" in agent_builtin_tool_ids and user_id and is_feature_enabled("MOBILE_COMPANION"):'),
        (
            "assignment_engine.py",
            'if "phone" in agent_builtin_tool_ids and _run_file_owner_id and is_feature_enabled("MOBILE_COMPANION"):',
        ),
        ("general_chat.py", 'if "phone" in ga_builtin_tool_ids and user_id and is_feature_enabled("MOBILE_COMPANION"):'),
        ("flow_access.py", 'and not (tool_id == "phone" and not is_feature_enabled("MOBILE_COMPANION"))'),
    ):
        assert _has(_read_backend("app", "services", module), gate), module


def test_the_tool_card_is_gated():
    from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS

    assert next(g for g in BUILTIN_TOOL_GROUPS if g.id == "phone").gate == "mobile_companion"
    assert _has(_read_frontend("src", "types", "index.ts"), '"video_studio" | "mobile_companion" | null')
    assert _has(_read_frontend("src", "hooks", "useBuiltinTools.ts"), "mobile_companion: mobileCompanionEnabled,")


# ── The frontend: no companion while the flag is off ──────────────────────────


def test_the_frontend_shows_no_companion_with_the_flag_off():
    # The device decides only while the flag is on: off, a phone gets the web
    # app (no companion shell, no PIN lock screen, no phone-only layout).
    device = _read_frontend("src", "hooks", "useCompanionDevice.ts")
    assert _has(device, 'const companion = useFeatureFlag("MOBILE_COMPANION"); return PHONE && companion;')
    for consumer in (
        ("components", "layout", "AppLayout.tsx"),
        ("components", "companion", "AppLockGate.tsx"),
        ("hooks", "api", "useAppLockApi.ts"),
    ):
        assert "useCompanionDevice()" in _read_frontend("src", *consumer), consumer

    app = _read_frontend("src", "App.tsx")
    for path in ("/inbox", "/share"):
        assert '<FeatureFlagGate flag="MOBILE_COMPANION">' in _line(app, f'path="{path}"'), path

    profile = _read_frontend("src", "pages", "Profile.tsx")
    assert _has(profile, 'const companionEnabled = useFeatureFlag("MOBILE_COMPANION");')
    assert _has(profile, '...(companionEnabled ? [{ key: "phones" as const')
    assert _has(profile, '{tab === "phones" && companionEnabled && <PhonesSection />}')
