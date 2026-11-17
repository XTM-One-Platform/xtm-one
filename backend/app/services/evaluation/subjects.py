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

"""An evaluation's subject, agent or flow (#8257), as the read paths name it.

A link (``EvaluationAgent``) and a run (``EvaluationRun``) carry exactly one of
``agent_id`` and ``flow_id``. These helpers are what every reader keys and
labels them by, so a flow subject is never read as an agent with no id.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.agent import Agent
from app.models.evaluation import EvaluationAgent
from app.models.flow import Flow
from app.models.user import User


def link_of(row: Any) -> ColumnElement[bool]:
    """The clause matching the ``EvaluationAgent`` link of *row*'s subject."""
    if row.flow_id is not None:
        return EvaluationAgent.flow_id == row.flow_id
    return EvaluationAgent.agent_id == row.agent_id


def subject_id(row: Any) -> uuid.UUID | None:
    return row.flow_id or row.agent_id


@dataclass(frozen=True)
class SubjectLabel:
    name: str
    slug: str
    owner_name: str | None


async def subject_label(db: AsyncSession, row: Any) -> SubjectLabel | None:
    """The display name of *row*'s subject, its slug (agents only) and its owner's name."""
    if row.flow_id is not None:
        found = (
            await db.execute(
                select(Flow.name, User.full_name).outerjoin(User, User.id == Flow.user_id).where(Flow.id == row.flow_id)
            )
        ).one_or_none()
        return SubjectLabel(found[0] or "", "", found[1]) if found else None
    found = (
        await db.execute(
            select(Agent.name, Agent.slug, User.full_name)
            .outerjoin(User, User.id == Agent.user_id)
            .where(Agent.id == row.agent_id)
        )
    ).one_or_none()
    return SubjectLabel(found[0] or "", found[1] or "", found[2]) if found else None
