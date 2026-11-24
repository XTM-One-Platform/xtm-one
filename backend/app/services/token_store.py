# SPDX-License-Identifier: Apache-2.0

"""Ephemeral token storage in Redis.

Invitation and password-reset tokens are short-lived, single-use secrets. They
live in Redis with a TTL so they auto-expire (no stale rows, no cleanup job).
Only the SHA-256 hash of the raw token is used in the key, so a Redis dump
never reveals a usable token. The raw token is returned once at creation.

Key layout:
    invite:{token_hash}   -> JSON {email, role, created_by_id, created_at, expires_at[, destination]}
    pwreset:{token_hash}  -> JSON {user_id, email, created_by_id, created_at, expires_at}
    emailchange:{user_id} -> JSON {id, email, mac, created_at}
    emailchange-attempts:{id} -> wrong-code counter of that pending change
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core.config import settings
from app.core.rate_limit import INCR_EXPIRE_LUA
from app.core.redis import redis_client
from app.core.security import hash_token, timing_safe_str_eq

_INVITE_PREFIX = "invite:"
_RESET_PREFIX = "pwreset:"
_EMAIL_CHANGE_PREFIX = "emailchange:"
_EMAIL_CHANGE_ATTEMPTS_PREFIX = "emailchange-attempts:"

#: Moves an invitation to its new key only while the old one still holds what
#: was read: one script, so the invitation is never lost between the two keys
#: and of two concurrent reissues exactly one wins.
#: Deletes a key only while it still holds ARGV[1]: a pending email change
#: replaced meanwhile by a newer request is left alone.
DELETE_IF_EQUAL_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

REISSUE_INVITATION_LUA = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return 0
end
redis.call('DEL', KEYS[1])
redis.call('SET', KEYS[2], ARGV[2], 'EX', ARGV[3])
return 1
"""


def _new_token() -> tuple[str, str]:
    """Return (raw_token, token_hash)."""
    raw = secrets.token_urlsafe(32)
    return raw, hash_token(raw)


# ---------------------------------------------------------------------------
# Invitations
# ---------------------------------------------------------------------------


async def create_invitation(
    *, email: str, role: str, created_by_id: Any, ttl_hours: int, destination: dict | None = None
) -> tuple[str, str, datetime]:
    """Store a new invitation. Returns (raw_token, token_hash, expires_at).

    The email is normalised (``.strip().lower()``) before being stored so the
    payload read back by :func:`get_invitation` / :func:`consume_invitation`
    is already in the canonical form used by every downstream consumer:
    ``find_invitation_by_email`` (the dedup check, already case-insensitive)
    and ``register-with-invitation`` (the exists-check, which must agree
    with the issuance-time check in ``invitations.create_invitation``).
    Without this normalisation an admin inviting ``Foo@Example.com`` would
    end up with a Redis payload still carrying mixed case, and the
    downstream ``User.email == invitation['email']`` lookup would be
    case-sensitive against a case-insensitive issuance check — letting
    duplicate accounts slip through on case-different invites.

    *destination* is where the person goes once registered: a registered
    platform's identifier and a path on it (``platform_access_notices``).
    """
    raw, token_hash = _new_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=ttl_hours)
    payload = {
        "email": email.strip().lower(),
        "role": role,
        "created_by_id": str(created_by_id) if created_by_id else None,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    if destination is not None:
        payload["destination"] = destination
    r = await redis_client()
    await r.set(_INVITE_PREFIX + token_hash, json.dumps(payload), ex=ttl_hours * 3600)
    return raw, token_hash, expires_at


async def reissue_invitation(
    token_hash: str, *, ttl_hours: int, destination: dict | None = None
) -> tuple[str, str, datetime] | None:
    """A new link for the pending invitation *token_hash*: same email and role, valid at least *ttl_hours*.

    Only the hash of a link is stored, so the link an invitation was sent with
    cannot be sent again: the invitation moves to a new token and the old link
    stops working, which keeps one pending invitation per email. The new link
    lives the longer of what the invitation had left and *ttl_hours*, so it is
    never sent nearly expired and a longer lifetime chosen at creation is kept.
    *destination* replaces the stored one when given. ``None`` when the
    invitation is gone (consumed, revoked, expired or reissued meanwhile).
    """
    r = await redis_client()
    key = _INVITE_PREFIX + token_hash
    val = await r.get(key)
    remaining = await r.ttl(key)
    if not val or remaining is None or remaining <= 0:
        return None
    payload = json.loads(val)
    if destination is not None:
        payload["destination"] = destination
    ttl = max(remaining, ttl_hours * 3600)
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl)
    payload["expires_at"] = expires_at.isoformat()
    raw, new_hash = _new_token()
    moved = await r.eval(REISSUE_INVITATION_LUA, 2, key, _INVITE_PREFIX + new_hash, val, json.dumps(payload), ttl)
    return (raw, new_hash, expires_at) if moved else None


def invitation_link(raw_token: str) -> str:
    """The frontend ``/register`` page for *raw_token*.

    ``frontend_url`` when the frontend lives on its own origin (as for
    ``oauth_callbacks`` and ``auth_sso``), else ``base_url``.
    """
    frontend_base = (settings.frontend_url or settings.base_url).rstrip("/")
    return f"{frontend_base}/register?token={raw_token}"


async def get_invitation(raw_token: str) -> dict | None:
    """Peek at an invitation by its raw token (does not consume it).

    Used by ``GET /api/v1/invitations/validate/{token}`` to render the
    register page without burning the link, and as the validate phase of
    ``register_with_invitation`` (the token is consumed atomically *after*
    the user row is committed via :func:`consume_invitation` so a commit
    failure cannot burn the invite).
    """
    r = await redis_client()
    val = await r.get(_INVITE_PREFIX + hash_token(raw_token))
    return json.loads(val) if val else None


async def consume_invitation(raw_token: str) -> dict | None:
    """Atomically read-and-delete an invitation by its raw token.

    Uses Redis ``GETDEL`` (Redis 6.2+) so two concurrent callers cannot
    both succeed: exactly one returns the payload, the other returns
    ``None``.  This is the right primitive for single-use enforcement at
    the consumption point — a non-atomic ``get`` followed by ``delete``
    would have a window where both callers read the same payload.

    Returns the decoded payload on success, ``None`` if the key was
    already consumed, expired, or never existed.
    """
    r = await redis_client()
    val = await r.getdel(_INVITE_PREFIX + hash_token(raw_token))
    return json.loads(val) if val else None


async def list_invitations() -> list[dict]:
    """Return all pending invitations (expired ones are gone via TTL)."""
    r = await redis_client()
    out: list[dict] = []
    async for key in r.scan_iter(match=_INVITE_PREFIX + "*"):
        val = await r.get(key)
        if not val:
            continue
        item = json.loads(val)
        item["id"] = key[len(_INVITE_PREFIX):]  # token_hash, safe to expose
        out.append(item)
    out.sort(key=lambda d: d.get("created_at", ""), reverse=True)
    return out


async def find_invitation_by_email(email: str) -> dict | None:
    """Return the pending invitation for ``email`` if one exists (else None).

    Email match is case-insensitive. Expired invites are already gone via TTL,
    so any hit is a still-valid pending invitation.
    """
    target = email.strip().lower()
    for item in await list_invitations():
        if item.get("email", "").strip().lower() == target:
            return item
    return None


async def revoke_invitation(token_hash: str) -> bool:
    """Revoke a pending invitation by its id (token_hash). Returns True if removed."""
    r = await redis_client()
    return bool(await r.delete(_INVITE_PREFIX + token_hash))


# ---------------------------------------------------------------------------
# Password resets
# ---------------------------------------------------------------------------


async def create_password_reset(
    *, user_id: Any, created_by_id: Any, ttl_hours: int, email: str
) -> tuple[str, datetime]:
    """Store a new password-reset token sent to *email*. Returns (raw_token, expires_at).

    The token names the address it is sent to: once the account's email
    changes, the link stops working (:func:`password_reset_addressed_to`)."""
    raw, token_hash = _new_token()
    now = datetime.now(UTC)
    expires_at = now + timedelta(hours=ttl_hours)
    payload = {
        "user_id": str(user_id),
        "email": email.strip().lower(),
        "created_by_id": str(created_by_id) if created_by_id else None,
        "created_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
    }
    r = await redis_client()
    await r.set(_RESET_PREFIX + token_hash, json.dumps(payload), ex=ttl_hours * 3600)
    return raw, expires_at


def password_reset_addressed_to(data: dict, user: Any) -> bool:
    """Whether the reset token *data* was sent to *user*'s current email.

    A link sent before the account's email changed is refused. A token that
    names no address (minted before tokens named one) is accepted."""
    sent_to = data.get("email")
    return sent_to is None or sent_to == (getattr(user, "email", None) or "").strip().lower()


async def get_password_reset(raw_token: str) -> dict | None:
    """Peek at a password-reset token by its raw value (does not consume it).

    Used by ``GET /api/v1/auth/reset-password/{token}`` so the reset page
    can render whose password is being set without burning the link.
    The actual reset endpoint uses :func:`consume_password_reset` so two
    concurrent reset requests cannot both succeed.
    """
    r = await redis_client()
    val = await r.get(_RESET_PREFIX + hash_token(raw_token))
    return json.loads(val) if val else None


async def consume_password_reset(raw_token: str) -> dict | None:
    """Atomically read-and-delete a password-reset token.

    Uses Redis ``GETDEL`` (Redis 6.2+) for the same reason as
    :func:`consume_invitation`: a non-atomic ``get`` then ``delete`` lets
    two concurrent callers both pass the read and race on the password
    update.  ``GETDEL`` collapses the read+delete into a single round-trip
    so exactly one caller wins.

    Returns the decoded payload on success, ``None`` if the key was
    already consumed, expired, or never existed.
    """
    r = await redis_client()
    val = await r.getdel(_RESET_PREFIX + hash_token(raw_token))
    return json.loads(val) if val else None


# ---------------------------------------------------------------------------
# Email change codes
# ---------------------------------------------------------------------------

#: A code lives 10 minutes: six digits are only safe with a short life and few tries.
EMAIL_CHANGE_TTL_SECONDS = 600
#: Wrong codes after which the pending change is dropped and a new one is needed.
EMAIL_CHANGE_MAX_ATTEMPTS = 5


def _secret_mac(*parts: str) -> str:
    message = "\x00".join(parts).encode()
    return hmac.new(settings.secret_key.encode(), message, hashlib.sha256).hexdigest()


def _email_change_mac(user_id: Any, email: str, code: str) -> str:
    """The stored form of *code*: bound to the user and the new address, keyed by the server secret."""
    return _secret_mac("email-change", str(user_id), email, code)


def email_change_address_bucket(email: str) -> str:
    """The rate-limit bucket of a target address: a keyed hash, so Redis never holds the address."""
    return _secret_mac("email-change-address", email.strip().lower())


async def create_email_change(*, user_id: Any, email: str) -> str:
    """Start an email change of *user_id* to *email*. Returns the 6-digit code to send.

    It replaces the user's pending change, whatever its address: the previous
    code stops working and its wrong-code count does not carry over."""
    code = f"{secrets.randbelow(1_000_000):06d}"
    email = email.strip().lower()
    payload = {
        "id": secrets.token_hex(16),
        "email": email,
        "mac": _email_change_mac(user_id, email, code),
        "created_at": datetime.now(UTC).isoformat(),
    }
    r = await redis_client()
    await r.set(_EMAIL_CHANGE_PREFIX + str(user_id), json.dumps(payload), ex=EMAIL_CHANGE_TTL_SECONDS)
    return code


async def drop_email_change(user_id: Any) -> None:
    """Forget *user_id*'s pending email change, if any."""
    r = await redis_client()
    await r.delete(_EMAIL_CHANGE_PREFIX + str(user_id))


async def consume_email_change(*, user_id: Any, code: str) -> str | None:
    """The new address of *user_id*'s pending change when *code* is its code, else ``None``.

    A match consumes the change. Every try counts, the right one included,
    and atomically: once :data:`EMAIL_CHANGE_MAX_ATTEMPTS` tries are spent
    the change is dropped, however many arrive at once. A change replaced
    by a newer request between the read and the match is not consumed."""
    r = await redis_client()
    key = _EMAIL_CHANGE_PREFIX + str(user_id)
    raw = await r.get(key)
    if not raw:
        return None
    pending = json.loads(raw)
    tries = int(
        await r.eval(INCR_EXPIRE_LUA, 1, _EMAIL_CHANGE_ATTEMPTS_PREFIX + pending["id"], EMAIL_CHANGE_TTL_SECONDS)
    )
    if tries > EMAIL_CHANGE_MAX_ATTEMPTS:
        await r.eval(DELETE_IF_EQUAL_LUA, 1, key, raw)
        return None
    if timing_safe_str_eq(_email_change_mac(user_id, pending["email"], code), pending["mac"]):
        consumed = int(await r.eval(DELETE_IF_EQUAL_LUA, 1, key, raw))
        return pending["email"] if consumed else None
    if tries >= EMAIL_CHANGE_MAX_ATTEMPTS:
        await r.eval(DELETE_IF_EQUAL_LUA, 1, key, raw)
    return None
