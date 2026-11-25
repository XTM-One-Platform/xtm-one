"""Regression tests for the :data:`BUILTIN_INTENT_BINDINGS` shape.

The editing-style intents (``global.make_it_shorter``, ``global.make_it_longer``,
``global.fix_spelling``, ``global.change_tone``, ``global.summarize``, ``explain``) were
declared with twelve hand-written rows that differed only in
``agent_slug``/``vertical``.  Any future tweak to the description or
priority of an editing intent had to be replicated in two places,
with nothing in the code preventing drift between the CTI and AEV
variants.

The fix expresses the editing intents as a single declaration plus
a cross-product helper.  These tests pin the contract so a future
contributor accidentally breaking the (CTI, AEV) pairing wiring is
caught at CI time rather than as a silent missing entry in
production.
"""

from __future__ import annotations

import pytest

from app.agents.seed import (
    _EDITING_INTENT_AGENTS,
    _EDITING_INTENTS,
    BUILTIN_INTENT_BINDINGS,
    _expand_editing_intent_bindings,
)


class TestEditingIntentExpansion:
    def test_each_editing_intent_emits_one_row_per_vertical(self):
        """The expansion helper cross-products
        ``_EDITING_INTENTS × _EDITING_INTENT_AGENTS``.  A regression that
        accidentally drops one side would cut the CTI-or-AEV chat
        surface from one of the editing actions.
        """
        rows = _expand_editing_intent_bindings()
        assert len(rows) == len(_EDITING_INTENTS) * len(_EDITING_INTENT_AGENTS)

    def test_editing_intents_pair_descriptions_stay_in_lockstep(self):
        """For every editing intent, the description of the CTI row and
        the AEV row must be identical — that's the maintainability
        property the refactor was designed to enforce.  Drift in either
        direction (CTI tweaked but AEV forgotten, or vice versa) is a
        regression.
        """
        rows = _expand_editing_intent_bindings()
        by_intent: dict[str, set[str]] = {}
        for row in rows:
            by_intent.setdefault(row["intent_name"], set()).add(
                row["intent_description"]
            )
        for intent_name, descriptions in by_intent.items():
            assert len(descriptions) == 1, (
                f"editing intent {intent_name!r} has divergent "
                f"descriptions across verticals: {sorted(descriptions)}"
            )

    @pytest.mark.parametrize(
        "intent_name",
        [name for name, _ in _EDITING_INTENTS],
    )
    def test_editing_intent_present_for_all_verticals(self, intent_name):
        """Every editing intent must be wired against every vertical
        assistant declared in ``_EDITING_INTENT_AGENTS`` exactly once
        (CTI, AEV, FIMI today).
        """
        rows = [
            row
            for row in BUILTIN_INTENT_BINDINGS
            if row["intent_name"] == intent_name
        ]
        slugs_verticals = {
            (row["agent_slug"], row["vertical"]) for row in rows
        }
        expected = set(_EDITING_INTENT_AGENTS)
        assert slugs_verticals == expected, (
            f"editing intent {intent_name!r} must be wired against "
            f"every vertical assistant {sorted(expected)}; observed "
            f"{sorted(slugs_verticals)}"
        )

    def test_no_duplicate_intent_bindings(self):
        """The full builtin list must have no duplicate
        ``(intent_name, agent_slug, vertical)`` tuples — accidental
        duplication would inflate the seed transaction and confuse the
        stale-binding cleanup.
        """
        fingerprints = [
            (row["intent_name"], row["agent_slug"], row["vertical"])
            for row in BUILTIN_INTENT_BINDINGS
        ]
        assert len(fingerprints) == len(set(fingerprints)), (
            f"duplicates in BUILTIN_INTENT_BINDINGS: "
            f"{[fp for fp in fingerprints if fingerprints.count(fp) > 1]}"
        )
