"""#10659: ``LAB_AI_ADOPTION_SCALE=large`` seeds AI Adoption figures of 1,000 and more.

The planned rows go through the dashboard's own ``activity_score`` and ``ACTIVITY_THRESHOLD``, so a
change that would leave them under the threshold (or the counts under 1,000) fails here, not in a
compact-numbers UI review. The services are the default lab with the switch.
"""

from __future__ import annotations

import ast
import importlib.util
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LAB = ROOT / ".xtm-one" / "lab" / "lab.py"
SCRIPT = LAB.parent / "seed_ai_adoption_large.py"
CHECKS = ROOT / ".xtm-one" / "checks.toml"

pytestmark = pytest.mark.skipif(not LAB.is_file() or not SCRIPT.is_file(), reason="no .xtm-one next to the backend")


def _module():
    spec = importlib.util.spec_from_file_location("lab_seed_ai_adoption_large", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_figure_is_over_1000_and_every_user_row_is_active():
    from app.api.v1.ai_adoption import ACTIVITY_THRESHOLD, activity_score

    module = _module()
    assert module.POOL_USERS > 5000
    for count in (module.AGENTIC_USERS, module.API_KEY_USERS):
        assert 1000 < count <= module.POOL_USERS
    for n in range(module.AGENTIC_USERS):
        assert activity_score(module.platform_usage(n)) >= ACTIVITY_THRESHOLD, n
    # Agentic AI only (#11380): no development provider is seeded.
    assert not hasattr(module, "cursor_usage") and not hasattr(module, "enterprise_daily")
    assert module.DAYS_BACK < 30
    # The top ten keys the API Calls KPI sums read in the thousands.
    assert sum(sorted((module.key_requests(n) for n in range(module.API_KEY_USERS)), reverse=True)[:10]) > 10_000
    assert len({module.pool_email(n) for n in range(module.POOL_USERS)}) == module.POOL_USERS


def test_no_token_authenticates_as_a_seeded_key():
    from app.services.api_key_access import display_prefix, generate_api_key

    module = _module()
    assert not module.KEY_HASH.startswith("$2")
    assert display_prefix(generate_api_key()) != module.KEY_PREFIX


def test_the_lab_starts_the_seed_under_the_switch_and_reads_the_figures_back():
    tree = ast.parse(LAB.read_text(encoding="utf-8"))
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    helper = ast.unparse(functions["seed_ai_adoption_large"])
    for part in (
        "'seed_ai_adoption_large.py'",
        "sys.executable",
        "env=seed_script_env()",
        "/ai-adoption/overview",
        "/ai-adoption/gaps",
        "/activity/stats?scope=platform",
        "seeded ai adoption large figures: ",
        "active_user_count",
        "stderr_summary(process.stderr)",
    ):
        assert part in helper, part
    seeded = ast.unparse(functions["seed_entities"])
    call = "seed_ai_adoption_large(token, base)"
    assert call in seeded and "skipped['ai adoption large figures']" in seeded
    assert "lab_switch('LAB_AI_ADOPTION_SCALE', LAB_AI_ADOPTION_SCALES) == 'large'" in seeded
    assert seeded.index("seed_ai_adoption_low_activity(token, base)") < seeded.index(call) < seeded.index("seeded.json")


def test_the_large_labs_are_the_default_lab_with_the_switch():
    services = tomllib.loads(CHECKS.read_text(encoding="utf-8"))["service"]
    lab = services["lab"]
    twins = {
        "lab-ai-adoption-large": {"LAB_AI_ADOPTION_SCALE": "large"},
        "lab-ai-adoption-large-light": {"LAB_AI_ADOPTION_SCALE": "large", "LAB_THEME": "light"},
    }
    for name, switches in twins.items():
        service = services[name]
        assert service["env"] == {**lab["env"], **switches}, name
        assert {k: v for k, v in service.items() if k not in ("env", "description")} == {
            k: v for k, v in lab.items() if k not in ("env", "description")
        }, name
    assert not any("LAB_AI_ADOPTION_SCALE" in s.get("env", {}) for n, s in services.items() if n not in twins)
