"""The lab administrator's memory: Profile > Memory renders Purge All and its Clear buttons on real rows.

Started by ``lab.py`` (``seed_profile_memory``) with the backend's interpreter, from ``backend/``
and with the backend's environment, once the backend answers. No API writes a user's memory (the
agentic loop does, after a conversation), so this writes it through the backend's own service code:

* one cross-agent profile statement (``user_memory.update_entries`` with a global ``new_entry``):
  the profile's ``medium_term`` is not empty, so the cross-agent card and its Clear render;
* one per-agent short-term memory of the given agent, scoped to the user
  (``agent_memory.store_short_term_memory``): the agent is listed with its own Clear.

``GET /settings/me/memory`` then answers both, which ``lab.py`` reads back. Every text is lab-only
and names the lab; nothing is sent to a model. Arguments: the user, the agent and the JSON file
the outcome is written to. Idempotent: a profile statement already stored is not added twice.

dev-docs/reference/agentic-labs.md, "The xtm-one lab".
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

#: The cross-agent profile statement: lab-only, read by no real agent.
LAB_PROFILE_STATEMENT = "Lab sample: prefers short answers with the sources linked."
#: The per-agent memory of the lab agent with the administrator.
LAB_AGENT_MEMORY = "Lab sample: reviewed the weekly threat digest with the administrator."


async def seed(user_id: str, agent_id: str) -> dict[str, object]:
    """Write the profile statement and the agent's memory; answer what the reads see."""
    from app.core.redis import redis_client
    from app.services.agent_memory import load_memory, store_short_term_memory
    from app.services.user_memory import load_user_memory, update_entries
    from app.services.user_memory_entries import new_entry

    redis = await redis_client()
    statement = new_entry(LAB_PROFILE_STATEMENT, now=datetime.now(UTC))

    def add_statement(stored):
        entries = list(stored or [])
        if any(entry.is_global and entry.text == statement.text for entry in entries):
            return None
        return [*entries, statement]

    await update_entries(redis, user_id, add_statement)
    if not (await load_memory(agent_id, scope_id=user_id))["short_term"]:
        await store_short_term_memory(
            agent_id, source="Lab sample chat", summary=LAB_AGENT_MEMORY, source_type="chat", scope_id=user_id,
        )
    profile = await load_user_memory(user_id)
    agent = await load_memory(agent_id, scope_id=user_id)
    return {
        "user": user_id,
        "agent": agent_id,
        "profile_medium_term": bool(profile.get("medium_term")),
        "agent_short_term": len(agent["short_term"]),
    }


def main() -> None:
    user_id, agent_id, out = sys.argv[1:4]
    Path(out).write_text(json.dumps(asyncio.run(seed(user_id, agent_id))))


if __name__ == "__main__":
    main()
