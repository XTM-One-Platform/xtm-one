"""`ai_disabled` is an XTM One platform on the client too.

The backend learned this the hard way: ``plan.md`` finding 6 records that nine
call sites read ``not is_xtm_one`` as "Copilot", so a fourth ``PLATFORM_MODE``
value mounted the OpenAI proxy, turned on self-registration and made the licence
gate mandatory in the one mode documented as needing none. It was fixed with an
explicit ``is_copilot_like`` and a guard test.

The frontend twin was never audited, and had the same bug: ten raw
``platform_mode === "xtm_one"`` comparisons meant an AI-disabled deployment
rendered as **Copilot** — Copilot logo, favicon and theme, the Copilot dashboard
on the command-centre route, CTEM routes redirecting home although the spec keeps
CTEM live, and the Copilot-only "AI Adoption" and "Evaluations" nav entries
appearing. Found by Fred in the running app, twice, not by any test.

So: one predicate, ``isXtmOneMode``, and nothing compares the mode by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"
SRC = FRONTEND / "src"

#: The one module allowed to compare the mode to a literal: it defines the
#: predicate, and names the two products apart for display.
_PREDICATE_MODULE = "hooks/usePlatformConfig.tsx"

_RAW_COMPARISON = re.compile(r'platform_?[Mm]ode\s*(?:===|!==)\s*"(xtm_one|ai_disabled)"')


@pytest.mark.skipif(not SRC.is_dir(), reason="frontend not checked out")
def test_nothing_compares_the_platform_mode_by_hand():
    """A raw comparison reads the enum as a closed set and drops ``ai_disabled``."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.ts*")):
        rel = str(path.relative_to(SRC))
        if rel == _PREDICATE_MODULE:
            continue
        lines = [
            i
            for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            if _RAW_COMPARISON.search(line)
        ]
        if lines:
            offenders[rel] = lines
    assert not offenders, (
        "compare via isXtmOneMode() instead — a literal check treats an "
        f"AI-disabled platform as Copilot: {offenders}"
    )


@pytest.mark.skipif(not SRC.is_dir(), reason="frontend not checked out")
def test_the_predicate_accepts_both_xtm_one_modes():
    """The point of the whole file. If this narrows, every caller narrows."""
    source = (SRC / _PREDICATE_MODULE).read_text(encoding="utf-8")
    body = source[source.index("export function isXtmOneMode") :][:400]
    assert '"xtm_one"' in body and '"ai_disabled"' in body, (
        "isXtmOneMode must accept both, or ai_disabled renders as Copilot"
    )


@pytest.mark.skipif(not SRC.is_dir(), reason="frontend not checked out")
def test_the_backend_agrees_that_ai_disabled_is_xtm_one():
    """Client and server must not disagree about which product this is."""
    from app.core.config import Settings

    for mode in ("xtm_one", "ai_disabled"):
        assert Settings(platform_mode=mode).is_xtm_one, mode
    assert not Settings(platform_mode="copilot").is_xtm_one
