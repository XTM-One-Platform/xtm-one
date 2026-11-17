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

"""Agent failure quota pool — renewal window, support codes, admin status.

The pool itself (claim / refund of a failed run) lives in ``quota.py`` next to
the counters it moves. This module owns everything around it:

* **When the pool renews.** Once per year, on the anniversary of the XTM
  license (falling back to the platform's own creation date when no license is
  installed). A license anniversary — rather than a calendar year or the
  agentic quota's own daily/monthly window — is what makes the pool line up
  with the commercial cycle it is part of: an agent that burns its allowance
  in March is not silently refilled on 1 April, and everyone's pools renew on
  the date the customer's year actually turns over.

* **Support codes.** A Filigran-signed token an admin pastes in to recharge
  every pool before that anniversary. Verified **offline** against the license
  trust anchor already embedded in ``app/enterprise/xtm_ca.py``: the platform
  needs no new secret, because verification only ever needs the public half —
  the private half stays in filigran-licenses, exactly as for licenses. Two
  consequences follow from being offline, and both are handled here: a
  signature alone is replayable forever, so every code carries an id burned
  into ``support_code_redemptions`` on redemption; and a code cannot be
  revoked, only expired, so every code carries an expiry. The same
  verification and ledger serve the other kind of code, a reset of the
  platform-wide usage counters (``support_codes.py``).

* **The admin read model** behind Settings → Usage and quotas → Quotas — which reports the two
  credit-back reasons apart: an **agent failure** spends a pool slot, while a
  run that never reached a model is credited back **free** and only tallied
  (``_FREE_REFUND_USAGE_TYPE``). Bounding the second would let one provider
  outage swallow an agent's whole allowance, which is the opposite of what the
  pool is for.

Which failure is funded from where
----------------------------------

The split is not "whose fault is it" — that question has no reliable answer at
the point of failure. It is **"did a model answer?"**:

* **Nothing answered** — transport failure, timeout, 502/503/504/529, an
  exhausted failover chain, no provider configured at all, a failure before LLM
  resolution, or work the orchestration engine abandoned. Credited back free
  and unbounded, because the platform failed to deliver and rationing that
  would let one outage swallow an agent's whole year.
* **Something answered "no"** — a rate limit, a rejected credential, an account
  out of credit. Forgiven too, but out of the BOUNDED pool. These are
  persistent and indefinitely reproducible: every run fails the same way until
  someone fixes the account, so putting them on the unbounded path would stop
  the action quota metering entirely for as long as the condition lasts. That
  is the free-unlimited-retries outcome the pool exists to prevent.
* **Everything else** — a tool crash, a hopeless prompt, an oversized payload,
  a bug. The bounded pool, which is what forgives the accidental case while
  still billing the systematic one.
* **Nothing was rationed in the first place** — the user's agentic quota is
  unlimited at every tier (``quota.has_enforceable_agentic_limit``). Funded
  from nowhere: the action the run spent is a counter no gate enforces, so the
  pool is left alone and nothing is credited back. Claiming a slot
  there only ever produced "pool exhausted, failed run stays charged" on a
  platform that has no quota.
* **No account behind the run** — an external-channel sender without an
  account, a public widget visitor, the ``/mcp/global`` platform key.
  The run was charged to the platform-wide counters alone. When nothing
  answered it gets them back, free, like anyone (logged, not tallied: the card
  counts credits per user); any other failure keeps its charge, since the pool
  is a per-user allowance and there is no user to draw it from.

The predicate that draws the line is ``llm.router.is_provider_unavailable_error``,
and it is a *floor*, not a taxonomy: it reads ``str(exc)`` because six provider
SDKs raise six different classes. Structural classification lives in
``services/failure_causes.py``.

.. todo:: Support codes sign with PKCS#1 v1.5 to match the existing licence
   trust anchor — reuse beats provisioning a second one. If that anchor is ever
   rotated, mint new token kinds with PSS instead.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import uuid
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.redis import redis_client
from app.models.support_code import SupportCodeRedemption
from app.services.quota import (
    _FREE_REFUND_USAGE_TYPE,
    _GLOBAL_REFUND_EPOCH,
    _POOL_USAGE_TYPE,
    _USAGE_PREFIX,
    QUOTA_CLEAR_BUSY_MESSAGE,
    QuotaClearBusyError,
    _abandon_clear,
    _advance_refund_epoch,
    _drop_committed_stash,
    _lock_pool_clear,
    _open_clear_stash,
    _settle_clear_stashes,
    _stash_counters,
    _usage_key,
    _wait_for_flush_lock,
    agent_failure_pool_size,
    agentic_platform_cap_enforced,
    clear_refund_causes,
    has_enforceable_agentic_limit,
    platform_agentic_quota_is_unlimited,
    read_refund_causes,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Renewal window
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PoolWindow:
    """The one-year window the pool counters are bucketed in."""

    start: datetime
    end: datetime
    # "xtm_license" (anniversary of the installed license) or "platform"
    # (anniversary of this instance's creation, when no license is readable).
    source: str

    @property
    def key(self) -> str:
        """Redis/DB period key — ``YYYY-MM-DD`` of the window start.

        Ten characters exactly, which is what ``quota_usage.period`` holds:
        the pool counters ride the ordinary quota-usage tables so they inherit
        their durability (see ``_POOL_USAGE_TYPE``).
        """
        return self.start.strftime("%Y-%m-%d")


def _add_years(moment: datetime, years: int) -> datetime:
    """Shift *moment* by whole years, surviving 29 February.

    A license issued on a leap day has no anniversary three years out of four;
    it lands on 28 February in those years, which is the convention every
    subscription uses and the only one that keeps the windows contiguous.
    """
    try:
        return moment.replace(year=moment.year + years)
    except ValueError:
        return moment.replace(year=moment.year + years, day=28)


def _window_from_anchor(anchor: datetime, now: datetime, source: str) -> PoolWindow:
    """Resolve the current one-year window of the series anchored at *anchor*."""
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    # An anchor in the future (clock skew, a license not yet started) would
    # otherwise produce a window that has not begun; clamp to one window
    # starting at the anchor so the key stays stable and legible.
    elapsed = now.year - anchor.year
    if elapsed < 0:
        elapsed = 0
    while elapsed > 0 and _add_years(anchor, elapsed) > now:
        elapsed -= 1
    while _add_years(anchor, elapsed + 1) <= now:
        elapsed += 1
    return PoolWindow(
        start=_add_years(anchor, elapsed),
        end=_add_years(anchor, elapsed + 1),
        source=source,
    )


async def resolve_pool_window(db: AsyncSession | None) -> PoolWindow:
    """Return the pool's current renewal window.

    Anchored on the Filigran license start date when one is readable, else on this
    platform instance's creation date. With no session at all (a caller that
    cannot query — the degraded path in ``quota.py``) the window falls back to
    the calendar year, so the key is still stable and still renews annually; it
    just may not sit on the license anniversary.

    Never raises: a failure to read the license or the platform row degrades to
    the calendar year rather than blocking a refund on an error path.
    """
    now = datetime.now(UTC)
    calendar_anchor = datetime(now.year, 1, 1, tzinfo=UTC)
    if db is None:
        return _window_from_anchor(calendar_anchor, now, "calendar")
    try:
        from app.api.v1.platform_settings_helpers import (
            get_platform_settings as _load_ps,
        )

        ps = await _load_ps(db)
        anchor: datetime | None = None
        source = "platform"
        if settings.is_xtm_one:
            from app.enterprise.licensing import get_xtm_license_info
            from app.enterprise.xtm_ca import XTM_CA_PEM

            info = get_xtm_license_info(
                pem_from_env=settings.enterprise_license,
                pem_from_db=ps.enterprise_license if ps else None,
                ca_pem=XTM_CA_PEM,
                platform_created_at=ps.created_at if ps else None,
            )
            # Only a cert that actually verified may anchor the window — an
            # unvalidated PEM's dates are attacker-controlled, and moving the
            # anniversary is worth a forged license on its own.
            if info.license_validated and info.license_start_date:
                anchor = info.license_start_date
                source = "xtm_license"
        if anchor is None and ps is not None and ps.created_at:
            anchor = ps.created_at
        if anchor is None:
            anchor, source = calendar_anchor, "calendar"
        return _window_from_anchor(anchor, now, source)
    except Exception:
        logger.debug("Pool window resolution failed — using the calendar year", exc_info=True)
        return _window_from_anchor(calendar_anchor, now, "calendar")


# ---------------------------------------------------------------------------
# Support codes
# ---------------------------------------------------------------------------

SUPPORT_CODE_PREFIX = "XTMP-"
POOL_RECHARGE_KIND = "agent_failure_pool_recharge"
# Resets the platform-wide usage counters the license ceilings are enforced
# against - redeemed through ``support_codes.redeem_support_code``.
PLATFORM_QUOTA_RESET_KIND = "platform_quota_reset"
GLOBAL_PLATFORM_SCOPE = "global"

# What each kind grants, in the words a refusal uses.
_KIND_GRANTS = {
    POOL_RECHARGE_KIND: "an agent failure pool recharge",
    PLATFORM_QUOTA_RESET_KIND: "a reset of the platform-wide usage counters",
}

# Bounds every field that becomes part of a query or a stored row, so a signed
# payload cannot push oversized values into the ledger. A code is minted by
# Filigran, but "signed" is not "sane".
_MAX_CODE_CHARS = 8192
_MAX_ID_CHARS = 64
_MAX_NOTE_CHARS = 500


class SupportCodeError(Exception):
    """A support code was refused. The message is shown to the admin verbatim."""


@dataclass(frozen=True)
class SupportCode:
    """A verified support code's payload."""

    code_id: str
    kind: str
    platform_scope: str | None
    issued_at: datetime | None
    expires_at: datetime | None
    note: str | None
    # The whole signed payload, for the claims only one kind reads (a reset's
    # ``quota_types`` / ``periods``). Trusted as Filigran's, never as sane:
    # whoever reads a claim bounds it.
    claims: Mapping[str, object] = field(default_factory=dict, compare=False)


def _b64url_decode(segment: str) -> bytes:
    """Decode one base64url segment, tolerating stripped ``=`` padding."""
    padding_needed = (-len(segment)) % 4
    try:
        return base64.urlsafe_b64decode(segment + "=" * padding_needed)
    except (binascii.Error, ValueError) as exc:
        raise SupportCodeError(
            "This support code is malformed and could not be read. Copy it "
            "again from the message Filigran support sent you."
        ) from exc


def _parse_timestamp(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def verify_support_code(
    code: str,
    ca_pem: str,
    platform_id: str | None,
    *,
    expected_kind: str | Collection[str] = POOL_RECHARGE_KIND,
    now: datetime | None = None,
) -> SupportCode:
    """Verify a support code and return its payload, or raise ``SupportCodeError``.

    Shape: ``XTMP-<b64url(payload JSON)>.<b64url(RSA signature)>``. The
    signature covers the payload's **raw bytes** — not a re-serialisation of
    the parsed JSON — so verification cannot be broken by key ordering or
    whitespace, and the platform never has to agree with the issuer on a
    canonical JSON form.

    The trust anchor is the license CA already embedded in the image, so this
    is the same offline check ``decode_license_pem`` performs, minus X.509:
    a ~700-character token is something an admin can paste out of a support
    message, where a full certificate is not.

    *expected_kind* is the grant, or the grants, the caller can apply; a code
    for any other kind is refused before its id is read.
    """
    from app.enterprise.licensing import _load_trust_anchor_public_key

    now = now or datetime.now(UTC)
    cleaned = "".join((code or "").split())
    if not cleaned:
        raise SupportCodeError("Enter the support code Filigran sent you.")
    if len(cleaned) > _MAX_CODE_CHARS:
        raise SupportCodeError("This support code is too long to be valid.")
    if not cleaned.startswith(SUPPORT_CODE_PREFIX):
        raise SupportCodeError(
            f"This does not look like a support code — one starts with "
            f"'{SUPPORT_CODE_PREFIX}'."
        )
    body = cleaned[len(SUPPORT_CODE_PREFIX) :]
    if body.count(".") != 1:
        raise SupportCodeError(
            "This support code is malformed and could not be read. Copy it "
            "again from the message Filigran support sent you."
        )
    payload_segment, signature_segment = body.split(".")
    payload_bytes = _b64url_decode(payload_segment)
    signature = _b64url_decode(signature_segment)

    # Signature FIRST: nothing inside the payload is trusted — not its kind,
    # not its expiry, not its length — until the signature says Filigran wrote
    # it.
    try:
        public_key = _load_trust_anchor_public_key(ca_pem)
        public_key.verify(
            signature, payload_bytes, padding.PKCS1v15(), hashes.SHA256()
        )
    except InvalidSignature as exc:
        raise SupportCodeError(
            "This support code was not issued by Filigran, or has been "
            "altered — its signature does not verify."
        ) from exc
    except Exception as exc:
        logger.warning("Support code signature check failed", exc_info=True)
        raise SupportCodeError(
            "This support code could not be verified. Contact Filigran "
            "support if it was issued to you."
        ) from exc

    try:
        payload = json.loads(payload_bytes.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupportCodeError(
            "This support code carries an unreadable payload."
        ) from exc
    if not isinstance(payload, dict):
        raise SupportCodeError("This support code carries an unreadable payload.")

    expected = (
        {expected_kind} if isinstance(expected_kind, str) else set(expected_kind)
    )
    kind = str(payload.get("kind") or "")
    if kind not in expected:
        wanted = " or ".join(sorted(_KIND_GRANTS.get(k, k) for k in expected))
        granted = _KIND_GRANTS.get(kind)
        raise SupportCodeError(
            f"This support code is valid but does not grant {wanted}: "
            + (
                f"it grants {granted}."
                if granted
                else "it grants something this version of the platform "
                "cannot apply."
            )
        )

    code_id = str(payload.get("id") or "")
    if not code_id or len(code_id) > _MAX_ID_CHARS:
        raise SupportCodeError(
            "This support code has no usable identifier and cannot be tracked "
            "as used."
        )

    scope = payload.get("platform")
    platform_scope = str(scope) if scope else GLOBAL_PLATFORM_SCOPE
    if platform_scope != GLOBAL_PLATFORM_SCOPE:
        # Fail CLOSED when this platform cannot identify itself: accepting a
        # code minted for someone else's install because ``PLATFORM_ID`` is
        # unset would make the binding decorative. A code meant for any install
        # is minted ``global`` — that is the escape hatch, not an empty id.
        if not platform_id:
            raise SupportCodeError(
                "This support code was issued for a specific platform, but this "
                "instance has no PLATFORM_ID configured, so it cannot verify "
                "the code was meant for it. Set PLATFORM_ID, or ask Filigran "
                "support for a platform-independent code."
            )
        if platform_scope != platform_id:
            raise SupportCodeError(
                "This support code was issued for a different platform."
            )

    expires_at = _parse_timestamp(payload.get("exp"))
    if expires_at is not None and now > expires_at:
        raise SupportCodeError(
            f"This support code expired on {expires_at.date().isoformat()}."
        )

    note = payload.get("note")
    return SupportCode(
        code_id=code_id,
        kind=kind,
        platform_scope=platform_scope,
        issued_at=_parse_timestamp(payload.get("iat")),
        expires_at=expires_at,
        note=(str(note)[:_MAX_NOTE_CHARS] if note else None),
        claims=payload,
    )


# ---------------------------------------------------------------------------
# Recharge
# ---------------------------------------------------------------------------

# Redis sweep bounds for the recharge. The batch size keeps the advisory lock
# held for a handful of round trips instead of one per key; the ceiling stops a
# pathological keyspace from holding it indefinitely — loudly, never silently
# (see ``_clear_pool_counters``).
_SWEEP_BATCH = 500
_SWEEP_MAX_KEYS = 100_000



async def _clear_pool_counters(db: AsyncSession) -> dict:
    """Zero every failure-pool counter, in Redis and in the durable table, and
    commit *db*.

    Runs under the *same* advisory lock ``flush_quota_usage`` takes. That
    ordering is the whole point: the flush copies a Redis snapshot into
    ``quota_usage`` with a monotonic ``max()``, so a flush interleaved with
    this deletion would resurrect the rows it had already read and the
    recharge would silently do nothing. Holding the lock means an in-flight
    flush finishes, or stands aside, first and a later one finds no keys. A
    flush that keeps the lock past the bounded wait fails the recharge with
    ``QuotaClearBusyError`` before anything is cleared, so the code is not
    burned (``quota._wait_for_flush_lock``).

    Redis is emptied inside the lock as well, for the same reason.

    The platform's pool-clear lock is held too, so a cold claim waits for this
    transaction to end and reads the pool it leaves - empty once committed,
    as it was if rolled back - instead of seeding back a row deleted here
    (``quota._load_pool_baseline``).

    The counters leave Redis through a stash (``quota._CLEAR_STASH_PREFIX``),
    dropped once the recharge has committed and put back if it does not, so a
    recharge that fails loses no increment only Redis held.
    """
    from app.models.quota import QuotaUsage

    redis = await redis_client()
    if not await _wait_for_flush_lock(db, redis):
        raise QuotaClearBusyError(QUOTA_CLEAR_BUSY_MESSAGE)
    await _lock_pool_clear(db, _GLOBAL_REFUND_EPOCH)
    await _settle_clear_stashes(redis, db)
    stash = await _open_clear_stash(redis, db)
    try:
        removed = (
            await db.execute(
                sa_delete(QuotaUsage)
                .where(QuotaUsage.usage_type == _POOL_USAGE_TYPE)
                .returning(
                    QuotaUsage.user_id,
                    QuotaUsage.agent_id,
                    QuotaUsage.period,
                    QuotaUsage.count,
                )
            )
        ).all()
        deleted = len(removed)
        # The durable rows a cold claim seeds these counters from once this
        # transaction is over; a rolled-back recharge puts them back.
        seeds = {
            _usage_key(row.user_id, _POOL_USAGE_TYPE, None, row.period, row.agent_id): (
                row.count
            )
            for row in removed
        }

        # The Redis sweep stays INSIDE the lock, and batches rather than moving
        # out of it. Deleting the rows, committing, then sweeping would shorten
        # the hold but reopen the exact race the lock is here for: a flush
        # landing in that gap re-persists the counters it had already read, and
        # the recharge silently does nothing. Batched moves cut the hold to a
        # handful of round trips instead of one per key, which addresses the
        # duration without giving up the ordering.
        cleared = 0
        truncated = False
        batch: list[str] = []
        async for raw_key in redis.scan_iter(
            match=f"{_USAGE_PREFIX}*:{_POOL_USAGE_TYPE}:*", count=500
        ):
            batch.append(raw_key if isinstance(raw_key, str) else raw_key.decode())
            if len(batch) >= _SWEEP_BATCH:
                cleared += await _stash_counters(redis, stash, batch, seeds)
                batch.clear()
            if cleared >= _SWEEP_MAX_KEYS:
                truncated = True
                break
        cleared += await _stash_counters(redis, stash, batch, seeds)
        if truncated:
            # Never silently: a partial sweep leaves some agents' pools spent,
            # and an admin who redeemed a code deserves to know it did not
            # fully land.
            logger.error(
                "Agent failure pool sweep stopped at the %d-key bound with keys "
                "still matching — some pools were not recharged. Re-run the "
                "redemption or clear the remaining %s* keys manually.",
                _SWEEP_MAX_KEYS,
                _POOL_USAGE_TYPE,
            )
        # The per-cause breakdown of the totals just zeroed. It lives
        # outside the counters' keyspace on purpose (a hash under
        # ``quota:usage:`` would break ``flush_quota_usage``, which GETs every
        # key there), so the sweep above cannot reach it and it has to be
        # cleared explicitly. Otherwise the card renders the full pre-recharge
        # breakdown against a freshly zeroed headline — the parts no longer
        # summing to the total, at the one moment an admin is looking at that
        # card hardest.
        #
        # Only the POOL kind. Free credits are not rationed and not recharged,
        # so their total survives this and their breakdown must survive with
        # it.
        #
        # The platform epoch moves only now that every pool counter is gone, so
        # a refund still between its claim and its tally files its cause under
        # the old epoch, which the card no longer reads; a tally under
        # the new one was counted after the sweep and is kept.
        causes_cleared = await clear_refund_causes(
            redis,
            _POOL_USAGE_TYPE,
            below_global_epoch=await _advance_refund_epoch(
                redis, _GLOBAL_REFUND_EPOCH
            ),
        )
        await db.commit()
    except Exception:
        await _abandon_clear(db, redis)
        raise
    await _drop_committed_stash(redis, stash)
    logger.info(
        "Agent failure pools recharged: %d durable rows, %d live counters and "
        "%d cause breakdown(s) cleared",
        deleted,
        cleared,
        causes_cleared,
    )
    # Both numbers, not a max(): the two sets overlap only partially, so
    # neither is a total and the larger of them is a count of nothing.
    return {"rows_deleted": deleted, "keys_cleared": cleared, "truncated": truncated}


async def burn_support_code(
    db: AsyncSession,
    verified: SupportCode,
    redeemed_by_user_id: uuid.UUID | None,
) -> None:
    """Record *verified* as used in *db*'s transaction, or raise
    ``SupportCodeError`` when it already is.

    Flushed, never committed: the caller commits it with what the code grants,
    so a grant that fails leaves the code unused. The UNIQUE constraint on
    ``code_id`` is what makes a code single-use, and two admins pasting the
    same code at the same moment are both resolved HERE - one inserts, the
    other is refused - rather than both going on to apply it.
    """
    existing = (
        await db.execute(
            select(SupportCodeRedemption).where(
                SupportCodeRedemption.code_id == verified.code_id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise SupportCodeError(
            "This support code has already been used, on "
            f"{existing.redeemed_at.date().isoformat()}."
        )

    db.add(
        SupportCodeRedemption(
            code_id=verified.code_id,
            kind=verified.kind,
            platform_scope=verified.platform_scope,
            issued_at=verified.issued_at,
            expires_at=verified.expires_at,
            redeemed_by_user_id=redeemed_by_user_id,
            note=verified.note,
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise SupportCodeError(
            "This support code has already been used."
        ) from exc


async def redeem_pool_recharge_code(
    db: AsyncSession,
    code: str,
    redeemed_by_user_id: uuid.UUID | None,
) -> dict:
    """Verify a support code, burn it, and recharge every failure pool.

    Platform-wide by design: the code says "give these people their failures
    back", and a support engineer holding one should not also have to know
    which users and agents were affected. Raises ``SupportCodeError`` (with a
    message meant for the admin) for every refusal, including a replay.
    """
    from app.enterprise.xtm_ca import XTM_CA_PEM

    verified = verify_support_code(
        code, XTM_CA_PEM, settings.platform_id or None
    )
    return await apply_pool_recharge(db, verified, redeemed_by_user_id)


async def apply_pool_recharge(
    db: AsyncSession,
    verified: SupportCode,
    redeemed_by_user_id: uuid.UUID | None,
) -> dict:
    """Burn the verified recharge code *verified* and recharge every pool, in
    one transaction. Raises ``SupportCodeError`` on a replay."""
    await burn_support_code(db, verified, redeemed_by_user_id)

    # Commits the redemption with the recharge, or neither.
    cleared = await _clear_pool_counters(db)

    window = await resolve_pool_window(db)
    return {
        "code_id": verified.code_id,
        "note": verified.note,
        **cleared,
        "pool_size": agent_failure_pool_size(),
        "window_start": window.start.isoformat(),
        "window_end": window.end.isoformat(),
    }


# ---------------------------------------------------------------------------
# Admin read model
# ---------------------------------------------------------------------------

_STATUS_MAX_ROWS = 100


def _cause_breakdown(counts: dict[str, int], total: int) -> list[dict]:
    """Order a cause tally, biggest first, and reconcile it against *total*.

    A shortfall becomes an explicit ``not_recorded`` entry instead of being
    dropped, so the parts always sum to the headline number an admin is looking
    at. An OVERSHOOT is clamped away rather than shown: the tally and the
    counters are reconciled from different places (a hash vs. per-agent keys
    merged with their durable rows), so the breakdown can transiently run ahead
    of a total that has not been flushed yet, and a negative remainder would be
    a rendering bug rather than information.
    """
    ordered = [
        {"cause": cause, "count": count}
        for cause, count in sorted(
            counts.items(), key=lambda kv: (-kv[1], kv[0])
        )
        if count > 0
    ]
    remainder = total - sum(item["count"] for item in ordered)
    if remainder < 0:
        # Clamped below, but never silently. The structural desyncs are gone:
        # the totals keep the credits of deleted users and agents and
        # sum each counter uncapped (a pool size lowered mid-window), and the
        # tally is read for exactly the users whose counters make the total (a
        # quota reset takes a user's causes out with their counters). What is
        # left is a card read racing a credit (the counter read before it, the
        # tally after), which the next read settles, and a counter lost from
        # Redis before it was ever flushed while its user's tally survived: a
        # Redis eviction, in practice.
        logger.warning(
            "Credit-back cause tally exceeds its total by %d (tallied %d, "
            "total %d) — transient flush lag, or a tally that has desynced "
            "from the counters it decomposes",
            -remainder,
            sum(item["count"] for item in ordered),
            total,
        )
        # Capped at the headline, biggest causes kept whole: the parts must
        # never add up to more credits than the card says were given.
        room = max(total, 0)
        capped = []
        for item in ordered:
            count = min(item["count"], room)
            if count > 0:
                capped.append({"cause": item["cause"], "count": count})
            room -= count
        ordered = capped
    if remainder > 0:
        # NOT ``unknown``: that is a real classification meaning "we looked and
        # could not attribute it". This means the credit was never tallied
        # against any cause at all — granted before this shipped, or lost with
        # the Redis hash. Same distinction as between ``unknown`` and
        # ``unrecorded`` on the trace filter, kept for the same reason.
        ordered.append({"cause": "not_recorded", "count": remainder})
    return ordered


async def get_pool_status(db: AsyncSession) -> dict:
    """Admin view of the failure pools: window, per-agent consumption, history.

    Reads the durable rows and the live Redis counters and keeps the larger of
    the two per (user, agent) — the same reconciliation
    ``get_platform_quota_monitoring`` performs, and for the same reason: the
    flush lags by up to a cycle, while Redis can have lost a window it already
    persisted.
    """
    from app.models.agent import Agent
    from app.models.quota import QuotaUsage
    from app.models.user import User

    pool_size = agent_failure_pool_size()
    window = await resolve_pool_window(db)
    redis = await redis_client()

    async def _counts_for(usage_type: str) -> dict[tuple[str, str], int]:
        """Per-(user, agent) counter for one usage type, DB and Redis merged.

        Keeps the larger of the two, the same reconciliation
        ``get_platform_quota_monitoring`` performs and for the same reason: the
        flush lags by up to a cycle, while Redis can have lost a window it
        already persisted.
        """
        merged: dict[tuple[str, str], int] = {}
        rows = (
            await db.execute(
                select(
                    QuotaUsage.user_id, QuotaUsage.agent_id, QuotaUsage.count
                ).where(
                    QuotaUsage.usage_type == usage_type,
                    QuotaUsage.period == window.key,
                    QuotaUsage.agent_id.is_not(None),
                )
            )
        ).all()
        for row in rows:
            merged[(str(row.user_id), str(row.agent_id))] = row.count or 0

        # Collect first, then ONE ``MGET``: this runs on a synchronous
        # admin endpoint, and a GET per matched key made the card's cost a
        # round trip per (user, agent) pair.
        pattern = f"{_USAGE_PREFIX}*:{usage_type}:_total:{window.key}:*"
        keys: list[str] = []
        idents: list[tuple[str, str]] = []
        async for raw_key in redis.scan_iter(match=pattern, count=500):
            key = raw_key if isinstance(raw_key, str) else raw_key.decode()
            parts = key.removeprefix(_USAGE_PREFIX).split(":")
            if len(parts) != 5 or parts[4] == "_all":
                continue
            keys.append(key)
            idents.append((parts[0], parts[4]))
        values = await redis.mget(keys) if keys else []
        for ident, value in zip(idents, values, strict=False):
            if value is None:
                continue
            merged[ident] = max(merged.get(ident, 0), int(value))
        return merged

    # The two credit-back reasons, counted apart: the pool is an allowance
    # that runs out, the free tally is an incident count that never does.
    counts = await _counts_for(_POOL_USAGE_TYPE)
    free_counts = await _counts_for(_FREE_REFUND_USAGE_TYPE)

    # ...and what each of them was actually forgiving. Two totals with
    # no breakdown tell an admin how much was credited back but nothing about
    # what to fix; "42 free credits" is a very different situation depending on
    # whether it is one provider outage or forty agents that cannot resolve a
    # model. Read for exactly the users whose counters make each total,
    # so a user whose counters are gone cannot leave their causes behind.
    pool_causes = await read_refund_causes(
        _POOL_USAGE_TYPE, window.key, user_ids={user for user, _agent in counts}
    )
    free_causes = await read_refund_causes(
        _FREE_REFUND_USAGE_TYPE,
        window.key,
        user_ids={user for user, _agent in free_counts},
    )

    agent_ids: set[uuid.UUID] = set()
    user_ids: set[uuid.UUID] = set()
    for user_str, agent_str in set(counts) | set(free_counts):
        try:
            user_ids.add(uuid.UUID(user_str))
            agent_ids.add(uuid.UUID(agent_str))
        except ValueError:
            continue

    agent_names: dict[str, str] = {}
    builtin_agents: set[str] = set()
    if agent_ids:
        for row in (
            await db.execute(
                select(Agent.id, Agent.name, Agent.is_builtin).where(
                    Agent.id.in_(agent_ids)
                )
            )
        ).all():
            agent_names[str(row.id)] = row.name
            if row.is_builtin:
                builtin_agents.add(str(row.id))
    user_emails: dict[str, str] = {}
    if user_ids:
        for row in (
            await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
        ).all():
            user_emails[str(row.id)] = row.email

    # Worst first — the order the card serves them in. Rows whose agent AND
    # user are both gone are stale key debris, not something an admin can act
    # on, and are dropped from the ROWS here. Their credits are not dropped
    # from the totals: the cause breakdown still counts them (their
    # counters are what put their users in it), so a total without them no
    # longer matched the breakdown decomposing it. They are reported as
    # ``stale_*`` instead.
    all_pairs = set(counts) | set(free_counts)
    rows = sorted(
        (
            pair
            for pair in all_pairs
            if agent_names.get(pair[1]) or user_emails.get(pair[0])
        ),
        key=lambda pair: (
            -counts.get(pair, 0),
            -free_counts.get(pair, 0),
            agent_names.get(pair[1]) or "",
        ),
    )

    def _pool_spent(pair: tuple[str, str]) -> bool:
        return pool_size > 0 and counts.get(pair, 0) >= pool_size

    # Whose quota the pool actually applies to. A user with no limit at
    # any tier is never pooled by the refund path any more, so a row of theirs
    # must not read "Spent" nor count under "pools exhausted" - the slots it
    # shows were spent before the rule landed (or under a limit since lifted),
    # and they are not what is happening to that user now. Resolved through the
    # SAME predicate the refund path runs, once per distinct user, with the
    # platform tier read once rather than per user (it parses the license) -
    # and only for the users it can change the card for: the rows the card
    # serves (``_STATUS_MAX_ROWS``, worst first) plus every row that would
    # otherwise read "exhausted", so the ``exhausted`` total stays exact while
    # a tenant with a long tail of historical rows does not pay one resolution
    # per user on every admin page load. Deliberately not a batched
    # resolution: that would be a second spelling of the gate's own tier
    # merge, which is the one thing this predicate must never have.
    # Fail toward "enforced": an unreadable config keeps the card's old reading
    # rather than hiding a real exhaustion.
    platform_cap = await agentic_platform_cap_enforced(db)
    platform_quota_unlimited = await platform_agentic_quota_is_unlimited(
        db, platform_cap_enforced=platform_cap
    )
    unlimited_users: set[str] = set()
    if not platform_cap:
        candidates = {user_str for user_str, _agent_str in rows[:_STATUS_MAX_ROWS]}
        candidates.update(pair[0] for pair in rows if _pool_spent(pair))
        for user_str in sorted(candidates):
            try:
                enforced = await has_enforceable_agentic_limit(
                    db, redis, uuid.UUID(user_str), platform_cap_enforced=False
                )
            except Exception:
                logger.debug(
                    "Could not resolve the agentic limit of user %s for the "
                    "failure pool card - treating it as enforced",
                    user_str,
                    exc_info=True,
                )
                continue
            if not enforced:
                unlimited_users.add(user_str)

    entries = [
        {
            "user_id": user_str,
            "user_email": user_emails.get(user_str),
            "agent_id": agent_str,
            # A counter can outlive its agent between the CASCADE delete and
            # the key's expiry; surface it rather than dropping the row.
            "agent_name": agent_names.get(agent_str),
            "used": counts.get((user_str, agent_str), 0),
            "remaining": max(pool_size - counts.get((user_str, agent_str), 0), 0),
            # Never "exhausted" for a user the pool does not apply to: the
            # amber "Spent - now spending actions on failures" reading would be
            # a quota alarm about a quota that does not exist.
            "exhausted": (
                _pool_spent((user_str, agent_str))
                and user_str not in unlimited_users
            ),
            # This user's effective agentic quota is unlimited, so failed runs
            # cost them nothing and the pool is not consumed for them. The
            # ``used`` count is history, not a live ration.
            "quota_unlimited": user_str in unlimited_users,
            # Credited back for free — no model was ever called (provider
            # unreachable / down / not configured, or the run abandoned by the
            # orchestration engine). Never bounded, so it has no "remaining".
            "free_refunds": free_counts.get((user_str, agent_str), 0),
            # A seeded platform agent. Its scheduled runs are quota-exempt and
            # can therefore never appear here at all — only its manually
            # triggered runs are charged, and those are what produced this row.
            # Flagged rather than filtered: hiding a charge that really happened
            # would make this card disagree with the quota gauges above it.
            "agent_is_builtin": agent_str in builtin_agents,
        }
        for (user_str, agent_str) in rows
    ]

    # The credits granted, as the counters record them. Summed UNCAPPED: a
    # counter holds the slots its claims were granted (a claim over the limit
    # is handed straight back), and clamping it at today's size under-reported
    # the credits already given whenever an operator lowered
    # AGENT_FAILURE_QUOTA_POOL mid-window - while the cause tally, which
    # records every granted credit, kept them.
    stale_pairs = all_pairs - set(rows)
    stale_forgiven = sum(counts.get(pair, 0) for pair in stale_pairs)
    stale_free = sum(free_counts.get(pair, 0) for pair in stale_pairs)
    failures_forgiven = sum(e["used"] for e in entries) + stale_forgiven
    free_credits = sum(e["free_refunds"] for e in entries) + stale_free

    redemptions = (
        await db.execute(
            select(SupportCodeRedemption)
            .order_by(SupportCodeRedemption.redeemed_at.desc())
            .limit(20)
        )
    ).scalars().all()
    redeemer_ids = {r.redeemed_by_user_id for r in redemptions if r.redeemed_by_user_id}
    redeemer_emails: dict[str, str] = {}
    if redeemer_ids:
        for row in (
            await db.execute(
                select(User.id, User.email).where(User.id.in_(redeemer_ids))
            )
        ).all():
            redeemer_emails[str(row.id)] = row.email

    return {
        "enabled": pool_size > 0,
        "pool_size": pool_size,
        # Nothing platform-wide caps agentic actions - no license ceiling /
        # platform limit, no platform default. The pool then only applies to
        # users or groups carrying a limit of their own; the card says
        # so instead of presenting an allowance nobody is drawing on.
        "platform_quota_unlimited": platform_quota_unlimited,
        "window": {
            "start": window.start.isoformat(),
            "end": window.end.isoformat(),
            "source": window.source,
            "days_remaining": max((window.end - datetime.now(UTC)).days, 0),
        },
        "totals": {
            "agents_with_failures": len(entries),
            "exhausted": sum(1 for e in entries if e["exhausted"]),
            # Pool-funded credits (agent failures) and free ones (the LLM was
            # never reached) are reported apart on purpose: only the first is
            # rationed, and an admin looking at a big number needs to know
            # which of the two it is before doing anything about it.
            "failures_forgiven": failures_forgiven,
            "free_credits": free_credits,
            # Credits whose user AND agent were both deleted since.
            # Included in the two totals above, which the cause breakdown
            # decomposes, but in no row: there is nobody left to act on.
            "stale_pairs": len(stale_pairs),
            "stale_failures_forgiven": stale_forgiven,
            "stale_free_credits": stale_free,
        },
        # Per-cause decomposition of the two totals above. Each is a
        # descending list of ``{cause, count}`` plus, when the tallied causes
        # do not add up to their total, an explicit ``not_recorded`` remainder.
        # The remainder is shown rather than absorbed: the breakdown is
        # Redis-only (see ``quota._refund_cause_key``) and predates nothing, so
        # a cache wipe or a credit granted before this shipped would otherwise
        # silently shrink the picture an admin is reading.
        "forgiven_by_cause": _cause_breakdown(pool_causes, failures_forgiven),
        "free_credits_by_cause": _cause_breakdown(free_causes, free_credits),
        "entries": entries[:_STATUS_MAX_ROWS],
        "truncated": len(entries) > _STATUS_MAX_ROWS,
        "redemptions": [
            {
                "code_id": r.code_id,
                "kind": r.kind,
                "note": r.note,
                "redeemed_at": r.redeemed_at.isoformat() if r.redeemed_at else None,
                "redeemed_by": (
                    redeemer_emails.get(str(r.redeemed_by_user_id))
                    if r.redeemed_by_user_id
                    else None
                ),
            }
            for r in redemptions
        ],
    }


__all__ = [
    "GLOBAL_PLATFORM_SCOPE",
    "PLATFORM_QUOTA_RESET_KIND",
    "POOL_RECHARGE_KIND",
    "SUPPORT_CODE_PREFIX",
    "PoolWindow",
    "SupportCode",
    "SupportCodeError",
    "apply_pool_recharge",
    "burn_support_code",
    "get_pool_status",
    "redeem_pool_recharge_code",
    "resolve_pool_window",
    "verify_support_code",
]
