# SPDX-License-Identifier: Apache-2.0

"""Which GitHub App installations an integration's own token sees.

``POST /api/hooks/providers/github`` is the admin GitHub App's webhook and
carries the events of every installation of that app. Under "Require verified
integration notifications" (``admin_app_scoping_required()``) the platform hands an
event only to integrations on that app the agent's owner may use and, when an
integration records its installations (``extra_config.installations``), only
for those. The usual OAuth connection records none, so such an integration
heard every installation of the app, including organisations its own GitHub
account cannot see. It now hears a GitHub App event only when its own token
sees the event's installation (``GET /user/installations``).

A delivery never asks or waits on GitHub: a lookup on the delivery path was
dropped for its latency and failure modes. The delivery reads the
answer cached for the integration's current credentials, and what it cannot
read is learned in the background for the next delivery:

- an answer listing the installation delivers the event, as before; a complete
  one not listing it, read after GitHub last announced a change to that
  installation, does not;
- no answer for the current credentials (the first delivery, one after a token
  refresh, a reconnect or a config edit, or after a failed lookup) delivers the
  event as before and starts the lookup; so does a Redis that does not answer,
  which starts none (there is nowhere to keep an answer);
- a token GitHub will not list installations for (a ``403`` that is not a rate
  limit: not a GitHub App user token) sees none, and so does an integration
  holding no usable token (none stored, or one that cannot be decrypted), with a
  WARNING naming it;
- an event that can change who sees an installation (``VISIBILITY_EVENTS``)
  marks it: an answer read before the mark does not decide for that
  installation until it is read again, so an organisation that installs the app
  later, or a member added to one, is not lost;
- an answer older than ``FRESH_SECONDS`` keeps deciding while it is read again;
  a listing cut at the page cap decides only for the installations it lists.

An answer is stamped with a keyed digest of the credentials it was read with
(``credentials_fingerprint``): a write to the token, the client id, the auth
type or the config re-encrypts a column, and another ``SECRET_KEY`` changes the
key, so either reads as no answer whichever code path made the change. Nothing
secret reaches Redis. The lookup never refreshes a token: an expired one is a
failed lookup, and the credentials the platform's refresh writes get their own
answer.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings
from app.core.log_once import log_once_soon
from app.core.redis import redis_client
from app.core.security import decrypt_stored_value
from app.core.utils import format_exception_message
from app.integrations.providers.github._common import GITHUB_API, _headers
from app.services.vault import SecretOwner, SecretUnavailableError, reveal_ciphertext

logger = logging.getLogger(__name__)

INSTALLATIONS_URL = f"{GITHUB_API}/user/installations"
PER_PAGE = 100
MAX_PAGES = 10
REQUEST_TIMEOUT_SECONDS = 10.0
LOOKUP_TIMEOUT_SECONDS = 30.0
LOOKUP_CONCURRENCY = 4
MAX_PENDING_LOOKUPS = 256
MAX_REMEMBERED_FAILURES = 4096
FRESH_SECONDS = 300
RETAIN_SECONDS = 86400
RETRY_AFTER_FAILURE_SECONDS = 60.0
#: The delivery reads Redis while it holds its database connection.
CACHE_TIMEOUT_SECONDS = 0.5
FAILURE_WARN_INTERVAL_SECONDS = 900.0
STANDING_WARN_INTERVAL_SECONDS = 86400.0

#: ``X-GitHub-Event`` values that can change which users see an installation:
#: the installation itself, its repositories, an organisation's members, a
#: repository's collaborators and a team's repositories.
VISIBILITY_EVENTS = frozenset(
    {
        "installation",
        "installation_repositories",
        "installation_target",
        "organization",
        "membership",
        "member",
        "team",
        "team_add",
    }
)

_ANSWER_KEY_PREFIX = "github:token_installations:"
_CHANGE_KEY_PREFIX = "github:installation_visibility_changed:"

#: Where answers are kept; ``None`` keeps none (every integration reads as unknown).
redis_store: Callable[[], Awaitable[Any]] | None = redis_client
#: The HTTP transport of the lookup. Test seam.
transport: httpx.AsyncBaseTransport | None = None


class InstallationLookupError(Exception):
    """GitHub did not say which installations a token sees."""


@dataclass(frozen=True)
class TokenListing:
    """The installation ids a token sees; ``complete`` is False past the page cap."""

    ids: frozenset[str]
    complete: bool = True


@dataclass(frozen=True)
class TokenCandidate:
    """What the check needs of one integration: never a plaintext token."""

    integration_id: str
    name: str
    fingerprint: str
    #: The access token, else the API key: the token its tools use.
    token_enc: str | None
    #: Whether that token is stored and decrypts.
    usable: bool
    #: The column ``token_enc`` was read from, without ``_enc``: what a vault failure is recorded under.
    token_field: str = "api_key"  # noqa: S105 - a column name, not a secret


@dataclass(frozen=True)
class _Answer:
    fingerprint: str
    #: When the lookup started: a change marked after it is not in the answer.
    read_at: float
    ids: frozenset[str]
    complete: bool


def _fingerprint_key() -> bytes:
    return hashlib.sha256(b"github-token-installations\x00" + settings.secret_key.encode()).digest()


def credentials_fingerprint(
    *,
    auth_type: str | None,
    client_id_enc: str | None,
    access_token_enc: str | None,
    api_key_enc: str | None,
    extra_config_enc: str | None,
) -> str:
    """A keyed digest of the stored credentials an answer was read with.

    Taken over the ciphertexts, so any write moves it, and keyed by the
    ``SECRET_KEY``, so a new key does too: a token the platform can no longer
    decrypt is never vouched for by an answer read while it could.
    """
    mac = hmac.new(_fingerprint_key(), digestmod=hashlib.sha256)
    for part in (auth_type, client_id_enc, access_token_enc, api_key_enc, extra_config_enc):
        mac.update((part or "").encode("utf-8", "surrogatepass"))
        mac.update(b"\x00")
    return mac.hexdigest()[:32]


def token_candidate(
    integration_id: object,
    name: str | None,
    auth_type: str | None,
    client_id_enc: str | None,
    access_token_enc: str | None,
    api_key_enc: str | None,
    extra_config_enc: str | None,
) -> TokenCandidate:
    """One integration row (the columns of ``webhooks._token_row_columns``) as a candidate."""
    token_enc = access_token_enc or api_key_enc or None
    return TokenCandidate(
        integration_id=str(integration_id),
        name=name or str(integration_id),
        fingerprint=credentials_fingerprint(
            auth_type=auth_type,
            client_id_enc=client_id_enc,
            access_token_enc=access_token_enc,
            api_key_enc=api_key_enc,
            extra_config_enc=extra_config_enc,
        ),
        token_enc=token_enc,
        token_field="access_token" if access_token_enc else "api_key",
        usable=_token_stored(token_enc),
    )


def _token_stored(token_enc: str | None) -> bool:
    """Whether a token is stored and decrypts: a vault reference counts, it is read at lookup."""
    if not token_enc:
        return False
    try:
        return bool(decrypt_stored_value(token_enc))
    except Exception:
        return False


async def _reveal_token(candidate: TokenCandidate) -> str:
    """The candidate's token, a vault reference resolved; ``""`` when it cannot be decrypted."""
    owner = SecretOwner(kind="integration", id=candidate.integration_id, field=candidate.token_field)
    try:
        return await reveal_ciphertext(candidate.token_enc, owner) or ""
    except SecretUnavailableError:
        raise InstallationLookupError("its token could not be read from its secret vault") from None
    except Exception:
        return ""


def changes_installation_visibility(event_key: str) -> bool:
    """Whether a GitHub event (``issues.opened`` form) can change who sees its installation."""
    return event_key.split(".", 1)[0] in VISIBILITY_EVENTS


def _answer_key(integration_id: str) -> str:
    return f"{_ANSWER_KEY_PREFIX}{integration_id}"


def _change_key(installation_id: str) -> str:
    return f"{_CHANGE_KEY_PREFIX}{installation_id}"


def _rate_limited(resp: httpx.Response) -> bool:
    headers = resp.headers
    if headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0":
        return True
    try:
        return "rate limit" in resp.text.lower()
    except Exception:
        return False


def _installation_id(entry: object) -> str | None:
    value = entry.get("id") if isinstance(entry, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | str):
        return None
    text = str(value)
    return text if text.isdigit() else None


async def fetch_installations(token: str) -> TokenListing:
    """Every installation *token* sees (``GET /user/installations``, paginated, bounded).

    A ``403`` on the first page that is not a rate limit is GitHub saying the
    token can list no installation (not a GitHub App user token): it sees none.
    Any other failure raises :class:`InstallationLookupError`.
    """
    ids: set[str] = set()
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, transport=transport, follow_redirects=False
    ) as client:
        for page in range(1, MAX_PAGES + 1):
            try:
                resp = await client.get(
                    INSTALLATIONS_URL,
                    headers=_headers(token),
                    params={"per_page": str(PER_PAGE), "page": str(page)},
                )
            except httpx.HTTPError as exc:
                raise InstallationLookupError(format_exception_message(exc)) from exc
            if resp.status_code == 403 and page == 1 and not _rate_limited(resp):
                return TokenListing(frozenset())
            if resp.status_code != 200:
                raise InstallationLookupError(f"HTTP {resp.status_code}")
            try:
                data = resp.json()
            except ValueError as exc:
                raise InstallationLookupError("a response that is not JSON") from exc
            entries = data.get("installations") if isinstance(data, dict) else None
            if not isinstance(entries, list):
                raise InstallationLookupError("a response without an installation list")
            ids.update(i for i in map(_installation_id, entries) if i is not None)
            total = data.get("total_count")
            known_total = isinstance(total, int) and not isinstance(total, bool)
            if len(entries) < PER_PAGE or (known_total and len(ids) >= total):
                return TokenListing(frozenset(ids))
    return TokenListing(frozenset(ids), complete=False)


def _parse_answer(raw: object) -> _Answer | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return _Answer(
            fingerprint=str(data["fp"]),
            read_at=float(data["at"]),
            ids=frozenset(str(i) for i in data["ids"]),
            complete=bool(data["complete"]),
        )
    except (ValueError, TypeError, KeyError):
        return None


def _parse_time(raw: object) -> float | None:
    try:
        return float(raw) if raw else None
    except (TypeError, ValueError):
        return None


async def _redis(store: Callable[[], Awaitable[Any]], call: Callable[[Any], Awaitable[Any]]) -> Any:
    async def _run() -> Any:
        return await call(await store())

    return await asyncio.wait_for(_run(), CACHE_TIMEOUT_SECONDS)


async def _read(
    candidates: list[TokenCandidate], installation_id: str
) -> tuple[dict[str, _Answer], float | None] | None:
    """The cached answers and the installation's change mark; ``None`` when Redis does not answer."""
    store = redis_store
    if store is None:
        return None
    keys = [_answer_key(c.integration_id) for c in candidates] + [_change_key(installation_id)]
    try:
        values = await _redis(store, lambda redis: redis.mget(keys))
    except Exception as exc:
        logger.debug("GitHub installation answers unreadable: %s", format_exception_message(exc))
        return None
    answers: dict[str, _Answer] = {}
    for candidate, raw in zip(candidates, values[:-1], strict=True):
        answer = _parse_answer(raw)
        if answer is not None:
            answers[candidate.integration_id] = answer
    return answers, _parse_time(values[-1])


async def _save(candidate: TokenCandidate, answer: _Answer) -> None:
    store = redis_store
    if store is None:
        raise InstallationLookupError("no store to keep the answer in")
    value = json.dumps(
        {
            "fp": answer.fingerprint,
            "at": answer.read_at,
            "ids": sorted(answer.ids),
            "complete": answer.complete,
        }
    )
    key = _answer_key(candidate.integration_id)
    try:
        await _redis(store, lambda redis: redis.set(key, value, ex=RETAIN_SECONDS))
    except Exception as exc:
        raise InstallationLookupError(f"the answer could not be kept ({format_exception_message(exc)})") from exc


async def note_installation_change(installation_id: str) -> None:
    """Mark *installation_id*: answers read before now no longer decide for it.

    GitHub installation ids are integers: anything else in a payload is not
    written to Redis.
    """
    store = redis_store
    if store is None or not (installation_id.isdigit() and len(installation_id) <= 20):
        return
    now = repr(time.time())
    key = _change_key(installation_id)
    try:
        await _redis(store, lambda redis: redis.set(key, now, ex=RETAIN_SECONDS))
    except Exception as exc:
        logger.debug(
            "Could not mark a change to GitHub App installation %s: %s",
            installation_id,
            format_exception_message(exc),
        )


def _warn_unusable(candidate: TokenCandidate, why: str) -> None:
    log_once_soon(
        logger,
        logging.WARNING,
        f"github_token_installations:unusable:{candidate.integration_id}:{candidate.fingerprint}",
        "Provider webhook github: integration %s (%s) receives no GitHub App event of the admin app "
        "under Require verified integration notifications: %s, so GitHub cannot say which "
        "installations it sees. Connect it again.",
        candidate.integration_id,
        candidate.name,
        why,
        interval=STANDING_WARN_INTERVAL_SECONDS,
    )


_semaphores: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = (
    weakref.WeakKeyDictionary()
)
#: Lookups in flight, by (integration id, fingerprint): one at a time for the same credentials.
_pending: dict[tuple[str, str], asyncio.Task[None]] = {}
_failed_until: dict[tuple[str, str], float] = {}


def _remember_failure(key: tuple[str, str]) -> None:
    """Back *key* off for ``RETRY_AFTER_FAILURE_SECONDS``, bounded: expired back-offs go first, then the oldest."""
    now = time.monotonic()
    if len(_failed_until) >= MAX_REMEMBERED_FAILURES:
        for expired in [k for k, until in _failed_until.items() if until <= now]:
            del _failed_until[expired]
        while len(_failed_until) >= MAX_REMEMBERED_FAILURES:
            del _failed_until[next(iter(_failed_until))]
    _failed_until.pop(key, None)
    _failed_until[key] = now + RETRY_AFTER_FAILURE_SECONDS


def _semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    semaphore = _semaphores.get(loop)
    if semaphore is None:
        semaphore = _semaphores[loop] = asyncio.Semaphore(LOOKUP_CONCURRENCY)
    return semaphore


async def _read_token_installations(candidate: TokenCandidate) -> None:
    async with _semaphore():
        started = time.time()
        token = await _reveal_token(candidate)
        if not token:
            raise InstallationLookupError("its token cannot be decrypted")
        listing = await asyncio.wait_for(fetch_installations(token), LOOKUP_TIMEOUT_SECONDS)
        await _save(candidate, _Answer(candidate.fingerprint, started, listing.ids, listing.complete))
    if not listing.complete:
        log_once_soon(
            logger,
            logging.WARNING,
            f"github_token_installations:truncated:{candidate.integration_id}",
            "Provider webhook github: integration %s (%s) sees more than %d GitHub App installations; "
            "for the others it receives the admin app's events as before.",
            candidate.integration_id,
            candidate.name,
            PER_PAGE * MAX_PAGES,
            interval=STANDING_WARN_INTERVAL_SECONDS,
        )


async def _lookup(candidate: TokenCandidate) -> None:
    """Ask GitHub, off the delivery path, and keep the answer for the next delivery."""
    key = (candidate.integration_id, candidate.fingerprint)
    try:
        await _read_token_installations(candidate)
    except Exception as exc:
        _remember_failure(key)
        log_once_soon(
            logger,
            logging.WARNING,
            f"github_token_installations:failed:{candidate.integration_id}",
            "Provider webhook github: could not ask GitHub which installations integration %s (%s) "
            "sees (%s). Until it answers, the last answer read for its current credentials decides, "
            "and without one it receives the admin app's GitHub App events as before.",
            candidate.integration_id,
            candidate.name,
            format_exception_message(exc) if isinstance(exc, InstallationLookupError) else type(exc).__name__,
            interval=FAILURE_WARN_INTERVAL_SECONDS,
        )
        return
    _failed_until.pop(key, None)


def _schedule_lookup(candidate: TokenCandidate) -> None:
    key = (candidate.integration_id, candidate.fingerprint)
    if key in _pending or len(_pending) >= MAX_PENDING_LOOKUPS:
        return
    until = _failed_until.get(key)
    if until is not None and time.monotonic() < until:
        return
    task = asyncio.get_running_loop().create_task(_lookup(candidate))
    _pending[key] = task

    def _done(finished: asyncio.Task[None], key: tuple[str, str] = key) -> None:
        if _pending.get(key) is finished:
            del _pending[key]

    task.add_done_callback(_done)


def _unique(candidates: Iterable[TokenCandidate]) -> list[TokenCandidate]:
    seen: dict[str, TokenCandidate] = {}
    for candidate in candidates:
        seen.setdefault(candidate.integration_id, candidate)
    return list(seen.values())


async def installation_reach(candidates: Iterable[TokenCandidate], installation_id: str) -> dict[str, bool]:
    """Whether each candidate may receive a GitHub App event of *installation_id*, by integration id.

    Never asks GitHub: reads the cached answers (one bounded ``MGET``) and
    starts, in the background, the lookups they call for.
    """
    reach: dict[str, bool] = {}
    known: list[TokenCandidate] = []
    for candidate in _unique(candidates):
        if candidate.usable:
            known.append(candidate)
            continue
        reach[candidate.integration_id] = False
        _warn_unusable(
            candidate, "its token cannot be decrypted" if candidate.token_enc else "it holds no token"
        )
    if not known:
        return reach
    cached = await _read(known, installation_id)
    if cached is None:
        # Nowhere to read an answer from, nor to keep one in: as before.
        reach.update((c.integration_id, True) for c in known)
        return reach
    answers, changed_at = cached
    now = time.time()
    for candidate in known:
        answer = answers.get(candidate.integration_id)
        if answer is None or answer.fingerprint != candidate.fingerprint:
            reach[candidate.integration_id] = True
            _schedule_lookup(candidate)
            continue
        outdated = changed_at is not None and answer.read_at < changed_at
        reach[candidate.integration_id] = (
            installation_id in answer.ids or outdated or not answer.complete
        )
        if outdated or now - answer.read_at >= FRESH_SECONDS:
            _schedule_lookup(candidate)
    return reach


async def settle() -> None:
    """Wait for the lookups in flight. Test seam."""
    while _pending:
        await asyncio.gather(*list(_pending.values()), return_exceptions=True)


def reset_state() -> None:
    """Forget the lookups in flight and the failure back-offs. Test seam."""
    _pending.clear()
    _failed_until.clear()
