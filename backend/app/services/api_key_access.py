# SPDX-License-Identifier: Apache-2.0

"""Personal API keys (``fcp-...``): one resolution, one set of scope checks.

Every surface that accepts a personal API key resolves it here, through
:func:`resolve_api_key`, naming the surface it guards (``API_KEY_SURFACE_SITES``
in ``tests/test_api_key_access.py`` lists them):

* the REST API - ``api/deps.get_current_user_ungated``;
* the OpenAI-compatible LLM proxy - ``api/v1/llm_proxy``;
* the MCP endpoints ``/mcp/user``, ``/mcp/xtm`` and the platform servers -
  ``api/v1/mcp_auth.authenticate_user``.

The resolution verifies the key (prefix lookup, then bcrypt), refuses a revoked
or expired key and a deactivated owner (401) and a surface the key does not
cover (403), then charges the key's own limits - its rate limit and its quota,
counted in Redis per key, ON TOP of the owner's quotas, which the surfaces keep
checking as before (429). Every request the key authenticates on a surface it
covers counts, whether or not the operation is then refused. It returns the
owner tagged with the key (``user.api_key_principal``); the surface then asks
this module about the operation: :func:`enforce_rest_scope`,
:func:`require_agent_run`, :func:`require_model_access`,
:func:`require_configuration_surface`, :func:`mcp_tools_for_key`.

A key never grants more than its owner has: it authenticates AS the owner, whose
permissions every route checks as for a session, and its scopes only take away.
A key created before scopes existed has no scope set and keeps full access.

What "read-only" means, per surface:

============  =============================================  ==================================
Surface       A read-only key may                            It may not
============  =============================================  ==================================
REST API      GET / HEAD / OPTIONS                           any other method - a POST that
                                                             only searches included
LLM proxy     list models, a raw model completion,           run an agent (``agent:<slug>``),
              count tokens                                   which acts with the agent's tools
MCP           initialize, list tools, call a tool on the     any other tool; on ``/mcp/user``
              endpoint's reviewed read list                  every tool runs an agent: none
============  =============================================  ==================================

An agent restriction keeps the key to the agents it names: the proxy's
``agent:<slug>`` models, the ``/mcp/user`` tools, and on the REST API the
agent's own routes (``/api/v1/agents/{agent_id}/...``) and the chat routes of
its conversations. Everything else - raw models, ``/mcp/xtm``, the platform MCP
servers, every other REST route - is refused: those are not agents.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import TYPE_CHECKING, Any, NoReturn

from fastapi import HTTPException, Request, status
from sqlalchemy import select

from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.core.redis import redis_client
from app.core.security import bcrypt_checkpw_async
from app.core.security_options import security_option
from app.core.utils import format_exception_message
from app.models.api_key import UserApiKey
from app.models.user import User

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: What every personal API key starts with ("filigran copilot proxy").
API_KEY_PREFIX = "fcp-"
#: How much of a key is stored in clear for display and lookup (``fcp-1a2b3c4d...``).
_DISPLAY_PREFIX_CHARS = 12


class ApiKeySurface(StrEnum):
    """The surfaces a personal API key may be scoped to."""

    REST = "rest"
    LLM_PROXY = "llm_proxy"
    MCP = "mcp"


API_KEY_SURFACES: tuple[str, ...] = tuple(member.value for member in ApiKeySurface)

#: How a refusal names a surface.
SURFACE_LABELS: dict[str, str] = {
    ApiKeySurface.REST: "the REST API",
    ApiKeySurface.LLM_PROXY: "the LLM proxy",
    ApiKeySurface.MCP: "the MCP endpoints",
}

#: The methods a read-only key may use on the REST API.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: The periods a per-key quota counts over.
QUOTA_PERIODS: tuple[str, ...] = ("daily", "monthly")
_PERIOD_NOUNS = {"daily": "day", "monthly": "month"}

#: Bounds on what a key may be given, far above any real use.
MAX_RATE_LIMIT_PER_MINUTE = 100_000
MAX_QUOTA_LIMIT = 1_000_000_000
MAX_AGENT_IDS = 200

#: The notices an owner gets before a key expires, in days. Each one once per key.
EXPIRY_NOTICE_DAYS: tuple[int, ...] = (7, 1)

#: Redis hash buffering a key's use, flushed to the row by the scheduler.
APIKEY_STATS_PREFIX = "apikey:stats:"
_STATS_TTL_SECONDS = 86_400
#: Redis counter of a key's quota, one per key and period.
QUOTA_KEY_PREFIX = "quota:apikey:"
#: The ``core/rate_limit`` bucket family of the per-key rate limit.
RATE_LIMIT_NAME = "api_key"

INVALID_KEY_DETAIL = "Invalid API key"
EXPIRED_KEY_DETAIL = "API key expired"
DISABLED_OWNER_DETAIL = "User account disabled"
AGENT_RESTRICTED_DETAIL = "This API key is restricted to selected agents."
QUOTA_UNAVAILABLE_DETAIL = (
    "Unable to verify this API key's quota because the quota store is unavailable. "
    "Please try again later."
)

#: The REST routes an agent-restricted key may reach, as FastAPI route templates.
AGENT_ROUTE = "/api/v1/agents/{agent_id}"
CONVERSATIONS_ROUTE = "/api/v1/chat/conversations"
CONVERSATION_ROUTE = "/api/v1/chat/conversations/{conversation_id}"


# ---------------------------------------------------------------------------
# The key itself
# ---------------------------------------------------------------------------


def looks_like_api_key(token: str | None) -> bool:
    """Whether *token* is shaped like a personal API key (says nothing of its validity)."""
    return bool(token) and str(token).startswith(API_KEY_PREFIX)


def generate_api_key() -> str:
    """A new random key: ``fcp-`` and 48 hex characters."""
    return API_KEY_PREFIX + secrets.token_hex(24)


def display_prefix(raw_key: str) -> str:
    """The clear part of a key, stored for display and as the lookup index."""
    return raw_key[:_DISPLAY_PREFIX_CHARS] + "..."


def canonical_agent_id(value: object) -> str | None:
    """*value* as the canonical UUID string an agent restriction holds, or ``None``."""
    if isinstance(value, uuid.UUID):
        return str(value)
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def available_surfaces() -> tuple[str, ...]:
    """The surfaces this deployment serves: the LLM proxy is mounted in Copilot / dev mode only."""
    if settings.is_copilot_like:
        return API_KEY_SURFACES
    return tuple(surface for surface in API_KEY_SURFACES if surface != ApiKeySurface.LLM_PROXY)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None or moment.tzinfo is not None:
        return moment
    return moment.replace(tzinfo=UTC)


def is_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    expires_at = _aware(expires_at)
    return expires_at is not None and expires_at <= (now or datetime.now(UTC))


def has_full_scope(row: Any) -> bool:
    """Every surface this deployment serves, read-write, every agent: the access of a key without scopes."""
    surfaces = row.surfaces
    covers = surfaces is None or set(available_surfaces()) <= set(surfaces)
    return covers and not row.read_only and row.agent_ids is None


# ---------------------------------------------------------------------------
# Expiry and its bound
# ---------------------------------------------------------------------------


def max_lifetime_days() -> int:
    """The administrator's bound on a key's lifetime, in days; 0 = none."""
    return int(security_option("api_key_max_lifetime_days") or 0)


def latest_allowed_expiry(now: datetime) -> datetime | None:
    """The latest expiry a key created or re-dated at *now* may have, ``None`` without a bound."""
    days = max_lifetime_days()
    return now + timedelta(days=days) if days > 0 else None


def beyond_max_lifetime(expires_at: datetime | None, now: datetime | None = None) -> bool:
    """A key that outlives the current bound: no expiry, or one later than a key re-dated at *now* may have.

    Measured from *now*, never from the key's creation: re-dating is bounded from
    now (:func:`latest_allowed_expiry`), so a key reported here leaves the report
    once its owner gives it an expiry the bound allows today.
    """
    latest = latest_allowed_expiry(now or datetime.now(UTC))
    if latest is None:
        return False
    expires_at = _aware(expires_at)
    return expires_at is None or expires_at > latest


def due_expiry_notice(expires_at: datetime | None, now: datetime) -> int | None:
    """The notice (days before expiry) a key expiring at *expires_at* is inside at *now*, if any."""
    expires_at = _aware(expires_at)
    if expires_at is None or expires_at <= now:
        return None
    remaining = expires_at - now
    for days in sorted(EXPIRY_NOTICE_DAYS):
        if remaining <= timedelta(days=days):
            return days
    return None


# ---------------------------------------------------------------------------
# The principal a key resolves to
# ---------------------------------------------------------------------------


def _frozen_surfaces(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple | set | frozenset):
        # A value nothing wrote: no surface, never every surface.
        return frozenset()
    return frozenset(str(entry) for entry in value)


def _frozen_agents(value: object) -> frozenset[str] | None:
    if value is None:
        return None
    if not isinstance(value, list | tuple | set | frozenset):
        return frozenset()
    return frozenset(canonical for entry in value if (canonical := canonical_agent_id(entry)))


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """What a resolved key may do, carried on the user it authenticated."""

    key_id: uuid.UUID
    name: str
    surfaces: frozenset[str] | None
    read_only: bool
    agent_ids: frozenset[str] | None
    expires_at: datetime | None
    rate_limit_per_minute: int | None
    quota_limit: int | None
    quota_period: str | None

    @classmethod
    def from_row(cls, row: Any) -> ApiKeyPrincipal:
        return cls(
            key_id=row.id,
            name=row.name,
            surfaces=_frozen_surfaces(row.surfaces),
            read_only=bool(row.read_only),
            agent_ids=_frozen_agents(row.agent_ids),
            expires_at=_aware(row.expires_at),
            rate_limit_per_minute=row.rate_limit_per_minute,
            quota_limit=row.quota_limit,
            quota_period=row.quota_period,
        )

    def allows_surface(self, surface: str) -> bool:
        return self.surfaces is None or surface in self.surfaces

    @property
    def agent_restricted(self) -> bool:
        return self.agent_ids is not None

    def allows_agent(self, agent_id: object) -> bool:
        if self.agent_ids is None:
            return True
        canonical = canonical_agent_id(agent_id)
        return canonical is not None and canonical in self.agent_ids


def principal_of(user: object) -> ApiKeyPrincipal | None:
    """The key *user* authenticated with, ``None`` for any other credential."""
    principal = getattr(user, "api_key_principal", None)
    return principal if isinstance(principal, ApiKeyPrincipal) else None


# ---------------------------------------------------------------------------
# The resolution
# ---------------------------------------------------------------------------


def _refuse_unauthorized(detail: str) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _unauthorized(refuse: Callable[[str], Any], detail: str) -> NoReturn:
    refuse(detail)
    # *refuse* raises: a surface hands its own 401 shape (MCP adds WWW-Authenticate).
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def surface_refused_detail(surface: str) -> str:
    return f"This API key cannot be used with {SURFACE_LABELS.get(surface, surface)}."


async def _verified_row(token: str, db: AsyncSession) -> UserApiKey | None:
    """The active key row *token* is, or ``None``: narrowed by the indexed prefix, then bcrypt."""
    rows = (
        (
            await db.execute(
                select(UserApiKey).where(
                    UserApiKey.key_prefix == display_prefix(token),
                    UserApiKey.is_active.is_(True),
                )
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if await bcrypt_checkpw_async(token.encode("utf-8"), row.key_hash.encode("utf-8")):
            return row
    return None


async def resolve_api_key(
    token: str,
    db: AsyncSession,
    *,
    surface: ApiKeySurface,
    request: Request | None = None,
    unauthorized: Callable[[str], Any] | None = None,
) -> User:
    """The owner of the personal API key *token*, used on *surface*, tagged with the key.

    401 (through *unauthorized* when given) for an unknown, revoked or expired
    key or a deactivated owner; 403 for a surface the key does not cover; 429
    when the key's own rate limit or quota is reached (503 when its quota
    cannot be read and quotas fail closed). The operation checks are the
    surface's, through this module.
    """
    refuse = unauthorized or _refuse_unauthorized
    if not looks_like_api_key(token):
        _unauthorized(refuse, INVALID_KEY_DETAIL)
    row = await _verified_row(token, db)
    if row is None:
        _unauthorized(refuse, INVALID_KEY_DETAIL)
    if is_expired(row.expires_at):
        _unauthorized(refuse, EXPIRED_KEY_DETAIL)
    user = (await db.execute(select(User).where(User.id == row.user_id))).scalar_one_or_none()
    if user is None or not user.is_active:
        _unauthorized(refuse, DISABLED_OWNER_DETAIL)

    principal = ApiKeyPrincipal.from_row(row)
    if not principal.allows_surface(surface):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=surface_refused_detail(surface))
    await _enforce_key_rate_limit(principal, request)
    await _charge_key_quota(principal)
    await _record_use(principal.key_id)

    user.auth_credential_kind = "api_key"
    user.api_key_principal = principal
    return user


# ---------------------------------------------------------------------------
# Per-key limits, on top of the owner's quotas
# ---------------------------------------------------------------------------


async def _enforce_key_rate_limit(principal: ApiKeyPrincipal, request: Request | None) -> None:
    """The key's requests per minute, in ``core/rate_limit``'s fixed window (its fail mode)."""
    limit = principal.rate_limit_per_minute
    if not limit or limit <= 0:
        return
    try:
        await enforce_rate_limit(
            request, RATE_LIMIT_NAME, limit=limit, window_seconds=60, bucket=str(principal.key_id)
        )
    except HTTPException as exc:
        if exc.status_code != status.HTTP_429_TOO_MANY_REQUESTS:
            raise
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"This API key's rate limit is reached ({limit} requests per minute). "
                "Please slow down and try again shortly."
            ),
            headers=exc.headers,
        ) from None


def _period_key(period: str, now: datetime) -> str:
    # The period keys of ``services/quota.py``: a quota period reads the same everywhere.
    return now.strftime("%Y-%m-%d") if period == "daily" else now.strftime("%Y-%m")


def quota_counter_key(key_id: uuid.UUID | str, period: str, now: datetime | None = None) -> str:
    return f"{QUOTA_KEY_PREFIX}{key_id}:{_period_key(period, now or datetime.now(UTC))}"


# Charge one request unless the counter already holds the limit; the TTL is set
# when missing, so a counter can never outlive its period.
_CHARGE_IF_BELOW_LUA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then
    return -1
end
current = redis.call('INCR', KEYS[1])
if redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return current
"""


async def _charge_key_quota(principal: ApiKeyPrincipal) -> None:
    """Count one request against the key's quota; 429 once it is reached, never past it."""
    from app.services.quota import quota_fail_closed, seconds_until_period_reset

    limit, period = principal.quota_limit, principal.quota_period
    if not limit or limit <= 0 or period not in QUOTA_PERIODS:
        return
    try:
        redis = await redis_client()
        charged = int(
            await redis.eval(
                _CHARGE_IF_BELOW_LUA,
                1,
                quota_counter_key(principal.key_id, period),
                limit,
                seconds_until_period_reset(period) + 3600,
            )
        )
    except Exception as exc:
        # The fail mode of the owner's quotas: closed always in XTM One mode.
        if quota_fail_closed():
            logger.error(
                "API key quota check failed for key %s - refusing the request (quotas fail closed): %s",
                principal.key_id,
                format_exception_message(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=QUOTA_UNAVAILABLE_DETAIL
            ) from None
        logger.warning(
            "API key quota check failed for key %s - the request proceeds unmetered: %s",
            principal.key_id,
            format_exception_message(exc),
        )
        return
    if charged < 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"This API key's quota is reached ({limit} requests per {_PERIOD_NOUNS[period]}).",
            headers={"Retry-After": str(seconds_until_period_reset(period))},
        )


async def read_quota_usage(rows: Sequence[Any]) -> dict[str, int]:
    """``{key id: requests counted this period}`` of the keys with a quota; best effort."""
    counted = [row for row in rows if row.quota_limit and row.quota_period in QUOTA_PERIODS]
    if not counted:
        return {}
    try:
        redis = await redis_client()
        pipe = redis.pipeline()
        for row in counted:
            pipe.get(quota_counter_key(row.id, row.quota_period))
        values = await pipe.execute()
    except Exception:
        logger.debug("Could not read API key quota usage", exc_info=True)
        return {}
    return {str(row.id): int(value or 0) for row, value in zip(counted, values, strict=False)}


async def _record_use(key_id: uuid.UUID) -> None:
    """Buffer the key's use in Redis: ``request_count`` and ``last_used_at`` reach the row on the next flush."""
    try:
        redis = await redis_client()
        stats_key = f"{APIKEY_STATS_PREFIX}{key_id}"
        pipe = redis.pipeline()
        pipe.hincrby(stats_key, "count", 1)
        pipe.hset(stats_key, "last_used", datetime.now(UTC).isoformat())
        pipe.expire(stats_key, _STATS_TTL_SECONDS)
        await pipe.execute()
    except Exception:
        logger.debug("Failed to buffer API key stats for %s", key_id, exc_info=True)


# ---------------------------------------------------------------------------
# The operation checks of each surface
# ---------------------------------------------------------------------------


def refuse_read_only(what: str) -> NoReturn:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"This API key is read-only: {what}.")


def refuse_agent_restricted() -> NoReturn:
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=AGENT_RESTRICTED_DETAIL)


async def enforce_rest_scope(principal: ApiKeyPrincipal, request: Request | None, db: AsyncSession) -> None:
    """The REST API: a read-only key makes safe requests only, an agent-restricted one reaches its agents only."""
    if request is None:
        # Nothing to judge the operation by: only a key with nothing to check passes.
        if principal.read_only or principal.agent_restricted:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This API key cannot be used here.")
        return
    if principal.read_only and request.method.upper() not in SAFE_METHODS:
        refuse_read_only("it can only make GET requests on the REST API")
    if principal.agent_restricted:
        agents = await _rest_request_agents(request, db)
        if not agents or not all(principal.allows_agent(agent_id) for agent_id in agents):
            refuse_agent_restricted()


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", "") or "")


def _under(template: str, route: str) -> bool:
    return template == route or template.startswith(route + "/")


async def _json_body(request: Request) -> object:
    if request.method.upper() in SAFE_METHODS:
        return None
    try:
        return await request.json()
    except Exception:
        return None


async def _rest_request_agents(request: Request, db: AsyncSession) -> list[object]:
    """The agents a REST request acts on, for the routes an agent-restricted key may reach; else none.

    A message names the agents it ``@mentions`` too: each answers it.
    """
    template = _route_template(request)
    params = request.path_params or {}
    if _under(template, AGENT_ROUTE):
        return [params.get("agent_id")]
    if template == CONVERSATIONS_ROUTE and request.method.upper() == "POST":
        body = await _json_body(request)
        return [body.get("agent_id")] if isinstance(body, dict) else []
    if _under(template, CONVERSATION_ROUTE):
        from app.models.chat import Conversation

        try:
            conversation_id = uuid.UUID(str(params.get("conversation_id")))
        except (TypeError, ValueError):
            return []
        agents: list[object] = [
            (
                await db.execute(select(Conversation.agent_id).where(Conversation.id == conversation_id))
            ).scalar_one_or_none()
        ]
        body = await _json_body(request)
        mentioned = body.get("mentioned_agent_ids") if isinstance(body, dict) else None
        if isinstance(mentioned, list):
            agents += mentioned
        return agents
    return []


def require_agent_run(user: object, agent_id: object) -> None:
    """An agent run through the key: refused for an agent it does not name, and for a read-only key."""
    principal = principal_of(user)
    if principal is None:
        return
    if not principal.allows_agent(agent_id):
        refuse_agent_restricted()
    if principal.read_only:
        refuse_read_only("running an agent acts with the agent's tools")


def require_model_access(user: object) -> None:
    """A raw model call (no agent): refused for a key restricted to agents."""
    principal = principal_of(user)
    if principal is not None and principal.agent_restricted:
        refuse_agent_restricted()


def require_configuration_surface(user: object) -> None:
    """``/mcp/xtm`` and the platform MCP servers expose no agent: refused for a key restricted to agents."""
    require_model_access(user)


def lists_raw_models(user: object) -> bool:
    principal = principal_of(user)
    return principal is None or not principal.agent_restricted


def agents_for_key[AgentT](user: object, agents: Iterable[AgentT]) -> list[AgentT]:
    """The agents *user*'s key may run: none for a read-only key, the named ones for a restricted key."""
    principal = principal_of(user)
    if principal is None:
        return list(agents)
    if principal.read_only:
        return []
    return [agent for agent in agents if principal.allows_agent(getattr(agent, "id", None))]


def mcp_tools_for_key[ToolT](user: object, definitions: Iterable[ToolT], read_tools: frozenset[str]) -> list[ToolT]:
    """The MCP tools *user*'s key may call: a read-only key keeps the endpoint's reviewed read list."""
    principal = principal_of(user)
    if principal is None or not principal.read_only:
        return list(definitions)
    return [definition for definition in definitions if getattr(definition, "name", None) in read_tools]


def require_within_caller_key(
    user: object,
    *,
    surfaces: Sequence[str] | None,
    read_only: bool,
    agent_ids: Sequence[str] | None,
    expires_at: datetime | None,
    rate_limit_per_minute: int | None,
    quota_limit: int | None,
    quota_period: str | None,
    creates: bool,
) -> None:
    """A key made or changed with another key never reaches further than that key.

    Limits are counted per key, so a key with a rate limit or a quota creates no key
    at all: each key it made would bring a fresh budget of its own.
    """
    caller = principal_of(user)
    if caller is None:
        return
    limited = caller.rate_limit_per_minute is not None or caller.quota_limit is not None
    if creates and limited:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An API key with its own rate limit or quota cannot create API keys.",
        )
    wider = (
        (caller.surfaces is not None and (surfaces is None or not set(surfaces) <= caller.surfaces))
        or (caller.read_only and not read_only)
        or (caller.agent_ids is not None and (agent_ids is None or not set(agent_ids) <= caller.agent_ids))
        or (
            caller.expires_at is not None
            and (expires_at is None or _aware(expires_at) > caller.expires_at)
        )
        or (
            caller.rate_limit_per_minute is not None
            and (rate_limit_per_minute is None or rate_limit_per_minute > caller.rate_limit_per_minute)
        )
        or (
            caller.quota_limit is not None
            and (quota_limit is None or quota_period != caller.quota_period or quota_limit > caller.quota_limit)
        )
    )
    if wider:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="An API key cannot create or widen a key beyond its own scopes, expiry and limits.",
        )
