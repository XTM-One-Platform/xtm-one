"""A remediation option that cites a version the case does not carry is refused (partner 0ae1fa2e).

The Loop wrote "upgrade to 9.10.0 or later" and "residual risk none on the 9.10.0 fix" on a case
whose fixed version is unknown, and recommended closing on it. The option must cite the case's
fixed version, give the source it read (version_source), or name no version.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.services import ctem_remediation_decision as decision_rules
from tests.test_ctem_case_tools import env  # noqa: F401 - fixture


def test_versions_the_case_does_not_carry_are_named() -> None:
    options = [
        {"key": "A", "label": "Upgrade the Macs to 9.10.0 or later", "residual_risk": "none on the 9.10.0 fix"},
        {"key": "B", "label": "Disable HTTP/2 on the frontends"},
    ]
    assert decision_rules.unverified_versions(options, []) == ["9.10.0"]
    assert decision_rules.unverified_versions(options, ["9.10.0"]) == []
    assert decision_rules.unverified_versions(options, ["", "v9.10.0"]) == []
    backed = [{**options[0], "version_source": "vendor release notes 9.10.0"}]
    assert decision_rules.unverified_versions(backed, []) == []
    assert decision_rules.unverified_versions("not a list", []) == []


async def test_the_tool_refuses_an_unbacked_version(env: SimpleNamespace) -> None:  # noqa: F811
    env.case.fixed_version = None
    env.case.fixed_versions = {}
    answer = await env.call(
        "decide_ctem_case_remediation",
        case_id=str(env.case.id),
        options=[{"key": "A", "label": "Upgrade to 9.10.0"}, {"key": "B", "label": "Accept"}],
        recommendation="A",
    )
    assert answer.startswith("Refused: the options cite version(s) 9.10.0") and "version_source" in answer
    assert env.db.commits == 0
