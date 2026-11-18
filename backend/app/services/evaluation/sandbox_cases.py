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

"""An evaluation entry run on a Platform Health sandbox copy (#8257, level 2's interception).

A sandbox agent copy runs each entry inside ``sandbox_case``: its writes are
intercepted, its calls replayed from the entry's own recordings when the entry
was built from production (``concierge.datasets.entry_recordings``), else from
the copy's source traces. The case's report - replayed calls, simulated and
intercepted writes - is stored with the result under :data:`SANDBOX_KEY`, so a
verdict states how many writes a model simulated (decision B17).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

#: Where a result keeps its sandbox case's report, in ``execution_details``.
SANDBOX_KEY = "sandbox"


class SandboxCaseUnavailableError(Exception):
    """A sandbox subject whose copy record is gone: the entry is left out."""


def case_ref(run_id: uuid.UUID, entry_id: uuid.UUID | None) -> str:
    return f"eval:{run_id}:{entry_id}"


@asynccontextmanager
async def agent_sandbox_case(
    db: AsyncSession,
    agent: Any,
    *,
    run_id: uuid.UUID,
    entry_id: uuid.UUID | None,
    user_id: str | None,
    model: str | None,
) -> AsyncIterator[Any | None]:
    """The sandbox session of one entry's turn, or ``None`` for an agent that is not a copy."""
    if not getattr(agent, "is_sandbox", False):
        yield None
        return
    from app.services.health.datasets import entry_recordings
    from app.services.health.sandbox.copies import sandbox_copy_of
    from app.services.health.sandbox.runs import sandbox_case

    copy = await sandbox_copy_of(db, agent_id=agent.id)
    if copy is None:
        raise SandboxCaseUnavailableError("The sandbox copy is not available.")
    recordings = (await entry_recordings(db, [entry_id])).get(entry_id) if entry_id is not None else None
    async with sandbox_case(
        db, copy, case_ref=case_ref(run_id, entry_id), recorded_calls=recordings, user_id=user_id, model=model
    ) as session:
        yield session
