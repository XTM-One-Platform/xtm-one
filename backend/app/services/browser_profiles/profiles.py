# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Browser profiles: an application's sign-in, its secrets and its saved session.

The ONE module that reads or writes a profile's secrets. Credentials
(``username`` / ``password`` / ``totp_secret``) and the saved session are
Fernet-encrypted (invariant 2) and write-only: a write may set, replace or
clear each one, and every reader (REST, agent tools) gets
:func:`serialize_profile`, which says whether each is set and never what it
is. A credential may be a vault reference instead: a browser task
receives :func:`resolved_snapshot`, which reads it through the vault resolver
when it is used, and :func:`snapshot` never hands a reference over as a value.
Using a profile (browsing or recording with it, signing it in) needs the
caller to see it through the normal three-tier predicate WITHOUT the admin
bypass: like targeting a runner, browsing with someone's private login is an
execution power, not an administration one. Managing it (editing, deleting,
sharing) follows ``can_manage``.

Whether a session is signed in is the answer of a probe, kept by
:func:`record_probe`: ``active`` only when the start URL opened in the saved
session rendered the application, ``expired`` with the URL it landed on
otherwise. Saving a session without probing it leaves it ``unverified``, and
:func:`effective_session_status` never reads a session as signed in from its
cookies.
"""

from __future__ import annotations

import dataclasses
import json
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import totp
from app.core.secret_refs import is_secret_reference
from app.core.security import decrypt_json, encrypt_value
from app.models.browser_profile import (
    KEEPALIVE_MINUTES_DEFAULT,
    MFA_TIMEOUT_DEFAULT,
    SESSION_ACTIVE,
    SESSION_AWAITING_MFA,
    SESSION_EXPIRED,
    SESSION_NONE,
    SESSION_SAVE_TIMEOUT_DEFAULT,
    SESSION_UNVERIFIED,
    SIGN_IN_TIMEOUT_DEFAULT,
    BrowserProfile,
)
from app.services.browser_profiles.browser import TargetPolicy, browser_options, normalise_hosts, target_policy

#: Selectors a profile may give the sign-in, tried before its own heuristics.
LOGIN_CONFIG_KEYS: tuple[str, ...] = (
    "username_selector",
    "password_selector",
    "submit_selector",
    "otp_selector",
    "otp_submit_selector",
    "confirm_selector",
    "signed_in_selector",
    "signed_in_url_pattern",
    "signed_out_url_pattern",
    "keep_storage_keys",
)
#: The ``localStorage`` / ``sessionStorage`` keys the application keeps its session in: what a recording's
#: page state reset keeps (``capture.reset_page``). A list, or one string of names separated by commas or newlines.
KEEP_STORAGE_KEYS = "keep_storage_keys"
_MAX_KEPT_KEYS = 30
_KEPT_KEY_SPLIT = re.compile(r"[,\n]")
KEEPALIVE_CHOICES: tuple[int, ...] = (0, 30, 60, 120, 240, 480, 720, 1440)
#: The credentials a profile holds, in ``credentials_enc``.
CREDENTIAL_KEYS: tuple[str, ...] = ("username", "password", "totp_secret")
#: How a credential is named in a message.
CREDENTIAL_LABELS: dict[str, str] = {"username": "username", "password": "password", "totp_secret": "TOTP secret"}
#: A TOTP secret read from a vault that is not a base32 key or an otpauth:// URI (a credential failure of its own).
INVALID_TOTP = "invalid_totp"
#: The bounds a profile's timeouts are held to (seconds), by column.
TIMEOUT_BOUNDS: dict[str, tuple[int, int, int]] = {
    "sign_in_timeout_seconds": (30, 600, SIGN_IN_TIMEOUT_DEFAULT),
    "session_save_timeout_seconds": (5, 120, SESSION_SAVE_TIMEOUT_DEFAULT),
    "mfa_timeout_seconds": (60, 900, MFA_TIMEOUT_DEFAULT),
}
_UNSET: Any = object()


class ProfileError(ValueError):
    """A profile write the platform refuses; ``str()`` is safe to show."""


def _check_url(value: str | None, label: str, *, required: bool) -> str | None:
    text = (value or "").strip()
    if not text:
        if required:
            raise ProfileError(f"{label} is required.")
        return None
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ProfileError(f"{label} must be an http(s) URL.")
    if parsed.username or parsed.password:
        raise ProfileError(f"{label} must not carry credentials; give them as the profile's username and password.")
    if len(text) > 2000:
        raise ProfileError(f"{label} is at most 2000 characters.")
    return text


def _kept_keys(raw: Any) -> list[str]:
    """``keep_storage_keys`` as stored: each storage key once, in the order given."""
    if raw is None:
        return []
    values = _KEPT_KEY_SPLIT.split(raw) if isinstance(raw, str) else raw
    if not isinstance(values, list | tuple):
        raise ProfileError(f"login_config.{KEEP_STORAGE_KEYS} is a list of storage keys.")
    kept: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise ProfileError(f"login_config.{KEEP_STORAGE_KEYS} is a list of storage keys.")
        name = value.strip()
        if len(name) > 200:
            raise ProfileError(f"A key of login_config.{KEEP_STORAGE_KEYS} is at most 200 characters.")
        if name and name not in kept:
            kept.append(name)
    if len(kept) > _MAX_KEPT_KEYS:
        raise ProfileError(f"login_config.{KEEP_STORAGE_KEYS} keeps at most {_MAX_KEPT_KEYS} keys.")
    return kept


def clean_login_config(value: dict | None) -> dict | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise ProfileError("login_config is an object of selectors.")
    cleaned: dict[str, str | list[str]] = {}
    for key, raw in value.items():
        if key not in LOGIN_CONFIG_KEYS:
            raise ProfileError(f"login_config.{key} is not a known setting ({', '.join(LOGIN_CONFIG_KEYS)}).")
        if key == KEEP_STORAGE_KEYS:
            if kept := _kept_keys(raw):
                cleaned[key] = kept
            continue
        text = str(raw or "").strip()
        if text:
            if len(text) > 500:
                raise ProfileError(f"login_config.{key} is at most 500 characters.")
            cleaned[key] = text
    return cleaned or None


def clean_browser_options(value: dict | None) -> dict | None:
    if not value:
        return None
    if not isinstance(value, dict):
        raise ProfileError("browser_options is an object.")
    allowed = {"color_scheme", "locale", "timezone"}
    unknown = set(value) - allowed
    if unknown:
        raise ProfileError(f"browser_options accepts {', '.join(sorted(allowed))}.")
    options = browser_options(value)
    return {k: v for k, v in options.items() if v}


#: How many other applications one profile may declare, and how long an application's name may be.
MAX_APPS = 20
_APP_NAME_MAX = 200
#: The marks of the profile's own application: another application declared on it is judged by its structure.
_APP_MARKS = ("signed_in_selector", "signed_in_url_pattern", "signed_out_url_pattern")


def clean_apps(value: Any) -> list[dict[str, str]]:
    """``apps`` as stored: each other application the session signs in, ``{name, url}``, one per host."""
    if value in (None, ""):
        return []
    if not isinstance(value, list | tuple):
        raise ProfileError("apps is a list of {name, url}.")
    if len(value) > MAX_APPS:
        raise ProfileError(f"A profile declares at most {MAX_APPS} other applications.")
    apps: list[dict[str, str]] = []
    hosts: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"name", "url"}:
            raise ProfileError("Each of apps is {name, url}.")
        url = _check_url(str(item.get("url") or ""), "apps.url", required=True) or ""
        host = (urlparse(url).hostname or "").lower().rstrip(".")
        if host in hosts:
            continue
        hosts.add(host)
        name = str(item.get("name") or "").strip()[:_APP_NAME_MAX] or host
        apps.append({"name": name, "url": url})
    return apps


def app_hosts(apps: list | None) -> list[str]:
    """The hosts of a profile's declared applications."""
    return normalise_hosts([str(app.get("url") or "") for app in apps or [] if isinstance(app, dict)])


def covers_url(profile: BrowserProfile, url: str) -> bool:
    """Whether *url*'s host is the profile's application or one it declares (a parent domain given matches)."""
    text = (url or "").strip().lower()
    if not text:
        return False
    host = (urlparse(text if "://" in text else f"https://{text}").hostname or "").rstrip(".")
    if not host:
        return False
    hosts = normalise_hosts([profile.start_url, profile.login_url or ""]) + app_hosts(profile.apps)
    return any(h == host or h.endswith("." + host) for h in hosts)


@dataclass(frozen=True)
class ProfileFields:
    name: str
    start_url: str
    login_url: str | None
    allowed_hosts: list[str]
    login_config: dict | None
    browser_options: dict | None
    description: str | None
    keepalive_minutes: int
    #: Column -> seconds; a timeout left out keeps the profile's (its default on a new one).
    timeouts: dict[str, int] = field(default_factory=dict)
    #: The other applications the session signs in (:func:`clean_apps`).
    apps: list[dict[str, str]] = field(default_factory=list)


def clean_timeouts(values: dict[str, Any] | None) -> dict[str, int]:
    """The timeouts given (``None`` = keep), each a whole number of seconds within its bounds."""
    cleaned: dict[str, int] = {}
    for key, value in (values or {}).items():
        if value is None:
            continue
        if key not in TIMEOUT_BOUNDS:
            raise ProfileError(f"{key} is not a profile timeout ({', '.join(TIMEOUT_BOUNDS)}).")
        low, high, _default = TIMEOUT_BOUNDS[key]
        try:
            seconds = int(value)
        except (TypeError, ValueError):
            raise ProfileError(f"{key} is a number of seconds.") from None
        if not low <= seconds <= high:
            raise ProfileError(f"{key} is between {low} and {high} seconds.")
        cleaned[key] = seconds
    return cleaned


def clean_fields(
    *,
    name: str,
    start_url: str,
    login_url: str | None = None,
    allowed_hosts: list[str] | None = None,
    login_config: dict | None = None,
    options: dict | None = None,
    description: str | None = None,
    keepalive_minutes: int = KEEPALIVE_MINUTES_DEFAULT,
    timeouts: dict[str, Any] | None = None,
    apps: Any = None,
) -> ProfileFields:
    clean_name = (name or "").strip()
    if not clean_name:
        raise ProfileError("A profile needs a name.")
    if len(clean_name) > 200:
        raise ProfileError("A profile name is at most 200 characters.")
    keepalive = int(keepalive_minutes or 0)
    if keepalive not in KEEPALIVE_CHOICES:
        raise ProfileError(f"keepalive_minutes is one of {', '.join(map(str, KEEPALIVE_CHOICES))}.")
    return ProfileFields(
        name=clean_name,
        start_url=_check_url(start_url, "start_url", required=True) or "",
        login_url=_check_url(login_url, "login_url", required=False),
        allowed_hosts=normalise_hosts(allowed_hosts),
        login_config=clean_login_config(login_config),
        browser_options=clean_browser_options(options),
        description=(description or "").strip()[:2000] or None,
        keepalive_minutes=keepalive,
        timeouts=clean_timeouts(timeouts),
        apps=clean_apps(apps),
    )


def apply_fields(profile: BrowserProfile, fields: ProfileFields) -> None:
    profile.name = fields.name
    profile.start_url = fields.start_url
    profile.login_url = fields.login_url
    profile.allowed_hosts = fields.allowed_hosts or None
    profile.apps = fields.apps or None
    profile.login_config = fields.login_config
    profile.browser_options = fields.browser_options
    profile.description = fields.description
    profile.keepalive_minutes = fields.keepalive_minutes
    for key, seconds in fields.timeouts.items():
        setattr(profile, key, seconds)


def destination(profile: BrowserProfile) -> tuple[str, str | None, list[str]]:
    """Where the profile types its credentials: the URLs it signs in at, the hosts it may reach."""
    return (profile.start_url, profile.login_url, sorted(profile.allowed_hosts or []) + app_hosts(profile.apps))


async def repoint_problem(db: AsyncSession, user: Any, profile: BrowserProfile, before: tuple) -> str | None:
    """Why *user* may not move where *profile* signs in (from *before*, its :func:`destination`), or ``None``.

    A credential read from a vault is typed wherever the profile signs in, so
    moving that while the profile keeps a reference *user* could not have
    written is refused unless the same edit replaced it - the rule of every
    secret field (``retained_reference_writer_problem``). The REST route
    and the agent tools both ask it.
    """
    if destination(profile) == before:
        return None
    from app.services.vault import retained_reference_writer_problem
    from app.services.vault.store import vault_principal

    return retained_reference_writer_problem(profile, principal=await vault_principal(db, user))


def timeout_of(profile: BrowserProfile, key: str) -> int:
    """A profile's timeout (seconds), its default when the row predates the column or holds none."""
    value = getattr(profile, key, None)
    return TIMEOUT_BOUNDS[key][2] if value is None else max(1, int(value))


# ── Secrets ───────────────────────────────────────────────────────────────


def read_credentials(profile: BrowserProfile) -> dict[str, str]:
    """The credentials as stored: one held as a vault reference is the ``vault://`` reference.

    For a write to merge into and for what a reader may say of them (set, read
    from a vault). A browser receives :func:`resolved_snapshot`'s credentials.
    """
    data = decrypt_json(profile.credentials_enc) if profile.credentials_enc else None
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in CREDENTIAL_KEYS and v}


def referenced_credentials(profile: BrowserProfile) -> frozenset[str]:
    """The credentials *profile* holds as vault references, by name."""
    return frozenset(key for key, value in read_credentials(profile).items() if is_secret_reference(value))


def write_credentials(
    profile: BrowserProfile,
    *,
    username: Any = _UNSET,
    password: Any = _UNSET,
    totp_secret: Any = _UNSET,
) -> bool:
    """Set, replace or clear (``""`` / ``None``) each credential; ``True`` when something changed.

    A credential may be a vault reference (``vault://<provider>/<path>#<key>``):
    it is checked as every secret field checks one (``reference_problem``), and
    a TOTP secret read from a vault is checked when it is read. Whether the
    writer may name that vault path is the route's check (``require_vault_writer``).
    """
    from app.services.vault import reference_problem

    current = read_credentials(profile)
    updated = dict(current)
    for key, value in (("username", username), ("password", password), ("totp_secret", totp_secret)):
        if value is _UNSET:
            continue
        text = "" if value is None else str(value)
        if key != "password" or is_secret_reference(text):
            text = text.strip()
        if not text:
            updated.pop(key, None)
            continue
        if len(text) > 4000:
            raise ProfileError(f"The {CREDENTIAL_LABELS[key]} is too long.")
        if is_secret_reference(text):
            if problem := reference_problem(text):
                raise ProfileError(f"The {CREDENTIAL_LABELS[key]}: {problem}.")
        elif key == "totp_secret":
            try:
                totp.validate_secret(text)
            except totp.TOTPSecretError as exc:
                raise ProfileError(str(exc)) from None
        updated[key] = text
    if updated == current:
        return False
    profile.credentials_enc = encrypt_value(json.dumps(updated)) if updated else None
    # New credentials: whether they sign in without a person is learnt again by the next sign-in.
    profile.signs_in_unattended = None
    return True


def read_storage_state(profile: BrowserProfile) -> dict | None:
    data = decrypt_json(profile.storage_state_enc) if profile.storage_state_enc else None
    return data if isinstance(data, dict) else None


def save_storage_state(
    profile: BrowserProfile, state: dict | None, *, probe: dict | None = None, message: str | None = None
) -> None:
    """Keep *state* as the profile's session (or forget it with ``None``), with the *probe* of it when one ran.

    Without a probe the session is ``unverified``: a saved session is signed
    in only when the start URL opened in it rendered the application.
    """
    now = datetime.now(UTC)
    if not state:
        profile.storage_state_enc = None
        profile.session_status = SESSION_NONE
        profile.session_saved_at = None
        profile.session_checked_at = now
        profile.last_probe_at = None
        profile.last_probe_result = None
    else:
        profile.storage_state_enc = encrypt_value(json.dumps(state))
        profile.session_saved_at = now
        if probe is not None:
            record_probe(profile, probe)
        else:
            profile.session_status = SESSION_UNVERIFIED
            profile.session_checked_at = now
    profile.mfa_challenge_id = None
    profile.mfa_challenge_kind = None
    profile.mfa_expires_at = None
    profile.mfa_screenshot_file_id = None
    if message is not None:
        profile.last_sign_in_message = message[:1000]


def record_probe(profile: BrowserProfile, probe: dict) -> None:
    """Keep the probe of the saved session: ``active`` when it rendered the application, else ``expired``.

    A probe the application could not answer (``http_status``) leaves the
    session ``unverified``: it says nothing of it.
    """
    now = datetime.now(UTC)
    result = {
        "authenticated": bool(probe.get("authenticated")),
        "url": str(probe.get("url") or "")[:2000],
        "reason": str(probe.get("reason") or "")[:500],
        "checked_at": str(probe.get("checked_at") or now.isoformat()),
        "http_status": probe.get("http_status") if isinstance(probe.get("http_status"), int) else None,
    }
    profile.last_probe_at = now
    profile.last_probe_result = result
    profile.session_checked_at = now
    if profile.storage_state_enc is None:
        profile.session_status = SESSION_NONE
    elif result["http_status"] is not None:
        profile.session_status = SESSION_UNVERIFIED
    else:
        profile.session_status = SESSION_ACTIVE if result["authenticated"] else SESSION_EXPIRED


def effective_session_status(profile: BrowserProfile) -> str:
    """The status every reader shows: ``active`` needs a probe that rendered the application."""
    status = profile.session_status or SESSION_NONE
    if status == SESSION_ACTIVE and not (profile.last_probe_result or {}).get("authenticated"):
        return SESSION_UNVERIFIED
    return status


def _secret_status(secret: str | None) -> str:
    """A TOTP secret's health: ``vault`` for one read from a vault when it is used (checked then)."""
    if secret and is_secret_reference(secret):
        return totp.SECRET_VAULT
    return totp.secret_status(secret)


def totp_secret_status(profile: BrowserProfile) -> str:
    """The stored TOTP secret's health (``none`` | ``valid`` | ``invalid`` | ``vault``), for the status and the tools."""
    return _secret_status(read_credentials(profile).get("totp_secret"))


def renews_by_itself(profile: BrowserProfile) -> bool:
    """Whether the platform renews this profile's session unattended: what
    :attr:`ProfileSnapshot.renews_unattended` reads, for the card and every reader."""
    return snapshot(profile).renews_unattended


def mask_username(username: str | None) -> str | None:
    if not username or is_secret_reference(username):
        return None
    if "@" in username:
        local, _, domain = username.partition("@")
        return f"{local[:1]}***@{domain}"
    return f"{username[:2]}***" if len(username) > 2 else "***"


# ── Snapshot handed to a browser task ─────────────────────────────────────


@dataclass(frozen=True)
class ProfileSnapshot:
    """What a sign-in, a take, an agent's browser or a keep-alive needs of a profile, without its ORM row."""

    id: str
    name: str
    start_url: str
    login_url: str | None
    login_config: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    #: The credentials a browser may type: never a vault reference.
    credentials: dict = field(default_factory=dict)
    storage_state: dict | None = None
    policy: TargetPolicy = field(default_factory=TargetPolicy)
    sign_in_timeout: float = float(SIGN_IN_TIMEOUT_DEFAULT)
    session_save_timeout: float = float(SESSION_SAVE_TIMEOUT_DEFAULT)
    mfa_timeout: float = float(MFA_TIMEOUT_DEFAULT)
    #: Credentials stored as vault references and not read (:func:`snapshot`; :func:`resolved_snapshot` reads them).
    referenced: frozenset[str] = frozenset()
    #: Credentials whose vault read failed, with the vault's reason (or :data:`INVALID_TOTP`).
    credential_failures: dict[str, str] = field(default_factory=dict)
    #: The last sign-in with these credentials: ``True`` it needed no person, ``False`` it asked one (an MFA
    #: code with no TOTP secret, an approval), ``None`` none ran since they were set
    #: (``BrowserProfile.signs_in_unattended``).
    signs_in_unattended: bool | None = None
    #: The other applications the session signs in (``BrowserProfile.apps``), ``{name, url}`` each.
    apps: tuple[dict, ...] = ()

    def app_for(self, url: str | None) -> dict | None:
        """The declared application whose host *url* is on, or ``None`` (the profile's own application, or none)."""
        host = (urlparse(url or "").hostname or "").lower().rstrip(".")
        if not host or host == self.app_host:
            return None
        return next((app for app in self.apps if app_hosts([app]) == [host]), None)

    def as_app(self, app: dict) -> ProfileSnapshot:
        """This session judged as the declared application *app*: its URL is the start URL, nothing to sign in at.

        The profile signs in at its own application; another one opens signed in through the same
        identity-provider session, so it is probed by its structure alone (the own application's marks and
        sign-in URL do not apply to it).
        """
        config = {key: value for key, value in self.login_config.items() if key not in _APP_MARKS}
        return dataclasses.replace(self, start_url=str(app.get("url") or ""), login_url=None, login_config=config)

    def scoped_to(self, url: str | None) -> ProfileSnapshot:
        """This session as the application *url* is on: a declared application's, else the profile's own."""
        app = self.app_for(url)
        return self.as_app(app) if app is not None else self

    def _has(self, key: str) -> bool:
        return bool(self.credentials.get(key)) or key in self.referenced

    @property
    def can_sign_in(self) -> bool:
        return self._has("username") and self._has("password")

    @property
    def totp_secret(self) -> str | None:
        return self.credentials.get("totp_secret") or None

    @property
    def has_valid_totp(self) -> bool:
        """Whether the TOTP secret can compute codes (base32 or an otpauth:// URI), or is read from a vault when used."""
        return "totp_secret" in self.referenced or totp.secret_status(self.totp_secret) == totp.SECRET_VALID

    @property
    def credential_problem(self) -> str | None:
        """Why a credential this profile signs in with could not be read from its vault, for a message; ``None`` when none failed."""
        from app.services.vault import REASON_DESCRIPTIONS

        problems = []
        for key in CREDENTIAL_KEYS:
            reason = self.credential_failures.get(key)
            if reason is None:
                continue
            if reason == INVALID_TOTP:
                problems.append("the TOTP secret read from its vault is not a base32 key or an otpauth:// URI")
            else:
                why = REASON_DESCRIPTIONS.get(reason, reason)
                problems.append(f"the {CREDENTIAL_LABELS[key]} could not be read from its vault ({why})")
        return "; ".join(problems) or None

    @property
    def renews_unattended(self) -> bool:
        """Whether the platform can complete a sign-in with no person, the default for every profile.

        Stored credentials with a valid TOTP secret; stored credentials whose
        last sign-in, dry run or keep-alive renewal reached the application
        asking no person (a local login with no MFA), or that no sign-in has
        tried since they were set (the default: the platform tries, and the
        first sign-in that has to ask a person for a code or an approval
        clears it, ``signs_in_unattended = False``); or a saved
        identity-provider session to replay on the sign-in URL.
        """
        if self.can_sign_in:
            if self.has_valid_totp or self.signs_in_unattended:
                return True
            # Not tried yet: a stored TOTP secret that cannot compute codes says the sign-in asks for one.
            untried = self.signs_in_unattended is None
            if untried and not self.totp_secret and "totp_secret" not in self.credential_failures:
                return True
        return bool(self.login_url and self.holds_idp_session)

    @property
    def app_host(self) -> str:
        return (urlparse(self.start_url).hostname or "").lower()

    @property
    def keep_storage_keys(self) -> tuple[str, ...]:
        """The storage keys the application keeps its session in: a page state reset keeps them."""
        kept = self.login_config.get(KEEP_STORAGE_KEYS)
        return tuple(key for key in kept if isinstance(key, str)) if isinstance(kept, list) else ()

    @property
    def needs_sign_in(self) -> bool:
        """Whether the profile stands for a signed-in application at all (a public site's profile does not)."""
        cfg = self.login_config
        return bool(
            self.login_url
            or self._has("username")
            or self._has("password")
            or self.storage_state
            or cfg.get("signed_in_selector")
            or cfg.get("signed_in_url_pattern")
        )

    @property
    def holds_idp_session(self) -> bool:
        """Whether the saved session carries cookies of another site than the application's (an IdP's)."""
        app = self.app_host
        for cookie in (self.storage_state or {}).get("cookies") or []:
            domain = str(cookie.get("domain") or "").lstrip(".").lower()
            if domain and app != domain and not app.endswith("." + domain):
                return True
        return False

    @property
    def renewable(self) -> bool:
        """Whether the platform may sign in again by itself: credentials, or a saved IdP session to replay."""
        return self.can_sign_in or bool(self.login_url and self.holds_idp_session)


def snapshot(profile: BrowserProfile) -> ProfileSnapshot:
    """What the status flags read of *profile*: a credential held as a vault reference is named, never handed over.

    A browser task receives :func:`resolved_snapshot` instead
    (``tests/test_vault_secret_readers.py`` keeps it that way).
    """
    stored = read_credentials(profile)
    return ProfileSnapshot(
        id=str(profile.id),
        name=profile.name,
        start_url=profile.start_url,
        login_url=profile.login_url,
        login_config=dict(profile.login_config or {}),
        options=browser_options(profile.browser_options),
        credentials={key: value for key, value in stored.items() if not is_secret_reference(value)},
        storage_state=read_storage_state(profile),
        policy=target_policy(
            start_url=profile.start_url,
            login_url=profile.login_url,
            allowed_hosts=list(profile.allowed_hosts or []) + app_hosts(profile.apps),
        ),
        sign_in_timeout=float(timeout_of(profile, "sign_in_timeout_seconds")),
        session_save_timeout=float(timeout_of(profile, "session_save_timeout_seconds")),
        mfa_timeout=float(timeout_of(profile, "mfa_timeout_seconds")),
        referenced=frozenset(key for key, value in stored.items() if is_secret_reference(value)),
        signs_in_unattended=profile.signs_in_unattended,
        apps=tuple(dict(app) for app in profile.apps or [] if isinstance(app, dict)),
    )


async def resolved_snapshot(profile: BrowserProfile) -> ProfileSnapshot:
    """What a browser task (a sign-in, a session check, an agent's browser, a recording, the keep-alive) receives.

    Every credential held as a vault reference is read from its vault now,
    through the one resolver (``app.services.vault``): the platform's vault
    identity reads it for whoever may use the profile, as for any entity's
    secret. One that cannot be read is left out and named in
    ``credential_failures`` (recorded on the profile, shown on its page); a
    TOTP secret that is not a base32 key or an otpauth:// URI likewise.
    """
    from app.services.vault import SecretUnavailableError, owner_of, resolve_secret_text

    snap = snapshot(profile)
    if not snap.referenced:
        return snap
    stored = read_credentials(profile)
    owner = owner_of(profile, "credentials_enc")
    credentials = dict(snap.credentials)
    failures: dict[str, str] = {}
    for key in CREDENTIAL_KEYS:
        if key not in snap.referenced:
            continue
        try:
            value = await resolve_secret_text(stored[key], owner)
        except SecretUnavailableError as exc:
            failures[key] = exc.reason
            continue
        if key == "totp_secret" and totp.secret_status(value) != totp.SECRET_VALID:
            failures[key] = INVALID_TOTP
            continue
        credentials[key] = value
    return dataclasses.replace(snap, credentials=credentials, referenced=frozenset(), credential_failures=failures)


# ── Loading ───────────────────────────────────────────────────────────────


async def load_usable_profile(db: AsyncSession, profile_id: object, user_id: uuid.UUID) -> BrowserProfile | None:
    """The profile *user_id* may browse, record or sign in with (no admin bypass), or ``None``."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.utils import safe_uuid

    pid = safe_uuid(str(profile_id or "").strip())
    if pid is None:
        return None
    groups = await get_user_group_ids(db, user_id)
    result = await db.execute(
        select(BrowserProfile).where(
            BrowserProfile.id == pid, user_visible_filter(BrowserProfile, user_id, groups, is_admin=False)
        )
    )
    return result.scalar_one_or_none()


async def resolve_profile_id_prefix(db: AsyncSession, profile_id: str, user_id: uuid.UUID) -> tuple[str, str | None]:
    """*profile_id* with an id prefix (8+ hexadecimal characters) of one
    profile *user_id* may use taken for its full id, and the refusal naming
    the candidates when it starts several (``builtin_tools/id_prefix.py``).
    The tools call it; the REST routes keep the full id."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.services.builtin_tools.id_prefix import id_prefix, resolve_id_prefix

    if id_prefix(profile_id) is None:
        return profile_id, None
    groups = await get_user_group_ids(db, user_id)
    return await resolve_id_prefix(
        db,
        BrowserProfile,
        profile_id,
        [user_visible_filter(BrowserProfile, user_id, groups, is_admin=False)],
        noun="browser profiles",
    )


async def list_usable_profiles(
    db: AsyncSession, user_id: uuid.UUID, *, limit: int = 100, ids: Iterable[object] | None = None
) -> list[BrowserProfile]:
    """The profiles *user_id* may use (no admin bypass), by name; only those among *ids* when given."""
    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.utils import safe_uuid

    clauses = [user_visible_filter(BrowserProfile, user_id, await get_user_group_ids(db, user_id), is_admin=False)]
    if ids is not None:
        wanted = {pid for pid in (safe_uuid(str(raw)) for raw in ids if raw) if pid is not None}
        if not wanted:
            return []
        clauses.append(BrowserProfile.id.in_(wanted))
    result = await db.execute(select(BrowserProfile).where(*clauses).order_by(BrowserProfile.name).limit(limit))
    return list(result.scalars().all())


# ── Serialisation ─────────────────────────────────────────────────────────


def mfa_challenge(profile: BrowserProfile) -> dict | None:
    if profile.session_status != SESSION_AWAITING_MFA or not profile.mfa_challenge_id:
        return None
    expires = profile.mfa_expires_at
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    if expires is not None and expires < datetime.now(UTC):
        return None
    return {
        "challenge_id": profile.mfa_challenge_id,
        "kind": profile.mfa_challenge_kind or "code",
        "expires_at": expires.isoformat() if expires else None,
        "screenshot_file_id": str(profile.mfa_screenshot_file_id) if profile.mfa_screenshot_file_id else None,
    }


def serialize_profile(profile: BrowserProfile, *, can_manage: bool) -> dict:
    """A profile as every reader sees it: whether each secret is set, never its value."""
    credentials = read_credentials(profile)
    return {
        "id": str(profile.id),
        "user_id": str(profile.user_id) if profile.user_id else None,
        "name": profile.name,
        "description": profile.description,
        "start_url": profile.start_url,
        "login_url": profile.login_url,
        "allowed_hosts": list(profile.allowed_hosts or []),
        "apps": [dict(app) for app in profile.apps or [] if isinstance(app, dict)],
        "login_config": dict(profile.login_config or {}),
        "browser_options": dict(profile.browser_options or {}),
        "username_hint": mask_username(credentials.get("username")),
        "has_username": bool(credentials.get("username")),
        "has_password": bool(credentials.get("password")),
        "has_totp": bool(credentials.get("totp_secret")),
        "totp_secret_status": _secret_status(credentials.get("totp_secret")),
        #: The credentials read from a vault when used, by name (where: the route's ``vault_secrets``).
        "vault_credentials": sorted(key for key, value in credentials.items() if is_secret_reference(value)),
        "renews_by_itself": renews_by_itself(profile),
        "session_status": effective_session_status(profile),
        "session_saved_at": profile.session_saved_at.isoformat() if profile.session_saved_at else None,
        "session_checked_at": profile.session_checked_at.isoformat() if profile.session_checked_at else None,
        "last_probe_at": profile.last_probe_at.isoformat() if profile.last_probe_at else None,
        "last_probe_result": dict(profile.last_probe_result) if profile.last_probe_result else None,
        "last_sign_in_message": profile.last_sign_in_message,
        "mfa_challenge": mfa_challenge(profile),
        "keepalive_minutes": (
            profile.keepalive_minutes if profile.keepalive_minutes is not None else KEEPALIVE_MINUTES_DEFAULT
        ),
        **{key: timeout_of(profile, key) for key in TIMEOUT_BOUNDS},
        "is_company_managed": bool(profile.is_company_managed),
        "shared_group_ids": list(profile.shared_group_ids or []),
        "shared_user_ids": list(profile.shared_user_ids or []),
        "can_manage": can_manage,
        "created_at": profile.created_at.isoformat() if profile.created_at else None,
        "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
    }
