"""Provisioning the built-in content (FR-012).

The extraction out of ``main.py`` moved ten ordered steps whose ordering had no
test — later ones reference entities earlier ones create. These assert the
ordering and that boot delegates rather than inlining, which is what makes the
runtime toggle able to reuse it.
"""

from __future__ import annotations

import inspect
import re

from app.services import provisioning

#: The order matters: each references entities the previous ones created.
#: ``seed_example_flow`` / ``ensure_runner_event_types`` are absent on purpose —
#: they are deterministic-flow infrastructure and stayed in boot, outside the AI
#: gate (FR-014).
EXPECTED = [
    "seed_builtin_templates",
    "seed_builtin_agents",
    "seed_builtin_assignments",
    "seed_builtin_skills",
    "seed_builtin_integrations",
    "seed_builtin_knowledge_bases",
    "seed_builtin_template_containers",
    "seed_builtin_intent_bindings",
    "sync_model_prices",
]


def _calls(source: str) -> list[str]:
    names = "|".join(EXPECTED)
    return list(dict.fromkeys(re.findall(rf"\b({names})\(", source)))


def test_every_step_survived_the_extraction_in_order():
    assert _calls(inspect.getsource(provisioning.provision_builtin_content)) == EXPECTED


def test_boot_delegates_instead_of_inlining():
    """If boot ever re-inlines these, the toggle silently provisions a
    different set — the drift the extraction exists to prevent."""
    from app import main

    source = inspect.getsource(main)
    assert "provision_builtin_content()" in source
    assert not _calls(source), (
        f"main.py calls AI seed steps directly again: {_calls(source)}"
    )


def test_boot_skips_provisioning_when_ai_is_disabled():
    """FR-005: skipped, and never silently — an operator debugging an empty
    platform needs the reason in the log."""
    from app import main

    source = inspect.getsource(main._bootstrap_platform_data)
    assert "is_ai_enabled_strict" in source
    assert "AI is disabled on this deployment" in source


def test_non_ai_boot_steps_are_not_skipped():
    """The bug this test exists for: an early ``return`` in the AI guard also
    skipped the admin upsert, so a fresh AI-disabled install had no admin user
    and nobody could log in. FR-021 — non-AI administration is unaffected.
    """
    from app import main

    source = inspect.getsource(main._bootstrap_platform_data)
    guard = source.index("AI is disabled on this deployment")
    admin = source.index("Upsert admin user from ADMIN_EMAIL")
    assert admin > guard, "sanity: the admin upsert comes after the AI guard"

    between = source[guard:admin]
    assert "\n        return\n" not in between, (
        "an early return here skips the admin upsert too — a fresh "
        "AI-disabled install would have no admin user"
    )
