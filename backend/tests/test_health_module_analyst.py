# SPDX-License-Identifier: Apache-2.0

"""Platform Health Module Analyst, S14a-1 (#101, ``dev-docs/platform-health.md`` section 12).

Test (k) of section 12.9 for the allow-list: every entry is a read the shared
classifier agrees with and a tool its provider really defines; a write or an
unlisted tool is refused. Plus the role, the ``module`` target type and the
recommendation store's model.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import get_args

import pytest

from app.models.health_module_analyst import (
    RECOMMENDATION_CONFIDENCES,
    RECOMMENDATION_MAX_CHARS,
    HealthFindingRecommendation,
)
from app.services.health import module_analyst
from app.services.health.cycles import TargetType
from app.services.health.identity import HEALTH_ROLES
from app.services.tool_classification import infer_tool_type

PROVIDERS_DIR = Path(module_analyst.__file__).resolve().parents[2] / "integrations" / "providers"


def _defined_tools(provider: str) -> set[str]:
    names: set[str] = set()
    for path in (PROVIDERS_DIR / provider).rglob("*.py"):
        names |= set(re.findall(r'name="([a-z0-9_]+)"', path.read_text(encoding="utf-8")))
    return names


def test_the_allow_list_covers_exactly_the_module_providers() -> None:
    assert set(module_analyst.MODULE_CHECKER_TOOLS) == set(module_analyst.MODULE_PROVIDERS)


@pytest.mark.parametrize(
    ("provider", "tool"),
    [(p, t) for p, tools in module_analyst.MODULE_CHECKER_TOOLS.items() for t in tools],
)
def test_every_allow_listed_tool_is_a_read_its_provider_defines(provider: str, tool: str) -> None:
    assert infer_tool_type(tool) == "read", f"{tool} classifies as a write: it cannot be allow-listed"
    assert tool in _defined_tools(provider), f"{tool} is not a tool of the {provider} integration"


@pytest.mark.parametrize(
    ("provider", "tool"),
    [
        ("opencti", "manage_opencti_connector"),
        ("opencti", "manage_opencti_playbook"),
        ("openaev", "manage_openaev_settings"),
        ("opencrq", "list_opencrq_users"),
        ("github", "list_github_repos"),
    ],
)
def test_a_write_or_an_unlisted_tool_is_refused(provider: str, tool: str) -> None:
    assert module_analyst.refuse_module_tool(provider, tool) == module_analyst.REFUSAL_REASON


def test_a_mislisted_write_is_still_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lock 1 behind lock 2: a write that slipped into the list stays refused."""
    monkeypatch.setitem(module_analyst.MODULE_CHECKER_TOOLS, "opencti", ("manage_opencti_connector",))
    assert module_analyst.refuse_module_tool("opencti", "manage_opencti_connector") is not None
    assert module_analyst.filter_module_tools("opencti", ["manage_opencti_connector"]) == []


def test_an_allow_listed_read_runs_and_is_kept_in_order() -> None:
    assert module_analyst.refuse_module_tool("opencrq", "list_opencrq_jobs") is None
    names = ["list_opencrq_users", "list_opencrq_jobs", "get_opencrq_platform_version"]
    assert module_analyst.filter_module_tools("opencrq", names) == ["list_opencrq_jobs", "get_opencrq_platform_version"]


def test_the_role_and_the_target_type_exist() -> None:
    assert "module_analyst" in HEALTH_ROLES
    assert "module" in get_args(TargetType)


def test_the_recommendation_store_bounds_its_text_and_confidence() -> None:
    table = HealthFindingRecommendation.metadata.tables["health_finding_recommendations"]
    constraints = {c.name: str(c.sqltext) for c in table.constraints if hasattr(c, "sqltext")}
    assert f"<= {RECOMMENDATION_MAX_CHARS}" in constraints["ck_health_finding_recommendations_text_length"]
    for confidence in RECOMMENDATION_CONFIDENCES:
        assert f"'{confidence}'" in constraints["ck_health_finding_recommendations_confidence_valid"]
    assert not table.foreign_keys


#: Name fragments of a specific connector, feed or third-party source (section 13.4 scope,
#: Sam, 22 Nov 2026): a module checker never reads or judges one.
_OUT_OF_SCOPE = ("connector", "feed", "collector", "injector", "executor", "drata", "cve")


@pytest.mark.parametrize("tool", sorted({t for tools in module_analyst.MODULE_CHECKER_TOOLS.values() for t in tools}))
def test_no_allow_listed_tool_reads_a_connector_or_a_third_party(tool: str) -> None:
    """Test (o) of section 13.9, the tools half."""
    assert not [f for f in _OUT_OF_SCOPE if f in tool], f"{tool} reads outside the module (13.4 scope)"


@pytest.mark.parametrize("provider", module_analyst.MODULE_PROVIDERS)
def test_no_playbook_check_names_a_connector_or_a_third_party(provider: str) -> None:
    """Test (o) of section 13.9, the playbooks half: the check ids of each seeded skill."""
    from app.services.health.check_reports import module_playbook

    text = module_playbook(provider)
    checks = re.findall(r"^\| `([a-z0-9_]+)` \|", text, flags=re.MULTILINE)
    assert checks, f"no check table for {provider} in health-module-checks.md"
    assert not [c for c in checks if any(f in c for f in _OUT_OF_SCOPE)]
