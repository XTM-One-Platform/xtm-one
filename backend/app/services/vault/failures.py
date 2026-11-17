"""The last time a stored vault reference could not be resolved, per entity.

The entity page shows it and the Security Audit lists it. Kept in Redis -
shared by every replica and the worker, gone after :data:`FAILURE_RETENTION_SECONDS`
- as the reason code and a keyed fingerprint of the reference only, never a
value, a path or the vault's answer. A record is shown only while its field
still holds the reference it was taken for (:func:`failure_matches`), so
replacing a broken reference clears it at once. Fail-open: a Redis that does
not answer costs the record, never the call.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any

from app.core.secret_refs import SecretReference
from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

_KEY_PREFIX = "secret_vault:failures:"
_INDEX_KEY = "secret_vault:failures"
FAILURE_RETENTION_SECONDS = 7 * 24 * 3600
#: How many entities the index keeps, the most recent first.
_INDEX_SIZE = 500
_REDIS_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class SecretOwner:
    """The secret field a reference was read for: ``integration`` / ``<id>`` / ``api_key``."""

    kind: str
    id: str
    field: str


def _entity_key(kind: str, entity_id: str) -> str:
    return f"{_KEY_PREFIX}{kind}:{entity_id}"


def reference_fingerprint(reference: SecretReference) -> str:
    """An opaque tag of *reference*: equal for the same reference, keyed by ``SECRET_KEY`` so a path cannot be guessed back."""
    from app.core.config import settings

    key = hashlib.sha256(b"vault-reference-fingerprint\x00" + settings.secret_key.encode()).digest()
    return hmac.new(key, str(reference).encode(), hashlib.sha256).hexdigest()[:16]


def failure_matches(entry: Mapping[str, Any], fingerprints: Mapping[str, Collection[str]]) -> bool:
    """Whether a recorded failure is about a reference its field still holds (*fingerprints*: per field).

    A record taken for a reference the field no longer holds - replaced by a
    value or by another reference - is history, never shown. A record without
    a fingerprint matches nothing: a value that does not parse as a reference
    is diagnosed from the stored value (``invalid_references``), not from a read.
    """
    ref = entry.get("ref")
    return bool(ref) and ref in fingerprints.get(entry["field"], ())


async def _redis() -> Any:
    from app.core.redis import redis_client

    return await redis_client()


async def record_failure(owner: SecretOwner, provider: str, reason: str, reference: SecretReference) -> None:
    now = time.time()
    member = f"{owner.kind}:{owner.id}"
    entry = json.dumps({"provider": provider, "reason": reason, "at": now, "ref": reference_fingerprint(reference)})
    try:
        client = await _redis()
        pipe = client.pipeline(transaction=False)
        pipe.hset(_entity_key(owner.kind, owner.id), owner.field, entry)
        pipe.expire(_entity_key(owner.kind, owner.id), FAILURE_RETENTION_SECONDS)
        pipe.zadd(_INDEX_KEY, {member: now})
        pipe.zremrangebyscore(_INDEX_KEY, "-inf", now - FAILURE_RETENTION_SECONDS)
        pipe.zremrangebyrank(_INDEX_KEY, 0, -_INDEX_SIZE - 1)
        await asyncio.wait_for(pipe.execute(), timeout=_REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not record a vault reference failure: %s", format_exception_message(exc))


#: Drops one field's record, and the entity from the index once its record holds no field.
#: One atomic step: a failure another worker records for another field meanwhile keeps the
#: index entry the Security Audit reads it through.
_CLEAR_FIELD_SCRIPT = """
redis.call('HDEL', KEYS[1], ARGV[1])
if redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('ZREM', KEYS[2], ARGV[2])
end
return 1
"""


async def clear_failure(owner: SecretOwner) -> None:
    try:
        client = await _redis()
        await asyncio.wait_for(
            client.eval(
                _CLEAR_FIELD_SCRIPT,
                2,
                _entity_key(owner.kind, owner.id),
                _INDEX_KEY,
                owner.field,
                f"{owner.kind}:{owner.id}",
            ),
            timeout=_REDIS_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.debug("Could not clear a vault reference failure: %s", format_exception_message(exc))


def _entries(kind: str, entity_id: str, raw: dict[Any, Any]) -> list[dict[str, Any]]:
    """The records of one entity's hash still within :data:`FAILURE_RETENTION_SECONDS`.

    The hash expires with its newest record: an older field's record is
    dropped here, so a recurring failure of one field never keeps another
    field's failure shown.
    """
    oldest = time.time() - FAILURE_RETENTION_SECONDS
    entries = []
    for field, value in raw.items():
        try:
            data = json.loads(value)
            at = float(data.get("at") or 0)
            if at < oldest:
                continue
            entries.append(
                {
                    "kind": kind,
                    "id": entity_id,
                    "field": field.decode() if isinstance(field, bytes) else str(field),
                    "provider": str(data.get("provider") or ""),
                    "reason": str(data.get("reason") or ""),
                    "at": at,
                    "ref": str(data.get("ref") or ""),
                }
            )
        except (TypeError, ValueError, AttributeError):
            continue
    return sorted(entries, key=lambda entry: entry["field"])


async def entity_failures(kind: str, entity_id: str) -> list[dict[str, Any]]:
    """The fields of one entity whose reference failed, most recent attempt each; ``[]`` when Redis is out."""
    try:
        client = await _redis()
        raw = await asyncio.wait_for(client.hgetall(_entity_key(kind, entity_id)), timeout=_REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not read vault reference failures: %s", format_exception_message(exc))
        return []
    return _entries(kind, entity_id, raw or {})


async def failures_by_entity(kind: str, entity_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """:func:`entity_failures` for several entities of one kind, in one round trip."""
    if not entity_ids:
        return {}
    try:
        client = await _redis()
        pipe = client.pipeline(transaction=False)
        for entity_id in entity_ids:
            pipe.hgetall(_entity_key(kind, entity_id))
        raws = await asyncio.wait_for(pipe.execute(), timeout=_REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not read vault reference failures: %s", format_exception_message(exc))
        return {}
    return {
        entity_id: entries
        for entity_id, raw in zip(entity_ids, raws, strict=False)
        if (entries := _entries(kind, entity_id, raw or {}))
    }


@dataclass(frozen=True)
class FailureHistory:
    """Every failure the index still points at, the most recent entities first."""

    entries: list[dict[str, Any]]
    #: The index is full: an entity whose last failure is older than its oldest one may be missing.
    truncated: bool


async def recent_failures() -> FailureHistory | None:
    """The whole retained failure history, in two round trips; ``None`` when Redis does not answer."""
    try:
        client = await _redis()
        members = await asyncio.wait_for(
            client.zrevrangebyscore(
                _INDEX_KEY, "+inf", time.time() - FAILURE_RETENTION_SECONDS, start=0, num=_INDEX_SIZE
            ),
            timeout=_REDIS_TIMEOUT_SECONDS,
        )
        owners = []
        for member in members or []:
            text = member.decode() if isinstance(member, bytes) else str(member)
            kind, _, entity_id = text.partition(":")
            owners.append((kind, entity_id))
        raws: list[Any] = []
        if owners:
            pipe = client.pipeline(transaction=False)
            for kind, entity_id in owners:
                pipe.hgetall(_entity_key(kind, entity_id))
            raws = await asyncio.wait_for(pipe.execute(), timeout=_REDIS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.debug("Could not read vault reference failures: %s", format_exception_message(exc))
        return None
    entries: list[dict[str, Any]] = []
    for (kind, entity_id), raw in zip(owners, raws, strict=False):
        entries.extend(_entries(kind, entity_id, raw or {}))
    return FailureHistory(entries=entries, truncated=len(owners) >= _INDEX_SIZE)
