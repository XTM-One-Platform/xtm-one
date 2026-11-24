"""Every seeded persona that keeps state in shared state states its bound (#10789).

An unbounded list in a fleet's shared state grows with every case until the
size guard refuses the write. Each persona that tells its agent to record a
list in a shared-state key must say how the list stays bounded: closed items
leave the open list (counters carry the totals) and history lists are appended
with ``keep_last``.
"""

from __future__ import annotations

import re
from pathlib import Path

PERSONA_DIR = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"

# Personas that WRITE a fleet's shared state (readers such as reporters only read it).
_WRITERS = {
    "ctem-ai-security-orchestrator.md",
    "ctem-third-party-risk-orchestrator.md",
    "ctem-threat-response-orchestrator.md",
    "ctem-vuln-prioritization-orchestrator.md",
    "openaev-detection-desk-orchestrator.md",
    "opencrq-risk-desk-orchestrator.md",
    "opencti-pir-desk-orchestrator.md",
}

_RECORDS = re.compile(r"(record|keep)[^.\n]*shared state `[a-z0-9:_-]+`", re.IGNORECASE)
# A list appended with keep_last, or no list at all: the CTEM loop fleets keep
# counters only since their open work moved to the cases (#891).
_BOUND = re.compile(r"keep_last \d+|keeps counters only")


def test_every_persona_that_records_shared_state_is_listed() -> None:
    recorders = {p.name for p in PERSONA_DIR.glob("*.md") if _RECORDS.search(p.read_text(encoding="utf-8"))}
    assert recorders <= _WRITERS, f"new shared-state writer without a bound check: {sorted(recorders - _WRITERS)}"


def test_every_shared_state_writer_states_its_list_bound() -> None:
    missing = [name for name in sorted(_WRITERS) if not _BOUND.search((PERSONA_DIR / name).read_text(encoding="utf-8"))]
    assert not missing, f"personas keeping shared-state lists without a keep_last bound: {missing}"
