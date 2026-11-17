"""Security contract for the seeded-automation agentic-quota exemption.

The exemption MUST be hard-gated on ``Agent.is_builtin`` - a flag only the
startup seeder sets and which is never writable through any API - so a user can
never make their own agent's runs escape the agentic quota, no matter what
trigger context is attached to the run.
"""

from types import SimpleNamespace

from app.services.assignment_engine import _run_is_quota_exempt


def _agent(is_builtin: bool):
    return SimpleNamespace(is_builtin=is_builtin, user_id=None, slug="x", id="x")


def _run(ctx):
    return SimpleNamespace(trigger_context=ctx)


# --- Built-in (seeded) agents: automatic runs are exempt ---------------------


def test_builtin_scheduled_fanout_is_exempt():
    assert _run_is_quota_exempt(_agent(True), _run({"type": "schedule_fanout"})) is True


def test_builtin_schedule_is_exempt():
    assert _run_is_quota_exempt(_agent(True), _run({"type": "schedule"})) is True


def test_builtin_first_run_flag_is_exempt():
    assert (
        _run_is_quota_exempt(
            _agent(True), _run({"type": "briefing_refresh", "quota_exempt": True})
        )
        is True
    )


# --- Built-in agents: deliberate user actions stay metered -------------------


def test_builtin_manual_is_metered():
    assert _run_is_quota_exempt(_agent(True), _run({"type": "manual"})) is False


def test_builtin_refresh_without_flag_is_metered():
    # A deliberate regenerate (no quota_exempt flag) stays metered.
    assert (
        _run_is_quota_exempt(_agent(True), _run({"type": "briefing_refresh"})) is False
    )


def test_builtin_none_context_is_metered():
    assert _run_is_quota_exempt(_agent(True), _run(None)) is False


# --- SECURITY: a non-builtin (user/admin) agent can NEVER be exempt, even if a
# scheduled / quota_exempt context is somehow forged onto its run. ------------


def test_non_builtin_scheduled_is_metered():
    assert _run_is_quota_exempt(_agent(False), _run({"type": "schedule_fanout"})) is False


def test_non_builtin_forged_exempt_flag_is_metered():
    assert (
        _run_is_quota_exempt(
            _agent(False), _run({"type": "schedule", "quota_exempt": True})
        )
        is False
    )


def test_agent_missing_is_builtin_attr_is_metered():
    # Defensive: an object without the attribute must default to metered.
    assert _run_is_quota_exempt(SimpleNamespace(), _run({"type": "schedule"})) is False
