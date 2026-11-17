"""readable_proposals loads a target once per proposal type, not once per target (#10401).

An assignment carries two proposal types (``assignment_prompt`` and ``agent_to_flow``) whose
handlers load it as different objects; a cache keyed by the target alone handed the first
handler's object to the second, whose ``describe`` raised and made GET /concierge/improvements 500.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.services import improvement_proposals as proposals
from app.services.concierge.cycle_access import readable_proposals


class _Handler:
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.loads = 0

    async def load_target(self, db, target_id, *, lock):
        self.loads += 1
        return SimpleNamespace(kind=self.kind, id=target_id)


def test_each_proposal_type_loads_its_own_target(monkeypatch):
    handlers = {"assignment_prompt": _Handler("prompt"), "agent_to_flow": _Handler("flow")}
    monkeypatch.setattr(proposals, "handler_for", lambda proposal_type: handlers.get(proposal_type))
    monkeypatch.setattr(proposals, "surface_enabled", lambda proposal: True)
    monkeypatch.setattr(proposals, "can_read", lambda handler, target, user, groups: target.kind == handler.kind)
    target_id = uuid.uuid4()
    rows = [
        SimpleNamespace(proposal_type=kind, target_type="assignment", target_id=target_id)
        for kind in ("assignment_prompt", "agent_to_flow", "assignment_prompt")
    ]

    readable = asyncio.run(readable_proposals(None, SimpleNamespace(), [], rows))

    assert [target.kind for _, target, _ in readable] == ["prompt", "flow", "prompt"]
    assert handlers["assignment_prompt"].loads == 1 and handlers["agent_to_flow"].loads == 1
