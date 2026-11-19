"""Platform Health's audit actions and tool group gate carry its name (#101 S1).

The rows written before the rename keep their ``concierge.*`` action: the audit
log is never rewritten, and a reader matches both names.
"""

from app.services.builtin_tool_groups import BUILTIN_TOOL_GROUPS
from app.services.health import audit_actions
from app.services.health.sandbox import dry_run


def test_audit_actions_are_named_after_platform_health():
    names = (
        audit_actions.DATASET_BUILD,
        audit_actions.EVALUATION_LAUNCH,
        audit_actions.DRY_RUN_WRITE,
        audit_actions.SANDBOX_RUN,
    )
    assert all(name.startswith("health.") for name in names)
    assert dry_run.DRY_RUN_AUDIT_ACTION == "health.dry_run_write"


def test_a_reader_matches_the_rows_written_before_the_rename():
    assert set(audit_actions.LEGACY_AUDIT_ACTIONS) == {
        audit_actions.DATASET_BUILD,
        audit_actions.EVALUATION_LAUNCH,
        audit_actions.DRY_RUN_WRITE,
        audit_actions.SANDBOX_RUN,
    }
    assert audit_actions.action_names("health.sandbox_run") == ("health.sandbox_run", "concierge.sandbox_run")
    assert audit_actions.action_names("agent.update") == ("agent.update",)


def test_no_tool_group_is_gated_on_the_former_name():
    gates = {group.gate for group in BUILTIN_TOOL_GROUPS}
    assert "concierge" not in gates
    assert "health" in gates
