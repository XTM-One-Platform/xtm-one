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

"""The seeded listener that resumes a fleet's work when a person answers its decision item (#1388).

A seeded fleet parks a decision for a person as a ``needs_human`` work item its
orchestrator owns, keyed by a prefix the fleet declares. The answer sets the
item from blocked back to open; this assignment hears exactly that transition on
exactly those prefixes, so the decision is applied without waiting for the next
scheduled run (which does not read it).
"""

from __future__ import annotations

from typing import Any

GATE_ANSWERED = "Gate answered"
#: The seed key naming the assignment of the same agent whose switch and run-as a
#: new row takes when the seed creates it (``seed.seed_builtin_assignments``): a
#: fleet already switched on gets the listener on upgrade.
INHERIT_SWITCH_FROM_KEY = "inherit_switch_from"
#: Answers of different decisions run side by side, up to this many at once: the
#: seeded event default (``assignment_dispatch.SEEDED_EVENT_CONCURRENCY``, one run)
#: made the answer of one case wait for the run applying another case's answer.
GATE_ANSWERED_CONCURRENCY = 5
#: The payload path the runs are ordered by: two answers on one decision key run
#: one at a time, in arrival order; answers on other keys run beside them.
GATE_ANSWERED_ORDERING_KEY = "correlation_key"

_CLAIM_FIRST = (
    "Claim it first with claim_work_item; when the claim is refused another run already "
    "applies this answer: stop there. Read it with get_work_item: payload.answer holds the "
    "option and label chosen (or a free text). "
)


def gate_answered_assignment(
    common: dict[str, Any],
    *,
    prefixes: tuple[str, ...],
    inherit_switch_from: str,
    description: str,
    apply: str,
    answered_only: bool = False,
) -> dict[str, Any]:
    """The "Gate answered" assignment of an orchestrator whose decision items carry *prefixes*.

    *apply* says what the run does with the answer, after the claim and the read.
    *answered_only* hears only the status change a person's answer made (the
    event's ``answered`` flag), so *prefixes* may be a fleet's whole namespace:
    a decision keyed outside its catalog resumes the work too, and a dependency
    release of another item under the same namespace wakes nothing.
    """
    match: dict[str, Any] = {
        "previous_status": "blocked",
        "status": "open",
        "correlation_key": {"starts_with": list(prefixes)},
    }
    if answered_only:
        match["answered"] = True
    return {
        **common,
        "name": GATE_ANSWERED,
        "description": description,
        "trigger_type": "platform_event",
        "trigger_config": {
            "event": "work_item.status_changed",
            "filter": match,
            "scope": "own",
            # Named here, so the seed keeps it over the one-run event default and
            # rewrites it on the row of an existing install at the next boot.
            "max_concurrent_runs": GATE_ANSWERED_CONCURRENCY,
            "ordering_key": GATE_ANSWERED_ORDERING_KEY,
        },
        INHERIT_SWITCH_FROM_KEY: inherit_switch_from,
        "prompt": (
            "A person answered a decision this fleet waits on: the work item in the trigger below "
            "went from blocked to open. " + _CLAIM_FIRST + apply + " Complete the decision item with "
            "the outcome. A rejection or a deferral runs nothing the decision covered."
        ),
    }
