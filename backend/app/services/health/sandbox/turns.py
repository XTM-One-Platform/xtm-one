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

"""Which turns are intercepted, and the sessions they get (#8255).

:func:`intercepted_agent_turn` wraps ``run_agent_chat``: a turn of a sandbox
copy, whoever started it (chat, assignment, evaluation, a consult), is bound to
a ``sandbox`` session replaying the copy's source recordings; a Concierge turn
while dry-run is on to a ``dry_run`` session. A turn inside an already bound
session (a nested consult, a flow's agent node, an evaluation case) counts on
that session. Nothing to intercept costs one attribute read.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.health.sandbox.replay import RecordedCall, ReplayIndex, load_recorded_calls
from app.services.health.sandbox.session import (
    SandboxReport,
    SandboxSession,
    current_sandbox_session,
    model_simulator,
    sandbox_session,
)

logger = logging.getLogger(__name__)


def recording_consent() -> Callable[[RecordedCall], Awaitable[bool]]:
    """The consent check of one session: level 1's rule read at each use, in a
    session of its own (calls of a parallel batch share the turn's), the trace
    read audited once per trace and session. Fails closed."""
    audited: set[str] = set()

    async def _check(recording: RecordedCall) -> bool:
        from app.core.database import async_session
        from app.services.health.consent import (
            ConsentDeniedError,
            content_readable,
            require_content_read,
        )

        agent_id = uuid.UUID(recording.agent_id) if recording.agent_id else None
        flow_id = uuid.UUID(recording.flow_id) if recording.flow_id else None
        user_id = uuid.UUID(recording.user_id) if recording.user_id else None
        target = {"flow_id": flow_id} if flow_id else {"agent_id": agent_id}
        try:
            async with async_session() as db:
                if recording.trace_id and recording.trace_id not in audited:
                    await require_content_read(
                        db,
                        kind="flow_run" if flow_id else "trace",
                        ref_id=recording.trace_id,
                        user_id=user_id,
                        **target,
                    )
                    await db.commit()
                    audited.add(recording.trace_id)
                    return True
                return await content_readable(db, user_id=user_id, **target)
        except ConsentDeniedError:
            return False
        except Exception:
            logger.exception("Sandbox: the consent of a recording could not be read; not used")
            return False

    return _check


def build_sandbox_session(
    *,
    user_id: str,
    model: str | None,
    recorded_calls: Sequence[RecordedCall] = (),
    case_id: str | None = None,
) -> SandboxSession:
    """A ``sandbox`` session replaying *recorded_calls*, simulating with *model*.

    The simulator opens a database session of its own per call, for the same
    reason as the consent check.
    """
    from app.core.database import async_session

    async def _simulate(
        tool: str, arguments: dict, recorded: list[RecordedCall], description: str
    ) -> tuple[str | None, int]:
        async with async_session() as db:
            return await model_simulator(db, user_id=user_id, model=model)(tool, arguments, recorded, description)

    return SandboxSession(
        mode="sandbox",
        replay=ReplayIndex(list(recorded_calls)),
        simulate=_simulate,
        consent=recording_consent(),
        report=SandboxReport(case_id=case_id),
    )


async def copy_recordings(db: AsyncSession, copy: Any) -> list[RecordedCall]:
    """The recordings of a copy's source traces (none for a flow copy)."""
    if not copy.copy_agent_id or not copy.source_trace_ids:
        return []
    return await load_recorded_calls(
        db, copy.source_trace_ids, excluded_agent_ids=[copy.copy_agent_id], only_agent_id=copy.source_id
    )


async def agent_turn_session(db: AsyncSession, user_id: str, agent: Any) -> SandboxSession | None:
    """The session a new turn of *agent* must be bound to, or ``None``."""
    if current_sandbox_session() is not None:
        return None
    if getattr(agent, "is_sandbox", False):
        from app.services.health.sandbox.copies import sandbox_copy_of

        copy = await sandbox_copy_of(db, agent_id=agent.id)
        recordings = await copy_recordings(db, copy) if copy is not None else []
        return build_sandbox_session(user_id=user_id, model=getattr(agent, "llm_model", None), recorded_calls=recordings)
    from app.services.health.identity import in_concierge_run

    if in_concierge_run():
        from app.services.health.sandbox.dry_run import dry_run_active

        if await dry_run_active(db):
            return SandboxSession(mode="dry_run")
    return None


def turn_binding(session: SandboxSession | None) -> contextlib.AbstractContextManager[Any]:
    """Bind *session* for a turn run outside ``run_agent_chat`` (the
    assignment engine's own loop); nothing when it is ``None``."""
    return sandbox_session(session) if session is not None else contextlib.nullcontext()


def intercepted_agent_turn[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Bind the turn's session (:func:`agent_turn_session`) around *fn*,
    ``run_agent_chat(db, user_id, agent, ...)``."""

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        bound = signature.bind_partial(*args, **kwargs).arguments
        session = await agent_turn_session(bound["db"], bound["user_id"], bound["agent"])
        if session is None:
            return await fn(*args, **kwargs)
        with sandbox_session(session):
            return await fn(*args, **kwargs)

    return _wrapper  # type: ignore[return-value]
