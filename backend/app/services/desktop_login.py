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

"""Browser sign-in for XTM One Desktop.

The Desktop app hosts the platform's web app in a native webview. Email +
password work fine in there; single sign-on does not: identity providers
routinely refuse or degrade embedded webviews (conditional access, passkeys,
Windows Hello, "sign in with the browser you are already signed in to"), and a
webview shares no session with the person's real browser. Every native client
worth copying - Claude Desktop, Cursor, Slack - therefore runs SSO in the
SYSTEM browser and hands the session back to the app. This module is that
handoff, shaped like the OAuth device / native-app flows it resembles:

1. **start** - the Desktop app, authenticated with its RUNNER credential,
   opens a *pending login* and gets an opaque ``login_id`` (256 bits). The
   binding to a runner is the security anchor: nobody can start a handoff
   without holding an enrolled machine's credential, so a link pasted in a chat
   cannot turn a victim's sign-in into an attacker's session (the classic
   login-CSRF against "sign in, then redirect to the app" flows).
2. **browser** - the app opens the system browser on the instance's own SSO
   start route with ``?desktop_login=<login_id>``. The id rides the provider
   round-trip in OIDC ``state`` / SAML ``RelayState`` (both echoed verbatim by
   the provider; SAML caps RelayState at 80 bytes, this is 51) and the SSO
   callback puts it in the ``/login#...`` fragment next to the token it would
   have handed the SPA.
3. **complete** - the login page in the browser, seeing ``desktop_login`` in
   its fragment, shows WHICH machine is asking (the runner's name, read from
   the server, never from the URL) and asks the person to confirm; on
   confirmation it hands the session over with the token as bearer. The server
   records WHO confirmed on the pending login - after checking the signed-in
   user may target that runner at all (``load_desktop_context``: visible and
   active) AND is its enroller, or the machine is company-managed (a shared
   workstation with no owner). A machine enrolled by someone else is refused
   even when it is group-shared with the person: sharing lets a colleague
   DRIVE the machine under their own account, it never makes the owner's
   Desktop app a place the colleague signs in on - that confirmation would hand
   the colleague's session to the owner's polling app. Only a real, first-hand
   browser session may confirm: an API key, a cross-platform JWT or an
   impersonation session is refused, because the session minted for the app
   carries none of those marks.
4. **exchange** - the app, polling with its runner credential, receives the
   session once and signs the webview in. The session is MINTED here, at the
   moment it is handed out, for the user recorded at step 3: Redis holds who
   confirmed, never a token, so a record that expires unclaimed (or a Redis
   dump) contains nothing that signs anyone in. Nothing is ever pushed to the
   app: it pulls, like the runner itself.

Records live in Redis under a short TTL (``LOGIN_TTL_SECONDS``); an abandoned
browser tab simply expires. The browser never stores the session it handed over
- the person asked to sign in on the desktop, not in that tab. A person who
does not recognise the machine can **decline** (the record is deleted and the
refusal audited) so nobody else can complete that request within its window.

Both state transitions are single Redis round-trips, because two of them can
race: two confirmation tabs (a reload, a double click) may both have read
``pending``, and two polls of the app may both have read ``completed``. The
pending -> completed write is a compare-and-set Lua script that rewrites the
record only while it still reads ``pending`` (and keeps its TTL, so a cancel
or an expiry landing in between is never undone by a stale ``SET``), and the
exchange read is a ``GETDEL``: whichever call reaches Redis first wins, the
other finds nothing - one session attached, one session handed out.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime

from app.core.redis import redis_client

logger = logging.getLogger(__name__)

#: How long a pending login stays claimable: long enough for a full SSO round
#: trip with a second factor, short enough that an abandoned tab is not a
#: standing session grant.
LOGIN_TTL_SECONDS = 300

#: ``login_id`` shape: ``secrets.token_urlsafe(32)`` yields 43 base64url chars.
#: The SSO routes validate the client-supplied value against this before
#: carrying it into ``state`` / ``RelayState``, so a crafted query string can
#: never smuggle anything else through the provider round-trip.
LOGIN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")

#: Prefix marking a provider ``state`` / ``RelayState`` as a desktop handoff.
STATE_PREFIX = "desktop:"

STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"

_KEY_PREFIX = "desktop_login:"

#: Atomic pending -> completed transition (single key, single round-trip).
#: Rewrites the record with ARGV[2] only while the stored one still reads
#: ``status == ARGV[1]`` (pending); ``KEEPTTL`` preserves the remaining window,
#: so completion never extends it and never resurrects a key a cancel or an
#: expiry removed a moment earlier. Returns 1 when this caller won, 0 when the
#: record is gone, unreadable, or already completed by a concurrent caller.
_COMPLETE_IF_PENDING_LUA = """
local raw = redis.call('GET', KEYS[1])
if not raw then
    return 0
end
local ok, record = pcall(cjson.decode, raw)
if not ok or type(record) ~= 'table' or record['status'] ~= ARGV[1] then
    return 0
end
redis.call('SET', KEYS[1], ARGV[2], 'KEEPTTL')
return 1
"""


@dataclass(slots=True)
class PendingLogin:
    login_id: str
    runner_id: str
    runner_name: str
    platform: str
    status: str
    created_at: str
    #: The user who confirmed, set on completion; the session for that user is
    #: minted by ``exchange`` exactly once, as the record is consumed.
    user_id: str | None = None
    #: Display name of the user who enrolled the machine; ``None`` for a
    #: company-managed one. Shown on the confirmation page so the person sees
    #: WHOSE machine would receive their session - the same rule ``complete``
    #: enforces, told before the click rather than as a refusal after it.
    enrolled_by: str | None = None
    #: When the confirming browser session signed in: the app's
    #: session counts its absolute lifetime from it, never from the exchange.
    auth_time: int | None = None
    #: When the confirming browser session ends: the app's session
    #: never outlasts it, whatever lifetime is in force at the exchange.
    expires_at: int | None = None


def _key(login_id: str) -> str:
    return f"{_KEY_PREFIX}{login_id}"


def is_valid_login_id(value: object) -> bool:
    return isinstance(value, str) and bool(LOGIN_ID_RE.fullmatch(value))


def encode_state(login_id: str) -> str:
    """The OIDC ``state`` / SAML ``RelayState`` carrying a desktop handoff."""
    return f"{STATE_PREFIX}{login_id}"


def decode_state(state: object) -> str | None:
    """The ``login_id`` a provider echoed back, or ``None`` for any other state.

    Strict on purpose: the callback reads ``state`` from an unauthenticated
    request, so anything that is not exactly our prefix + a well-formed id is
    treated as an ordinary browser sign-in.
    """
    if not isinstance(state, str) or not state.startswith(STATE_PREFIX):
        return None
    login_id = state[len(STATE_PREFIX) :]
    return login_id if is_valid_login_id(login_id) else None


async def _save(record: PendingLogin, ttl: int) -> None:
    redis = await redis_client()
    await redis.set(_key(record.login_id), json.dumps(asdict(record)), ex=ttl)


async def load_login(login_id: str) -> PendingLogin | None:
    if not is_valid_login_id(login_id):
        return None
    redis = await redis_client()
    raw = await redis.get(_key(login_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return PendingLogin(**data)
    except (TypeError, ValueError):
        logger.warning("desktop login %s: unreadable record dropped", login_id[:8])
        await redis.delete(_key(login_id))
        return None


async def start_login(
    *,
    runner_id: str,
    runner_name: str,
    platform: str,
    enrolled_by: str | None = None,
) -> PendingLogin:
    """Open a pending login for the calling runner."""
    record = PendingLogin(
        login_id=secrets.token_urlsafe(32),
        runner_id=runner_id,
        runner_name=runner_name,
        platform=platform,
        status=STATUS_PENDING,
        created_at=datetime.now(UTC).isoformat(),
        enrolled_by=enrolled_by,
    )
    await _save(record, LOGIN_TTL_SECONDS)
    logger.info("desktop login %s opened by runner %s", record.login_id[:8], runner_id)
    return record


async def remaining_ttl(login_id: str) -> int:
    redis = await redis_client()
    ttl = await redis.ttl(_key(login_id))
    return max(0, int(ttl or 0))


async def complete_login(
    record: PendingLogin, user_id: str, *, auth_time: int | None = None, expires_at: int | None = None
) -> bool:
    """Record who confirmed a pending login. False when it is no longer pending.

    Atomic: the write lands only if the STORED record still reads pending at
    that instant (``_COMPLETE_IF_PENDING_LUA``), whatever *record* - a copy
    loaded a moment ago - says. Two confirmations racing each other therefore
    complete exactly one login; the loser is told the request was already used.

    Keeps the remaining TTL: completion does not extend the window, so a
    login confirmed at the very end of it is claimable only for the seconds
    that remain and then evaporates with the record. No token is stored: the
    session is minted by ``exchange`` for *user_id* when the app claims it,
    counting its lifetime from *auth_time*, the confirming session's sign-in,
    and ending no later than *expires_at*, when that session ends.
    On success *record* is updated in place to mirror what was stored.
    """
    if record.status != STATUS_PENDING:
        return False
    completed = replace(record, status=STATUS_COMPLETED, user_id=user_id, auth_time=auth_time, expires_at=expires_at)
    redis = await redis_client()
    written = await redis.eval(
        _COMPLETE_IF_PENDING_LUA,
        1,
        _key(record.login_id),
        STATUS_PENDING,
        json.dumps(asdict(completed)),
    )
    if not written:
        return False
    record.status = STATUS_COMPLETED
    record.user_id = user_id
    record.auth_time = auth_time
    record.expires_at = expires_at
    return True


async def consume_login(login_id: str) -> PendingLogin | None:
    """Delete and return a record - the single-use read behind ``exchange``.

    ``GETDEL``, so two polls racing for a completed record cannot both read it
    before either deletes it: exactly one receives the session, the other gets
    ``None`` and is told the request is gone.
    """
    if not is_valid_login_id(login_id):
        return None
    redis = await redis_client()
    raw = await redis.getdel(_key(login_id))
    if not raw:
        return None
    try:
        return PendingLogin(**json.loads(raw))
    except (TypeError, ValueError):
        logger.warning("desktop login %s: unreadable record consumed", login_id[:8])
        return None


async def cancel_login(login_id: str) -> None:
    if not is_valid_login_id(login_id):
        return
    redis = await redis_client()
    await redis.delete(_key(login_id))
    logger.info("desktop login %s dropped", login_id[:8])
