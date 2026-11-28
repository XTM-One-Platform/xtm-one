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

"""Seeded CTEM personas quote only times they were given (issue #2764).

The Loop Orchestrator and the Scenario Creator wrote clock times later than
the run's end into case evidence and delegated results. Every ``ctem-*``
persona and the Scenario Creator carry the rule that a written time is one a
tool result, the wait answer or the prompt's clock gave.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.seed import _resolve_seed_content

_PERSONAS = Path(__file__).resolve().parents[1] / "app" / "agents" / "seed_data" / "agent_persona"
_SLUGS = [*sorted(p.stem for p in _PERSONAS.glob("ctem-*.md")), "openaev-scenario-creator"]
_RULE = (
    "a time in a note, an evidence line or a result is the server time a tool result returned, "
    'the time the wait answer gave, or the prompt\'s clock written "as of" it; never an estimate of now.'
)


def test_the_guard_reads_the_ctem_loop_personas() -> None:
    assert "ctem-threat-response-orchestrator" in _SLUGS
    assert len(_SLUGS) >= 10


@pytest.mark.parametrize("slug", _SLUGS)
def test_the_persona_carries_the_time_quoting_rule_once(slug: str) -> None:
    text = _resolve_seed_content("agent_persona", slug) or ""
    assert text.count(_RULE) == 1, f"{slug}: the time-quoting rule must appear exactly once"
