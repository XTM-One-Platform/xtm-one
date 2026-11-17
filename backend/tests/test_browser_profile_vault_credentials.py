"""Browser profile credentials read from a secret vault.

A profile's username, password and TOTP secret may each be a
``vault://<provider>/<path>#<key>`` reference: it is checked when written, a
browser task receives it read through the one resolver
(``profiles.resolved_snapshot``), the synchronous snapshot never hands it over
as a value, a credential that cannot be read is named rather than typed, and
moving where the profile signs in is held to the re-point rule of every secret
field. A 1Password one-time password field resolves to its seed, from which the
platform's TOTP implementation computes the codes.
"""

from __future__ import annotations

import base64
import uuid
from types import SimpleNamespace

import pytest

from app.core import totp
from app.models.browser_profile import BrowserProfile
from app.services.browser_profiles import profiles
from app.services.vault.config import VaultProviderConfig
from tests.test_vault_references import FakeVault, _use, fake_redis  # noqa: F401 - fixture

_SEED = base64.b32encode(b"neutral-test-key-0001").decode()
_PASSWORD_REF = "vault://corp/apps/demo#password"
_USERNAME_REF = "vault://corp/apps/demo#username"
_TOTP_REF = "vault://corp/apps/demo#TOTP_k3j"


def _profile(**columns) -> BrowserProfile:
    return BrowserProfile(id=uuid.uuid4(), name="Demo", start_url="https://app.example.test/", **columns)


def _corp(**secrets) -> FakeVault:
    return FakeVault("corp", {"apps/demo": {"username": "demo@example.test", "password": "pw", "TOTP_k3j": _SEED, **secrets}})


def test_a_credential_may_be_a_vault_reference_and_a_totp_reference_is_checked_when_read():
    _use(_corp())
    profile = _profile()
    assert profiles.write_credentials(profile, username=_USERNAME_REF, password=_PASSWORD_REF, totp_secret=_TOTP_REF)
    assert profiles.read_credentials(profile) == {"username": _USERNAME_REF, "password": _PASSWORD_REF, "totp_secret": _TOTP_REF}
    assert profiles.referenced_credentials(profile) == {"username", "password", "totp_secret"}


def test_a_reference_to_a_vault_the_platform_does_not_know_is_refused_when_written():
    _use(_corp())
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.write_credentials(_profile(), password="vault://elsewhere/apps/demo#password")
    assert "elsewhere" in str(caught.value)


def test_a_1password_style_op_reference_is_still_refused_with_the_way_to_read_it():
    with pytest.raises(profiles.ProfileError) as caught:
        profiles.write_credentials(_profile(), totp_secret="op://Private/Entra/one-time password")
    assert "secret vault" in str(caught.value)


def test_the_synchronous_snapshot_never_hands_a_reference_over_as_a_value():
    _use(_corp())
    profile = _profile()
    profiles.write_credentials(profile, username="demo@example.test", password=_PASSWORD_REF, totp_secret=_TOTP_REF)
    snap = profiles.snapshot(profile)
    assert snap.credentials == {"username": "demo@example.test"}
    assert snap.referenced == {"password", "totp_secret"}
    assert snap.can_sign_in and snap.has_valid_totp and snap.renews_unattended


async def test_a_browser_task_receives_the_credentials_read_from_the_vault(fake_redis):  # noqa: F811
    vault = _corp()
    _use(vault)
    profile = _profile()
    profiles.write_credentials(profile, username=_USERNAME_REF, password=_PASSWORD_REF, totp_secret=_TOTP_REF)
    snap = await profiles.resolved_snapshot(profile)
    assert snap.credentials == {"username": "demo@example.test", "password": "pw", "totp_secret": _SEED}
    assert snap.referenced == frozenset() and snap.credential_failures == {} and snap.credential_problem is None
    assert totp.current_code(snap.totp_secret)
    assert vault.reads == 1  # one secret, read once for its three keys


async def test_a_credential_that_cannot_be_read_is_named_never_typed(fake_redis):  # noqa: F811
    _use(FakeVault("corp", {}))
    profile = _profile()
    profiles.write_credentials(profile, username="demo@example.test", password=_PASSWORD_REF)
    snap = await profiles.resolved_snapshot(profile)
    assert "password" not in snap.credentials
    assert snap.credential_failures == {"password": "not_found"}
    assert not snap.can_sign_in
    assert "password could not be read from its vault" in (snap.credential_problem or "")
    assert fake_redis.hashes, "the failure is recorded on the profile, shown on its page and in the Security Audit"


async def test_a_value_read_for_the_totp_secret_that_is_not_one_is_a_failure_of_its_own(fake_redis):  # noqa: F811
    _use(_corp(TOTP_k3j="not a seed"))
    profile = _profile()
    profiles.write_credentials(profile, username="u", password="p", totp_secret=_TOTP_REF)
    snap = await profiles.resolved_snapshot(profile)
    assert snap.credential_failures == {"totp_secret": profiles.INVALID_TOTP}
    assert not snap.has_valid_totp
    assert "not a base32 key" in (snap.credential_problem or "")


async def test_a_recording_step_typing_a_password_read_from_a_vault_is_refused(fake_redis):  # noqa: F811
    """The record tool's preflight compares the steps with the credentials as read, never with their references."""
    from app.services.video_studio import take_preflight
    from app.services.video_studio.spec import parse_take_script, validate_take_script

    _use(_corp(password="vault-held-password"))
    profile = _profile()
    profiles.write_credentials(profile, username="demo@example.test", password=_PASSWORD_REF)
    typed = parse_take_script(
        validate_take_script({"steps": [{"action": "type", "selector": "#note", "text": "vault-held-password"}]})
    )
    credentials = (await profiles.resolved_snapshot(profile)).credentials
    assert "stored credentials" in (take_preflight.credential_refusal(typed, credentials) or "")
    assert take_preflight.credential_refusal(typed, profiles.read_credentials(profile)) is None


def test_a_response_says_which_credentials_read_from_a_vault_never_the_reference():
    _use(_corp())
    profile = _profile()
    profiles.write_credentials(profile, username=_USERNAME_REF, password=_PASSWORD_REF, totp_secret=_TOTP_REF)
    data = profiles.serialize_profile(profile, can_manage=True)
    assert data["has_username"] and data["has_password"] and data["has_totp"]
    assert data["username_hint"] is None
    assert data["totp_secret_status"] == totp.SECRET_VAULT
    assert data["vault_credentials"] == ["password", "totp_secret", "username"]
    assert data["renews_by_itself"] is True
    assert "vault://" not in str(data)


def _env_provider(name: str = "corp") -> FakeVault:
    """A provider declared in the environment: only administrators may write a reference to it (WRITERS=admins)."""
    vault = FakeVault(name, {"apps/demo": {"password": "pw"}})
    vault.config = VaultProviderConfig(name=name, type="hashicorp", settings={})
    return vault


async def test_moving_where_a_profile_signs_in_is_held_to_the_rule_of_writing_its_reference(monkeypatch):
    from app.services.vault import store

    _use(_env_provider())
    profile = _profile(login_url="https://app.example.test/login")
    profiles.write_credentials(profile, username="demo@example.test", password=_PASSWORD_REF)
    member = SimpleNamespace(id=uuid.uuid4(), is_admin=False)
    admin = SimpleNamespace(id=uuid.uuid4(), is_admin=True)

    async def _principal(_db, user):
        return store.principal_from(user)

    monkeypatch.setattr(store, "vault_principal", _principal)
    before = profiles.destination(profile)
    assert await profiles.repoint_problem(None, member, profile, before) is None  # nothing moved

    profile.start_url = "https://attacker.example.test/"
    refused = await profiles.repoint_problem(None, member, profile, before)
    assert refused and "credentials holds a reference to vault provider 'corp'" in refused
    assert await profiles.repoint_problem(None, admin, profile, before) is None

    profiles.write_credentials(profile, password="typed-instead")
    assert await profiles.repoint_problem(None, member, profile, before) is None  # the same edit replaced it


def test_browser_profiles_are_scanned_for_where_a_vault_is_used():
    from app.services.vault import ENTITY_KINDS, REFERENCE_COLUMNS
    from app.services.vault.usage import KIND_LABELS, _models

    assert REFERENCE_COLUMNS["browser_profiles"] == ("credentials_enc",)
    assert ENTITY_KINDS["browser_profiles"] == "browser_profile"
    assert KIND_LABELS["browser_profile"] == "browser profile"
    assert _models()["browser_profiles"] is BrowserProfile
