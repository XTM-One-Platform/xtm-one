"""Generic AI is never gated on the platform mode (dev-docs/mode-split.md, S5).

A generic AI capability (evaluations, AI adoption, ROI, fleets, persona
improvements) runs the same code path in XTM One mode and in Copilot mode; only
domain content (catalogs, seeded agents, wording) and licence semantics differ
per mode. These tests fail when a feature flag condition or the evaluations
seed starts to tell the two products apart again.
"""

from __future__ import annotations

import pytest

from app.core.config import AI_MODES, settings
from app.core.feature_flags import FLAG_CONDITIONS

#: Flags whose condition may differ between the two products, each with its reason.
MODE_GATED_FLAGS = {
    "AGENTIC_CODING": "internal tooling: Filigran's agentic coding labs, never an XTM One feature",
}


def _answers(flag: str, mode: str, monkeypatch: pytest.MonkeyPatch) -> bool:
    monkeypatch.setattr(settings, "platform_mode", mode)
    return FLAG_CONDITIONS[flag]()


@pytest.mark.parametrize("flag", sorted(FLAG_CONDITIONS.keys() - MODE_GATED_FLAGS.keys()))
def test_a_generic_flag_answers_the_same_in_both_products(flag: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _answers(flag, "xtm_one", monkeypatch) == _answers(flag, "copilot", monkeypatch), (
        f"{flag} is gated on the platform mode: a generic AI capability runs in both modes "
        "(dev-docs/mode-split.md); add it to MODE_GATED_FLAGS with its reason only if it is internal tooling"
    )


def test_the_mode_gated_allowlist_only_names_flags_that_still_differ(monkeypatch: pytest.MonkeyPatch) -> None:
    stale = [
        flag
        for flag in MODE_GATED_FLAGS
        if flag not in FLAG_CONDITIONS
        or _answers(flag, "xtm_one", monkeypatch) == _answers(flag, "copilot", monkeypatch)
    ]
    assert stale == [], "drop a flag that no longer differs between the modes from MODE_GATED_FLAGS"


def test_the_evaluations_seed_covers_every_mode_where_ai_runs() -> None:
    from app.evaluations.seed import _SEED_MODES

    assert set(_SEED_MODES) == set(AI_MODES)
