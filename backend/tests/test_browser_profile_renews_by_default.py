"""Every browser profile renews its session by itself by default.

``renews_by_itself`` (the card badge, the list, ``check_browser_profile_session``)
reads one computation, ``ProfileSnapshot.renews_unattended``: stored
credentials with a valid TOTP secret, stored credentials whose last sign-in
asked no person (``signs_in_unattended``), credentials not tried yet, or a
saved identity-provider session. The sign-in, the dry run and the keep-alive
learn the flag; a new profile's keep-alive interval is 240 minutes.
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from types import SimpleNamespace

from app.api.v1.schemas.browser_profiles import BrowserProfileWrite
from app.models.browser_profile import KEEPALIVE_MINUTES_DEFAULT, SESSION_ACTIVE, BrowserProfile
from app.services.browser_profiles import profiles, signin

_SECRET = base64.b32encode(b"neutral-test-key-0001").decode()
_STATE = {"cookies": [{"name": "sid", "value": "v", "domain": "app.example.test", "path": "/"}], "origins": []}
_PASSED = signin.ProbeResult(True, "https://app.example.test/home", "start_url renders the application")
_SIGNED_OUT = signin.ProbeResult(False, "https://app.example.test/login", "the sign-in form is shown")


def _profile(**columns) -> BrowserProfile:
    return BrowserProfile(id=uuid.uuid4(), name="Demo", start_url="https://app.example.test/", **columns)


def _local_login(**columns) -> BrowserProfile:
    """A local login: username and password, no MFA secret."""
    profile = _profile(**columns)
    profiles.write_credentials(profile, username="demo.user@example.test", password="pw")
    return profile


# ── The computation ───────────────────────────────────────────────────────


def test_a_local_login_not_tried_yet_renews_by_itself():
    profile = _local_login()
    assert profile.signs_in_unattended is None
    assert profiles.renews_by_itself(profile) is True
    assert profiles.serialize_profile(profile, can_manage=True)["renews_by_itself"] is True


def test_a_local_login_whose_last_sign_in_asked_no_person_renews_by_itself():
    profile = _local_login()
    profile.signs_in_unattended = True
    assert profiles.snapshot(profile).renews_unattended is True
    assert profiles.renews_by_itself(profile) is True


def test_credentials_whose_sign_in_asked_a_person_do_not_renew_by_themselves():
    profile = _local_login()
    profile.signs_in_unattended = False
    assert profiles.renews_by_itself(profile) is False
    # They stay renewable: the keep-alive and a recording still try them.
    assert profiles.snapshot(profile).renewable is True


def test_a_valid_totp_secret_renews_even_after_a_sign_in_asked_a_person():
    profile = _profile()
    profiles.write_credentials(profile, username="u@example.test", password="pw", totp_secret=_SECRET)
    profile.signs_in_unattended = False
    assert profiles.renews_by_itself(profile) is True


def test_a_profile_without_credentials_or_idp_session_does_not_renew():
    assert profiles.renews_by_itself(_profile()) is False
    username_only = _profile()
    profiles.write_credentials(username_only, username="u@example.test")
    assert profiles.renews_by_itself(username_only) is False


def test_an_unusable_totp_secret_untried_does_not_read_as_renewing():
    import json

    from app.core.security import encrypt_value

    profile = _profile()
    profile.credentials_enc = encrypt_value(json.dumps({"username": "u", "password": "p", "totp_secret": "op://x/y"}))
    assert profiles.renews_by_itself(profile) is False


def test_a_saved_identity_provider_session_still_renews_without_credentials():
    profile = _profile(login_url="https://app.example.test/sso")
    idp = {"cookies": [{"name": "ESTSAUTH", "value": "v", "domain": ".login.idp.example.test", "path": "/"}]}
    profiles.save_storage_state(profile, idp)
    assert profiles.renews_by_itself(profile) is True


def test_new_credentials_forget_what_the_last_sign_in_learnt():
    profile = _local_login()
    profile.signs_in_unattended = False
    assert profiles.write_credentials(profile, password="new-pw") is True
    assert profile.signs_in_unattended is None
    assert profiles.renews_by_itself(profile) is True
    # Unchanged credentials keep it.
    profile.signs_in_unattended = False
    assert profiles.write_credentials(profile, password="new-pw") is False
    assert profile.signs_in_unattended is False


# ── The keep-alive interval defaults to 240 minutes ───────────────────────


def test_a_new_profile_keeps_its_session_alive_every_240_minutes():
    assert KEEPALIVE_MINUTES_DEFAULT == 240
    assert KEEPALIVE_MINUTES_DEFAULT in profiles.KEEPALIVE_CHOICES
    assert profiles.clean_fields(name="Demo", start_url="https://app.example.test/").keepalive_minutes == 240
    assert BrowserProfile.__table__.c.keepalive_minutes.default.arg == KEEPALIVE_MINUTES_DEFAULT
    assert BrowserProfile.__table__.c.keepalive_minutes.server_default.arg.text == "240"
    assert profiles.serialize_profile(_profile(), can_manage=True)["keepalive_minutes"] == 240
    # 0 stays a choice a person makes.
    never = profiles.clean_fields(name="D", start_url="https://app.example.test/", keepalive_minutes=0)
    assert never.keepalive_minutes == 0
    assert profiles.serialize_profile(_profile(keepalive_minutes=0), can_manage=True)["keepalive_minutes"] == 0


def test_a_write_that_omits_the_keepalive_keeps_the_profile_interval():
    body = BrowserProfileWrite(name="Demo", start_url="https://app.example.test/")
    assert body.keepalive_minutes is None


# ── The sign-in, the dry run and the keep-alive learn it ──────────────────


@contextlib.asynccontextmanager
async def _browser(**_kwargs):
    yield SimpleNamespace(page=SimpleNamespace(url="https://app.example.test/home"))


def _recorder(monkeypatch) -> list[dict]:
    updates: list[dict] = []

    async def update(profile_id, **changes):
        updates.append(changes)

    async def no_screenshot(*_args, **_kwargs):
        return None

    monkeypatch.setattr(signin, "_update_profile", update)
    monkeypatch.setattr(signin, "_screenshot", no_screenshot)
    monkeypatch.setattr(signin, "profile_browser", _browser)
    return updates


async def test_a_dry_run_that_reaches_the_application_alone_marks_the_profile_renewing(monkeypatch):
    updates = _recorder(monkeypatch)

    async def drive(*_args, **_kwargs):
        return signin.SignInResult(True, "Signed in.", _PASSED, driven=True)

    async def save_session(rb, snap):
        return _STATE, _PASSED

    monkeypatch.setattr(signin, "drive_sign_in", drive)
    monkeypatch.setattr(signin, "save_session", save_session)

    outcome = await signin.dry_run_sign_in(_local_login(), user_id=None)

    assert outcome.status == signin.STATUS_ACTIVE
    assert updates == [{"signs_in_unattended": True}]


async def test_a_dry_run_on_a_session_already_signed_in_learns_nothing(monkeypatch):
    updates = _recorder(monkeypatch)

    async def drive(*_args, **_kwargs):
        return signin.SignInResult(True, "Already signed in.", _PASSED)  # not driven

    async def save_session(rb, snap):
        return _STATE, _PASSED

    monkeypatch.setattr(signin, "drive_sign_in", drive)
    monkeypatch.setattr(signin, "save_session", save_session)

    await signin.dry_run_sign_in(_local_login(), user_id=None)

    assert updates == []


async def test_a_dry_run_that_needs_a_person_clears_it(monkeypatch):
    updates = _recorder(monkeypatch)

    async def drive(*_args, **_kwargs):
        raise signin.SignInRequiredError("The sign-in asks for a one-time code.")

    monkeypatch.setattr(signin, "drive_sign_in", drive)

    outcome = await signin.dry_run_sign_in(_local_login(), user_id=None)

    assert outcome.status == signin.STATUS_FAILED
    assert updates == [{"signs_in_unattended": False}]


async def test_a_sign_in_that_asks_no_person_saves_the_session_and_marks_it_renewing(monkeypatch):
    updates = _recorder(monkeypatch)
    published: list = []

    async def publish(cid, outcome):
        published.append(outcome)

    async def drive(*_args, **_kwargs):
        return signin.SignInResult(True, "Signed in.", _PASSED, driven=True)

    async def save_session(rb, snap):
        return _STATE, _PASSED

    monkeypatch.setattr(signin, "publish", publish)
    monkeypatch.setattr(signin, "drive_sign_in", drive)
    monkeypatch.setattr(signin, "save_session", save_session)

    await signin._run_sign_in("cid", profiles.snapshot(_local_login()), None)

    saved = [u for u in updates if "storage_state" in u]
    assert saved and saved[-1]["signs_in_unattended"] is True
    assert published[-1].status == signin.STATUS_ACTIVE


async def test_a_sign_in_that_asks_for_a_code_marks_the_profile_as_needing_a_person(monkeypatch):
    updates = _recorder(monkeypatch)

    async def publish(cid, outcome):
        return None

    async def next_code(cid, timeout):
        return "123456"

    async def drive(rb, snap, *, user_id, ask, announce_approval=None, check_first=True):
        await ask(signin.MfaPrompt(kind=signin.KIND_CODE, url="https://idp.example.test/mfa"))
        return signin.SignInResult(True, "Signed in.", _PASSED, driven=True)

    async def save_session(rb, snap):
        return _STATE, _PASSED

    monkeypatch.setattr(signin, "publish", publish)
    monkeypatch.setattr(signin, "_next_code", next_code)
    monkeypatch.setattr(signin, "drive_sign_in", drive)
    monkeypatch.setattr(signin, "save_session", save_session)

    await signin._run_sign_in("cid", profiles.snapshot(_local_login()), None)

    assert any(u.get("signs_in_unattended") is False for u in updates)
    saved = [u for u in updates if "storage_state" in u]
    assert saved and "signs_in_unattended" not in saved[-1]


class _Db:
    def __init__(self, rows):
        self.rows = list(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, *_args, **_kwargs):
        rows = self.rows
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))

    async def get(self, _model, _id):
        return self.rows[0]

    async def commit(self):
        return None


def _keepalive(monkeypatch, profile, drive):
    from app.core import redis as redis_module
    from app.services.browser_profiles import browser as browser_module
    from app.services.browser_profiles import keepalive

    class _Redis:
        async def set(self, *_args, **_kwargs):
            return True

        async def delete(self, *_args):
            return 1

    async def client():
        return _Redis()

    async def probe_in(rb, snap):
        return _SIGNED_OUT

    async def save_session(rb, snap):
        return _STATE, _PASSED

    monkeypatch.setattr(redis_module, "redis_client", client)
    monkeypatch.setattr(keepalive, "async_session", lambda: _Db([profile]))
    monkeypatch.setattr(browser_module, "profile_browser", _browser)
    monkeypatch.setattr(signin, "probe_in", probe_in)
    monkeypatch.setattr(signin, "drive_sign_in", drive)
    monkeypatch.setattr(signin, "save_session", save_session)
    return keepalive


def _expired_local_login() -> BrowserProfile:
    profile = _local_login(keepalive_minutes=KEEPALIVE_MINUTES_DEFAULT)
    profiles.save_storage_state(profile, _STATE, probe=_SIGNED_OUT.to_dict())
    profile.last_probe_at = None
    return profile


async def test_the_keepalive_renewal_of_a_local_login_marks_it_renewing(monkeypatch):
    profile = _expired_local_login()

    async def drive(*_args, **_kwargs):
        return signin.SignInResult(True, "Signed in.", _PASSED, driven=True)

    await _keepalive(monkeypatch, profile, drive).keepalive_sweep()

    assert profile.signs_in_unattended is True
    assert profile.session_status == SESSION_ACTIVE
    assert profiles.serialize_profile(profile, can_manage=True)["renews_by_itself"] is True


async def test_a_keepalive_renewal_that_needs_a_person_clears_it(monkeypatch):
    profile = _expired_local_login()

    async def drive(*_args, **_kwargs):
        raise signin.SignInRequiredError("The sign-in waits for an approval on a phone.")

    await _keepalive(monkeypatch, profile, drive).keepalive_sweep()

    assert profile.signs_in_unattended is False
    assert profiles.renews_by_itself(profile) is False
