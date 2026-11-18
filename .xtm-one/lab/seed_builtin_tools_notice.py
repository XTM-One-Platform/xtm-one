"""The lab's built-in tools release notice: one bell entry of kind ``builtin_tools_outside_allowlist`` (#12192).

Started by ``lab.py`` (``seed_builtin_tools_notice``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers. The platform writes this notice only at
the first boot of a build whose built-in catalog gained a function in a group an agent's allowlist
scopes (``services/builtin_tool_release_notice.py``, ``on_release_deployed``): a lab boots one build,
so it never sees one. This writes the same notice through the platform's own functions.

``seed OWNER AGENT OUT``: the agent (owned by OWNER) is limited to the first function of one built-in
group of :data:`GROUPS` (the first the catalog ships with at least two functions); the rest of that
group is told as the release's new functions the allowlist leaves out (``agents_left_out``,
``by_owner``, ``notice``), delivered with ``notify_web_user``. Answers the group, the functions left
out, the notice's title and the path it opens in ``OUT``. The bell entry opens the agent's Tooling
section, where each one is added or dismissed (#12245).

dev-docs/reference/agentic-labs.md, "The xtm-one lab" (dev:harness-gaps lab-seed-builtin-tools-notice).
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from pathlib import Path

#: Built-in groups the seed scopes, in order of preference.
GROUPS = ("web", "files", "coordination", "platform")

#: The release the notice names.
RELEASE = "lab release"


def scope(catalog: dict[str, list[str]], groups: tuple[str, ...] = GROUPS) -> tuple[str, list[str], list[str]]:
    """``(group, kept, left_out)``: the first group of *groups* with two functions or more. Pure."""
    for group in groups:
        names = list(catalog.get(group) or ())
        if len(names) >= 2:
            return group, names[:1], names[1:]
    raise RuntimeError(f"no built-in group of {', '.join(groups)} ships two functions")


async def seed(owner: str, agent_id: str) -> dict[str, object]:
    """Scope the agent, write the notice; answer the group, the functions left out and the notification id."""
    import app.models  # noqa: F401 - every table, so the foreign keys resolve
    from sqlalchemy import select, update

    from app.core.database import async_session, engine
    from app.models.agent import Agent
    from app.services.builtin_tool_release_notice import (
        SOURCE_LABEL,
        agents_left_out,
        by_owner,
        notice,
    )
    from app.services.notify import notify_web_user
    from app.tools.builtin_suites import builtin_suite_catalog

    group, kept, left_out = scope({g: list(names) for g, names in builtin_suite_catalog().items()})
    agent_uuid = uuid.UUID(agent_id)
    async with async_session() as db:
        await db.execute(
            update(Agent)
            .where(Agent.id == agent_uuid)
            .values(builtin_tool_ids=[group], builtin_allowed_tools={group: kept})
        )
        await db.flush()
        row = (
            await db.execute(
                select(Agent.id, Agent.name, Agent.user_id, Agent.builtin_tool_ids, Agent.builtin_allowed_tools, Agent.config)
                .where(Agent.id == agent_uuid)
            )
        ).one()
        owners = by_owner(agents_left_out({group: left_out}, [row]))
        agents = owners.get(owner)
        if not agents:
            raise RuntimeError(f"agent {agent_id} of {owner}: nothing left out of {group}")
        title, summary, extra = notice(RELEASE, agents)
        await notify_web_user(db, owner, title=title, summary=summary, source_label=SOURCE_LABEL, extra_data=extra)
        await db.commit()
    await engine.dispose()
    return {"group": group, "left_out": left_out, "title": title, "path": extra["path"]}


def main() -> None:
    mode, *args = sys.argv[1:]
    if mode != "seed" or len(args) != 3:
        raise SystemExit("usage: seed OWNER AGENT OUT")
    owner, agent_id, out = args
    Path(out).write_text(json.dumps(asyncio.run(seed(owner, agent_id))))


if __name__ == "__main__":
    main()
