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

"""A new fleet's first documentation, generated once at creation (#10316).

A fleet created by New fleet, Create with a prompt, Duplicate or a fleet spec
lands on its About page (the default view, #10147). So that page is never a
bare "no documentation yet", the creating request schedules ONE generation
with the creator's own model, after its transaction committed:

* asked once, at creation, never on page view; a fleet that already carries a
  documentation (a fork of a documented fleet, a re-applied spec) is left as is;
* only when agentic AI may run (AI on, licensed): the gate is read before the
  work is scheduled, as every background entry point does (``agentic_gate``);
* on a session of its own, never the request's, so the request answers at once;
* while it runs, a short-lived Redis marker lets ``GET /fleets/{id}/documentation``
  answer ``generating`` and the About page poll; the marker expires on its own,
  so a worker stopped mid-generation never leaves the page waiting.

It is the same write path as the About page's "Regenerate documentation"
button (:func:`fleet_doc_generation.regenerate_fleet_documentation`), with the
creator as its author.

The marker is the running state of EVERY generation of a fleet's documentation
(#10817), the first one and a regenerate from the page or the fleet tool alike:

* :func:`claim_generation` takes it atomically (``SET NX``) before the model
  call, so a second click or a second tab starts no parallel generation;
* :func:`generation_running` refreshes it while the generation runs, so one
  longer than the marker's lifetime keeps the page spinning to the end;
* :func:`release_generation` clears it once the version is saved or the
  generation failed, a failure leaving a short-lived record the page reads
  (the person who clicked may have left the page and come back);
* :func:`generation_state` is what ``GET /fleets/{id}/documentation`` answers,
  so the About page derives its spinner from the server on every mount.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

#: How long the "generating" marker lives without a refresh: a worker stopped mid-generation frees it by then.
GENERATING_TTL_SECONDS = 300
#: How often a running generation refreshes its marker, well inside its lifetime.
GENERATING_REFRESH_SECONDS = 60
#: How long a failed generation stays readable on the page.
FAILURE_TTL_SECONDS = 600
#: The failure a generation that raised something unexpected records.
GENERATION_FAILED = "The documentation could not be generated."
_MARKER_PREFIX = "fleet_doc:generating:"
_FAILURE_PREFIX = "fleet_doc:failed:"
#: Strong references, so a scheduled generation is never garbage-collected mid-flight.
_tasks: set[asyncio.Task] = set()


def _marker(fleet_id: uuid.UUID | str) -> str:
    return f"{_MARKER_PREFIX}{fleet_id}"


def _failure(fleet_id: uuid.UUID | str) -> str:
    return f"{_FAILURE_PREFIX}{fleet_id}"


@dataclass(frozen=True)
class DocGeneration:
    """The running state of a fleet's documentation generation, as the About page reads it."""

    generating: bool = False
    #: ISO 8601 UTC time the running generation started; None when unknown.
    started_at: str | None = None
    #: Why the last generation failed, while its record lives and nothing runs.
    failure: str | None = None


async def generation_state(fleet_id: uuid.UUID | str) -> DocGeneration:
    """Whether a generation of *fleet_id*'s documentation runs, or why the last one failed; idle when Redis is unreachable."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        started = await client.get(_marker(fleet_id))
        if started is not None:
            # A marker written before #10817 (or by the lab) holds "1": running, start unknown.
            return DocGeneration(generating=True, started_at=started if "T" in str(started) else None)
        failure = await client.get(_failure(fleet_id))
        return DocGeneration(failure=str(failure) if failure else None)
    except Exception:
        logger.debug("fleet doc marker unreadable for %s", fleet_id, exc_info=True)
        return DocGeneration()


async def claim_generation(fleet_id: uuid.UUID | str) -> bool:
    """Take the marker of *fleet_id* for a generation; False when another one runs.

    True when Redis is unreachable: the marker shows a running generation, it
    never stops one.
    """
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        started = datetime.now(UTC).isoformat(timespec="seconds")
        if not await client.set(_marker(fleet_id), started, nx=True, ex=GENERATING_TTL_SECONDS):
            return False
        await client.delete(_failure(fleet_id))
        return True
    except Exception:
        logger.debug("fleet doc marker not written for %s", fleet_id, exc_info=True)
        return True


async def release_generation(fleet_id: uuid.UUID | str, failure: str | None = None) -> None:
    """Clear the marker of *fleet_id*; a *failure* is kept readable for a few minutes."""
    try:
        from app.core.redis import redis_client

        client = await redis_client()
        if failure:
            await client.set(_failure(fleet_id), failure[:500], ex=FAILURE_TTL_SECONDS)
        await client.delete(_marker(fleet_id))
    except Exception:
        logger.debug("fleet doc marker not cleared for %s", fleet_id, exc_info=True)


async def _refresh(fleet_id: uuid.UUID | str) -> None:
    """Push the marker's expiry back every refresh period until cancelled."""
    while True:
        await asyncio.sleep(GENERATING_REFRESH_SECONDS)
        try:
            from app.core.redis import redis_client

            client = await redis_client()
            await client.expire(_marker(fleet_id), GENERATING_TTL_SECONDS)
        except Exception:
            logger.debug("fleet doc marker not refreshed for %s", fleet_id, exc_info=True)


@contextlib.asynccontextmanager
async def generation_running(fleet_id: uuid.UUID | str) -> AsyncIterator[None]:
    """Keep the claimed marker of *fleet_id* alive for the duration of the block."""
    refresher = asyncio.create_task(_refresh(fleet_id), name=f"fleet-doc-marker-{fleet_id}")
    try:
        yield
    finally:
        refresher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresher


async def schedule_documentation_on_create(db: AsyncSession, fleet_id: uuid.UUID | None, user_id: uuid.UUID) -> bool:
    """Schedule the first documentation of the fleet *fleet_id* just created by *user_id*; answers whether it was.

    Call it AFTER the creating transaction committed (the background session
    must see the fleet). Nothing is scheduled when agentic AI may not run or the
    fleet already has a documentation. Never raises: a fleet is created whatever
    happens to its documentation.
    """
    if fleet_id is None:
        return False
    try:
        from app.models.fleet import Fleet
        from app.services.agentic_gate import agentic_refusal

        if await agentic_refusal(db) is not None:
            return False
        fleet = await db.get(Fleet, fleet_id)
        if fleet is None or _has_documentation(fleet):
            return False
    except Exception:
        logger.warning("fleet documentation at creation not scheduled for %s", fleet_id, exc_info=True)
        return False
    if not await claim_generation(fleet_id):
        return False
    task = asyncio.create_task(_generate(fleet_id, user_id), name=f"fleet-doc-on-create-{fleet_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


#: Waits before each background attempt at a pending section (#11292): a provider that failed a section's
#: own retries is given time to recover; the last failure is logged and the section keeps its text.
PENDING_RETRY_DELAYS: tuple[float, ...] = (30.0, 120.0, 300.0)


def schedule_pending_sections(fleet_id: uuid.UUID, user_id: uuid.UUID, pending: list[str] | None) -> bool:
    """Fill the *pending* sections of a version just saved, in the background (#11292); answers whether scheduled.

    Call it AFTER the saving transaction committed and the generation marker was
    released. Each section is regenerated on its own (the one-section path, which
    keeps every other section and the structured parts) on a session of its own,
    under the same marker as any generation, so the page spins while it runs and
    never overlaps another one. Never raises.
    """
    sections = [s for s in dict.fromkeys(pending or []) if s]
    if not sections:
        return False
    task = asyncio.create_task(_fill_pending(fleet_id, user_id, sections), name=f"fleet-doc-pending-{fleet_id}")
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return True


async def _fill_pending(fleet_id: uuid.UUID, user_id: uuid.UUID, sections: list[str]) -> None:
    """Regenerate each pending section until it lands or :data:`PENDING_RETRY_DELAYS` is spent."""
    from app.core.database import async_session
    from app.models.fleet import Fleet
    from app.services.fleet_doc_generation import regenerate_fleet_documentation

    left = list(sections)
    for delay in PENDING_RETRY_DELAYS:
        if not left:
            return
        await asyncio.sleep(delay)
        if not await claim_generation(fleet_id):
            continue  # another generation runs: try again at the next delay
        try:
            async with generation_running(fleet_id):
                for section in list(left):
                    try:
                        async with async_session() as db:
                            fleet = await db.get(Fleet, fleet_id)
                            if fleet is None:
                                return
                            draft = await regenerate_fleet_documentation(db, user_id, fleet, section=section)
                            if section in draft.pending or section not in draft.sections:
                                await db.rollback()
                                continue
                            await db.commit()
                        left.remove(section)
                    except Exception:
                        logger.info("pending section %s of fleet %s not filled yet", section, fleet_id, exc_info=True)
        finally:
            # A pending section is never a failure shown to the page: the version is saved.
            await release_generation(fleet_id)
    if left:
        logger.warning(
            "fleet documentation %s: section(s) %s still pending after the background retries", fleet_id, left
        )


def _has_documentation(fleet: object) -> bool:
    doc = getattr(fleet, "documentation", None)
    return isinstance(doc, dict) and bool(doc.get("sections"))


async def _generate(fleet_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Generate and save the first version on a session of its own; a failure is logged, the fleet stays undocumented."""
    from app.core.database import async_session
    from app.models.fleet import Fleet
    from app.services.fleet_doc_generation import regenerate_fleet_documentation

    failure: str | None = None
    pending: list[str] = []
    try:
        async with generation_running(fleet_id), async_session() as db:
            fleet = await db.get(Fleet, fleet_id)
            if fleet is None or _has_documentation(fleet):
                return
            try:
                draft = await regenerate_fleet_documentation(db, user_id, fleet, base_version=0)
                await db.commit()
                pending = list(draft.pending)
            except Exception:
                await db.rollback()
                raise
    except Exception:
        # The About page shows the failure and its Generate panel: a person can ask again.
        failure = GENERATION_FAILED
        logger.warning("fleet documentation at creation failed for %s", fleet_id, exc_info=True)
    finally:
        await release_generation(fleet_id, failure)
    schedule_pending_sections(fleet_id, user_id, pending)
