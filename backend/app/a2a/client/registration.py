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

"""Verifying a registration end to end: credentials, card, verdict.

The seam between :mod:`app.a2a.client.credentials` (how do I authenticate to
this target) and :mod:`app.a2a.client.verification` (is this card usable), plus
the database write. Kept apart from both so the defective-card matrix stays
testable with no session and no credential store in sight.

One rule worth stating because it looks like an omission: card retrieval
presents the registration's credentials **where the target requires them**.
Two targets serve their cards only to an authenticated caller; the third does
not, because this platform serves its own cards anonymously so a partner can
register from a URL alone (FR-014, as corrected). Failing a registration
because we could not mint a token for a card that needs none would refuse the
one target testable without a vendor account.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.a2a.client.credentials import CredentialError, forget_token, outbound_headers
from app.a2a.client.verification import Verdict, apply_verdict, verify_card

# Where a partner lists the agents a credential reaches. Not an A2A method —
# see ``a2a_server.list_partner_agents``. Imported rather than re-spelled: it
# is the one definition our own card advertises, our own route serves, and a
# partner's declaration is matched against, so those cannot disagree about
# where this lives.
from app.a2a.extensions import PARTNER_DISCOVERY_PATH
from app.a2a.providers import GOOGLE_A2A_PROVIDER, OAUTH_A2A_PROVIDER
from app.core.config import settings as app_settings
from app.models.a2a import (
    UNUSABLE_CARD_UNFETCHABLE,
    UNUSABLE_CREDENTIAL_NO_TOKEN,
    USABILITY_UNUSABLE,
    USABILITY_USABLE,
)

logger = logging.getLogger(__name__)

#: Targets whose card endpoint requires a credential. The OAuth target is
#: absent on purpose — see the module docstring.
_CARD_NEEDS_CREDENTIALS = frozenset({GOOGLE_A2A_PROVIDER, "entra_a2a"})

#: Targets whose card is anonymous but whose credential an operator's Verify
#: must still test (#105): without the mint, Verify answered usable on a
#: secret the partner refuses, and the first real send turned it red.
_VERIFY_MINTS_CREDENTIALS = frozenset({OAUTH_A2A_PROVIDER})

#: A card fetch is a small GET. Bounded well below the delegation ceiling so a
#: partner cannot hold a verification open the way it might hold a task.
_CARD_TIMEOUT_SECONDS = 20.0


#: How long a delegation waits for another session to release a target row
#: before refusing as retryable. The holder commits within one write (the
#: card fetch happens before any lock), so this only covers a slow commit.
_CONTENDED_WAIT_SECONDS = 5.0
_CONTENDED_POLL_SECONDS = 0.25

#: Every column :func:`apply_verdict` may write: what a delegation copies from
#: the committed row onto its own copy of the target.
_VERDICT_COLUMNS = (
    "usability",
    "unusable_reason",
    "last_verified_at",
    "last_verification_error",
    "card_url",
    "signature_state",
    "agreed_protocol_version",
    "pinned_origin",
    "invocation_url",
    "transport",
    "card_fingerprint",
    "accepted_card_fingerprint",
    "partner_discovery_path",
    "declared_name",
    "declared_description",
    "declared_skills",
    "declared_input_types",
    "declared_output_types",
    "declares_callbacks",
)


class VerdictWriteContendedError(Exception):
    """Another session held the target row past the wait. Transient.

    The holder is a sweep cycle, an operator's Verify or another delegation
    re-verifying the same agent; each releases the row at its commit. The
    caller refuses this attempt as retryable instead of waiting out the
    statement timeout, which cancels the statement and rolls back the session.
    """


async def verify_registration(db, integration, target, *, trigger: str = "manual") -> Verdict:
    """Verify ONE target's card and write the verdict. Never raises.

    A target, not a registration: a partner exposes as many agents as it
    likes, each with its own card, and one verdict per partner could only ever
    describe one of them. The credential is still the partner's — read from
    the integration — because that is the thing one operator decision trusts.

    Returns the verdict rather than a bool because every caller needs the
    reason, not just the outcome: the health surface renders it, the breaker
    reads its kind, and the sweep counts it.

    The card is fetched before anything is written, so the row lock the
    write takes is held from the flush to the caller's commit, never across
    the fetch.
    """
    from app.a2a import metrics as a2a_metrics

    verdict, url = await _fetch_verdict(integration, target, trigger=trigger)
    apply_verdict(target, verdict, card_url=url)
    a2a_metrics.record_card_verification("usable" if verdict.is_usable else "unusable", trigger)
    await db.flush()
    return verdict


async def reverify_before_delegating(integration: Any, target: Any) -> None:
    """Re-verify a stale target for a delegation, outside its transaction.

    FR-050 as :func:`verify_registration` does it, with the write in a short
    session of its own: the card is fetched first, then the row is claimed
    with ``SKIP LOCKED``, written and committed at once. The delegation's own
    session never updates the row, so a sweep, a Verify or a concurrent
    delegation holding it cannot cancel the send or roll that session back.

    Another verifier that committed a fresh verdict while this one fetched
    wins: the committed row is what the gate reads. The committed state is
    copied onto *target* without marking it changed, so the caller's session
    writes nothing for it. Raises :class:`VerdictWriteContendedError` when the
    row stays held past the wait.
    """
    import asyncio
    import time

    from sqlalchemy import select

    from app.a2a import metrics as a2a_metrics
    from app.core.database import async_session
    from app.models.a2a import A2AClientTarget

    verdict, url = await _fetch_verdict(integration, target, trigger="delegation")
    deadline = time.monotonic() + _CONTENDED_WAIT_SECONDS
    async with async_session() as own_db:
        while True:
            row = (
                await own_db.execute(
                    select(A2AClientTarget).where(A2AClientTarget.id == target.id).with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is not None:
                break
            exists = (
                await own_db.execute(select(A2AClientTarget.id).where(A2AClientTarget.id == target.id))
            ).scalar_one_or_none()
            await own_db.rollback()
            if exists is None:
                # Removed while the card was fetched: nothing to write, and
                # nothing to send to.
                _adopt(
                    target,
                    {
                        "usability": USABILITY_UNUSABLE,
                        "unusable_reason": UNUSABLE_CARD_UNFETCHABLE,
                        "last_verification_error": "This partner agent was removed.",
                    },
                )
                return
            if time.monotonic() >= deadline:
                raise VerdictWriteContendedError(
                    "Another verification of this partner agent is still "
                    "writing its verdict; call again in a few seconds."
                )
            await asyncio.sleep(_CONTENDED_POLL_SECONDS)
        if verdict_is_stale(row):
            apply_verdict(row, verdict, card_url=url)
            a2a_metrics.record_card_verification("usable" if verdict.is_usable else "unusable", "delegation")
        committed = {name: getattr(row, name) for name in _VERDICT_COLUMNS}
        await own_db.commit()
    _adopt(target, committed)


def _adopt(target: Any, values: dict[str, Any]) -> None:
    """Copy committed verdict columns onto *target* without dirtying it."""
    from sqlalchemy.orm.attributes import set_committed_value

    mapped = hasattr(target, "_sa_instance_state")
    for name, value in values.items():
        if mapped:
            set_committed_value(target, name, value)
        else:
            setattr(target, name, value)


async def _fetch_verdict(integration: Any, target: Any, *, trigger: str) -> tuple[Verdict, str | None]:
    """Fetch and judge one target's card. Network only: no session, no write."""
    url = target.card_url
    if not url:
        # Only reachable for a row written by hand: ``card_url`` is NOT NULL,
        # and a target IS a card.
        return (
            Verdict(
                usability=USABILITY_UNUSABLE,
                unusable_reason="card_unfetchable",
                error_detail="This target has no card URL.",
            ),
            None,
        )

    async with httpx.AsyncClient(
        timeout=_CARD_TIMEOUT_SECONDS,
        # FR-034: a redirect is one of the ways a partner could move us to an
        # origin we never verified, so the client does not follow any.
        follow_redirects=False,
    ) as client:
        verdict: Verdict | None = None
        headers: dict[str, str] = {}
        if integration.provider in _CARD_NEEDS_CREDENTIALS:
            try:
                # An operator's Verify tests the credential, not a cached token.
                headers = await outbound_headers(
                    integration, client, fresh=trigger == "manual"
                )
            except CredentialError as exc:
                verdict = Verdict(
                    usability=USABILITY_UNUSABLE,
                    unusable_reason=UNUSABLE_CREDENTIAL_NO_TOKEN,
                    error_detail=str(exc),
                )
            except Exception as exc:
                # The token endpoint, not the card: transient, and it says
                # nothing about the credential — so unfetchable, not the
                # durable reason that would send an operator to re-type a
                # good secret.
                verdict = Verdict(
                    usability=USABILITY_UNUSABLE,
                    unusable_reason=UNUSABLE_CARD_UNFETCHABLE,
                    error_detail=(
                        "Could not mint a token to fetch the card — "
                        f"{type(exc).__name__}: {exc}"
                    ),
                )

        if verdict is None and integration.provider in _VERIFY_MINTS_CREDENTIALS:
            # Every verification, the sweep's and a delegation's re-verify
            # included, not only an operator's: otherwise a wrong secret flips
            # back to usable on the anonymous card at the next sweep and a
            # fixed one never heals the target by itself (#105). Only Verify
            # bypasses the token cache.
            verdict = await test_credential(integration, client, fresh=trigger == "manual")

        if verdict is None:
            verdict = await verify_card(card_url=url, http_client=client, headers=headers)

    if not verdict.is_usable:
        # Whatever went wrong, the next attempt starts from a fresh mint.
        forget_token(integration)
    return verdict, url


async def test_credential(integration: Any, client: httpx.AsyncClient, *, fresh: bool = True) -> Verdict | None:
    """Mint a fresh token to test the credential; the unusable verdict, or None.

    Test only: the token is not presented to the card, which stays anonymous.
    A mint stamps ``last_token_minted_at`` and restores ``connected``
    (``outbound_headers``); a refusal moves the integration to ``error`` with
    the detail, exactly as a refused delegation does, so the badge after a
    Verify says what the next send will meet.
    """
    try:
        await outbound_headers(integration, client, fresh=fresh)
        return None
    except CredentialError as exc:
        from app.a2a.client.credential_state import record_mint_failure

        await record_mint_failure(integration, str(exc))
        return Verdict(
            usability=USABILITY_UNUSABLE,
            unusable_reason=UNUSABLE_CREDENTIAL_NO_TOKEN,
            error_detail=str(exc),
        )
    except Exception as exc:
        # The token endpoint did not answer: says nothing about the secret.
        return Verdict(
            usability=USABILITY_UNUSABLE,
            unusable_reason=UNUSABLE_CARD_UNFETCHABLE,
            error_detail=f"Could not reach the partner token endpoint — {type(exc).__name__}: {exc}",
        )


def verdict_is_stale(target) -> bool:
    """Whether a delegation must re-verify before submitting (FR-050).

    The binding control, not the sweep. A sweep that never runs widens nothing
    because this is what a delegation actually consults.
    """
    from datetime import UTC, datetime, timedelta

    if target.last_verified_at is None:
        return True
    # Fail-safe: a verdict that says usable while carrying no invocation
    # endpoint is fresh by the clock and useless in practice — delegation has
    # nowhere to send work. Treating it as stale makes such a row heal on the
    # next delegation instead of refusing until its verdict ages out.
    if target.usability == USABILITY_USABLE and not target.invocation_url:
        return True
    threshold = timedelta(seconds=app_settings.a2a_client_verdict_staleness_seconds)
    last = target.last_verified_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return datetime.now(UTC) - last > threshold


#: Partners known not to implement discovery, and when we learned it. The
#: SECOND line of defence: a partner whose cards have been read and declare no
#: discovery extension is never asked at all (:func:`_discovery_path`), so
#: what reaches a 404 is the bootstrap probe against a registration nothing
#: has verified yet, or a partner that declares the extension and does not
#: honour it. Both are worth remembering rather than repeating per poll.
#: Process-local like any cache: losing it on restart costs one probe.
_UNSUPPORTED: dict[str, float] = {}
_UNSUPPORTED_BACKOFF_SECONDS = 600.0



def _discovery_path(targets: list) -> str | None:
    """Where to ask this partner for its agent list, or ``None`` for "do not".

    Three states, and the third is the reason this is a function rather than a
    truth test on one column:

    * **some card declared it** — ask there. The first declaration in the
      targets' stable order wins; every card from one deployment declares the
      same path, and a partner that somehow disagreed with itself is answered
      by asking one of the places it named rather than none.
    * **cards were read and none declared it** — do NOT ask. This is the whole
      point of the extension: a partner that has told us what it implements is
      taken at its word instead of being probed on a path it never advertised.
    * **no card has been read yet** — ask at the extension's default path,
      exactly as before any of this existed.

    That third state is not a hedge, it is unavoidable: a declaration can only
    be known after a card has been verified, and discovery runs before the
    first verification on a brand-new registration. It costs one probe and
    self-corrects on the next cycle — by then a card has been read, and its
    answer decides.

    "A card was read" is ``card_fingerprint``, not ``last_verified_at``: a
    verification that FAILED stamps the time without ever parsing a card, and
    reading that as "the partner declares nothing" would silently switch
    discovery off for a partner whose card merely went unreachable for a
    while.
    """
    for target in targets:
        declared = getattr(target, "partner_discovery_path", None)
        if declared:
            return declared
    if any(getattr(t, "card_fingerprint", None) for t in targets):
        return None
    return PARTNER_DISCOVERY_PATH


async def discover_partner_agents(db, integration) -> tuple[int, int]:
    """Reconcile targets from what the partner says its credential reaches.

    Returns ``(added, removed)``; ``(0, 0)`` when the partner does not answer,
    which is every non-XTM One deployment and the ordinary case.

    Always on where the partner implements it, and NOT gated on a setting.
    Which agents a credential reaches has only ever been the partner's
    decision — it makes that decision when it binds an agent to the
    registration the credential belongs to. A list typed on this side was a
    copy of that decision, and a stale one the moment the partner changed it,
    so following the source is more accurate rather than more permissive. A
    toggle would only have decided how out of date this platform's copy is.

    **Asked only of a partner that says it answers**, once we have read a card
    from it: the endpoint is not an A2A method, and the spec's own way to
    advertise one it does not define is ``capabilities.extensions``, which our
    own cards use (:mod:`app.a2a.extensions`). :func:`_discovery_path` below
    turns what that partner's cards declared into where to ask, or into "do
    not ask".

    Two refusals matter more than the happy path:

    * a discovered URL not on the origin we authenticated to is DISCARDED. The
      list arrives over an authenticated channel, but a partner naming
      somewhere else is the redirect FR-034 refuses, wearing a different hat.
    * the response is only ever a set of card URLs. Every one still runs the
      whole verification — destination guard, TLS, signature, version, origin
      pin — so discovery decides what to LOOK at and never what to trust.
    """
    import time

    from app.a2a.client.credentials import configured_card_urls, outbound_headers
    from app.a2a.client.destinations import assert_allowed_async, origin_of
    from app.a2a.providers import outbound_targets, sync_targets

    key = str(integration.id)
    # ``None`` sentinel, never ``0.0``: monotonic time counts from BOOT, so a
    # zero default reads as "marked at boot" and silently disables discovery
    # for the machine's first ten minutes of uptime.
    marked = _UNSUPPORTED.get(key)
    if marked is not None and time.monotonic() - marked < _UNSUPPORTED_BACKOFF_SECONDS:
        return (0, 0)

    # The partner's origin, from a card URL the operator supplied. Discovery
    # extends a relationship rather than establishing one: without at least
    # one URL there is nobody to ask, and guessing an origin from a token
    # endpoint would be this platform inventing a partner address.
    targets = await outbound_targets(db, integration.id)
    path = _discovery_path(targets)
    if path is None:
        return (0, 0)
    seeds = [t.card_url for t in targets]
    seeds += configured_card_urls(integration)
    if not seeds:
        return (0, 0)
    # Guarded before the bearer minted below is attached: the seed is an
    # operator-typed URL that may never have been verified, and ``origin_of``
    # checks nothing but parseability.
    try:
        origin = await assert_allowed_async(seeds[0])
    except Exception:
        return (0, 0)

    async with httpx.AsyncClient(
        timeout=_CARD_TIMEOUT_SECONDS, follow_redirects=False
    ) as client:
        try:
            headers = await outbound_headers(integration, client)
            response = await client.get(f"{origin}{path}", headers=headers)
        except Exception:
            # A credential that cannot mint, a partner that is not there, a body
            # that is not JSON: discovery is an optimisation and every one of
            # them means "keep the operator's list".
            return (0, 0)
        if response.status_code != 200:
            # 404 is the ordinary answer from a partner that does not
            # implement this, which is every non-XTM One deployment. Recorded
            # so it is not asked again on the next read.
            if response.status_code == 404:
                now = time.monotonic()
                _UNSUPPORTED[key] = now
                # Bounded: an entry past its backoff is dead weight.
                for stale in [
                    k for k, t in _UNSUPPORTED.items()
                    if now - t >= _UNSUPPORTED_BACKOFF_SECONDS
                ]:
                    del _UNSUPPORTED[stale]
            return (0, 0)
        # It answered: a partner that once implemented this is no longer
        # backed off, so re-enabling the feature on their side is picked up.
        _UNSUPPORTED.pop(key, None)
        try:
            payload = response.json()
        except Exception:
            return (0, 0)

    listed = payload.get("agents") if isinstance(payload, dict) else None
    if not isinstance(listed, list):
        return (0, 0)

    urls: list[str] = []
    for entry in listed:
        url = entry.get("card_url") if isinstance(entry, dict) else None
        if not isinstance(url, str) or not url.strip():
            continue
        try:
            if origin_of(url) != origin:
                # Off-origin: refused, not followed.
                logger.warning(
                    "a2a.client: partner %s listed an off-origin card URL; ignored",
                    integration.id,
                )
                continue
        except Exception:
            logger.warning(
                "a2a.client: partner %s listed an unparseable card URL; ignored",
                integration.id,
                exc_info=True,
            )
            continue
        urls.append(url.strip())

    # An EMPTY list is meaningful — the partner has unbound every agent — but
    # an empty list from a partner that merely answered oddly would silently
    # remove every tool. Only a well-formed response with at least one agent
    # reconciles; a partner with genuinely none is left to the operator.
    if not urls:
        return (0, 0)
    # ``sync_targets`` deletes every row absent from the list it is given, so
    # the operator's own card URLs are merged in rather than replaced by what
    # the partner enumerates — one the partner does not list would otherwise
    # lose its verdict, pin and fingerprint on every page load.
    merged = list(
        dict.fromkeys(
            [*urls, *(u.strip() for u in configured_card_urls(integration) if u and u.strip())]
        )
    )
    return await sync_targets(db, integration.id, merged)
