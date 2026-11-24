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

"""Read-only text search across the configuration a caller can see.

``search_config_text`` answers "where does this word appear?" over the
skills, prompts, agents (persona, guardrails) and assignments (prompt,
activation prompt) visible to the caller, in one call, instead of one
``get_*`` per object. It reads through the same visibility filters as the
``list_*`` tools and writes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: The kinds of object searched, and the text fields of each.
SCOPES: dict[str, tuple[str, ...]] = {
    "skill": ("content", "description"),
    "prompt": ("content", "description"),
    "agent": ("persona", "guardrails"),
    "assignment": ("prompt", "activation_prompt"),
}
#: Objects listed per call, and hit lines shown per field.
MAX_OBJECTS = 50
MAX_LINES_PER_FIELD = 3
#: Characters kept of a hit line, centred on the match.
LINE_WINDOW = 160


@dataclass(frozen=True)
class ConfigHit:
    kind: str
    object_id: str
    name: str
    field: str
    count: int
    lines: list[tuple[int, str]]


def _window(line: str, start: int, length: int) -> str:
    if len(line) <= LINE_WINDOW:
        return line.strip()
    half = max(0, (LINE_WINDOW - length) // 2)
    left = max(0, start - half)
    right = min(len(line), left + LINE_WINDOW)
    left = max(0, right - LINE_WINDOW)
    return ("..." if left else "") + line[left:right].strip() + ("..." if right < len(line) else "")


def find_hits(text: str | None, query: str) -> tuple[int, list[tuple[int, str]]]:
    """The case-insensitive occurrences of *query* in *text*: count, and the first hit lines (1-based)."""
    if not text or not query:
        return 0, []
    needle = query.casefold()
    count = 0
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        folded = line.casefold()
        found = folded.count(needle)
        if not found:
            continue
        count += found
        if len(lines) < MAX_LINES_PER_FIELD:
            lines.append((number, _window(line, folded.index(needle), len(needle))))
    return count, lines


def hits_of(kind: str, rows: Iterable[Any], query: str) -> list[ConfigHit]:
    """One :class:`ConfigHit` per field of each row that holds *query*."""
    hits: list[ConfigHit] = []
    for row in rows:
        for field in SCOPES[kind]:
            count, lines = find_hits(getattr(row, field, None), query)
            if count:
                hits.append(ConfigHit(kind, str(row.id), str(getattr(row, "name", "") or ""), field, count, lines))
    return hits


def parse_scope(scope: object) -> tuple[list[str], str | None]:
    """The kinds named by *scope* (comma-separated, empty: all), or an error naming the valid ones."""
    raw = scope if isinstance(scope, str) else ",".join(scope) if isinstance(scope, list) else ""
    kinds = [part.strip().lower().rstrip("s") for part in raw.split(",") if part.strip()]
    unknown = [kind for kind in kinds if kind not in SCOPES]
    if unknown:
        return (
            [],
            f"Unknown scope {', '.join(unknown)}: use any of {', '.join(SCOPES)} (comma-separated), or leave it empty.",
        )
    return kinds or list(SCOPES), None


def render(query: str, hits: list[ConfigHit], kinds: list[str]) -> str:
    if not hits:
        return f"No {', '.join(kinds)} visible to you holds '{query}' (case-insensitive)."
    objects: dict[tuple[str, str], list[ConfigHit]] = {}
    for hit in hits:
        objects.setdefault((hit.kind, hit.object_id), []).append(hit)
    shown = list(objects.items())[:MAX_OBJECTS]
    out = [
        f"'{query}' found in {len(objects)} object(s) ({sum(h.count for h in hits)} occurrence(s))"
        + (
            f"; the first {MAX_OBJECTS} are listed, narrow scope or query for the rest"
            if len(objects) > MAX_OBJECTS
            else ""
        )
        + ":"
    ]
    for (kind, object_id), object_hits in shown:
        out.append(f"\n• {kind} **{object_hits[0].name}** (id: `{object_id}`)")
        for hit in object_hits:
            out.append(f"  {hit.field}: {hit.count} occurrence(s)")
            out.extend(f"    L{number}: {line}" for number, line in hit.lines)
    return "\n".join(out)


async def search_config_text(user_id: str | None, query: str, scope: object = "") -> str:
    """The skills, prompts, agents and assignments visible to *user_id* whose text holds *query*."""
    from sqlalchemy import func, or_, select

    from app.api.v1.helpers import assignment_visible_filter, caller_visible_filter, get_user_group_ids
    from app.core.database import async_session
    from app.models.agent import Agent
    from app.models.agent_skill import AgentSkill
    from app.models.assignment import Assignment
    from app.models.prompt import Prompt

    query = (query or "").strip() if isinstance(query, str) else ""
    if len(query) < 2:
        return "Rejected: give a query of at least 2 characters to search for."
    kinds, error = parse_scope(scope)
    if error:
        return error
    try:
        uid: uuid.UUID | None = uuid.UUID(str(user_id))
    except (ValueError, TypeError):
        uid = None
    models: dict[str, Any] = {"skill": AgentSkill, "prompt": Prompt, "agent": Agent, "assignment": Assignment}
    pattern = "%" + query.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    hits: list[ConfigHit] = []
    async with async_session() as db:
        group_ids = await get_user_group_ids(db, uid) if uid else []
        for kind in kinds:
            model = models[kind]
            visible = (
                assignment_visible_filter(uid, group_ids)
                if kind == "assignment"
                else caller_visible_filter(model, uid, group_ids)
            )
            matches = or_(*(func.lower(getattr(model, field)).like(pattern, escape="\\") for field in SCOPES[kind]))
            rows = (await db.execute(select(model).where(visible, matches).order_by(model.name))).scalars().all()
            hits.extend(hits_of(kind, rows, query))
    return render(query, hits, kinds)
