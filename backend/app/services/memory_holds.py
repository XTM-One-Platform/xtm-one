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

"""Memory entries a Memory Steward consolidation keeps for its undo (#8258).

An entry a consolidation flagged superseded stays restorable for
``RETENTION_DAYS``. The hygiene passes that delete superseded entries (the
per-scope cap, the age prune) leave a held entry to the consolidation purge,
or its undo would find nothing to restore.
"""

from __future__ import annotations

from sqlalchemy import String, cast, exists, func

from app.models.agent_memory_entry import AgentMemoryEntry
from app.models.memory_consolidation import MemoryConsolidation

#: How long a consolidated-away entry stays restorable: the medium-term
#: memory TTL (decision B19).
RETENTION_DAYS = 30

#: The operations that flag entries superseded, and so hold them.
HOLDING_OPERATIONS = ("merge", "remove", "demote")


def held_by_consolidation():
    """SQL: the ``AgentMemoryEntry`` row is held by a consolidation not undone."""
    return exists().where(
        MemoryConsolidation.undone_at.is_(None),
        MemoryConsolidation.operation.in_(HOLDING_OPERATIONS),
        MemoryConsolidation.before_entry_ids.op("@>")(
            func.jsonb_build_array(cast(AgentMemoryEntry.id, String))
        ),
    )
