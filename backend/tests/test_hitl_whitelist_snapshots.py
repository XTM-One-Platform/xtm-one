"""The derived exempt sets are reviewed as data, so the snapshots must be current.

``hitl_whitelist_platform`` says the golden file is what "forces every future
change to this set through review". That only holds if something fails when it
goes stale — the script had a ``--check`` mode that nothing ran, and the far
larger integration layer had no snapshot at all.
"""

import re

import pytest

from scripts.dump_platform_read_whitelist import (
    INTEGRATION_SNAPSHOT,
    SNAPSHOT,
    render,
    render_integrations,
)

CASES = [
    pytest.param(SNAPSHOT, render, id="platform"),
    pytest.param(INTEGRATION_SNAPSHOT, render_integrations, id="integration"),
]


@pytest.mark.parametrize(("path", "renderer"), CASES)
def test_the_snapshot_matches_what_is_derived(path, renderer):
    assert path.exists(), f"{path.name} is missing"

    assert path.read_text(encoding="utf-8") == renderer(), (
        f"{path.name} is stale. Run "
        "`python -m scripts.dump_platform_read_whitelist` from backend/ and "
        "READ THE DIFF: every added line is a tool that will run with no human "
        "in the loop, on every deployment, scheduled runs included."
    )


@pytest.mark.parametrize("renderer", [render, render_integrations], ids=["platform", "integration"])
def test_the_header_holds_no_count_that_every_tool_pr_would_rewrite(renderer):
    """A '# N tools' header line changed with every added tool, so two PRs
    adding tools always conflicted on it once one of them merged (#1121)."""
    header = [line for line in renderer().splitlines() if line.startswith("#")]
    counted = [line for line in header if re.search(r"\b\d+ (tools|providers|held|read-classified)\b", line)]

    assert not counted, counted


def test_the_hand_gated_family_is_absent_from_both():
    """A hand-gate that still appeared in a snapshot would be a derivation
    honouring it in one place and not the other."""
    from app.services.hitl_whitelist_platform import HAND_GATED

    for path in (SNAPSHOT, INTEGRATION_SNAPSHOT):
        listed = {
            line.split(":", 1)[1]
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and ":" in line
        }
        assert not (listed & set(HAND_GATED)), f"{path.name} lists a hand-gated tool"
